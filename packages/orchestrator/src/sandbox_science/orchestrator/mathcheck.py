# SPDX-License-Identifier: MIT
"""Bounded mechanical KaTeX checks in a separate, operator-configured Node process.

This is the D82 renderer boundary, not finding storage or save-time scheduling.
No text reaches a shell, model, network endpoint or JavaScript evaluator.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import stat
import tempfile
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from sandbox_science.contracts import Contract

MAX_INPUT = 262_144
MAX_OUTPUT = 32_768
MAX_BATCH = 32
ENGINE = "0.18.9"
OPTIONS = {
    "throwOnError": True,
    "trust": False,
    "strict": "ignore",
    "maxSize": 20,
    "maxExpand": 1000,
    "output": "htmlAndMathml",
}


class Formula(Contract):
    """One pinned span's text and effective document macros; IDs are caller-owned."""

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    tex: str = Field(max_length=20_000)
    display: bool = False
    macros: dict[str, str] = Field(default_factory=dict, max_length=200)

    @field_validator("tex")
    @classmethod
    def utf8_text(cls, value: str) -> str:
        value.encode("utf-8")
        return value

    @field_validator("macros")
    @classmethod
    def bounded_macros(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(key) > 100 or len(text) > 1000 for key, text in value.items()):
            raise ValueError("macro name/definition exceeds renderer limit")
        for key, text in value.items():
            key.encode("utf-8")
            text.encode("utf-8")
        return value


class _Answer(Contract):
    protocol: Literal[1]
    id: str
    engine: str
    runtime: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    options: dict[str, bool | int | str]
    max_formula: Literal[5000]
    status: Literal["accepted", "rejected", "unavailable"]
    error: str | None = Field(max_length=300)
    remedy: str | None = Field(max_length=5000)

    @field_validator("protocol", "max_formula", mode="before")
    @classmethod
    def exact_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("protocol limits must be JSON integers")
        return value

    @field_validator("options", mode="before")
    @classmethod
    def exact_options(cls, value: object) -> object:
        if json.dumps(value, sort_keys=True) != json.dumps(OPTIONS, sort_keys=True):
            raise ValueError("renderer settings mismatch")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status == "accepted" and (self.error is not None or self.remedy is not None):
            raise ValueError("accepted math cannot carry an error or repair")
        if self.status != "accepted" and not self.error:
            raise ValueError("non-accepted math needs a reason")
        if self.status == "unavailable" and self.remedy is not None:
            raise ValueError("an inconclusive check cannot suggest a verified repair")
        return self


class MathCheck(Contract):
    """A checked observation. Unavailable is inconclusive, never a passing check."""

    id: str
    status: Literal["accepted", "rejected", "unavailable"]
    error: str | None = None
    remedy: str | None = None
    input_sha256: str
    bundle_sha256: str | None
    launcher_sha256: str | None = None
    engine: str | None = None
    runtime: str | None = None


class _PipeLimitError(Exception):
    pass


async def _read(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        raise _PipeLimitError("checker pipe missing")
    value = bytearray()
    while chunk := await stream.read(min(4096, limit + 1 - len(value))):
        value.extend(chunk)
        if len(value) > limit:
            raise _PipeLimitError("checker pipe exceeded limit")
    return bytes(value)


async def _exchange(process: asyncio.subprocess.Process, data: bytes) -> bytes:
    async def send() -> None:
        if process.stdin is None:
            raise _PipeLimitError("checker stdin missing")
        process.stdin.write(data)
        await process.stdin.drain()
        process.stdin.close()

    async with asyncio.TaskGroup() as group:
        stdout = group.create_task(_read(process.stdout, MAX_OUTPUT))
        group.create_task(_read(process.stderr, 4096))
        group.create_task(send())
    await process.wait()
    return stdout.result()


async def _reap(process: asyncio.subprocess.Process) -> None:
    # Also reap descendants remaining in the launcher's process group.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)

    async def discard(stream: asyncio.StreamReader | None) -> None:
        # A paused pipe can prevent Process.wait() from completing on Python 3.12.
        # The exchange tasks have ended; drain without retaining the remaining bytes.
        if stream is not None:
            while await stream.read(4096):
                pass

    await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())


async def _owned[T](task: asyncio.Task[T]) -> tuple[T, bool]:
    """Finish ownership handoff/cleanup before propagating repeated cancellation."""
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


def _code(path: Path, limit: int) -> bytes:
    """Configuration must be an ordinary bounded file, never a blocking device/FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("math code is not a regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("math code exceeds byte limit")
    return data


class MathChecker:
    """At most two processes and 32 admitted calls; fixed byte and time budgets.

    Paths are trusted service configuration. The V8 old-space cap is 96 MiB;
    it is not an operating-system RSS limit. Each formula gets a fresh process,
    so state, macro definitions and caches cannot leak between projects.
    """

    def __init__(self, node: Path, launcher: Path, bundle: Path, *, timeout: float = 2.0) -> None:
        if not 0.05 <= timeout <= 10:
            raise ValueError("checker deadline must be between 0.05 and 10 seconds")
        self.node = node.resolve()
        self.launcher = launcher.resolve()
        self.bundle = bundle.resolve()
        self.timeout = timeout
        self._slots = asyncio.Semaphore(2)
        self._pending = 0

    async def check_many(self, formulas: list[Formula]) -> list[MathCheck]:
        """Check a bounded batch in input order; duplicate IDs are refused."""
        if len(formulas) > MAX_BATCH or len({item.id for item in formulas}) != len(formulas):
            raise ValueError("math batch exceeds 32 spans or repeats an ID")
        return list(await asyncio.gather(*(self.check(item) for item in formulas)))

    async def check(self, formula: Formula) -> MathCheck:
        """Return a mechanical observation or an explicit unavailable reason."""
        data = formula.model_dump_json().encode()
        digest = hashlib.sha256(data).hexdigest()
        bundle_digest: str | None = None
        launcher_digest: str | None = None

        def unavailable(reason: str) -> MathCheck:
            return MathCheck(
                id=formula.id,
                status="unavailable",
                error=reason,
                input_sha256=digest,
                bundle_sha256=bundle_digest,
                launcher_sha256=launcher_digest,
            )

        if len(data) > MAX_INPUT:
            return unavailable("math request exceeds byte limit")
        if self._pending >= MAX_BATCH:
            return unavailable("math checker admission limit reached")
        self._pending += 1
        try:
            async with asyncio.timeout(self.timeout):
                async with self._slots:
                    code = _code(self.bundle, 8 * 1024 * 1024)
                    launcher_code = _code(self.launcher, 65_536)
                    bundle_digest = hashlib.sha256(code).hexdigest()
                    launcher_digest = hashlib.sha256(launcher_code).hexdigest()
                    # Pin the exact bytes for this call even during an operator rollout.
                    with tempfile.TemporaryDirectory(prefix="ss-mathcheck-") as directory:
                        pinned = Path(directory)
                        launcher = pinned / "check-math.mjs"
                        bundle = pinned / "mathcheck.mjs"
                        launcher.write_bytes(launcher_code)
                        bundle.write_bytes(code)
                        return await self._run(
                            formula, data, digest, bundle_digest, launcher_digest, launcher, bundle
                        )
        except TimeoutError:
            return unavailable("math checker deadline exceeded")
        except (OSError, ValueError, RecursionError, ExceptionGroup):
            return unavailable("math checker unavailable or invalid response")
        finally:
            self._pending -= 1

    async def _run(
        self,
        formula: Formula,
        data: bytes,
        digest: str,
        bundle_digest: str,
        launcher_digest: str,
        launcher: Path,
        bundle: Path,
    ) -> MathCheck:
        # This helper runs inside the caller's queue-inclusive deadline.
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(self.node),
                "--max-old-space-size=96",
                "--max-semi-space-size=4",
                str(launcher),
                str(bundle),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"LANG": "C.UTF-8"},
                cwd=launcher.parent,
                start_new_session=True,
            )
        )
        process, cancelled = await _owned(spawning)
        if cancelled:
            await _owned(asyncio.create_task(_reap(process)))
            raise asyncio.CancelledError
        try:
            output = await _exchange(process, data)
        finally:
            _, cleanup_cancelled = await _owned(asyncio.create_task(_reap(process)))
            if cleanup_cancelled:
                raise asyncio.CancelledError
        if process.returncode != 0:
            raise ValueError("math checker process failed")
        answer = _Answer.model_validate(json.loads(output))
        major, minor, _ = (int(part) for part in answer.runtime.split("."))
        if (
            answer.id != formula.id
            or answer.engine != ENGINE
            or (major, minor) < (22, 12)
            or json.dumps(answer.options, sort_keys=True) != json.dumps(OPTIONS, sort_keys=True)
        ):
            raise ValueError("math checker identity/version/settings mismatch")
        return MathCheck(
            id=formula.id,
            status=answer.status,
            error=answer.error,
            remedy=answer.remedy,
            input_sha256=digest,
            bundle_sha256=bundle_digest,
            launcher_sha256=launcher_digest,
            engine=answer.engine,
            runtime=answer.runtime,
        )
