# SPDX-License-Identifier: MIT
"""Static producing-input descriptors retain no private request or dispatch claim."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, cast

import pytest
from aiohttp.test_utils import TestClient

from sandbox_science.orchestrator.model import Message, ModelError, ModelTurn, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace import mathrepair, repair_prompt
from sandbox_science.workspace.app import create_app
from sandbox_science.workspace.mathrepair import (
    MathRepairService,
    RepairAnswer,
    RepairRequest,
    SessionLimitError,
)
from sandbox_science.workspace.repair_prompt import (
    MAX_PROVENANCE_BYTES,
    RepairProvenance,
    RequestFormat,
    assemble_repair,
    provenance_json,
    read_provenance,
)

HOSTILE = "Authorization: Bearer test-secret-repair-provenance-not-a-credential"
SYSTEM = mathrepair.SYSTEM_PROMPT


@pytest.mark.asyncio
@pytest.mark.parametrize("display", [False, True])
@pytest.mark.parametrize("error", ["", HOSTILE])
async def test_actual_original_inputs_have_static_only_pins(display, error):
    calls = []

    class Model:
        async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]):
            calls.append(messages)
            assert messages == [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": f"Mode: {'display' if display else 'inline'}\n"
                    f"KaTeX error: {error or 'unknown'}\nExpression:\n{HOSTILE}",
                },
            ]
            assert tools == ()
            return ModelTurn("x^2", model="actual-model-not-inferred")

    service = MathRepairService(Model(), reviewer="configured-alias")
    answer = await service.repair(
        RepairRequest(HOSTILE, display, error, "private-project", "private-context"),
        "private-cookie",
    )
    await service.close()
    assert len(calls) == 1
    pin = answer.input_provenance
    assert isinstance(pin, RepairProvenance)
    assert pin.system_kind == "builtin_content"
    assert pin.prompt_version == "workspace-display-repair-1"
    assert pin.system_bytes == 575
    assert pin.system_sha256 == "6c3eb945a1fc5fb98ce4be807d9dc7e4ea1a1702302cde8957df22eadb541151"
    assert answer.model == "configured-alias"
    encoded = json.dumps(provenance_json(pin))
    assert len(encoded.encode()) <= MAX_PROVENANCE_BYTES
    for private in (
        HOSTILE,
        "private-project",
        "private-context",
        "private-cookie",
        "actual-model-not-inferred",
        "configured-alias",
    ):
        assert private not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["usable", "unfixable", "unchanged", "error", "timeout"])
async def test_all_producing_outcomes_keep_input_without_raw_errors(outcome, caplog):
    calls = []

    class Model:
        async def complete(self, messages, tools):
            calls.append(messages)
            if outcome == "error":
                raise ModelError(HOSTILE, status=429)
            if outcome == "timeout":
                await asyncio.Event().wait()
            return ModelTurn(
                {"usable": "x^2", "unfixable": "UNFIXABLE", "unchanged": "x^"}.get(outcome)
            )

    service = MathRepairService(Model(), reviewer="rev", timeout=0.01)
    first = await service.repair(RepairRequest("x^", False, HOSTILE), "s")
    again = await service.repair(RepairRequest("x^", False, "a different error"), "other")
    await service.close()
    assert len(calls) == 1
    assert first.input_provenance is not None
    assert again.input_provenance is first.input_provenance
    assert again.cached
    assert HOSTILE not in json.dumps(first.to_json())
    assert HOSTILE not in caplog.text
    assert (first.tex is not None) == (outcome == "usable")


@pytest.mark.asyncio
async def test_first_producer_survives_waiter_cancel_and_different_error_join(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Model:
        async def complete(self, messages, tools):
            calls.append(messages)
            entered.set()
            await release.wait()
            return ModelTurn("x^2")

    service = MathRepairService(Model(), reviewer="rev")
    one = asyncio.create_task(service.repair(RepairRequest("x^", False, "first"), "s1"))
    await entered.wait()
    two = asyncio.create_task(service.repair(RepairRequest("x^", False, "second"), "s2"))
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    monkeypatch.setattr(mathrepair, "SYSTEM_PROMPT", "New custom content")
    release.set()
    result = await two
    cached = await service.repair(RepairRequest("x^", False, "third"), "s3")
    await service.close()
    assert len(calls) == 1
    assert "KaTeX error: first" in calls[0][1]["content"]
    assert result.input_provenance is not None
    assert result.input_provenance.system_text == SYSTEM
    assert cached.input_provenance is result.input_provenance
    assert cached.cached


@pytest.mark.asyncio
async def test_close_cancels_active_and_queued_producers_without_answers():
    calls, canceled = [], []
    started = asyncio.Event()

    class Model:
        async def complete(self, messages, tools):
            calls.append(messages)
            if len(calls) == mathrepair.MAX_ACTIVE:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.append(messages)
            return ModelTurn("Unexpected release")

    service = MathRepairService(Model(), reviewer="rev")
    tasks = [
        asyncio.create_task(service.repair(RepairRequest(f"x{i}", False, "e"), "s"))
        for i in range(3)
    ]
    await started.wait()
    await service.close()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(value, asyncio.CancelledError) for value in results)
    assert len(calls) == len(canceled) == 2
    assert not service._cache
    assert not service._in_flight


@pytest.mark.asyncio
async def test_disabled_busy_rate_and_legacy_answers_have_no_fabricated_pin():
    off = await MathRepairService().repair(RepairRequest("x^", False, "e"), "s")
    assert "input_provenance" not in off.to_json()
    legacy = RepairAnswer(True, tex="old", model="old-alias")
    assert "input_provenance" not in legacy.to_json()
    service = MathRepairService(reviewer="rev", model=NeverCalled(), session_limit=0)
    with pytest.raises(SessionLimitError):
        await service.repair(RepairRequest("x^", False, "e"), "s")
    assert not service._cache
    # A full admission queue is an existing no-call response.
    pending = asyncio.create_task(asyncio.Event().wait())
    service._in_flight.update(
        {str(index): cast(asyncio.Task[RepairAnswer], pending) for index in range(10)}
    )
    busy = await service.repair(RepairRequest("other", False, "e"), "other")
    assert busy.reason == "busy"
    assert busy.input_provenance is None
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await service.close()


class NeverCalled:
    async def complete(self, messages, tools):
        pytest.fail("local metadata/admission refusal reached model")


@pytest.mark.asyncio
@pytest.mark.parametrize("system", ["", "x" * 2049, "\ud800", '"' * 2048, "∀" * 682, "\x7f" * 1800])
async def test_local_metadata_failure_is_safe_unpinned_cached_and_before_model(
    monkeypatch, caplog, system
):
    monkeypatch.setattr(mathrepair, "SYSTEM_PROMPT", system)
    service = MathRepairService(NeverCalled(), reviewer="rev")
    first = await service.repair(RepairRequest(HOSTILE, False, HOSTILE), "s")
    assert first.input_provenance is None
    assert first.reason == "repair input metadata unavailable"
    assert (await service.repair(RepairRequest(HOSTILE, False, "other"), "s")).cached
    assert HOSTILE not in json.dumps(first.to_json()) + caplog.text
    await service.close()


@pytest.mark.asyncio
async def test_changed_installed_system_is_custom_and_history_is_self_contained(monkeypatch):
    original = assemble_repair(SYSTEM, "x^", False, "e").provenance
    retained = json.dumps(provenance_json(original))
    monkeypatch.setattr(mathrepair, "SYSTEM_PROMPT", "Changed installed content")
    monkeypatch.setattr(repair_prompt, "PROMPT_VERSION", "future-current-version")
    assert read_provenance(retained) == original

    class Model:
        async def complete(self, messages, tools):
            assert messages[0]["content"] == "Changed installed content"
            return ModelTurn("x^2")

    service = MathRepairService(Model(), reviewer="rev")
    result = await service.repair(RepairRequest("x^", False, "e"), "s")
    await service.close()
    assert result.input_provenance is not None
    assert result.input_provenance.system_kind == "custom_content"
    assert result.input_provenance.prompt_version is None


def test_actual_message_or_tool_change_refuses():
    wanted = assemble_repair(SYSTEM, "x^", True, "e")
    expected = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "Mode: display\nKaTeX error: e\nExpression:\nx^"},
    ]
    wanted.verify(expected, ())
    with pytest.raises(ValueError, match="actual repair input"):
        wanted.verify(expected, (ToolSpec("extra", "Denied", {}),))
    with pytest.raises(ValueError, match="actual repair input"):
        wanted.verify([*expected[:1], {"role": "user", "content": "different"}], ())


@pytest.mark.parametrize(
    "field", ["system_text", "request_format", "envelope_sha256", "tool_count"]
)
def test_copied_metadata_is_bounded_before_encoding(monkeypatch, field):
    pin = assemble_repair(SYSTEM, "x", False, "e").provenance
    changes = {
        "system_text": "x" * 2_000_000,
        "request_format": RequestFormat().model_copy(update={"mode_prefix": "x" * 2_000_000}),
        "envelope_sha256": "x" * 2_000_000,
        "tool_count": False,
    }
    damaged = pin.model_copy(update={field: changes[field]})

    def unexpected(value):
        pytest.fail("oversized/copied scalar reached aggregate encoding")

    monkeypatch.setattr(repair_prompt, "_canonical", unexpected)
    with pytest.raises(ValueError, match="repair provenance"):
        provenance_json(damaged)


def test_retained_raw_input_is_bounded_before_json(monkeypatch):
    def unexpected(*args):
        pytest.fail("oversized descriptor reached parser")

    monkeypatch.setattr(RepairProvenance, "model_validate_json", unexpected)
    with pytest.raises(ValueError, match="repair provenance"):
        read_provenance("x" * 2_000_000)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", True),
        ("format_version", 1.0),
        ("tool_count", False),
        ("tool_count", 0.0),
    ],
)
def test_retained_raw_literal_counters_never_coerce_even_with_valid_original_digest(field, value):
    pin = assemble_repair(SYSTEM, "x", False, "e").provenance
    payload = provenance_json(pin)
    payload[field] = value
    with pytest.raises(ValueError, match="counters require strict integers"):
        read_provenance(json.dumps(payload))


@pytest.mark.parametrize("nested", [False, True])
def test_declared_subclass_fields_cannot_bypass_prefetch_bounds(monkeypatch, nested):
    pin = assemble_repair(SYSTEM, "x", False, "e").provenance
    if nested:

        class ExtendedFormat(RequestFormat):
            extra_text: str

        formatter = ExtendedFormat.model_construct(
            **pin.request_format.__dict__, extra_text="x" * 2_000_000
        )
        damaged = pin.model_copy(update={"request_format": formatter})
    else:

        class ExtendedPin(RepairProvenance):
            extra_text: str

        damaged = ExtendedPin.model_construct(**pin.__dict__, extra_text="x" * 2_000_000)

    def unexpected(value):
        pytest.fail("undeclared subtype fields reached aggregate encoding")

    monkeypatch.setattr(repair_prompt, "_canonical", unexpected)
    with pytest.raises(ValueError, match="invalid repair provenance type"):
        provenance_json(damaged)


@pytest.mark.asyncio
async def test_existing_api_adds_only_static_pin_and_preserves_auth(
    aiohttp_client, state_dir, static_dir
):
    token = "test-secret-repair-input-login-not-a-credential"

    class Model:
        async def complete(self, messages, tools):
            return ModelTurn("x^2")

    service = MathRepairService(Model(), reviewer="rev")
    client: TestClient[Any, Any] = await aiohttp_client(
        create_app(ProjectStore(state_dir), static_dir, login_token=token, math_repair=service)
    )
    origin = {"Origin": f"http://127.0.0.1:{client.port}"}
    body = {"tex": "x^", "display": False, "error": HOSTILE}
    assert (await client.post("/api/v0/math-repair", json=body, headers=origin)).status == 401
    assert (
        await client.get("/login", params={"token": token}, allow_redirects=False)
    ).status == 303
    assert (
        await client.post(
            "/api/v0/math-repair", json=body, headers={"Origin": "https://foreign.invalid"}
        )
    ).status == 403
    response = await client.post("/api/v0/math-repair", json=body, headers=origin)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    answer = await response.json()
    pin = read_provenance(json.dumps(answer.pop("input_provenance")))
    assert pin.system_text == SYSTEM
    assert HOSTILE not in pin.model_dump_json()
    assert answer == {
        "enabled": True,
        "tex": "x^2",
        "model": "rev",
        "reason": None,
        "cached": False,
    }
