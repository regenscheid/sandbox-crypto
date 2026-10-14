# SPDX-License-Identifier: MIT
"""Internal export pins, hostile retained state and atomic replay/quota boundaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tracemalloc
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from sandbox_science.contracts.document_exports import (
    ExportBudgets,
    ExportInputRequest,
    ExportInputSnapshot,
    ExportQuota,
    canonical,
)
from sandbox_science.orchestrator.documents import USER, Author, DocumentError, DocumentStore
from sandbox_science.orchestrator.documents.exports import ExportInputs
from sandbox_science.orchestrator.documents.project_macros import ProjectMacros
from sandbox_science.orchestrator.projects import ProjectStore

MAIN = Author(kind="agent", session_id="s", run_id="run-one", role="main")


@pytest.fixture
def state(tmp_path):
    projects = ProjectStore(tmp_path)
    for project in ("p", "q"):
        projects.create(project, "minimal", "sha256:" + "a" * 64)
    projects.open_session("p", "s", dedicated=False)
    projects.open_session("p", "other", dedicated=False)
    docs = DocumentStore(tmp_path / "control.sqlite3")
    exports = ExportInputs(docs, projects)
    yield docs, exports
    docs.close()


def create(docs, text="A formula $\\unknown{ x }$.", *, project="p", session=None):
    return docs.create(
        project, name="notes", title="Notes", text=text, author=USER, session_id=session
    ).document_id


def request(document, operation="pin", revision=None):
    return {"document_id": document, "operation_id": operation, "revision": revision}


def setting(docs, value, operation):
    api = ProjectMacros(docs)
    api.change(
        "p",
        {"expected": api.get("p")["revision"], "macros": value, "operation_id": operation},
        USER,
    )


def save(docs, document, text):
    return docs.save_text(
        "p",
        document,
        base=docs.document("p", document).head,
        text=text,
        author=USER,
        reason="Fixture edit",
        operation_id=f"save-{docs.document('p', document).head}",
    )


def mutate_revision(docs, column, value):
    assert column in {"block_map", "sha256", "text", "parser"}
    with docs._write() as db:
        db.execute("DROP TRIGGER revisions_no_update")
        db.execute(f"UPDATE revisions SET {column}=?", (value,))


def test_exact_source_map_parser_span_and_macro_pins(state):
    docs, exports = state
    setting(docs, {r"\F": "project", r"\G": "g"}, "setting")
    doc = create(
        docs,
        "---\nmacros:\n  '\\F': 'document'\n---\n\n> $\\unknown{ x }$\n\n$$  y \\notacommand  $$\n",
        session="s",
    )
    made = exports.prepare("p", request(doc), MAIN)
    snapshot = made.snapshot
    assert snapshot.status == "prepared"
    assert (snapshot.source.scope, snapshot.source.session_id) == ("session", "s")
    assert snapshot.source.text == docs.revision("p", doc).text
    assert snapshot.source.sha256 == hashlib.sha256(snapshot.source.text.encode()).hexdigest()
    with docs._read() as db:
        raw_map, raw_parser = db.execute("SELECT block_map,parser FROM revisions").fetchone()
    assert snapshot.source.block_map_sha256 == hashlib.sha256(raw_map.encode()).hexdigest()
    assert snapshot.source.parser_json == raw_parser
    mapped = docs.mapped("p", doc)
    assert [s.tex for s in snapshot.spans] == [s.tex for s in mapped.parsed.spans]
    assert [s.span_id for s in snapshot.spans] == list(mapped.span_ids)
    assert snapshot.macros.definitions == {r"\F": "document", r"\G": "g"}
    assert made.sha256 == hashlib.sha256(canonical(snapshot.model_dump(mode="json"))).hexdigest()
    assert exports.get("p", snapshot.snapshot_id) == made


def test_current_vs_numbered_macros_and_replay_across_changes_restart_runs(state):
    docs, exports = state
    setting(docs, {r"\F": "first"}, "one")
    doc = create(docs, r"$\F$")
    original = exports.prepare("p", request(doc), MAIN)
    setting(docs, {r"\F": "second"}, "two")
    save(docs, doc, r"$\F+1$")
    historical = exports.prepare("p", request(doc, "history", 1), USER)
    current = exports.prepare("p", request(doc, "current"), USER)
    assert historical.snapshot.macros.definitions == {r"\F": "first"}
    assert (current.snapshot.source.revision, current.snapshot.macros.project_revision) == (2, 2)
    docs.close()
    replay = exports.prepare("p", request(doc), MAIN.model_copy(update={"run_id": "run-two"}))
    assert replay == original.model_copy(update={"replayed": True})
    assert replay.snapshot.requested_by.run_id == "run-one"
    with pytest.raises(DocumentError, match="operation ID"):
        exports.prepare("p", request(doc, revision=1), MAIN)


def test_parse_outside_transaction_pins_one_read_snapshot_and_allows_concurrent_save(
    state, monkeypatch
):
    docs, exports = state
    setting(docs, {r"\F": "one"}, "one")
    doc = create(docs, r"$\F$")
    second = DocumentStore(docs.path)
    original = exports._assemble

    def race(*args):
        assert not docs._db.in_transaction
        setting(second, {r"\F": "two"}, "two")
        save(second, doc, r"$\F+2$")
        return original(*args)

    monkeypatch.setattr(exports, "_assemble", race)
    try:
        made = exports.prepare("p", request(doc), USER)
    finally:
        second.close()
    assert made.snapshot.source.revision == 1
    assert made.snapshot.macros.definitions == {r"\F": "one"}
    assert docs.document("p", doc).head == 2


@pytest.mark.parametrize(
    "text",
    [
        "![figure](plot.png)",
        "[web](https://example.invalid)",
        "[artifact](artifact:one)",
        "[reference][r]\n\n[r]: result.json",
        "<img\nsrc='file'>",
        "<img src='file'>",
        r"$\input{secret}$",
        r"$\input0$",
        r"$\openin1=secret$",
        r"$\write18{command}$",
        r"$\^^69nput{secret}$",
        r"$\csname input\endcsname{secret}$",
        r"$\includegraphics{plot}$",
        "```\n[example](file)\n```",
        "[unresolved](project-file:anything)",
    ],
)
def test_unsupported_resource_syntax_is_durably_held_without_dropping_text(state, text):
    docs, exports = state
    doc = create(docs, text)
    made = exports.prepare("p", request(doc), USER)
    assert made.snapshot.status == "held"
    assert "unsupported_resources" in made.snapshot.holds
    assert made.snapshot.source.text == docs.revision("p", doc).text
    assert not made.snapshot.resources_resolved
    assert exports.prepare("p", request(doc), USER).snapshot == made.snapshot


def test_unknown_historical_context_and_invalid_macro_definitions_are_held(state):
    docs, exports = state
    doc = create(docs, "---\nmacros:\n  '\\F': '\\input{file}'\n---\n\n$\\F$")
    made = exports.prepare("p", request(doc), USER)
    assert made.snapshot.status == "held"
    assert "unsupported_resources" in made.snapshot.holds
    with docs._write() as db:
        db.execute("DROP TRIGGER document_macro_pins_delete")
        db.execute("DELETE FROM document_macro_pins")
    old = exports.prepare("p", request(doc, "old", 1), USER)
    assert old.snapshot.holds == ("unknown_historical_macros", "unsupported_resources")
    assert not old.snapshot.macros.known
    assert old.snapshot.macros.project_revision is None


@pytest.mark.parametrize("body", [r"\input0", r"\openin1=file", r"\write18{command}"])
def test_effective_macros_cannot_hide_enumerated_resource_controls(state, body):
    docs, exports = state
    setting(docs, {r"\F": body}, "resource")
    doc = create(docs, r"$\F$")
    made = exports.prepare("p", request(doc), USER)
    assert made.snapshot.macros.definitions == {r"\F": body}
    assert made.snapshot.holds == ("unsupported_resources",)


def test_unsupported_profile_retains_exact_bytes_but_does_not_claim_spans_or_context(state):
    docs, exports = state
    doc = create(docs)
    with docs._read() as db:
        value = json.loads(db.execute("SELECT block_map FROM revisions").fetchone()[0])
    value["profile"] = "future-profile"
    mutate_revision(docs, "block_map", json.dumps(value))
    made = exports.prepare("p", request(doc), USER)
    assert made.snapshot.holds == ("unsupported_profile",)
    assert made.snapshot.source.profile == "future-profile"
    assert made.snapshot.source.block_map_sha256
    assert not made.snapshot.spans_complete
    assert made.snapshot.spans == ()
    assert not made.snapshot.macros.known


def test_profile_one_history_uses_its_saved_parser(state):
    from sandbox_science.orchestrator.documents.blocks import parse
    from sandbox_science.orchestrator.documents.engine import block_map
    from sandbox_science.orchestrator.documents.identity import Allocator, Changes, fresh

    docs, exports = state
    doc = create(docs, "$a$ | $b$\n---|---\n$x$|$y$\n")
    mapped = fresh(parse(docs.revision("p", doc).text, profile="ss-markdown-1"), Allocator(0, 0))
    changes = Changes(
        dict.fromkeys(mapped.block_ids, "added"), dict.fromkeys(mapped.span_ids, "added"), (), ()
    )
    mutate_revision(docs, "block_map", json.dumps(block_map(mapped, changes)))
    made = exports.prepare("p", request(doc, revision=1), USER)
    assert made.snapshot.status == "prepared"
    assert made.snapshot.source.profile == "ss-markdown-1"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("sha256", "b" * 64),
        ("text", "different"),
        ("parser", "[[]]"),
    ],
)
def test_corrupt_retained_source_refuses_without_snapshot(state, column, value):
    docs, exports = state
    doc = create(docs)
    mutate_revision(docs, column, value)
    with pytest.raises(DocumentError):
        exports.prepare("p", request(doc), USER)
    with docs._read() as db:
        assert db.execute("SELECT count(*) FROM document_export_inputs").fetchone() == (0,)


def test_corrupt_math_map_cannot_invent_tex_or_identities(state):
    docs, exports = state
    doc = create(docs)
    with docs._read() as db:
        value = json.loads(db.execute("SELECT block_map FROM revisions").fetchone()[0])
    value["blocks"][0]["spans"][0]["hash"] = "b" * 64
    mutate_revision(docs, "block_map", json.dumps(value))
    with pytest.raises(DocumentError, match="map does not bind"):
        exports.prepare("p", request(doc), USER)


def test_overlarge_map_is_compact_held_before_fetch_or_json_parse(state):
    docs, exports = state
    doc = create(docs)
    mutate_revision(docs, "block_map", "x" * (20 * 1024 * 1024))
    tracemalloc.start()
    try:
        made = exports.prepare("p", request(doc), USER)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024
    assert made.snapshot.holds == ("map_limit",)
    assert made.snapshot.source.block_map_sha256 is None
    assert not made.snapshot.spans_complete


@pytest.mark.parametrize("column", ["payload", "digest", "fingerprint"])
def test_receipt_corruption_is_bounded_and_cannot_replace_canonical_pins(state, column):
    docs, exports = state
    doc = create(docs)
    exports.prepare("p", request(doc), USER)
    with docs._write() as db:
        db.execute("DROP TRIGGER document_export_inputs_no_update")
        db.execute("PRAGMA ignore_check_constraints=ON")
        value = b"x" * (20 * 1024 * 1024) if column == "payload" else "x" * (20 * 1024 * 1024)
        db.execute(f"UPDATE document_export_inputs SET {column}=?", (value,))
        db.execute("PRAGMA ignore_check_constraints=OFF")
    tracemalloc.start()
    try:
        with pytest.raises(DocumentError):
            exports.prepare("p", request(doc), USER)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024


@pytest.mark.parametrize(
    "forgery", ["tex", "macros", "profile", "held", "setting", "scope", "requester"]
)
def test_coherent_projection_and_digest_forgery_cannot_replace_original_inputs(state, forgery):
    docs, exports = state
    setting(docs, {r"\F": "original"}, "one")
    doc = create(docs, r"$\F$" + (" [link](file)" if forgery == "held" else ""))
    made = exports.prepare("p", request(doc), MAIN)
    value = made.snapshot.model_dump(mode="json")
    if forgery == "tex":
        value["spans"][0]["tex"] = "forged"
        value["spans"][0]["sha256"] = hashlib.sha256(b"forged").hexdigest()
    elif forgery == "macros":
        value["macros"]["definitions"] = {r"\F": "forged"}
        value["macros"]["sha256"] = hashlib.sha256(canonical({r"\F": "forged"})).hexdigest()
    elif forgery == "profile":
        value["source"]["profile"] = "ss-markdown-1"
    elif forgery == "held":
        value["status"], value["holds"] = "prepared", []
    elif forgery == "setting":
        setting(docs, {r"\F": "later"}, "two")
        later = ProjectMacros(docs).get("p")
        value["macros"].update(
            project_revision=2,
            project_sha256=later["sha256"],
            definitions=later["macros"],
            sha256=later["sha256"],
        )
    elif forgery == "scope":
        value["source"].update(scope="session", session_id="other")
    else:
        value["requested_by"]["run_id"] = "another-run"
    # Self-consistent serialized receipts must still bind independent original pins.
    forged = ExportInputSnapshot.model_validate(value)
    with docs._write() as db:
        db.execute("DROP TRIGGER document_export_inputs_no_update")
        db.execute(
            "UPDATE document_export_inputs SET payload=?,digest=?",
            (canonical(value), forged.sha256),
        )
    with pytest.raises(DocumentError, match=r"canonical|selection header"):
        exports.get("p", made.snapshot.snapshot_id)
    with pytest.raises(DocumentError, match=r"canonical|selection header"):
        exports.prepare("p", request(doc), MAIN)


def test_atomic_receipt_fault_rolls_back_and_does_not_veto_source_edits(state):
    docs, exports = state
    doc = create(docs)
    with docs._write() as db:
        db.execute(
            "CREATE TRIGGER fail_export BEFORE INSERT ON document_export_inputs "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        exports.prepare("p", request(doc), USER)
    assert save(docs, doc, "$x$").revision == 2
    with docs._write() as db:
        assert db.execute("SELECT count(*) FROM document_export_inputs").fetchone() == (0,)
        db.execute("DROP TRIGGER fail_export")
    assert exports.prepare("p", request(doc), USER).snapshot.source.revision == 2


def test_exact_utf8_count_and_global_quotas_replay_has_no_new_reservation(state):
    docs, exports = state
    doc = create(docs, "é")
    first = exports.prepare("p", request(doc), USER)
    with docs._read() as db:
        row = db.execute("SELECT * FROM document_export_inputs").fetchone()
    # Logical data bytes include every stored column, with integers as decimal UTF-8.
    actual = sum(len(v if isinstance(v, bytes) else str(v).encode()) for v in row if v is not None)
    limited = ExportInputs(
        docs,
        exports.projects,
        budgets=ExportBudgets(
            project=ExportQuota(bytes=actual, records=1),
            global_=ExportQuota(bytes=actual, records=1),
        ),
    )
    assert limited.prepare("p", request(doc), USER).snapshot == first.snapshot
    with pytest.raises(DocumentError, match="quota"):
        limited.prepare("p", request(doc, "new"), USER)
    assert save(docs, doc, "ordinary edit").revision == 2
    qdoc = create(docs, "q", project="q")
    with pytest.raises(DocumentError, match="quota"):
        limited.prepare("q", request(qdoc), USER)


def test_concurrent_same_operation_and_last_quota_slot_are_atomic(state):
    docs, exports = state
    doc = create(docs)
    other = DocumentStore(docs.path)
    budgets = ExportBudgets(project=ExportQuota(bytes=1024 * 1024, records=1))
    apis = [
        ExportInputs(docs, exports.projects, budgets=budgets),
        ExportInputs(other, exports.projects, budgets=budgets),
    ]
    try:
        with ThreadPoolExecutor(2) as pool:
            values = list(pool.map(lambda api: api.prepare("p", request(doc), USER), apis))
        assert values[0].snapshot == values[1].snapshot
        assert {v.replayed for v in values} == {False, True}
        with pytest.raises(DocumentError, match="quota"):
            apis[1].prepare("p", request(doc, "different"), USER)
    finally:
        other.close()


def test_byte_quota_counts_utf8_not_codepoints_and_final_manifest_limit(state, monkeypatch):
    from sandbox_science.orchestrator.documents import exports as module

    docs, exports = state
    doc = create(docs, "é" * 1000)
    tiny = ExportInputs(
        docs, exports.projects, budgets=ExportBudgets(project=ExportQuota(bytes=2200, records=100))
    )
    with pytest.raises(DocumentError, match="quota"):
        tiny.prepare("p", request(doc), USER)
    with docs._read() as db:
        assert db.execute("SELECT count(*) FROM document_export_inputs").fetchone() == (0,)
    monkeypatch.setattr(module, "MAX_SNAPSHOT_BYTES", 1024)
    with pytest.raises(DocumentError, match="logical limit"):
        exports.prepare("p", request(doc), USER)
    assert save(docs, doc, "Still editable").revision == 2


def test_authority_scope_readonly_and_legacy_read_compatibility(state):
    docs, exports = state
    doc = create(docs)
    for author in (
        Author(kind="platform", service="export"),
        Author(kind="import", user="user", import_id="import"),
        MAIN.model_copy(update={"role": "reviewer"}),
        MAIN.model_copy(update={"session_id": "foreign"}),
        Author(
            kind="worker",
            role="delegated",
            worker_binding="w",
            environment_id="env",
            incarnation="i",
        ),
    ):
        with pytest.raises(DocumentError):
            exports.prepare("p", request(doc), author)
    with pytest.raises(DocumentError, match="unavailable"):
        exports.prepare("q", request(doc), USER)
    made = exports.prepare("p", request(doc), USER)
    with pytest.raises(DocumentError, match="unavailable"):
        exports.get("q", made.snapshot.snapshot_id)
    readonly = DocumentStore(docs.path, readonly=True)
    try:
        api = ExportInputs(readonly, exports.projects)
        assert api.get("p", made.snapshot.snapshot_id) == made
        with pytest.raises(DocumentError, match="readonly"):
            api.prepare("p", request(doc), USER)
    finally:
        readonly.close()
    with docs._write() as db:
        db.execute("DROP TABLE document_export_inputs")
    with pytest.raises(DocumentError, match="unavailable"):
        exports.get("p", made.snapshot.snapshot_id)


def test_user_identity_byte_bound_preserves_unicode_replay(state):
    docs, exports = state
    doc = create(docs)
    author = Author(kind="user", user="é" * 64)
    made = exports.prepare("p", request(doc), author)
    assert exports.get("p", made.snapshot.snapshot_id) == made
    assert exports.prepare("p", request(doc), author).replayed
    with pytest.raises(DocumentError, match="requester scope"):
        exports.prepare("p", request(doc, "oversized-user"), Author(kind="user", user="é" * 65))
    with docs._read() as db:
        assert db.execute("SELECT count(*) FROM document_export_inputs").fetchone() == (1,)


@pytest.mark.parametrize("revision", [0, -1, True, 1.0, "1", {}, []])
def test_strict_request_revisions(revision):
    with pytest.raises(ValidationError):
        ExportInputRequest.model_validate(request("doc-" + "a" * 24, revision=revision))


def test_empty_source_and_snapshot_model_completeness(state):
    docs, exports = state
    doc = create(docs, "")
    made = exports.prepare("p", request(doc), USER)
    assert made.snapshot.status == "prepared"
    assert made.snapshot.spans == ()
    data = made.snapshot.model_dump(mode="json")
    data["spans_complete"] = False
    with pytest.raises(ValidationError):
        ExportInputSnapshot.model_validate(data)
