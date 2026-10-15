# SPDX-License-Identifier: MIT
"""The model budget during a run (run-service spec RS-6a, amended 2026-10-15).

Lab 39: a run kept answering a review finding that arrived while it ran and spent
370,000 tokens past the project's cap, which was checked only when a run started. A run
now stops at its next safe point, before a model call, once the project has spent its
budget over the last 24 hours (``budget``) or once its own calls have used its share of
it (``run_budget``). What arrives after that waits, as it does at a run's start.

The scripted gateway reports a call's input tokens as its messages and its output
tokens as the calls made so far: a few tokens a call.
"""

from __future__ import annotations

from sandbox_science.orchestrator import service as service_module
from sandbox_science.orchestrator.runstate import cost
from sandbox_science.testing import eventually

from .loop_harness import PROJECT, SESSION, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_run_service import ended, names, post, runs, serving, states


async def test_a_run_that_spends_the_budget_stops_before_its_next_call(
    harness: Harness,
) -> None:
    runs(harness).set_budget(PROJECT, 1)  # spent by the first call
    post(harness, "m-1", "Read the plan, then answer.")
    async with serving(harness, Reply(calls=[call("plan_read")]), Reply("Never sent.")):
        (record,) = await ended(harness)
    assert (record.stopped, record.failure, record.final_text) == ("budget", None, None)
    assert len(harness.gateway.main) == 1
    assert [tool["tool"] for tool in record.tools] == ["plan_read"]  # its call still ran
    assert states(harness) == {"m-1": "delivered"}
    stopped = runs(harness).events(PROJECT, SESSION, names(harness)[0])[-1]
    assert stopped.data["stopped"] == "budget"


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


async def test_what_arrives_after_the_budget_is_spent_waits(harness: Harness) -> None:
    """An answer stands; the message that would have continued the run waits, and the
    run it starts is refused for the budget, as at any run's start."""
    runs(harness).set_budget(PROJECT, 1)

    async def another() -> None:
        post(harness, "m-2", "And one more thing.")

    post(harness, "m-1", "A quick question.")
    async with serving(harness, Reply("The answer.", before=another), Reply("Never sent.")):
        (record,) = await ended(harness)
        await eventually(lambda: states(harness).get("m-2") == "refused", timeout=10)
    assert (record.stopped, record.final_text) == ("budget", "The answer.")
    assert len(harness.gateway.main) == 1
    assert record.delivered["messages"] == ["m-1"]
    assert states(harness) == {"m-1": "delivered", "m-2": "refused"}
    (refused,) = [m for m in runs(harness).messages(PROJECT, SESSION) if m.message_id == "m-2"]
    assert refused.reason == "budget"
    assert len(names(harness)) == 1


async def test_a_run_stops_once_it_has_used_its_share(harness: Harness) -> None:
    post(harness, "m-1", "Read the plan, then answer.")
    replies = (Reply(calls=[call("plan_read")]), Reply("Never sent."))
    async with serving(harness, *replies, run_share=0.000_001):  # 2 of 2,000,000 tokens
        (record,) = await ended(harness)
    assert (record.stopped, record.final_text) == ("run_budget", None)
    assert len(harness.gateway.main) == 1
    assert not runs(harness).over_budget(PROJECT, record.ended_at)  # the day's is not


async def test_a_run_within_its_budget_goes_on(harness: Harness) -> None:
    post(harness, "m-1", "Read the plan, then answer.")
    async with serving(harness, Reply(calls=[call("plan_read")]), Reply("Done.")):
        (record,) = await ended(harness)
    assert (record.stopped, record.final_text) == ("answered", "Done.")
    assert len(harness.gateway.main) == 2
    assert sum(cost(usage) for usage in record.usage) > 0
