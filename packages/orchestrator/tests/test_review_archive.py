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
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.consistency.archived import held_calls, places, results
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.rubric import RUBRIC
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.model import Message
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord, run_name

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_consistency import store as store
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


def actions(
    reviews: ReviewStore,
    review: str,
    items: list[Json],
    *,
    scope: tuple[str, str] = (PROJECT, SESSION),
) -> Json:
    (listed,) = [i for i in items if i["kind"] == "actions"]
    return json.loads(reviews.read_evidence(*scope, review, listed["evidence"])["text"])


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
        rows = {r.message_id: r for r in archive.messages(PROJECT, SESSION)}
        (review, items), *_ = reviews_of(reviews, "run-2")
        placed = [cast(Json, i["locator"]["archive"]) for i in items if i["kind"] == "transcript"]
        assert placed
        assert all(rows[found["id"]].position == found["position"] for found in placed)
        answer = next(
            row
            for row in archive.messages(PROJECT, SESSION, run=name(second))
            if row.entry.kind == "assistant"
        )
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


RUN = "20261016T030000000000Z-run-1"
"""A run's name in the archive, as a record keeps it."""


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    """An archive whose project has the session and another, with nothing in them yet."""
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    projects.open_session(PROJECT, "other", dedicated=False)
    yield Archive(projects.control)
    projects.close()


def asked(*calls: tuple[str, str]) -> Json:
    """An answer calling ``sage`` once for each (call ID, arguments)."""
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": i, "type": "function", "function": {"name": "sage", "arguments": a}}
        for i, a in calls
    ]}  # fmt: skip


def kept(archive: Archive, entry: Entry, *, session: str = SESSION, run: str = RUN) -> Json:
    """``entry`` archived; the message as it was sent."""
    return archive.append(PROJECT, session, run, entry).chat()


def answer(archive: Archive, message: Json) -> None:
    calls = tuple(cast(list[Json], message.get("tool_calls") or []))
    entry = Entry("assistant", "assistant", message["content"], tool_calls=calls)
    archive.append(PROJECT, SESSION, RUN, entry)


def result(archive: Archive, call_id: str, text: str, **where: str) -> Json:
    return kept(
        archive, Entry("tool", "tool_result", text, source="sage", call_id=call_id), **where
    )


def test_an_answer_that_is_not_the_archived_one_stops_the_pairing(archive: Archive) -> None:
    """The review of #462: answers have no header and are placed by their order, so each
    is checked against the archived answer it is paired with; from the first that differs,
    no answer is placed, a shift giving no place rather than a wrong one. A message with
    no header has no place, and the run's own state block, archived first as part of its
    view, shifts nothing."""
    block = kept(archive, Entry("user", "state_block", "What came before."))
    task = kept(archive, Entry("user", "task", "Factor n."))
    first = asked(("c1", "{}"))
    answer(archive, first)
    returned = result(archive, "c1", "7031")
    answer(archive, {"role": "assistant", "content": "It ends in 7031."})
    answer(archive, {"role": "assistant", "content": "Done."})
    rows = archive.messages(PROJECT, SESSION, run=RUN)
    position = {row.entry.kind + str(n): row.position for n, row in enumerate(rows)}
    system = {"role": "system", "content": "You are a research assistant."}
    unheaded = {"role": "user", "content": "A note with no header."}
    transcript = [system, block, task, first, returned, unheaded,
                  {"role": "assistant", "content": "It ends in 7033."},
                  {"role": "assistant", "content": "Done."}]  # fmt: skip
    found = places(archive, PROJECT, SESSION, RUN, transcript, 2, 0)
    assert sorted(found) == [2, 3, 4]  # the task, the first answer, its result
    assert [found[i]["position"] for i in (2, 3, 4)] == [
        position["task1"], position["assistant2"], position["tool_result3"]
    ]  # fmt: skip
    assert position["state_block0"] not in {f["position"] for f in found.values()}
    shifted = [system, block, task, asked(("c9", "{}")), returned]
    assert sorted(places(archive, PROJECT, SESSION, RUN, shifted, 2, 0)) == [2, 4]


def test_a_shift_among_calls_of_one_tool_gives_no_place(archive: Archive) -> None:
    """The review of #462: a delegated run's records and archived results are paired by
    order while they agree on the tool and on its arguments, so two ``sage`` calls taken
    in the wrong order give no place."""
    answer(archive, asked(("c1", '{"code": "a"}'), ("c2", '{"code": "b"}')))
    result(archive, "c1", "1")
    result(archive, "c2", "2")
    record = {"tool": "sage", "arguments": '{"code": "a"}'}
    other = {"tool": "sage", "arguments": '{"code": "b"}'}
    assert all(
        found is not None for found in results(archive, PROJECT, SESSION, RUN, [record, other])
    )
    assert results(archive, PROJECT, SESSION, RUN, [other, record]) == [None, None]


def test_paged_tool_places_extend_after_an_earlier_review(archive: Archive) -> None:
    from sandbox_science.orchestrator.context.history.collections import Collection

    tools = Collection(archive.control, (PROJECT, SESSION, RUN), "tools")
    for number in (1, 2):
        arguments = json.dumps({"code": str(number)})
        answer(archive, asked((f"c{number}", arguments)))
        result(archive, f"c{number}", str(number))
        tools.append({"tool": "sage", "arguments": arguments})
        places = results(archive, PROJECT, SESSION, RUN, tools)
        assert len(places) == number
        assert all(place is not None for place in places)


def test_held_calls_are_the_sessions_earlier_runs_only(archive: Archive) -> None:
    """The review of #462: an earlier run's call counts only when its result is in the
    session reviewed, and is not the reviewed run's own."""
    later = "20261016T040000000000Z-run-2"
    here = result(archive, "c1", "1")
    elsewhere = result(archive, "c2", "2", session="other")
    own = result(archive, "c3", "3", run=later)
    assert held_calls(archive, PROJECT, SESSION, later, [here, elsewhere, own]) == [("run-1", "c1")]


def test_a_refused_earlier_action_is_listed_as_refused(store: Any) -> None:
    """The review of #462: an earlier run's call that was refused comes in with its
    outcome, and the rubric holds a refused call to be no record, so a later answer
    saying it happened still has none."""
    from .test_consistency import claim

    reviews, _, _ = store
    reviews.observe(
        "p", "s", "turn-1:action:3", "action",
        {"run": "run-1", "invocation": "turn-1", "message": 3},
        {"tool": "document_patch", "call": "c1", "target": {"document": "notes"},
         "outcome": "refused", "made": []},
    )  # fmt: skip
    claim(reviews, "Done: the first run patched the notes.")
    review = reviews.dispatch(
        "p", "s", "terminal", author_run="author-1", owner="owner", alias="reviewer",
        viewed=[("run-1", "c1")],
    )  # fmt: skip
    assert review is not None
    items = reviews.manifest("p", "s", review)["evidence"]
    held = actions(reviews, review, items, scope=("p", "s"))
    assert [(a["run"], a["tool"], a["outcome"]) for a in held["earlier_runs"]["actions"]] == [
        ("run-1", "document_patch", "refused")
    ]
    assert "A refused or failed call is no record" in " ".join(RUBRIC.split())
