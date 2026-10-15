# SPDX-License-Identifier: MIT
"""The project ledger's store (docs/specs/ledger.md): entries of the seven types with
their handles, revisions and links (LG-1, LG-2, LG-5), mutations that commit all or
nothing under an operation ID (LG-30, LG-31), their events (LG-33), and who may change
what (LG-13)."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.control import EventLog
from sandbox_science.orchestrator.documents import (
    USER,
    Author,
    DocumentError,
    DocumentStore,
    store_path,
)
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.projects import ProjectStore

MAIN = Author(kind="agent", session_id="s", run_id="r1", role="main", model="scripted")
DELEGATED = Author(kind="agent", session_id="s", run_id="r2", role="delegated")
REFERENCE_FIELDS = {"identifiers": {"eprint": "2024/123"}, "published": "2024-02-01"}

NEW: dict[str, dict[str, Any]] = {
    "issue": {"type": "issue", "title": "Prove it for every prime", "statement": "Is $a^{p-1}=1$?"},
    "claim": {
        "type": "claim",
        "origin": "project",
        "title": "Fermat for 17",
        "statement": "$a^{16} = 1$ for every nonzero $a$ in GF(17).",
        "fields": {"kind": "observation"},
    },
    "result": {
        "type": "result",
        "origin": "project",
        "title": "Key recovery",
        "statement": "Time $2^{105}$.",
        "fields": {"category": "key-recovery", "measures": {"time": 105, "memory": 80}},
    },
    "assumption": {"type": "assumption", "title": "p = 17", "statement": "$p = 17$."},
    "decision": {"type": "decision", "title": "Use Sage", "statement": "Sage for the field."},
    "task": {"type": "task", "title": "Run the check", "statement": "Run it over GF(17)."},
    "reference": {
        "type": "reference",
        "title": "A paper",
        "statement": "A. Author, *A paper*, ePrint 2024/123.",
        "fields": REFERENCE_FIELDS,
    },
}


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.create("q", "minimal", "sha256:" + "a" * 64)
    projects.open_session("q", "t", dedicated=False)
    projects.close()
    documents = DocumentStore(store_path(tmp_path))
    yield Ledger(documents)
    documents.close()


def change(
    ledger: Ledger, *changes: dict[str, Any], op: str = "op-1", author: Author = MAIN
) -> dict[str, Any]:
    return ledger.change(
        "p", {"operation_id": op, "reason": "Recorded.", "changes": list(changes)}, author
    ).model_dump()


def events(ledger: Ledger) -> list[tuple[str, dict[str, Any]]]:
    log = EventLog(ledger.control, "ledger_entry", "test")
    with ledger.control.read() as tx:
        return [(e.kind, e.payload) for e in log.page(tx.sql, "p", 0, 200)]


def test_each_type_gets_its_handle_its_first_status_and_its_event(ledger: Ledger) -> None:
    made = change(ledger, *NEW.values())
    assert [(e["handle"], e["status"], e["revision"]) for e in made["entries"]] == [
        ("I-1", "open", 1),
        ("C-1", "proposed", 1),
        ("R-1", "proposed", 1),
        ("A-1", "active", 1),
        ("D-1", "proposed", 1),
        ("T-1", "todo", 1),
        ("REF-1", "active", 1),
    ]
    reference = ledger.entry("p", "REF-1")
    assert (reference.type, reference.fields) == ("reference", REFERENCE_FIELDS)
    assert reference.statement == NEW["reference"]["statement"]
    assert reference.author["role"] == "main"
    assert [kind for kind, _ in events(ledger)] == ["ledger.entry_created"] * 7
    more = change(ledger, NEW["claim"], NEW["claim"], op="op-2")
    assert [e["handle"] for e in more["entries"]] == ["C-2", "C-3"]


def test_a_statement_is_saved_in_the_dollar_form(ledger: Ledger) -> None:
    change(ledger, {**NEW["assumption"], "statement": r"Let \(p = 17\)."})
    assert ledger.entry("p", "A-1").statement == "Let $p = 17$."


def test_a_revision_names_the_revision_it_expects_and_is_never_rebased(ledger: Ledger) -> None:
    change(ledger, NEW["task"])
    change(ledger, {"entry": "T-1", "expected": 1, "status": "in_progress"}, op="op-2")
    with pytest.raises(DocumentError, match="at revision 2, not 1") as stale:
        change(ledger, {"entry": "T-1", "expected": 1, "title": "Late"}, op="op-3")
    assert (stale.value.kind, stale.value.details["current"]) == ("conflict", 2)
    done = change(ledger, {"entry": "T-1", "expected": 2, "status": "done"}, op="op-4")
    assert done["entries"][0]["revision"] == 3
    history = ledger.history("p", "T-1")
    assert [(r.revision, r.status) for r in history.revisions] == [
        (3, "done"),
        (2, "in_progress"),
        (1, "todo"),
    ]
    assert ledger.entry("p", "T-1", revision=1).status == "todo"
    revised = [p for kind, p in events(ledger) if kind == "ledger.entry_revised"]
    assert [(p["status_before"], p["status"], p["changed"]) for p in revised] == [
        ("todo", "in_progress", ["status"]),
        ("in_progress", "done", ["status"]),
    ]


def test_a_retry_replays_the_first_result_and_a_reused_id_is_refused(ledger: Ledger) -> None:
    first = change(ledger, NEW["issue"])
    again = change(ledger, NEW["issue"])
    assert again == {**first, "replayed": True}
    assert len(ledger.entries("p").entries) == 1
    with pytest.raises(DocumentError, match="names another ledger change"):
        change(ledger, NEW["task"])


def test_a_mutation_commits_all_or_nothing(ledger: Ledger) -> None:
    with pytest.raises(DocumentError, match="a new task starts todo"):
        change(ledger, NEW["issue"], {**NEW["task"], "status": "done"})
    change(ledger, NEW["task"], op="op-2")
    with pytest.raises(DocumentError, match="a issue holds no answers"):
        change(
            ledger,
            {"entry": "T-1", "expected": 1, "status": "done"},
            {**NEW["issue"], "links": [{"kind": "answers", "entry": "T-1"}]},
            op="op-3",
        )
    assert [e.handle for e in ledger.entries("p").entries] == ["T-1"]
    assert ledger.entry("p", "T-1").status == "todo"
    assert [kind for kind, _ in events(ledger)] == ["ledger.entry_created"]


def test_links_are_typed_pinned_and_seen_from_their_target(ledger: Ledger) -> None:
    change(ledger, NEW["issue"], NEW["assumption"])
    made = change(
        ledger,
        {
            **NEW["claim"],
            "key": "c",
            "links": [
                {"kind": "answers", "entry": "I-1"},
                {"kind": "assumes", "entry": "A-1"},
            ],
        },
        {**NEW["result"], "links": [{"kind": "builds_on", "entry": "new:c"}]},
        op="op-2",
    )
    assert [e["handle"] for e in made["entries"]] == ["C-1", "R-1"]
    claim = ledger.entry("p", "C-1")
    assert [(link.kind, link.handle, link.revision) for link in claim.links] == [
        ("answers", "I-1", None),
        ("assumes", "A-1", 1),
    ]
    issue = ledger.entry("p", "I-1")
    assert [(link.kind, link.handle) for link in issue.incoming] == [("answers", "C-1")]
    assert [(link.kind, link.handle) for link in ledger.entry("p", "C-1").incoming] == [
        ("builds_on", "R-1")
    ]
    for n, (bad, message) in enumerate(
        (
            ({"kind": "answers", "entry": "A-1"}, "points to no assumption"),
            ({"kind": "split_into", "entry": "I-1"}, "a claim holds no split_into"),
            ({"kind": "compares_to", "entry": "R-1"}, "states the difference"),
            ({"kind": "builds_on", "entry": "I-1"}, "a builds_on link points to no issue"),
            ({"kind": "answers", "entry": "I-9"}, "no entry I-9"),
            ({"kind": "assumes", "entry": "A-1", "revision": 2}, "A-1 has no revision 2"),
            ({"kind": "concerns", "finding": "finding-" + "0" * 24}, "a claim holds no concerns"),
        )
    ):
        with pytest.raises(DocumentError, match=message):
            change(ledger, {"entry": "C-1", "expected": 1, "links": [bad]}, op=f"bad-{n}")


def test_an_issue_concerns_findings_and_document_blocks_that_exist(ledger: Ledger) -> None:
    documents = ledger.documents
    doc = documents.create("p", name="ff", title="FF", text="# Fields\n\nText.\n", author=USER)
    block = documents.mapped("p", doc.document_id, 1).block_ids[0]
    change(
        ledger,
        {
            **NEW["issue"],
            "links": [
                {"kind": "concerns", "document": doc.document_id, "revision": 1, "block": block}
            ],
        },
    )
    (link,) = ledger.entry("p", "I-1").links
    assert (link.target, link.target_id, link.revision, link.block) == (
        "block",
        doc.document_id,
        1,
        block,
    )
    with pytest.raises(DocumentError, match="has no block"):
        change(
            ledger,
            {
                **NEW["issue"],
                "links": [
                    {"kind": "concerns", "document": doc.document_id, "revision": 1, "block": "b99"}
                ],
            },
            op="op-2",
        )
    with pytest.raises(DocumentError, match="no finding"):
        change(
            ledger,
            {**NEW["issue"], "links": [{"kind": "concerns", "finding": "finding-" + "0" * 24}]},
            op="op-3",
        )


def test_a_supersedes_link_and_its_target_becoming_superseded_commit_together(
    ledger: Ledger,
) -> None:
    change(ledger, NEW["claim"])
    corrected = {**NEW["claim"], "links": [{"kind": "supersedes", "entry": "C-1"}]}
    with pytest.raises(DocumentError, match="becomes superseded in the change"):
        change(ledger, corrected, op="op-2")
    with pytest.raises(DocumentError, match="C-1 is superseded only while an entry supersedes it"):
        change(ledger, {"entry": "C-1", "expected": 1, "status": "superseded"}, op="op-3")
    change(ledger, corrected, {"entry": "C-1", "expected": 1, "status": "superseded"}, op="op-4")
    assert ledger.entry("p", "C-1").status == "superseded"
    assert [(link.kind, link.handle) for link in ledger.entry("p", "C-2").links] == [
        ("supersedes", "C-1")
    ]


def test_statuses_follow_their_type_and_decisions(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["issue"])
    for status, message in (
        ("established", "puts in effect a decision that affects it"),
        ("refuted", "puts in effect a decision that affects it"),
        ("recorded", "only a published entry is recorded"),
        ("done", "a claim is never done"),
    ):
        with pytest.raises(DocumentError, match=message):
            change(ledger, {"entry": "C-1", "expected": 1, "status": status}, op=status)
    with pytest.raises(DocumentError, match="puts in effect a decision that affects it"):
        change(ledger, {"entry": "I-1", "expected": 1, "status": "obstructed"}, op="obstructed")
    with pytest.raises(DocumentError, match="names the issues it is split into"):
        change(ledger, {"entry": "I-1", "expected": 1, "status": "split"}, op="split")
    change(
        ledger,
        {**NEW["issue"], "key": "part"},
        {
            "entry": "I-1",
            "expected": 1,
            "status": "split",
            "links": [{"kind": "split_into", "entry": "new:part"}],
        },
        op="split-2",
    )
    assert ledger.entry("p", "I-1").status == "split"
    change(ledger, {"entry": "C-1", "expected": 1, "status": "disputed"}, op="disputed")
    assert ledger.entry("p", "C-1").status == "disputed"


def test_a_new_entry_starts_in_its_first_status_with_its_fields(ledger: Ledger) -> None:
    for n, (bad, message) in enumerate(
        (
            ({**NEW["task"], "status": "done"}, "a new task starts todo"),
            ({**NEW["claim"], "origin": None}, "claims and results, and only they"),
            ({**NEW["issue"], "origin": "project"}, "claims and results, and only they"),
            ({**NEW["claim"], "origin": "published"}, "cites its reference"),
            ({**NEW["reference"], "fields": {"published": "2024"}}, "names a locator"),
            (
                {**NEW["reference"], "fields": {"identifiers": {"doi": "10.1/x"}}},
                "when it was retrieved",
            ),
            ({**NEW["task"], "fields": {"kind": "lemma"}}, "task fields"),
            ({**NEW["task"], "title": None}, "a title and a statement"),
        )
    ):
        with pytest.raises(DocumentError, match=message):
            change(ledger, bad, op=f"bad-{n}")
    assert ledger.entries("p").entries == []


def test_who_may_change_what(ledger: Ledger) -> None:
    proposed = change(ledger, NEW["claim"], author=DELEGATED)
    assert proposed["entries"][0]["status"] == "proposed"
    with pytest.raises(DocumentError, match="not_permitted_for_role") as refused:
        change(
            ledger,
            {"entry": "C-1", "expected": 1, "status": "disputed"},
            op="op-2",
            author=DELEGATED,
        )
    assert refused.value.kind == "policy_denied"
    reviewer = Author(kind="agent", session_id="s", run_id="r3", role="reviewer")
    worker = Author(
        kind="worker",
        worker_binding="b",
        environment_id="e",
        incarnation="i",
        role="delegated",
        session_id="s",
    )
    for author in (reviewer, worker):
        with pytest.raises(DocumentError, match="not_permitted_for_role"):
            change(ledger, NEW["issue"], op="op-3", author=author)
    stranger = Author(kind="agent", session_id="t", run_id="r4", role="main")
    with pytest.raises(DocumentError, match="not in this project"):
        change(ledger, NEW["issue"], op="op-4", author=stranger)
    change(ledger, {"entry": "C-1", "expected": 1, "status": "disputed"}, op="op-5", author=USER)
    assert ledger.entry("p", "C-1").author == USER.model_dump(exclude_none=True)


def test_another_projects_entries_are_not_found(ledger: Ledger) -> None:
    made = change(ledger, NEW["issue"])
    entry = made["entries"][0]["entry_id"]
    with pytest.raises(DocumentError, match="no entry"):
        ledger.entry("q", entry)
    with pytest.raises(DocumentError, match="no entry"):
        ledger.entry("q", "I-1")


def test_lists_and_histories_page(ledger: Ledger) -> None:
    change(ledger, *(NEW["task"] for _ in range(5)))
    change(ledger, NEW["issue"], op="op-2")
    change(ledger, {"entry": "T-2", "expected": 1, "status": "done"}, op="op-3")
    page = ledger.entries("p", limit=4)
    assert [e.handle for e in page.entries] == ["T-1", "T-2", "T-3", "T-4"]
    rest = ledger.entries("p", after=page.next_after)
    assert ([e.handle for e in rest.entries], rest.next_after) == (["T-5", "I-1"], None)
    assert [e.handle for e in ledger.entries("p", kind="task", status="done").entries] == ["T-2"]
    for n in range(3):
        change(ledger, {"entry": "I-1", "expected": n + 1, "title": f"Title {n}"}, op=f"t{n}")
    history = ledger.history("p", "I-1", limit=2)
    assert [r.revision for r in history.revisions] == [4, 3]
    older = ledger.history("p", "I-1", before=history.next_before)
    assert [(r.revision, r.statement_changed) for r in older.revisions] == [
        (2, False),
        (1, True),
    ]


def test_several_mutations_apply_together_with_their_provenance(ledger: Ledger) -> None:
    from sandbox_science.orchestrator.ledger import Mutation

    lines = [
        Mutation.model_validate(
            {"operation_id": f"line-{n}", "reason": "From a file.", "changes": [NEW["task"]]}
        )
        for n in range(2)
    ]
    source = {"file": "results.jsonl", "sha256": "0" * 64}
    results = ledger.apply("p", [(m, {**source, "line": n}) for n, m in enumerate(lines, 1)], MAIN)
    assert [r.entries[0].handle for r in results] == ["T-1", "T-2"]
    (revision,) = ledger.history("p", "T-2").revisions
    assert revision.provenance == {**source, "line": 2}
    again = ledger.apply("p", [(lines[0], None)], MAIN)
    assert again[0].replayed


# The review of #252.


def test_a_mutations_fingerprint_is_pinned() -> None:
    """A receipt compares a retry by this digest: it leaves out unset fields, so a field
    a later version adds with a None default does not turn a retry into a conflict."""
    from sandbox_science.orchestrator.ledger import Mutation
    from sandbox_science.orchestrator.ledger.store import fingerprint

    mutation = Mutation.model_validate(
        {
            "operation_id": "op-1",
            "reason": "r",
            "changes": [{"type": "task", "title": "Run the check", "statement": "Run it."}],
        }
    )
    canonical = (
        '{"changes":[{"statement":"Run it.","title":"Run the check","type":"task"}],'
        '"operation_id":"op-1","reason":"r"}'
    )
    assert fingerprint(mutation) == hashlib.sha256(canonical.encode()).hexdigest()
    assert fingerprint(mutation) == (
        "6eaba7140bd1064c786138fa769661356ae5924458946dda7da9841ec74bc8fd"
    )


def test_fields_beyond_the_tables_bound_are_refused_and_nothing_is_written(
    ledger: Ledger,
) -> None:
    # 64 authors of 256 characters of four bytes each: more than 64 KiB as UTF-8.
    huge = {
        **NEW["reference"],
        "fields": {**REFERENCE_FIELDS, "authors": ["\U0001d53d" * 256] * 64},
    }
    with pytest.raises(DocumentError, match="exceed 65536 bytes") as refused:
        change(ledger, *([NEW["task"]] * 31), huge)
    assert refused.value.kind == "invalid_request"
    assert ledger.entries("p").entries == []
    assert events(ledger) == []
    # Two-byte characters fit: the bound counts UTF-8, not the six-byte escapes.
    change(ledger, {**huge, "fields": {**huge["fields"], "authors": ["é" * 256] * 64}}, op="ok")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_measure_that_is_not_a_finite_number_is_refused(ledger: Ledger, value: float) -> None:
    result = {**NEW["result"], "fields": {"measures": {"time": value}}}
    with pytest.raises(DocumentError, match="result fields") as refused:
        change(ledger, result)
    assert refused.value.kind == "invalid_request"


def test_a_decision_is_rejected_or_reversed_only_by_another_decision(ledger: Ledger) -> None:
    change(ledger, NEW["decision"])
    change(ledger, {"entry": "D-1", "expected": 1, "status": "in_effect"}, op="op-2")
    for status in ("reversed", "rejected"):
        with pytest.raises(DocumentError, match="reversed"):
            change(ledger, {"entry": "D-1", "expected": 2, "status": status}, op=status)


def test_a_references_pdf_is_an_artifact_revision_of_the_project(
    ledger: Ledger, tmp_path: Path
) -> None:
    from sandbox_science.contracts import ArtifactRef, ExecutionContext
    from sandbox_science.contracts.artifact_inventory import ArtifactPublicationSource
    from sandbox_science.orchestrator.artifacts import ArtifactInventory

    def pdf(artifact: str, revision: int) -> dict[str, Any]:
        fields = {**REFERENCE_FIELDS, "pdf": {"artifact_id": artifact, "revision": revision}}
        return {**NEW["reference"], "fields": fields}

    with pytest.raises(DocumentError, match="no artifact nothing r7") as refused:
        change(ledger, pdf("nothing", 7))
    assert refused.value.kind == "not_found"
    projects = ProjectStore(tmp_path, readonly=True)
    environment = projects.get("p").sessions["s"].environment_id
    projects.close()
    source = ArtifactPublicationSource(
        context=ExecutionContext(project_id="p", session_id="s", environment_id=environment),
        run_id="r1",
        tool_call="publish",
        worker_endpoint_sha256="a" * 64,
    )
    data = b"%PDF-1.4\n"
    worker = ArtifactRef(
        artifact_id="worker-1",
        project_id="p",
        source_path="paper.pdf",
        media_type="application/pdf",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at=datetime.now(UTC),
        produced_by="unproven-hint",
    )
    inventory = ArtifactInventory(ledger.documents)
    inventory.begin(source, "publish-1", "paper.pdf", "application/pdf")
    inventory.pin(source, "publish-1", "paper.pdf", "application/pdf", worker)
    receipt = inventory.finish(source, "publish-1", "paper.pdf", "application/pdf", worker, data)
    assert receipt.revision is not None
    made = pdf(receipt.revision.artifact_id, receipt.revision.revision)
    assert change(ledger, made, op="op-2")["entries"][0]["handle"] == "REF-1"
    with pytest.raises(DocumentError, match="no artifact"):
        change(ledger, pdf(receipt.revision.artifact_id, 2), op="op-3")
    # The link pins the revision's SHA-256, which the read returns.
    digest = hashlib.sha256(data).hexdigest()
    assert ledger.entry("p", "REF-1").fields["pdf"] == {
        "artifact_id": receipt.revision.artifact_id,
        "revision": 1,
        "sha256": digest,
    }
    pinned = pdf(receipt.revision.artifact_id, 1)
    pinned["fields"]["pdf"]["sha256"] = digest
    assert change(ledger, pinned, op="op-4")["entries"][0]["handle"] == "REF-2"
    pinned["fields"]["pdf"]["sha256"] = "0" * 64
    with pytest.raises(DocumentError, match="has another SHA-256") as refused:
        change(ledger, pinned, op="op-5")
    assert (refused.value.kind, refused.value.details) == ("conflict", {"sha256": digest})


def test_a_mutation_reads_the_clock_once(ledger: Ledger) -> None:
    ticks = iter(datetime(2026, 10, 14, 12, n, tzinfo=UTC) for n in range(60))
    timed = Ledger(ledger.documents, clock=lambda: next(ticks))
    timed.change(
        "p", {"operation_id": "op-1", "reason": "r", "changes": [NEW["task"], NEW["issue"]]}, MAIN
    )
    assert {e.updated for e in timed.entries("p").entries} == {"2026-10-14T12:00:00+00:00"}
    log = EventLog(ledger.control, "ledger_entry", "test")
    with ledger.control.read() as tx:
        times = {e.time for e in log.page(tx.sql, "p", 0, 10)}
    assert times == {datetime(2026, 10, 14, 12, 0, tzinfo=UTC)}


def test_a_retry_after_a_restart_replays(tmp_path: Path, ledger: Ledger) -> None:
    first = change(ledger, NEW["claim"], NEW["task"])
    reopened = DocumentStore(store_path(tmp_path))
    try:
        again = change(Ledger(reopened), NEW["claim"], NEW["task"])
        assert again == {**first, "replayed": True}
    finally:
        reopened.close()


def test_concurrent_writers_never_share_a_handle(tmp_path: Path, ledger: Ledger) -> None:
    stores = [DocumentStore(store_path(tmp_path)) for _ in range(4)]
    failures: list[BaseException] = []

    def write(n: int) -> None:
        try:
            for k in range(10):
                Ledger(stores[n]).change(
                    "p",
                    {"operation_id": f"w{n}-{k}", "reason": "r", "changes": [NEW["task"]]},
                    MAIN,
                )
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for store in stores:
        store.close()
    assert not failures, failures
    handles = [e.handle for e in ledger.entries("p", limit=200).entries]
    assert sorted(handles, key=lambda h: int(h[2:])) == [f"T-{n}" for n in range(1, 41)]


# The review of #253.


def superseded(ledger: Ledger) -> None:
    """C-2 supersedes C-1."""
    change(ledger, NEW["claim"], op="s-1")
    change(
        ledger,
        {**NEW["claim"], "links": [{"kind": "supersedes", "entry": "C-1"}]},
        {"entry": "C-1", "expected": 1, "status": "superseded"},
        op="s-2",
    )


def test_supersedes_and_split_into_never_lead_back_to_their_holder(ledger: Ledger) -> None:
    change(ledger, NEW["claim"], NEW["claim"], NEW["issue"], NEW["issue"])
    for kind, first, second, status in (
        ("supersedes", "C-1", "C-2", "superseded"),
        ("split_into", "I-1", "I-2", "split"),
    ):
        with pytest.raises(DocumentError, match="already leads back to"):
            change(
                ledger,
                {
                    "entry": first,
                    "expected": 1,
                    "status": status,
                    "links": [{"kind": kind, "entry": second}],
                },
                {
                    "entry": second,
                    "expected": 1,
                    "status": status,
                    "links": [{"kind": kind, "entry": first}],
                },
                op=f"cycle-{kind}",
            )
    # Across changes: I-1 is split into I-2, which then names I-1 as its part.
    change(
        ledger,
        {
            "entry": "I-1",
            "expected": 1,
            "status": "split",
            "links": [{"kind": "split_into", "entry": "I-2"}],
        },
        op="split",
    )
    with pytest.raises(DocumentError, match="I-2 split_into I-1, which already leads back"):
        change(
            ledger,
            {
                "entry": "I-2",
                "expected": 1,
                "status": "split",
                "links": [{"kind": "split_into", "entry": "I-1"}],
            },
            op="split-back",
        )


def test_a_superseded_entry_supersedes_nothing_new(ledger: Ledger) -> None:
    superseded(ledger)
    with pytest.raises(DocumentError, match="leads back to C-1"):
        change(
            ledger,
            {"entry": "C-1", "expected": 2, "links": [{"kind": "supersedes", "entry": "C-2"}]},
            {"entry": "C-2", "expected": 1, "status": "superseded"},
            op="back",
        )
    change(ledger, NEW["claim"], op="c-3")
    with pytest.raises(DocumentError, match="C-1 is superseded and supersedes nothing new"):
        change(
            ledger,
            {"entry": "C-1", "expected": 2, "links": [{"kind": "supersedes", "entry": "C-3"}]},
            {"entry": "C-3", "expected": 1, "status": "superseded"},
            op="stale",
        )
    # An entry that supersedes another may itself be superseded later.
    change(
        ledger,
        {**NEW["claim"], "links": [{"kind": "supersedes", "entry": "C-2"}]},
        {"entry": "C-2", "expected": 1, "status": "superseded"},
        op="chain",
    )
    assert [ledger.entry("p", h).status for h in ("C-1", "C-2", "C-4")] == [
        "superseded",
        "superseded",
        "proposed",
    ]


def test_superseded_stays_paired_with_its_link_both_ways(ledger: Ledger) -> None:
    superseded(ledger)
    with pytest.raises(DocumentError, match="C-1 stays superseded while C-2 supersedes it"):
        change(ledger, {"entry": "C-1", "expected": 2, "status": "proposed"}, op="back")
    with pytest.raises(DocumentError, match="C-1 is superseded only while an entry supersedes"):
        change(ledger, {"entry": "C-2", "expected": 1, "links": []}, op="drop")
    assert len(events(ledger)) == 3
    # Both together undo the supersession, with a decision: set aside, C-1 comes back
    # only by one.
    undo = (
        {"entry": "C-2", "expected": 1, "links": []},
        {"entry": "C-1", "expected": 2, "status": "proposed"},
    )
    with pytest.raises(DocumentError, match="C-1 leaves superseded only in a change"):
        change(ledger, *undo, op="undo")
    decision = {**NEW["decision"], "status": "in_effect"}
    affects = [{"kind": "affects", "entry": "C-1"}]
    change(ledger, {**decision, "links": affects}, *undo, op="undone", author=USER)
    assert [link.kind for link in ledger.entry("p", "C-1").incoming] == ["affects"]


def test_an_issue_is_split_exactly_while_it_holds_split_into_links(ledger: Ledger) -> None:
    change(ledger, NEW["issue"], NEW["issue"])
    with pytest.raises(DocumentError, match="I-1 holds split_into links only while split"):
        change(
            ledger,
            {"entry": "I-1", "expected": 1, "links": [{"kind": "split_into", "entry": "I-2"}]},
            op="unsplit",
        )
    with pytest.raises(DocumentError, match="I-3 holds split_into links only while split"):
        change(
            ledger,
            {**NEW["issue"], "links": [{"kind": "split_into", "entry": "I-2"}]},
            op="new",
        )
    change(
        ledger,
        {
            "entry": "I-1",
            "expected": 1,
            "status": "split",
            "links": [{"kind": "split_into", "entry": "I-2"}],
        },
        op="split",
    )
    with pytest.raises(DocumentError, match="holds split_into links only while split"):
        change(ledger, {"entry": "I-1", "expected": 2, "status": "open"}, op="reopen")
    with pytest.raises(DocumentError, match="names the issues it is split into"):
        change(ledger, {"entry": "I-1", "expected": 2, "links": []}, op="drop")


def test_assumes_pins_an_assumption_as_the_same_change_leaves_it(ledger: Ledger) -> None:
    """LG-F5, LG-F12: the agent revises A1 and pins C2 to the new revision at once."""
    change(ledger, NEW["assumption"], NEW["claim"])
    made = change(
        ledger,
        {"entry": "A-1", "expected": 1, "statement": "$p = 17$, a prime."},
        {"entry": "C-1", "expected": 1, "links": [{"kind": "assumes", "entry": "A-1"}]},
        op="op-2",
    )
    assert made["entries"][0]["revision"] == 2
    assert [(link.handle, link.revision) for link in ledger.entry("p", "C-1").links] == [("A-1", 2)]
    change(
        ledger,
        {"entry": "A-1", "expected": 2, "statement": "$p = 17$, an odd prime."},
        {
            "entry": "C-1",
            "expected": 2,
            "links": [{"kind": "assumes", "entry": "A-1", "revision": 3}],
        },
        op="op-3",
    )
    assert ledger.entry("p", "C-1").links[0].revision == 3
    pinned = [{"kind": "assumes", "entry": "A-1", "revision": 4}]
    with pytest.raises(DocumentError, match="A-1 has no revision 4"):
        change(ledger, {"entry": "C-1", "expected": 3, "links": pinned}, op="op-4")


def test_a_block_link_keeps_the_documents_id_when_given_its_name(ledger: Ledger) -> None:
    documents = ledger.documents
    doc = documents.create("p", name="ff", title="FF", text="# F\n\nText.\n", author=USER)
    block = documents.mapped("p", doc.document_id, 1).block_ids[0]
    concerns = {"kind": "concerns", "document": "ff.md", "revision": 1, "block": block}
    change(ledger, {**NEW["issue"], "links": [concerns]})
    (link,) = ledger.entry("p", "I-1").links
    assert link.target_id == doc.document_id
    # Another document takes the name; the link still names the first.
    documents.delete("p", doc.document_id, author=USER, reason="gone")
    documents.create("p", name="ff", title="FF 2", text="# Other\n\nOther.\n", author=USER)
    assert ledger.entry("p", "I-1").links[0].target_id == doc.document_id


def test_links_to_another_projects_records_are_not_found(ledger: Ledger) -> None:
    ledger.change("q", {"operation_id": "q-1", "reason": "r", "changes": [NEW["issue"]]}, USER)
    doc = ledger.documents.create("q", name="g", title="G", text="# G\n\nText.\n", author=USER)
    block = ledger.documents.mapped("q", doc.document_id, 1).block_ids[0]
    other = ledger.entry("q", "I-1").entry_id
    for n, (holder, link) in enumerate(
        (
            (NEW["claim"], {"kind": "answers", "entry": other}),
            (
                NEW["issue"],
                {"kind": "concerns", "document": doc.document_id, "revision": 1, "block": block},
            ),
        )
    ):
        with pytest.raises(DocumentError) as refused:
            change(ledger, {**holder, "links": [link]}, op=f"x{n}")
        assert refused.value.kind == "not_found"


def test_issues_build_on_claims_and_claims_compare_to_entries(ledger: Ledger) -> None:
    """LG-35: an idea is an issue that builds on the claims it starts from; a finding is
    a claim or result that compares to the published entry it names."""
    change(ledger, NEW["claim"], NEW["result"])
    change(
        ledger,
        {**NEW["issue"], "links": [{"kind": "builds_on", "entry": "C-1"}]},
        {**NEW["claim"], "links": [{"kind": "compares_to", "entry": "R-1", "note": "Smaller."}]},
        {**NEW["result"], "links": [{"kind": "compares_to", "entry": "C-1", "note": "Tighter."}]},
        op="op-2",
    )
    assert [link.kind for link in ledger.entry("p", "C-1").incoming] == [
        "builds_on",
        "compares_to",
    ]


def test_giving_the_same_links_again_changes_nothing(ledger: Ledger) -> None:
    change(ledger, NEW["issue"], NEW["assumption"])
    links = [{"kind": "answers", "entry": "I-1"}, {"kind": "assumes", "entry": "A-1"}]
    change(ledger, {**NEW["claim"], "links": links}, op="op-2")
    with pytest.raises(DocumentError, match="the change to C-1 changes nothing"):
        change(ledger, {"entry": "C-1", "expected": 1, "links": links}, op="op-3")
    # The same links with a new title change only the title.
    change(ledger, {"entry": "C-1", "expected": 1, "title": "New", "links": links}, op="op-4")
    assert events(ledger)[-1][1]["changed"] == ["title"]
    # The assumption moves on: the same request now pins its new revision.
    change(ledger, {"entry": "A-1", "expected": 1, "title": "p = 17 (prime)"}, op="op-5")
    change(ledger, {"entry": "C-1", "expected": 2, "links": links}, op="op-6")
    assert events(ledger)[-1][1]["changed"] == ["links"]
    assert ledger.entry("p", "C-1").links[1].revision == 2


def test_incoming_links_page_with_their_count(ledger: Ledger) -> None:
    change(ledger, NEW["issue"])
    linked = {**NEW["claim"], "links": [{"kind": "answers", "entry": "I-1"}]}
    for batch in range(8):
        change(ledger, *([linked] * 30), op=f"b{batch}")
    first = ledger.entry("p", "I-1")
    assert (len(first.incoming), first.incoming_total) == (200, 240)
    rest = ledger.entry("p", "I-1", incoming_after=200)
    assert (len(rest.incoming), rest.incoming_total) == (40, 240)
    handles = [link.handle for link in [*first.incoming, *rest.incoming]]
    assert handles == [f"C-{n}" for n in range(1, 241)]
    with pytest.raises(DocumentError, match="count of links"):
        ledger.entry("p", "I-1", incoming_after=-1)


def test_a_finding_link_names_the_session_the_finding_is_in(ledger: Ledger) -> None:
    from sandbox_science.orchestrator.consistency.store import ReviewStore

    from .test_consistency import claim, dispatch, raise_finding

    reviews = ReviewStore(ledger.documents, clock=lambda: 1000.0)
    claim(reviews)
    found = raise_finding(reviews, dispatch(reviews))["finding_id"]
    concerns = {"kind": "concerns", "finding": found}
    change(ledger, {**NEW["issue"], "links": [concerns]})
    (link,) = ledger.entry("p", "I-1").links
    assert (link.target, link.target_id, link.session) == ("finding", found, "s")
    # The session is read from the finding, which the math checks can move to another.
    with ledger.control.write() as tx:
        tx.sql.execute("UPDATE review_findings SET session='s2' WHERE finding_id=?", (found,))
    assert ledger.entry("p", "I-1").links[0].session == "s2"
    with pytest.raises(DocumentError, match="changes nothing"):
        change(ledger, {"entry": "I-1", "expected": 1, "links": [concerns]}, op="op-2")
