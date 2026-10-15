# SPDX-License-Identifier: MIT
"""Golden snapshots of ``ReviewStore.dispatch`` (B-06): for author work, a confirmation
that fits and one that does not, the snapshot's items, grants, coverage flag and
confirmation key, with identifiers made deterministic. The digests were taken from the
code before B-06 split ``dispatch``, and again for each rubric since, which is all that
changed: ``sandbox-consistency-2`` asked the reviewer to compare an entry's numbers with the
artifacts it pins (B6), ``sandbox-consistency-3`` to check quotes (CR-4),
``sandbox-consistency-4`` to find the record of each action the author narrates, with
the window's actions item and each document revision's changed blocks; a change to any
of them changes what a reviewer sees."""

from __future__ import annotations

import hashlib
import itertools
import json
import secrets
from typing import Any

import pytest

from sandbox_science.orchestrator.consistency import store as storage
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER

from .test_consistency import MAIN, anchor, claim, dispatch, raise_finding
from .test_consistency import store as store

GOLDEN = {
    "author": "77eaf38c45099e0215484606e99d2dcdca7b0d7f2dc6e0bcc2bfc2fdc90db08b",
    "confirmation": "18c8a755a8aaee94dc420265facc3ec3225987fa09db6b8d5b27a5611b980461",
    "fallback": "b3e5f71f4eddd05582b1199e0f73f913b668c7c97af47bb2a050eb733626f3cd",
}


@pytest.fixture(autouse=True)
def limits_the_digests_were_taken_with(store: Any) -> None:
    """The review limits before lab 38 raised the defaults, which the run records hold:
    what the snapshots pin is unchanged."""
    reviews, _, _ = store
    reviews.defaults = ReviewConfig(
        max_turns=12,
        time_seconds=120,
        max_input_chars=150000,
        token_budget=64000,
        calls_per_hour=60,
    )


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


@pytest.mark.parametrize("change", ["disable", "reconfigure", "none"])
def test_a_fallback_reads_the_configuration_again(
    store, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """When a confirmation falls back to author work, it plans that work with the
    configuration as it is then: disabled meanwhile, it dispatches nothing; reconfigured,
    the review records the new configuration."""
    reviews, documents, _ = store
    fixed_finding(reviews, documents, statement="The count is wrong. " * 100)
    claim(reviews, "The final author answer.", "final-answer")
    monkeypatch.setattr(storage, "MAX_ITEM_BYTES", 512)
    revision = type(documents).revision
    calls = [0]

    def changing(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] == 1:  # while the confirmation is planned
            if change == "disable":
                reviews.configure("p", "s", ReviewConfig(enabled=False))
            elif change == "reconfigure":
                reviews.configure("p", "s", ReviewConfig(min_interval_seconds=7))
        return revision(self, *args, **kwargs)

    monkeypatch.setattr(type(documents), "revision", changing)
    review = reviews.dispatch(
        "p", "s", "confirmation", author_run="author-1", owner="owner", alias="reviewer"
    )
    if change == "disable":
        assert review is None
        return
    assert review is not None
    run = reviews.run("p", "s", review)
    assert run["kind"] == "terminal"
    assert run["config_revision"] == reviews.config("p")[1]
    assert run["config"]["min_interval_seconds"] == (7 if change == "reconfigure" else 120)
