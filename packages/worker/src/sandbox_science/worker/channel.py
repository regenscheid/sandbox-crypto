# SPDX-License-Identifier: MIT
"""A connection to one executor process: requests, replies and events.

Everything an executor sends is untrusted: research code shares its sandbox
and can take it over. Each line is decoded on its own and within bounds; a
line that does not decode is dropped, and the connection carries on.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from sandbox_science.contracts import ErrorKind, new_id

from .errors import WorkerError

_log = logging.getLogger(__name__)

MAX_LINE = 16 * 1024 * 1024
"""The longest line accepted from an executor. The executor keeps its own lines shorter."""
MAX_CONTAINERS = 100_000
"""Objects and arrays one line may hold, so a line cannot expand into millions of objects."""
REPLY_TIMEOUT = 120.0
CLOSE_GRACE = 15.0
"""Seconds an executor gets to stop its commands and kernels once its stdin closes."""
MAX_ERROR_MESSAGE = 2000

_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"')
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F]")
_SURROGATE = re.compile("[\ud800-\udfff]")

Message = dict[str, Any]
EventHandler = Callable[[Message], Awaitable[None]]


class ProtocolError(ValueError):
    """A line from the executor that is not a usable message."""


def decode_line(line: bytes) -> Message:
    """Decode one line from an executor into a message, or raise ProtocolError.

    Invalid UTF-8 and lone surrogates become U+FFFD, and non-finite numbers
    become None, so that every value can be stored and sent on as JSON.
    """
    if line.count(b"{") + line.count(b"[") > MAX_CONTAINERS:
        # Brackets inside strings counted too: count again without the strings.
        outside = _STRING.sub(b"", line)
        if outside.count(b"{") + outside.count(b"[") > MAX_CONTAINERS:
            raise ProtocolError(f"more than {MAX_CONTAINERS} objects and arrays")
    text = line.decode("utf-8", "replace")
    try:
        decoded: object = json.loads(text, parse_constant=_no_constant, parse_float=_finite)
        if _SURROGATE_ESCAPE.search(text):
            decoded = _without_surrogates(decoded)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError, long integers, deep nesting
        raise ProtocolError(f"not JSON: {str(exc)[:200]}") from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("not a JSON object")
    return cast(Message, decoded)


def _no_constant(_: str) -> None:
    """NaN and Infinity, which JSON does not have, become null."""


def _finite(text: str) -> float | None:
    value = float(text)
    return value if math.isfinite(value) else None


def _without_surrogates(value: object) -> object:
    if isinstance(value, str):
        return _SURROGATE.sub("�", value)
    if isinstance(value, list):
        return [_without_surrogates(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return {
            _without_surrogates(key): _without_surrogates(item)
            for key, item in cast(dict[object, object], value).items()
        }
    return value


class ExecutorChannel:
    """Runs ``argv`` (an executor speaking JSON lines on stdio) and talks to it.

    Every executor process gets a new ``incarnation``: kernels do not survive
    from one incarnation to the next. Whenever reading from the executor ends,
    for whatever reason, the process is stopped too.
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
        self._stopping: asyncio.Future[None] | None = None
        self._dropped = 0
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
        try:
            self.hello = await self.request("hello", reply_timeout=REPLY_TIMEOUT)
        except BaseException:
            await self.close()
            raise
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
        if reply.get("ok") is not True:
            raw = reply.get("error")
            error = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
            try:
                kind = ErrorKind(str(error.get("kind")))
            except ValueError:
                kind = ErrorKind.INTERNAL
            message = str(error.get("message", "the executor refused the request"))
            raise WorkerError(kind, message[:MAX_ERROR_MESSAGE])
        return reply

    async def close(self) -> None:
        """Stop the executor: close its stdin, and kill it if it does not exit in time."""
        self.closed = True
        await self._stop()
        if self._tasks:
            # Orphaned kernels could hold stderr open: do not wait for them.
            _, pending = await asyncio.wait(self._tasks, timeout=5)
            for task in pending:
                task.cancel()

    async def _stop(self) -> None:
        if self._stopping is None:
            self._stopping = asyncio.ensure_future(self._terminate())
        await asyncio.shield(self._stopping)

    async def _terminate(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), CLOSE_GRACE)
        except TimeoutError:
            _log.warning("the executor did not exit within %g s; killing it", CLOSE_GRACE)
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError:
                    # Over MAX_LINE: asyncio has discarded what it buffered of the line.
                    self._drop(f"a line over {MAX_LINE} bytes", b"")
                    continue
                if not line:
                    break
                await self._receive(line)
        except Exception:
            _log.exception("reading from the executor failed")
        finally:
            self.closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
                    )
            try:
                await self._on_closed(self)
            finally:
                await self._stop()

    async def _receive(self, line: bytes) -> None:
        try:
            message = decode_line(line)
        except ProtocolError as exc:
            self._drop(str(exc), line)
            return
        kind = message.get("type")
        if kind == "reply":
            request_id = message.get("id")
            if not isinstance(request_id, int) or isinstance(request_id, bool):
                self._drop("a reply without an integer id", line)
                return
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(message)
        elif kind == "event":
            try:
                await self._on_event(message)
            except Exception:
                _log.exception("handling an executor event failed")

    def _drop(self, reason: str, line: bytes) -> None:
        self._dropped += 1
        if self._dropped <= 10 or self._dropped % 1000 == 0:
            _log.warning(
                "dropped %s from the executor (%d so far): %r",
                reason,
                self._dropped,
                line[:200],
            )

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            try:
                line = await process.stderr.readline()
            except ValueError:
                continue  # a line over the limit, already discarded
            except Exception:
                _log.exception("reading the executor's stderr failed")
                return
            if not line:
                return
            _log.debug("executor: %s", line[:2000].decode("utf-8", "replace").rstrip())
