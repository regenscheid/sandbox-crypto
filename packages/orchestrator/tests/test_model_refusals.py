# SPDX-License-Identifier: MIT
"""A model that declines to answer (docs/specs/context-management.md, CM-42a).

Claude answers some requests with ``stop_reason: "refusal"`` and its ``stop_details``.
That is never an empty answer: the call raises ``ModelRefusedError``, or, when the
deployment names a fallback alias for the alias, the same request goes once to the
fallback, which the rest of the run then calls in the alias's place. The gateway is the
fake LiteLLM of ``test_messages_model``; its key is synthetic.
"""

from __future__ import annotations

import argparse
from typing import Any, cast

import pytest

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.confinement.wire import ModelReply, WireTurn
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.messages import MessagesModel
from sandbox_science.orchestrator.model import (
    MODEL_REFUSED,
    AssistantMessage,
    Message,
    ModelRefusedError,
    ModelTurn,
    Refusal,
    gateway_model,
    run_fallbacks,
)
from sandbox_science.orchestrator.projects import ModelUsage
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_messages_model import (
    COUNTING,
    KEY,
    LOOKING,
    NOTE,
    SETTING,
    SYSTEM,
    TASK,
    TOOLS,
    Fake,
    Reply,
    converse,
    texts,
    thoughts,
)
from .test_messages_model import fake as fake
from .test_model_limits import Waits

FALLBACK = "fallback"
CYBER = ("cyber", "This request touches on offensive security.")
DECLINED = Reply(refusal=CYBER)
CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")


def claude(fake: Fake, *, fallback: bool = True, **options: Any) -> MessagesModel:
    """Opus 5.5 on ``claude``, with Opus 4.8 on ``fallback`` as its fallback."""
    fake.models[FALLBACK] = "anthropic/claude-opus-4-8"
    fallbacks = {"claude": FALLBACK} if fallback else None
    return MessagesModel(fake.url, "claude", KEY, clock=Waits(), fallbacks=fallbacks, **options)


def start() -> list[Message]:
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}]


async def test_a_refusal_is_an_error_with_its_category_never_an_empty_answer(fake: Fake) -> None:
    """Without a fallback, the call raises with the API's category and explanation, the
    model and alias, and the declined call's tokens and cost."""
    model = claude(fake, fallback=False)
    fake.then(DECLINED)
    try:
        with pytest.raises(ModelRefusedError) as raised:
            await model.complete(start(), TOOLS)
    finally:
        await model.close()
    (refusal,) = raised.value.refusals
    assert (refusal.category, refusal.explanation) == CYBER
    assert (refusal.alias, refusal.model) == ("claude", "claude-opus-5-5")
    assert refusal.cost == raised.value.cost == 0.01
    assert refusal.input_tokens
    assert refusal.output_tokens
    assert str(raised.value) == "the model declined: cyber (claude-opus-5-5 on claude)"
    assert raised.value.summary()["category"] == "cyber"
    assert len(fake.bodies) == 1


async def test_a_refusal_goes_once_to_the_fallback_with_the_same_request(fake: Fake) -> None:
    """CM-42a: the same request, on the fallback alias; its answer stands in, carrying
    the refusal, with both calls' cost."""
    model = claude(fake)
    fake.then(DECLINED, Reply(text="The plan, in four steps."))
    try:
        turn = await model.complete(start(), TOOLS)
    finally:
        await model.close()
    declined, asked = fake.bodies
    assert (declined["model"], asked["model"]) == ("claude", FALLBACK)
    for part in ("system", "tools", "messages", "max_tokens"):
        assert asked[part] == declined[part]
    assert (turn.text, turn.alias, turn.model) == (
        "The plan, in four steps.",
        FALLBACK,
        "claude-opus-4-8",
    )
    assert turn.refused is not None
    assert (turn.refused.category, turn.refused.alias, turn.refused.cost) == (
        "cyber",
        "claude",
        0.01,
    )
    assert turn.cost == pytest.approx(0.02)  # both calls
    assert turn.thinking_replay == "off: a call on the fallback of claude"
    assert AssistantMessage.of(turn).alias == FALLBACK


async def test_the_fallback_gets_no_thinking_from_the_history(fake: Fake) -> None:
    """With replay on, the declined request sends the run's thinking back; the fallback's
    request leaves it out, its progress notes sent as text, as at a change of model
    (CM-42, CM-38a)."""
    model = claude(fake, replay=SETTING)
    try:
        messages, _ = await converse(model, [LOOKING, COUNTING], fake)
        fake.then(DECLINED, Reply(text="x has 42 lines."))
        turn = await model.complete(messages, TOOLS)
    finally:
        await model.close()
    declined, asked = fake.bodies[-2:]
    assert thoughts(declined)
    assert not thoughts(asked)
    assert "thinking" in asked  # the fallback still thinks, from scratch
    assert "block_binding" not in asked["thinking"]
    assert NOTE + "Listing the files first." in texts(asked)
    assert turn.text == "x has 42 lines."


async def test_reasoning_extraction_is_not_sent_to_the_fallback(fake: Fake) -> None:
    model = claude(fake)
    fake.then(Reply(refusal=("reasoning_extraction", None)))
    try:
        with pytest.raises(ModelRefusedError) as raised:
            await model.complete(start(), TOOLS)
    finally:
        await model.close()
    assert raised.value.refusals[0].category == "reasoning_extraction"
    assert [body["model"] for body in fake.bodies] == ["claude"]


async def test_a_refusal_after_part_of_an_answer_is_not_sent_to_the_fallback(fake: Fake) -> None:
    model = claude(fake)
    fake.then(Reply(text="Step one is", refusal=CYBER))
    try:
        with pytest.raises(ModelRefusedError):
            await model.complete(start(), TOOLS)
    finally:
        await model.close()
    assert len(fake.bodies) == 1


async def test_a_refusal_on_the_fallback_too_ends_the_call_declined(fake: Fake) -> None:
    model = claude(fake)
    fake.then(DECLINED, Reply(refusal=("cyber", None)))
    try:
        with pytest.raises(ModelRefusedError) as raised:
            await model.complete(start(), TOOLS)
    finally:
        await model.close()
    first, second = raised.value.refusals
    assert (first.alias, second.alias, second.model) == ("claude", FALLBACK, "claude-opus-4-8")
    assert raised.value.cost == pytest.approx(0.02)
    assert str(raised.value).endswith("; its fallback fallback declined too: cyber")
    assert len(fake.bodies) == 2


async def test_a_run_stays_on_the_fallback_and_the_next_starts_on_its_alias(fake: Fake) -> None:
    """Within a run, an alias that fell back calls its fallback directly; outside one,
    and in a new run, a call starts on the alias again."""
    model = claude(fake)
    try:
        with run_fallbacks() as scope:
            fake.then(DECLINED, Reply(text="one"), Reply(text="two"))
            await model.complete(start(), TOOLS)
            second = await model.complete(start(), TOOLS)
            assert scope.taken == {"claude": FALLBACK}
            summary = scope.summary()
            assert summary is not None
            assert summary["taken"] == {"claude": FALLBACK}
        assert [body["model"] for body in fake.bodies] == ["claude", FALLBACK, FALLBACK]
        assert (second.text, second.alias, second.refused) == ("two", FALLBACK, None)
        with run_fallbacks():
            fake.then(Reply(text="three"))
            third = await model.complete(start(), TOOLS)
        fake.then(Reply(text="four"))
        fourth = await model.complete(start(), TOOLS)
    finally:
        await model.close()
    assert (third.alias, fourth.alias) == ("claude", "claude")
    assert fake.info_calls == 2  # the alias's model and the fallback's, each asked once


def toolbox() -> Toolbox:
    return Toolbox(cast(WorkerClient, object()), CONTEXT)


async def test_a_declined_call_stops_the_run_with_the_refusal_and_its_cost(fake: Fake) -> None:
    """Without a fallback the run stops ``model_refused``: the refusal in its summaries,
    and the declined call's cost in its usage."""
    model = claude(fake, fallback=False)
    fake.then(DECLINED)
    try:
        transcript = await run_task(model, toolbox(), "Plan the attack.", run_id="r")
    finally:
        await model.close()
    assert (transcript.stopped, transcript.final_text, transcript.failure) == (
        MODEL_REFUSED,
        None,
        None,
    )
    refusal = cast(dict[str, Any], transcript.summaries["refusal"])
    assert refusal["category"] == "cyber"
    assert refusal["calls"][0]["model"] == "claude-opus-5-5"
    (usage,) = transcript.context.usage
    assert (usage.cost, usage.model, [r.category for r in usage.refusals]) == (
        0.01,
        "claude-opus-5-5",
        ["cyber"],
    )
    assert not any(m.get("role") == "assistant" for m in transcript.context.messages)


async def test_a_run_that_falls_back_records_both_costs_and_stays_on_it(fake: Fake) -> None:
    """The fallback's answer is the run's, its usage carrying the refusal and both calls'
    cost; the run's next call goes straight to the fallback; the next run starts on the
    configured alias."""
    model = claude(fake)
    fake.then(
        DECLINED,
        Reply(calls=[("no_such_tool", {})]),
        Reply(text="Done."),
        Reply(text="Again."),
    )
    try:
        first = await run_task(model, toolbox(), "Plan it.", run_id="r1")
        second = await run_task(model, toolbox(), "Once more.", run_id="r2")
    finally:
        await model.close()
    assert (first.stopped, first.final_text, second.final_text) == ("answered", "Done.", "Again.")
    assert [body["model"] for body in fake.bodies] == ["claude", FALLBACK, FALLBACK, "claude"]
    fell, after = first.context.usage
    assert fell.cost == pytest.approx(0.02)
    assert after.cost == 0.01
    assert [r.category for r in fell.refusals] == ["cyber"]
    assert after.refusals == []
    assert fell.model == after.model == "claude-opus-4-8"
    taken = cast(dict[str, Any], first.summaries["model_fallback"])
    assert taken["taken"] == {"claude": FALLBACK}
    assert "model_fallback" not in second.summaries


def test_a_refusal_crosses_the_confinement_wire() -> None:
    """A fallback's answer keeps its refusal through ``WireTurn``; a declined call comes
    back from the runtime as a ``refused`` error that raises the same refusal."""
    refusal = Refusal(category="cyber", alias="claude", model="claude-opus-5-5", cost=0.01)
    turn = ModelTurn("ok", alias=FALLBACK, refused=refusal, cost=0.02)
    assert WireTurn.of(turn).turn().refused == refusal
    reply = ModelReply.model_validate(
        {"error": "refused", "message": "the model declined", "refusals": [refusal]}
    )
    error = ModelRefusedError(reply.refusals)
    assert (error.refusals, error.cost) == ((refusal,), 0.01)
    assert ModelUsage(refusals=[refusal]).model_dump(mode="json")["refusals"][0]["category"] == (
        "cyber"
    )


def test_the_fallback_setting_is_two_different_aliases() -> None:
    assert cli.fallback_setting("main=older") == ("main", "older")
    for bad in ("main", "main=", "=older", "main=main"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.fallback_setting(bad)
    made = gateway_model(
        "https://gateway.test/v1",
        "main",
        KEY,
        api="messages",
        options={"fallbacks": {"main": "older"}},
    )
    assert isinstance(made, MessagesModel)
    assert made.fallback == "older"
    alone = gateway_model("https://gateway.test/v1", "older", KEY, api="messages")
    assert isinstance(alone, MessagesModel)
    assert alone.fallback is None
