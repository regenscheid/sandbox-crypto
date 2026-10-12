# SPDX-License-Identifier: MIT
"""Pinned review evidence, explicit grants and durable finding/window boundaries."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency import store as storage
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import ReviewConfig, ReviewType
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.model import ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore

MAIN = Author(kind="agent", session_id="s", run_id="author-1", role="main", model="scripted")


@pytest.fixture
def store(tmp_path: Path):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.open_session("p", "other", dedicated=False)
    documents = DocumentStore(store_path(tmp_path))
    now = [1000.0]
    reviews = ReviewStore(documents, clock=lambda: now[0])
    yield reviews, documents, now
    documents.close()


def claim(store: ReviewStore, content: str = "The check gave 16.", key: str = "m1") -> int:
    return store.observe(
        "p",
        "s",
        key,
        "transcript",
        {"run": "author-1", "message": 1},
        {"role": "assistant", "content": content},
    )


def dispatch(store: ReviewStore, kind: ReviewType = "terminal") -> str:
    review = store.dispatch("p", "s", kind, author_run="author-1", owner="owner", alias="reviewer")
    assert review is not None
    return review


def anchor(store: ReviewStore, review: str, kind: str = "transcript") -> dict[str, Any]:
    return {
        "evidence": next(
            i["evidence"] for i in store.manifest("p", "s", review)["evidence"] if i["kind"] == kind
        )
    }


def raise_finding(store: ReviewStore, review: str, **overrides: Any) -> dict[str, Any]:
    return Findings(store).create(
        "p",
        "s",
        review,
        "owner",
        {
            "severity": "fail",
            "category": "contradicting_value",
            "statement": "The output records 15, not 16.",
            "anchors": overrides["anchors"] if "anchors" in overrides else [anchor(store, review)],
            "operation_id": "finding-1",
            **overrides,
        },
    )


def read_confirmation(reviews: ReviewStore, review: str) -> None:
    after = 0
    while True:
        page = reviews.manifest("p", "s", review, after=after, limit=20)
        for item in page["evidence"]:
            reviews.read_evidence("p", "s", review, item["evidence"], owner="owner")
        if page["next_after"] is None:
            break
        after = page["next_after"]


def test_document_snapshot_reads_exact_revision_after_head_changes(store) -> None:
    reviews, documents, _ = store
    doc = documents.create(
        "p", name="notes", title="N", text="# Count\n\nThere are 16.\n", author=MAIN, session_id="s"
    ).document_id
    reviews.ingest_documents("p", "s")
    review = dispatch(reviews)
    pinned = anchor(reviews, review, "document")
    documents.edit_source(
        "p",
        doc,
        base=1,
        text="# Count\n\nThere are 15.\n",
        author=USER,
        reason="Correct count",
        operation_id="correct",
    )
    read = reviews.read_evidence("p", "s", review, pinned["evidence"], owner="owner")
    assert "16" in read["text"]
    assert "15" not in read["text"]
    with documents._read() as db:
        assert (
            db.execute("SELECT count(*) FROM review_reads WHERE review_id=?", (review,)).fetchone()[
                0
            ]
            == 1
        )
    reviews.ingest_documents("p", "s")
    reviews.finish("p", "s", review, "owner", "passed", "Checked the pinned count.")
    next_review = dispatch(reviews)
    assert (
        reviews.run("p", "s", next_review)["start_seq"]
        == reviews.run("p", "s", review)["end_seq"] + 1
    )


@pytest.mark.asyncio
async def test_reviewer_has_no_execution_or_editing_tools(store) -> None:
    reviews, documents, _ = store
    claim(reviews)
    review = dispatch(reviews)
    evidence = anchor(reviews, review)["evidence"]
    denied = ["shell", "python", "document_patch", "web_fetch", "delegate", "permission_decide"]
    model = ScriptedModel.from_json(
        [
            {
                "tool_calls": [
                    {"name": n, "arguments": {"script": "touch should-not-exist"}} for n in denied
                ]
            },
            {"tool_calls": [{"name": "review_read", "arguments": {"evidence": evidence}}]},
            {
                "tool_calls": [
                    {
                        "name": "review_checked",
                        "arguments": {
                            "statement": "The transcript is present.",
                            "evidence": [evidence],
                        },
                    }
                ]
            },
            {
                "text": json.dumps(
                    {
                        "outcome": "passed",
                        "summary": "No inconsistency in the checked transcript.",
                        "cap_reached": False,
                    }
                )
            },
        ]
    )
    await ReviewerRunner(reviews, "p", "s", review, "owner", model).run()
    assert reviews.run("p", "s", review)["state"] == "passed"
    with documents._read() as db:
        records = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM review_records WHERE kind='tool' AND review_id=?", (review,)
            )
        ]
    assert [r["tool"] for r in records if r["result"].get("error") == "tool_not_granted"] == denied
    assert not (documents.path.parent / "should-not-exist").exists()


def test_value_finding_has_pinned_document_and_log_anchors(store) -> None:
    reviews, documents, _ = store
    doc = documents.create(
        "p", name="count", title="N", text="There are 16.\n", author=MAIN, session_id="s"
    ).document_id
    reviews.ingest_documents("p", "s")
    reviews.observe(
        "p",
        "s",
        "exe-1",
        "execution",
        {"execution": "exe-1", "attempt": "attempt-1"},
        {"stdout": "nonzero_ok = 15\n", "stderr": ""},
        effects=True,
    )
    review = dispatch(reviews)
    a = anchor(reviews, review, "document")
    a["block"] = documents.mapped("p", doc, 1).block_ids[0]
    log = {
        **anchor(reviews, review, "execution"),
        "stream": "stdout",
        "first_line": 1,
        "last_line": 1,
    }
    finding = raise_finding(reviews, review, anchors=[a, log])
    assert finding["status"] == "open"
    saved = Findings(reviews).get("p", "s", finding["finding_id"])
    assert saved["anchors"][0]["locator"]["revision"] == 1
    assert saved["anchors"][1]["locator"]["attempt"] == "attempt-1"
    with pytest.raises(DocumentError, match="outside the pinned"):
        raise_finding(reviews, review, anchors=[{**log, "last_line": 2}], operation_id="bad-log")


def test_fix_requires_main_or_user_and_confirmation_grant(store) -> None:
    reviews, documents, _ = store
    claim(reviews)
    review = dispatch(reviews)
    finding = raise_finding(reviews, review)
    reviews.finish("p", "s", review, "owner", "findings", "One contradiction.")
    doc = documents.create(
        "p",
        name="fixed",
        title="N",
        text="The recorded count is 15.\n",
        author=MAIN,
        session_id="s",
    ).document_id
    response = {
        "finding": finding["finding_id"],
        "expected": 1,
        "action": "fix",
        "explanation": "Corrected the count.",
        "revisions": [[doc, 1]],
        "operation_id": "respond",
    }
    delegated = MAIN.model_copy(update={"role": "delegated"})
    with pytest.raises(DocumentError, match="only the user"):
        Findings(reviews).respond("p", "s", response, delegated)
    with pytest.raises(DocumentError, match="cannot confirm"):
        Findings(reviews).respond("p", "s", {**response, "action": "confirm"}, MAIN)
    result = Findings(reviews).respond("p", "s", response, MAIN)
    assert result["status"] == "claimed fixed"
    assert Findings(reviews).respond("p", "s", response, MAIN)["replayed"]
    confirm = dispatch(reviews, "confirmation")
    finding_evidence = next(
        i for i in reviews.manifest("p", "s", confirm)["evidence"] if i["kind"] == "finding"
    )
    pinned = reviews.evidence("p", "s", confirm, finding_evidence["evidence"])
    assert pinned["payload"]["response"]["payload"]["explanation"] == "Corrected the count."
    read_confirmation(reviews, confirm)
    result = Findings(reviews).confirm(
        "p",
        "s",
        confirm,
        "owner",
        {
            "finding": finding["finding_id"],
            "expected": 2,
            "decision": "confirm",
            "explanation": "The pinned revision now agrees.",
            "anchors": [anchor(reviews, confirm, "document")],
            "operation_id": "confirm",
        },
    )
    assert result["status"] == "confirmed fixed"
    assert [
        h["status"] for h in Findings(reviews).get("p", "s", finding["finding_id"])["history"]
    ] == ["open", "claimed fixed", "confirmed fixed"]


def test_missing_evidence_is_inconclusive_and_never_a_fail_notice(store) -> None:
    reviews, documents, _ = store
    claim(reviews)
    review = dispatch(reviews)
    finding = raise_finding(reviews, review, assessment="missing_evidence")
    assert finding["status"] == "inconclusive"
    with documents._read() as db:
        assert db.execute("SELECT count(*) FROM review_notices").fetchone()[0] == 0


def test_finding_and_reported_caps_remain_capped_at_finish(store) -> None:
    reviews, _, _ = store
    reviews.configure("p", "s", ReviewConfig(max_findings=1))
    claim(reviews)
    review = dispatch(reviews)
    raise_finding(reviews, review)
    assert raise_finding(reviews, review, operation_id="over-cap")["cap_reached"]
    reviews.finish("p", "s", review, "owner", "findings", "A finding was retained.")
    assert reviews.run("p", "s", review)["state"] == "capped"
    assert reviews.run("p", "s", review)["cap_reached"]
    another = dispatch(reviews)
    reviews.finish("p", "s", another, "owner", "passed", "Bound reached.", cap_reached=True)
    assert reviews.run("p", "s", another)["state"] == "capped"


def test_failed_and_dropped_windows_retry_without_skipping_new_work(store) -> None:
    reviews, _documents, now = store
    first = claim(reviews)
    review = dispatch(reviews)
    claim(reviews, "Later claim", "m2")
    reviews.finish("p", "s", review, "owner", "failed", "Reviewer unavailable.")
    retry = dispatch(reviews)
    assert reviews.run("p", "s", retry)["end_seq"] == first
    now[0] += 121
    assert reviews.recover("p", "s") == 1
    assert reviews.run("p", "s", retry)["state"] == "dropped"
    third = dispatch(reviews)
    reviews.finish("p", "s", third, "owner", "failed", "Third failure.")
    listed = reviews.listing("p", "s")
    assert len(listed["unreviewed"]) == 1
    next_review = dispatch(reviews)
    assert reviews.run("p", "s", next_review)["start_seq"] == first + 1
    assert reviews.manifest("p", "s", next_review)["evidence"][0]["locator"]["run"] == "author-1"


def test_checkpoint_interval_and_one_running_review_merge_window(store) -> None:
    reviews, _, now = store
    reviews.observe("p", "s", "d1", "document", {}, {"value": 1}, characters=2000)
    assert reviews.due("p", "s")
    first = dispatch(reviews, "checkpoint")
    now[0] += 30
    reviews.observe("p", "s", "d2", "document", {}, {"value": 2}, characters=1000)
    now[0] += 20
    reviews.observe("p", "s", "d3", "document", {}, {"value": 3}, characters=1000)
    assert not reviews.due("p", "s")
    assert (
        reviews.dispatch(
            "p", "s", "checkpoint", author_run="author-1", owner="other", alias="reviewer"
        )
        is None
    )
    now[0] += 10
    reviews.finish("p", "s", first, "owner", "passed", "Checked.")
    assert not reviews.due("p", "s")
    now[0] += 60
    assert reviews.due("p", "s")
    second = dispatch(reviews, "checkpoint")
    assert reviews.run("p", "s", second)["start_seq"] == reviews.run("p", "s", first)["end_seq"] + 1
    assert len(reviews.manifest("p", "s", second)["evidence"]) == 2


def test_snapshot_byte_prefix_keeps_overflow_for_next_window(store, monkeypatch) -> None:
    reviews, _, _ = store
    monkeypatch.setattr(storage, "MAX_SNAPSHOT_BYTES", 2000)
    for i in range(6):
        claim(reviews, "x" * 900, f"m{i}")
    seen = []
    for _ in range(6):
        review = dispatch(reviews)
        manifest = reviews.manifest("p", "s", review)
        seen.extend(i["sha256"] for i in manifest["evidence"])
        reviews.finish("p", "s", review, "owner", "inconclusive", "Bounded prefix checked.")
    assert len(seen) == 6


@pytest.mark.parametrize("bad", [{}, [], True, 1.5, "x" * 100000])
def test_malformed_scope_cursors_are_refused(store, bad: Any) -> None:
    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    for fn in (
        lambda: reviews.manifest("p", "s", review, after=bad),
        lambda: reviews.listing("p", "s", after=bad),
        lambda: reviews.evidence("p", "s", review, bad),
        lambda: Findings(reviews).listing("p", "s", after=bad),
    ):
        with pytest.raises(DocumentError):
            fn()


def test_concurrent_budget_reservations_and_usage_are_idempotent(store) -> None:
    reviews, _documents, _ = store
    reviews.configure("p", "s", ReviewConfig(calls_per_hour=1))
    claim(reviews)
    review = dispatch(reviews)
    with ThreadPoolExecutor(max_workers=10) as executor:
        reserved = list(
            executor.map(
                lambda _: reviews.reserve_call("p", "s", review, 100, owner="owner"), range(20)
            )
        )
    assert len([r for r in reserved if r is not None]) == 1
    call = next(r for r in reserved if r is not None)
    reviews.call_usage(
        "p", "s", review, call, model="resolved", input_tokens=2, output_tokens=3, owner="owner"
    )
    reviews.call_usage(
        "p", "s", review, call, model="resolved", input_tokens=2, output_tokens=3, owner="owner"
    )
    assert reviews.run("p", "s", review)["input_tokens"] == 2
    assert reviews.run("p", "s", review)["output_tokens"] == 3


def test_supersession_preserves_unrelated_block_and_never_confirms(store) -> None:
    reviews, documents, _ = store
    doc = documents.create(
        "p",
        name="notes",
        title="N",
        text="# Count\n\nThere are 16.\n\nOther text.\n",
        author=MAIN,
        session_id="s",
    ).document_id
    reviews.ingest_documents("p", "s")
    review = dispatch(reviews)
    block = documents.mapped("p", doc, 1).block_ids[1]
    finding = raise_finding(
        reviews, review, anchors=[{**anchor(reviews, review, "document"), "block": block}]
    )
    documents.edit_source(
        "p",
        doc,
        base=1,
        text="# Count\n\nThere are 16.\n\nNew unrelated text.\n",
        author=USER,
        reason="Other block",
        operation_id="unrelated",
    )
    Findings(reviews).supersede("p", "s")
    assert Findings(reviews).get("p", "s", finding["finding_id"])["status"] == "open"
    documents.edit_source(
        "p",
        doc,
        base=2,
        text="# Count\n\nThere are 15.\n\nNew unrelated text.\n",
        author=USER,
        reason="Changed anchored block",
        operation_id="change",
    )
    Findings(reviews).supersede("p", "s")
    final = Findings(reviews).get("p", "s", finding["finding_id"])
    assert final["status"] == "superseded"
    assert all(h["status"] != "confirmed fixed" for h in final["history"])


def test_confirmation_retains_original_and_response_evidence_with_exact_ids(store) -> None:
    reviews, _documents, _ = store
    claim(reviews)
    reviews.observe(
        "p",
        "s",
        "original-output",
        "execution",
        {"execution": "exe-original"},
        {"stdout": "nonzero_ok = 15\n", "stderr": ""},
        effects=True,
    )
    original = dispatch(reviews)
    log = anchor(reviews, original, "execution")
    finding = raise_finding(reviews, original, anchors=[log])
    reviews.finish("p", "s", original, "owner", "findings", "Original output disagrees.")
    reviews.observe(
        "p",
        "s",
        "response-output",
        "execution",
        {"execution": "exe-fix"},
        {"stdout": "checked = 15\n", "stderr": ""},
        effects=True,
    )
    response_review = dispatch(reviews)
    changed = anchor(reviews, response_review, "execution")
    reviews.finish("p", "s", response_review, "owner", "passed", "Pinned response output.")
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding["finding_id"],
            "expected": 1,
            "action": "fix",
            "explanation": "Updated the claim using this output.",
            "evidence": [changed],
            "operation_id": "fix-output",
        },
        MAIN,
    )
    confirmation = dispatch(reviews, "confirmation")
    manifest = reviews.manifest("p", "s", confirmation, limit=20)["evidence"]
    ids = [item["evidence"] for item in manifest]
    assert ids.count(log["evidence"]) == ids.count(changed["evidence"]) == 1
    assert (
        "nonzero_ok = 15"
        in reviews.read_evidence("p", "s", confirmation, log["evidence"], owner="owner")["text"]
    )
    assert (
        "checked = 15"
        in reviews.read_evidence("p", "s", confirmation, changed["evidence"], owner="owner")["text"]
    )
    foreign = reviews.dispatch(
        "p", "other", "terminal", author_run="other", owner="other", alias="r"
    )
    assert foreign is not None
    with pytest.raises(DocumentError, match="outside this snapshot"):
        reviews.evidence("p", "other", foreign, log["evidence"])


def test_superseded_snapshot_retains_old_and_user_replacement_revision(store) -> None:
    reviews, documents, _ = store
    doc = documents.create(
        "p", name="replace", title="N", text="There are 16.\n", author=MAIN, session_id="s"
    ).document_id
    reviews.ingest_documents("p", "s")
    original = dispatch(reviews)
    old = anchor(reviews, original, "document")
    finding = raise_finding(reviews, original, anchors=[old])
    reviews.finish("p", "s", original, "owner", "findings", "Count finding.")
    documents.edit_source(
        "p",
        doc,
        base=1,
        text="There are 15.\n",
        author=USER,
        reason="New count",
        operation_id="user-replacement",
    )
    Findings(reviews).supersede("p", "s")
    current = dispatch(reviews)
    evidence = reviews.manifest("p", "s", current, limit=20)["evidence"]
    replacement = next(
        item for item in evidence if item["kind"] == "document" and item["locator"]["revision"] == 2
    )
    assert "15" in reviews.read_evidence("p", "s", current, replacement["evidence"])["text"]
    assert "16" in reviews.read_evidence("p", "s", current, old["evidence"])["text"]
    assert Findings(reviews).get("p", "s", finding["finding_id"])["status"] == "superseded"


def context_for(store: ReviewStore):
    from sandbox_science.contracts import ExecutionContext

    project = ProjectStore(store.documents.path.parent).get("p")
    return ExecutionContext(
        project_id="p", session_id="s", environment_id=project.sessions["s"].environment_id
    )


def test_confirmation_does_not_clear_or_replace_failed_author_window(store) -> None:
    reviews, _, _ = store
    claim(reviews)
    initial = dispatch(reviews)
    finding = raise_finding(reviews, initial)
    reviews.finish("p", "s", initial, "owner", "findings", "Original finding.")
    second = claim(reviews, "Second claim", "m2")
    failed = dispatch(reviews)
    reviews.finish("p", "s", failed, "owner", "failed", "Window must retry exactly.")
    Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": finding["finding_id"],
            "expected": 1,
            "action": "fix",
            "explanation": "Changed the evidence.",
            "evidence": [anchor(reviews, initial)],
            "operation_id": "new-fix",
        },
        MAIN,
    )
    claim(reviews, "Later claim", "m3")
    for _ in range(3):
        confirmation = dispatch(reviews, "confirmation")
        reviews.finish("p", "s", confirmation, "owner", "failed", "Confirmation unavailable.")
        assert reviews.progress("p", "s")["retry_end"] == second
    assert (
        reviews.dispatch("p", "s", "confirmation", author_run="author-1", owner="owner", alias="r")
        is None
    )
    retry = dispatch(reviews)
    assert reviews.run("p", "s", retry)["end_seq"] == second


@pytest.mark.asyncio
async def test_cancelled_terminal_wait_requeues_consumed_user_input(store) -> None:
    import asyncio
    import contextlib

    from sandbox_science.orchestrator.consistency.controller import ReviewController

    reviews, _, _ = store
    queue: asyncio.Queue[str] = asyncio.Queue()
    queue.put_nowait("Preserve this new user turn")
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None, messages=queue
    )

    async def block() -> None:
        await asyncio.Event().wait()

    blocker = asyncio.create_task(block())
    waiter = asyncio.create_task(controller._wait(blocker))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert queue.get_nowait() == "Preserve this new user turn"
    blocker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await blocker


@pytest.mark.asyncio
async def test_terminal_drains_prior_document_events_and_classifies_neutral_answer(store) -> None:
    from sandbox_science.orchestrator.consistency.controller import ReviewController

    reviews, documents, _ = store
    with documents._write() as db:
        db.executemany(
            "INSERT INTO events(time,kind,producer,project_id,session_id,document_id,payload) "
            "VALUES('now','document.read','test','p','other','unrelated','{}')",
            [() for _ in range(250)],
        )
    documents.create(
        "p", name="terminal", title="N", text="A report.\n", author=MAIN, session_id="s"
    )
    controller = ReviewController(
        reviews, context_for(reviews), run_id="author-1", model=None, alias=None
    )
    summary = await controller.terminal([], "Here is the report.")
    assert summary["classifier"]["effects"]
    assert summary["classifier"]["document_ingestion"]["complete"]
    assert summary["outcome"] == "unreviewed"
    assert not summary.get("pending")


@pytest.mark.asyncio
async def test_final_provider_usage_over_budget_is_capped(store) -> None:
    reviews, _, _ = store
    claim(reviews)
    review = dispatch(reviews)
    evidence = anchor(reviews, review)["evidence"]
    from sandbox_science.orchestrator.model import ModelTurn, ToolCall

    class Overspend:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(
                    None,
                    (
                        ToolCall(
                            "check",
                            "review_checked",
                            json.dumps({"statement": "Checked", "evidence": [evidence]}),
                        ),
                    ),
                )
            return ModelTurn(
                json.dumps({"outcome": "passed", "summary": "Checked", "cap_reached": False}),
                input_tokens=100000,
                output_tokens=100,
            )

    await ReviewerRunner(reviews, "p", "s", review, "owner", Overspend()).run()
    assert reviews.run("p", "s", review)["state"] == "capped"


@pytest.mark.asyncio
async def test_warn_checkpoint_delivers_counts_without_statement(store) -> None:
    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    reviews, _, _ = store
    claim(reviews)
    prior = dispatch(reviews)
    raise_finding(
        reviews, prior, severity="warn", statement="Private warning body must not be injected."
    )
    reviews.finish("p", "s", prior, "owner", "findings", "Warning.")
    reviews.configure("p", "s", ReviewConfig(enabled=False))
    model = ScriptedModel.from_json([{"text": "A conversational answer."}])
    controller = ReviewController(
        reviews, context_for(reviews), run_id="next", model=None, alias=None
    )
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        transcript = await run_task(
            model,
            Toolbox(client, context_for(reviews), reviews=controller),
            "Continue",
            run_id="next",
            max_turns=1,
        )
    sent = json.dumps(model.requests[0])
    assert "warn:open" in sent
    assert "Private warning body" not in sent
    assert transcript.review_notice_ids == []


@pytest.mark.asyncio
async def test_late_final_turn_notice_remains_pending_for_next_run(store) -> None:
    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.consistency.notices import FindingNotices
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.model import ModelTurn
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    reviews, _, _ = store

    class Late:
        async def complete(self, messages, tools):
            review = dispatch(reviews)
            raise_finding(reviews, review)
            reviews.finish("p", "s", review, "owner", "findings", "Late finding.")
            return ModelTurn("Here is the answer.")

    controller = ReviewController(
        reviews, context_for(reviews), run_id="late", model=None, alias=None
    )
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        transcript = await run_task(
            Late(),
            Toolbox(client, context_for(reviews), reviews=controller),
            "Continue",
            run_id="late",
            max_turns=1,
        )
    assert transcript.review_notice_ids == []
    assert FindingNotices(reviews).pending("p", "s", "next", set())


@pytest.mark.asyncio
async def test_persistent_owner_keeps_review_running_when_new_input_arrives(store) -> None:
    import asyncio

    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.model import ModelTurn
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    reviews, _, _ = store
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking:
        async def complete(self, messages, tools):
            entered.set()
            await release.wait()
            return ModelTurn(
                json.dumps(
                    {
                        "outcome": "inconclusive",
                        "summary": "No pinned check completed.",
                        "cap_reached": False,
                    }
                )
            )

    queue: asyncio.Queue[str] = asyncio.Queue()
    controller = ReviewController(
        reviews,
        context_for(reviews),
        run_id="new-input",
        model=Blocking(),
        alias="isolated-reviewer",
        messages=queue,
        persistent=True,
    )
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        task = asyncio.create_task(
            run_task(
                ScriptedModel.from_json([{"text": "Done."}]),
                Toolbox(client, context_for(reviews), reviews=controller),
                "Finish",
                run_id="new-input",
            )
        )
        await entered.wait()
        queue.put_nowait("Start the next user turn now.")
        transcript = await asyncio.wait_for(task, 2)
        assert transcript.stopped == "user_message"
        assert transcript.review_summary["user_message"] == "Start the next user turn now."
        assert controller.task is not None
        assert not controller.task.done()
        assert reviews.run("p", "s", controller.review_ids[-1])["state"] == "running"
        release.set()
        await controller.task
        await controller.close()


@pytest.mark.asyncio
async def test_finding_notice_acknowledgement_requires_saved_successful_model_turn(
    store, monkeypatch
) -> None:
    import argparse
    from datetime import UTC, datetime

    from sandbox_science.contracts import ErrorKind
    from sandbox_science.orchestrator import cli
    from sandbox_science.orchestrator.consistency.controller import ReviewController
    from sandbox_science.orchestrator.consistency.notices import FindingNotices
    from sandbox_science.orchestrator.loop import run_task
    from sandbox_science.orchestrator.projects import ProjectError
    from sandbox_science.orchestrator.tools import Toolbox
    from sandbox_science.orchestrator.worker_client import WorkerClient

    reviews, documents, _ = store
    claim(reviews)
    review = dispatch(reviews)
    raise_finding(reviews, review)
    reviews.finish("p", "s", review, "owner", "findings", "A finding.")
    reviews.configure("p", "s", ReviewConfig(enabled=False))
    controller = ReviewController(
        reviews, context_for(reviews), run_id="saved", model=None, alias=None
    )
    async with WorkerClient("http://127.0.0.1:1", "synthetic-token") as client:
        transcript = await run_task(
            ScriptedModel.from_json([{"text": "Read the finding."}]),
            Toolbox(client, context_for(reviews), reviews=controller),
            "Continue",
            run_id="saved",
            max_turns=1,
        )
    assert transcript.review_notice_ids
    now = datetime.now(UTC)
    fields = {
        "run_id": "saved",
        "context": context_for(reviews).model_dump(mode="json"),
        "task": "Continue",
        "started_at": now,
        "ended_at": now,
        "stopped": transcript.stopped,
        "final_text": transcript.final_text,
        "messages": transcript.messages,
        "tools": transcript.tool_records,
        "review_notice_ids": transcript.review_notice_ids,
        "review_summary": transcript.review_summary,
    }
    args = argparse.Namespace(state_dir=documents.path.parent, transcript=None)
    original = ProjectStore.save_run

    def failed_save(self, record):
        raise ProjectError(ErrorKind.INTERNAL, "Synthetic save failure")

    monkeypatch.setattr(ProjectStore, "save_run", failed_save)
    assert cli._keep(args, fields, 0) == 1
    with documents._read() as db:
        assert db.execute("SELECT delivered_run FROM review_notices").fetchone()[0] is None
    monkeypatch.setattr(ProjectStore, "save_run", original)
    assert cli._keep(args, fields, 0) == 0
    assert (
        ProjectStore(documents.path.parent)
        .run("p", "s", ProjectStore(documents.path.parent).runs("p", "s")[0].name)
        .review_notice_ids
        == transcript.review_notice_ids
    )
    assert FindingNotices(reviews).pending("p", "s", "next", set()) == []


def test_confirmation_requires_nonempty_reads_and_stops_after_two_rejections(store) -> None:
    reviews, documents, _ = store
    claim(reviews)
    original = dispatch(reviews)
    finding = raise_finding(reviews, original)
    reviews.finish("p", "s", original, "owner", "findings", "One contradiction.")
    doc = documents.create(
        "p", name="claimed-fix", title="N", text="Still says 16.\n", author=MAIN, session_id="s"
    ).document_id
    for expected in (1, 3):
        response = Findings(reviews).respond(
            "p",
            "s",
            {
                "finding": finding["finding_id"],
                "expected": expected,
                "action": "fix",
                "explanation": "Claiming this exact revision fixed it.",
                "revisions": [[doc, 1]],
                "operation_id": f"fix-{expected}",
            },
            MAIN,
        )
        confirmation = dispatch(reviews, "confirmation")
        decision = {
            "finding": finding["finding_id"],
            "expected": response["revision"],
            "decision": "reject",
            "explanation": "The revision still contradicts the original output.",
            "anchors": [anchor(reviews, confirmation, "document")],
            "operation_id": f"reject-{expected}",
        }
        for item in reviews.manifest("p", "s", confirmation, limit=20)["evidence"]:
            reviews.read_evidence(
                "p", "s", confirmation, item["evidence"], start=1000000, owner="owner"
            )
        with pytest.raises(DocumentError, match="read the pinned"):
            Findings(reviews).confirm("p", "s", confirmation, "owner", decision)
        for item in reviews.manifest("p", "s", confirmation, limit=20)["evidence"]:
            reviews.read_evidence("p", "s", confirmation, item["evidence"], count=1, owner="owner")
        with pytest.raises(DocumentError, match="read the pinned"):
            Findings(reviews).confirm("p", "s", confirmation, "owner", decision)
        read_confirmation(reviews, confirmation)
        ended = Findings(reviews).confirm("p", "s", confirmation, "owner", decision)
        reviews.finish("p", "s", confirmation, "owner", "findings", "The claimed fix was rejected.")
    assert ended["status"] == "unaddressed"
    assert Findings(reviews).get("p", "s", finding["finding_id"])["reflags"] == 2
