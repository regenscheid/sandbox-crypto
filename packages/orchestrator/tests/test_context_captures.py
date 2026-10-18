# SPDX-License-Identifier: MIT
"""Actual dispatch evidence survives failure without duplicating repeated bodies."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sandbox_science.orchestrator.confinement.paid import PaidModel
from sandbox_science.orchestrator.confinement.transport import Capability
from sandbox_science.orchestrator.confinement.wire import ModelIntent, WireMessage
from sandbox_science.orchestrator.continuity.models import IntentModel, ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.history.capture import (
    ContextMessage,
    Origin,
    prompt_pins,
    request,
)
from sandbox_science.orchestrator.history.captured import CapturedContexts
from sandbox_science.orchestrator.model import AssistantMessage, ModelTurn, ScriptedModel, ToolSpec
from sandbox_science.orchestrator.prompts import Section, assemble

from .test_tool_journal import RUN
from .test_tool_journal import projects as projects


async def test_capture_precedes_dispatch_and_survives_model_failure_and_reopen(projects):
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    contexts = CapturedContexts(projects.control)
    messages = [{"role": "user", "content": "Original \u03b1 reference 937"}]

    class Fails:
        async def complete(self, sent, tools):
            calls = contexts.calls("p", "s", RUN).calls
            assert len(calls) == 1
            assert calls[0].captured
            part = contexts.text("p", "s", RUN, calls[0].identity, 0)
            assert json.loads(part.text) == messages[0]
            raise RuntimeError("synthetic failure after dispatch admission")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await IntentModel(Fails(), ModelIntents(projects.control), owner, "fixture").complete(
            messages, []
        )
    projects.control.close()
    call = contexts.calls("p", "s", RUN).calls[0]
    assert contexts.text("p", "s", RUN, call.identity, 0).text == json.dumps(
        messages[0], ensure_ascii=False, separators=(",", ":")
    )
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (call.identity,)


async def test_exact_system_sections_view_origins_and_tools_are_ordered_and_reused(projects):
    sections = [Section("system", "v1", "system \u03b1"), Section("skills", "v2", "skills body")]
    tools = [ToolSpec("example", "description", {"type": "object"})]
    system, pins = assemble("main", sections, tools)
    folded = ContextMessage({"role": "user", "content": "fold summary"}, Origin(kind="fold_region"))
    stubbed = ContextMessage(
        {"role": "tool", "content": "stub"}, Origin(kind="stub", message="msg-123", position=3)
    )
    wire = WireMessage.of(stubbed)
    assert wire.message().origin == stubbed.origin
    native = AssistantMessage("Visible", native=b"opaque-provider-payload")
    messages = [{"role": "system", "content": system}, folded, stubbed, native]
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    model = IntentModel(
        ScriptedModel([ModelTurn("one"), ModelTurn("two")]),
        ModelIntents(projects.control),
        owner,
        "fixture",
    )
    with prompt_pins(pins):
        one = await model.complete(messages, tools)
        two = await model.complete(messages, tools)
    contexts = CapturedContexts(projects.control)
    parts = contexts.parts("p", "s", RUN, one.intent_id).parts
    assert [part.section or part.kind for part in parts] == [
        "system",
        "skills",
        "fold_region",
        "stub",
        "message",
        "tool_schemas",
    ]
    assert parts[3].origin == stubbed.origin
    assert parts[4].native_omitted
    assert [part.sha256 for part in parts] == [
        part.sha256 for part in contexts.parts("p", "s", RUN, two.intent_id).parts
    ]
    with projects.control.read() as tx:
        assert tx.sql.execute("SELECT count(*) FROM context_bodies").fetchone() == (6,)
        assert tx.sql.execute("SELECT count(*) FROM context_parts").fetchone() == (12,)
    assert "opaque-provider" not in contexts.text("p", "s", RUN, one.intent_id, 4).text
    with prompt_pins(pins):
        changed = request([{"role": "system", "content": system + "changed"}], tools)
    assert changed.parts[0].kind == "message"
    assert changed.parts[0].section is None


async def test_confined_paid_model_submits_capture_before_contacting_model():
    seen = []

    class Channel:
        async def request(self, kind, value):
            assert kind == Capability.MODEL_INTENT
            item = ModelIntent.model_validate(value)
            assert item.captured is not None
            seen.append(item)
            return {"identity": "model-fixture"}

    class Model:
        async def complete(self, messages, tools):
            assert len(seen) == 1
            assert json.loads(seen[0].captured.parts[0].text) == messages[0]
            return ModelTurn("Done")

    result = await PaidModel(
        Channel(), Model(), "cap-" + "a" * 48, "fixture", "delegated"
    ).complete([{"role": "user", "content": "child request"}], [])
    assert result.intent_id == "model-fixture"
    assert seen[0].role == "delegated"


def test_capture_is_immutable_and_refused_owner_leaves_no_capture(projects):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    intents = ModelIntents(projects.control)
    captured = request([{"role": "user", "content": "a" * 40000}], [])
    identity = intents.begin(owner, kind="main", alias="fixture", captured=captured)
    with pytest.raises(sqlite3.IntegrityError), projects.control.write() as tx:
        tx.sql.execute("UPDATE context_bodies SET text='changed'")
    contexts = CapturedContexts(projects.control)
    first = contexts.text("p", "s", RUN, identity, 0)
    second = contexts.text("p", "s", RUN, identity, 0, offset=first.next_offset)
    assert len(first.text) == len(second.text) == 16384
    assert first.sha256 == second.sha256
    assert contexts.parts("p", "other", RUN, identity).parts == ()
    assert contexts.calls("p", "other", RUN).calls == ()
    works.interrupt("p", "s", RUN)
    with pytest.raises(ControlError):
        intents.begin(owner, kind="main", alias="fixture", captured=request([], []))
    assert len(contexts.calls("p", "s", RUN).calls) == 1


def test_captured_text_chunks_preserve_unicode_and_nul_and_refuse_middle_byte(projects):
    from sandbox_science.orchestrator.documents import DocumentError

    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    original = "\u03b1" * 10000 + "\x00tail"
    system, pins = assemble("main", [Section("system", "v1", original)], [])
    with prompt_pins(pins):
        captured = request([{"role": "system", "content": system}], [])
    identity = ModelIntents(projects.control).begin(
        owner, kind="main", alias="fixture", captured=captured
    )
    reader = CapturedContexts(projects.control)
    first = reader.text("p", "s", RUN, identity, 0, limit=16383)
    second = reader.text("p", "s", RUN, identity, 0, offset=first.next_offset)
    assert first.text + second.text == original
    assert first.next_offset == 16382
    assert second.next_offset is None
    assert second.total == len(original.encode())
    with pytest.raises(DocumentError):
        reader.text("p", "s", RUN, identity, 0, offset=1)
