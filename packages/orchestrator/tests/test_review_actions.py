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
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

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
