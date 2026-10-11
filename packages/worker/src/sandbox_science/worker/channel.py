# SPDX-License-Identifier: MIT
"""A connection to one executor process: requests, replies and events."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from sandbox_science.contracts import ErrorKind, new_id

from .errors import WorkerError

_log = logging.getLogger(__name__)

MAX_LINE = 64 * 1024 * 1024
REPLY_TIMEOUT = 120.0

Message = dict[str, Any]
EventHandler = Callable[[Message], Awaitable[None]]


class ExecutorChannel:
    """Runs ``argv`` (an executor speaking JSON lines on stdio) and talks to it.

    Every executor process gets a new ``incarnation``: kernels do not survive
    from one incarnation to the next.
    """

    def __init__(
        self,
        argv: Sequence[str],
        on_event: EventHandler,
        on_closed: Callable[[ExecutorChannel], Awaitable[None]],
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.argv = list(argv)
        self.env = dict(env) if env is not None else None
        self.incarnation = new_id("inc")
        self._on_event = on_event
        self._on_closed = on_closed
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Message]] = {}
        self._ids = itertools.count(1)
        self._write_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self.closed = False
        self.hello: Message = {}

    async def start(self) -> Message:
        """Start the executor and return its ``hello`` reply."""
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                limit=MAX_LINE,
            )
        except OSError as exc:
            self.closed = True
            raise WorkerError(ErrorKind.UNAVAILABLE, f"cannot start the executor: {exc}") from exc
        self._tasks = [
            asyncio.ensure_future(self._read_stdout()),
            asyncio.ensure_future(self._read_stderr()),
        ]
        self.hello = await self.request("hello", reply_timeout=REPLY_TIMEOUT)
        return self.hello

    async def request(
        self, op: str, *, reply_timeout: float | None = REPLY_TIMEOUT, **fields: Any
    ) -> Message:
        """Send a request and return its reply; raise WorkerError on failure."""
        process = self._process
        if self.closed or process is None or process.stdin is None:
            raise WorkerError(ErrorKind.UNAVAILABLE, "the executor is not running")
        request_id = next(self._ids)
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        line = json.dumps({"id": request_id, "op": op, **fields}, separators=(",", ":")) + "\n"
        try:
            async with self._write_lock:
                process.stdin.write(line.encode())
                await process.stdin.drain()
            reply = await asyncio.wait_for(future, reply_timeout)
        except (ConnectionError, BrokenPipeError) as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, "the executor went away") from exc
        except TimeoutError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, f"the executor did not answer {op!r}") from exc
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            error = cast(dict[str, Any], reply.get("error") or {})
            try:
                kind = ErrorKind(str(error.get("kind")))
            except ValueError:
                kind = ErrorKind.INTERNAL
            raise WorkerError(kind, str(error.get("message", "the executor refused the request")))
        return reply

    async def close(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                try:
                    decoded: object = json.loads(line)
                except json.JSONDecodeError:
                    _log.warning("executor sent a line that is not JSON: %.200r", line)
                    continue
                if not isinstance(decoded, dict):
                    continue
                message = cast(Message, decoded)
                if message.get("type") == "reply":
                    future = self._pending.get(int(message.get("id") or 0))
                    if future is not None and not future.done():
                        future.set_result(message)
                elif message.get("type") == "event":
                    try:
                        await self._on_event(message)
                    except Exception:
                        _log.exception("handling an executor event failed")
        finally:
            self.closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
                    )
            await self._on_closed(self)

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            _log.debug("executor: %s", line.decode("utf-8", "replace").rstrip())
