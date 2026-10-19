# SPDX-License-Identifier: MIT
"""What a run did, for the reviewer to match its narration against (docs/specs/
consistency-review.md, CR-2 as the coordinator amended it for lab 40): the toolbox names
each call's effect class, the review controller records each call that changes something,
and a review's snapshot holds one ``actions`` item for its window, listing them by run with
their outcomes and what they made, and the blocks each document revision changed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.consistency.actions import action_of
from sandbox_science.orchestrator.consistency.controller import ReviewController
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .ledger_support import AnchoredLedger as Ledger
from .test_consistency import MAIN, context_for, dispatch
from .test_consistency import store as store


def called(number: int, tool: str, arguments: dict[str, Any], **record: Any) -> tuple[
    list[dict[str, Any]], dict[str, Any]
]:  # fmt: skip
    """The transcript's call of ``tool`` and its result, and the call as the run executed
    it, with its record (``tool_records``)."""
    call_id = f"call-{number}"
    encoded = json.dumps(arguments)
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function",
         "function": {"name": tool, "arguments": encoded}}]},
        {"role": "tool", "tool_call_id": call_id, "content": "done"},
    ]  # fmt: skip
    return messages, {"tool": tool, "arguments": encoded, **record}


def test_an_action_says_what_the_call_did_and_how_it_ended() -> None:
    """Only a call whose effect class is effect is an action; a gate's or a store's
    refusal is refused, a failure an error, and what the call made comes from its record."""
    ledger = {"ledger": "ledger_change", "effect": "effect",
              "result": {"entries": [{"handle": "C-1", "revision": 2}],
                         "work": ["work-" + "1" * 24]}}  # fmt: skip
    messages, call = called(1, "ledger_change", {"reason": "r"}, **ledger)
    assert action_of(messages, call) == {
        "tool": "ledger_change", "call": "call-1", "target": {}, "outcome": "ok",
        "made": ["ledger C-1 r2", "work record work-" + "1" * 24],
    }  # fmt: skip
    for record, outcome in (
        ({"error": "policy_denied"}, "refused"),
        ({"error": "internal"}, "error"),
        ({"document": {"outcome": "conflict", "document_id": "doc-1", "revision": 3}},
         "refused"),
    ):  # fmt: skip
        messages, call = called(2, "document_patch", {"document": "notes"}, effect="effect",
                                **record)  # fmt: skip
        found = action_of(messages, call)
        assert found is not None
        assert (found["outcome"], found["made"], found["target"]) == (
            outcome, [], {"document": "notes"}
        )  # fmt: skip
    messages, call = called(3, "ledger_read", {"entry": "C-1"}, ledger="ledger_read", effect="read")
    assert action_of(messages, call) is None
    assert action_of(messages, None) is None


@pytest.mark.asyncio
async def test_the_toolbox_names_each_calls_effect_class(store: Any) -> None:
    from sandbox_science.orchestrator.ledger.tool import LedgerTool

    from .test_ledger_tools import CONTEXT

    reviews, _, _ = store
    tool = LedgerTool(Ledger(reviews.documents), CONTEXT, run_id="r1", role="main")
    toolbox = Toolbox(cast(WorkerClient, object()), CONTEXT, ledger=tool)
    listed = await toolbox.call(ToolCall("c1", "ledger_list", "{}"), "op-1")
    task = {"type": "task", "title": "Run it", "statement": "Run it."}
    changed = await toolbox.call(
        ToolCall("c2", "ledger_change", json.dumps({"reason": "r", "changes": [task]})), "op-2"
    )
    assert (listed.record["effect"], changed.record["effect"]) == ("read", "effect")


@pytest.mark.asyncio
async def test_a_reviews_snapshot_lists_the_windows_actions_and_changed_blocks(
    store: Any,
) -> None:
    """Lab 40's agent narrated edits it never made. The snapshot holds one actions item
    for the window: each run's calls that changed something, in order, an ok call with
    what it made and a refused one with nothing, and the blocks each document revision
    changed; the calls are not items one by one, and the reads are not listed."""
    reviews, documents, _ = store
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    transcript: list[dict[str, Any]] = []
    made = {"ledger": "ledger_change", "effect": "effect",
            "result": {"entries": [{"handle": "C-1", "revision": 1}]}}  # fmt: skip
    for number, (tool, record) in enumerate(
        [("ledger_change", made), ("ledger_read", {"ledger": "ledger_read", "effect": "read"}),
         ("python", {"effect": "effect", "error": "policy_denied"})], 1
    ):  # fmt: skip
        exchange, call = called(number, tool, {"entry": "C-1"}, **record)
        transcript += exchange
        await controller.safe_point(transcript, tool=record, call=call)
    notes = documents.create(
        "p", name="notes", title="Notes", text="First.\n\nThe order is 17.\n", author=MAIN,
        session_id="s",
    )  # fmt: skip
    second = documents.mapped("p", notes.document_id, 1).block_ids[1]
    documents.patch(
        "p", notes.document_id, base=1, reason="Fix the order.", author=MAIN, operation_id="fix",
        operations=[{"op": "replace", "block": second, "markdown": "The order is 19."}],
    )  # fmt: skip
    reviews.ingest_documents("p", "s", drain=True)
    review = dispatch(reviews)
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    assert "action" not in {i["kind"] for i in items}
    (listed,) = [i for i in items if i["kind"] == "actions"]
    found = json.loads(reviews.read_evidence("p", "s", review, listed["evidence"])["text"])
    assert [(a["tool"], a["outcome"], a["made"]) for a in found["actions"]] == [
        ("ledger_change", "ok", ["ledger C-1 r1"]),
        ("python", "refused", []),
    ]
    assert {a["run"] for a in found["actions"]} == {"author-1"}
    (second_revision,) = [d for d in found["documents"] if d["revision"] == 2]
    assert (second_revision["changed"], second_revision["since"]) == ([second], 1)


@pytest.mark.asyncio
async def test_the_earlier_conversation_is_not_this_windows_narration(store: Any) -> None:
    """Lab 41: each run's prompt begins with the session's earlier conversation, whose
    answers say what earlier runs did. Observed again, they came into each later window
    as fresh narration, with no record in its actions, and the reviewer raised
    missing_action findings on edits that had been made. Only the run's own messages are
    observed."""
    reviews, _, _ = store
    earlier = [
        {"role": "user", "content": "Fix the order in the notes."},
        {"role": "assistant", "content": "Updated the notes: the order is 19."},
    ]
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None, earlier=2
    )
    transcript: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a research assistant."},
        *earlier,
        {"role": "user", "content": "What is the order now?"},
        {"role": "assistant", "content": "It is 19, as the notes say."},
    ]
    await controller.terminal(transcript, "It is 19, as the notes say.")
    review = dispatch(reviews)
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    # Each transcript item holds the message as the run sent it, as JSON.
    said = [
        json.loads(
            json.loads(reviews.read_evidence("p", "s", review, i["evidence"])["text"])["content"]
        )["content"]
        for i in items
        if i["kind"] == "transcript"
    ]
    assert said == ["What is the order now?", "It is 19, as the notes say."]
    assert {i["locator"]["message"] for i in items if i["kind"] == "transcript"} == {3, 4}
    with pytest.raises(ValueError, match="earlier counts messages"):
        ReviewController(reviews, context_for(reviews), run_id="a", model=None, alias=None,
                         earlier=-1)  # fmt: skip


def test_a_review_without_actions_or_documents_has_no_actions_item(store: Any) -> None:
    from .test_consistency import claim

    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    kinds = {i["kind"] for i in cast(ReviewStore, reviews).manifest("p", "s", review)["evidence"]}
    assert "actions" not in kinds


def published(documents: Any, tmp_path: Path, path: str, data: bytes) -> str:
    """An artifact revision of project p at ``path``: its ID."""
    import hashlib
    from datetime import UTC, datetime

    from sandbox_science.contracts import ArtifactRef
    from sandbox_science.orchestrator.artifacts import ArtifactInventory

    from .test_ledger_evidence import source

    publisher = source(tmp_path)
    worker = ArtifactRef(
        artifact_id="worker-1", project_id="p", source_path=path,
        media_type="application/json", size=len(data),
        sha256=hashlib.sha256(data).hexdigest(), created_at=datetime.now(UTC),
        produced_by="unproven-hint",
    )  # fmt: skip
    inventory = ArtifactInventory(documents)
    inventory.begin(publisher, "publish-1", path, "application/json")
    inventory.pin(publisher, "publish-1", path, "application/json", worker)
    receipt = inventory.finish(publisher, "publish-1", path, "application/json", worker, data)
    assert receipt.revision is not None
    return receipt.revision.artifact_id


def test_a_confirmation_holds_the_fixs_actions_and_the_files_it_names(
    store: Any, tmp_path: Path
) -> None:
    """Lab 41: run 7's "fix" said results/ell.json "concerns a separate subgroup-order
    task", and a confirmation review confirmed it without the file or what the run did.
    A confirmation now holds the actions since the finding's review, as an author
    window's actions item, and the artifacts the fix names by path."""
    from sandbox_science.orchestrator.artifacts import ArtifactInventory
    from sandbox_science.orchestrator.consistency.findings import Findings

    from .test_consistency import anchor, claim, raise_finding

    _, documents, now = store
    reviews = ReviewStore(documents, artifacts=ArtifactInventory(documents), clock=lambda: now[0])
    artifact = published(documents, tmp_path, "results/ell.json", b'{"ell": 13}\n')
    claim(reviews)
    initial = dispatch(reviews)
    finding = raise_finding(reviews, initial)
    reviews.finish("p", "s", initial, "owner", "findings", "One finding.")
    reviews.observe(
        "p", "s", "turn:action:9", "action", {"run": "author-7", "message": 9},
        {"tool": "document_patch", "call": "call-9", "target": {"document": "notes"},
         "outcome": "ok", "made": ["document doc-1 r3"]},
    )  # fmt: skip
    Findings(reviews).respond(
        "p", "s",
        {"finding": finding["finding_id"], "expected": 1, "action": "fix",
         "explanation": "Fixed: `results/ell.json` concerns a separate subgroup-order task.",
         "evidence": [anchor(reviews, initial)], "operation_id": "fix"},
        MAIN,
    )  # fmt: skip
    confirmation = dispatch(reviews, "confirmation")
    items = reviews.manifest("p", "s", confirmation, limit=50)["evidence"]
    (cited,) = [i for i in items if i["kind"] == "artifact"]
    assert cited["locator"]["cited_by"] == {"finding": finding["finding_id"]}
    assert cited["locator"]["artifact"]["artifact_id"] == artifact
    text = reviews.read_evidence("p", "s", confirmation, cited["evidence"])["text"]
    assert "13" in text
    (listed,) = [i for i in items if i["kind"] == "actions"]
    found = json.loads(reviews.read_evidence("p", "s", confirmation, listed["evidence"])["text"])
    assert [(a["tool"], a["made"]) for a in found["actions"]] == [
        ("document_patch", ["document doc-1 r3"])
    ]


@pytest.mark.asyncio
async def test_a_runs_review_sees_its_delegated_runs_work(store: Any, tmp_path: Path) -> None:
    """Lab 41's B5: the main run's answer gave orders its delegated tasks computed, and
    its review held only their returned summaries, so the correct results read as
    unverified. Once a delegation ends, the run's review window holds the delegated run's
    actions (naming the delegation), its outcome and its result files; a grandchild's
    too; and the delegated runs of another run, none of them."""
    import hashlib
    from datetime import UTC, datetime, timedelta

    from sandbox_science.orchestrator.delegation.records import (
        Delegation,
        DelegationRecords,
        Ending,
    )
    from sandbox_science.orchestrator.projects import ProjectStore, RunRecord, run_name

    _, documents, now = store
    from sandbox_science.orchestrator.artifacts import ArtifactInventory

    reviews = ReviewStore(documents, artifacts=ArtifactInventory(documents), clock=lambda: now[0])
    artifact = published(documents, tmp_path, "results/ell.json", b'{"ell": 13}\n')
    context = context_for(reviews)
    start = datetime(2026, 10, 15, 12, tzinfo=UTC)
    runs = ["author-1", "child-1", "grandchild-1", "other-1"]
    names = {run: run_name(start + timedelta(seconds=n), run) for n, run in enumerate(runs)}
    parents = {"child-1": "author-1", "grandchild-1": "child-1", "other-1": "elsewhere-1"}
    publish = {
        "tool": "publish",
        "arguments": json.dumps({"path": "results/ell.json"}),
        "effect": "effect",
        "artifact": {"artifact_id": "worker-1"},
        "artifact_inventory": {"revision": {"artifact_id": artifact, "revision": 1}},
    }
    read: dict[str, Any] = {"tool": "job_status", "arguments": "{}", "effect": "read"}
    projects = ProjectStore(documents.path.parent)
    records = DelegationRecords(documents)
    try:
        projects.save_run(
            RunRecord(run_id="author-1", context=context, task="t", started_at=start,
                      ended_at=start, stopped="answered", messages=[], tools=[])
        )  # fmt: skip
        for n, run in enumerate(["child-1", "grandchild-1", "other-1"], 1):
            delegation = f"dlg-{n:024x}"
            parent = names[parents[run]] if parents[run] != "elsewhere-1" else (
                run_name(start, "elsewhere-1"))  # fmt: skip
            records.start("p", "s", Delegation.model_validate(
                {"delegation_id": delegation, "parent": parent, "run": names[run],
                 "kind": "check", "brief": {"task": "Compute ell."},
                 "started_at": (start + timedelta(seconds=n)).isoformat()}))  # fmt: skip
            files: dict[str, Any] = {
                "results": [{"path": "results/ell.json", "artifact_id": artifact, "revision": 1,
                             "sha256": hashlib.sha256(b'{"ell": 13}\n').hexdigest(),
                             "content_retained": True}],
                "total": 1,
            }  # fmt: skip
            records.end("p", "s", delegation,
                        Ending("returned", "answered", f"ell = 13 ({run})",
                               result={"status": "complete", "summary": "ell = 13"},
                               files=files if run == "child-1" else None))  # fmt: skip
            if parents[run] in ("author-1", "child-1"):
                projects.save_run(
                    RunRecord(run_id=run, role="delegated", parent=names[parents[run]],
                              delegation=delegation, context=context, task="t",
                              started_at=start + timedelta(seconds=n),
                              ended_at=start + timedelta(seconds=n), stopped="answered",
                              messages=[], tools=[publish, read])
                )  # fmt: skip
    finally:
        projects.close()
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    await controller.safe_point([])
    await controller.safe_point([])  # once each
    review = dispatch(reviews)
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    outcomes = sorted(i["locator"]["delegation"] for i in items if i["kind"] == "delegation")
    assert outcomes == ["dlg-" + f"{1:024x}", "dlg-" + f"{2:024x}"]
    (result,) = [i for i in items if i["kind"] == "artifact" and "delegated" in i["locator"]]
    assert result["locator"]["delegated"]["delegation"] == "dlg-" + f"{1:024x}"
    assert "13" in reviews.read_evidence("p", "s", review, result["evidence"])["text"]
    (listed,) = [i for i in items if i["kind"] == "actions"]
    found = json.loads(reviews.read_evidence("p", "s", review, listed["evidence"])["text"])
    assert [(a["run"], a["tool"], a["made"]) for a in found["actions"]] == [
        ("child-1", "publish", [f"artifact {artifact} r1"]),
        ("grandchild-1", "publish", [f"artifact {artifact} r1"]),
    ]
    assert {a["delegation"] for a in found["actions"]} == {
        "dlg-" + f"{1:024x}", "dlg-" + f"{2:024x}"
    }  # fmt: skip


@pytest.mark.asyncio
async def test_a_waited_jobs_end_reaches_the_review(store: Any) -> None:
    """Lab 41's B7: a job's execution item stayed queued, since wait_jobs is a read and
    its answer only summarised the end. A wait's record keeps the executions it saw end,
    and each becomes an execution item in its ended state."""
    reviews, _, _ = store
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    started = {"execution_id": "exe-1", "kind": "job", "state": "queued",
               "submitted_at": "2026-10-15T12:00:00Z"}  # fmt: skip
    ended = {**started, "state": "succeeded", "started_at": "2026-10-15T12:00:01Z",
             "ended_at": "2026-10-15T12:05:00Z"}  # fmt: skip
    transcript: list[dict[str, Any]] = []
    for number, (tool, record) in enumerate(
        [("start_job", {"effect": "effect", "execution": started}),
         ("wait_jobs", {"effect": "read", "wait": {"reason": "ended"}, "executions": [ended]})],
        1,
    ):  # fmt: skip
        exchange, call = called(number, tool, {"job_ids": ["exe-1"]}, **record)
        transcript += exchange
        await controller.safe_point(transcript, tool=record, call=call)
    review = dispatch(reviews)
    states = [
        json.loads(reviews.read_evidence("p", "s", review, i["evidence"])["text"])["state"]
        for i in reviews.manifest("p", "s", review, limit=50)["evidence"]
        if i["kind"] == "execution"
    ]
    assert states == ["queued", "succeeded"]


def test_a_memory_proposal_and_a_cancel_are_actions_and_a_read_is_not() -> None:
    """Lab 42's B2: only calls of class effect were actions. A memory proposal and a
    cancel change something too; a skill's load, a read, does not."""
    result = {"proposal_id": "mp-1", "state": "pending", "replayed": False}
    proposed = {"memory": "memory_propose", "effect": "proposal", "result": result}
    messages, call = called(1, "memory_propose", {"reason": "r"}, **proposed)
    found = action_of(messages, call)
    assert found is not None
    assert (found["outcome"], found["made"]) == ("ok", ["memory proposal mp-1 (pending)"])
    stopped = {"effect": "cancel", "execution": {"execution_id": "exe-1", "state": "cancelled"}}
    messages, call = called(2, "cancel_job", {"job_id": "exe-1"}, **stopped)
    found = action_of(messages, call)
    assert found is not None
    assert (found["target"], found["made"]) == (
        {"job_id": "exe-1"}, ["execution exe-1 (cancelled)"]
    )  # fmt: skip
    messages, call = called(3, "skills", {"load": "verify-claim"}, effect="read")
    assert action_of(messages, call) is None


@pytest.mark.asyncio
async def test_a_plan_change_is_an_action_and_the_review_holds_the_plan(store: Any) -> None:
    """Lab 42's B2: run 3 recorded its steps, finished its plan and said so. Its review
    listed no plan call among its actions, read the plan as an earlier plan_read showed
    it, and raised fail findings. The plan tools' calls are actions, with the step and
    status, or the plan's status, each recorded, and the snapshot holds the session plan
    as recorded at dispatch."""
    from sandbox_science.orchestrator.plan_tools import PlanTools
    from sandbox_science.orchestrator.projects import ProjectStore
    from sandbox_science.orchestrator.session_plans import SessionPlans

    from .test_plan_tools import body

    reviews, documents, _ = store
    context = context_for(reviews)
    controller = ReviewController(reviews, context, run_id="author-1", model=None, alias=None)
    transcript: list[dict[str, Any]] = [{"role": "user", "content": "Check the claim."}]
    projects = ProjectStore(documents.path.parent)
    plans = PlanTools(SessionPlans(documents), projects, context, run_id="author-1", alias="main")
    toolbox = Toolbox(cast(WorkerClient, object()), context, plans=plans)

    async def call(number: int, tool: str, **arguments: Any) -> Any:
        request = ToolCall(f"call-{number}", tool, json.dumps(arguments))
        outcome = await toolbox.call(request, f"op-{number}", model="scripted")
        exchange, executed = called(number, tool, arguments, **outcome.record)
        transcript.extend(exchange)
        await controller.safe_point(transcript, tool=outcome.record, call=executed)
        return json.loads(outcome.text)

    try:
        created = await call(1, "plan_create", operation_id="create", body=body(count=2),
                             reason="Plan the check")  # fmt: skip
        ref = created["receipt"]["ref"]
        await call(2, "plan_activate", operation_id="activate", base=ref, reason="Begin",
                   expected_status_revision=1, expected_progress_cursor=0)  # fmt: skip
        progress = {"base": ref, "expected_step_cursor": 0, "status": "completed", "note": "Done"}
        await call(3, "plan_progress", operation_id="s0", step_id="step-0",
                   expected_status_revision=2, **progress)  # fmt: skip
        await call(4, "plan_progress", operation_id="stale", step_id="step-1",
                   expected_status_revision=1, **progress)  # fmt: skip
        await call(5, "plan_progress", operation_id="s1", step_id="step-1",
                   expected_status_revision=2, **progress)  # fmt: skip
        await call(6, "plan_finish", operation_id="finish", base=ref, expected_status_revision=2,
                   expected_progress_cursor=2, reason="Finished",
                   outcome={"status": "achieved", "summary": "Checked", "evidence": [],
                            "remaining_work": []})  # fmt: skip
    finally:
        projects.close()
    transcript.append(
        {"role": "assistant", "content": "Both steps are done; the plan is finished."}
    )
    await controller.safe_point(transcript)
    review = dispatch(reviews)
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    (listed,) = [i for i in items if i["kind"] == "actions"]
    found = json.loads(reviews.read_evidence("p", "s", review, listed["evidence"])["text"])
    plan = f"plan {ref['plan_id']} v1"
    assert [(a["tool"], a["outcome"], a["made"]) for a in found["actions"]] == [
        ("plan_create", "ok", [f"{plan} (draft)"]),
        ("plan_activate", "ok", [f"{plan}: active"]),
        ("plan_progress", "ok", [f"{plan}: step step-0 completed"]),
        ("plan_progress", "refused", []),
        ("plan_progress", "ok", [f"{plan}: step step-1 completed"]),
        ("plan_finish", "ok", [f"{plan}: completed, goal achieved"]),
    ]
    assert found["actions"][2]["target"] == {"step_id": "step-0"}
    (held,) = [i for i in items if i["kind"] == "plan"]
    state = json.loads(reviews.read_evidence("p", "s", review, held["evidence"])["text"])
    assert (state["plan"], state["status"], state["goal"]) == (
        ref["plan_id"], "completed", "achieved"
    )  # fmt: skip
    assert [(s["step"], s["status"], s["by"]) for s in state["steps"]] == [
        ("step-0", "completed", "author-1"),
        ("step-1", "completed", "author-1"),
    ]
    assert held["locator"]["version"] == 1


def test_a_session_without_a_plan_has_no_plan_item(store: Any) -> None:
    from .test_consistency import claim

    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    assert "plan" not in {i["kind"] for i in reviews.manifest("p", "s", review)["evidence"]}


@pytest.mark.asyncio
async def test_a_terminal_review_holds_the_runs_actions_in_its_earlier_windows(
    store: Any,
) -> None:
    """Lab 42's B5: run 1's terminal review held only its closing answer, whose
    document_create a checkpoint had reviewed, and it ended inconclusive. A terminal
    review's actions item lists, as context, the run's actions in the windows reviewed
    before, with its document revisions' changed blocks; an earlier run's are not its."""
    reviews, documents, _ = store
    reviews.observe(
        "p", "s", "turn-0:action:1", "action", {"run": "author-0", "message": 1},
        {"tool": "document_patch", "call": "call-1", "target": {"document": "old"},
         "outcome": "ok", "made": ["document doc-0 r2"]},
    )  # fmt: skip
    reviews.finish("p", "s", dispatch(reviews, "checkpoint"), "owner", "inconclusive", "Seen.")
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    notes = documents.create(
        "p", name="notes", title="Notes", text="The order is 19.\n", author=MAIN, session_id="s"
    )  # fmt: skip
    created = {"effect": "effect",
               "document": {"outcome": "created", "document_id": notes.document_id,
                            "revision": 1}}  # fmt: skip
    exchange, call = called(1, "document_create", {"name": "notes"}, **created)
    transcript = [{"role": "user", "content": "Write the notes."}, *exchange]
    await controller.safe_point(transcript, tool=created, call=call)
    # Delegated work brought for this run's review is its; work brought for another's
    # is not (the review of #449).
    for n, (task, reviewed) in enumerate([("task-0", "author-0"), ("task-1", "author-1")]):
        reviews.observe(
            "p", "s", f"delegated:dlg-{n}:action:0", "action",
            {"run": task, "delegation": f"dlg-{n}", "for_run": reviewed, "message": 0},
            {"tool": "sage", "call": "0", "target": {}, "outcome": "ok",
             "made": [f"execution exe-{n} (succeeded)"]},
        )  # fmt: skip
    checkpoint = dispatch(reviews, "checkpoint")
    reviews.finish("p", "s", checkpoint, "owner", "inconclusive", "Checked.")
    transcript.append({"role": "assistant", "content": "I created the notes: the order is 19."})
    await controller.safe_point(transcript)
    terminal = dispatch(reviews)
    items = reviews.manifest("p", "s", terminal, limit=50)["evidence"]
    # Recent transcript evidence from the preceding window is now carried as
    # context so the closing answer can be checked against earlier claims.
    non_actions = [i for i in items if i["kind"] != "actions"]
    assert non_actions
    assert {i["kind"] for i in non_actions} <= {"transcript", "document"}
    assert any(i["kind"] == "transcript" for i in non_actions)
    (listed,) = [i for i in items if i["kind"] == "actions"]
    found = json.loads(reviews.read_evidence("p", "s", terminal, listed["evidence"])["text"])
    assert (found["actions"], found["documents"]) == ([], [])
    earlier = found["earlier"]
    assert [(a["run"], a["tool"], a["made"]) for a in earlier["actions"]] == [
        ("author-1", "document_create", [f"document {notes.document_id} r1"]),
        ("task-1", "sage", ["execution exe-1 (succeeded)"]),
    ]
    assert [(d["document"], d["revision"]) for d in earlier["documents"]] == [
        (notes.document_id, 1)
    ]
    assert listed["locator"]["earlier"]["to"] == reviews.run("p", "s", checkpoint)["end_seq"]
    # A checkpoint lists its own window's actions only.
    items = reviews.manifest("p", "s", checkpoint, limit=50)["evidence"]
    (listed,) = [i for i in items if i["kind"] == "actions"]
    assert "earlier" not in listed["locator"]


DELEGATION = "dlg-" + "1" * 24


def delegated(
    documents: Any,
    tools: list[dict[str, Any]],
    summary: str,
    brief: str = "Factor the twist order.",
) -> None:
    """Run author-1 of session s, and its delegation ``DELEGATION``, a task run task-1
    asked ``brief``, whose tool records are ``tools``, ended with ``summary``."""
    from datetime import UTC, datetime, timedelta

    from sandbox_science.orchestrator.delegation.records import (
        Delegation,
        DelegationRecords,
        Ending,
    )
    from sandbox_science.orchestrator.projects import ProjectStore, RunRecord, run_name

    projects = ProjectStore(documents.path.parent)
    context = context_for(ReviewStore(documents))
    start = datetime(2026, 10, 16, 3, tzinfo=UTC)
    later = start + timedelta(seconds=1)
    parent, child = run_name(start, "author-1"), run_name(later, "task-1")
    records = DelegationRecords(documents)
    try:
        projects.save_run(
            RunRecord(run_id="author-1", context=context, task="t", started_at=start,
                      ended_at=start, stopped="answered", messages=[], tools=[])
        )  # fmt: skip
        records.start("p", "s", Delegation.model_validate(
            {"delegation_id": DELEGATION, "parent": parent, "run": child, "kind": "task",
             "brief": {"task": brief},
             "started_at": start.isoformat()}))  # fmt: skip
        records.end("p", "s", DELEGATION, Ending(
            "returned", "answered", summary,
            result={"status": "complete", "summary": summary}))  # fmt: skip
        projects.save_run(
            RunRecord(run_id="task-1", role="delegated", parent=parent, delegation=DELEGATION,
                      context=context, task="t", started_at=later, ended_at=later,
                      stopped="answered", messages=[], tools=tools)
        )  # fmt: skip
    finally:
        projects.close()


def cell(number: int, text: str, **record: Any) -> dict[str, Any]:
    """A delegated run's ``sage`` call whose cell printed ``text``."""
    execution = {"execution_id": f"exe-{number}", "attempt_id": f"att-{number}",
                 "kind": "cell", "state": "succeeded",
                 "request": {"code": f"step{number}()", "language": "sage"},
                 "result": {"stdout": {"text": text, "truncated": False},
                            "stderr": {"text": "", "truncated": False}, "exit_code": 0,
                            "outputs": [], "written_files": []}}  # fmt: skip
    return {"tool": "sage", "arguments": json.dumps({"code": f"step{number}()"}),
            "effect": "effect", "execution": execution, **record}  # fmt: skip


def evidence(reviews: ReviewStore, review: str) -> list[dict[str, Any]]:
    """Every item of ``review``, in order."""
    items: list[dict[str, Any]] = []
    after = 0
    while True:
        page = reviews.manifest("p", "s", review, after=after, limit=50)
        items += page["evidence"]
        if page["next_after"] is None:
            return items
        after = page["next_after"]


def read(reviews: ReviewStore, review: str, item: dict[str, Any]) -> Any:
    return json.loads(reviews.read_evidence("p", "s", review, item["evidence"])["text"])


@pytest.mark.asyncio
async def test_a_delegated_runs_executions_and_their_output_reach_the_review(
    store: Any,
) -> None:
    """Lab 42's B3: a task printed ...7031 from Sage and its summary said ...7033. The
    review held the summary and the task's actions, not its output, took the summary as
    checked and failed the document's correct value. Once the delegation ends, the run's
    review holds the delegated run's executions with their output, in each state its
    calls saw them, bounded as the run's own are, each naming its run and delegation;
    none of it makes a review due."""
    reviews, documents, _ = store
    factor = "1013176677300131846900870239606035638738100997248092069256697437031"
    queued = {"execution_id": "exe-2", "kind": "job", "state": "queued"}
    ended = {**queued, "state": "succeeded",
             "result": {"stdout": {"text": "is_prime: True\n", "truncated": False}}}  # fmt: skip
    summary = f"The largest factor ends in {factor[-4:-1]}3."
    delegated(documents, [
        cell(1, f"{factor}\n"),
        {"tool": "start_job", "arguments": "{}", "effect": "effect", "execution": queued},
        {"tool": "wait_jobs", "arguments": "{}", "effect": "read", "executions": [ended]},
        {"tool": "job_status", "arguments": "{}", "effect": "read", "execution": ended},
    ], summary)  # fmt: skip
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    await controller.safe_point([])
    assert not reviews.due("p", "s")
    assert not reviews.run_effects("p", "s", "author-1")
    review = dispatch(reviews)
    items = evidence(reviews, review)
    executions = [i for i in items if i["kind"] == "execution"]
    assert [(i["locator"]["execution"], i["locator"]["message"]) for i in executions] == [
        ("exe-1", 0), ("exe-2", 1), ("exe-2", 2)
    ]  # fmt: skip
    assert {(i["locator"]["run"], i["locator"]["delegation"]) for i in executions} == {
        ("task-1", DELEGATION)
    }
    found = [read(reviews, review, i) for i in executions]
    assert [(e["state"], e["stdout"]) for e in found] == [
        ("succeeded", f"{factor}\n"), ("queued", ""), ("succeeded", "is_prime: True\n")
    ]  # fmt: skip
    assert found[0]["request"] == {"code": "step1()", "language": "sage"}
    # The outcome comes first in its batch, and says that none was left out.
    assert items[0]["kind"] == "delegation"
    outcome = read(reviews, review, items[0])
    assert outcome["result"]["summary"] == summary
    assert (outcome["executions"]["held"], outcome["executions"]["left_out"]) == (3, 0)


@pytest.mark.asyncio
async def test_a_busy_delegated_runs_review_holds_its_decisive_executions(store: Any) -> None:
    """The review of #449: of 150 executions, the first 100 came in and the last, decisive
    ones were left out. Those whose output holds a number the outcome states, or nearly,
    come first, then the latest, and the outcome says how many were left out."""
    reviews, documents, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=256, max_feed_bytes=8 * 1024 * 1024)
    stated = "301234567890123"
    tools = [cell(n, f"attempt {n}: residue {n:05d}\n") for n in range(150)]
    tools[20] = cell(20, f"attempt 20: the order is {stated[:-1]}1\n")
    delegated(documents, tools, f"The order is {stated}.")
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    await controller.safe_point([])
    review = dispatch(reviews)
    items = evidence(reviews, review)
    held = {i["locator"]["execution"] for i in items if i["kind"] == "execution"}
    assert len(held) == 100
    assert {"exe-20", "exe-149", "exe-51"} <= held
    assert not {"exe-0", "exe-19", "exe-50"} & held
    (outcome,) = [i for i in items if i["kind"] == "delegation"]
    counted = read(reviews, review, outcome)["executions"]
    assert (counted["held"], counted["left_out"]) == (100, 50)


@pytest.mark.asyncio
async def test_an_input_every_call_echoes_does_not_decide_the_choice(store: Any) -> None:
    """The recheck of #449: every cell printed the modulus N, which the brief gives and
    the summary restates, so every execution matched and cell 20, the one that printed the
    factor, was left out. Numbers are read from the result and report themselves, those
    the brief states are left out, and each stated number holds the latest execution that
    prints it; a factor grouped in LaTeX is read as one number."""
    reviews, documents, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=256, max_feed_bytes=8 * 1024 * 1024)
    modulus, factor = "1234567890123456789012345678901234567891", "98765432109876543211"
    tools = [cell(n, f"N = {modulus}\nresidue {n:05d}\n") for n in range(150)]
    tools[20] = cell(20, f"N = {modulus}\nfactor {factor}\n")
    grouped = r"98765\,43210\,98765\,43211"
    delegated(documents, tools, f"N = {modulus} has the factor ${grouped}$.", f"Factor {modulus}.")
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    await controller.safe_point([])
    review = dispatch(reviews)
    held = {
        i["locator"]["execution"] for i in evidence(reviews, review) if i["kind"] == "execution"
    }
    assert {"exe-20", "exe-149"} <= held
    assert len(held) == 100


@pytest.mark.asyncio
async def test_choosing_executions_makes_a_bounded_number_of_comparisons(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recheck of #449: 150 outputs of 200 twenty-digit numbers each, against a
    summary stating 300, held one safe point for 44 seconds. An outcome's numbers are
    capped, an output's are a set matched exactly first, and the near comparisons have one
    budget for the delegation."""
    import random
    import time

    from sandbox_science.orchestrator.consistency.delegated import MAX_NEAR
    from sandbox_science.orchestrator.consistency.numbers import distance

    counted = 0

    def counting(a: str, b: str, bound: int) -> int | None:
        nonlocal counted
        counted += 1
        return distance(a, b, bound)

    monkeypatch.setattr("sandbox_science.orchestrator.consistency.delegated.distance", counting)
    draw = random.Random(7)  # noqa: S311 - test data, not secrets
    reviews, documents, _ = store
    reviews.defaults = ReviewConfig(max_feed_items=256, max_feed_bytes=8 * 1024 * 1024)
    stated = [str(draw.randrange(10**19, 10**20)) for _ in range(300)]
    tools = [
        cell(n, "\n".join(str(draw.randrange(10**19, 10**20)) for _ in range(200)))
        for n in range(150)
    ]
    delegated(documents, tools, "Values: " + ", ".join(stated), "Compute the table.")
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    began = time.monotonic()
    await controller.safe_point([])
    assert 0 < counted <= MAX_NEAR
    assert time.monotonic() - began < 15
    review = dispatch(reviews)
    held = {
        i["locator"]["execution"] for i in evidence(reviews, review) if i["kind"] == "execution"
    }
    assert "exe-149" in held


@pytest.mark.asyncio
async def test_a_split_delegation_keeps_its_outcome_in_every_evidence_window(store: Any) -> None:
    """A long delegation is split for review, with its outcome repeated as incomplete
    context. Each execution can be checked, but no partial review claims a full pass."""
    from .test_consistency import claim

    reviews, documents, now = store
    tools = [cell(n, f"attempt {n}: candidate ...{7000 + n}\n" + "x" * 3000) for n in range(200)]
    delegated(documents, tools, "The final factor ends in 7199.")
    for n in range(200):
        claim(reviews, f"Step {n} is done.", f"m{n}")
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    await controller.safe_point(
        [{"role": "user", "content": "Factor it."},
         {"role": "assistant", "content": "The factor ends in 7199."}]
    )  # fmt: skip
    reviews.observe("p", "s", "after", "transcript", {"run": "author-1", "message": 9},
                    {"role": "assistant", "content": "Done."})  # fmt: skip
    seen: list[tuple[str, list[dict[str, Any]]]] = []
    while reviews.progress("p", "s")["remaining"]:
        now[0] += 1000
        review = dispatch(reviews, "checkpoint")
        seen.append((review, evidence(reviews, review)))
        reviews.finish("p", "s", review, "owner", "inconclusive", "Checked.")
    assert len(seen) >= 2
    holding = [
        (review, items)
        for review, items in seen
        if any("delegation" in i["locator"] for i in items)
    ]
    assert len(holding) > 1
    executions = {
        i["locator"]["execution"] for _, items in holding for i in items if i["kind"] == "execution"
    }
    assert len(executions) == 100
    assert "exe-199" in executions
    for review, items in holding:
        (outcome,) = [i for i in items if i["kind"] == "delegation"]
        assert read(reviews, review, outcome)["executions"]["left_out"] == 100
        assert not reviews.run("p", "s", review)["coverage_complete"]


@pytest.mark.asyncio
async def test_an_output_cut_short_brings_the_file_it_was_saved_to(
    store: Any, tmp_path: Path
) -> None:
    """The review of #449: a delegated execution's stdout past 8,192 characters is cut in
    its item. The file the harness saved the whole output to comes with it, as the run
    recorded it, copied as a ledger entry's pins are."""
    import hashlib

    reviews, documents, _ = store
    whole = "".join(f"line {n}: residue {n:06d}\n" for n in range(1000))
    path, data = "call-output/exe-1/stdout.txt", whole.encode()
    artifact = published(documents, tmp_path, path, data)
    recorded = {"recorded": 1, "files": [
        {"path": path, "artifact_id": artifact, "revision": 1, "size": len(data),
         "sha256": hashlib.sha256(data).hexdigest(), "content_retained": True,
         "deleted": False, "reproduced": False}]}  # fmt: skip
    delegated(
        documents,
        [cell(1, whole, saved_output={"stdout": path}, recorded_files=recorded)],
        "The last residue is 999.",
    )
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    await controller.safe_point([])
    review = dispatch(reviews)
    items = evidence(reviews, review)
    (execution,) = [i for i in items if i["kind"] == "execution"]
    assert read(reviews, review, execution)["stdout_truncated"]
    (output,) = [i for i in items if "output_of" in i["locator"]]
    assert output["locator"]["output_of"] == {"execution": "exe-1", "stream": "stdout"}
    assert output["locator"]["delegated"]["delegation"] == DELEGATION
    payload = reviews.evidence("p", "s", review, output["evidence"])["payload"]
    assert "line 999: residue 000999" in json.dumps(payload)


def test_open_findings_keep_their_slots_before_feed_rows(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review of #449: feed rows took a snapshot's items first, and open findings that
    did not fit what was left were silently left out of the review that should weigh
    them. Open findings take their slots first, up to half the snapshot; the feed rows
    that do not fit wait for the next window."""
    from sandbox_science.orchestrator.consistency import store as storage

    from .test_consistency import claim, raise_finding

    reviews, _, _ = store
    for n in range(3):  # three messages, each its own place in the transcript
        reviews.observe("p", "s", f"first-{n}", "transcript", {"run": "author-1", "message": n},
                        {"role": "assistant", "content": f"The count is {16 + n}."})  # fmt: skip
    first = dispatch(reviews)
    claimed = reviews.manifest("p", "s", first)["evidence"]
    raised = [
        raise_finding(reviews, first, anchors=[{"evidence": item["evidence"]}],
                      operation_id=f"finding-{n}", statement=f"The count is wrong ({n}).")
        for n, item in enumerate(claimed)
    ]  # fmt: skip
    assert len({finding["finding_id"] for finding in raised}) == 3
    reviews.finish("p", "s", first, "owner", "findings", "Three findings.")
    for n in range(20):
        claim(reviews, f"Step {n} is done.", f"later-{n}")
    monkeypatch.setattr(storage, "MAX_SNAPSHOT_ITEMS", 16)
    review = dispatch(reviews)
    items = reviews.manifest("p", "s", review, limit=50)["evidence"]
    held = [i["locator"]["finding"] for i in items if i["kind"] == "finding"]
    assert sorted(held) == sorted(finding["finding_id"] for finding in raised)
    transcripts = [i for i in items if i["kind"] == "transcript"]
    assert 0 < len(transcripts) < 20
    # The selected prefix is pinned, but global coverage stays incomplete until
    # all linked feed evidence has been reviewed (the ledger gate requires this).
    assert not reviews.run("p", "s", review)["coverage_complete"]
    assert reviews.progress("p", "s")["remaining"]
