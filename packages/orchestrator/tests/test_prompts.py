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
from sandbox_science.orchestrator.delegation.prompt import (
    CHECK_PROMPT,
    CHECK_VERSION,
    TASK_PROMPT,
    TASK_VERSION,
)
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
from sandbox_science.orchestrator.provision import PROVISION
from sandbox_science.orchestrator.session_plans import SessionPlans
from sandbox_science.orchestrator.skills.derive import PROMPT as PASS_PROMPT
from sandbox_science.orchestrator.skills.derive import VERSION as PASS_VERSION
from sandbox_science.orchestrator.skills.index import FRAMING as SKILLS_FRAMING
from sandbox_science.orchestrator.skills.index import FRAMING_VERSION as SKILLS_VERSION
from sandbox_science.orchestrator.stock_prompts import TEMPLATE_IDS, select_template
from sandbox_science.orchestrator.tools import ADMIT_HOSTS, PDF_SPECS, Toolbox, mcp_tools_spec
from sandbox_science.orchestrator.watcher_prompt import PROMPT_VERSION
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
HOSTILE = "Authorization: Bearer test-secret-prompt-not-a-credential"

# Each installed version and the SHA-256 of its UTF-8 text. Changing a text means a new
# version: add it here, and keep the old version's entry for its records.
GOLDEN = {
    "sandbox-main-15": "af20158edc4af5d4ad4930e869fe63e50f2ddaf9111faa51e08e9a0d50eff240",
    "sandbox-delegated-check-13": (
        "a37f12e337620c81928a5203073b1d6656c6ed713b110528b165d91c1e38960f"
    ),
    "sandbox-delegated-task-13": "5100c90d70c6c6d7874f802f7117f655943073b896ad6f568702d0849223bf20",
    "sandbox-delegated-check-12": (
        "d7aedb60054af516dbcf4d434c40316b54417a3550856bfb5745f39aa1bc7c19"
    ),
    "sandbox-delegated-task-12": "dbc6ffade88a1a97f17fa76cbf4b8cf567aedb89c2365628d78b1c973c62dc23",
    "sandbox-main-14": "2e7fb45eafdd59d481bbbe05b9180117d1f2778fbce6772200773690e0c5e8d2",
    "sandbox-delegated-check-11": (
        "5b889a336e9bac771e63eedfdfc0cef5c1c5c6144bb3a3bd0c1fc1642628aad6"
    ),
    "sandbox-delegated-task-11": "32813ebc1a6b0724e242ace6a78e7e5708c070a1518014493f1c507759f8229c",
    "sandbox-main-13": "f711149fab6e3794b8dc812d2b5cc045b586f75f0402d82e920f899c27c4c3d6",
    "sandbox-delegated-check-10": (
        "7a7a25a2f16db7fd2b1aa785fb4a16311a94298eaed54559fe079495e71a155b"
    ),
    "sandbox-delegated-task-10": "a841796e6f7c045fd406db57b271a91d4df3fc5ccfc30d3f4542235c985396da",
    "sandbox-main-12": "6da989a0f7f7fb55760bda37ab9cdb554c9c19a9e316f2ce0528afec12aacd19",
    "sandbox-delegated-check-9": "35de7e126f4daf28e92c08248ea66ba4feeac529955e10da77ee9e738dd7e489",
    "sandbox-delegated-task-9": "70e83d0cf1f6f803b862cf773d3c35f1d67a390391ab0419ade26836989f7ee1",
    "sandbox-main-11": "f67fd27c0f4937b2e370bb07563ac8aa6d41089f8307e61f5f32e505093d057c",
    "sandbox-main-10": "e8b5e21ed6aa88254c3b40661874fea44954d4c4d4ef7d577c198ee4419f5f23",
    "sandbox-main-plan-2": "768cd18b5403bf7d316a60eb89ca100ba270ec29f5195343baf41d0d55a301df",
    "sandbox-stock-intake-2": "2bdb3f65587f99c7f78555349196c27f7bb2cbe1429b0adebb3ca549e93e6572",
    "sandbox-stock-experiment-2": (
        "2de1561e772650fc64280da0aaf60b650eab5063526416a6635fd51c2f8a50dd"
    ),
    "sandbox-stock-report-2": "c612dd1f37614dc0ea8e317b2ade2f320ec79bc327604b01ff0cbe59f1a7e63f",
    "sandbox-main-9": "6ad2ced30036974bde1ecfc642f56c6eca32d3149652563c702b4bbc8e59f2d6",
    "sandbox-main-plan-1": "efb031b0fab2b9855dc3c9adcb869108dd835b725f878b977eec9ba118feb866",
    "sandbox-stock-intake-1": "e846770bc153995c6da1c87cca1913400d0008f3983361246d04d9b9a94f45ab",
    "sandbox-stock-literature-2": (
        "ccdebe03645b7ec025486235bfe08ccfebd37f0fbd84d2f28405be6b0aa7e565"
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
    "sandbox-notice-review-challenged-1": (
        "11b7df629d84233aae8275c05199f83db8d5f4f94b75517e4b77065b759ec47c"
    ),
    "sandbox-notice-review-counts-2": (
        "37d2f85e902081c4fc1623d53bfe31a9b7443f88352d655fa8e50de424f5763e"
    ),
    "sandbox-notice-math-1": "c65c1f751f7b2928f15f0d846b816e2287eeb3ddd63fa63ad083226fe0cf19c9",
    "sandbox-kernels-1": "a0211d3dbc39708fdf924610123e76f5cb70c5c25d888c29188d858b594733d9",
    "sandbox-notice-delegations-2": (
        "c5205b989bf47c287caabdd8f9b7c3c8a7190be02ab1cabfc53631e904c1157a"
    ),
    "sandbox-notice-delegations-attached-1": (
        "c1be06fbad94b08b6eceb24ac20ed31ad2f77b7664c262cd06c623fe99c7407e"
    ),
    "sandbox-notice-delegations-running-2": (
        "c9a8bab83313d421f56598703924f57397727a0983b4c9156bcff6df39a9970a"
    ),
    "sandbox-notice-outcome-correction-1": (
        "4d308ab03dd3549555c60202b1690a59e7025667f6189cdb5abb4aa25cf6eae1"
    ),
    "sandbox-delegated-check-8": "1911d9d5cf0036fe6d952132042f96124e0845bde8271078eb65c5960b655a78",
    "sandbox-delegated-task-8": "0928db5b6b9d08a097dd67464f69ff56eae48890d32f982dd2e516d71ae157fc",
    "sandbox-job-watcher-1": "4b545fe81e35622bf1b894c435833d78fb122116f9364d429b21c6c3551e1ab1",
    "sandbox-consistency-9": "3905f61a0b76144e6ec941e436cd37dea2343b76a7f84f34e5ecf65d8048a588",
    "sandbox-memory-run-1": "b69cc482f75853487a499ac89b5701b148066e919842a9970e6e318b2bdce121",
    "sandbox-memory-reviewer-1": (
        "d391d1a7e6c33b4fca0970450432103c38b5ea34b80821210f7c9d27f80b9742"
    ),
    "sandbox-memory-watcher-1": (
        "7d0f75c8dfcd72a66c74e0a9d7bd76cb71df5286de234f82573034f717d53790"
    ),
    "sandbox-notice-skills-3": ("2cce68a8be83ec7606f3673b325dc6bf3053b1889745ea1be8857cb32f2a9d1c"),
    "sandbox-notice-skills-words-2": (
        "95abbf47b2ed323a776ad1ed94e791530e2fc9352cdea710406c22a3aa572063"
    ),
    "sandbox-skills-index-4": ("3b46d2a21cd57e15bc9ef583284bffd33f0ec72e5c33e8adb456e345c2cf8ef5"),
    "sandbox-skills-pass-1": ("c1959f76747a1361f5e9ebe5b97c4b675c3fe45037c28e3dc1513a8fc56ff0b6"),
    "sandbox-state-block-2": "c32d80a06a317c3faa60cc2a997fa4040b1f75479f4e00995dce3afcdf6c27c1",
    "sandbox-digest-2": "6e3a9bb129ea96671d73d6fd7e5f066451184232ae2278bc78b3e4d3f6a082ca",
    "sandbox-task-constraints-1": (
        "5c0a224ad9bba6d3803ebf613e5b4df51e0341425b6f7e8b95244294d9f81681"
    ),
    "sandbox-notice-redelivered-1": (
        "706f5ef5f1441a5194177af083185c8eb53cf9f9b31cfd7bb5609d3e666b4726"
    ),
    "sandbox-kernel-lines-1": "99de31152145fd974d443f21919e252f90f922b3c961e183522d98c46c79c246",
    "sandbox-fold-region-3": "b844c1fa240b73328d646cb1304caff16d5114ebee7b6c4a0ddf75a886e6cf00",
    "sandbox-fold-enrich-3": "c76e9d3e1abb63bb694323af221cfd8fd772b36d096b72368356f01cf40156aa",
    "sandbox-fold-overview-2": "019dbbdb8a5b18c94d8a82b88b06b42bc5e81048e4c668481404bfefb3e7ddb9",
    "sandbox-consolidation-1": ("d67d1fdf2583ba97951b057b2246ce1d458a60806edf9bf7a7545920cf33f20a"),
    "sandbox-image-1": "6aad833a6a22472424296c64943b54adcd0d064aef1db5403c5f5eea2eb95f11",
    "sandbox-notice-report-1": "93112b881cb91462c7b85f862d93cd154a9d5c47684af6815a4eedc9156c9c48",
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
        CHECK_VERSION: CHECK_PROMPT,
        TASK_VERSION: TASK_PROMPT,
        SKILLS_VERSION: SKILLS_FRAMING,
        PASS_VERSION: PASS_PROMPT,
    }
    texts |= {select_template(t).version: select_template(t).text for t in TEMPLATE_IDS}
    texts |= {framing.version: framing.text for framing in FRAMINGS}
    return texts


def test_a_refusal_is_a_decision_and_a_miss_is_not_absence() -> None:
    """Lane K's triage, P1 and P2: the system message and the descriptions of the tools a
    policy can refuse, or whose search can miss, say so where the model reads them."""
    mcp_tools = mcp_tools_spec("(an index)")
    (pdf_search,) = [spec for spec in PDF_SPECS if spec.name == "pdf_search"]
    for text in (SYSTEM_PROMPT, ADMIT_HOSTS.description, PROVISION.description):
        assert "the policy's decision" in text or "the user's policy deciding" in text
    assert "a refused call is its decision" in mcp_tools.description
    assert "never resubmit it unchanged" in SYSTEM_PROMPT
    assert "retry once it is approved" in SYSTEM_PROMPT  # a pending approval is not a refusal
    assert "No hit means only" in pdf_search.description
    assert "not that nothing exists" in mcp_tools.description
    assert "never conclude from a miss" in SYSTEM_PROMPT
    # Lane W's saved output (#388): what a truncated result shows is a sample.
    assert "from the saved file it names, never from the sample" in SYSTEM_PROMPT
    assert "no evidence that the work or attack does not exist" in (
        select_template("literature").text
    )


def test_each_installed_version_names_its_exact_text() -> None:
    sha = {
        version: hashlib.sha256(text.encode()).hexdigest() for version, text in installed().items()
    }
    # Retired versions remain pinned for historical records as the installed
    # catalog advances; every installed version must still match its exact hash.
    assert sha.items() <= GOLDEN.items()


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
        "messages": transcript.context.messages,
        "tools": transcript.tool_records,
        "prompt_provenance": transcript.context.prompt_provenance,
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
    pins = transcript.context.prompt_provenance
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

        async def fence(self, request):
            return request

        async def environment(self, environment_id):
            raise WorkerAPIError(ErrorKind.UNSUPPORTED, "a local worker manages no environments")

        async def claim_environment(self, request):
            from sandbox_science.contracts.environment_claims import EnvironmentClaimResult

            return EnvironmentClaimResult(
                environment_id=request.context.environment_id, state="unmanaged"
            )

        async def capabilities(self, *args):
            location = SimpleNamespace(
                isolation_evidence=None, pool_id="p", backend="local", sandbox_id="e", hostname="h"
            )
            return SimpleNamespace(languages=(), features=(), location=location, kernel_reason=None)

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
