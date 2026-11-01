# SPDX-License-Identifier: MIT
"""Bounded, cancellation-owned file reads in a fixed sandbox helper process.

This is a byte observation, not proof that another process closed its writer.
Kill/reap can outlast the requested budget on an unkillable filesystem wait; its
owner and admission slot remain until the process actually exits.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
from collections import OrderedDict
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, cast

PROTOCOL = 1
MAX_BYTES = 1 << 30
MAX_RANGE = 8 * 1024 * 1024
MAX_METADATA = 16 * 1024
MAX_ACTIVE = 2
RECENT_IDS = 256
_CAPTURE = re.compile(r"cap-[0-9a-f]{24}")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class CaptureError(Exception):
    """A bounded capture refusal; no retained artifact is implied."""

    def __init__(self, kind: str, message: str, *, cleaned: bool = True) -> None:
        super().__init__(message)
        self.kind, self.message = kind, message[:512]
        self.cleaned = cleaned


async def owned[T](task: Awaitable[T]) -> tuple[T, bool]:
    """Await actual work despite repeated owner cancellation, reporting interruption."""
    future = asyncio.ensure_future(task)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(future), cancelled
        except asyncio.CancelledError:
            if future.done():
                return future.result(), True
            cancelled = True


def _integer(value: object, maximum: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise CaptureError("invalid_request", f"invalid capture {name}")
    return value


def parameters(value: object) -> dict[str, Any]:
    """Validate small fixed helper metadata before any filesystem operation."""
    if not isinstance(value, dict):
        raise CaptureError("invalid_request", "capture metadata must be an object")
    data = cast(dict[str, Any], value)
    if set(data) != {"root", "session", "path", "offset", "length", "max_bytes", "hash"}:
        raise CaptureError("invalid_request", "unexpected capture metadata")
    root, session, path = data["root"], data["session"], data["path"]
    if not isinstance(root, str) or not root.startswith("/") or "\x00" in root:
        raise CaptureError("invalid_request", "invalid capture root")
    if len(root.encode("utf-8")) > 4096:
        raise CaptureError("invalid_request", "capture root is too long")
    if not isinstance(session, str) or _SESSION.fullmatch(session) is None:
        raise CaptureError("invalid_request", "invalid capture session")
    if (
        not isinstance(path, str)
        or not path
        or len(path.encode("utf-8")) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part in ("", ".", "..") for part in path.split("/"))
        or len(path.split("/")) > 128
    ):
        raise CaptureError("invalid_request", "invalid capture path")
    _integer(data["offset"], MAX_BYTES, "offset")
    _integer(data["length"], MAX_RANGE, "length")
    _integer(data["max_bytes"], MAX_BYTES, "byte limit")
    if type(data["hash"]) is not bool:
        raise CaptureError("invalid_request", "invalid capture hash flag")
    return data


def _open(data: dict[str, Any]) -> int:
    path: str = data["path"]
    parts = path.split("/")
    base = ["shared"] if parts[0] == "shared" else ["sessions", data["session"]]
    rest = parts[1:] if parts[0] == "shared" else parts
    if not rest:
        raise CaptureError("invalid_request", "capture requires a regular file")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(data["root"], flags)
    try:
        for name in [*base, *rest[:-1]]:
            child = os.open(name, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(
            rest[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
    finally:
        os.close(directory)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read(data: dict[str, Any]) -> dict[str, Any]:
    """Read at most the source limit plus one byte, retaining only the requested range."""
    data = parameters(data)
    limit, offset, length = data["max_bytes"], data["offset"], data["length"]
    descriptor = _open(data)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CaptureError("invalid_request", "capture source is not a regular file")
        if before.st_size > limit:
            raise CaptureError("invalid_request", "capture source exceeds the byte limit")
        content = bytearray()
        digest = hashlib.sha256() if data["hash"] else None
        position = 0
        if digest is None:
            os.lseek(descriptor, offset, os.SEEK_SET)
            content.extend(os.read(descriptor, min(length, max(0, limit - offset))))
            if len(content) > length or offset + len(content) > limit:
                raise CaptureError("conflict", "capture source grew beyond its range or byte limit")
            size = before.st_size
        else:
            while True:
                block = os.read(descriptor, min(1024 * 1024, limit + 1 - position))
                if not block:
                    break
                end = position + len(block)
                if end > limit:
                    raise CaptureError("conflict", "capture source grew beyond the byte limit")
                digest.update(block)
                if end > offset and position < offset + length:
                    content.extend(block[max(0, offset - position) : offset + length - position])
                position = end
            size = position
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or size != before.st_size:
            raise CaptureError("conflict", "capture source changed while it was read")
        stamp = hashlib.sha256(json.dumps(_identity(before)).encode()).hexdigest()
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "size": size,
            "offset": offset,
            "eof": offset + len(content) >= size,
            "modified": before.st_mtime,
            "sha256": digest.hexdigest() if digest is not None else None,
            "stamp": stamp,
        }
    finally:
        os.close(descriptor)


async def _reap(process: asyncio.subprocess.Process) -> None:
    async def drain() -> None:
        if process.stdout is not None:
            while await process.stdout.read(64 * 1024):
                pass

    # Always drain: a full paused stdout pipe can prevent asyncio.wait from
    # completing even after SIGKILL. Discarding uses one fixed-size chunk.
    draining = asyncio.create_task(drain())
    waiting = asyncio.create_task(process.wait())
    try:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                # Fixed helper code never creates descendants. OpenShell refuses
                # group signals; signal only the process this pool actually owns.
                process.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(waiting), 0.2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
    finally:
        # No false reap claim for kernel-level unkillable I/O. Ownership/slot stays.
        outcomes = await asyncio.gather(waiting, draining, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome


class Pool:
    """Two admitted helpers, with no queue or detached spawn/read/reap ownership."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.work: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.recent: OrderedDict[str, None] = OrderedDict()
        self.closing = False

    def remember(self, capture_id: str) -> None:
        """Bound cancellation tombstones and recent IDs; IDs are never retry tokens."""
        self.recent[capture_id] = None
        self.recent.move_to_end(capture_id)
        while len(self.recent) > RECENT_IDS:
            self.recent.popitem(last=False)

    async def read(
        self, capture_id: object, data: dict[str, Any], timeout: object
    ) -> dict[str, Any]:
        if not isinstance(capture_id, str) or _CAPTURE.fullmatch(capture_id) is None:
            raise CaptureError("invalid_request", "invalid capture ID")
        if capture_id in self.work:
            raise CaptureError("conflict", "capture ID is already active", cleaned=False)
        if capture_id in self.recent:
            raise CaptureError("conflict", "capture ID was already used or cancelled")
        if (
            not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 600
        ):
            raise CaptureError("invalid_request", "invalid capture budget")
        parameters({**data, "root": str(self.root)})
        if self.closing or len(self.work) >= MAX_ACTIVE:
            raise CaptureError("unavailable", "capture admission limit reached or executor closing")
        task = asyncio.create_task(self._run(data, float(timeout)))
        self.work[capture_id] = task

        def finished(done: asyncio.Task[dict[str, Any]]) -> None:
            self.work.pop(capture_id, None)
            self.remember(capture_id)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await owned(task)
            raise

    async def _run(self, data: dict[str, Any], timeout: float) -> dict[str, Any]:
        metadata = json.dumps({**data, "root": str(self.root)}, ensure_ascii=False)
        if len(metadata.encode()) > MAX_METADATA:
            raise CaptureError("invalid_request", "capture metadata is too large")
        process: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(timeout):
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        "-S",
                        str(Path(__file__).resolve()),
                        "--helper",
                        metadata,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        env={"LANG": "C.UTF-8"},
                        cwd="/",
                        start_new_session=True,
                    )
                )
                process, cancelled = await owned(spawning)
                if cancelled:
                    raise asyncio.CancelledError
                if process.stdout is None:
                    raise CaptureError("internal", "capture helper has no stdout")
                output = bytearray()
                maximum = 4 * ((data["length"] + 2) // 3) + MAX_METADATA
                while chunk := await process.stdout.read(64 * 1024):
                    if len(output) + len(chunk) > maximum:
                        raise CaptureError("internal", "capture helper reply is too large")
                    output.extend(chunk)
                status = await process.wait()
                if status != 0:
                    raise CaptureError("internal", f"capture helper exited with status {status}")
                try:
                    raw: object = json.loads(output)
                except (ValueError, RecursionError) as exc:
                    raise CaptureError("internal", "invalid capture helper reply") from exc
                if not isinstance(raw, dict):
                    raise CaptureError("internal", "invalid capture helper reply")
                reply = cast(dict[str, Any], raw)
                if type(reply.get("ok")) is not bool:
                    raise CaptureError("internal", "invalid capture helper reply")
                if reply["ok"] is not True:
                    raw_error: object = reply.get("error")
                    if not isinstance(raw_error, dict):
                        raise CaptureError("internal", "invalid capture helper error")
                    error = cast(dict[str, Any], raw_error)
                    kind, message = error.get("kind"), error.get("message")
                    if (
                        not isinstance(kind, str)
                        or kind not in {"not_found", "invalid_request", "conflict"}
                        or not isinstance(message, str)
                        or len(message) > 512
                    ):
                        raise CaptureError("internal", "invalid capture helper error")
                    raise CaptureError(kind, message)
                result: object = reply.get("result")
                if not isinstance(result, dict):
                    raise CaptureError("internal", "invalid capture helper result")
                return cast(dict[str, Any], result)
        except TimeoutError as exc:
            raise CaptureError("timed_out", "capture budget expired") from exc
        finally:
            if process is not None:
                _, cancelled = await owned(asyncio.create_task(_reap(process)))
                if cancelled:
                    raise asyncio.CancelledError

    async def read_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """A capture endpoint's bounded read and cleanup acknowledgement payload."""
        data = {
            key: request.get(key)
            for key in ("session", "path", "offset", "length", "max_bytes", "hash")
        }
        try:
            result = await self.read(request.get("capture_id"), data, request.get("timeout"))
        except asyncio.CancelledError:
            return {
                "capture_id": request.get("capture_id"),
                "cleaned": True,
                "capture_error": {
                    "kind": "cancelled",
                    "message": "capture cancelled after cleanup",
                },
            }
        except CaptureError as exc:
            return {
                "capture_id": request.get("capture_id"),
                "cleaned": exc.cleaned,
                "capture_error": {"kind": exc.kind, "message": exc.message},
            }
        return {**result, "capture_id": request["capture_id"], "cleaned": True}

    async def cancel_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """End only the named helper, acknowledging cleanup after it is reaped."""
        capture_id = request.get("capture_id")
        if not isinstance(capture_id, str) or re.fullmatch(r"cap-[0-9a-f]{24}", capture_id) is None:
            raise CaptureError("invalid_request", "invalid capture ID")
        await self.cancel(capture_id)
        return {"capture_id": capture_id, "cleaned": True}

    async def cancel(self, capture_id: str) -> None:
        """Cancel only a helper owned by this pool, and await its actual cleanup."""
        self.remember(capture_id)
        task = self.work.get(capture_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await owned(task)

    async def close(self) -> None:
        """Drain actual admitted work before the executor can shut down."""
        self.closing = True
        await self.cancel_active()

    async def cancel_active(self) -> None:
        """Drain the current controller's helpers without disabling a later controller."""
        tasks = list(self.work.values())
        for task in tasks:
            task.cancel()
        _, cancelled = await owned(asyncio.gather(*tasks, return_exceptions=True))
        if cancelled:
            raise asyncio.CancelledError


def main(argv: list[str]) -> int:
    """Fixed helper entry point; input and output are bounded and no rich code is loaded."""
    try:
        if len(argv) != 2 or argv[0] != "--helper" or len(argv[1].encode()) > MAX_METADATA:
            raise CaptureError("invalid_request", "invalid capture helper arguments")
        result = read(parameters(json.loads(argv[1])))
        reply: dict[str, Any] = {"ok": True, "result": result}
    except FileNotFoundError:
        reply = {"ok": False, "error": {"kind": "not_found", "message": "capture source not found"}}
    except CaptureError as exc:
        reply = {"ok": False, "error": {"kind": exc.kind, "message": exc.message}}
    except (ValueError, RecursionError, UnicodeError, OSError) as exc:
        reply = {
            "ok": False,
            "error": {
                "kind": "invalid_request",
                "message": f"capture refused ({type(exc).__name__})",
            },
        }
    sys.stdout.buffer.write(
        json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
