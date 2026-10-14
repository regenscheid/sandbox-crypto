# SPDX-License-Identifier: MIT
"""Recorded content pins match the actual MAIN boundary, without new grants."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import cli, prompt_bundle
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.hooks import TurnHook
from sandbox_science.orchestrator.loop import SYSTEM_PROMPT, run_task
from sandbox_science.orchestrator.model import (
    Message,
    ModelError,
    ModelTurn,
    ScriptedModel,
    ToolCall,
    ToolSpec,
)
from sandbox_science.orchestrator.plan_tools import GUIDANCE, GUIDANCE_VERSION, PlanTools
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.prompt_bundle import (
    MAX_PROVENANCE_BYTES,
    MAX_SCHEMA_BYTES,
    MAX_SYSTEM_BYTES,
    CompleteProvenance,
    PromptProvenance,
    UnavailableProvenance,
    assemble_main,
    verify_retained,
)
from sandbox_science.orchestrator.session_plans import SessionPlans
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
HOSTILE = "Authorization: Bearer test-secret-prompt-not-a-credential"


def spec(parameters=None, *, name="inspect", description="Read data"):
    return ToolSpec(name, description, parameters if parameters is not None else {"type": "object"})


def bundle(specs=(), *, system="Original Unicode ∀x\n", guidance=None):
    return assemble_main(system, specs, builtin_content=False, guidance=guidance)


def record(transcript, **changes):
    fields = {
        "run_id": "run",
        "context": CONTEXT,
        "task": "Inspect",
        "started_at": datetime.now(UTC),
        "ended_at": datetime.now(UTC),
        "stopped": transcript.stopped,
        "failure": transcript.failure,
        "final_text": transcript.final_text,
        "messages": transcript.messages,
        "tools": transcript.tool_records,
        "prompt_provenance": transcript.prompt_provenance,
    }
    return RunRecord(**(fields | changes))


def test_exact_unicode_join_component_pins_and_canonical_schema_order():
    first = spec(
        {"properties": {"z": {"type": "string"}, "a": {"type": "integer"}}, "required": ["z", "a"]}
    )
    copied = spec(
        {"required": ["z", "a"], "properties": {"a": {"type": "integer"}, "z": {"type": "string"}}}
    )
    original = bundle((first,), guidance=(GUIDANCE_VERSION, GUIDANCE))
    other = bundle((copied,), guidance=(GUIDANCE_VERSION, GUIDANCE))
    assert isinstance(original.provenance, CompleteProvenance)
    assert original.provenance == other.provenance
    assert original.system == "Original Unicode ∀x\n\n\n" + GUIDANCE
    assert original.provenance.components[0].component == "custom"
    assert original.provenance.components[0].version is None
    assert original.provenance.components[1].version == GUIDANCE_VERSION
    assert list(original.tool_specs()[0].parameters) == list(first.parameters)
    assert list(original.tool_specs()[0].parameters["properties"]) == ["z", "a"]
    verify_retained(original.provenance, {"role": "system", "content": original.system})
    assert len(original.provenance.model_dump_json().encode()) <= MAX_PROVENANCE_BYTES
    assert (
        bundle((spec({"required": ["a", "z"]}),)).provenance
        != bundle((spec({"required": ["z", "a"]}),)).provenance
    )
    assert (
        bundle((first, spec(name="second"))).provenance
        != bundle((spec(name="second"), first)).provenance
    )
    assert (
        bundle((first,)).provenance
        != bundle((spec(first.parameters, description="Changed"),)).provenance
    )


def test_owned_schemas_and_actual_passed_projection_are_fenced():
    original = spec({"nested": {"items": ["original"]}})
    owned = bundle((original,))
    original.parameters["nested"]["items"].append("outside mutation")
    passed = owned.tool_specs()
    assert passed[0].parameters["nested"]["items"] == ["original"]
    passed[0].parameters["nested"]["items"].append("inside mutation")
    with pytest.raises(ValueError, match="tool input changed"):
        owned.verify({"role": "system", "content": owned.system}, passed)
    owned.verify({"role": "system", "content": owned.system}, owned.tool_specs())
    with pytest.raises(ValueError, match="system input changed"):
        owned.verify({"role": "system", "content": "different"}, owned.tool_specs())


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("x" * (MAX_SYSTEM_BYTES + 1), "system_too_large"),
        ("∀" * 22000, "system_too_large"),
        ("bad\ud800text", "system_not_utf8"),
    ],
)
@pytest.mark.asyncio
async def test_unavailable_custom_provenance_preserves_actual_invocation(text, reason):
    class Model:
        async def complete(self, messages, tools):
            assert messages[0]["content"] == text
            assert tools
            return ModelTurn("Observed")

    transcript = await run_task(
        Model(),
        Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT),
        "Inspect",
        run_id="run",
        system=text,
    )
    assert transcript.stopped == "answered"
    assert transcript.prompt_provenance == UnavailableProvenance(reason=reason)
    assert isinstance(record(transcript).prompt_provenance, UnavailableProvenance)


@pytest.mark.parametrize(
    "parameters",
    [
        {"x": float("nan")},
        {"x": object()},
        {1: "key"},
        {"x": "a" * (MAX_SCHEMA_BYTES + 1)},
        {"x": 1 << 5000},
    ],
)
def test_malformed_or_unrepresentable_schema_refuses(parameters):
    with pytest.raises((ValueError, UnicodeError)):
        bundle((spec(parameters),))


def test_deep_cyclic_and_count_limits_refuse_before_serializing():
    deep = value = {}
    for _ in range(34):
        value["child"] = child = {}
        value = child
    with pytest.raises(ValueError, match="structure exceeds"):
        bundle((spec(deep),))
    cycle = {}
    cycle["self"] = cycle
    with pytest.raises(ValueError, match="structure exceeds"):
        bundle((spec(cycle),))
    with pytest.raises(ValueError, match="duplicate"):
        bundle((spec(), spec()))
    with pytest.raises(ValueError, match="too many"):
        bundle(tuple(spec(name=f"tool-{i}") for i in range(129)))
    assert bundle(()).tool_specs() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("planning", [False, True])
async def test_real_stateful_plan_capabilities_match_actual_model_input(tmp_path, planning):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    context = CONTEXT.model_copy(update={"environment_id": session.environment_id})
    documents = DocumentStore(tmp_path / "control.sqlite3")
    adapter = PlanTools(SessionPlans(documents), projects, context, run_id="run", alias="main")
    toolbox = Toolbox(
        cast(WorkerClient, SimpleNamespace()), context, plans=adapter if planning else None
    )
    expected = toolbox.specs
    seen = []

    class Model:
        async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]):
            assert messages[0]["content"] == SYSTEM_PROMPT + ("\n\n" + GUIDANCE if planning else "")
            assert tools == expected
            assert all(
                a is not b and a.parameters is not b.parameters
                for a, b in zip(tools, expected, strict=True)
            )
            seen.append(tools)
            return ModelTurn("Observed")

    try:
        transcript = await run_task(Model(), toolbox, "A small factual reply", run_id="run")
        assert transcript.stopped == "answered"
        assert len(seen) == 1
        provenance = transcript.prompt_provenance
        assert isinstance(provenance, CompleteProvenance)
        assert [c.component for c in provenance.components] == (
            ["main", "planning"] if planning else ["main"]
        )
        assert provenance.tools.names == tuple(tool.name for tool in expected)
        assert record(transcript, context=context).prompt_provenance == provenance
    finally:
        adapter.close()
        documents.close()


@pytest.mark.parametrize(
    ("features", "languages"),
    [((), ()), (("pdf", "admission", "provisioning"), ("python", "sage")), ((), ("python",))],
)
def test_actual_registry_capability_schemas_fit_validated_bounds(features, languages):
    toolbox = Toolbox(
        cast(WorkerClient, SimpleNamespace()), CONTEXT, features=features, languages=languages
    )
    assembled = assemble_main(SYSTEM_PROMPT, toolbox.specs, builtin_content=True)
    assert isinstance(assembled.provenance, CompleteProvenance)
    assert assembled.tool_specs() == toolbox.specs


@pytest.mark.asyncio
async def test_invalid_actual_planning_assembly_calls_no_model():
    class Model:
        async def complete(self, messages, tools):
            pytest.fail("an incomplete planning capability must not reach the model")

    toolbox = Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT)
    toolbox.plans = cast(PlanTools, object())
    with pytest.raises(ValueError, match="capability assembly"):
        await run_task(Model(), toolbox, "Inspect", run_id="run")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_retention_legacy_and_save_before_ack(tmp_path, monkeypatch, failure):
    class Model:
        async def complete(self, messages, tools):
            if failure:
                raise ModelError(HOSTILE)
            return ModelTurn("Observed")

    transcript = await run_task(
        Model(), Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT), "Inspect", run_id="run"
    )
    kept = record(transcript)
    assert kept.failure == ("model" if failure else None)
    assert HOSTILE not in kept.to_json()
    legacy = kept.model_dump(mode="json")
    legacy.pop("prompt_provenance")
    assert RunRecord.model_validate(legacy).prompt_provenance is None
    damaged = kept.model_dump(mode="json")
    damaged["messages"][0]["content"] += "forged"
    with pytest.raises(ValidationError, match="recorded system does not match"):
        RunRecord.model_validate(damaged)
    invalid = kept.model_dump(mode="json")
    invalid["prompt_provenance"]["bundle_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        RunRecord.model_validate(invalid)
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    acknowledged = []

    class Hook(TurnHook):
        async def acknowledge(self, record):
            entries = projects.runs("p", "s")
            assert len(entries) == 1
            assert (
                projects.run("p", "s", entries[0].name).prompt_provenance == kept.prompt_provenance
            )
            acknowledged.append(record.delivered.get("jobs", []))

    args = argparse.Namespace(transcript=None, state_dir=tmp_path)
    fields = kept.model_dump(mode="json")
    assert await cli._save(args, fields, int(failure), [Hook()]) == int(failure)
    assert acknowledged == [[]]
    assert await cli._save(args, fields, 0, [Hook()]) == 1
    assert acknowledged == [[]]


@pytest.mark.asyncio
async def test_cancellation_never_reaches_retention():
    class Model:
        async def complete(self, messages, tools):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_task(
            Model(),
            Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT),
            "Inspect",
            run_id="run",
        )


def test_unavailable_union_forbids_fabricated_pins():
    with pytest.raises(ValidationError):
        TypeAdapter(PromptProvenance).validate_python(
            {"status": "unavailable", "reason": "system_too_large", "system": {"sha256": "0" * 64}}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_actual_cli_retains_provenance_without_credentials(tmp_path, monkeypatch, failure):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    context = CONTEXT.model_copy(update={"environment_id": session.environment_id})
    closed = []

    class Client:
        base_url = "http://unused.invalid"

        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("client")

        async def capabilities(self, *args):
            return SimpleNamespace(
                languages=(),
                features=(),
                location=SimpleNamespace(
                    isolation_evidence=None,
                    pool_id="p",
                    backend="local",
                    sandbox_id="e",
                    hostname="synthetic",
                ),
            )

    class Model:
        async def complete(self, messages, tools):
            if failure:
                raise ModelError(HOSTILE)
            return ModelTurn("Observed")

    async def resolved(*args):
        return context

    script = tmp_path / "script.json"
    script.write_text("[]")
    args = argparse.Namespace(
        script=script,
        run_id="cli-run",
        worker="unused",
        token_file=None,
        allow_unisolated=True,
        state_dir=tmp_path,
        watcher_model=None,
        reviewer_model=None,
        math_node=None,
        watcher_calls_per_hour=1,
        watcher_first_check=1,
        watcher_interval=1,
        task="A small factual reply",
        max_turns=1,
        transcript=None,
    )
    monkeypatch.setattr(cli, "WorkerClient", Client)
    monkeypatch.setattr(cli, "_context", resolved)
    monkeypatch.setattr(cli, "_read_secret", lambda _: "test-secret-unused-not-a-credential")
    monkeypatch.setattr(ScriptedModel, "from_json", lambda _: Model())
    assert await cli._run(args) == int(failure)
    assert closed == ["client"]
    (entry,) = projects.runs("p", "s")
    kept = projects.run("p", "s", entry.name)
    assert kept.failure == ("model" if failure else None)
    assert isinstance(kept.prompt_provenance, CompleteProvenance)
    assert [c.component for c in kept.prompt_provenance.components] == ["main", "planning"]
    assert HOSTILE not in kept.to_json()
    assert "test-secret-unused" not in kept.to_json()


@pytest.mark.parametrize(
    "parameters",
    [{"enum": [1 << 4095] * 4096}, {"enum": ["\0" * 400000]}, {"enum": ['"\\' * 200000]}],
)
def test_encoding_expansion_is_refused_before_json(monkeypatch, parameters):
    def unexpected(*args, **kwargs):
        pytest.fail("oversized schema must refuse before any aggregate JSON encoding")

    monkeypatch.setattr(prompt_bundle, "_json", unexpected)
    with pytest.raises(ValueError, match="bytes exceed"):
        bundle((spec(parameters),))


@pytest.mark.parametrize(
    ("system", "guidance"),
    [
        ("x" * (MAX_SYSTEM_BYTES + 1), None),
        ("normal", (GUIDANCE_VERSION, "x" * (MAX_SYSTEM_BYTES + 1))),
        ("normal", (GUIDANCE_VERSION, "\ud800")),
    ],
)
def test_invalid_installed_content_refuses(system, guidance):
    with pytest.raises(ValueError, match="installed MAIN prompt"):
        assemble_main(system, (), builtin_content=True, guidance=guidance)


@pytest.mark.asyncio
async def test_actual_second_call_uses_fresh_owned_schema_after_first_call_mutation(monkeypatch):
    toolbox = Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT)
    original = toolbox.specs[0].parameters.copy()
    calls = []

    class Model:
        async def complete(self, messages, tools):
            assert tools[0].parameters == original
            calls.append(tools)
            if len(calls) == 1:
                tools[0].parameters["description"] = "synthetic model-side mutation"
                return ModelTurn(None, (ToolCall("call", "read_file", "{}"),))
            return ModelTurn("Observed")

    async def observed(*args, **kwargs):
        return ToolOutcome("untrusted source text", {})

    monkeypatch.setattr(toolbox, "call", observed)
    transcript = await run_task(Model(), toolbox, "Inspect", run_id="run")
    assert transcript.stopped == "answered"
    assert len(calls) == 2
    assert calls[0][0].parameters is not calls[1][0].parameters
    assert toolbox.specs[0].parameters == original
    assert isinstance(record(transcript).prompt_provenance, CompleteProvenance)


@pytest.mark.asyncio
@pytest.mark.parametrize("builtin", [False, True])
async def test_metadata_overflow_refuses_builtin_but_preserves_custom_invocation(builtin):
    toolbox = Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT)
    toolbox.specs = tuple(spec(name=f"tool-{i:03}-" + "x" * 119) for i in range(128))
    system = SYSTEM_PROMPT if builtin else "Custom system remains exact"
    called = []

    class Model:
        async def complete(self, messages, tools):
            assert messages[0]["content"] == system
            assert tools == toolbox.specs
            called.append(True)
            return ModelTurn("Observed")

    if builtin:
        with pytest.raises(ValueError, match="installed MAIN prompt metadata"):
            await run_task(Model(), toolbox, "Inspect", run_id="run", system=system)
        assert called == []
    else:
        transcript = await run_task(Model(), toolbox, "Inspect", run_id="run", system=system)
        assert transcript.stopped == "answered"
        assert called == [True]
        assert transcript.prompt_provenance == UnavailableProvenance(reason="metadata_too_large")
