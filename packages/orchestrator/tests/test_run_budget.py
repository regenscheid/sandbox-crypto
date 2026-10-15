# SPDX-License-Identifier: MIT
"""Spending during a run (run-service spec RS-6a, RS-6c and RS-6d, as the owner decided
on 2026-10-15: spending is mostly enforced upstream, by the gateway's own budget, and a
project's own limit is an optional daily cost, as the gateway reports it).

With no limit, as by default, nothing stops a run for spending but the gateway. With a
limit, a run stops at its next safe point, before a model call, once the project's calls
over the last 24 hours reached it (``budget``) or once its own calls reached its part of
it (``run_budget``). What arrives after that waits, as it does at a run's start.

The scripted gateway reports a call's cost in LiteLLM's header when its reply has one.
"""

from __future__ import annotations

from sandbox_science.orchestrator import service as service_module
from sandbox_science.testing import eventually

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import ended, names, post, runs, serving, states


async def test_without_a_limit_nothing_stops_a_run_for_its_cost(harness: Harness) -> None:
    post(harness, "m-1", "Read the plan, then answer.")
    replies = (Reply(calls=[call("plan_read")], cost="1000"), Reply("Done.", cost="1000"))
    async with serving(harness, *replies, run_share=0.5):
        (record,) = await ended(harness)
    assert (record.stopped, record.final_text) == ("answered", "Done.")
    assert len(harness.gateway.main) == 2


async def test_a_run_that_reaches_the_limit_stops_before_its_next_call(
    harness: Harness,
) -> None:
    runs(harness).set_cost_limit(PROJECT, 0.01)  # reached by the first call
    post(harness, "m-1", "Read the plan, then answer.")
    replies = (Reply(calls=[call("plan_read")], cost="0.02"), Reply("Never sent."))
    async with serving(harness, *replies):
        (record,) = await ended(harness)
    assert (record.stopped, record.failure, record.final_text) == ("budget", None, None)
    assert len(harness.gateway.main) == 1
    assert [tool["tool"] for tool in record.tools] == ["plan_read"]  # its call still ran
    assert states(harness) == {"m-1": "delivered"}
    stopped = runs(harness).events(PROJECT, SESSION, names(harness)[0])[-1]
    assert stopped.data["stopped"] == "budget"


async def test_a_limit_the_gateway_reports_no_cost_against_is_not_kept(harness: Harness) -> None:
    runs(harness).set_cost_limit(PROJECT, 0)
    post(harness, "m-1", "Read the plan, then answer.")
    async with serving(harness, Reply(calls=[call("plan_read")]), Reply("Done.")):
        (record,) = await ended(harness)
    assert (record.stopped, record.final_text) == ("answered", "Done.")


async def test_a_gateway_that_refuses_for_its_budget_ends_the_run_and_the_session_waits(
    harness: Harness,
) -> None:
    """RS-6d: the gateway's own budget, as LiteLLM keeps it for the key. The refused call
    is not tried again, the run ends there, and the message waits, no run starting for it
    until the session's wait is over."""
    refusal = '{"error":{"message":"Budget has been exceeded! Current cost: 5.1, Max budget: 5.0","type":"budget_exceeded","param":null,"code":"400"}}'  # noqa: E501
    post(harness, "m-1", "Are you there?")
    replies = (Reply(status=400, error=refusal), Reply("Never sent."))
    async with serving(harness, *replies) as service:
        (record,) = await ended(harness)
        await eventually(lambda: (PROJECT, SESSION) in service.waiting_until)
        waited = service.waiting_until[PROJECT, SESSION] - service.clock.monotonic()
        assert waited > service_module.MAX_WAIT - 60
        assert len(names(harness)) == 1  # no other run starts for the waiting message
    assert (record.stopped, record.failure, record.final_text) == ("gateway_budget", None, None)
    assert len(harness.gateway.main) == 1
    assert states(harness) == {"m-1": "queued"}  # no model call saw it: it waits
    stopped = runs(harness).events(PROJECT, SESSION, names(harness)[0])[-1]
    assert stopped.data["stopped"] == "gateway_budget"


async def test_what_arrives_after_the_limit_is_reached_waits(harness: Harness) -> None:
    """An answer stands; the message that would have continued the run waits, and the
    run it starts is refused for the limit, as at any run's start."""
    runs(harness).set_cost_limit(PROJECT, 0.01)

    async def another() -> None:
        post(harness, "m-2", "And one more thing.")

    post(harness, "m-1", "A quick question.")
    replies = (Reply("The answer.", before=another, cost="0.02"), Reply("Never sent."))
    async with serving(harness, *replies):
        (record,) = await ended(harness)
        await eventually(lambda: states(harness).get("m-2") == "refused", timeout=10)
    assert (record.stopped, record.final_text) == ("budget", "The answer.")
    assert len(harness.gateway.main) == 1
    assert record.delivered["messages"] == ["m-1"]
    assert states(harness) == {"m-1": "delivered", "m-2": "refused"}
    (refused,) = [m for m in runs(harness).messages(PROJECT, SESSION) if m.message_id == "m-2"]
    assert refused.reason == "budget"
    assert len(names(harness)) == 1


async def test_a_run_stops_once_it_has_used_its_part_of_the_limit(harness: Harness) -> None:
    runs(harness).set_cost_limit(PROJECT, 1.0)
    post(harness, "m-1", "Read the plan, then answer.")
    replies = (Reply(calls=[call("plan_read")], cost="0.6"), Reply("Never sent."))
    async with serving(harness, *replies, run_share=0.5):
        (record,) = await ended(harness)
    assert (record.stopped, record.final_text) == ("run_budget", None)
    assert len(harness.gateway.main) == 1
    assert not runs(harness).over_cost_limit(PROJECT, record.ended_at)  # the day's is not


async def test_a_run_within_its_limit_goes_on(harness: Harness) -> None:
    runs(harness).set_cost_limit(PROJECT, 1.0)
    post(harness, "m-1", "Read the plan, then answer.")
    replies = (Reply(calls=[call("plan_read")], cost="0.1"), Reply("Done.", cost="0.1"))
    async with serving(harness, *replies, run_share=0.5):
        (record,) = await ended(harness)
    assert (record.stopped, record.final_text) == ("answered", "Done.")
    assert [usage.cost for usage in record.usage] == [0.1, 0.1]
