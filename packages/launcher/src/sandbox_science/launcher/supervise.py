# SPDX-License-Identifier: MIT
"""Running the parts: each started when the one before is ready, and stopped in reverse.

Each part runs in a session of its own, so a Ctrl-C at the terminal reaches only the
launcher, which then stops the parts one at a time: SIGTERM, then SIGKILL for whatever
of the part's session is left after its stop timeout. A part's output goes to its log
in the state directory, except lines that hold a secret (the workspace's login
address), which go to a private file of their own instead.

A part that listens must find its address free before it starts, and is ready when it
prints its ready line and its address takes connections while it still runs. One that
stops before it is ready, or later by itself, stops the launcher, unless it is one to
start again (the MCP proxy's forward, which exits whenever its sandbox stops): that one
starts again after a delay that doubles while it keeps stopping, and the launcher gives
up on it after ``RESTARTS`` in a row, or at once if something else took its address.
Besides the parts, the launcher can have actions to take when it stops, such as deleting
a sandbox it made; they run in the same reverse order.

Each part dies with the launcher (Linux's ``PR_SET_PDEATHSIG`` or a Darwin
parent-pipe watcher), so a launcher killed outright leaves no part
running unsupervised. A part gets only the environment variables ``ENVIRONMENT`` names
and those the config passes on, never the whole shell's: an API key exported there does
not reach the run service.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import socket
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.clock import wait_or_sleep

from .config import LauncherError
from .processes import members
from .processes import start_time as start_time
from .state import Layout, write_private

CHUNK = 65536
TAIL_LINES = 12
POLL = 0.2
ENVIRONMENT = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR",
        "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    }
)  # fmt: skip
"""The variables of the launcher's environment every part gets, with the ``LC_`` ones."""
RESTART_DELAYS = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
"""Seconds before a part that is started again starts: doubling while it keeps stopping."""
RESTARTS = 10
"""Times in a row a part is started again before the launcher gives up on it."""
STEADY = 60.0
"""Seconds a part must run for its next stop to count as the first in a row again."""
_TRAMPOLINE = (
    "import ctypes, os, sys\n"
    "ctypes.CDLL(None, use_errno=True).prctl(1, 15)  # PR_SET_PDEATHSIG, SIGTERM\n"
    "if os.getppid() != int(sys.argv[1]):\n"
    "    sys.exit(70)  # the launcher died before the signal was set\n"
    "os.execvp(sys.argv[2], sys.argv[2:])\n"
)


def environment() -> dict[str, str]:
    """The launcher's environment as every part gets it: ``ENVIRONMENT`` and ``LC_*``."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in ENVIRONMENT or key.startswith("LC_")
    }


def passed(names: Iterable[str]) -> dict[str, str]:
    """The variables of the launcher's environment a part's ``pass_env`` names."""
    return {name: os.environ[name] for name in names if name in os.environ}


def free(address: str) -> bool:
    """Whether nothing listens on ``address``, so a part may."""
    host, _, port = address.rpartition(":")
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, int(port)))
        except OSError:
            return False
    return True


class StopRequested(Exception):  # noqa: N818 - a request, not an error
    """The launcher was asked to stop while it was starting the parts."""


@dataclass(frozen=True)
class Part:
    name: str
    argv: list[str]
    cwd: Path
    ready_line: str | None = None
    """Text in a line of the part's output that says it is ready."""
    address: str | None = None
    """``host:port`` the part listens on; ready once it takes a connection."""
    private: tuple[re.Pattern[str], Path] | None = None
    """Lines that match go to this file, mode 600, instead of the log."""
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    required: bool = True
    """An optional part exits into degraded status until explicit operator replacement."""
    restart: bool = False
    """Started again when it exits, rather than stopping the launcher."""

    @property
    def title(self) -> str:
        return self.name.replace("-", " ")


@dataclass
class Running:
    part: Part
    process: asyncio.subprocess.Process
    log: Path
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    pump: asyncio.Task[None] | None = None
    since: float = field(default_factory=time.monotonic)
    reported: bool = False
    start_timeout: float = 300
    restarts: int = 0
    parent_pipe: int | None = None
    """Darwin's write end: EOF tells the part its launcher died."""
    """Times in a row it was started again, for a part that is."""


def _append(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)


def tail(path: Path, lines: int = TAIL_LINES) -> str:
    """The last lines of a log, to show with an error."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(f"    {line}" for line in text.splitlines()[-lines:])


async def reachable(address: str, timeout: float = 1.0) -> bool:
    host, _, port = address.rpartition(":")
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def _exited(process: asyncio.subprocess.Process) -> None:
    """Return once the process has exited. ``Process.wait`` also waits until its output
    pipe closes (Python 3.13), which an orphaned child of a killed Darwin watcher keeps
    open until its session is ended: waiting on it then would never end."""
    while process.returncode is None:
        await asyncio.sleep(POLL)


class Supervisor:
    """The parts started so far, in their order."""

    def __init__(
        self,
        layout: Layout,
        stop: asyncio.Event,
        *,
        stop_timeout: float,
        changed: Callable[[], None] = lambda: None,
        env: Mapping[str, str] | None = None,
        clock: Clock = SYSTEM_CLOCK,
        restart_delays: tuple[float, ...] = RESTART_DELAYS,
        restarts: int = RESTARTS,
        steady: float = STEADY,
    ) -> None:
        self.layout = layout
        self.clock, self.restart_delays, self.restarts, self.steady = (
            clock,
            restart_delays,
            restarts,
            steady,
        )
        self.env = dict(environment() if env is None else env)
        """What every part's environment starts from (``environment``)."""
        self.stop_requested = stop
        self.stop_timeout = stop_timeout
        self.changed = changed
        """Called when a part starts or stops, to keep the instance's record."""
        self.running: list[Running] = []
        self._stack: list[Running | tuple[str, Callable[[], Awaitable[object]]]] = []
        """The parts and the actions to take on stopping, in the order they came."""

    def on_stop(self, what: str, action: Callable[[], Awaitable[object]]) -> None:
        """Take ``action`` when the launcher stops, before stopping what came before it."""
        self._stack.append((what, action))

    async def start(self, part: Part, timeout: float) -> Running:
        """Start ``part`` and wait until it is ready."""
        if part.address is not None and not free(part.address):
            raise LauncherError(
                f"something else listens on {part.address}, where the {part.title} would: "
                "stop it, or give the part another address"
            )
        running = await self._spawn(part)
        running.start_timeout = timeout
        self.running.append(running)
        self._stack.append(running)
        self.changed()
        await self._wait_ready(running, timeout)
        return running

    async def _wait_ready(self, running: Running, timeout: float) -> None:
        part = running.part
        ready = asyncio.ensure_future(self._ready(running))
        exited = asyncio.ensure_future(_exited(running.process))
        stopped = asyncio.ensure_future(self.stop_requested.wait())
        try:
            done, _ = await asyncio.wait(
                {ready, exited, stopped}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiting in (ready, exited, stopped):
                waiting.cancel()
        alive = exited not in done and running.process.returncode is None
        if alive and ready in done and not ready.cancelled() and ready.exception() is None:
            return
        if stopped in done:
            raise StopRequested
        if exited in done or running.process.returncode is not None:
            # What it left in its session may hold its output open, as in watch().
            await asyncio.to_thread(end_session, running.process.pid)
            if running.pump is not None:
                await running.pump
            raise LauncherError(
                f"the {part.title} stopped as it started, with status "
                f"{running.process.returncode}; the end of its log, {running.log}:\n"
                + tail(running.log)
            )
        raise LauncherError(
            f"the {part.title} was not ready within {timeout:g} s; "
            f"the end of its log, {running.log}:\n" + tail(running.log)
        )

    async def run(
        self,
        name: str,
        argv: list[str],
        cwd: Path,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Run a command to its end, its output in the launcher's log, and return it;
        ``env`` adds to the parts' environment, as a part's own does."""
        status, text = await self.outcome(name, argv, cwd, timeout, env)
        if status != 0:
            shown = "\n".join(f"    {line}" for line in text.splitlines()[-TAIL_LINES:])
            raise LauncherError(f"{name} failed, with status {status}:\n{shown}")
        return text

    async def outcome(
        self,
        name: str,
        argv: list[str],
        cwd: Path,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run a command to its end, its output in the launcher's log: its exit status and
        its output, whatever the status."""
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env={**self.env, **(env or {})}, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
        talking = asyncio.ensure_future(process.communicate())
        stopped = asyncio.ensure_future(self.stop_requested.wait())
        try:
            done, _ = await asyncio.wait(
                {talking, stopped}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stopped.cancel()
        if talking not in done:
            _end_session(process.pid, signal.SIGKILL)
        output, _ = await talking
        text = output.decode("utf-8", errors="replace")
        with os.fdopen(_append(self.layout.log("launcher")), "w", encoding="utf-8") as log:
            log.write(f"$ {name}\n{text}")
        if stopped in done:
            raise StopRequested
        if talking not in done:
            raise LauncherError(f"{name} took longer than {timeout:g} s")
        return (-1 if process.returncode is None else process.returncode), text

    async def watch(self) -> str | None:
        """Wait until the launcher is asked to stop (None) or a part stops by itself."""
        while True:
            exits = {
                asyncio.ensure_future(_exited(running.process)): running
                for running in self.running
                if not (running.reported and running.process.returncode is not None)
            }
            stopped = asyncio.ensure_future(self.stop_requested.wait())
            try:
                done, _ = await asyncio.wait({stopped, *exits}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiting in (stopped, *exits):
                    waiting.cancel()
            if stopped in done:
                return None
            running = next(exits[waiting] for waiting in exits if waiting in done)
            if running.parent_pipe is not None:
                os.close(running.parent_pipe)
                running.parent_pipe = None
            # A Darwin watcher can be killed while its child still owns the
            # output pipe. Drain the session before waiting for that pipe's EOF.
            await asyncio.to_thread(end_session, running.process.pid)
            if running.pump is not None:
                await running.pump
            if not running.part.required and not running.part.restart:
                running.reported = True
                self.changed()
                print(
                    f"the {running.part.title} stopped; research is interrupted. "
                    "Control and workspace remain available; use sandbox-science "
                    "replace-orchestrator, then explicitly Resume unfinished work.",
                    flush=True,
                )
                continue
            if not running.part.restart:
                return (
                    f"the {running.part.title} stopped by itself, with status "
                    f"{running.process.returncode}; the end of its log, {running.log}:\n"
                    + tail(running.log)
                )
            steady = self.clock.monotonic() - running.since >= self.steady
            restarts = 1 if steady else running.restarts + 1
            if restarts > self.restarts:
                reason = (
                    f"the {running.part.title} kept stopping: it was started again "
                    f"{self.restarts} times in a row; the end of its log, {running.log}:\n"
                    + tail(running.log)
                )
                if running.part.required:
                    return reason
                running.reported = True
                self.changed()
                print(reason + "; remaining services and research VMs retained", flush=True)
                continue
            delay = self.restart_delays[min(restarts, len(self.restart_delays)) - 1]
            print(
                f"the {running.part.title} stopped, with status {running.process.returncode}; "
                f"starting it again in {delay:g} s",
                flush=True,
            )
            if await wait_or_sleep(self.clock, self.stop_requested, delay):
                return None
            address = running.part.address
            if address is not None and not free(address):
                reason = (
                    f"something else listens on {address}, where the {running.part.title} "
                    "would: it is not started again"
                )
                if running.part.required:
                    return reason
                running.reported = True
                self.changed()
                print(reason + "; remaining services and research VMs retained", flush=True)
                continue
            try:
                again = await self._spawn(running.part)
            except OSError as exc:
                # No replacement process was admitted. Keep the dead owned part
                # as the retry identity so this failure consumes the same bound.
                running.restarts = restarts
                running.since = self.clock.monotonic()
                print(f"could not restart {running.part.title}: {exc}", flush=True)
                continue
            again.restarts = restarts
            again.start_timeout = running.start_timeout
            self.running[self.running.index(running)] = again
            self._stack[self._stack.index(running)] = again
            self.changed()
            try:
                await self._wait_ready(again, again.start_timeout)
            except StopRequested:
                return None
            except LauncherError as exc:
                print(str(exc), flush=True)
                await self._stop(again, lambda: False)

    async def stop_part(self, name: str) -> bool:
        """Stop one owned part for explicit replacement; preserve all other services."""
        running = next((item for item in self.running if item.part.name == name), None)
        if running is None:
            return False
        await self._stop(running, lambda: False)
        self.running.remove(running)
        self._stack.remove(running)
        self.changed()
        return True

    async def stop(self, *, now: Callable[[], bool] = lambda: False) -> None:
        """Stop every part and take every action, the last first. ``now`` cuts each part's
        wait short."""
        while self._stack:
            entry = self._stack.pop()
            if isinstance(entry, Running):
                await self._stop(entry, now)
                self.running.remove(entry)
                self.changed()
                continue
            what, action = entry
            try:
                await action()
            except Exception as exc:
                print(f"could not {what}: {exc}", flush=True)

    async def _stop(self, running: Running, now: Callable[[], bool]) -> None:
        process = running.process
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.send_signal(signal.SIGTERM)
            deadline = self.clock.monotonic() + self.stop_timeout
            while process.returncode is None and self.clock.monotonic() < deadline and not now():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(process.wait()), POLL)
            if process.returncode is None:
                print(f"the {running.part.title} did not stop in time; killing it", flush=True)
        # Whatever the part started and left in its session goes with it.
        await asyncio.to_thread(end_session, process.pid)
        await process.wait()
        if running.parent_pipe is not None:
            os.close(running.parent_pipe)
            running.parent_pipe = None
        if running.pump is not None:
            await running.pump

    async def _spawn(self, part: Part) -> Running:
        log = self.layout.log(part.name)
        os.close(_append(log))
        if sys.platform == "darwin":
            read_fd, write_fd = os.pipe()
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-m", "sandbox_science.launcher.parent_watch",
                    str(read_fd), str(os.getpid()), *part.argv,
                    cwd=part.cwd, env={**self.env, **part.env}, pass_fds=(read_fd,),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                )  # fmt: skip
            except BaseException:
                os.close(write_fd)
                raise
            finally:
                os.close(read_fd)
            running = Running(
                part, process, log, since=self.clock.monotonic(), parent_pipe=write_fd
            )
        else:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", _TRAMPOLINE, str(os.getpid()), *part.argv,
                cwd=part.cwd, env={**self.env, **part.env},
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )  # fmt: skip
            running = Running(part, process, log, since=self.clock.monotonic())
        running.pump = asyncio.ensure_future(self._pump(running))
        return running

    async def _pump(self, running: Running) -> None:
        stream = running.process.stdout
        if stream is None:  # never: the part's output is a pipe
            return
        with os.fdopen(_append(running.log), "w", encoding="utf-8") as log:
            pending = b""
            while chunk := await stream.read(CHUNK):
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    self._line(running, line.decode("utf-8", errors="replace"), log)
                log.flush()
            if pending:
                self._line(running, pending.decode("utf-8", errors="replace"), log)

    def _line(self, running: Running, line: str, log: TextIO) -> None:
        part = running.part
        shown = line
        if part.private is not None and part.private[0].search(line):
            write_private(part.private[1], line.strip() + "\n")
            shown = f"[a private line, in {part.private[1]}]"
        print(shown, file=log)
        # Ready only once the line is written, private ones included.
        if part.ready_line is not None and part.ready_line in line:
            running.ready.set()

    async def _ready(self, running: Running) -> None:
        part = running.part
        if part.ready_line is not None:
            await running.ready.wait()
        if part.address is not None:
            while not await reachable(part.address) or running.process.returncode is not None:
                await self.clock.sleep(POLL)


def end_session(session: int) -> None:
    """End every process in the session a part leads: SIGTERM, then SIGKILL two seconds
    later for what is left. A process that started a session of its own (``setsid``)
    has left the part's, and is not found."""
    if not _end_session(session, signal.SIGTERM):
        return
    for _ in range(int(2 / POLL)):
        time.sleep(POLL)
        if not _end_session(session, 0):
            return
    _end_session(session, signal.SIGKILL)


def _end_session(session: int, signal_number: int) -> bool:
    """Send ``signal_number`` to every process in ``session``, whatever its process
    group; whether any was there."""
    found = members(session)
    for pid in found:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal_number)
    return bool(found)
