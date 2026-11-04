# SPDX-License-Identifier: MIT
"""Tool groups (docs/specs/tools.md, tool groups): a run is offered its core tools from
its start and loads the others by group, with ``load_tools``, by calling one of their
tools, or when a notice that needs a group enters its conversation. A loaded group is
in the run's record and pins."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import JsonValue

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import capabilities as caps
from sandbox_science.orchestrator.agent_messages import (
    CHILD_SPEC,
    PARENT_SPEC,
    STATUS_SPEC,
    WAIT_SPEC,
    WITHDRAW_SPEC,
)
from sandbox_science.orchestrator.confinement.host import HostLoops
from sandbox_science.orchestrator.confinement.models import (
    CallbackGateway,
    CallbackModel,
    RuntimeModels,
)
from sandbox_science.orchestrator.confinement.runtime_loop import RuntimeLoops
from sandbox_science.orchestrator.confinement.transport import Capability, Channel, TransportError
from sandbox_science.orchestrator.consistency.controller import AUTHOR_SPECS, Reviews
from sandbox_science.orchestrator.context.history import SPECS as HISTORY_SPECS
from sandbox_science.orchestrator.continuity.resources import ResourceRequest
from sandbox_science.orchestrator.delegation.outcome import SUBMIT, Asked, Outcome, Target
from sandbox_science.orchestrator.delegation.prompt import (
    CHECK_PROMPT,
    TASK_PROMPT,
    processes_text,
    write_area_text,
)
from sandbox_science.orchestrator.delegation.specs import SPECS as DELEGATION_SPECS
from sandbox_science.orchestrator.delegation.tracks import SPECS as TRACK_SPECS
from sandbox_science.orchestrator.documents import annotation_tools
from sandbox_science.orchestrator.documents.tool import SPECS as DOCUMENT_SPECS
from sandbox_science.orchestrator.documents.tool import WorkRequests
from sandbox_science.orchestrator.harness import Role
from sandbox_science.orchestrator.hooks import Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.jobs import JOB_SPECS, JobNotices
from sandbox_science.orchestrator.ledger.tool import PROPOSALS
from sandbox_science.orchestrator.ledger.tool import SPECS as LEDGER_SPECS
from sandbox_science.orchestrator.loop import SYSTEM_PROMPT, Transcript, run_task
from sandbox_science.orchestrator.mathchecks.controller import MathChecks
from sandbox_science.orchestrator.memory_prompt import RUN_FRAMING
from sandbox_science.orchestrator.memory_tools import SPECS as MEMORY_SPECS
from sandbox_science.orchestrator.model import ChatModel, Message, ModelGateway, ModelTurn, ToolSpec
from sandbox_science.orchestrator.notices import REVIEW_FINDINGS
from sandbox_science.orchestrator.plan_tools import GUIDANCE
from sandbox_science.orchestrator.plan_tools import SPECS as PLAN_SPECS
from sandbox_science.orchestrator.prompts import GroupPin, PromptPins, pin_tools, schema_json
from sandbox_science.orchestrator.purpose import offered
from sandbox_science.orchestrator.question_tool import SPECS as QUESTION_SPECS
from sandbox_science.orchestrator.resource_hooks import SOURCE as RESOURCES
from sandbox_science.orchestrator.resource_hooks import SPEC as RESOURCE_SPEC
from sandbox_science.orchestrator.resource_hooks import USAGE_SPEC, Resources
from sandbox_science.orchestrator.skills.tool import SPEC as SKILL_SPEC
from sandbox_science.orchestrator.tool_entry import (
    GROUP_OF,
    GROUPS,
    LOAD_TOOLS,
    NOTICE_GROUPS,
)
from sandbox_science.orchestrator.tools import HARNESS_TOOL_NAMES, MCP_TOOLS, Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .loop_harness import API_KEY, PROJECT, SESSION, Harness, Json, Reply, call, six_sources
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .scripted import ToolCall
from .test_run_service import records, serving

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e", actor="agent")
FEATURES = ("jobs", "pdf", "admission", "provisioning")


async def answer(*args: object, **kwargs: object) -> tuple[str, dict[str, Any]]:
    return json.dumps({"done": True}), {"answered": True}


async def nothing(*args: object, **kwargs: object) -> list[object]:
    return []


def domain(specs: Sequence[ToolSpec] = (), **kwargs: Any) -> Any:
    """A domain controller whose every tool answers at once, whose plan admits every
    call and whose documents have no notices."""
    return SimpleNamespace(
        specs=tuple(specs),
        call=answer,
        author_call=answer,
        handlers={spec.name: answer for spec in specs},
        check_dispatch=nothing,
        notices=nothing,
        **kwargs,
    )


EACH_KIND = (
    Asked("d1", "The table", "artifact"),
    Asked("d2", "The notes", "file"),
    Asked("d3", "A task to recheck C-1", "entry"),
)
"""A task brief's deliverables, one of each kind, which ``submit_result``'s schema takes."""


def bundles(role: str, *, plans: bool = True) -> tuple[caps.Capability, ...]:
    """Every domain a run of ``role`` has, bound as the harness binds them."""
    main = role == "main"
    kwargs: dict[str, Any] = {
        "documents": domain((*DOCUMENT_SPECS, *annotation_tools.SPECS)),
        "jobs": domain(JOB_SPECS),
        "ledger": domain((*LEDGER_SPECS, PROPOSALS) if main else LEDGER_SPECS),
        "memory": domain(MEMORY_SPECS),
        "resources": domain((RESOURCE_SPEC, USAGE_SPEC), name="resources"),
        "skills": domain(
            (SKILL_SPEC,),
            given=SimpleNamespace(pin=None, section=None),
            summary=lambda first: None,
            stager=None,
        ),
        "history": domain(HISTORY_SPECS),
        "images": domain(),
        "math_checks": domain(),
        "recorder": domain(),
        "artifacts": domain(),
    }
    if main:
        kwargs.update(
            delegations=domain(DELEGATION_SPECS, name="delegations", live=lambda: ()),
            mailbox=domain(
                (CHILD_SPEC, STATUS_SPEC, WAIT_SPEC, WITHDRAW_SPEC), name="agent_messages"
            ),
            reviews=domain(AUTHOR_SPECS),
            questions=domain(QUESTION_SPECS, name="questions"),
        )
        if plans:
            kwargs["plans"] = domain(PLAN_SPECS)
    else:
        kwargs.update(
            mailbox=domain((PARENT_SPEC, STATUS_SPEC, WAIT_SPEC), name="agent_messages"),
            tracks=domain(TRACK_SPECS),
            # A delegated run returns its result through submit_result (PD-18).
            outcome=Outcome(task=True, deliverables=EACH_KIND)
            if role == "task"
            else Outcome(Target("ent-1", "C-1", 1)),
        )
    found = Toolbox(cast(WorkerClient, object()), CONTEXT, features=FEATURES, **kwargs)
    return found.capabilities.bundles


def full(role: str) -> Toolbox:
    """A role's every tool, offered from the start, as a direct caller's toolbox is."""
    selected = caps.CapabilitySet(
        {"main": caps.main, "task": caps.task, "check": caps.check}[role](bundles(role)).bundles
    )
    return toolbox(role, selected)


def toolbox(
    role: str, selected: caps.CapabilitySet | None = None, *, plans: bool = True
) -> Toolbox:
    """A run of ``role`` as the harness offers it its tools: its trusted factory's; a
    main run without ``plans`` has no plan tools, whose hook the loop would need."""
    if selected is None:
        select = {"main": caps.main, "task": caps.task, "check": caps.check}[role]
        selected = select(bundles(role, plans=plans))
    return Toolbox(
        cast(WorkerClient, object()),
        CONTEXT,
        languages=("python", "sage"),
        features=FEATURES,
        delegated=role != "main",
        capabilities=selected,
    )


def names(specs: Iterable[ToolSpec]) -> list[str]:
    return [spec.name for spec in specs]


CORE: dict[str, list[str]] = {
    "main": [
        *("python", "sage", "shell", "read_file", "write_file", "edit_file", "list_files"),
        *("glob", "grep", "publish", "find_artifacts", "restart_kernel"),
        *("pdf_info", "pdf_pages", "pdf_search", "pdf_render", "admit_hosts"),
        *("document_list", "document_read", "show_outline"),
        *("plan_read", "plan_create", "plan_revise", "plan_activate", "plan_progress"),
        "plan_finish",
        *("ledger_list", "ledger_read", "ledger_change", "ledger_proposals"),
        *("memory_list", "memory_propose"),
        *("delegate", "wait_delegations", "delegation_status", "cancel_delegation"),
        "accept_delegation",
        *("message_child", "agent_message_status", "wait_agent_message"),
        "withdraw_child_request",
        *("skills", "history_search", "history_read", "view_image", "ask_user", LOAD_TOOLS),
    ],
    "task": [
        *("python", "sage", "shell", "read_file", "write_file", "edit_file", "list_files"),
        *("glob", "grep", "publish", "find_artifacts", "restart_kernel"),
        *("pdf_info", "pdf_pages", "pdf_search", "pdf_render", "admit_hosts"),
        *("document_list", "document_read", "show_outline"),
        *("ledger_list", "ledger_read", "ledger_change"),
        *("memory_list", "memory_propose", "track_read", "track_progress"),
        *("message_parent", "agent_message_status", "wait_agent_message"),
        *("skills", "history_search", "history_read", "view_image", "submit_result"),
        LOAD_TOOLS,
    ],
    "check": [
        *("python", "sage", "shell", "read_file", "write_file", "edit_file", "list_files"),
        *("glob", "grep", "publish", "find_artifacts", "restart_kernel"),
        *("pdf_info", "pdf_pages", "pdf_search", "pdf_render", "admit_hosts"),
        *("document_list", "document_read", "show_outline"),
        *("ledger_list", "ledger_read", "memory_list", "track_read", "track_progress"),
        *("message_parent", "agent_message_status", "wait_agent_message"),
        *("skills", "history_search", "history_read", "view_image", "submit_result"),
        LOAD_TOOLS,
    ],
}
"""Each role's core tools, in the order its model reads them."""


def test_the_groups_hold_harness_tools_each_in_one_group() -> None:
    grouped = [tool for group in GROUPS for tool in group.tools]
    assert len(grouped) == len(set(grouped)) == len(GROUP_OF)
    assert len({group.name for group in GROUPS}) == len(GROUPS)
    assert set(grouped) <= HARNESS_TOOL_NAMES
    assert {LOAD_TOOLS, MCP_TOOLS} <= HARNESS_TOOL_NAMES
    # A delegated run's result has one way back, which is never deferred (PD-18).
    assert SUBMIT not in GROUP_OF
    for group in GROUPS:
        assert re.fullmatch(r"[a-z_]+", group.name)
        assert "\n" not in group.description
        assert len(group.description) <= 160


def test_each_notice_source_that_loads_a_group_is_a_hooks() -> None:
    """The one mapping's keys are the sources the notices that need a group come from."""
    sources = {
        WorkRequests.name: "documents",
        MathChecks.name: "documents",
        Reviews.name: "reviews",
        JobNotices.name: "jobs",
        RESOURCES: "resources",
    }
    assert sources == NOTICE_GROUPS
    assert Resources.name == RESOURCES


@pytest.mark.parametrize("role", ["main", "task", "check"])
def test_a_role_is_offered_its_core_tools_and_loads_the_rest(role: str) -> None:
    every, box = full(role), toolbox(role)
    assert names(box.specs) == CORE[role]
    assert not set(names(box.specs)) & set(GROUP_OF)
    grouped = {name: names(e.spec for e in group) for name, group in box.groups.items()}
    # Nothing is lost: every tool of the role is core or in one of its groups, which
    # keeps their order.
    rest = [name for name in names(every.specs) if name not in CORE[role]]
    assert sorted(rest) == sorted(tool for group in grouped.values() for tool in group)
    for tools in grouped.values():
        assert tools == [name for name in rest if name in tools]
    assert list(grouped) == [group.name for group in GROUPS if group.name in grouped]
    if role == "main":
        assert list(grouped) == [group.name for group in GROUPS]
    else:
        assert "reviews" not in grouped  # a child responds to no review (DA-1)
    if role == "check":
        # A check reads, and changes no document or entry (PD-34).
        assert grouped["documents"] == ["document_comments", "document_comment_read"]
        assert grouped["ledger_admin"] == ["ledger_work", "ledger_history", "ledger_frontier"]
    load = next(spec for spec in box.specs if spec.name == LOAD_TOOLS)
    assert load.parameters["properties"]["group"]["enum"] == list(grouped)
    for name, tools in grouped.items():
        assert f"\n- {name}: " in load.description
        assert f"({', '.join(tools)})" in load.description
    assert len(schema_json(box.specs)) < 0.8 * len(schema_json(every.specs))
    assert box.groups_summary() == {"deferred": grouped, "loaded": []}
    assert box.group_pins() == ()


def test_a_direct_callers_toolbox_offers_every_tool_from_the_start() -> None:
    box = Toolbox(cast(WorkerClient, object()), CONTEXT, jobs=domain(JOB_SPECS), features=FEATURES)
    assert box.groups == {}
    assert LOAD_TOOLS not in names(box.specs)
    assert {spec.name for spec in JOB_SPECS} <= set(names(box.specs))
    assert box.groups_summary() is None


async def test_load_tools_offers_a_group_for_the_rest_of_the_run() -> None:
    box = toolbox("main")
    before, load = box.specs, next(e for e in box.entries.values() if e.spec.name == LOAD_TOOLS)
    outcome = await box.call(ToolCall("c1", LOAD_TOOLS, '{"group": "jobs"}'), "op-1")
    jobs = [spec.name for spec in JOB_SPECS]
    assert json.loads(outcome.text) == {
        "loaded": "jobs",
        "tools": jobs,
        "note": "your tools include these from your next turn: call them by name",
    }
    assert outcome.record[LOAD_TOOLS] == {"group": "jobs", "loaded": True}
    # The loaded tools come after the run's others; nothing the model had changes.
    assert box.specs[: len(before)] == before
    assert names(box.specs[len(before) :]) == jobs
    assert box.specs[len(before) :] == tuple(offered(spec) for spec in JOB_SPECS)
    assert box.entries[LOAD_TOOLS] is load
    assert box.revision == 1
    again = await box.call(ToolCall("c2", LOAD_TOOLS, '{"group": "jobs"}'), "op-2")
    assert json.loads(again.text)["already_loaded"] == "jobs"
    assert box.revision == 1
    wrong = await box.call(ToolCall("c3", LOAD_TOOLS, '{"group": "kernels"}'), "op-3")
    assert json.loads(wrong.text)["error"]["kind"] == "invalid_request"
    assert "group: one of documents, reviews, ledger_admin" in wrong.text
    assert box.groups_summary() == {
        "deferred": {name: names(e.spec for e in g) for name, g in box.groups.items()},
        "loaded": [{"group": "jobs", "by": "load_tools"}],
    }
    assert box.group_pins() == (
        GroupPin(group="jobs", tools=pin_tools([offered(spec) for spec in JOB_SPECS])),
    )


@pytest.mark.parametrize(("role", "tool", "group"), [
    ("main", "start_job", "jobs"),
    ("main", "review_respond", "reviews"),
    ("task", "document_reply", "documents"),
    ("check", "ledger_frontier", "ledger_admin"),
])  # fmt: skip
async def test_a_tool_of_a_group_called_by_name_loads_its_group_and_runs(
    role: str, tool: str, group: str
) -> None:
    """As a connector tool called before it is loaded is: a delegated run is not refused
    a tool of its own groups."""
    box = toolbox(role)
    outcome = await box.call(ToolCall("c1", tool, "{}"), "op-1")
    assert json.loads(outcome.text) == {"done": True}
    assert box.loaded == {group: {"by": "call", "tool": tool}}
    assert tool in names(box.specs)


async def test_a_delegated_run_is_still_refused_what_its_role_lacks() -> None:
    outcome = await toolbox("check").call(ToolCall("c1", "document_reply", "{}"), "op-1")
    assert json.loads(outcome.text)["error"]["kind"] == "policy_denied"
    assert "not_permitted_for_role" in outcome.text


class Script:
    """Answers as scripted, keeping the names of the tools each call was given."""

    def __init__(self, turns: Sequence[ModelTurn]) -> None:
        self.turns = list(turns)
        self.offered: list[list[str]] = []

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        self.offered.append(names(tools))
        return self.turns.pop(0)


class Notifying(TurnHook):
    """Gives one notice of ``source`` at ``point``, once."""

    def __init__(self, source: str, point: Point, payload: object = "x") -> None:
        self.name, self.point, self.payload = source, point, payload
        self.given = False

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point != self.point or self.given:
            return ()
        self.given = True
        return (Notice(self.name, ("n-1",), f"{self.name}_notice", self.payload),)


def working() -> ModelTurn:
    """A turn whose one call answers at once, for the tool point's notices to ride on."""
    return ModelTurn(None, (ToolCall("c1", "ledger_list", "{}"),))


@pytest.mark.parametrize("point", ["start", "turn", "tool"])
@pytest.mark.parametrize(("source", "group"), sorted(NOTICE_GROUPS.items()))
async def test_a_notice_loads_the_group_it_needs(source: str, group: str, point: Point) -> None:
    """At a safe point or riding on a tool result, a notice that enters the conversation
    loads its group, and the next model call carries the group's tools."""
    box = toolbox("main", plans=False)
    tools = names(e.spec for e in box.groups[group])
    model = Script([working(), ModelTurn("Done.")])
    transcript = await run_task(model, box, "Go on", run_id="r", hooks=[Notifying(source, point)])
    assert transcript.final_text == "Done."
    first, second = model.offered
    if point == "tool":
        assert not set(tools) & set(first)
    else:
        assert set(tools) <= set(first)
    assert set(tools) <= set(second)
    assert box.loaded == {group: {"by": "notice", "source": source}}
    assert transcript.summaries["tool_groups"]["loaded"] == [
        {"group": group, "by": "notice", "source": source}
    ]
    # The conversation's size counts the schemas the run now carries.
    assert transcript.context.tools_chars == len(schema_json(box.specs))


async def test_a_notice_that_is_not_attached_loads_no_group() -> None:
    box = toolbox("main", plans=False)
    model = Script([working(), ModelTurn("Done.")])
    hook = Notifying("reviews", "tool", payload="x" * (box.output_chars * 2))
    await run_task(model, box, "Go on", run_id="r", hooks=[hook])
    assert box.loaded == {}
    assert not set(model.offered[1]) & set(GROUP_OF)


async def test_a_loaded_group_is_in_the_run_record_and_its_pins() -> None:
    box = toolbox("main", plans=False)
    started = pin_tools(box.specs)
    model = Script(
        [
            ModelTurn(None, (ToolCall("c1", LOAD_TOOLS, '{"group": "provisioning"}'),)),
            ModelTurn(None, (ToolCall("c2", "list_jobs", "{}"),)),
            ModelTurn("Done."),
        ]
    )
    transcript = await run_task(model, box, "Install", run_id="r", hooks=())
    assert [set(later) - set(model.offered[0]) for later in model.offered[1:]] == [
        {"provision", "provision_status"},
        {"provision", "provision_status", *(spec.name for spec in JOB_SPECS)},
    ]
    assert transcript.summaries["tool_groups"]["loaded"] == [
        {"group": "provisioning", "by": "load_tools"},
        {"group": "jobs", "by": "call", "tool": "list_jobs"},
    ]
    pins = transcript.context.prompt_provenance
    assert pins is not None
    # The tools pin is what the run started with; the groups, what it loaded since.
    assert pins.tools == started
    assert [pin.group for pin in pins.groups] == ["provisioning", "jobs"]
    assert pins.groups == box.group_pins()
    assert pins.groups[1].tools == pin_tools([offered(spec) for spec in JOB_SPECS])
    assert pins.tools.names + tuple(n for p in pins.groups for n in p.tools.names) == tuple(
        names(box.specs)
    )
    assert type(pins).model_validate_json(pins.model_dump_json()) == pins


async def test_the_shell_note_on_a_computation_offers_start_job() -> None:
    """The note names start_job: the run is offered it with the note."""
    from .test_tool_mix import Worker, call

    jobs = caps.jobs(domain(JOB_SPECS))
    box = Toolbox(
        cast(WorkerClient, Worker()),
        CONTEXT,
        languages=("python",),
        features=("jobs",),
        capabilities=caps.main([jobs]),
    )
    assert "start_job" not in names(box.specs)
    outcome = await box.call(call(1, "shell", script="python3 -c 'print(1)'"), "op-1")
    assert "start_job runs a long computation" in json.loads(outcome.text)["note"]
    assert box.loaded == {"jobs": {"by": "note"}}
    assert "start_job" in names(box.specs)


def _named(text: str, tools: set[str]) -> set[str]:
    return {tool for tool in tools if re.search(rf"(?<![\w]){re.escape(tool)}(?![\w])", text)}


def check_in() -> Notice:
    """A child's check-in, as the resources hook gives it its parent."""
    request = ResourceRequest.model_construct(
        request_id="rr-1",
        work="w",
        current="c",
        epoch=1,
        parent=None,
        resource="turns",
        grant_revision=1,
        used=1,
        total=2,
        checkpoint_message_id=None,
        created_at=0.0,
    )
    return Resources._request_notice(request)


NOTICED: dict[str, tuple[str, str, str | None]] = {
    "document_reply": (WorkRequests.name, "document work request", None),
    "review_respond": (Reviews.name, "consistency finding", REVIEW_FINDINGS.text),
    "resource_decide": (RESOURCES, "checks in and waits", check_in().framing),
}
"""The deferred tools a prompt names: the source of the notice that asks for each, the
words of the prompt's sentence that name that notice, and the notice's own text where it
names the tool too."""


WHERE_OFFERED: dict[str, str] = {"provision": "when you have it"}
"""The deferred tools a prompt names only for a run that has them, with the words that
say so: ``load_tools`` lists their group to every run that has it."""


def test_every_tool_a_prompt_names_is_core_or_loaded_by_the_notice_that_mentions_it() -> None:
    every = set().union(*(names(full(role).specs) for role in ("main", "task", "check")))
    brief = [processes_text(False), processes_text(True)]
    brief += [write_area_text("work/d", own_environment=own) for own in (False, True)]
    texts = {
        "main": SYSTEM_PROMPT,
        "task": TASK_PROMPT,
        "check": CHECK_PROMPT,
        "planning": GUIDANCE,
        "memory": RUN_FRAMING,
        "brief": " ".join(brief),
    }
    named = {label: _named(text, every) for label, text in texts.items()}
    deferred = {
        label: {tool for tool in found if tool in GROUP_OF} for label, found in named.items()
    }
    assert deferred == {
        "main": {"document_reply", "review_respond", "resource_decide"},
        "task": set(),
        "check": set(),
        "planning": set(),
        "memory": set(),
        "brief": {"provision"},
    }
    # What a prompt tells the model to use unprompted is core, offered from the start.
    started = {role: set(names(toolbox(role).specs)) for role in ("main", "task", "check")}
    unprompted = {"ledger_proposals", "memory_propose", "admit_hosts", "ask_user", "plan_read"}
    assert unprompted <= named["main"] | named["planning"] | named["memory"]
    assert unprompted <= started["main"]
    assert {SUBMIT, "message_parent"} <= named["task"] & named["check"]
    assert {SUBMIT, "message_parent"} <= started["task"] & started["check"]
    for role in ("main", "task", "check"):
        # Every core tool a role's own prompt names that the role has, it has from the start.
        offer = set(names(full(role).specs))
        assert {tool for tool in named[role] - set(GROUP_OF) if tool in offer} <= started[role]
    assert check_in().source == RESOURCES
    for tool in deferred["main"]:
        source, words, notice = NOTICED[tool]
        assert NOTICE_GROUPS[source] == GROUP_OF[tool]
        sentence = next(s for s in SYSTEM_PROMPT.split(". ") if tool in s)
        assert words in sentence
        if notice is not None:
            assert tool in notice
    for tool in deferred["brief"]:
        assert WHERE_OFFERED[tool] in next(s for s in brief[0].split(". ") if tool in s)
        load = next(spec for spec in toolbox("task").specs if spec.name == LOAD_TOOLS)
        assert f"- {GROUP_OF[tool]}: " in load.description
        assert tool in load.description


def test_no_core_tool_names_a_tool_the_run_may_not_be_offered_yet() -> None:
    for role in ("main", "task", "check"):
        box = toolbox(role)
        grouped = {e.spec.name for group in box.groups.values() for e in group}
        for spec in box.specs:
            if spec.name == LOAD_TOOLS:
                continue
            assert not _named(spec.description + json.dumps(spec.parameters), grouped), spec.name


def offered_names(tools: Sequence[Json]) -> list[str]:
    """The names of the tools a request to the gateway carried, in either API's form."""
    return [str(tool.get("name") or tool["function"]["name"]) for tool in tools]


@pytest.mark.slow
async def test_the_notices_of_a_real_run_load_their_groups(harness: Harness) -> None:
    """Through the CLI, with the hooks the harness builds: a work request, a job's end
    and an earlier review's counts waiting for the run load the document, job and review
    tools before its first model call; the record and pins keep what it loaded."""
    await six_sources(harness)
    earlier = len(harness.gateway.tools["main"])
    record = await harness.run("run-1", [Reply("Answer A."), Reply("Answer B.")])
    first = harness.gateway.tools["main"][earlier]
    shown = offered_names(first)
    assert {"document_reply", "start_job", "review_respond"} <= set(shown)
    assert not {"provision", "resource_decide", "ledger_frontier"} & set(shown)
    groups = record.summaries["tool_groups"]
    assert isinstance(groups, dict)
    assert groups["loaded"] == [
        {"group": "documents", "by": "notice", "source": "requests"},
        {"group": "jobs", "by": "notice", "source": "jobs"},
        {"group": "reviews", "by": "notice", "source": "reviews"},
    ]
    pins = record.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert [pin.group for pin in pins.groups] == ["documents", "jobs", "reviews"]
    assert LOAD_TOOLS in pins.tools.names
    assert not set(pins.tools.names) & set(GROUP_OF)


@pytest.mark.slow
async def test_a_confined_run_loads_groups_through_its_host(
    harness: Harness, tmp_path: Path
) -> None:
    """The host's toolbox loads a group as it loads connector tools: ``load_tools`` in a
    call's reply, and a notice the host hands out in the reply to its append, so the
    runtime's next model call carries the group's tools."""
    harness.work_request()
    script = [Reply(calls=[call(LOAD_TOOLS, group="jobs")]), Reply("Done.")]
    async with serving(harness, *script, serve=False) as service:
        gateway_models = ModelGateway(harness.gateway.url, API_KEY)

        async def host_handle(kind: Capability, value: JsonValue) -> JsonValue:
            return await host.capability(kind, value)

        async def vm_handle(kind: Capability, value: JsonValue) -> JsonValue:
            if kind is Capability.RUN:
                return await runtime.run(value)
            if kind is Capability.CANCEL:
                return await runtime.cancel(value)
            if kind is Capability.MODEL:
                return await vm_models.complete(value)
            if kind is Capability.MODEL_INFO:
                return await vm_models.info(value)
            raise TransportError("denied", "fixture refused capability")

        host_channel = Channel(lambda text: vm_channel.receive(text), host_handle)
        vm_channel = Channel(lambda text: host_channel.receive(text), vm_handle)
        vm_models = RuntimeModels(gateway_models, frozenset({"main"}), vm_channel)
        host = HostLoops(host_channel, frozenset({PROJECT}))
        runtime = RuntimeLoops(vm_channel, gateway_models, tmp_path / "scratch")
        runtime.scratch.mkdir()
        service.harness.main = Role(CallbackModel(host_channel, "main", role="main"), "main")
        service.harness.gateway = CallbackGateway(host_channel)

        async def runner(
            model: ChatModel, toolbox: Toolbox, task: str | None, **options: Any
        ) -> Transcript:
            return await host.run(model, toolbox, task, **options)

        service.harness.loop_runner = runner
        service.harness.prepare_attempt = lambda admitted: host.model_scope()
        try:
            service.runs.post(PROJECT, SESSION, "msg-runtime", "Go on", author="user")
            assert await service.start_selected(PROJECT, SESSION)
            task = service.active[PROJECT, SESSION].task
            assert task is not None
            await task
            (record,) = records(harness)
        finally:
            await runtime.close()
            await host_channel.close()
            await vm_channel.close()
            await gateway_models.close()
    assert (record.stopped, record.failure, record.final_text) == ("answered", None, "Done.")
    first, second = (offered_names(tools) for tools in harness.gateway.tools["main"])
    # The work request's notice loaded the document tools before the first call.
    assert "document_reply" in first
    assert "start_job" not in first
    assert set(second) - set(first) == {spec.name for spec in JOB_SPECS}
    groups = record.summaries["tool_groups"]
    assert isinstance(groups, dict)
    assert groups["loaded"] == [
        {"group": "documents", "by": "notice", "source": "requests"},
        {"group": "jobs", "by": "load_tools"},
    ]
    pins = record.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert [pin.group for pin in pins.groups] == ["documents", "jobs"]
