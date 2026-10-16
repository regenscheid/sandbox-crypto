# SPDX-License-Identifier: MIT
"""The consistency review by the session's archive (docs/specs/consistency-review.md,
CR-2 and CR-14 as the context management spec amends them; its build step (g)).

Runs go through ``cli.main`` against the in-process worker and the scripted gateway: a
review's transcript items name the archived messages they copy, its dispatch names the
positions its snapshot covers, the actions of earlier runs' calls a run's view held are
its review's context, and a delegated run's actions and executions name the tool results
they came back as."""

from __future__ import annotations

import json
from typing import Any, cast

from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.history import Archive
from sandbox_science.orchestrator.model import Message
from sandbox_science.orchestrator.projects import RunRecord, run_name

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, REPORT, children, wait
from .test_delegation import delegating as delegating

Json = dict[str, Any]


def inconclusive(messages: list[Message]) -> Reply:
    """A reviewer that reads the manifest and ends inconclusive."""
    if messages[-1]["role"] == "user":
        return Reply(calls=[call("review_manifest")])
    return Reply(json.dumps({"outcome": "inconclusive", "summary": "Read.", "cap_reached": False}))


def reviews_of(reviews: ReviewStore, run: str) -> list[tuple[str, list[Json]]]:
    """Each review of ``run``'s work, newest first, with its items."""
    return [
        (
            str(review["review_id"]),
            cast(
                list[Json],
                reviews.manifest(PROJECT, SESSION, review["review_id"], limit=50)["evidence"],
            ),
        )
        for review in reviews.listing(PROJECT, SESSION)["reviews"]
        if review["author_run"] == run
    ]


def actions(reviews: ReviewStore, review: str, items: list[Json]) -> Json:
    (listed,) = [i for i in items if i["kind"] == "actions"]
    return json.loads(reviews.read_evidence(PROJECT, SESSION, review, listed["evidence"])["text"])


def name(record: RunRecord) -> str:
    return run_name(record.started_at, record.run_id)


async def test_a_review_names_archive_places_and_earlier_runs_actions(harness: Harness) -> None:
    """CR-14: each transcript item names the archived message it copies, the model's
    answers among them, and the dispatch names the positions its snapshot covers. CR-2:
    a later run that tells what an earlier run did is reviewed with the earlier run's
    calls its view held, which lab 41's reviews flagged as unrecorded."""
    harness.configure_reviews(ReviewConfig())
    harness.gateway.reviewer = inconclusive
    first = await harness.run(
        "run-1", [Reply(calls=[call("python", code="x = 41")]), Reply("Done: x is set.")],
        reviewer=True,
    )  # fmt: skip
    second = await harness.run("run-2", [Reply("Done: the first run set x to 41.")], reviewer=True)
    documents = harness.documents()
    try:
        reviews = ReviewStore(documents)
        archive = Archive(documents.control)
        rows = {r.message_id: r for r in archive.messages(PROJECT, SESSION, run=name(second))}
        (review, items), *_ = reviews_of(reviews, "run-2")
        placed = [cast(Json, i["locator"]["archive"]) for i in items if i["kind"] == "transcript"]
        assert placed
        assert all(rows[found["id"]].position == found["position"] for found in placed)
        answer = next(row for row in rows.values() if row.entry.kind == "assistant")
        assert {"id": answer.message_id, "position": answer.position} in placed
        with documents.control.read() as tx:
            (dispatched, *_) = reviews.review_log.recent(
                tx.sql, PROJECT, SESSION, "review.dispatched", 10
            )
        positions = [found["position"] for found in placed]
        assert (dispatched.payload["review"], dispatched.payload["archive"]) == (
            review, {"from": min(positions), "to": max(positions)}
        )  # fmt: skip
        held = actions(reviews, review, items)["earlier_runs"]
        assert [(a["run"], a["tool"], a["outcome"]) for a in held["actions"]] == [
            (first.run_id, "python", "ok")
        ]
        # The first run had no earlier run: its reviews list none.
        for earlier, earlier_items in reviews_of(reviews, "run-1"):
            if any(i["kind"] == "actions" for i in earlier_items):
                assert "earlier_runs" not in actions(reviews, earlier, earlier_items)
    finally:
        documents.close()


async def test_a_delegated_runs_work_names_the_results_it_came_back_as(
    delegating: Harness,
) -> None:
    """CR-14 for delegated work, still read from the delegated run's record: each action
    and execution names the archived tool result its call came back as."""
    delegating.configure_reviews(ReviewConfig())
    delegating.gateway.reviewer = inconclusive
    root = await delegating.run(
        "root",
        [Reply(calls=[CHECK]), wait(), Reply("Done: C-1 holds.")],
        delegated=[Reply(calls=[call("python", code="print(2**16 % 17)")]), Reply(REPORT)],
        reviewer=True,
    )
    (child,) = children(delegating, root)
    documents = delegating.documents()
    try:
        reviews = ReviewStore(documents)
        (result,) = [
            row
            for row in Archive(documents.control).messages(PROJECT, SESSION, run=name(child))
            if row.entry.kind == "tool_result"
        ]
        place = {"id": result.message_id, "position": result.position}
        found = [
            (i["kind"], i["locator"].get("archive"))
            for _, items in reviews_of(reviews, "root")
            for i in items
            if i["kind"] == "execution" and i["locator"].get("delegation")
        ]
        assert found
        assert all(at == place for _, at in found)
        held = [
            a
            for review, items in reviews_of(reviews, "root")
            if any(i["kind"] == "actions" for i in items)
            for a in actions(reviews, review, items)["actions"]
            if a.get("delegation")
        ]
        assert [a["tool"] for a in held] == ["python"]
    finally:
        documents.close()
