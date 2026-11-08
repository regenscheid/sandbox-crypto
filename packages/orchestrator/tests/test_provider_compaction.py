# SPDX-License-Identifier: MIT
"""The provider-compaction context mode (docs/specs/context-management.md, CM-2a,
CM-38b): Anthropic's server-side compaction in place of folds, for Claude aliases on the
Messages route.

The adapter's part runs against the fake LiteLLM of ``test_messages_model``, which
compacts a request when its reply says so, as Anthropic's API does: a ``compaction``
block first, and ``usage.iterations`` with the compaction's tokens written to the 1-hour
cache. The harness's part runs through the command line against the loop harness's
gateway and an in-process worker. Keys are synthetic.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.context.compaction import compaction
from sandbox_science.orchestrator.context.history import header
from sandbox_science.orchestrator.harness import Settings
from sandbox_science.orchestrator.messages import COMPACT_BETA, MessagesModel
from sandbox_science.orchestrator.model import (
    AssistantMessage,
    Message,
    ModelError,
    ModelRefusedError,
    run_compaction,
    run_fallbacks,
)
from sandbox_science.orchestrator.model_profiles import ModelRole
from sandbox_science.orchestrator.notices import COMPACTION
from sandbox_science.orchestrator.pricing import Prices, Tokens, cost, messages_tokens, prices_of

from .loop_harness import API_KEY, PROJECT, SESSION, TOKEN, Harness, call
from .loop_harness import Reply as Scripted
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import children, wait
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE as CHILD_DONE
from .test_delegation_tasks import TASK as DELEGATE
from .test_messages_model import (
    DONE,
    KEY,
    LOOKING,
    SYSTEM,
    TASK,
    TOOLS,
    Fake,
    Reply,
    converse,
    texts,
)
from .test_messages_model import fake as fake
from .test_model_limits import Waits

Json = dict[str, Any]
TRIGGER = 60_000
SUMMARY = "Summary of the work so far: x was listed and read; it has 42 lines."
STATE = header("msg-0123456789abcdef", "state_block") + "Session record: the ledger holds C-1."
COMPACTING = Reply(calls=[("shell", {"script": "wc x"})], notes=["Counting."], compact=SUMMARY)


def claude(fake: Fake, alias: str = "claude", **options: Any) -> MessagesModel:
    return MessagesModel(fake.url, alias, KEY, clock=Waits(), **options)


def start() -> list[Message]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": STATE},
        {"role": "user", "content": TASK},
    ]


def blocks(body: Json, kind: str) -> list[Json]:
    return [b for m in body["messages"] for b in m["content"] if b.get("type") == kind]


# --- The adapter ---------------------------------------------------------------------


async def test_a_compacting_run_s_calls_carry_the_edit_at_its_trigger(fake: Fake) -> None:
    """Each of the run's calls carries the compaction edit at the run's trigger, with its
    beta; outside such a run, none does."""
    model = claude(fake)
    try:
        with run_compaction(TRIGGER):
            await converse(model, [LOOKING, DONE], fake, messages=start())
        await converse(model, [DONE], fake, messages=start())
    finally:
        await model.close()
    compacting, outside = fake.bodies[:2], fake.bodies[2]
    for body, betas in zip(compacting, fake.betas, strict=False):
        (edit,) = body["context_management"]["edits"]
        assert edit.pop("instructions") == COMPACTION.text
        assert body["context_management"] == {
            "edits": [
                {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": TRIGGER}}
            ]
        }
        assert COMPACT_BETA in betas
    assert "context_management" not in outside
    assert COMPACT_BETA not in fake.betas[2]


@pytest.mark.parametrize("role", ["reviewer", "watcher", "fold"])
async def test_no_other_role_s_conversation_is_compacted(fake: Fake, role: str) -> None:
    """A reviewer's, watcher's or fold writer's call within a compacting run sends no
    edit: only the run's own conversation is the provider's to condense."""
    model = claude(fake).for_role(cast(ModelRole, role))
    try:
        with run_compaction(TRIGGER):
            await converse(model, [DONE], fake, messages=start())
    finally:
        await model.close()
    assert "context_management" not in fake.bodies[0]


async def test_after_a_compaction_a_request_starts_at_the_summary_with_the_state_block(
    fake: Fake,
) -> None:
    """The API takes a compaction block for everything before it: a request sends the
    conversation from the answer that holds it, the system message first and the state
    block right after that answer, with its results; the answer goes back with its
    block. The call's tokens are every iteration's, the compaction's included."""
    model = claude(fake)
    try:
        with run_compaction(TRIGGER):
            messages, turns = await converse(
                model, [LOOKING, COMPACTING, LOOKING, DONE], fake, messages=start()
            )
    finally:
        await model.close()
    before, at, after, last = fake.bodies
    assert before["messages"][0]["role"] == "user"
    assert TASK in texts(at)
    assert STATE in texts(at)
    for body in (after, last):
        first = body["messages"][0]
        assert first["role"] == "assistant"
        assert first["content"][0] == {"type": "compaction", "content": SUMMARY}
        results = body["messages"][1]["content"]
        assert results[0]["type"] == "tool_result"
        assert {"type": "text", "text": STATE} in [
            {k: v for k, v in b.items() if k != "cache_control"} for b in results
        ]
        assert TASK not in texts(body)  # summarized
        assert len(blocks(body, "compaction")) == 1
        assert body["system"][0]["text"] == SYSTEM
    assert after["messages"][0] == last["messages"][0]  # a stable prefix from then on
    usage = json.loads(fake.answered[1])["usage"]
    compacted, answered = usage["iterations"]
    turn = turns[1]
    assert turn.input_tokens == sum(
        step[key]
        for step in (compacted, answered)
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    assert turn.output_tokens == compacted["output_tokens"] + answered["output_tokens"]
    assert turn.cache_creation_input_tokens == (
        compacted["cache_creation_input_tokens"] + answered["cache_creation_input_tokens"]
    )
    assert turn.text is None
    assert turn.tool_calls
    (found,) = [made for message in messages if (made := compaction(message)) is not None]
    assert found["summary_chars"] == len(SUMMARY)
    assert [step["type"] for step in found["iterations"]] == ["compaction", "message"]
    assert (
        found["iterations"][0]["cache_1h_input_tokens"] == compacted["cache_creation_input_tokens"]
    )


async def test_a_compaction_without_a_summary_is_never_sent(fake: Fake) -> None:
    """When the model calls a tool instead of summarizing, the API returns a compaction
    block whose content is null. It stands for nothing: the next request sends the whole
    conversation, without it, and the API compacts again when it must."""
    model = claude(fake)
    try:
        with run_compaction(TRIGGER):
            messages, _ = await converse(
                model,
                [Reply(calls=[("shell", {"script": "wc x"})], compact=""), DONE],
                fake,
                messages=start(),
            )
    finally:
        await model.close()
    sent = fake.bodies[-1]
    assert blocks(sent, "compaction") == []
    assert TASK in texts(sent)
    assert sent["messages"][0]["role"] == "user"
    (found,) = [made for message in messages if (made := compaction(message)) is not None]
    assert found["summarized"] is False


async def test_outside_the_mode_compaction_blocks_are_never_sent(fake: Fake) -> None:
    """A conversation that holds a compaction, sent by a run that folds, goes whole and
    without the block, which the API would refuse without its beta."""
    model = claude(fake)
    try:
        with run_compaction(TRIGGER):
            messages, _ = await converse(model, [COMPACTING], fake, messages=start())
        await converse(model, [DONE], fake, messages=messages)
    finally:
        await model.close()
    sent = fake.bodies[-1]
    assert blocks(sent, "compaction") == []
    assert TASK in texts(sent)
    assert STATE in texts(sent)
    assert sent["messages"][0]["role"] == "user"


async def test_a_model_without_compaction_is_refused_before_the_request(fake: Fake) -> None:
    """Haiku 4.5 takes no server-side compaction: the call is refused before anything is
    sent, never sent uncompacted."""
    model = claude(fake, "haiku")
    try:
        with (
            run_compaction(TRIGGER),
            pytest.raises(ModelError, match="does not take server-side compaction"),
        ):
            await model.complete(start(), TOOLS)
    finally:
        await model.close()
    assert fake.bodies == []


def test_the_trigger_is_at_least_the_api_s_least() -> None:
    with pytest.raises(ValueError, match="50,000"), run_compaction(49_999):
        pass


CYBER = ("cyber", "This request touches on offensive security.")


async def test_the_fallback_of_a_compacting_call_compacts_too(fake: Fake) -> None:
    """CM-42a with CM-38b: the declined call's fallback gets the same request, edit and
    compaction block included."""
    fake.models["fallback"] = "anthropic/claude-opus-4-8"
    model = claude(fake, fallbacks={"claude": "fallback"})
    try:
        with run_compaction(TRIGGER):
            messages, _ = await converse(model, [COMPACTING], fake, messages=start())
            fake.then(Reply(refusal=CYBER), Reply(text="The fallback's answer."))
            with run_fallbacks():
                turn = await model.complete(messages, TOOLS)
    finally:
        await model.close()
    declined, asked = fake.bodies[-2:]
    assert asked["model"] == "fallback"
    for part in ("context_management", "system", "messages"):
        assert asked[part] == declined[part]
    assert asked["messages"][0]["content"][0]["type"] == "compaction"
    assert COMPACT_BETA in fake.betas[-1]
    assert turn.refused is not None
    assert turn.text == "The fallback's answer."


async def test_a_fallback_that_takes_no_compaction_is_refused(fake: Fake) -> None:
    """The fallback's model must take compaction too: one that does not ends the call
    with a model error, never a request without the conversation's compaction."""
    fake.models["fallback"] = "anthropic/claude-haiku-4-5"
    model = claude(fake, fallbacks={"claude": "fallback"})
    fake.then(Reply(refusal=CYBER))
    try:
        with (
            run_compaction(TRIGGER),
            run_fallbacks(),
            pytest.raises(ModelError, match="does not take server-side compaction") as raised,
        ):
            await model.complete(start(), TOOLS)
    finally:
        await model.close()
    assert not isinstance(raised.value, ModelRefusedError)
    assert raised.value.cost == 0.01  # the declined call's
    assert len(fake.bodies) == 1


# --- Pricing -------------------------------------------------------------------------


OPUS_4_8 = {
    "input_cost_per_token": 5e-06,
    "output_cost_per_token": 2.5e-05,
    "cache_read_input_token_cost": 5e-07,
    "cache_creation_input_token_cost": 6.25e-06,
    "cache_creation_input_token_cost_above_1hr": 1e-05,
}


def test_a_compacting_call_is_priced_for_every_iteration_at_the_1_hour_rate() -> None:
    """The kit's measured call (2026-11-02): 88,755 tokens written to the 1-hour cache by
    the compaction, then a 267-token answer; the gateway reported USD 0.895."""
    usage = {
        "input_tokens": 267, "output_tokens": 20,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "iterations": [
            {"type": "compaction", "input_tokens": 45, "output_tokens": 220,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 88_755,
             "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                "ephemeral_1h_input_tokens": 88_755}},
            {"type": "message", "input_tokens": 267, "output_tokens": 20,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        ],
    }  # fmt: skip
    tokens = messages_tokens(usage)
    assert tokens == Tokens(312, 0, 88_755, 240, 88_755)
    data = {"data": [{"model_name": "opus", "model_info": OPUS_4_8}]}
    listed = prices_of(data, "opus")
    assert listed is not None
    assert tokens is not None
    (prices,) = listed
    assert prices.cache_write_1h == 1e-05
    assert cost(prices, tokens) == pytest.approx(0.89511)


def test_a_1_hour_write_without_its_price_leaves_the_call_unpriced() -> None:
    tokens = Tokens(10, 0, 100, 5, 100)
    assert cost(Prices(5e-06, 2.5e-05, 5e-07, 6.25e-06), tokens) is None
    minutes = Tokens(10, 0, 100, 5)
    assert cost(Prices(5e-06, 2.5e-05, 5e-07, 6.25e-06), minutes) == pytest.approx(
        10 * 5e-06 + 100 * 6.25e-06 + 5 * 2.5e-05
    )


# --- The engine ----------------------------------------------------------------------


def test_an_answer_without_a_compaction_has_none() -> None:
    plain = AssistantMessage(
        "Done.", native=json.dumps({"content": [{"type": "text", "text": "Done."}]}).encode(),
        native_format="messages",
    )  # fmt: skip
    assert compaction(plain) is None
    assert compaction({"role": "user", "content": "x"}) is None


# --- Settings ------------------------------------------------------------------------


def test_the_settings_take_a_trigger_only_in_the_compaction_mode() -> None:
    assert Settings(context_mode="skeleton").skeleton_only
    assert not Settings(context_mode="provider_compaction").skeleton_only
    with pytest.raises(ValueError, match="provider_compaction"):
        Settings(compaction_trigger=60_000)
    with pytest.raises(ValueError, match="50,000"):
        Settings(context_mode="provider_compaction", compaction_trigger=10_000)
    with pytest.raises(ValueError, match="context mode"):
        Settings(context_mode=cast(Any, "summaries"))


def test_the_confined_runtime_refuses_the_compaction_mode() -> None:
    from sandbox_science.orchestrator.confinement.server import ControlSettings

    base: Json = {
        "state_dir": "/state", "worker": "http://127.0.0.1:1", "worker_token_file": "/t",
        "runtime_token_file": "/r", "generation": "g1", "projects": ["p"], "main": "claude",
        "image_revision": "r",
    }  # fmt: skip
    ControlSettings.model_validate({**base, "settings": {"context_mode": "skeleton"}})
    with pytest.raises(ValueError, match="confined agent runtime"):
        ControlSettings.model_validate(
            {**base, "settings": {"context_mode": "provider_compaction"}}
        )


# --- The harness, through the command line -------------------------------------------

MESSAGES = ["--api", "main=messages", "--context-mode", "provider_compaction"]


async def test_a_run_in_the_compaction_mode_compacts_and_never_folds(harness: Harness) -> None:
    """A run at C = 100,000 sends each call the edit at 75,000; a compaction comes back
    and the run goes on from it; nothing folds, and the run's record keeps the mode, the
    trigger and the compaction, its call's tokens and cost every iteration's. The next
    run starts its requests at the compaction."""
    harness.gateway.served = {"main": "anthropic/claude-opus-4-8"}
    shell = [call("shell", script="echo hello")]
    record = await harness.run(
        "run-1",
        [Scripted("Looking.", calls=shell), Scripted("Again.", calls=shell, compact=SUMMARY),
         Scripted("Done.")],
        options=[*MESSAGES, "--context-ceiling", "100000"],
    )  # fmt: skip
    found = harness.gateway
    assert len(found.claude) == 3
    for body, betas in zip(found.claude, found.claude_betas, strict=True):
        (edit,) = body["context_management"]["edits"]
        assert edit["trigger"] == {"type": "input_tokens", "value": 75_000}
        assert COMPACT_BETA in betas
    last = found.claude[-1]["messages"]
    assert last[0]["role"] == "assistant"
    assert last[0]["content"][0] == {"type": "compaction", "content": SUMMARY}
    assert record.final_text == "Done."
    summary = cast(Json, record.summaries["folds"])
    assert summary["mode"] == "provider_compaction"
    assert summary["geometry"] == {"ceiling": 100_000, "trigger": 75_000}
    assert summary["compactions_total"] == 1
    (made,) = summary["compactions"]
    assert made["iterations"][0]["cache_1h_input_tokens"] == 60_000
    usage = record.usage[1]
    assert usage.cache_creation_input_tokens == 60_000
    assert usage.output_tokens == 201
    assert "folds_total" not in summary  # no fold engine ran

    second = await harness.run(
        "run-2", [Scripted("Still done.")], options=[*MESSAGES, "--context-ceiling", "100000"]
    )
    assert len(found.claude) == 4
    body = found.claude[-1]
    assert body["messages"][0]["content"][0] == {"type": "compaction", "content": SUMMARY}
    assert cast(Json, second.summaries["folds"])["compactions_total"] == 0


async def test_a_gpt_alias_is_refused_in_the_compaction_mode(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mode is Claude's, on the Messages route: a run on an alias called through
    another API fails before its first call, saying why."""
    token, key = harness.tmp / "worker.token", harness.tmp / "gateway.key"
    token.write_text(TOKEN)
    key.write_text(API_KEY)
    argv = [
        "run", "--worker", harness.worker.url, "--token-file", str(token),
        "--project", PROJECT, "--session", SESSION, "--state-dir", str(harness.state),
        "--allow-unisolated", "--model-url", harness.gateway.url, "--api-key-file", str(key),
        "--model", "main", "--api", "main=responses", "--context-mode", "provider_compaction",
        "Go.",
    ]  # fmt: skip
    assert await asyncio.to_thread(cli.main, argv) == 1
    said = capsys.readouterr()
    assert "for aliases called through the Messages API: main is not" in said.out + said.err
    assert harness.gateway.responses == []
    assert harness.gateway.claude == []


async def test_a_delegated_run_compacts_as_its_root(delegating: Harness) -> None:
    """A delegated run is in its root's mode: its calls carry the edit at its own
    trigger, it records its compactions apart, and its brief stays in view after one,
    pinned in its state block."""
    delegating.gateway.served = {"main": "anthropic/claude-opus-4-8"}
    root = await delegating.run(
        "root",
        [Scripted(calls=[DELEGATE]), wait(), Scripted("Done.")],
        delegated=[
            Scripted("Working.", calls=[call("shell", script="echo 1")], compact=SUMMARY),
            Scripted(calls=[call("submit_result", **CHILD_DONE)]),
            Scripted("Done."),
        ],
        options=[*MESSAGES, "--context-ceiling", "100000"],
    )
    (child,) = children(delegating, root)
    summary = cast(Json, child.summaries["folds"])
    assert summary["mode"] == "provider_compaction"
    assert summary["compactions_total"] == 1
    bodies = [
        body for body in delegating.gateway.claude
        if str(body["system"][0]["text"]).startswith("You are a delegated")
    ]  # fmt: skip
    assert len(bodies) == 3
    assert all("context_management" in body for body in bodies)
    after = bodies[1]["messages"]
    assert after[0]["content"][0]["type"] == "compaction"
    pinned = [b["text"] for b in after[1]["content"] if b.get("type") == "text"]
    assert any("Delegated work brief (unchanged)" in text for text in pinned)
