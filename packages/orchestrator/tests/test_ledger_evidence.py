# SPDX-License-Identifier: MIT
"""Evidence links (docs/specs/ledger.md, LG-8 to LG-11, LG-13, LG-15): roles, types,
scope and pinned targets; kept across revisions with their IDs; added by delegated
agents; and the citation a published entry needs."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.documents import USER, DocumentError
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.projects import ProjectStore

from .test_ledger import DELEGATED, NEW, change, events
from .test_ledger import ledger as ledger


def source(tmp_path: Path) -> Any:
    """Session s's run r1 publishing in project p."""
    from sandbox_science.contracts import ExecutionContext
    from sandbox_science.contracts.artifact_inventory import ArtifactPublicationSource

    projects = ProjectStore(tmp_path, readonly=True)
    environment = projects.get("p").sessions["s"].environment_id
    projects.close()
    return ArtifactPublicationSource(
        context=ExecutionContext(project_id="p", session_id="s", environment_id=environment),
        run_id="r1",
        tool_call="publish",
        worker_endpoint_sha256="a" * 64,
    )


def publish(ledger: Ledger, tmp_path: Path, data: bytes = b"16\n") -> tuple[str, str]:
    """An artifact revision of project p: its ID and SHA-256."""
    from sandbox_science.contracts import ArtifactRef
    from sandbox_science.orchestrator.artifacts import ArtifactInventory

    publisher = source(tmp_path)
    digest = hashlib.sha256(data).hexdigest()
    worker = ArtifactRef(
        artifact_id="worker-1",
        project_id="p",
        source_path="ff.json",
        media_type="application/json",
        size=len(data),
        sha256=digest,
        created_at=datetime.now(UTC),
        produced_by="unproven-hint",
    )
    inventory = ArtifactInventory(ledger.documents)
    inventory.begin(publisher, "publish-1", "ff.json", "application/json")
    inventory.pin(publisher, "publish-1", "ff.json", "application/json", worker)
    receipt = inventory.finish(publisher, "publish-1", "ff.json", "application/json", worker, data)
    assert receipt.revision is not None
    return receipt.revision.artifact_id, digest


def review(ledger: Ledger) -> str:
    """A review of project p's session s."""
    from sandbox_science.orchestrator.consistency.store import ReviewStore

    from .test_consistency import claim, dispatch

    reviews = ReviewStore(ledger.documents, clock=lambda: 1000.0)
    claim(reviews)
    return dispatch(reviews)


def evidence(entry: str, expected: int, *items: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {"entry": entry, "expected": expected, "evidence": list(items), **more}


def test_evidence_is_pinned_to_each_kind_of_target(ledger: Ledger, tmp_path: Path) -> None:
    documents = ledger.documents
    doc = documents.create(
        "p", name="ff", title="FF", text="# F\n\nWe have $a^{16}=1$.\n", author=USER
    )
    mapped = documents.mapped("p", doc.document_id, 1)
    block, span = mapped.block_ids[1], mapped.span_ids[0]
    artifact, digest = publish(ledger, tmp_path)
    reviewed = review(ledger)
    change(ledger, NEW["claim"], NEW["assumption"])
    assert ledger.entries("p").entries[0].asserted_only is True
    assert ledger.entries("p").entries[1].asserted_only is None
    change(
        ledger,
        evidence(
            "C-1",
            1,
            {
                "role": "derivation",
                "type": "proof",
                "document": "ff.md",
                "revision": 1,
                "block": block,
                "math": span,
            },
            {
                "role": "supports",
                "type": "exact_computation",
                "artifact": artifact,
                "revision": 1,
                "scope": "$p = 17$ only",
            },
            {"role": "supports", "type": "finite_test", "entry": "A-1"},
            {"role": "contradicts", "type": "numerical_experiment", "review": reviewed},
        ),
        op="op-2",
    )
    view = ledger.entry("p", "C-1")
    assert view.asserted_only is False
    assert ledger.entries("p").entries[0].asserted_only is False
    assert [
        (e.role, e.type, e.target, e.revision, e.block, e.span, e.sha256, e.added)
        for e in view.evidence
    ] == [
        ("derivation", "proof", "block", 1, block, span, None, 2),
        ("supports", "exact_computation", "artifact", 1, None, None, digest, 2),
        ("supports", "finite_test", "entry", 1, None, None, None, 2),
        ("contradicts", "numerical_experiment", "review", None, None, None, None, 2),
    ]
    assert view.evidence[0].target_id == doc.document_id
    assert view.evidence[1].scope == "$p = 17$ only"
    assert view.evidence[2].handle == "A-1"
    assert events(ledger)[-1][1]["changed"] == ["evidence"]
    # The first revision shows none; later revisions keep them with their IDs.
    assert ledger.entry("p", "C-1", 1).evidence == []
    change(ledger, {"entry": "C-1", "expected": 2, "title": "Renamed"}, op="op-3")
    assert ledger.entry("p", "C-1").evidence == view.evidence


def test_evidence_that_cannot_be_pinned_is_refused(ledger: Ledger, tmp_path: Path) -> None:
    documents = ledger.documents
    doc = documents.create("p", name="ff", title="FF", text="# F\n\n$x$ and text.\n", author=USER)
    mapped = documents.mapped("p", doc.document_id, 1)
    artifact, _ = publish(ledger, tmp_path)
    change(ledger, NEW["claim"], NEW["issue"], NEW["claim"])
    good = {"role": "supports", "type": "proof", "entry": "C-2"}
    for n, (item, message, kind) in enumerate(
        (
            ({**good, "type": "unclassified"}, "only from imports", "invalid_request"),
            ({**good, "review": "review-1"}, "names one target", "invalid_request"),
            (
                {"role": "supports", "type": "proof", "execution": "exe-1"},
                "execution",
                "invalid_request",
            ),
            ({**good, "role": "independent_check"}, "names the revision", "invalid_request"),
            (
                {**good, "role": "independent_check", "checked": 2},
                "C-1 has no revision 2",
                "not_found",
            ),
            ({**good, "checked": 1}, "only an independent_check", "invalid_request"),
            ({**good, "type": "source_claim"}, "with a reference", "invalid_request"),
            ({**good, "entry": "C-1"}, "not its own evidence", "invalid_request"),
            ({**good, "entry": "C-2", "revision": 5}, "C-2 has no revision 5", "not_found"),
            (
                {**good, "entry": None, "artifact": artifact},
                "names its revision",
                "invalid_request",
            ),
            (
                {**good, "entry": None, "artifact": "nothing", "revision": 1},
                "no artifact",
                "not_found",
            ),
            (
                {**good, "entry": None, "artifact": artifact, "revision": 1, "sha256": "0" * 64},
                "another SHA-256",
                "conflict",
            ),
            (
                {**good, "entry": None, "document": "ff", "revision": 1, "block": "b99"},
                "no block b99",
                "not_found",
            ),
            (
                {
                    **good,
                    "entry": None,
                    "document": "ff",
                    "revision": 1,
                    "block": mapped.block_ids[0],
                    "math": mapped.span_ids[0],
                },
                "no math span",
                "not_found",
            ),
            ({**good, "entry": None, "review": "review-x"}, "no review", "not_found"),
        )
    ):
        with pytest.raises(DocumentError, match=message) as refused:
            change(ledger, evidence("C-1", 1, item), op=f"bad-{n}")
        assert refused.value.kind == kind, (n, refused.value.message)
    with pytest.raises(DocumentError, match="an issue holds no evidence"):
        change(ledger, evidence("I-1", 1, good), op="issue")
    with pytest.raises(DocumentError, match="holds that evidence already"):
        change(ledger, evidence("C-1", 1, good, good), op="twice")
    with pytest.raises(DocumentError, match="holds no evidence evidence-"):
        change(ledger, evidence("C-1", 1, evidence_removed=["evidence-" + "0" * 24]), op="gone")
    assert ledger.entry("p", "C-1").revision == 1


def test_evidence_is_removed_by_its_id(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["claim"])
    item = {"role": "supports", "type": "proof", "entry": "C-2"}
    change(ledger, evidence("C-1", 1, item), op="op-2")
    (held,) = ledger.entry("p", "C-1").evidence
    change(ledger, evidence("C-1", 2, evidence_removed=[held.evidence_id]), op="op-3")
    view = ledger.entry("p", "C-1")
    assert (view.evidence, view.asserted_only) == ([], True)
    assert ledger.entry("p", "C-1", 2).evidence == [held]


def test_a_delegated_agent_adds_evidence_and_changes_nothing_else(ledger: Ledger) -> None:
    """LG-13, LG-F3: a delegated run's check is a revision of the entry it checked."""
    change(ledger, NEW["claim"], NEW["claim"])
    check = {"role": "independent_check", "type": "proof", "entry": "C-2", "checked": 1}
    made = change(ledger, evidence("C-1", 1, check), op="op-2", author=DELEGATED)
    assert made["entries"][0]["revision"] == 2
    (held,) = ledger.entry("p", "C-1").evidence
    assert (held.checked, held.added) == (1, 2)
    assert ledger.history("p", "C-1").revisions[0].author["run_id"] == "r2"
    for n, refused in enumerate(
        (
            evidence("C-1", 2, check | {"checked": 2}, title="Renamed"),
            evidence("C-1", 2, evidence_removed=[held.evidence_id]),
            {"entry": "C-1", "expected": 2, "status": "disputed"},
        )
    ):
        with pytest.raises(DocumentError, match="not_permitted_for_role"):
            change(ledger, refused, op=f"refused-{n}", author=DELEGATED)


def test_a_published_entry_cites_its_reference(ledger: Ledger) -> None:
    """LG-15: a published claim or result cites a reference through a supports link of
    type source_claim, which may be to a reference recorded in the same change."""
    published = {**NEW["claim"], "origin": "published"}
    cite = {"role": "supports", "type": "source_claim"}
    with pytest.raises(DocumentError, match="cites its reference"):
        change(ledger, published)
    with pytest.raises(DocumentError, match="cites its reference"):
        change(
            ledger,
            {**NEW["claim"], "key": "c"},
            {**published, "evidence": [{**cite, "type": "proof", "entry": "new:c"}]},
        )
    made = change(
        ledger,
        {**NEW["reference"], "key": "paper"},
        {**published, "evidence": [{**cite, "entry": "new:paper"}]},
        op="op-2",
    )
    assert [(e["handle"], e["status"]) for e in made["entries"]] == [
        ("REF-1", "active"),
        ("C-1", "recorded"),
    ]
    (held,) = ledger.entry("p", "C-1").evidence
    assert (held.handle, held.revision) == ("REF-1", 1)
    change(ledger, {**published, "evidence": [{**cite, "entry": "REF-1"}]}, op="op-3")
    with pytest.raises(DocumentError, match="cites its reference"):
        change(ledger, evidence("C-1", 1, evidence_removed=[held.evidence_id]), op="op-4")


def test_entries_of_a_version_21_store_hold_no_evidence_after_the_migration(
    ledger: Ledger, tmp_path: Path
) -> None:
    """Version 22 adds ``ledger_evidence``: the entries an older store holds keep their
    revisions, hold no evidence, and take evidence as new entries do."""
    import sqlite3

    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path

    from .control_versions import older_store

    change(ledger, NEW["claim"], NEW["claim"], NEW["task"])
    before = ledger.entry("p", "C-1")
    ledger.documents.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(store_path(tmp_path), store_path(old), migrations.BASELINE + 19)
    raw = sqlite3.connect(store_path(old))
    tables = {name for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert (raw.execute("PRAGMA user_version").fetchone()[0], "ledger_evidence" in tables) == (
        21,
        False,
    )
    raw.close()
    documents = DocumentStore(store_path(old))
    documents.initialize()  # the writer brings the store to the current version
    try:
        migrated = Ledger(documents)
        after = migrated.entry("p", "C-1")
        assert (after.evidence, after.asserted_only) == ([], True)
        assert after.model_dump(exclude={"evidence", "asserted_only"}) == before.model_dump(
            exclude={"evidence", "asserted_only"}
        )
        migrated.change(
            "p",
            {
                "operation_id": "after",
                "reason": "r",
                "changes": [
                    {
                        "entry": "C-1",
                        "expected": 1,
                        "evidence": [{"role": "supports", "type": "proof", "entry": "C-2"}],
                    }
                ],
            },
            USER,
        )
        assert migrated.entry("p", "C-1").asserted_only is False
        with documents.control.read() as tx:
            version = tx.sql.execute("PRAGMA user_version").fetchone()[0]
        assert version == migrations.VERSION
    finally:
        documents.close()


def test_evidence_and_a_pdf_pin_content_the_platform_captured(
    ledger: Ledger, tmp_path: Path
) -> None:
    """A tombstone, or a revision that keeps only the hash the sandbox reported, pins no
    content (RA-31 to RA-33): neither is evidence nor a reference's PDF."""
    from sandbox_science.orchestrator.artifacts import ArtifactInventory

    from .test_ledger import REFERENCE_FIELDS

    artifact, _ = publish(ledger, tmp_path)
    inventory = ArtifactInventory(ledger.documents)
    hashed, _ = inventory.record_metadata(
        source(tmp_path),
        "big.bin",
        sha256="b" * 64,
        size=10**9,
        media_type="application/octet-stream",
        note="too_large",
    )
    deleted = inventory.record_deletion(source(tmp_path), "ff.json")
    assert deleted is not None
    assert deleted.artifact_id == artifact
    change(ledger, NEW["claim"])
    for n, (target, revision, message) in enumerate(
        (
            (hashed.artifact_id, hashed.revision, "only its hash and size"),
            (artifact, deleted.revision, "records a deletion"),
        )
    ):
        item = {"role": "supports", "type": "exact_computation", "artifact": target,
                "revision": revision}  # fmt: skip
        with pytest.raises(DocumentError, match=message) as refused:
            change(ledger, evidence("C-1", 1, item), op=f"evidence-{n}")
        assert refused.value.kind == "invalid_request"
        pdf = {**REFERENCE_FIELDS, "pdf": {"artifact_id": target, "revision": revision}}
        with pytest.raises(DocumentError, match=message):
            change(ledger, {**NEW["reference"], "fields": pdf}, op=f"pdf-{n}")
    # The revision before the deletion still pins its bytes.
    kept = {"role": "supports", "type": "exact_computation", "artifact": artifact, "revision": 1}
    change(ledger, evidence("C-1", 1, kept), op="kept")


def test_an_entrys_scope_notes_are_bounded(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], *([NEW["assumption"]] * 9))
    items = [
        {"role": "supports", "type": "proof", "entry": f"A-{n}", "scope": "s" * 4096}
        for n in range(1, 10)
    ]
    with pytest.raises(DocumentError, match="scope notes exceed 32768 bytes"):
        change(ledger, evidence("C-1", 1, *items), op="long")
    change(ledger, evidence("C-1", 1, *items[:8]), op="fits")
