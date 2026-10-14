# SPDX-License-Identifier: MIT
"""Operation receipts (B-04): a retry under an operation ID gets the change's first
result, and another request under it is refused, for documents, comments, findings and
project macros alike; version 12 moves the four kinds' receipts into one table."""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import ControlError, migrations
from sandbox_science.orchestrator.control.errors import CONFLICT
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.documents.project_macros import ProjectMacros

from .control_versions import RECEIPTS, older_store
from .test_comment_math import comment
from .test_consistency import claim, dispatch, raise_finding
from .test_document_math import MAIN, create
from .test_document_math import stores as stores

FIXTURE = Path(__file__).parent / "fixtures" / "control_v9"

REFUSALS = {
    "document": "was used for another change",
    "comment": "already names another annotation change",
    "review": "already names another finding mutation",
    "macros": "already names another macro change",
}

type Change = Callable[[str, int], Any]
"""A change of one kind under an operation ID, as request 0 or another request, 1."""


def kinds(docs: DocumentStore, reviews: ReviewStore, doc: str, review: str) -> dict[str, Change]:
    def document(op: str, variant: int) -> Any:
        return docs.create(
            "p",
            name=f"receipt-{op}",
            title="R",
            text=f"Text {variant}.\n",
            author=USER,
            operation_id=op,
        )

    def note(op: str, variant: int) -> Any:
        return comment(docs, doc, body=f"Note {variant}.", op=op)

    def finding(op: str, variant: int) -> Any:
        return raise_finding(
            reviews, review, operation_id=op, statement=f"The output records {15 + variant}."
        )

    def macros(op: str, variant: int) -> Any:
        return ProjectMacros(docs).change(
            "p", {"expected": 0, "macros": {r"\F": f"v{variant}"}, "operation_id": op}, USER
        )

    return {"document": document, "comment": note, "review": finding, "macros": macros}


def setting(docs: DocumentStore, reviews: ReviewStore) -> tuple[str, str]:
    doc = create(docs, MAIN, "First paragraph.\n")
    claim(reviews)
    return doc, dispatch(reviews)


def plain(result: Any) -> tuple[dict[str, Any], bool]:
    """A result without its replay flag, and the flag."""
    data = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    return {k: v for k, v in data.items() if k != "replayed"}, bool(data.get("replayed"))


@pytest.mark.parametrize("kind", sorted(REFUSALS))
def test_a_retry_gets_the_first_result_and_another_request_is_refused(stores, kind):
    docs, reviews, _ = stores
    change = kinds(docs, reviews, *setting(docs, reviews))[kind]
    first, replayed = plain(change("op-1", 0))
    assert not replayed
    assert plain(change("op-1", 0)) == (first, True)
    with pytest.raises(ControlError, match=REFUSALS[kind]) as refused:
        change("op-1", 1)
    assert refused.value.kind == CONFLICT
    assert plain(change("op-1", 0)) == (first, True)


def test_one_operation_id_names_a_change_of_each_kind(stores):
    """The kinds keep their receipts apart, as their tables did: one ID used for a
    document, a comment, a finding and a macro change makes four changes."""
    docs, reviews, _ = stores
    for change in kinds(docs, reviews, *setting(docs, reviews)).values():
        assert not plain(change("shared", 0))[1]


def test_receipts_from_version_11_replay_and_refuse_as_before(stores, tmp_path):
    docs, reviews, _ = stores
    doc, review = setting(docs, reviews)
    firsts = {
        kind: plain(change("op-1", 0))[0]
        for kind, change in kinds(docs, reviews, doc, review).items()
    }
    old = store_path(tmp_path / "old")
    older_store(docs.path, old, RECEIPTS - 1)
    copied = DocumentStore(old)
    try:
        copied.initialize()  # the writer brings the store to the current version
        changes = kinds(copied, ReviewStore(copied, clock=lambda: 1000.0), doc, review)
        for kind, change in changes.items():
            assert plain(change("op-1", 0)) == (firsts[kind], True), kind
            with pytest.raises(ControlError, match=REFUSALS[kind]):
                change("op-1", 1)
    finally:
        copied.close()


def test_a_receipt_version_12_refuses_leaves_the_store_at_version_11(stores, tmp_path):
    docs, _, _ = stores
    create(docs, USER, "Text.\n")
    old = store_path(tmp_path / "old")
    older_store(docs.path, old, RECEIPTS - 1)
    with sqlite3.connect(old) as db:
        db.execute("INSERT INTO requests VALUES('p','user:user','bad','not-a-digest','{}','t')")
    copied = DocumentStore(old)
    try:
        with pytest.raises(ControlError, match=r"stays at version 11.*requests"):
            copied.initialize()
    finally:
        copied.close()
    with sqlite3.connect(old) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (RECEIPTS - 1,)


_OLD_TABLES = (
    ("document", "SELECT project_id,actor,operation_id,fingerprint,result FROM requests"),
    (
        "comment",
        "SELECT project_id,actor,operation_id,fingerprint,result FROM annotation_operations",
    ),
    ("review", "SELECT project,actor,operation_id,fingerprint,result FROM review_operations"),
    (
        "macros",
        "SELECT project,actor,operation,fingerprint,json_object('revision',revision) "
        "FROM project_macro_operations",
    ),
)


def test_a_version_9_store_from_the_code_before_keeps_its_receipts(tmp_path):
    """The receipts of the version-9 store main wrote (fixtures/control_v9) are the
    receipts of the migrated store, and its first macro change replays."""
    path = store_path(tmp_path)
    with sqlite3.connect(path) as db:
        db.executescript(gzip.decompress((FIXTURE / "control-v9.sql.gz").read_bytes()).decode())
        db.execute(f"PRAGMA application_id = {migrations.APPLICATION_ID}")
        db.execute(f"PRAGMA user_version = {migrations.BASELINE + 7}")
        before = sorted(
            (scope, *row[:4], json.loads(row[4]))
            for scope, query in _OLD_TABLES
            for row in db.execute(query)
        )
    assert {row[0] for row in before} == set(REFUSALS)
    docs = DocumentStore(path)
    try:
        docs.initialize()
        replay = ProjectMacros(docs).change(
            "p",
            {"expected": 0, "macros": {r"\F": "before", r"\G": "g"}, "operation_id": "m0"},
            USER,
        )
        assert (replay["revision"], replay["replayed"]) == (1, True)
    finally:
        docs.close()
    with sqlite3.connect(path) as db:
        after = sorted(
            (*row[:5], json.loads(row[5])) for row in db.execute("SELECT * FROM operation_receipts")
        )
    assert after == before
