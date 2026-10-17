# SPDX-License-Identifier: MIT
"""Bounded duplex transport; operation payloads are validated by their owning handler.

A channel has one authenticated peer, not a caller-selected scope. Capability names
are a closed enum. No Python object, method name or callable crosses the boundary.
Cancellation terminates a waiting request but does not cancel admitted host effects.
The host handler remains their owner and reconciles them after disconnect.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import Context
from enum import StrEnum
from typing import Annotated, Final, Literal

from aiohttp import WSMsgType, web
from pydantic import Field, JsonValue, TypeAdapter

from sandbox_science.contracts.model import Contract, Counter

CONTROL_PROTOCOL: Final = 1
CONTROL_HEADER: Final = "X-Sandbox-Control-Protocol"
MAX_FRAME_BYTES: Final = 24 * 1024 * 1024
MAX_PENDING: Final = 128
MAX_RECEIVED: Final = 128


class Capability(StrEnum):
    """The closed operation catalog, with both directions explicitly named."""

    HANDSHAKE = "handshake"
    QUEUE = "queue"
    ADMIT = "admit"
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


class Request(Contract):
    kind: Literal["request"] = "request"
    id: Counter
    capability: Capability
    payload: JsonValue


class Reply(Contract):
    kind: Literal["reply"] = "reply"
    id: Counter
    payload: JsonValue = None
    error: Literal["invalid_request", "unavailable", "conflict", "denied", "internal"] | None = None
    message: str = Field(default="", max_length=512)


Frame = Annotated[Request | Reply, Field(discriminator="kind")]
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

    def __init__(self, send: Sender, handler: Handler) -> None:
        self._send, self._handler = send, handler
        self._next = 0
        self._pending: dict[int, asyncio.Future[JsonValue]] = {}
        self._received: set[int] = set()
        self._handling: set[asyncio.Task[None]] = set()
        self._closed = False

    async def request(
        self, capability: Capability, payload: JsonValue, *, timeout: float = 60
    ) -> JsonValue:
        """Send one bounded call. Timeout is ambiguous for a mutating capability."""
        if self._closed:
            raise TransportError("unavailable", "control connection closed")
        if len(self._pending) >= MAX_PENDING:
            raise TransportError("unavailable", "control request limit reached")
        self._next += 1
        identity = self._next
        future = asyncio.get_running_loop().create_future()
        self._pending[identity] = future
        try:
            await self._write(Request(id=identity, capability=capability, payload=payload))
            async with asyncio.timeout(timeout):
                return await asyncio.shield(future)
        except TimeoutError as error:
            raise TransportError("unavailable", "control request outcome is unknown") from error
        finally:
            self._pending.pop(identity, None)
            # An error arriving concurrently with cancellation must not leave an
            # unobserved Future exception. No handler is detached or cancelled here.
            if future.done() and not future.cancelled():
                future.exception()
            elif not future.done():
                future.cancel()

    async def _write(self, frame: Request | Reply) -> None:
        text = frame.model_dump_json()
        if len(text.encode()) > MAX_FRAME_BYTES:
            raise TransportError("invalid_request", "control frame exceeds its byte bound")
        await self._send(text)

    async def receive(self, text: str) -> None:
        """Validate a frame before dispatch; malformed frames close the connection."""
        if len(text.encode()) > MAX_FRAME_BYTES:
            raise TransportError("invalid_request", "control frame exceeds its byte bound")
        frame = _FRAME.validate_json(text)
        if isinstance(frame, Reply):
            pending = self._pending.get(frame.id)
            if pending is None or pending.done():
                return  # a late reply to a cancelled/timed-out call
            if frame.error is None:
                pending.set_result(frame.payload)
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
        try:
            result = await self._handler(frame.capability, frame.payload)
            reply = Reply(id=frame.id, payload=result)
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
        except Exception:
            reply = Reply(id=frame.id, error="internal", message="control capability failed")
        if not self._closed:
            try:
                await self._write(reply)
            except (ConnectionError, RuntimeError, TransportError):
                # The effect remains committed/reconcilable at its owning handler;
                # a disconnected peer has no acknowledgement to advance from.
                return

    async def close(self) -> None:
        """Stop admission, fail waiting calls, then join already admitted handlers."""
        self._closed = True
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(TransportError("unavailable", "control connection closed"))
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
