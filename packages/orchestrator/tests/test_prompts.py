# SPDX-License-Identifier: MIT
"""Prompt versions name exact text, and run records keep pins of what a run was given."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator import cli, notices
from sandbox_science.orchestrator.consistency.rubric import RUBRIC, RUBRIC_VERSION
from sandbox_science.orchestrator.documents import DocumentStore, DocumentTool
from sandbox_science.orchestrator.harness import save
from sandbox_science.orchestrator.hooks import TurnHook
from sandbox_science.orchestrator.jobs import WATCHER_PROMPT
from sandbox_science.orchestrator.loop import MAIN_VERSION, SYSTEM_PROMPT, run_task
from sandbox_science.orchestrator.memory_prompt import (
    REVIEWER_FRAMING,
    REVIEWER_VERSION,
    RUN_FRAMING,
    RUN_VERSION,
    WATCHER_FRAMING,
    WATCHER_VERSION,
)
from sandbox_science.orchestrator.model import (
    Message,
    ModelError,
    ModelTurn,
    ScriptedModel,
    ToolSpec,
)
from sandbox_science.orchestrator.notices import FRAMINGS
from sandbox_science.orchestrator.plan_tools import GUIDANCE, GUIDANCE_VERSION, PlanTools
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.prompts import PromptPins, Section, assemble, pin_tools
from sandbox_science.orchestrator.session_plans import SessionPlans
from sandbox_science.orchestrator.stock_prompts import TEMPLATE_IDS, select_template
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.watcher_prompt import PROMPT_VERSION
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
HOSTILE = "Authorization: Bearer test-secret-prompt-not-a-credential"

# Each installed version and the SHA-256 of its UTF-8 text. Changing a text means a new
# version: add it here, and keep the old version's entry for its records.
GOLDEN = {
    "sandbox-main-3": "8e710bfbfddb81951f2222d9e605200dd2849bec46c5d2b69315178981049f98",
    "sandbox-main-plan-1": "efb031b0fab2b9855dc3c9adcb869108dd835b725f878b977eec9ba118feb866",
    "sandbox-stock-intake-1": "e846770bc153995c6da1c87cca1913400d0008f3983361246d04d9b9a94f45ab",
    "sandbox-stock-literature-1": (
        "a08bf961f0df4e7f328cbc34eb999983a09d38da9a0f20e23fc1b05bb7e9e822"
    ),
    "sandbox-stock-experiment-1": (
        "7a13ddca7cd62c11d9baf11c61a3c642ad89db29c2c25d0375c18aaa7b37ae40"
    ),
    "sandbox-stock-proof-audit-1": (
        "8c1d68c6b70afdc772a187eef7fefc1bd188e3bc40cd7e81908804cb0f55225d"
    ),
    "sandbox-stock-report-1": "e90c5ffdef09050bd3362e6077efe37965ee5a5f0e25fd33fd888a68f9d7dce7",
    "sandbox-notice-requests-1": "735a675d32ae12bd4cbeaf5e7189dc83181e2aac28501f14f3612a855c87958d",
    "sandbox-notice-jobs-1": "834393d48266ca1af8f6c676c6dfeee0892a50c3ec312e658e93268973e5449a",
    "sandbox-notice-jobs-attached-1": (
        "962543a55f349e3fab95b93c92e0a765c03933193b4e1be7fa70b92032e20f67"
    ),
    "sandbox-notice-plans-1": "4257527cd034fa0bc91f2fe0939af3ff63e2d28fddd04dfb438d39a94e30d00b",
    "sandbox-notice-review-findings-1": (
        "d9fe98f7e1b697bb2d3264496fd601484a9dbce030d3fd0985e049aabd9e67a1"
    ),
    "sandbox-notice-review-counts-1": (
        "38520c582417fef8435cd529982761dfb11663e7f018977f6f4b589934099593"
    ),
    "sandbox-notice-math-1": "c65c1f751f7b2928f15f0d846b816e2287eeb3ddd63fa63ad083226fe0cf19c9",
    "sandbox-kernels-1": "a0211d3dbc39708fdf924610123e76f5cb70c5c25d888c29188d858b594733d9",
    "sandbox-job-watcher-1": "4b545fe81e35622bf1b894c435833d78fb122116f9364d429b21c6c3551e1ab1",
    "sandbox-consistency-1": "311981a62b6e7fe3416ffe3836ab1492e14c25c336db90c82fa9e4f88b23b3d9",
    "sandbox-memory-run-1": "b69cc482f75853487a499ac89b5701b148066e919842a9970e6e318b2bdce121",
    "sandbox-memory-reviewer-1": (
        "d391d1a7e6c33b4fca0970450432103c38b5ea34b80821210f7c9d27f80b9742"
    ),
    "sandbox-memory-watcher-1": (
        "7d0f75c8dfcd72a66c74e0a9d7bd76cb71df5286de234f82573034f717d53790"
    ),
}


def installed() -> dict[str, str]:
    texts = {
        MAIN_VERSION: SYSTEM_PROMPT,
        GUIDANCE_VERSION: GUIDANCE,
        PROMPT_VERSION: WATCHER_PROMPT,
        RUBRIC_VERSION: RUBRIC,
        RUN_VERSION: RUN_FRAMING,
        REVIEWER_VERSION: REVIEWER_FRAMING,
        WATCHER_VERSION: WATCHER_FRAMING,
    }
    texts |= {select_template(t).version: select_template(t).text for t in TEMPLATE_IDS}
    texts |= {framing.version: framing.text for framing in FRAMINGS}
    return texts


def test_each_installed_version_names_its_exact_text() -> None:
    sha = {
        version: hashlib.sha256(text.encode()).hexdigest() for version, text in installed().items()
    }
    assert sha == GOLDEN


def spec(parameters: dict[str, object] | None = None, *, name: str = "inspect") -> ToolSpec:
    return ToolSpec(name, "Read data", parameters if parameters is not None else {"type": "object"})


def test_sections_are_joined_and_pinned_in_order() -> None:
    text, pins = assemble(
        "main",
        [Section("custom", None, "Original Unicode ∀x\n"), Section("planning", "v1", "Plan.")],
        [spec()],
    )
    assert text == "Original Unicode ∀x\n\n\nPlan."
    assert [(p.id, p.version, p.bytes) for p in pins.sections] == [
        ("custom", None, len("Original Unicode ∀x\n".encode())),
        ("planning", "v1", 5),
    ]
    assert pins.sections[1].sha256 == hashlib.sha256(b"Plan.").hexdigest()
    assert [p.version for p in pins.notices] == [f.version for f in FRAMINGS]
    # A text that is not valid UTF-8 still gets a pin.
    _, odd = assemble("main", [Section("custom", None, "bad\ud800text")], [])
    assert odd.sections[0].bytes == 10


def test_the_tools_pin_follows_order_and_content_not_key_order() -> None:
    first = spec({"properties": {"z": {}, "a": {}}, "required": ["z", "a"]})
    reordered = spec({"required": ["z", "a"], "properties": {"a": {}, "z": {}}})
    assert pin_tools([first]) == pin_tools([reordered])
    assert pin_tools([first, spec(name="b")]) != pin_tools([spec(name="b"), first])
    assert pin_tools([spec({"required": ["a", "z"]})]) != pin_tools(
        [spec({"required": ["z", "a"]})]
    )
    assert pin_tools([first]).names == ("inspect",)


def record(transcript, **changes) -> RunRecord:
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


@pytest.mark.parametrize("planning", [False, True])
async def test_a_run_records_the_sections_and_tools_its_model_was_given(tmp_path, planning):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    session = projects.open_session("p", "s", dedicated=False)
    context = CONTEXT.model_copy(update={"environment_id": session.environment_id})
    documents = DocumentStore(tmp_path / "control.sqlite3")
    adapter = PlanTools(SessionPlans(documents), projects, context, run_id="run", alias="main")
    toolbox = Toolbox(
        cast(WorkerClient, SimpleNamespace()), context, plans=adapter if planning else None
    )

    class Model:
        async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]):
            assert messages[0]["content"] == SYSTEM_PROMPT + ("\n\n" + GUIDANCE if planning else "")
            assert tuple(tools) == toolbox.specs
            return ModelTurn("Observed")

    try:
        transcript = await run_task(Model(), toolbox, "A small factual reply", run_id="run")
    finally:
        adapter.projects.close()
        documents.close()
    pins = transcript.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert [(p.id, p.version) for p in pins.sections] == [("main", MAIN_VERSION)] + (
        [("planning", GUIDANCE_VERSION)] if planning else []
    )
    assert pins.tools == pin_tools(toolbox.specs)
    assert record(transcript, context=context).prompt_provenance == pins


async def test_an_incomplete_planning_assembly_calls_no_model() -> None:
    class Model:
        async def complete(self, messages, tools):
            pytest.fail("an incomplete planning capability must not reach the model")

    toolbox = Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT)
    toolbox.plans = cast(PlanTools, object())
    with pytest.raises(ValueError, match="capability assembly"):
        await run_task(Model(), toolbox, "Inspect", run_id="run")


@pytest.mark.parametrize("failure", [False, True])
async def test_records_keep_pins_as_read_and_save_before_acknowledging(tmp_path, failure):
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
    assert RunRecord.model_validate_json(kept.to_json()) == kept
    unknown = kept.model_dump(mode="json")
    unknown.pop("prompt_provenance")
    assert RunRecord.model_validate(unknown).prompt_provenance is None
    # The pins are not checked against the messages again: a record that differs loads.
    changed = kept.model_dump(mode="json")
    changed["messages"][0]["content"] += "changed"
    assert RunRecord.model_validate(changed).prompt_provenance == kept.prompt_provenance
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    acknowledged = []

    class Hook(TurnHook):
        async def acknowledge(self, record):
            (entry,) = projects.runs("p", "s")
            assert projects.run("p", "s", entry.name).prompt_provenance == kept.prompt_provenance
            acknowledged.append(record.run_id)

    fields = kept.model_dump(mode="json")
    status = await save(fields, int(failure), [Hook()], projects=projects, error=print)
    assert (status, acknowledged) == (int(failure), ["run"])
    # The same record again refuses; nothing is acknowledged twice.
    assert await save(fields, 0, [Hook()], projects=projects, error=print) == 1
    assert acknowledged == ["run"]


def test_an_older_runs_prompt_bundle_is_kept_as_read() -> None:
    bundle = {
        "status": "complete",
        "assembly_version": "sandbox-main-bundle-1",
        "role": "main",
        "components": [{"component": "main", "version": "sandbox-main-1", "bytes": 1}],
        "bundle_sha256": "0" * 64,
    }
    now = datetime.now(UTC)
    kept = RunRecord(
        run_id="old",
        context=CONTEXT,
        task="t",
        started_at=now,
        ended_at=now,
        stopped="answered",
        final_text="x",
        messages=[{"role": "system", "content": "anything"}],
        tools=[],
        prompt_provenance=bundle,
    )
    assert kept.prompt_provenance == bundle


async def test_cancellation_never_reaches_retention() -> None:
    class Model:
        async def complete(self, messages, tools):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_task(
            Model(), Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT), "Inspect", run_id="r"
        )


@pytest.mark.parametrize("failure", [False, True])
async def test_the_cli_keeps_the_pins_without_credentials(tmp_path, monkeypatch, failure):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)

    class Client:
        base_url = "http://unused.invalid"

        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def environment(self, environment_id):
            raise WorkerAPIError(ErrorKind.UNSUPPORTED, "a local worker manages no environments")

        async def capabilities(self, *args):
            location = SimpleNamespace(
                isolation_evidence=None, pool_id="p", backend="local", sandbox_id="e", hostname="h"
            )
            return SimpleNamespace(languages=(), features=(), location=location)

    class Model:
        async def complete(self, messages, tools):
            if failure:
                raise ModelError(HOSTILE)
            return ModelTurn("Observed")

    script = tmp_path / "script.json"
    script.write_text("[]")
    args = argparse.Namespace(
        project="p", session="s", environment=None, dedicated=False, research_template=None,
        script=script, run_id="cli-run", worker="unused", token_file=None, allow_unisolated=True,
        state_dir=tmp_path, watcher_model=None, reviewer_model=None, math_node=None,
        watcher_calls_per_hour=1, watcher_first_check=1, watcher_interval=1,
        review_token_budget=400000, review_calls_per_hour=240,
        task="A small factual reply", max_turns=1, max_input_chars=400_000, transcript=None,
        mcp_proxy=None, mcp_token_file=None, mcp_server=[],
    )  # fmt: skip
    monkeypatch.setattr(cli, "WorkerClient", Client)
    monkeypatch.setattr(cli, "read_secret", lambda _: "test-secret-unused-not-a-credential")
    monkeypatch.setattr(ScriptedModel, "from_json", lambda _: Model())
    assert await cli._run(args) == int(failure)
    (entry,) = projects.runs("p", "s")
    kept = projects.run("p", "s", entry.name)
    assert isinstance(kept.prompt_provenance, PromptPins)
    assert [p.id for p in kept.prompt_provenance.sections] == ["main", "planning"]
    assert HOSTILE not in kept.to_json()
    assert "test-secret-unused" not in kept.to_json()


def test_every_notice_framing_is_pinned() -> None:
    declared = {value for value in vars(notices).values() if isinstance(value, notices.Framing)}
    assert declared == set(FRAMINGS)
    assert len({framing.version for framing in FRAMINGS}) == len(FRAMINGS)


def test_a_toolbox_refuses_two_tools_with_one_name() -> None:
    class Documents:
        specs = (ToolSpec("shell", "Another shell", {"type": "object"}),)

    with pytest.raises(ValueError, match="two offered tools have one name: shell"):
        Toolbox(
            cast(WorkerClient, SimpleNamespace()),
            CONTEXT,
            documents=cast(DocumentTool, Documents()),
        )
