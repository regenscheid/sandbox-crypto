# SPDX-License-Identifier: MIT
"""Golden snapshots of ``ReviewStore.dispatch`` (B-06): for author work, a confirmation
that fits and one that does not, the snapshot's items, grants, coverage flag and
confirmation key, with identifiers made deterministic. The digests were taken from the
code before B-06 split ``dispatch``; a change to any of them changes what a reviewer
sees."""

from __future__ import annotations

import hashlib
import itertools
import json
import secrets
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency import store as storage
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER

from .test_consistency import MAIN, anchor, claim, dispatch, raise_finding
from .test_consistency import store as store

GOLDEN = {
    "author": "173643b672a77de411ba358dc0b3fc8c14227d6cf4bff3cfd48ce6ceaedecd37",
    "confirmation": "b31c094ad11cdfef8a53dbb66d9ebfc69691328a39d86305a75dd37214f54f08",
    "fallback": "c8f000e56c7ff7bc15e4f10552ecf5d29d8839dd83562a5cd2050a87c9f2c7ed",
}


@pytest.fixture(autouse=True)
def deterministic_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = itertools.count(1)
    monkeypatch.setattr(secrets, "token_hex", lambda n: f"{next(counter):0{2 * n}x}")


def snapshot(reviews: ReviewStore, review: str) -> Any:
    """The review's run record and every item and grant it pins, without times."""
    run = reviews.run("p", "s", review)
    for key in ("created", "ended", "lease_until"):
        run.pop(key, None)
    items = []
    after = 0
    while True:
        page = reviews.manifest("p", "s", review, after=after, limit=20)
        items.extend(
            {**item, "payload": reviews.evidence("p", "s", review, item["evidence"])["payload"]}
            for item in page["evidence"]
        )
        if page["next_after"] is None:
            break
        after = page["next_after"]
    return {"run": run, "items": items}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fixed_finding(reviews: ReviewStore, documents: Any, **overrides: Any) -> tuple[str, int]:
    """A finding claimed fixed with a changed document revision and response evidence."""
    claim(reviews)
    original = dispatch(reviews)
    raised = raise_finding(reviews, original, **overrides)
    reviews.finish("p", "s", original, "owner", "findings", "The count is wrong.")
    doc = documents.create(
        "p", name="fix", title="N", text="There are 15.\n", author=USER, session_id="s"
    ).document_id
    response = Findings(reviews).respond(
        "p",
        "s",
        {
            "finding": raised["finding_id"],
            "expected": 1,
            "action": "fix",
            "explanation": "Corrected the count in the document.",
            "revisions": [[doc, 1]],
            "operation_id": "fix",
        },
        MAIN,
    )
    return raised["finding_id"], response["revision"]


def test_author_work_snapshot(store) -> None:
    reviews, documents, _ = store
    claim(reviews)
    documents.create(
        "p", name="notes", title="N", text="There are 16.\n", author=MAIN, session_id="s"
    )
    reviews.ingest_documents("p", "s")
    first = dispatch(reviews)
    raise_finding(reviews, first, anchors=[anchor(reviews, first, "document")])
    reviews.finish("p", "s", first, "owner", "findings", "One finding.")
    claim(reviews, "The final count is 16.", "final")
    second = dispatch(reviews)
    found = snapshot(reviews, second)
    assert found["run"]["kind"] == "terminal"
    assert found["run"]["coverage_complete"]
    assert fingerprint(found) == GOLDEN["author"], fingerprint(found)


def test_a_confirmation_that_fits(store) -> None:
    reviews, documents, _ = store
    fixed_finding(reviews, documents)
    confirmation = dispatch(reviews, "confirmation")
    found = snapshot(reviews, confirmation)
    assert found["run"]["kind"] == "confirmation"
    assert found["run"]["confirmation_key"] is not None
    assert fingerprint(found) == GOLDEN["confirmation"], fingerprint(found)


def test_a_confirmation_that_does_not_fit_falls_back_to_terminal(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews, documents, _ = store
    fixed_finding(reviews, documents, statement="The count is wrong. " * 100)
    claim(reviews, "The final author answer.", "final-answer")
    monkeypatch.setattr(storage, "MAX_ITEM_BYTES", 512)
    fallback = dispatch(reviews, "confirmation")
    found = snapshot(reviews, fallback)
    assert found["run"]["kind"] == "terminal"
    assert not found["run"]["coverage_complete"]
    assert found["run"]["confirmation_key"] is None
    assert fingerprint(found) == GOLDEN["fallback"], fingerprint(found)
