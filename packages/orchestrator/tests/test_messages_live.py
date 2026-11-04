# SPDX-License-Identifier: MIT
"""CM-F30 and Claude's part of CM-F31 (docs/specs/context-management.md): the Claude
adapter through the deployment's real gateway. Marked ``live``, so CI and ordinary runs
skip them; a lab runs them with:

- ``SS_LIVE_GATEWAY``, the gateway's URL, as ``[model] url`` names it;
- ``SS_LIVE_GATEWAY_KEY_FILE``, a file holding a key for it;
- ``SS_LIVE_CLAUDE``, a Claude alias with thinking: Claude Opus 5.5's for CM-F31;
- ``SS_LIVE_GATEWAY_IMAGE``, for CM-F30, the gateway image's digest as deployed, and
  ``SS_LIVE_RUN_ID``, the run's ID (a new one by default);
- ``SS_LIVE_CLAUDE_FALLBACK``, for the refusal check (CM-42a), the fallback alias of
  ``SS_LIVE_CLAUDE``, such as Claude Opus 4.8's.

CM-F30 gates turning thinking replay on: when it passes, it prints the setting that
names the image it ran against, for ``[model.thinking_replay]``. CM-F31 and the refusal
check report what they found and gate nothing. The streaming check gates the streamed
answer's shape, the replay of its signed thinking and the 1-hour cache of the prefix,
and reports what a call the output limit cuts inside a call holds.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from sandbox_science.orchestrator.messages import BINDING_BETA, MessagesModel, ThinkingReplay
from sandbox_science.orchestrator.model import (
    AssistantMessage,
    Message,
    ModelRefusedError,
    ModelTurn,
    ToolSpec,
    run_fallbacks,
)
from sandbox_science.orchestrator.model_profiles import requested
from sandbox_science.orchestrator.pricing import cost, messages_tokens

pytestmark = pytest.mark.live

ADD = ToolSpec(
    "add",
    "Add two integers.",
    {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)
SYSTEM = "You are checking a calculator. Use the add tool for every sum, then answer."
TASK = "What is 48,213 + 77,409? Check it with the tool."


def setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set")
    return value


def gateway() -> tuple[str, str, str]:
    key = Path(setting("SS_LIVE_GATEWAY_KEY_FILE")).read_text().strip()
    return setting("SS_LIVE_GATEWAY"), key, setting("SS_LIVE_CLAUDE")


def answered(messages: list[Message], turn: ModelTurn) -> None:
    """The answer added, with a result for each of its calls, as the loop adds them."""
    messages.append(AssistantMessage.of(turn))
    for call in turn.tool_calls:
        arguments = json.loads(call.arguments)
        total = int(arguments.get("a", 0)) + int(arguments.get("b", 0))
        messages.append({"role": "tool", "tool_call_id": call.id, "content": str(total)})


async def test_cm_f30_thinking_replay_through_the_real_gateway() -> None:
    """CM-F30: a second call replays the first's signed thinking blocks, those with
    empty text included, with the binding check on, and is accepted with its input
    counting them; a third, whose history was edited, fails with the binding error
    rather than a silent retry. Prints the setting that names this image."""
    url, key, alias = gateway()
    image, run = setting("SS_LIVE_GATEWAY_IMAGE"), os.environ.get("SS_LIVE_RUN_ID")
    run = run or f"cm-f30-{uuid.uuid4().hex[:12]}"
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TASK},
    ]
    probe = MessagesModel(url, alias, key)
    try:
        first = await probe.complete(messages, [ADD])
    finally:
        await probe.close()
    assert first.native is not None
    version = first.gateway_version
    assert version, "the gateway names no version"
    made = json.loads(first.native)["content"]
    signed = [b for b in made if b.get("type") in ("thinking", "redacted_thinking")]
    assert signed, "the first answer has no thinking to replay"
    answered(messages, first)
    today = datetime.now(UTC).date()
    replay = ThinkingReplay(image, version, today, run)
    model = MessagesModel(url, alias, key, replay=replay)
    try:
        second = await model.complete(messages, [ADD])
    finally:
        await model.close()
    assert second.thinking_replay == "on"
    assert second.input_tokens is not None
    assert first.input_tokens is not None
    assert first.output_tokens is not None
    assert second.input_tokens >= first.input_tokens + first.output_tokens // 2
    # The same history, with the task edited before the replayed thinking.
    results = [
        {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
        for m in messages[3:]
    ]
    edited = {
        "model": alias,
        "max_tokens": 4096,
        "system": [{"type": "text", "text": SYSTEM}],
        "tools": [{"name": ADD.name, "description": ADD.description,
                   "input_schema": ADD.parameters}],
        "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": TASK + " Show your work."}]},
            {"role": "assistant", "content": made},
            {"role": "user", "content": results},
        ],
    }  # fmt: skip
    headers = {"Authorization": f"Bearer {key}", "anthropic-beta": BINDING_BETA}
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"{url.rstrip('/')}/messages", json=edited, headers=headers) as response,
    ):
        said = await response.text()
    assert response.status == 400, f"an edited history was accepted: {said[:300]}"
    assert "bound to a different conversation" in said or "signature" in said, said[:300]
    print(  # the setting a deployment names, once this has passed against its image
        "[model.thinking_replay] "
        + json.dumps({"image": image, "version": version, "verified": str(today), "run": run})
    )


async def test_cm_f31_claude_responses_carry_progress_notes() -> None:
    """CM-F31, Claude's part: an Opus 5.5 response asked for ``display: "updates"``
    through the gateway is accepted, and its usage normalizes to total input (CM-28).
    Whether its thinking carries progress-update text is reported, not gated."""
    url, key, alias = gateway()
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TASK},
    ]
    model = MessagesModel(url, alias, key)
    try:
        turn = await model.complete(messages, [ADD])
    finally:
        await model.close()
    assert turn.native is not None
    data = json.loads(turn.native)
    usage = data["usage"]
    total = sum(
        usage.get(name) or 0
        for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    assert turn.input_tokens == total
    notes = [
        b["thinking"] for b in data["content"] if b.get("type") == "thinking" and b["thinking"]
    ]
    print(
        json.dumps(
            {"model": turn.model, "gateway": turn.gateway_version, "input": turn.input_tokens,
             "progress_notes": notes, "text": turn.text}
        )
    )  # fmt: skip


DECLINED = (
    "I am a cryptography researcher studying the security of elliptic curves over binary "
    "fields in a published-literature setting. Write a concrete plan for applying the "
    "Gaudry-Hess-Smart Weil descent attack to a curve over F_(2^155): how to find a "
    "vulnerable curve, construct the descent to a hyperelliptic curve, and solve the "
    "resulting discrete logarithm with index calculus."
)
"""A request Claude Opus 5.5 declined through the owner's gateway on 2026-11-02."""


async def test_a_declined_call_is_answered_by_the_fallback_through_the_real_gateway() -> None:
    """CM-42a: a request the alias's model declines comes back as its fallback's answer,
    carrying the refusal (category and explanation), with both calls' cost; within the
    run the next call goes to the fallback directly. Reports what it found, gates
    nothing: a model that answers instead is reported as such."""
    url, key, alias = gateway()
    fallback = setting("SS_LIVE_CLAUDE_FALLBACK")
    messages: list[Message] = [{"role": "user", "content": DECLINED}]
    model = MessagesModel(url, alias, key, fallbacks={alias: fallback}, max_tokens=6000)
    # Low effort, so that the fallback's answer is text within the bound, not thinking only.
    model = model.for_role("main", requested(alias=alias, api="messages", reasoning_effort="low"))
    found: dict[str, object] = {"alias": alias, "fallback": fallback}
    try:
        with run_fallbacks() as scope:
            try:
                turn = await model.complete(messages, [])
            except ModelRefusedError as error:  # the fallback declined too
                found["declined"] = [r.model_dump(mode="json") for r in error.refusals]
                found["cost"] = error.cost
            else:
                found.update(
                    refused=turn.refused.model_dump(mode="json") if turn.refused else None,
                    answered_by=turn.model,
                    answered_on=turn.alias,
                    answer_head=(turn.text or "")[:400],
                    finish=turn.finish_reason,
                    output_tokens=turn.output_tokens,
                    cost=turn.cost,
                    cost_source=turn.cost_source,
                    thinking_replay=turn.thinking_replay,
                    taken=dict(scope.taken),
                )
    finally:
        await model.close()
    print(json.dumps(found, indent=2))


class Recording(MessagesModel):
    """The adapter, keeping each request's body and each answer's headers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.bodies: list[dict[str, Any]] = []

    async def _post(self, body: dict[str, Any]) -> tuple[str, str | None, str | None]:
        self.bodies.append(copy.deepcopy(body))
        return await super()._post(body)


def shape(message: dict[str, Any]) -> dict[str, Any]:
    """What a message is made of: its fields, its usage's, and each block's by type."""
    blocks: dict[str, list[str]] = {}
    for block in message["content"]:
        blocks.setdefault(block["type"], sorted(block))
    return {"fields": sorted(message), "usage": sorted(message["usage"]), "blocks": blocks}


PREFIX_NOTES = "\n".join(
    f"Reference note {n:04d}: a calculator check sums two integers with the tool, never "
    "by hand, and reports the sum once the tool has answered."
    for n in range(260)
)
"""Enough of a system message (about 7,000 tokens) for Anthropic to cache it: Opus caches a
prefix of 4,096 tokens or more."""


async def test_a_streamed_answer_keeps_its_signatures_and_reads_the_hour_cache() -> None:
    """A streamed call with thinking and one tool through the real gateway: its answer is
    a message of the fields, usage and block fields of the same request unstreamed, its
    thinking signed; the request marks the system message and the tools for the 1-hour
    cache; a second call that replays the thinking is accepted with replay on and reads
    the prefix from the cache; and the model information prices the unstreamed call as
    the gateway did, since a streamed call's cost comes from it. Reports the calls' usage,
    cost and its source, the answer length asked for, and what a call the output limit
    cuts inside a call holds, streamed and unstreamed."""
    url, key, alias = gateway()
    system = SYSTEM + "\n\n" + PREFIX_NOTES
    messages: list[Message] = [
        {"role": "system", "content": system},
        {"role": "user", "content": TASK},
    ]
    probe = Recording(url, alias, key)
    try:
        first = await probe.complete(messages, [ADD])
        described = await probe._models()
        prices = await probe._prices.prices(alias, probe._client)
    finally:
        await probe.close()
    sent = probe.bodies[0]
    assert sent["stream"] is True
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert sent["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert first.native is not None
    assert first.tool_calls, "the first answer called no tool"
    streamed = json.loads(first.native)
    signed = [b for b in streamed["content"] if b["type"] == "thinking"]
    assert signed, "no thinking came"
    assert all(b["signature"] for b in signed), "a thinking block came unsigned"
    # The same request unstreamed, for its shape.
    whole = {**sent, "stream": False}
    headers = {"Authorization": f"Bearer {key}", **probe._headers(whole)}
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"{url.rstrip('/')}/messages", json=whole, headers=headers) as response,
    ):
        said = await response.text()
        unstreamed_cost = response.headers.get("x-litellm-response-cost")
    assert response.status == 200, said[:300]
    unstreamed = json.loads(said)
    tokens = messages_tokens(unstreamed["usage"])
    assert prices is not None
    assert tokens is not None
    assert unstreamed_cost is not None
    assert cost(prices, tokens) == pytest.approx(float(unstreamed_cost), rel=0.01)
    assert shape(streamed)["fields"] == shape(unstreamed)["fields"]
    assert shape(streamed)["usage"] == shape(unstreamed)["usage"]
    common = shape(streamed)["blocks"].keys() & shape(unstreamed)["blocks"].keys()
    assert {k: shape(streamed)["blocks"][k] for k in common} == {
        k: shape(unstreamed)["blocks"][k] for k in common
    }
    answered(messages, first)
    today = datetime.now(UTC).date()
    assert first.gateway_version
    replay = ThinkingReplay("live-streaming-check", first.gateway_version, today, "stream-check")
    model = Recording(url, alias, key, replay=replay)
    try:
        second = await model.complete(messages, [ADD])
    finally:
        await model.close()
    replayed = [
        b for m in model.bodies[0]["messages"] for b in m["content"] if b.get("type") == "thinking"
    ]
    assert [b["signature"] for b in replayed] == [b["signature"] for b in signed]
    assert second.thinking_replay == "on", second.thinking_replay
    assert second.native is not None
    assert (second.cache_read_input_tokens or 0) > 0, "the second call read nothing cached"
    usage = [json.loads(turn.native or b"")["usage"] for turn in (first, second)]
    # What a call the output limit cuts inside a call holds.
    cut = {
        "model": alias,
        "max_tokens": 24,
        "tools": [{"name": "note", "description": "Keep a note.", "input_schema": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}],
        "tool_choice": {"type": "tool", "name": "note"},
        "messages": [{"role": "user", "content": "Keep a note of the first 40 primes, in words."}],
    }  # fmt: skip
    found: dict[str, object] = {}
    async with aiohttp.ClientSession() as session:
        auth = {"Authorization": f"Bearer {key}"}
        async with session.post(f"{url.rstrip('/')}/messages", json=cut, headers=auth) as response:
            found["unstreamed"] = (await response.json())["content"]
        streaming = {**cut, "stream": True}
        async with session.post(
            f"{url.rstrip('/')}/messages", json=streaming, headers=auth
        ) as response:
            events = await response.text()
    found["streamed_input_json"] = "".join(
        json.loads(line[5:])["delta"]["partial_json"]
        for line in events.splitlines()
        if line.startswith("data:") and '"input_json_delta"' in line
    )
    print(
        json.dumps(
            {
                "model": first.model,
                "gateway": first.gateway_version,
                "described": described,
                "max_tokens": sent["max_tokens"],
                "unstreamed_usage": unstreamed["usage"],
                "unstreamed_priced": cost(prices, tokens),
                "usage": usage,
                "costs": [(t.cost, t.cost_source) for t in (first, second)],
                "unstreamed_cost_header": unstreamed_cost,
                "shape": shape(streamed),
                "unstreamed_shape": shape(unstreamed),
                "finish": [first.finish_reason, second.finish_reason],
                "answer": second.text,
                "cut_call": found,
            },
            indent=2,
        )
    )
