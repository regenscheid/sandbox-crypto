# SPDX-License-Identifier: MIT
"""Where executors run: a local subprocess (tests only) or an OpenShell sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, Protocol

from sandbox_science.contracts import ErrorKind

from . import executor, pdfhelper
from .channel import EventHandler, ExecutorChannel
from .errors import WorkerError
from .package_clients import ClientConfiguration, render
from .settings import LocalBackendSettings, OpenShellBackendSettings

_log = logging.getLogger(__name__)

EXECUTOR_SOURCE = Path(executor.__file__)
PDF_HELPER_SOURCE = Path(pdfhelper.__file__)
SCHED_SHIM_SOURCE = Path(__file__).with_name("shim") / "sched_self.c"
SCHED_SHIM_NAME = "libsandbox-science-sched.so"
COMMAND_TIMEOUT = 120.0
"""Seconds a helper command (the OpenShell CLI, the compiler) may take."""
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ClosedHandler = Callable[[ExecutorChannel], Awaitable[None]]


async def _command(*argv: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Run a helper command to completion, within COMMAND_TIMEOUT; return its code and output."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        raise WorkerError(ErrorKind.UNAVAILABLE, f"cannot run {argv[0]}: {exc}") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise WorkerError(
            ErrorKind.UNAVAILABLE, f"{argv[0]} did not finish within {COMMAND_TIMEOUT:g} s"
        ) from exc
    return process.returncode or 0, output


class Backend(Protocol):
    """Starts executors for logical environments."""

    @property
    def name(self) -> Literal["local", "openshell"]: ...

    async def open(
        self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
    ) -> ExecutorChannel: ...

    def sandbox_id(self, environment_id: str) -> str | None: ...

    @property
    def isolation_tier(self) -> int: ...


class LocalBackend:
    """Runs the executor beside the worker. No isolation at all: development and tests."""

    name: Literal["local"] = "local"

    def __init__(self, settings: LocalBackendSettings, state_dir: Path) -> None:
        self.settings = settings
        self.root = state_dir / "environments"

    async def open(
        self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
    ) -> ExecutorChannel:
        # The ID becomes a directory name: nothing but an identifier may reach the path.
        if not _IDENTIFIER.fullmatch(environment_id):
            raise WorkerError(ErrorKind.INVALID_REQUEST, "invalid environment ID")
        project = self.root / environment_id / "project"
        project.mkdir(parents=True, exist_ok=True)
        # -I keeps the executor's directory off sys.path, so the worker's other
        # modules cannot shadow anything it imports.
        argv = [self.settings.python, "-I", str(EXECUTOR_SOURCE), "--stdio", "--root", str(project)]
        channel = ExecutorChannel(argv, on_event, on_closed, env=_child_env())
        await channel.start()
        return channel

    def sandbox_id(self, environment_id: str) -> str | None:
        return None

    @property
    def isolation_tier(self) -> int:
        return 0


class OpenShellBackend:
    """Copies the executor into a configured sandbox and runs it with ``openshell sandbox exec``."""

    name: Literal["openshell"] = "openshell"

    def __init__(
        self,
        settings: OpenShellBackendSettings,
        state_dir: Path,
        resolve: Callable[[str], str | None] | None = None,
    ) -> None:
        self.settings = settings
        self.state_dir = state_dir
        self.resolve = resolve
        """Finds the sandboxes the worker created (environments.py) before the static map."""
        self._shim_lock = asyncio.Lock()
        self._clients = None if settings.package_cache is None else render(settings.package_cache)

    def _sandbox(self, environment_id: str) -> str:
        sandbox = self.sandbox_id(environment_id)
        if sandbox is None:
            raise WorkerError(
                ErrorKind.UNAVAILABLE,
                f"no sandbox is configured for environment {environment_id!r}",
            )
        return sandbox

    def _env(self) -> dict[str, str]:
        return {**_child_env(), **self.settings.env}

    async def _run(self, *args: str) -> None:
        code, output = await _command(self.settings.cli, *args, env=self._env())
        if code != 0:
            text = output.decode("utf-8", "replace").strip()[-500:]
            raise WorkerError(
                ErrorKind.UNAVAILABLE, f"openshell {args[0]} {args[1]} failed: {text}"
            )

    async def _shim(self) -> Path:
        """Build the scheduler shim once per source revision, with the worker host's compiler.

        OpenShell's seccomp filter refuses pthread_setschedparam, which libzmq
        calls on every thread and aborts on, so no Jupyter kernel could start
        (shim/sched_self.c; lab 4).
        """
        source = SCHED_SHIM_SOURCE.read_bytes()
        target = self.state_dir / "shim" / hashlib.sha256(source).hexdigest()[:16] / SCHED_SHIM_NAME
        async with self._shim_lock:
            if target.exists():
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".tmp")
            code, output = await _command(
                self.settings.cc, "-shared", "-fPIC", "-O2", "-o", str(partial),
                str(SCHED_SHIM_SOURCE),
            )  # fmt: skip
            if code != 0:
                raise WorkerError(
                    ErrorKind.UNAVAILABLE,
                    f"cannot build the scheduler shim: {output.decode('utf-8', 'replace')[-300:]}",
                )
            partial.replace(target)
        return target

    async def open(
        self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
    ) -> ExecutorChannel:
        sandbox = self._sandbox(environment_id)
        settings = self.settings
        await self._run(
            "sandbox", "exec", "-n", sandbox, "--no-tty", "--no-login-shell", "--",
            "mkdir", "-p", settings.executor_dir, settings.project_root,
        )  # fmt: skip
        files = [EXECUTOR_SOURCE, PDF_HELPER_SOURCE]
        if settings.sched_shim:
            files.append(await self._shim())
        for path in files:
            await self._run(
                "sandbox", "upload", "--no-git-ignore", sandbox, str(path),
                f"{settings.executor_dir}/",
            )  # fmt: skip
        argv = [settings.cli, "sandbox", "exec", "-n", sandbox, "--no-tty", "--no-login-shell"]
        if settings.sched_shim:
            argv += ["--env", f"LD_PRELOAD={settings.executor_dir}/{SCHED_SHIM_NAME}"]
        argv += [
            "--", settings.python, "-I", f"{settings.executor_dir}/{EXECUTOR_SOURCE.name}",
            "--stdio", "--root", settings.project_root,
        ]  # fmt: skip
        channel = ExecutorChannel(argv, on_event, on_closed, env=self._env())
        await channel.start()
        if self._clients is not None:
            try:
                await configure_package_clients(channel, self._clients, environment_id)
            except BaseException:
                await channel.close()
                raise
        return channel

    def sandbox_id(self, environment_id: str) -> str | None:
        created = self.resolve(environment_id) if self.resolve is not None else None
        return created or self.settings.sandboxes.get(environment_id)

    def cli_env(self) -> dict[str, str]:
        """The environment the ``openshell`` CLI runs with."""
        return self._env()

    @property
    def isolation_tier(self) -> int:
        return self.settings.isolation_tier


async def configure_package_clients(
    channel: ExecutorChannel, clients: ClientConfiguration, environment_id: str
) -> None:
    """Have the executor write the package clients' configuration (docs/PACKAGE-CACHE.md).

    The executor keeps a file that someone changed since it wrote it, or that
    it did not write; each one is logged, since its client may then miss the cache.
    """
    reply = await channel.request(
        "configure",
        files=[dataclasses.asdict(file) for file in clients.files],
        env=dict(clients.env),
    )
    for path in reply.get("written", ()):
        _log.info("environment %s: wrote %s", environment_id, path)
    for kept in reply.get("skipped", ()):
        _log.warning(
            "environment %s: left %s as it is (%s); check that its client uses the package cache",
            environment_id,
            kept.get("path"),
            kept.get("reason"),
        )


def _child_env() -> dict[str, str]:
    """The worker's environment, without variables that would change Python's behavior."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTHON") and key not in ("VIRTUAL_ENV",)
    }
