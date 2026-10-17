# SPDX-License-Identifier: MIT
"""The control channel bounds authority transport and retains admitted effects."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from pydantic import JsonValue, ValidationError

from sandbox_science.orchestrator.confinement.transport import (
    MAX_FRAME_BYTES,
    Capability,
    Channel,
    Request,
    TransportError,
)
from sandbox_science.orchestrator.confinement.wire import WireMessage, WireTurn
from sandbox_science.orchestrator.model import AssistantMessage, ModelTurn, ToolCall


async def test_reverse_capability_does_not_deadlock_the_reader() -> None:
    async def left_handler(kind: Capability, value: JsonValue) -> JsonValue:
        assert kind is Capability.MODEL
        return {"text": value}

    async def right_handler(kind: Capability, value: JsonValue) -> JsonValue:
        assert kind is Capability.TOOL
        return await right.request(Capability.MODEL, value)

    left = Channel(lambda frame: right.receive(frame), left_handler)
    right = Channel(lambda frame: left.receive(frame), right_handler)
    try:
        assert await left.request(Capability.TOOL, "current") == {"text": "current"}
    finally:
        await left.close()
        await right.close()


async def test_cancelled_waiter_does_not_abandon_admitted_effect() -> None:
    entered, release, committed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(kind: Capability, value: JsonValue) -> JsonValue:
        entered.set()
        await release.wait()
        committed.set()
        return value

    left = Channel(lambda frame: right.receive(frame), handler)
    right = Channel(lambda frame: left.receive(frame), handler)
    call = asyncio.create_task(left.request(Capability.TOOL, "effect"))
    await entered.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    closing = asyncio.create_task(right.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert committed.is_set()
    await left.close()


async def test_unknown_duplicate_and_oversized_control_frames_are_refused() -> None:
    async def send(frame: str) -> None:
        pass

    async def handler(kind: Capability, value: JsonValue) -> JsonValue:
        return value

    channel = Channel(send, handler)
    try:
        with pytest.raises(ValidationError):
            await channel.receive('{"kind":"request","id":1,"capability":"sql","payload":null}')
        frame = Request(id=1, capability=Capability.QUEUE, payload=None).model_dump_json()
        await channel.receive(frame)
        with pytest.raises(TransportError, match="identity"):
            await channel.receive(frame)
        with pytest.raises(TransportError, match="byte bound"):
            await channel.receive("x" * (MAX_FRAME_BYTES + 1))
    finally:
        await channel.close()


async def test_timeout_leaves_effect_owned_and_replies_after_timeout_are_ignored() -> None:
    release = asyncio.Event()

    async def handler(kind: Capability, value: JsonValue) -> JsonValue:
        await release.wait()
        return value

    left = Channel(lambda frame: right.receive(frame), handler)
    right = Channel(lambda frame: left.receive(frame), handler)
    try:
        with pytest.raises(TransportError, match="unknown"):
            await left.request(Capability.TOOL, None, timeout=0.001)
        release.set()
    finally:
        await right.close()
        await left.close()


def test_native_model_metadata_roundtrips_without_python_deserialization() -> None:
    turn = ModelTurn(
        "answer",
        (ToolCall("call-1", "read_file", '{"path":"a"}'),),
        native=b'\x00\xff{"reply":"original"}',
        native_format="messages",
        alias="main",
        model="actual",
        adapter_state={"tools": []},
    )
    wire = WireTurn.model_validate_json(WireTurn.of(turn).model_dump_json())
    assert wire.turn() == turn
    message = AssistantMessage.of(turn)
    restored = WireMessage.model_validate_json(WireMessage.of(message).model_dump_json()).message()
    assert restored == message
    assert isinstance(restored, AssistantMessage)
    assert restored.native == message.native
    assert restored.adapter_state == message.adapter_state
    with pytest.raises(ValidationError):
        WireTurn.model_validate({**cast(dict[str, object], wire.model_dump()), "execute": "code"})


async def test_oversized_reply_returns_a_bounded_error_instead_of_timing_out() -> None:
    async def handler(kind: Capability, value: JsonValue) -> JsonValue:
        return "x" * MAX_FRAME_BYTES

    left = Channel(lambda frame: right.receive(frame), handler)
    right = Channel(lambda frame: left.receive(frame), handler)
    try:
        with pytest.raises(TransportError, match="reply exceeds") as failed:
            await left.request(Capability.TOOL, None, timeout=5)
        assert failed.value.kind == "invalid_request"
    finally:
        await left.close()
        await right.close()
