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
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Final, cast

from sandbox_science.contracts import ErrorKind, new_id

from . import request_frames
from .errors import ExecutorRefusalError, WorkerError
from .recovery_fence import check_dispatch
from .timeouts import seconds
from .transfers import offload

_log = logging.getLogger(__name__)

MAX_LINE = 16 * 1024 * 1024
"""The longest line accepted from an executor. The executor keeps its own lines shorter."""
MAX_CONTAINERS = 100_000
"""Objects and arrays one line may hold, so a line cannot expand into millions of objects."""
REPLY_TIMEOUT = 120.0
WRITE_TIMEOUT = 30.0
"""How long a request may wait to be written: an executor that stopped reading its stdin
(research code can stop it) must not hold up its callers."""
CLOSE_GRACE = 15.0
"""Seconds an executor gets to stop its commands and kernels once its stdin closes."""
MAX_ERROR_MESSAGE = 2000


@dataclass(frozen=True)
class ChannelTimeouts:
    """Seconds a channel waits for its executor; the defaults are the production values."""

    write: float = WRITE_TIMEOUT
    transfer_write: float = 120.0
    transfer_reply: float = 240.0
    close_grace: float = CLOSE_GRACE

    def __post_init__(self) -> None:
        seconds("write", self.write)
        seconds("transfer_write", self.transfer_write)
        seconds("transfer_reply", self.transfer_reply)
        seconds("close_grace", self.close_grace)


CHANNEL_TIMEOUTS: Final = ChannelTimeouts()
"""The production values."""

_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"')
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F]")
_SURROGATE = re.compile("[\ud800-\udfff]")

Message = dict[str, Any]
EventHandler = Callable[[Message], Awaitable[None]]


@dataclass
class _EventScope:
    active: bool = True


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
        daemon_token: str | None = None,
        framed: bool = False,
        timeouts: ChannelTimeouts = CHANNEL_TIMEOUTS,
    ) -> None:
        self.framed = framed
        self._ack: tuple[str, int, int, asyncio.Future[None]] | None = None
        self._events: asyncio.Queue[tuple[Message, int]] = asyncio.Queue(maxsize=128)
        self._event_bytes = 0
        self._event_received = 0
        self._event_delivered = 0
        self._event_progress = asyncio.Event()
        self._reply_barriers: dict[int, int] = {}
        self._in_event: ContextVar[_EventScope | None] = ContextVar(
            "executor_event_callback", default=None
        )
        self._event_active = False
        self._event_task: asyncio.Task[None] | None = None
        self.daemon_token = daemon_token
        self.timeouts = timeouts
        self.detaching = False
        self.bridge_id = new_id("bridge")
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
        self._abandoned: asyncio.Task[None] | None = None
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
        if self.framed:
            self._event_task = asyncio.create_task(self._deliver_events())
            self._tasks.append(self._event_task)
        try:
            fields = {"daemon_token": self.daemon_token} if self.daemon_token else {}
            self.hello = await self.request("hello", reply_timeout=REPLY_TIMEOUT, **fields)
            if self.daemon_token is not None:
                incarnation = self.hello.get("daemon_incarnation")
                if (
                    not isinstance(incarnation, str)
                    or re.fullmatch(r"inc-[0-9a-f]{24}", incarnation) is None
                ):
                    raise WorkerError(ErrorKind.UNAVAILABLE, "invalid daemon incarnation")
                self.incarnation = incarnation
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
        large_write = op == "write" and len(str(fields.get("content_b64", ""))) > 8 * 1024 * 1024
        if large_write:
            capacity = self.hello.get("max_request_bytes", 64 * 1024 * 1024)
            if (
                not isinstance(capacity, int)
                or len(str(fields.get("content_b64", ""))) + 65536 > capacity
            ):
                raise WorkerError(
                    ErrorKind.UNSUPPORTED, "recreate this environment to accept larger file uploads"
                )
            if reply_timeout == REPLY_TIMEOUT:
                reply_timeout = self.timeouts.transfer_reply
        write_timeout = self.timeouts.transfer_write if large_write else self.timeouts.write
        request_id = next(self._ids)

        def encode() -> bytes:
            return (
                json.dumps({"id": request_id, "op": op, **fields}, separators=(",", ":")) + "\n"
            ).encode()

        line = await offload(encode) if large_write else encode()
        if self.framed:
            capacity = self.hello.get(
                "max_request_bytes", 64 * 1024 * 1024 if self.hello else 65536
            )
            if type(capacity) is not int or len(line) > capacity:
                raise WorkerError(
                    ErrorKind.INVALID_REQUEST, "executor request exceeds its byte limit"
                )
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        writing_started = False
        try:
            try:
                async with asyncio.timeout(write_timeout), self._write_lock:
                    if self.closed:
                        raise WorkerError(ErrorKind.UNAVAILABLE, "the executor transport is closed")
                    check_dispatch(op)
                    writing_started = True
                    if self.framed:
                        await self._write_frames(request_id, op, line)
                    else:
                        process.stdin.write(line)
                        await process.stdin.drain()
            except TimeoutError as exc:
                # Waiting behind a large frame has written nothing and must not kill
                # its sender's healthy executor. Only a partial frame makes it unusable.
                if writing_started:
                    self.closed = True
                    self._abandoned = asyncio.ensure_future(self.close())
                else:
                    raise WorkerError(
                        ErrorKind.UNAVAILABLE, "the executor transport is busy"
                    ) from exc
                raise WorkerError(
                    ErrorKind.UNAVAILABLE, "the executor stopped reading its requests"
                ) from exc
            except asyncio.CancelledError:
                if writing_started:
                    self.closed = True
                    self._abandoned = asyncio.ensure_future(self.close())
                raise
            except BaseException:
                if self.framed and writing_started:
                    self.closed = True
                    self._abandoned = asyncio.ensure_future(self.close())
                raise
            async with asyncio.timeout(reply_timeout):
                reply = await future
                scope = self._in_event.get()
                if self.framed and not (scope is not None and scope.active):
                    await self._wait_events(self._reply_barriers.get(request_id, 0))
        except (ConnectionError, BrokenPipeError) as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, "the executor went away") from exc
        except TimeoutError as exc:
            raise WorkerError(ErrorKind.UNAVAILABLE, f"the executor did not answer {op!r}") from exc
        finally:
            self._pending.pop(request_id, None)
            self._reply_barriers.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # a failed transfer may never reach its reply await
        if reply.get("ok") is not True:
            raw = reply.get("error")
            error = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
            try:
                kind = ErrorKind(str(error.get("kind")))
            except ValueError:
                kind = ErrorKind.INTERNAL
            message = str(error.get("message", "the executor refused the request"))
            raise ExecutorRefusalError(kind, message[:MAX_ERROR_MESSAGE])
        return reply

    async def _write_frames(self, request_id: int, op: str, line: bytes) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise ConnectionError("executor disappeared")
        transfer = secrets.token_hex(16)
        for seq, offset in enumerate(range(0, len(line), request_frames.CHUNK_BYTES)):
            chunk = line[offset : offset + request_frames.CHUNK_BYTES]
            final = offset + len(chunk) == len(line)
            # The final frame is the first point at which the bridge may dispatch.
            # Recheck authority after every ACK wait, including before that frame.
            await asyncio.sleep(0)  # observe cancellation before admitting another frame
            check_dispatch(op)
            ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._ack = transfer, request_id, seq, ack
            try:
                process.stdin.write(request_frames.frame(transfer, request_id, seq, final, chunk))
                await process.stdin.drain()
                await ack
            finally:
                self._ack = None

    async def _deliver_events(self) -> None:
        while True:
            message, size = await self._events.get()
            scope = _EventScope()
            token = self._in_event.set(scope)
            self._event_active = True
            processed = False
            try:
                await self._on_event(message)
                processed = True
            except Exception:
                processed = True  # preserve the existing logged-handler-failure behavior
                _log.exception("handling an executor event failed")
            finally:
                scope.active = False
                self._in_event.reset(token)
                self._event_active = False
                self._event_bytes -= size
                if processed:
                    self._event_delivered += 1
                self._event_progress.set()
                self._events.task_done()
            if self.closed:
                return

    async def _wait_events(self, target: int) -> None:
        # Normal replay replies retain the old ordering: preceding event callbacks
        # have committed before the RPC returns. Callback-issued RPCs must bypass
        # their own barrier or they would wait for themselves.
        while self._event_delivered < target:
            if self.closed:
                raise WorkerError(ErrorKind.UNAVAILABLE, "executor event delivery interrupted")
            self._event_progress.clear()
            await self._event_progress.wait()

    async def _stop_events(self) -> None:
        while not self._events.empty():
            _message, size = self._events.get_nowait()
            self._event_bytes -= size
            self._events.task_done()
        self._event_progress.set()
        task = self._event_task
        if task is None or task is asyncio.current_task():
            return
        # The current callback may own a package offload/SQL continuation. Drain
        # it before recovery can install a replacement bridge; only idle gets
        # cancelled. Closed request futures already unblock nested RPCs.
        if not self._event_active:
            task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass  # joining remains owned even under repeated shutdown cancellation
            except Exception:
                break
        with contextlib.suppress(asyncio.CancelledError):
            task.result()

    async def detach(self) -> None:
        """Close only the stdio bridge; a daemon keeps its research processes alive."""
        self.detaching = True
        await self.close()

    async def shutdown(self) -> None:
        """Stop the daemon and its processes, then the bridge (environment lifecycle)."""
        if self.daemon_token is not None and not self.closed:
            with contextlib.suppress(WorkerError):
                await self.request("daemon_shutdown", reply_timeout=self.timeouts.close_grace)
        self.detaching = True
        await self.close()

    async def close(self) -> None:
        """Stop the executor: close its stdin, and kill it if it does not exit in time."""
        self.closed = True
        await self._stop()
        await self._stop_events()
        if asyncio.current_task() is self._event_task:
            return  # stdout's close path joins this callback after it returns
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
            await asyncio.wait_for(process.wait(), self.timeouts.close_grace)
        except TimeoutError:
            grace = self.timeouts.close_grace
            _log.warning("the executor did not exit within %g s; killing it", grace)
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
            self._event_progress.set()
            if self._ack is not None and not self._ack[3].done():
                self._ack[3].set_exception(
                    WorkerError(ErrorKind.UNAVAILABLE, "executor transport closed")
                )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
                    )
            try:
                await self._stop_events()
                await self._stop()
            finally:
                await self._on_closed(self)

    async def _receive(self, line: bytes) -> None:
        try:
            message = decode_line(line)
        except ProtocolError as exc:
            self._drop(str(exc), line)
            return
        kind = message.get("type")
        if kind == "transport_ack" and self.framed:
            ack = self._ack
            if (
                ack is None
                or set(message) != {"type", "transfer", "id", "seq"}
                or type(message.get("id")) is not int
                or type(message.get("seq")) is not int
                or (message.get("transfer"), message.get("id"), message.get("seq")) != ack[:3]
                or ack[3].done()
            ):
                raise ProtocolError("invalid request transport acknowledgement")
            ack[3].set_result(None)
        elif kind == "reply":
            request_id = message.get("id")
            if not isinstance(request_id, int) or isinstance(request_id, bool):
                self._drop("a reply without an integer id", line)
                return
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                if self.framed:
                    self._reply_barriers[request_id] = self._event_received
                future.set_result(message)
        elif kind == "event":
            if self.framed:
                if self._event_bytes + len(line) > 32 * 1024 * 1024:
                    raise ProtocolError("executor event callback backlog exceeds its byte bound")
                self._events.put_nowait(({**message, "_bridge": self.bridge_id}, len(line)))
                self._event_bytes += len(line)
                self._event_received += 1
                return
            try:
                await self._on_event({**message, "_bridge": self.bridge_id})
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
