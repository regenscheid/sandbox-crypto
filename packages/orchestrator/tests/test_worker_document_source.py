# SPDX-License-Identifier: MIT
"""Captured worker data never widens authority or loses the exact rejected bytes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.contracts.document_edits import DirectEditCandidate
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    publication,
    store_path,
)
from sandbox_science.orchestrator.documents.publication import Publications
from sandbox_science.orchestrator.documents.worker_source import (
    MAX_CAPTURE_BYTES,
    Quota,
    RawBudgets,
    TrustedWorkerBinding,
    VerifiedCapture,
    WorkerSourceAdapter,
    encoded,
    sha,
)
from sandbox_science.orchestrator.projects import ProjectStore

TEXT = "# Count\n\nAlpha stays.\n\nBeta stays.\n\n$x^2$\n"


@pytest.fixture
def setup(tmp_path: Path):
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.open_session("p", "dedicated", dedicated=True)
    documents = DocumentStore(store_path(tmp_path))
    doc = documents.create("p", name="report", title="Report", text=TEXT, author=USER).document_id
    binding = TrustedWorkerBinding(
        project_id="p",
        worker_binding="worker-1",
        environment_id=projects.get("p").default_environment_id,
        incarnation="inc-1",
        document_id=doc,
        catalog_seq=1,
        published_revision=1,
        published_sha256=documents.revision("p", doc, 1).info.sha256,
        session_id="s",
        execution_id="exe-1",
        attempt_id="att-1",
        run_id="run-1",
    )
    adapter = WorkerSourceAdapter(documents, projects)
    yield documents, projects, adapter, binding
    documents.close()


def request(binding: TrustedWorkerBinding, raw: bytes, *, op: str = "op-1", **changes):
    candidate = DirectEditCandidate(
        edit_id="edit-" + op,
        operation_id=op,
        document_id=binding.document_id,
        base_revision=binding.published_revision,
        base_written_sha256=binding.published_sha256,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        raw_bytes=len(raw),
        capture_id="capture-" + op,
        catalog_seq=binding.catalog_seq,
    ).model_copy(update=changes)
    return candidate, VerifiedCapture(candidate.capture_id, raw, complete=True, writer_closed=True)


def stored(documents: DocumentStore) -> int:
    with documents._read() as db:
        assert db is not None
        return db.execute("SELECT count(*) FROM worker_source_receipts").fetchone()[0]


def test_named_rebase_normalization_ids_and_lost_ack_replay(setup, monkeypatch) -> None:
    documents, projects, adapter, binding = setup
    before = documents.mapped("p", binding.document_id, 1)
    documents.edit_source(
        "p",
        binding.document_id,
        base=1,
        text=TEXT.replace("Beta stays.", "Beta user change."),
        author=USER,
        reason="Unrelated user edit",
        operation_id="user-change",
    )
    raw = TEXT.replace("Alpha stays.", "Alpha worker change.").replace("$x^2$", r"\(x^3\)").encode()
    candidate, capture = request(binding, raw)
    receipt = adapter.apply(binding, candidate, capture)
    assert receipt.change.outcome == "applied"
    assert receipt.change.rebased
    assert receipt.change.revision == 3
    assert receipt.retained_exact
    assert not receipt.safe_to_restore
    assert receipt.publication_pending
    after = documents.mapped("p", binding.document_id)
    assert "Alpha worker change." in after.parsed.text
    assert "Beta user change." in after.parsed.text
    assert "$x^3$" in after.parsed.text
    assert after.block_ids == before.block_ids
    assert after.span_ids == before.span_ids
    author = documents.revision("p", binding.document_id).info.author
    assert author.kind == "worker"
    assert author.role == "delegated"
    assert author.execution_id == "exe-1"
    canonical = receipt.model_dump(mode="json")
    canonical["receipt_sha256"] = None
    assert receipt.receipt_sha256 == sha(encoded(canonical).encode())
    documents.close()
    fresh = DocumentStore(documents.path)
    try:
        restarted = WorkerSourceAdapter(fresh, projects)

        def no_parse(*args, **kwargs):
            raise AssertionError("replay must not prepare or parse")

        monkeypatch.setattr(fresh, "_prepare_source", no_parse)
        replay = restarted.apply(binding, candidate, capture)
        assert replay.replayed
        assert replay.receipt_sha256 == receipt.receipt_sha256
        assert fresh.revision("p", binding.document_id).info.number == 3
        assert restarted.read_raw(binding, "op-1") == raw
        assert stored(fresh) == 1
    finally:
        fresh.close()


@pytest.mark.parametrize("mode", ["unknown", "mismatch", "nonutf8", "oversize", "deleted"])
def test_exact_raw_refusals_never_guess_base_or_apply_empty_operations(setup, mode: str) -> None:
    documents, _, adapter, binding = setup
    raw = (
        b"\xff\x00untrusted"
        if mode == "nonutf8"
        else TEXT.replace("Alpha stays.", "Changed.").encode()
    )
    changes: dict[str, object] = {}
    if mode == "unknown":
        changes = {"base_revision": None, "base_written_sha256": None}
    elif mode == "mismatch":
        changes = {"base_written_sha256": "b" * 64}
    elif mode == "oversize":
        raw = b"x" * (1024 * 1024 + 1)
    elif mode == "deleted":
        documents.delete(
            "p", binding.document_id, author=USER, reason="Deleted", operation_id="delete"
        )
    candidate, capture = request(binding, raw)
    candidate = candidate.model_copy(update=changes)
    result = adapter.apply(binding, candidate, capture)
    assert result.status == "recorded"
    assert result.change.outcome == "refused"
    assert result.retained_exact
    assert result.refusal_code is not None
    found = b"".join(adapter.read_raw(binding, "op-1", start=i) for i in range(0, len(raw), 65536))
    assert found == raw
    assert documents.revision("p", binding.document_id).info.number == (
        2 if mode == "deleted" else 1
    )
    patch = documents.patches("p", binding.document_id)[0]
    assert patch.details["raw_rejection"]
    assert patch.details["reported_base"] == candidate.base_revision
    assert patch.operations == []
    with pytest.raises(DocumentError, match="no safe patch"):
        documents.preview_patch("p", binding.document_id, patch.patch_id)
    with pytest.raises(DocumentError, match="no safe patch"):
        documents.apply_patch(
            "p",
            binding.document_id,
            patch.patch_id,
            base=documents.revision("p", binding.document_id).info.number,
            author=USER,
            reason="Must refuse",
            operation_id="apply-raw",
        )
    assert adapter.apply(binding, candidate, capture).replayed


@pytest.mark.parametrize(("closed", "complete"), [(False, True), (True, False)])
def test_uncertain_writer_or_partial_capture_is_held_without_consuming_operation(
    setup, closed, complete
) -> None:
    documents, _, adapter, binding = setup
    candidate, capture = request(binding, b"Updated text")
    uncertain = VerifiedCapture(
        capture.capture_id, capture.raw, complete=complete, writer_closed=closed
    )
    held = adapter.apply(binding, candidate, uncertain)
    assert held.status == "held"
    assert not held.retained_exact
    assert not held.safe_to_restore
    assert stored(documents) == 0
    assert adapter.apply(binding, candidate, capture).status == "recorded"


def test_capture_cap_and_digest_mismatch(setup) -> None:
    documents, _, adapter, binding = setup
    candidate, capture = request(binding, b"x" * (MAX_CAPTURE_BYTES + 1))
    assert adapter.apply(binding, candidate, capture).status == "held"
    assert stored(documents) == 0
    candidate, capture = request(binding, b"abc")
    with pytest.raises(DocumentError, match="digest/length"):
        adapter.apply(binding, candidate, VerifiedCapture(capture.capture_id, b"abd", True, True))
    assert stored(documents) == 0


def test_conflict_and_suggestions_keep_exact_raw_without_overwriting(setup) -> None:
    documents, _, adapter, binding = setup
    documents.edit_source(
        "p",
        binding.document_id,
        base=1,
        text=TEXT.replace("Alpha stays.", "Alpha user."),
        author=USER,
        reason="User",
        operation_id="user",
    )
    raw = TEXT.replace("Alpha stays.", "Alpha worker.").encode()
    candidate, capture = request(binding, raw)
    conflict = adapter.apply(binding, candidate, capture)
    assert conflict.change.outcome == "conflict"
    assert "Alpha user." in documents.revision("p", binding.document_id).text
    assert adapter.read_raw(binding, candidate.operation_id) == raw
    current = documents.revision("p", binding.document_id)
    binding = binding.model_copy(
        update={"published_revision": 2, "published_sha256": current.info.sha256, "catalog_seq": 2}
    )
    documents.set_suggestions("p", binding.document_id, on=True, author=USER)
    candidate, capture = request(
        binding, current.text.replace("Beta stays.", "Beta worker.").encode(), op="suggest"
    )
    suggested = adapter.apply(binding, candidate, capture)
    assert suggested.change.outcome == "suggested"
    assert documents.revision("p", binding.document_id).info.number == 2
    accepted = documents.decide(
        "p", binding.document_id, suggested.change.patch_id, accept=True, author=USER
    )
    assert accepted.outcome == "accepted"
    assert documents.revision("p", binding.document_id).info.author.kind == "worker"


@pytest.mark.parametrize("budget_name", ["environment", "binding", "project", "global_"])
def test_each_logical_byte_quota_holds_without_eviction(setup, budget_name) -> None:
    documents, projects, _, binding = setup
    budgets = RawBudgets().model_copy(update={budget_name: Quota(bytes=4, records=10)})
    adapter = WorkerSourceAdapter(documents, projects, budgets=budgets)
    candidate, capture = request(binding, b"abcd", base_revision=None, base_written_sha256=None)
    assert adapter.apply(binding, candidate, capture).status == "recorded"
    other, extra = request(binding, b"e", op="second", base_revision=None, base_written_sha256=None)
    assert adapter.apply(binding, other, extra).status == "held"
    assert adapter.read_raw(binding, "op-1") == b"abcd"
    assert stored(documents) == 1
    assert adapter.apply(binding, candidate, capture).replayed


def test_concurrent_zero_byte_refusals_share_atomic_record_budget(setup) -> None:
    documents, projects, _, binding = setup
    budgets = RawBudgets(global_=Quota(bytes=100, records=1))
    adapters = [
        WorkerSourceAdapter(documents, projects, budgets=budgets),
        WorkerSourceAdapter(DocumentStore(documents.path), projects, budgets=budgets),
    ]

    def apply(index):
        candidate, capture = request(
            binding, b"", op=f"op-{index}", base_revision=None, base_written_sha256=None
        )
        return adapters[index].apply(binding, candidate, capture)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(apply, range(2)))
        assert sorted(item.status for item in outcomes) == ["held", "recorded"]
        assert stored(documents) == 1
    finally:
        adapters[1].documents.close()


def test_actor_replay_collision_and_cross_binding_reads(setup) -> None:
    documents, _, adapter, binding = setup
    candidate, capture = request(binding, b"worker change")
    first = adapter.apply(binding, candidate, capture)
    changed, changed_capture = request(binding, b"different")
    with pytest.raises(DocumentError, match="another payload"):
        adapter.apply(binding, changed, changed_capture)
    with pytest.raises(DocumentError, match="another payload"):
        adapter.apply(binding, candidate.model_copy(update={"operation_id": "new-op"}), capture)
    with pytest.raises(DocumentError, match="another change"):
        documents.patch(
            "p",
            binding.document_id,
            base=2,
            operations=[{"op": "insert", "at": "end", "markdown": "extra"}],
            reason="Collision",
            author=binding.author(candidate.operation_id),
            operation_id=candidate.operation_id,
        )
    for change in ({"worker_binding": "worker-other"}, {"incarnation": "inc-other"}):
        with pytest.raises(DocumentError, match="no retained"):
            adapter.read_raw(binding.model_copy(update=change), candidate.operation_id)
    assert first.change.revision == documents.revision("p", binding.document_id).info.number


def test_transaction_failure_rolls_back_source_raw_event_and_operation(setup, monkeypatch) -> None:
    documents, _, adapter, binding = setup
    candidate, capture = request(binding, TEXT.replace("Alpha stays.", "Alpha worker.").encode())
    original = documents._remember

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic receipt transaction failure")

    monkeypatch.setattr(documents, "_remember", fail)
    before = len(documents.events("p"))
    with pytest.raises(RuntimeError, match="synthetic"):
        adapter.apply(binding, candidate, capture)
    assert documents.revision("p", binding.document_id).info.number == 1
    assert len(documents.events("p")) == before
    assert stored(documents) == 0
    monkeypatch.setattr(documents, "_remember", original)
    assert adapter.apply(binding, candidate, capture).change.revision == 2
    with documents._read() as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM worker_source_receipts")


def test_publication_hook_failure_keeps_raw_source_receipt_and_cursor_atomic(
    setup, monkeypatch
) -> None:
    documents, _, adapter, binding = setup
    candidate, capture = request(binding, TEXT.replace("Alpha stays.", "Alpha worker.").encode())
    exporter = Publications(documents)
    before = exporter.plan("p")
    before_events = len(documents.events("p"))
    original = publication.record_event

    def fail(db, kind, document, event):
        original(db, kind, document, event)
        raise sqlite3.OperationalError("synthetic publication hook failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(publication, "record_event", fail)
        with pytest.raises(sqlite3.OperationalError, match="publication hook failure"):
            adapter.apply(binding, candidate, capture)
    assert exporter.plan("p").snapshot == before.snapshot
    assert documents.revision("p", binding.document_id).info.number == 1
    assert len(documents.events("p")) == before_events
    assert stored(documents) == 0
    receipt = adapter.apply(binding, candidate, capture)
    replay = adapter.apply(binding, candidate, capture)
    assert receipt.change.revision == 2
    assert replay.replayed
    assert stored(documents) == 1
    target = exporter.plan("p", after=before.snapshot.catalog_seq)
    assert target.delta is not None
    assert len(target.delta.changes) == 1
    assert target.delta.changes[0].entry.revision == 2
    assert b"Alpha worker." in exporter.content("p", target.entries[0])
    assert not receipt.safe_to_restore


def test_head_race_is_busy_and_current_suggestion_setting_is_honored(setup, monkeypatch) -> None:
    documents, _, adapter, binding = setup
    candidate, capture = request(binding, TEXT.replace("Alpha stays.", "Alpha worker.").encode())
    original = documents._prepare_source

    def racing(*args, **kwargs):
        result = original(*args, **kwargs)
        documents.patch(
            "p",
            binding.document_id,
            base=1,
            operations=[{"op": "insert", "at": "end", "markdown": "User addition."}],
            reason="Racing user",
            author=USER,
            operation_id="race",
        )
        return result

    monkeypatch.setattr(documents, "_prepare_source", racing)
    with pytest.raises(DocumentError, match="during preparation") as error:
        adapter.apply(binding, candidate, capture)
    assert error.value.kind == "busy"
    assert stored(documents) == 0

    def settings(*args, **kwargs):
        result = original(*args, **kwargs)
        documents.set_suggestions("p", binding.document_id, on=True, author=USER)
        return result

    monkeypatch.setattr(documents, "_prepare_source", settings)
    assert adapter.apply(binding, candidate, capture).change.outcome == "suggested"


def test_worker_positive_authority_and_scope_guards(setup) -> None:
    documents, _, adapter, binding = setup
    author = binding.author("guard")
    for action in (
        lambda: documents.save_text(
            "p", binding.document_id, base=1, text="bad", author=author, reason="denied"
        ),
        lambda: documents.edit_source(
            "p",
            binding.document_id,
            base=1,
            text="bad",
            author=author,
            reason="denied",
            operation_id="denied",
        ),
        lambda: documents.delete("p", binding.document_id, author=author, reason="denied"),
        lambda: documents.create("p", name="bad", title="Bad", text="bad", author=author),
        lambda: documents.set_suggestions("p", binding.document_id, on=False, author=author),
        lambda: documents.promote(
            "p",
            binding.document_id,
            base=1,
            name="bad",
            author=author,
            reason="denied",
            operation_id="denied",
        ),
        lambda: documents.revert(
            "p",
            binding.document_id,
            base=1,
            target=1,
            author=author,
            reason="denied",
            operation_id="denied",
        ),
    ):
        with pytest.raises(DocumentError) as error:
            action()
        assert error.value.kind == "policy_denied"
    candidate, capture = request(binding, b"safe")
    for changes in (
        {"environment_id": "foreign"},
        {"session_id": "dedicated"},
        {"catalog_seq": 2},
        {"document_id": "doc-" + "a" * 24},
    ):
        with pytest.raises(DocumentError):
            adapter.apply(binding.model_copy(update=changes), candidate, capture)
    with pytest.raises(ValidationError):
        Author(
            kind="worker",
            worker_binding="w",
            environment_id="e",
            incarnation="i",
            role="main",
            user="user",
        )
    for bad in (True, -1, 2**100):
        with pytest.raises(DocumentError):
            adapter.read_raw(binding, "op", start=bad)


def test_empty_source_uses_named_engine_without_whole_save_authority(setup) -> None:
    documents, _, adapter, binding = setup
    doc = documents.create("p", name="empty", title="Empty", text="", author=USER).document_id
    bound = binding.model_copy(update={"document_id": doc, "published_sha256": sha(b"")})
    candidate, capture = request(bound, b"")
    result = adapter.apply(bound, candidate, capture)
    assert result.change.outcome == "unchanged"
    assert result.change.revision == 1
    assert result.source_event_seq is None


def test_user_empty_draft_keeps_legacy_save_retry_identity(setup) -> None:
    documents, _, _, _ = setup
    doc = documents.create("p", name="empty", title="Empty", text="", author=USER).document_id
    saved = documents.save_text(
        "p", doc, base=1, text="", author=USER, reason="Empty draft", operation_id="legacy-empty"
    )
    retry = documents.edit_source(
        "p", doc, base=1, text="", author=USER, reason="Empty draft", operation_id="legacy-empty"
    )
    assert retry.replayed
    assert retry.revision == saved.revision == 1


def test_state_roots_and_corrupt_receipt_cannot_cross_authority(setup, tmp_path: Path) -> None:
    documents, _, adapter, binding = setup
    with pytest.raises(DocumentError, match="same state root"):
        WorkerSourceAdapter(documents, ProjectStore(tmp_path / "unrelated"))
    candidate, capture = request(binding, b"new document text")
    receipt = adapter.apply(binding, candidate, capture)
    with documents._write() as db:
        db.execute("DROP TRIGGER worker_source_receipts_immutable_update")
        altered = receipt.model_dump(mode="json")
        altered["source_event_seq"] = 123456
        db.execute("UPDATE worker_source_receipts SET receipt=?", (json.dumps(altered),))
    with pytest.raises(DocumentError, match="identity or digest") as error:
        adapter.apply(binding, candidate, capture)
    assert error.value.kind == "internal"
    assert documents.revision("p", binding.document_id).info.number == 2


def test_unsafe_source_layout_is_retained_without_under_lock_preparation(setup) -> None:
    documents, _, adapter, binding = setup
    # 201 old and new roots exceed the bounded source matching matrix (40,000).
    text = "\n\n".join(f"Paragraph {i} original text." for i in range(201)) + "\n"
    doc = documents.create("p", name="many", title="Many", text=text, author=USER).document_id
    bound = binding.model_copy(update={"document_id": doc, "published_sha256": sha(text.encode())})
    candidate, capture = request(bound, text.replace("original", "changed").encode())
    receipt = adapter.apply(bound, candidate, capture)
    assert receipt.refusal_code == "invalid_source"
    assert receipt.retained_exact
    assert adapter.read_raw(bound, candidate.operation_id) == capture.raw
    assert documents.revision("p", doc).text == text


def test_worker_cannot_comment_or_respond_to_findings(setup) -> None:
    from sandbox_science.orchestrator.consistency.findings import Findings
    from sandbox_science.orchestrator.documents.annotations import Annotations

    documents, _, _, binding = setup
    author = binding.author("cannot-be-main")
    with pytest.raises(DocumentError) as denied:
        Annotations(documents).change(
            "p",
            binding.document_id,
            {
                "action": "create",
                "anchor": {"revision": 1, "block": "b2"},
                "body": "Untrusted code asks for work",
                "operation_id": "comment",
                "addressed_to": "s",
            },
            author,
        )
    assert denied.value.kind == "policy_denied"
    with pytest.raises(DocumentError) as denied:
        Findings(ReviewStore(documents)).respond(
            "p",
            "s",
            {
                "finding": "finding-" + "a" * 24,
                "expected": 1,
                "action": "rebut",
                "explanation": "Untrusted code cannot answer as main",
                "operation_id": "answer",
            },
            author,
        )
    assert denied.value.kind == "policy_denied"


def test_established_session_worker_changes_reach_review_feed_ambiguous_changes_do_not(
    setup,
) -> None:
    documents, _, adapter, binding = setup
    candidate, capture = request(binding, TEXT.replace("Alpha stays.", "Alpha worker.").encode())
    adapter.apply(binding, candidate, capture)
    reviews = ReviewStore(documents)
    reviews.ingest_documents("p", "s")
    assert reviews.run_effects("p", "s", "run-1")
    revision = documents.revision("p", binding.document_id)
    ambiguous = binding.model_copy(
        update={
            "session_id": None,
            "run_id": None,
            "published_revision": 2,
            "published_sha256": revision.info.sha256,
            "catalog_seq": 2,
        }
    )
    candidate, capture = request(
        ambiguous, revision.text.replace("Beta stays.", "Beta code.").encode(), op="ambiguous"
    )
    adapter.apply(ambiguous, candidate, capture)
    reviews.ingest_documents("p", "s")
    with documents._read() as db:
        values = list(db.execute("SELECT locator FROM review_feed"))
    assert len(values) == 1
    assert json.loads(values[0][0])["revision"] == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol": True},
        {"raw_bytes": True},
        {"catalog_seq": 1.0},
        {"raw_sha256": "A" * 64},
        {"base_revision": None},
        {"path": "/untrusted/escape"},
        {"author": {"kind": "user"}},
    ],
)
def test_candidate_is_strict_and_carries_no_author_or_path(setup, changes) -> None:
    _, _, _, binding = setup
    candidate, _ = request(binding, b"text")
    with pytest.raises(ValidationError):
        DirectEditCandidate.model_validate({**candidate.model_dump(), **changes})
