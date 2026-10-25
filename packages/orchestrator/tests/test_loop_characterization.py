# SPDX-License-Identifier: MIT
"""Characterization of the agent loop: the messages a model sees and what a run delivers.

These tests pin today's behavior for the turn-hook migration (A-02, step 0). They drive
the harness only through ``cli.main(["run", ...])``, with an in-process worker and a
scripted OpenAI-compatible gateway, and read results back through the transcript, the
project's run records and public store methods. The six notice sources are made the way
a user or an earlier run makes them.

Today's order: before each model call, work requests, plans, reviews, math (a safe
point, no message) and kernels; at a closing answer, plans, requests, jobs, reviews,
math, then the plans recheck. Job and document-change notices also ride on tool results.
The record's delivery fields name what a successful model call saw; the CLI saves the
record and then acknowledges them.

Assertions expected to move with the refactor carry a comment naming the finding.
"""

from __future__ import annotations

import json

from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.context.history import body
from sandbox_science.orchestrator.projects import RunRecord

from .loop_harness import (
    ANSWER,
    RESULT,
    SYSTEM,
    TASK_MESSAGE,
    Harness,
    Json,
    Reply,
    Reviewer,
    call,
    calls,
    counts,
    notice,
    plans,
    shape,
    six_sources,
    start_job,
)
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

SOURCES = ("requests", "jobs", "reviews", "math", "kernels")


# --- Delivery bookkeeping ------------------------------------------------------------
# A-02 replaces the per-source RunRecord fields with ``RunRecord.delivered`` and
# ``RunRecord.summaries``; the migration changes these two helpers, not the tests.


def delivered(record: RunRecord, source: str) -> list[str]:
    """The notice IDs a saved run reports as delivered for one source."""
    match source:
        case "requests" | "jobs" | "reviews" | "math" | "kernels":
            # A-04 changed this: kernel notices are a stateless pre-turn check, and
            # the record names them with the other sources.
            return list(record.delivered.get(source, []))
        case _:
            raise ValueError(source)


def summary(record: RunRecord, source: str) -> Json:
    """The closing summary a saved run reports for reviews or math."""
    found = record.summaries.get(source)
    return dict(found) if isinstance(found, dict) else {}


def all_delivered(record: RunRecord) -> dict[str, list[str]]:
    return {source: delivered(record, source) for source in SOURCES}


NOTHING: dict[str, list[str]] = {source: [] for source in SOURCES}


# --- Scenarios -------------------------------------------------------------------------


async def test_a_normal_answer(harness: Harness) -> None:
    record = await harness.run("run-1", [Reply("Here is the answer.")])
    (seen,) = harness.gateway.main
    assert shape(seen) == [SYSTEM, TASK_MESSAGE, plans("no_current_plan", False)]
    assert shape(record.messages) == [*shape(seen), ANSWER]
    assert (record.stopped, record.final_text, record.failure) == (
        "answered",
        "Here is the answer.",
        None,
    )
    assert all_delivered(record) == NOTHING
    reviews = summary(record, "reviews")
    assert (reviews["classifier"]["label"], reviews["reviews"], reviews["counts"]) == (
        "conversation",
        [],
        {},
    )
    assert summary(record, "math") == {"pending": False, "renderer": "unavailable", "counts": {}}


async def test_all_six_sources_in_todays_order_then_acknowledged_after_save(
    harness: Harness,
) -> None:
    sources = await six_sources(harness)

    async def newer_kernel_revision() -> None:
        harness.worker.lineage.current = 3

    record = await harness.run(
        "run-1",
        [
            Reply(calls=[call("plan_read")]),
            Reply("Answer A.", before=newer_kernel_revision),
            Reply("Answer B."),
        ],
    )
    first, second, third = harness.gateway.main
    kernel, newer = shape(first)[-1][2][0], shape(third)[-1][2][0]
    start = [
        SYSTEM,
        TASK_MESSAGE,
        notice("requests", sources.request),
        notice("jobs", sources.job),
        plans("draft", True),
        # The owner, 2026-10-16: an earlier run's finding comes as a count, not a notice.
        counts(fail_open=1),
        notice("kernels", kernel),
    ]
    assert shape(first) == start
    assert shape(second) == [*start, calls("plan_read"), RESULT]
    # Closing: the plan read was delivered (a new plan state), then the math response
    # round; the job, request and review sources had nothing new. A-04 changed this:
    # the kernel check before the response round tells of the revision-3 observation.
    assert shape(third) == [
        *shape(second), ANSWER, plans("draft", False), notice("math", sources.math),
        notice("kernels", newer),
    ]  # fmt: skip
    assert shape(record.messages) == [*shape(third), ANSWER]
    assert (record.stopped, record.final_text) == ("answered", "Answer B.")
    assert all_delivered(record) == {
        "requests": [sources.request],
        "jobs": [sources.job],
        "reviews": [],
        "math": [sources.math],
        "kernels": sorted([kernel, newer]),
    }
    reviews = summary(record, "reviews")
    # Only delivered open findings are bounded: the earlier one, a count, stays open.
    assert (reviews["classifier"]["label"], reviews["counts"]) == ("terminal", {"fail:open": 1})
    # The math bound is staged until the saved record is acknowledged.
    assert summary(record, "math") == {
        "pending": False,
        "renderer": "unavailable",
        "counts": {"open": 1},
    }

    # The CLI saved the record, then acknowledged what it names.
    assert harness.request_states() == {sources.request: "delivered"}
    assert harness.findings() == {"review": "open", "math": "open at bound"}
    again = await harness.run("run-2", [Reply(calls=[call("plan_read")]), Reply("Answer C.")])
    first, _ = harness.gateway.main
    assert delivered(again, "kernels") == [newer]
    assert newer != kernel
    assert shape(first) == [
        SYSTEM,
        TASK_MESSAGE,
        plans("draft", True),
        counts(fail_open=1),
        notice("kernels", newer),  # A-04 changes this: the revision-3 observation.
    ]
    # A-02 may change this: a closing plan notice is recorded after the final answer
    # although no model call sees it.
    assert shape(again.messages) == [
        *shape(first), calls("plan_read"), RESULT, ANSWER, plans("draft", False)
    ]  # fmt: skip
    assert all_delivered(again) == {**NOTHING, "kernels": [newer]}


async def test_a_failed_first_model_call_preserves_its_request_while_other_notices_return(
    harness: Harness,
) -> None:
    sources = await six_sources(harness)
    failed = await harness.run("run-1", [Reply(status=500)], status=1)
    (offered,) = harness.gateway.main
    assert (failed.stopped, failed.failure, failed.final_text) == ("error", "model", None)
    assert shape(failed.messages) == shape(offered)
    assert all_delivered(failed) == NOTHING
    assert harness.request_states() == {sources.request: "queued"}
    assert harness.findings() == {"review": "open", "math": "open"}

    record = await harness.run(
        "run-2", [Reply(calls=[call("plan_read")]), Reply("Answer A."), Reply("Answer B.")]
    )
    first = harness.gateway.main[0]
    # Fresh explicit work may consume other notices, but cannot implicitly retry
    # the annotation owned by the earlier unfinished paid attempt.
    assert shape(first) == [item for item in shape(offered) if item[1] != "requests"]
    assert all_delivered(record) == {
        "requests": [],
        "jobs": [sources.job],
        "reviews": [],
        "math": [sources.math],
        "kernels": delivered(record, "kernels"),
    }
    assert shape(first)[-1] == notice("kernels", *delivered(record, "kernels"))
    assert harness.request_states() == {sources.request: "queued"}


async def test_a_failed_response_round_keeps_only_what_earlier_calls_delivered(
    harness: Harness,
) -> None:
    sources = await six_sources(harness)
    failed = await harness.run(
        "run-1",
        [Reply(calls=[call("plan_read")]), Reply("Answer A."), Reply(status=500)],
        status=1,
    )
    *_, offered = harness.gateway.main
    assert shape(offered)[-1] == notice("math", sources.math)
    assert (failed.stopped, failed.failure, failed.final_text) == ("error", "model", None)
    # The math notice was offered only to the failed call; the rest were delivered.
    assert all_delivered(failed) == {
        "requests": [sources.request],
        "jobs": [sources.job],
        "reviews": [],
        "math": [],
        "kernels": delivered(failed, "kernels"),
    }
    assert len(delivered(failed, "kernels")) == 1
    assert harness.request_states() == {sources.request: "delivered"}
    # The run ended before its closing bound: the review finding stays open.
    assert harness.findings() == {"review": "open", "math": "open"}

    record = await harness.run(
        "run-2", [Reply(calls=[call("plan_read")]), Reply("Answer B."), Reply("Answer C.")]
    )
    first, _, third = harness.gateway.main
    # A-04 changed this: each run is told again while a kernel predates the packages.
    (kernel,) = delivered(failed, "kernels")
    assert shape(first) == [
        SYSTEM, TASK_MESSAGE, plans("draft", True), counts(fail_open=1), notice("kernels", kernel)
    ]  # fmt: skip
    assert shape(third)[-1] == notice("math", sources.math)
    assert all_delivered(record) == {**NOTHING, "math": [sources.math], "kernels": [kernel]}


async def test_closing_sources_take_turns_in_todays_order(harness: Harness) -> None:
    await harness.run("run-job", [Reply(calls=[start_job()]), Reply("A job is running.")])
    math = await harness.math_finding()
    harness.configure_reviews(ReviewConfig())
    reviewer = Reviewer(flags=1)
    harness.gateway.reviewer = reviewer
    arrived: list[str] = []

    async def arrive() -> None:
        harness.user_plan()
        arrived.extend([await harness.end_job(), harness.work_request()])

    record = await harness.run(
        "run-1",
        [
            Reply("Answer 1.", before=arrive),
            Reply(calls=[call("plan_read")]),
            Reply("Answer 2."),
            Reply("Answer 3, done."),
            Reply("Answer 4."),
        ],
        reviewer=True,
    )
    job, request = arrived
    calls_seen = [shape(messages) for messages in harness.gateway.main]
    assert calls_seen[0] == [SYSTEM, TASK_MESSAGE, plans("no_current_plan", False)]
    # Closing 1: the unread plan wins and gets a response turn; the request that
    # arrived with it is picked up before the next model call, not at the closing.
    assert calls_seen[1] == [
        *calls_seen[0], ANSWER, plans("draft", True), notice("requests", request)
    ]  # fmt: skip
    assert calls_seen[2] == [*calls_seen[1], calls("plan_read"), RESULT]
    # Closing 2: the delivered read changes the plan state, then the ended job.
    assert calls_seen[3] == [
        *calls_seen[2], ANSWER, plans("draft", False), notice("jobs", job)
    ]  # fmt: skip
    # Closing 3: the terminal review and the math check share one response round.
    (flagged,) = calls_seen[4][-3][2]
    assert calls_seen[4] == [
        *calls_seen[3], ANSWER, notice("reviews", flagged), counts(fail_open=1),
        notice("math", math),
    ]  # fmt: skip
    # Closing 4: nothing new; both delivered findings are bounded.
    assert shape(record.messages) == [*calls_seen[4], ANSWER]
    assert (record.stopped, record.final_text) == ("answered", "Answer 4.")
    # An inconclusive review leaves its evidence queued for a later window.
    assert reviewer.windows == 3
    assert all_delivered(record) == {
        **NOTHING,
        "requests": [request],
        "jobs": [job],
        "reviews": [flagged],
        "math": [math],
    }
    # A finding at its bound is still open, and counted as open.
    assert summary(record, "reviews")["counts"] == {"fail:open": 1}
    assert harness.findings() == {"review": "open at bound", "math": "open at bound"}


async def test_a_job_ending_at_closing_continues_the_run_with_its_notice(
    harness: Harness,
) -> None:
    ended: list[str] = []

    async def end() -> None:
        ended.append(await harness.end_job())

    record = await harness.run(
        "run-1",
        [
            Reply(calls=[start_job()]),
            Reply("Waiting for the job.", before=end),
            Reply("The job ended."),
        ],
    )
    (job,) = ended
    *_, last = harness.gateway.main
    assert shape(last) == [
        SYSTEM, TASK_MESSAGE, plans("no_current_plan", False), calls("start_job"), RESULT,
        ANSWER, notice("jobs", job),
    ]  # fmt: skip
    assert (record.stopped, record.final_text) == ("answered", "The job ended.")
    assert all_delivered(record) == {**NOTHING, "jobs": [job]}
    await harness.run("run-2", [Reply("Nothing new.")])
    (seen,) = harness.gateway.main
    assert shape(seen) == [SYSTEM, TASK_MESSAGE, plans("no_current_plan", False)]


async def test_job_and_document_notices_ride_on_tool_results(harness: Harness) -> None:
    harness.notes()
    ended: list[str] = []

    async def end_and_edit() -> None:
        ended.append(await harness.end_job("go"))
        harness.user_edit("A better result.\n")

    async def end_second() -> None:
        ended.append(await harness.end_job("go2"))

    head, tail = "HEAD-MARK", "TAIL-MARK"
    # Quotes double when the result is escaped again inside the combined JSON.
    loud = f"printf {head}; head -c 6000 /dev/zero | tr '\\0' '\"'; printf {tail}"
    record = await harness.run(
        "run-1",
        [
            Reply(
                calls=[start_job("go"), start_job("go2"), call("document_read", document="notes")]
            ),
            Reply(calls=[call("shell", script="echo small")], before=end_and_edit),
            Reply(calls=[call("shell", script=loud)], before=end_second),
            Reply(calls=[call("list_jobs")]),
            Reply("Read all three."),
        ],
    )
    first, second = ended
    *_, last = harness.gateway.main
    assert shape(last) == [
        SYSTEM, TASK_MESSAGE, plans("no_current_plan", False),
        calls("start_job", "start_job", "document_read"), RESULT, RESULT, RESULT,
        calls("shell"), ("tool", "result+jobs+documents", (first,)),
        calls("shell"), ("tool", "result+jobs", (second,)),
        calls("list_jobs"), RESULT,
    ]  # fmt: skip
    assert all_delivered(record) == {**NOTHING, "jobs": sorted([first, second])}
    attached = json.loads(body(last[-3]["content"]))
    assert [n["job_id"] for n in attached["job_notices"]] == [second]
    assert len(last[-3]["content"]) <= 20000
    # A-02 changed this: attaching job notices to a long result keeps its head and its
    # tail, where tracebacks and platform notices are.
    assert head in attached["tool_result"]
    assert tail in attached["tool_result"]


async def test_a_review_response_round_then_the_bound(harness: Harness) -> None:
    harness.configure_reviews(ReviewConfig())
    reviewer = Reviewer()
    harness.gateway.reviewer = reviewer
    record = await harness.run(
        "run-1",
        [Reply("The task is done."), Reply("It is done."), Reply("It is done, as stated.")],
        reviewer=True,
    )
    first, second, third = harness.gateway.main
    assert shape(first) == [SYSTEM, TASK_MESSAGE, plans("no_current_plan", False)]
    flagged = shape(second)[-2][2]
    (finding,) = {notice_id.rsplit("-r", 1)[0] for notice_id in flagged}
    assert shape(second) == [
        *shape(first), ANSWER, notice("reviews", f"{finding}-r1"), counts(fail_open=1)
    ]  # fmt: skip
    # Each later window re-flags the same finding at a new revision; the counts are
    # unchanged, so they are not repeated.
    assert shape(third) == [*shape(second), ANSWER, notice("reviews", f"{finding}-r2")]
    assert shape(record.messages) == [*shape(third), ANSWER]
    assert (record.stopped, record.final_text) == ("answered", "It is done, as stated.")
    # Two response rounds, then a third window whose re-flag (r3) reaches no model:
    # the delivered findings stay open at their bound. A-02 step 4 keeps this or
    # changes it deliberately: the bound also covers the re-flag no model saw.
    assert reviewer.windows == 3
    assert summary(record, "reviews")["reviews_total"] == 3
    assert all_delivered(record) == {**NOTHING, "reviews": [f"{finding}-r1", f"{finding}-r2"]}
    reviews = summary(record, "reviews")
    assert (reviews["outcome"], len(reviews["reviews"]), reviews["counts"]) == (
        "findings",
        3,
        {"fail:open": 1},
    )
    assert harness.findings() == {"review": "open at bound"}

    again = await harness.run("run-2", [Reply("Fine.")], reviewer=True)
    (seen,) = harness.gateway.main
    assert shape(seen) == [
        SYSTEM, TASK_MESSAGE, plans("no_current_plan", False), counts(fail_open=1)
    ]  # fmt: skip
    assert all_delivered(again) == NOTHING
    assert reviewer.windows == 3  # a conversational answer starts no review


async def test_a_user_plan_edit_mid_run_needs_a_reread_or_stops_for_the_user(
    harness: Harness,
) -> None:
    async def steer() -> None:
        harness.user_plan()

    stopped = await harness.run(
        "run-1", [Reply("First answer.", before=steer), Reply("Second answer.")], status=3
    )
    first, second = harness.gateway.main
    assert shape(first) == [SYSTEM, TASK_MESSAGE, plans("no_current_plan", False)]
    assert shape(second) == [*shape(first), ANSWER, plans("draft", True)]
    assert shape(stopped.messages) == [*shape(second), ANSWER]
    # The unread USER plan wins over the answer: no stale final answer is presented.
    assert (stopped.stopped, stopped.final_text, stopped.failure) == ("plan_unread", None, None)

    record = await harness.run(
        "run-2", [Reply("Ignoring the plan."), Reply(calls=[call("plan_read")]), Reply("Read.")]
    )
    first, second, third = harness.gateway.main
    assert shape(first) == [SYSTEM, TASK_MESSAGE, plans("draft", True)]
    # The first closing gets one response turn, with a notice that says why.
    assert shape(second) == [*shape(first), ANSWER, plans("draft", True)]
    assert shape(third) == [*shape(second), calls("plan_read"), RESULT]
    # A-02 may change this: the closing plan notice after the read is recorded only.
    assert shape(record.messages) == [*shape(third), ANSWER, plans("draft", False)]
    assert (record.stopped, record.final_text) == ("answered", "Read.")


async def test_on_the_last_turn_closing_notices_wait_and_the_answer_stands(
    harness: Harness,
) -> None:
    await harness.run("run-job", [Reply(calls=[start_job()]), Reply("A job is running.")])
    math = await harness.math_finding()
    arrived: list[str] = []

    async def arrive() -> None:
        arrived.extend([await harness.end_job(), harness.work_request()])

    record = await harness.run("run-1", [Reply("Last answer.", before=arrive)], max_turns=1)
    job, request = arrived
    (seen,) = harness.gateway.main
    assert shape(seen) == [SYSTEM, TASK_MESSAGE, plans("no_current_plan", False)]
    # A-10: the job, the request and the math finding wait for the next run.
    assert shape(record.messages) == [*shape(seen), ANSWER]
    assert (record.stopped, record.final_text) == ("answered", "Last answer.")
    assert all_delivered(record) == NOTHING
    assert harness.request_states() == {request: "queued"}

    later = await harness.run("run-2", [Reply("Handled."), Reply("Fixed the formula.")])
    first, second = harness.gateway.main
    assert shape(first) == [
        SYSTEM, TASK_MESSAGE, notice("requests", request), notice("jobs", job),
        plans("no_current_plan", False),
    ]  # fmt: skip
    assert shape(second) == [*shape(first), ANSWER, notice("math", math)]
    assert all_delivered(later) == {
        **NOTHING,
        "requests": [request],
        "jobs": [job],
        "math": [math],
    }
