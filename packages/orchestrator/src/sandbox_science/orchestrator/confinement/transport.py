# SPDX-License-Identifier: MIT
"""Bounded duplex transport; operation payloads are validated by their owning handler.

A channel has one authenticated peer, not a caller-selected scope. Capability names
are a closed enum. No Python object, method name or callable crosses the boundary.
Cancellation terminates a waiting request but does not cancel admitted host effects.
The host handler remains their owner and reconciles them after disconnect.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from contextvars import Context
from enum import StrEnum
from typing import Annotated, Final, Literal

from aiohttp import WSMsgType, web
from pydantic import Field, JsonValue, TypeAdapter

from sandbox_science.contracts import SYSTEM_CLOCK, Clock
from sandbox_science.contracts.model import Contract, Counter
from sandbox_science.orchestrator.aio import owned
from sandbox_science.orchestrator.context.history.limits import WorkingMemoryLimitError
from sandbox_science.orchestrator.effects import UnknownEffectError

_log = logging.getLogger(__name__)

CONTROL_PROTOCOL: Final = 6
CONTROL_HEADER: Final = "X-Sandbox-Control-Protocol"
MAX_FRAME_BYTES: Final = 24 * 1024 * 1024
MAX_PENDING: Final = 128
MAX_RECEIVED: Final = 128
HEARTBEAT_SECONDS: Final = 30.0
"""While a request's handler runs, how often its channel tells the peer that it is still
working on it (``progress``)."""
PATIENCE_SECONDS: Final = 300.0
"""How long a request made with patience goes without its reply or a ``progress`` frame
before its outcome is unknown: ten heartbeats. Such a request has no other limit, so a
handler that waits on long work (a child that runs for hours, a long tool, a model call
the gateway retries) never fails it while the peer is alive."""


class Capability(StrEnum):
    """The closed operation catalog, with both directions explicitly named."""

    HANDSHAKE = "handshake"
    QUEUE = "queue"
    ADMIT = "admit"
    RESUME_NEXT = "resume_next"
    RESUME_BRANCH = "resume_branch"
    RUN = "run"
    CANCEL = "cancel"
    MODEL = "model"
    MODEL_INFO = "model_info"
    MODEL_INTENT = "model_intent"
    MODEL_OBSERVED = "model_observed"
    EMBEDDING = "embedding"
    TOOL = "tool"
    HOOK_ENTER = "hook_enter"
    HOOK_EXIT = "hook_exit"
    NOTICES = "notices"
    TURN_LIMIT = "turn_limit"
    """Runtime to host: a hook's authoritative turn limit for the attempt, which the host
    then holds the runtime's loop state to (a child's ``turn_ceiling``, D102)."""
    HALT = "halt"
    LAST_CALL = "last_call"
    MODEL_RETURNED = "model_returned"
    CONSUMPTION = "consumption"
    GATE = "gate"
    OBSERVE_TOOL = "observe_tool"
    TOOL_DONE = "tool_done"
    CLOSING = "closing"
    ACCEPT = "accept"
    FINISHING = "finishing"
    SUMMARY = "summary"
    LOG_INIT = "log_init"
    LOG_APPEND = "log_append"
    LOG_TOOL = "log_tool"
    FOLD_BEFORE = "fold_before"
    FOLD_OVERFLOW = "fold_overflow"
    FINALIZE = "finalize"
    CONFIRM_REQUEST = "confirm_request"
    """Whether one MCP permission request is a call's, for the attempt's project (D98)."""
    PUBLISH_MCP_BINARY = "publish_mcp_binary"
    """Runtime to host: keep one binary item of a direct-path connector call's result as an
    artifact revision of the attempt's run (D98)."""
    REPORT_MCP_LISTING = "report_mcp_listing"
    """Runtime to host: a complete listing the runtime's direct MCP client read, for the
    worker's drift check against the project's pin (D98, D103)."""
    LIST_MCP_TOOLS = "list_mcp_tools"
    """Host to runtime: one bounded canary listing of a server's tools through the runtime's
    direct MCP client, never a tool call (D98, D103)."""
    MCP_REQUEST = "mcp_request"
    """Host to runtime: one MCP request (``tools/list`` or ``tools/call``) of an attempt's
    connector tools, made by the runtime's direct MCP client on the direct path, after the
    host's toolbox admitted the call (D98)."""


class Request(Contract):
    kind: Literal["request"] = "request"
    id: Counter
    capability: Capability
    payload: JsonValue


class Reply(Contract):
    kind: Literal["reply"] = "reply"
    id: Counter
    payload: JsonValue = None
    error: (
        Literal["invalid_request", "unavailable", "conflict", "denied", "internal", "memory_limit"]
        | None
    ) = None
    message: str = Field(default="", max_length=512)


class Progress(Contract):
    """The peer is still working on request ``id`` (``HEARTBEAT_SECONDS``)."""

    kind: Literal["progress"] = "progress"
    id: Counter


Frame = Annotated[Request | Reply | Progress, Field(discriminator="kind")]
_FRAME = TypeAdapter(Frame)
Handler = Callable[[Capability, JsonValue], Awaitable[JsonValue]]
Sender = Callable[[str], Awaitable[None]]


class TransportError(Exception):
    """A bounded remote failure; details contain no traceback or credentials."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class Channel:
    """One connection with bounded in-flight calls and joined incoming effects.

    The connection reader calls ``receive``. It never awaits a request handler:
    handlers may themselves need a reverse-direction capability, so awaiting one
    there would deadlock. ``close`` wakes local waiters and joins admitted handlers.
    """

    def __init__(
        self,
        send: Sender,
        handler: Handler,
        *,
        clock: Clock = SYSTEM_CLOCK,
        heartbeat: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._send, self._handler = send, handler
        self.clock = clock
        """The time a request's timeout, its patience and the heartbeat are measured in."""
        self.heartbeat = heartbeat
        self._next = 0
        self._pending: dict[int, asyncio.Future[JsonValue]] = {}
        self._alive: dict[int, asyncio.Event] = {}
        """Set by a ``progress`` frame for each pending request made with patience."""
        self._received: set[int] = set()
        self._handling: set[asyncio.Task[None]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        """The peer is disconnected, even while admitted handlers still drain."""
        return self._closed

    async def request(
        self,
        capability: Capability,
        payload: JsonValue,
        *,
        timeout: float | None = 60,
        patience: float | None = None,
    ) -> JsonValue:
        """Send one bounded call. Timeout is ambiguous for a mutating capability.

        With ``patience`` the call has no ``timeout`` of its own: it waits for as long as
        the peer keeps saying, at least every ``patience`` seconds, that it is still working
        on it, which a live peer does for every request it handles. Use it for a call whose
        handler may wait on work of unbounded length."""
        if patience is not None and timeout is not None:
            raise ValueError("a call with patience has no timeout of its own")
        if self._closed:
            raise TransportError("unavailable", "control connection closed")
        if len(self._pending) >= MAX_PENDING:
            raise TransportError("unavailable", "control request limit reached")
        self._next += 1
        identity = self._next
        future = asyncio.get_running_loop().create_future()
        self._pending[identity] = future
        if patience is not None:
            self._alive[identity] = asyncio.Event()
        try:
            await self._write(Request(id=identity, capability=capability, payload=payload))
            if patience is not None:
                return await self._patiently(identity, future, patience)
            if timeout is None:
                return await asyncio.shield(future)
            expiry = asyncio.ensure_future(self.clock.sleep(timeout))
            try:
                await asyncio.wait((future, expiry), return_when=asyncio.FIRST_COMPLETED)
            finally:
                expiry.cancel()
            if not future.done():
                raise TransportError("unavailable", "control request outcome is unknown")
            return future.result()
        finally:
            self._pending.pop(identity, None)
            self._alive.pop(identity, None)
            # An error arriving concurrently with cancellation must not leave an
            # unobserved Future exception. No handler is detached or cancelled here.
            if future.done() and not future.cancelled():
                future.exception()
            elif not future.done():
                future.cancel()

    async def _patiently(
        self, identity: int, future: asyncio.Future[JsonValue], patience: float
    ) -> JsonValue:
        alive = self._alive[identity]
        while True:
            expiry = asyncio.ensure_future(self.clock.sleep(patience))
            beat = asyncio.ensure_future(alive.wait())
            try:
                await asyncio.wait((future, expiry, beat), return_when=asyncio.FIRST_COMPLETED)
            finally:
                expiry.cancel()
                beat.cancel()
            if future.done():
                return future.result()
            if not alive.is_set():
                raise TransportError("unavailable", "control request outcome is unknown")
            alive.clear()

    async def _beat(self, identity: int) -> None:
        """Tell the peer, every ``heartbeat`` seconds, that request ``identity`` is still
        being worked on, until its handler ends."""
        while True:
            await self.clock.sleep(self.heartbeat)
            if self._closed:
                return
            try:
                await self._write(Progress(id=identity))
            except (ConnectionError, RuntimeError, TransportError):
                return

    async def _write(self, frame: Request | Reply | Progress) -> None:
        text = frame.model_dump_json()
        if len(text.encode()) > MAX_FRAME_BYTES:
            raise TransportError("invalid_request", "control frame exceeds its byte bound")
        await self._send(text)

    async def receive(self, text: str) -> None:
        """Validate a frame before dispatch; malformed frames close the connection."""
        if self._closed:
            raise TransportError("unavailable", "control connection closed")
        if len(text.encode()) > MAX_FRAME_BYTES:
            raise TransportError("invalid_request", "control frame exceeds its byte bound")
        frame = _FRAME.validate_json(text)
        if isinstance(frame, Progress):
            alive = self._alive.get(frame.id)
            if alive is not None:
                alive.set()  # a request without patience, or an ended one, ignores it
            return
        if isinstance(frame, Reply):
            pending = self._pending.get(frame.id)
            if pending is None or pending.done():
                return  # a late reply to a cancelled/timed-out call
            if frame.error is None:
                pending.set_result(frame.payload)
            elif frame.error == "memory_limit":
                pending.set_exception(WorkingMemoryLimitError(frame.message))
            else:
                pending.set_exception(TransportError(frame.error, frame.message))
            return
        if frame.id in self._received:
            raise TransportError("conflict", "duplicate control request identity")
        if len(self._handling) >= MAX_RECEIVED:
            await self._write(
                Reply(id=frame.id, error="unavailable", message="control service busy")
            )
            return
        # IDs are monotonically increasing within each direction. Keeping the last
        # accepted ID rather than every old ID bounds the connection's bookkeeping.
        if self._received and frame.id <= next(iter(self._received)):
            raise TransportError("conflict", "control request identity moved backwards")
        self._received = {frame.id}
        task = asyncio.create_task(self._handle(frame), context=Context())
        self._handling.add(task)
        task.add_done_callback(self._handling.discard)

    async def _handle(self, frame: Request) -> None:
        beating = asyncio.ensure_future(self._beat(frame.id))
        try:
            await self._respond(frame)
        finally:
            beating.cancel()

    async def _respond(self, frame: Request) -> None:
        try:
            result = await self._handler(frame.capability, frame.payload)
            reply = Reply(id=frame.id, payload=result)
        except WorkingMemoryLimitError as error:
            reply = Reply(id=frame.id, error="memory_limit", message=str(error)[:512])
        except UnknownEffectError as error:
            # The worker was lost mid-operation; the host keeps its intent pending. The
            # runtime's loop records the kind, so automatic recovery can reconcile and
            # retry it (D100). The message is the adapter's fixed text.
            reply = Reply(id=frame.id, error="unavailable", message=str(error)[:512])
        except TransportError as error:
            kind = (
                error.kind
                if error.kind
                in {"invalid_request", "unavailable", "conflict", "denied", "internal"}
                else "internal"
            )
            reply = Reply.model_validate(
                {"id": frame.id, "error": kind, "message": str(error)[:512]}
            )
        except Exception as error:
            _log.error(
                "control capability %s failed (%s):\n%s",
                frame.capability,
                type(error).__name__,
                "".join(
                    f"  {location.filename}:{location.lineno} in {location.name}\n"
                    for location in traceback.extract_tb(error.__traceback__)
                ),
            )
            reply = Reply(id=frame.id, error="internal", message="control capability failed")
        if not self._closed:
            try:
                await self._write(reply)
            except TransportError:
                # Never turn a known bounded-reply violation into a long unknown
                # timeout. The handler still owns its effect and full evidence.
                try:
                    await self._write(
                        Reply(
                            id=frame.id,
                            error="invalid_request",
                            message="capability reply exceeds its byte bound",
                        )
                    )
                except (ConnectionError, RuntimeError, TransportError):
                    return
            except (ConnectionError, RuntimeError):
                # A disconnected peer has no acknowledgement to advance from.
                return

    async def close(self) -> None:
        """Stop admission, fail waiting calls, then join already admitted handlers."""
        self._closed = True
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(TransportError("unavailable", "control connection closed"))
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain())
        await owned(self._close_task)

    async def _drain(self) -> None:
        if self._handling:
            await asyncio.gather(*tuple(self._handling), return_exceptions=True)


async def serve_socket(socket: web.WebSocketResponse, channel: Channel) -> None:
    """Read one server-side socket; admitted effects stay owned during teardown."""
    try:
        async for frame in socket:
            if frame.type is WSMsgType.TEXT:
                await channel.receive(frame.data)
            elif frame.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                break
            elif frame.type is WSMsgType.BINARY:
                raise TransportError("invalid_request", "binary control frames are refused")
    finally:
        await channel.close()
