# SPDX-License-Identifier: MIT
"""Running the parts: each started when the one before is ready, and stopped in reverse.

Each part runs in a session of its own, so a Ctrl-C at the terminal reaches only the
launcher, which then stops the parts one at a time: SIGTERM, then SIGKILL for whatever
of the part's session is left after its stop timeout. A part's output goes to its log
in the state directory, except lines that hold a secret (the workspace's login
address), which go to a private file of their own instead.

A part is ready when it prints its ready line, or once its address takes connections.
One that stops before it is ready, or later by itself, stops the launcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .config import LauncherError
from .state import Layout, write_private

CHUNK = 65536
TAIL_LINES = 12
POLL = 0.2


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


class Supervisor:
    """The parts started so far, in their order."""

    def __init__(
        self,
        layout: Layout,
        stop: asyncio.Event,
        *,
        stop_timeout: float,
        changed: Callable[[], None] = lambda: None,
    ) -> None:
        self.layout = layout
        self.stop_requested = stop
        self.stop_timeout = stop_timeout
        self.changed = changed
        """Called when a part starts or stops, to keep the instance's record."""
        self.running: list[Running] = []

    async def start(self, part: Part, timeout: float) -> Running:
        """Start ``part`` and wait until it is ready."""
        running = await self._spawn(part)
        self.running.append(running)
        self.changed()
        ready = asyncio.ensure_future(self._ready(running))
        exited = asyncio.ensure_future(running.process.wait())
        stopped = asyncio.ensure_future(self.stop_requested.wait())
        try:
            done, _ = await asyncio.wait(
                {ready, exited, stopped}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiting in (ready, exited, stopped):
                waiting.cancel()
        if ready in done and not ready.cancelled() and ready.exception() is None:
            return running
        if stopped in done:
            raise StopRequested
        if exited in done:
            if running.pump is not None:
                await running.pump
            raise LauncherError(
                f"the {part.title} stopped as it started, with status "
                f"{running.process.returncode}; the end of its log, {running.log}:\n"
                + tail(running.log)
            )
        raise LauncherError(
            f"the {part.title} was not ready within {timeout:g} s; its log: {running.log}"
        )

    async def run(self, name: str, argv: list[str], cwd: Path, timeout: float) -> str:
        """Run a command to its end, its output in the launcher's log, and return it."""
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
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
        if process.returncode != 0:
            shown = "\n".join(f"    {line}" for line in text.splitlines()[-TAIL_LINES:])
            raise LauncherError(f"{name} failed, with status {process.returncode}:\n{shown}")
        return text

    async def watch(self) -> str | None:
        """Wait until the launcher is asked to stop (None) or a part stops by itself."""
        exits = {asyncio.ensure_future(running.process.wait()): running for running in self.running}
        stopped = asyncio.ensure_future(self.stop_requested.wait())
        try:
            done, _ = await asyncio.wait({stopped, *exits}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiting in (stopped, *exits):
                waiting.cancel()
        if stopped in done:
            return None
        running = next(exits[waiting] for waiting in exits if waiting in done)
        if running.pump is not None:
            await running.pump
        return (
            f"the {running.part.title} stopped by itself, with status "
            f"{running.process.returncode}; the end of its log, {running.log}:\n"
            + tail(running.log)
        )

    async def stop(self, *, now: Callable[[], bool] = lambda: False) -> None:
        """Stop every part, the last started first. ``now`` cuts each wait short."""
        while self.running:
            running = self.running.pop()
            await self._stop(running, now)
            self.changed()

    async def _stop(self, running: Running, now: Callable[[], bool]) -> None:
        process = running.process
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + self.stop_timeout
            while process.returncode is None and time.monotonic() < deadline and not now():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(process.wait()), POLL)
            if process.returncode is None:
                print(f"the {running.part.title} did not stop in time; killing it", flush=True)
        # Whatever the part started and left in its session goes with it.
        await asyncio.to_thread(end_session, process.pid)
        await process.wait()
        if running.pump is not None:
            await running.pump

    async def _spawn(self, part: Part) -> Running:
        log = self.layout.log(part.name)
        os.close(_append(log))
        process = await asyncio.create_subprocess_exec(
            *part.argv, cwd=part.cwd, env={**os.environ, **part.env},
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )  # fmt: skip
        running = Running(part, process, log)
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
            while not await reachable(part.address):
                await asyncio.sleep(POLL)


def end_session(session: int) -> None:
    """End every process left in ``session``: SIGTERM, and SIGKILL after a moment."""
    if not _end_session(session, signal.SIGTERM):
        return
    for _ in range(int(2 / POLL)):
        time.sleep(POLL)
        if not _end_session(session, 0):
            return
    _end_session(session, signal.SIGKILL)


def _end_session(session: int, signal_number: int) -> bool:
    """Send ``signal_number`` to the process group ``session`` leads; whether one was there."""
    try:
        os.killpg(session, signal_number)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
