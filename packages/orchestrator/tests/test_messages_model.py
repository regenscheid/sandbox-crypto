# SPDX-License-Identifier: MIT
"""Claude through the gateway's ``/v1/messages`` (docs/specs/context-management.md,
CM-38, CM-38a, CM-39, CM-42, CM-28, CM-29a): CM-F10's wire, checked on the requests
themselves, and the parts of CM-F12, CM-F14, CM-F24 and CM-F28 that are Claude's.

The gateway is a fake LiteLLM in this process. It answers from a script, as Anthropic's
API would: signed thinking blocks, empty unless ``display`` asks for summaries or
progress notes; usage that counts the replayed thinking; and Anthropic's rules for a
request, the binding of thinking to the history before it among them, which it checks on
every request and refuses with Anthropic's errors. Its key is synthetic, and retries
sleep on a clock that only records the waits.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.history import Archive, entry_of
from sandbox_science.orchestrator.hooks import Appended
from sandbox_science.orchestrator.messages import (
    BINDING_BETA,
    MAX_TOKENS,
    MESSAGES,
    NOTE,
    TOOLS_BETA,
    UPDATES_BETA,
    MessagesModel,
    ThinkingReplay,
)
from sandbox_science.orchestrator.model import (
    AssistantMessage,
    ContextLimitError,
    GatewayBudgetError,
    Message,
    ModelError,
    ModelGateway,
    ModelTurn,
    OpenAICompatibleModel,
    ToolSpec,
    gateway_model,
)
from sandbox_science.orchestrator.pairing import ABORTED
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.responses import ResponsesModel

from .loop_harness import API_KEY, GATEWAY_VERSION, PROJECT, SESSION, Harness, call
from .loop_harness import Reply as Scripted
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_model_limits import Waits

KEY = "test-secret-messages-gateway-key"
VERSION = "1.107.0"
Json = dict[str, Any]
SYSTEM = "You are a research agent."
TASK = "Measure the thing."
SHELL = ToolSpec("shell", "Run a script.", {"type": "object", "properties": {}})
READ = ToolSpec("read", "Read a file.", {"type": "object", "properties": {}})
SEARCH = ToolSpec("crypto_search", "Search papers.", {"type": "object", "properties": {}})
TOOLS = [SHELL, READ]
TODAY = Waits().now().date()
SETTING = ThinkingReplay("sha256:" + "a" * 64, VERSION, TODAY, "cm-f30-test-run")
THINKING = {"thinking", "redacted_thinking"}


@dataclass
class Reply:
    """One scripted answer: progress notes, one before each call (or before the text),
    a signed reasoning block of ``thinking`` tokens, calls and text."""

    text: str | None = None
    calls: Sequence[tuple[str, Json]] = ()
    notes: Sequence[str] = ()
    thinking: int = 2000
    summary: str = "Weighed the two orders before choosing one."
    status: int = 200
    body: str | None = None
    """An answer sent as it is, instead."""
    headers: dict[str, str] = field(default_factory=dict[str, str])


def unthought(content: Sequence[Json]) -> list[Json]:
    return [
        {k: v for k, v in block.items() if k != "cache_control"}
        for block in content
        if block.get("type") not in THINKING
    ]


def plain(messages: Sequence[Json]) -> list[Json]:
    """Messages as a cache and a thinking block see their prefix: no markers."""
    return [{**m, "content": unthought(m["content"])} for m in messages]


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass
class Fake:
    """A LiteLLM proxy's ``/v1/messages`` in front of Anthropic's API, its
    ``/v1/model/info``, and the other two endpoints for answers that cross adapters."""

    url: str = ""
    models: dict[str, str] = field(
        default_factory=lambda: {
            "claude": "anthropic/claude-opus-5-5",
            "fable": "anthropic/claude-fable-5-1",
            "older": "anthropic/claude-opus-5",
            "sonnet": "anthropic/claude-sonnet-5",
            "haiku": "anthropic/claude-haiku-4-5",
        }
    )
    script: list[Reply] = field(default_factory=list[Reply])
    bodies: list[Json] = field(default_factory=list[Json])
    betas: list[set[str]] = field(default_factory=list[set[str]])
    answered: list[str] = field(default_factory=list[str])
    paths: list[str] = field(default_factory=list[str])
    version: str | None = VERSION
    forwards_beta: bool = True
    """Passes ``anthropic-beta`` on, which LiteLLM 1.102.1 is not known to do."""
    rebound: bool = False
    """Takes every thinking block sent back for one bound to another conversation."""
    drops: bool = False
    """Strips every thinking block before the model sees it, as LiteLLM 1.102.1 may."""
    info_status: int = 200
    info_calls: int = 0
    bound: dict[str, tuple[str, str | None]] = field(
        default_factory=dict[str, tuple[str, str | None]]
    )
    """Each signature's prefix, and the thinking block before it."""
    tokens: dict[str, int] = field(default_factory=dict[str, int])
    numbered: int = 0

    def then(self, *replies: Reply) -> None:
        self.script.extend(replies)

    async def info(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        self.info_calls += 1
        if self.info_status != 200:
            return web.Response(status=self.info_status, text="no")
        data = [{"model_name": alias, "litellm_params": {"model": model}, "model_info": {}}
                for alias, model in self.models.items()]  # fmt: skip
        return web.json_response({"data": data})

    async def other(self, request: web.Request) -> web.Response:
        """Chat completions and responses, answered "ok"."""
        self.paths.append(request.path)
        self.bodies.append(cast(Json, await request.json()))
        if request.path.endswith("/responses"):
            item = {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}]}  # fmt: skip
            return web.json_response({"status": "completed", "output": [item], "usage": {}})
        return web.json_response({"choices": [{"message": {"content": "ok"}}]})

    async def handle(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        self.paths.append(request.path)
        body = cast(Json, await request.json())
        betas = {b for b in request.headers.get("anthropic-beta", "").split(",") if b}
        if not self.forwards_beta:
            betas = set()
        self.bodies.append(body)
        self.betas.append(betas)
        reply = self.script[0]
        headers = {"Content-Type": "application/json", **reply.headers}
        if self.version is not None:
            headers["x-litellm-version"] = self.version
        if reply.body is not None or reply.status != 200:
            self.script.pop(0)
            return web.Response(status=reply.status, text=reply.body or "", headers=headers)
        if self.drops:
            body = {**body, "messages": [{**m, "content": [b for b in m["content"]
                                                           if b.get("type") not in THINKING]}
                                         for m in body["messages"]]}  # fmt: skip
        if (refused := self.refused(body, betas)) is not None:
            error = {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": refused},
            }
            return web.Response(status=400, text=json.dumps(error), headers=headers)
        self.script.pop(0)
        text = json.dumps(self.answer(body, reply))
        self.answered.append(text)
        return web.Response(text=text, headers={**headers, "x-litellm-response-cost": "0.01"})

    def fingerprint(self, body: Json, upto: int) -> str:
        """What a thinking block is bound to: the system message, the tools as a set
        less the deferred ones, and the messages before it, all without thinking."""
        system = [block["text"] for block in body.get("system", [])]
        tools = sorted(
            ({k: v for k, v in t.items() if k != "cache_control"} for t in body.get("tools", [])
             if not t.get("defer_loading")),
            key=lambda t: str(t["name"]),
        )  # fmt: skip
        return canonical([system, tools, plain(body["messages"][:upto])])

    def refused(self, body: Json, betas: set[str]) -> str | None:
        """Why Anthropic's API would refuse the request, if it would."""
        thinking = body.get("thinking", {})
        if thinking.get("display") == "updates" and UPDATES_BETA not in betas:
            return "thinking.display: Input should be 'summarized' or 'omitted'"
        if "block_binding" in thinking and BINDING_BETA not in betas:
            return "thinking.block_binding: Extra inputs are not permitted"
        deferred = {t["name"] for t in body.get("tools", []) if t.get("defer_loading")}
        markers = sum("cache_control" in t for t in body.get("tools", []))
        markers += sum("cache_control" in b for b in body.get("system", []))
        messages = body["messages"]
        if not messages or messages[0]["role"] != "user":
            return "messages: the first message must use the user role"
        previous: str | None = None
        for index, message in enumerate(messages):
            content = message["content"]
            markers += sum("cache_control" in block for block in content)
            if not content:
                return f"messages.{index}: content is empty"
            if any(b.get("type") == "text" and not b.get("text") for b in content):
                return f"messages.{index}: text content blocks must be non-empty"
            kinds = [block["type"] for block in content]
            others = next((i for i, kind in enumerate(kinds) if kind != "tool_result"), len(kinds))
            if "tool_result" in kinds[others:]:
                return f"messages.{index}: tool_result blocks must come first"
            if message["role"] == "system":
                if TOOLS_BETA not in betas:
                    return f"messages.{index}: tool_addition needs its beta"
                if messages[index - 1]["role"] != "user" or (
                    index + 1 < len(messages) and messages[index + 1]["role"] != "assistant"
                ):
                    return f"messages.{index}: a system message is misplaced"
                if any(b["tool"]["name"] not in deferred for b in content):
                    return f"messages.{index}: a tool_addition names a tool not deferred"
            if message["role"] == "assistant":
                calls = [b["id"] for b in content if b["type"] == "tool_use"]
                after = messages[index + 1]["content"] if index + 1 < len(messages) else []
                answered = [b.get("tool_use_id") for b in after if b["type"] == "tool_result"]
                if calls and sorted(calls) != sorted(answered):
                    return f"messages.{index}: tool_use ids without tool_result blocks"
                for block in content:
                    if block["type"] not in THINKING:
                        continue
                    found = self.bound.get(block.get("signature", ""))
                    if found is None:
                        return f"messages.{index}: Invalid `signature` in `thinking` block."
                    if self.rebound or found[0] != self.fingerprint(body, index):
                        return (
                            f"messages.{index}: Invalid `signature` in `thinking` block. The "
                            "block is bound to a different conversation."
                        )
                    if previous is not None and found[1] != previous:
                        return f"messages.{index}: a thinking block was removed before this one"
                    previous = block["signature"]
        if markers > 4:
            return "A maximum of 4 blocks with cache_control may be provided."
        return None

    def answer(self, body: Json, reply: Reply) -> Json:
        display = body.get("thinking", {}).get("display", "omitted")
        replayed = [b["signature"] for m in body["messages"] for b in m["content"]
                    if b.get("type") in THINKING]  # fmt: skip
        prefix = self.fingerprint(body, len(body["messages"]))
        before = replayed[-1] if replayed else None
        blocks: list[Json] = []

        def think(text: str, tokens: int) -> None:
            nonlocal before
            self.numbered += 1
            signature = f"sig-{self.numbered}-{'x' * 40}"
            self.bound[signature], self.tokens[signature] = (prefix, before), tokens
            before = signature
            blocks.append({"type": "thinking", "thinking": text, "signature": signature})

        think(reply.summary if display == "summarized" else "", reply.thinking)
        notes = list(reply.notes)
        for name, arguments in reply.calls:
            if notes:
                note = notes.pop(0)
                think(note if display == "updates" else "", len(note) // 4)
            self.numbered += 1
            blocks.append({"type": "tool_use", "id": f"toolu_{self.numbered:04d}", "name": name,
                           "input": arguments})  # fmt: skip
        for note in notes:
            think(note if display == "updates" else "", len(note) // 4)
        if reply.text is not None:
            blocks.append({"type": "text", "text": reply.text})
        total = (
            len(canonical([body.get("system"), body.get("tools"), plain(body["messages"])])) // 4
        )
        total += sum(self.tokens[s] for s in replayed)
        shown = sum(len(json.dumps(b)) for b in blocks if b["type"] not in THINKING) // 4
        output = sum(self.tokens[b["signature"]] for b in blocks if b["type"] in THINKING) + shown
        cached = total * 3 // 4
        return {
            "id": f"msg_{self.numbered:04d}", "type": "message", "role": "assistant",
            "model": self.models[body["model"]].split("/")[-1], "content": blocks,
            "stop_reason": "tool_use" if reply.calls else "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": total - cached - total // 8,
                      "cache_read_input_tokens": cached,
                      "cache_creation_input_tokens": total // 8, "output_tokens": output},
        }  # fmt: skip


@pytest_asyncio.fixture
async def fake() -> AsyncIterator[Fake]:
    found = Fake()
    app = web.Application()
    app.router.add_post("/v1/messages", found.handle)
    app.router.add_get("/v1/model/info", found.info)
    app.router.add_post("/v1/responses", found.other)
    app.router.add_post("/v1/chat/completions", found.other)
    async with TestServer(app) as server:
        found.url = str(server.make_url("/v1"))
        yield found


def claude(fake: Fake, alias: str = "claude", **options: Any) -> MessagesModel:
    return MessagesModel(fake.url, alias, KEY, clock=Waits(), **options)


def tool(call_id: str, text: str) -> Message:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


async def converse(
    model: MessagesModel,
    replies: Sequence[Reply],
    fake: Fake,
    *,
    tools: Callable[[int], Sequence[ToolSpec]] = lambda _: TOOLS,
    messages: list[Message] | None = None,
) -> tuple[list[Message], list[ModelTurn]]:
    """A run as the loop makes one: each answer added, then a result for each call."""
    fake.then(*replies)
    messages = messages if messages is not None else [
        {"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}
    ]  # fmt: skip
    turns: list[ModelTurn] = []
    for number in range(len(replies)):
        turn = await model.complete(messages, tools(number))
        turns.append(turn)
        messages.append(AssistantMessage.of(turn))
        messages.extend(tool(call.id, f"{call.name} said {number}") for call in turn.tool_calls)
    return messages, turns


def thoughts(body: Json) -> list[Json]:
    return [b for m in body["messages"] for b in m["content"] if b.get("type") in THINKING]


def texts(body: Json) -> list[str]:
    return [b["text"] for m in body["messages"] for b in m["content"] if b.get("type") == "text"]


LOOKING = Reply(calls=[("shell", {"script": "ls"}), ("read", {"path": "x"})],
                notes=["Listing the files first.", "Then reading x."])  # fmt: skip
COUNTING = Reply(calls=[("shell", {"script": "wc x"})], notes=["Counting x's lines."])
DONE = Reply(text="x has 42 lines.")


async def test_requests_ask_for_progress_notes_on_the_models_that_have_them(fake: Fake) -> None:
    """CM-F10, CM-38: Opus 5.5 is asked for ``display: "updates"`` with its beta
    header, a model without it for ``"summarized"``; nothing is replayed, by default;
    the system message, the tools and the last user turn carry cache breakpoints."""
    for alias, display in (("claude", "updates"), ("older", "summarized")):
        model = claude(fake, alias)
        try:
            await converse(model, [Reply(text="Hello.")], fake)
        finally:
            await model.close()
        body, betas = fake.bodies[-1], fake.betas[-1]
        assert fake.paths[-1] == "/v1/messages"
        assert body["thinking"] == {"type": "adaptive", "display": display}
        assert betas == ({UPDATES_BETA} if display == "updates" else set())
        assert (body["model"], body["max_tokens"]) == (alias, MAX_TOKENS)
        assert body["system"] == [
            {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
        ]
        assert [t["name"] for t in body["tools"]] == ["shell", "read"]
        assert body["tools"][0] == {"name": "shell", "description": "Run a script.",
                                    "input_schema": SHELL.parameters}  # fmt: skip
        assert body["tools"][1]["cache_control"] == {"type": "ephemeral"}
        assert body["messages"] == [{"role": "user", "content": [
            {"type": "text", "text": TASK, "cache_control": {"type": "ephemeral"}}]}]  # fmt: skip
    assert fake.info_calls == 2  # once for each model


async def test_with_replay_off_no_thinking_goes_back_and_notes_go_as_text_from_the_start(
    fake: Fake,
) -> None:
    """CM-F10, CM-38a, CM-F14 (Claude's part): with replay off, the default, no thinking
    block and no ``block_binding`` is sent; each progress note goes back as a text
    block marked as the agent's, from the first time its answer goes back, rendered the
    same on every later request, so each request begins as the one before it did."""
    model = claude(fake)
    try:
        messages, turns = await converse(model, [LOOKING, COUNTING, DONE], fake)
    finally:
        await model.close()
    assert turns[0].text is None  # the notes are not an answer
    assert turns[0].notes == ("Listing the files first.", "Then reading x.")
    assert messages[2]["content"] == f"{NOTE}Listing the files first.\n\n{NOTE}Then reading x."
    assert turns[-1].text == "x has 42 lines."
    assert {t.thinking_replay for t in turns} == {"off: no thinking replay setting"}
    for body, betas in zip(fake.bodies, fake.betas, strict=True):
        assert thoughts(body) == []
        assert "block_binding" not in body["thinking"]
        assert BINDING_BETA not in betas
    first, second, third = (plain(body["messages"]) for body in fake.bodies)
    assert second[: len(first)] == first
    assert third[: len(second)] == second
    looked = second[1]["content"]
    assert [b["type"] for b in looked] == ["text", "tool_use", "text", "tool_use"]
    assert looked[0]["text"] == f"{NOTE}Listing the files first."
    assert third[1] == second[1]
    assert f"{NOTE}Counting x's lines." in texts(fake.bodies[2])
    # The answers' calls went back as Claude made them, each with its result after it.
    made = json.loads(fake.answered[0])["content"]
    assert [b for b in looked if b["type"] == "tool_use"] == [
        b for b in made if b["type"] == "tool_use"
    ]
    assert [b["type"] for b in second[2]["content"]] == ["tool_result", "tool_result"]
    assert len(messages) == 2 + 3 + 3  # the task, three answers, three results


async def test_with_replay_on_signed_thinking_goes_back_byte_for_byte(fake: Fake) -> None:
    """CM-F10, CM-39: with replay on and the gateway reporting the setting's version,
    the run's signed thinking blocks, empty ones included, go back as Claude made them,
    with ``block_binding`` and its header, and the fake's binding check accepts each."""
    model = claude(fake, replay=SETTING)
    try:
        _, turns = await converse(model, [LOOKING, COUNTING, DONE], fake)
    finally:
        await model.close()
    assert [t.thinking_replay for t in turns] == ["on", "on", "on"]
    last, betas = fake.bodies[-1], fake.betas[-1]
    assert last["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "error"}
    assert BINDING_BETA in betas
    assert UPDATES_BETA in betas
    made = [json.loads(text)["content"] for text in fake.answered[:2]]
    assert last["messages"][1]["content"] == made[0]
    assert last["messages"][3]["content"] == made[1]
    assert [b["thinking"] for b in thoughts(last)][:1] == [""]  # the empty reasoning block
    assert not any(NOTE in text for text in texts(last))


async def test_a_changed_history_takes_back_only_the_thinking_still_bound_to_it(
    fake: Fake,
) -> None:
    """CM-38, CM-38a: when an earlier result changes, as a stub would change it, the
    answers after it go back without their thinking, their notes as text, and those
    before it with theirs; thinking is never left out between two blocks sent."""
    model = claude(fake, replay=SETTING)
    try:
        messages, _ = await converse(model, [LOOKING, COUNTING], fake)
        # The system message, the task, the first answer, then its first result.
        assert messages[3]["role"] == "tool"
        messages[3] = {**messages[3], "content": "[stubbed: 2,000 characters]"}
        fake.then(DONE)
        turn = await model.complete(messages, TOOLS)
    finally:
        await model.close()
    assert turn.thinking_replay == "on"
    body = fake.bodies[-1]
    first, second = body["messages"][1]["content"], body["messages"][3]["content"]
    assert first == json.loads(fake.answered[0])["content"]
    assert [b["type"] for b in second] == ["text", "tool_use"]
    assert second[0]["text"] == f"{NOTE}Counting x's lines."


@pytest.mark.parametrize(
    ("versions", "records"),
    [
        (["1.107.0-rc.1"] * 3, ["on", "off", "off"]),
        ([VERSION, "1.107.1", VERSION], ["on", "on", "off"]),
        ([None, VERSION, VERSION], ["on", "off", "off"]),
    ],
)
async def test_another_version_than_the_settings_turns_replay_off_for_the_run(
    fake: Fake, versions: list[str | None], records: list[str]
) -> None:
    """CM-F10, CM-39: a header that names a release candidate, or that changes during
    the run, or none, turns replay off for the rest of the run, recorded with both."""
    model = claude(fake, replay=SETTING)
    turns: list[ModelTurn] = []
    messages: list[Message] = [{"role": "user", "content": TASK}]
    try:
        for version, reply in zip(versions, [LOOKING, COUNTING, DONE], strict=True):
            fake.version = version
            turns.extend((await converse(model, [reply], fake, messages=messages))[1])
    finally:
        await model.close()
    assert [(t.thinking_replay or "")[:3] for t in turns] == [r[:3] for r in records]
    turned = next(t.thinking_replay for t in turns if (t.thinking_replay or "").startswith("off"))
    got = next(v for v in versions if v != VERSION) or "no version"
    assert turned == f"off: the gateway reported {got}, the setting names {VERSION}"
    off = [b for b, t in zip(fake.bodies, turns, strict=True) if t.thinking_replay != "on"]
    assert all(thoughts(b) == [] and "block_binding" not in b["thinking"] for b in off)
    assert all(f"{NOTE}Listing the files first." in texts(b) for b in off)


async def test_a_binding_refusal_is_answered_again_without_thinking(fake: Fake) -> None:
    """CM-39, Anthropic's documented recovery: a request refused because the thinking
    it sent back is bound to another conversation goes again once with no thinking from
    the history, and replay stays off for the rest of the run, recorded."""
    model = claude(fake, replay=SETTING)
    try:
        messages, turns = await converse(model, [LOOKING], fake)
        fake.rebound = True
        _, more = await converse(model, [COUNTING, DONE], fake, messages=messages)
    finally:
        await model.close()
    turns += more
    refused, retried, last = fake.bodies[1:]
    assert thoughts(refused)  # sent, and refused with a 400
    for body in (retried, last):
        assert thoughts(body) == []
        assert "block_binding" not in body["thinking"]
    assert f"{NOTE}Listing the files first." in texts(retried)
    assert turns[0].thinking_replay == "on"
    refusal = "off: the API refused the thinking sent back as bound elsewhere"
    assert [t.thinking_replay for t in turns[1:]] == [refusal, refusal]  # for the run


async def test_a_gateway_that_drops_the_beta_header_gets_summaries(fake: Fake) -> None:
    """CM-F31's risk: when the gateway does not pass ``anthropic-beta`` on, the API
    refuses ``display: "updates"``; the client asks again for summaries, and keeps to
    them, rather than failing every call on the alias."""
    fake.forwards_beta = False
    model = claude(fake)
    try:
        _, turns = await converse(model, [LOOKING, DONE], fake)
    finally:
        await model.close()
    displays = [body["thinking"]["display"] for body in fake.bodies]
    assert displays == ["updates", "summarized", "summarized"]
    assert turns[0].notes == ()
    assert turns[1].text == "x has 42 lines."


async def test_progress_notes_are_not_the_answer(fake: Fake) -> None:
    """CM-38a: a last turn's progress notes, Anthropic's stand-in for an interrupted
    part among them, stay out of the answer the user gets, which is its text blocks,
    and stay in what the conversation holds, which the archive indexes and other
    adapters send."""
    cut = "This part of the response was interrupted before it finished."
    model = claude(fake)
    try:
        messages, (turn,) = await converse(
            model, [Reply(text="The order is 42.", notes=["Checking the order.", cut])], fake
        )
    finally:
        await model.close()
    assert turn.text == "The order is 42."
    assert turn.notes == ("Checking the order.", cut)
    assert messages[-1]["content"] == (
        f"{NOTE}Checking the order.\n\n{NOTE}{cut}\n\nThe order is 42."
    )


async def test_claude_models_before_adaptive_thinking_are_sent_none(fake: Fake) -> None:
    model = claude(fake, "haiku")
    try:
        await converse(model, [Reply(text="Hello.")], fake)
    finally:
        await model.close()
    assert "thinking" not in fake.bodies[-1]


async def test_a_setting_over_thirty_days_old_leaves_replay_off(fake: Fake) -> None:
    old = replace(SETTING, verified=TODAY - timedelta(days=31))
    model = claude(fake, replay=old)
    try:
        _, turns = await converse(model, [LOOKING, DONE], fake)
    finally:
        await model.close()
    assert {t.thinking_replay for t in turns} == {
        f"off: the setting's check of {old.verified} is over 30 days old"
    }
    assert thoughts(fake.bodies[-1]) == []
    assert ThinkingReplay("i", "v", TODAY - timedelta(days=30), "r").lapsed(TODAY) is None


async def test_a_gateway_that_drops_thinking_turns_replay_off(fake: Fake) -> None:
    """CM-F10, CM-39: a scripted strip-and-retry, whose reported input falls short by
    the replayed thinking's size, turns replay off for the rest of the run, recorded."""
    fake.drops = True
    model = claude(fake, replay=SETTING)
    try:
        _, turns = await converse(model, [LOOKING, COUNTING, DONE], fake)
    finally:
        await model.close()
    assert [t.thinking_replay for t in turns[:2]] == ["on", "on"]
    reported = turns[1].input_tokens
    assert turns[2].thinking_replay is not None
    assert turns[2].thinking_replay.startswith(
        f"off: the gateway dropped thinking: {reported:,} input tokens reported, about "
    )
    assert thoughts(fake.bodies[2]) == []
    assert "block_binding" not in fake.bodies[2]["thinking"]


async def test_without_a_drop_the_check_stays_quiet(fake: Fake) -> None:
    """The estimate's tolerance: a long result after an answer does not read as thinking
    dropped (CM-39)."""
    model = claude(fake, replay=SETTING)
    messages: list[Message] = [{"role": "user", "content": TASK}]
    try:
        await converse(model, [LOOKING], fake, messages=messages)
        messages[-1] = {**messages[-1], "content": "word " * 20_000}
        _, turns = await converse(model, [COUNTING, DONE], fake, messages=messages)
    finally:
        await model.close()
    assert [t.thinking_replay for t in turns] == ["on", "on"]


async def test_tools_loaded_mid_run_are_deferred_and_announced(fake: Fake) -> None:
    """CM-F10, CM-38: a connector tool loaded during the run is declared with
    ``defer_loading`` and announced by a ``tool_addition`` where it first appeared,
    never by changing the tool list, so the thinking before it still goes back."""
    model = claude(fake, replay=SETTING)
    loaded = [SHELL, READ, SEARCH]
    try:
        _, turns = await converse(
            model, [LOOKING, COUNTING, DONE], fake, tools=lambda n: TOOLS if n == 0 else loaded
        )
    finally:
        await model.close()
    assert [t.thinking_replay for t in turns] == ["on", "on", "on"]
    second, third = fake.bodies[1], fake.bodies[2]
    for body, betas in zip((second, third), fake.betas[1:], strict=True):
        assert [(t["name"], t.get("defer_loading")) for t in body["tools"]] == [
            ("shell", None), ("read", None), ("crypto_search", True)
        ]  # fmt: skip
        assert TOOLS_BETA in betas
    reference = {"type": "tool_reference", "name": "crypto_search"}
    added = {"role": "system", "content": [{"type": "tool_addition", "tool": reference}]}
    assert second["messages"][-1] == added
    assert third["messages"][3] == added  # where it first appeared, every time
    assert plain(third["messages"][:4]) == plain(second["messages"])
    assert thoughts(third)  # the answers' thinking still goes back
    assert TOOLS_BETA not in fake.betas[0]


async def test_a_tool_change_the_model_cannot_take_is_a_rebuild(fake: Fake) -> None:
    """CM-38: on a model without mid-conversation tool changes, the tool list changes,
    and the thinking before the change no longer goes back; the thinking after it
    does, so thinking is left out only from the run's start."""
    model = claude(fake, "sonnet", replay=SETTING)
    loaded = [SHELL, READ, SEARCH]
    try:
        _, turns = await converse(
            model, [LOOKING, COUNTING, DONE], fake, tools=lambda n: TOOLS if n == 0 else loaded
        )
    finally:
        await model.close()
    assert [t.thinking_replay for t in turns] == ["on", "on", "on"]
    second, third = fake.bodies[1], fake.bodies[2]
    for body in (second, third):
        assert [t["name"] for t in body["tools"]] == ["shell", "read", "crypto_search"]
        assert not any(t.get("defer_loading") for t in body["tools"])
    assert thoughts(second) == []
    made = json.loads(fake.answered[1])["content"]
    assert thoughts(third) == [b for b in made if b["type"] in THINKING]
    assert [b["type"] for b in third["messages"][1]["content"]] == ["tool_use", "tool_use"]
    assert third["messages"][3]["content"] == made


async def test_native_items_cross_no_provider_and_pass_between_claude_models(
    fake: Fake,
) -> None:
    """CM-F12, CM-42: Claude's answers go to GPT and to chat completions as text, their
    progress notes included, and calls, with no thinking; another adapter's answer goes
    to Claude as its text and calls; and between Claude models thinking blocks go back
    as they are, for the API to leave out what the other model cannot read."""
    model = claude(fake, replay=SETTING)
    try:
        messages, _ = await converse(model, [LOOKING], fake)
    finally:
        await model.close()
    looked = messages[2]
    gpt = ResponsesModel(fake.url, "gpt", KEY, clock=Waits())
    chat = OpenAICompatibleModel(fake.url, "other", KEY, clock=Waits())
    try:
        await gpt.complete(messages, TOOLS)
        await chat.complete(messages, TOOLS)
    finally:
        await gpt.close()
        await chat.close()
    sent_to_gpt, sent_to_chat = fake.bodies[-2]["input"], fake.bodies[-1]["messages"]
    assert sent_to_gpt[1] == {"type": "message", "role": "assistant", "content": looked["content"]}
    assert not any(item.get("type") == "reasoning" for item in sent_to_gpt)
    assert sent_to_chat[2] == dict(looked)
    assert NOTE + "Listing the files first." in looked["content"]
    # GPT's answer, then to another Claude model, which gets Opus's thinking as it is.
    gpts = AssistantMessage("From GPT.", native=b'{"output": []}', native_format="responses")
    fable = claude(fake, "fable", replay=SETTING)
    try:
        _, turns = await converse(
            fable, [DONE], fake, messages=[*messages, gpts, {"role": "user", "content": "Go on."}]
        )
    finally:
        await fable.close()
    body = fake.bodies[-1]
    assert body["messages"][1]["content"] == json.loads(fake.answered[0])["content"]
    assert body["messages"][3] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "From GPT."}],
    }
    assert turns[0].thinking_replay == "on"


async def test_calls_and_results_are_paired_before_they_are_sent(fake: Fake) -> None:
    """CM-29a, CM-F28 (Claude's part): an interrupted call gets a result saying it was
    aborted; a result whose call is gone is left out; results come first in their turn."""
    model = claude(fake)
    try:
        messages, _ = await converse(model, [LOOKING], fake)
        answer = messages[2]
        interrupted: list[Message] = [
            {"role": "user", "content": TASK},
            tool("call_gone", "an output whose call is gone"),
            answer,
            {"role": "user", "content": "A notice from the platform."},
            messages[3],
        ]
        fake.then(DONE)
        await model.complete(interrupted, TOOLS)
    finally:
        await model.close()
    sent = fake.bodies[-1]["messages"]
    calls = [b["id"] for b in sent[1]["content"] if b["type"] == "tool_use"]
    results = sent[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result", "text"]
    assert {b["tool_use_id"]: b["content"] for b in results[:2]} == {
        calls[0]: "shell said 0",
        calls[1]: ABORTED,
    }
    assert "call_gone" not in json.dumps(sent)
    assert len(sent) == 3


async def test_a_message_is_read_as_a_turn_and_kept_as_the_gateway_sent_it(fake: Fake) -> None:
    """CM-5, CM-28, CM-F24 (Claude's part): 2,000 uncached tokens, 400,000 read from the
    cache and 10,000 written to it are 412,000 tokens of input; the turn keeps the answer
    byte for byte, its cost, model and the gateway's version."""
    usage = {"input_tokens": 2000, "cache_read_input_tokens": 400_000,
             "cache_creation_input_tokens": 10_000, "output_tokens": 345}  # fmt: skip
    content = [
        {"type": "redacted_thinking", "data": "EuoBCkQ"},
        {"type": "text", "text": "Partial."},
    ]
    text = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "usage": usage,
                       "model": "claude-opus-5-5", "stop_reason": "max_tokens",
                       "content": content})  # fmt: skip
    fake.then(Reply(body=text, headers={"x-litellm-response-cost": "0.125"}))
    model = claude(fake)
    try:
        turn = await model.complete([{"role": "user", "content": TASK}], [])
    finally:
        await model.close()
    assert (turn.input_tokens, turn.output_tokens) == (412_000, 345)
    assert (turn.text, turn.finish_reason, turn.model) == ("Partial.", "length", "claude-opus-5-5")
    assert (turn.cost, turn.gateway_version) == (0.125, VERSION)
    assert (turn.native, turn.native_format) == (text.encode(), MESSAGES)
    assert "tools" not in fake.bodies[-1]


def error(message: str, kind: str = "invalid_request_error") -> str:
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}})


@pytest.mark.parametrize(
    ("status", "body", "raised"),
    [
        (400, error("prompt is too long: 1050000 tokens > 1000000 maximum"), ContextLimitError),
        (400, error('litellm.BadRequestError: AnthropicException - {"type":"error","error":'
                    '{"type":"invalid_request_error","message":"prompt is too long: 210000 '
                    'tokens > 200000 maximum"}}'), ContextLimitError),
        (400, error("input length and `max_tokens` exceed context limit: 197202 + 21333 > "
                    "200000, decrease input length or `max_tokens` and try again"),
         ContextLimitError),
        (400, error("litellm.ContextWindowExceededError: too long"), ContextLimitError),
        (400, json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                          "message": "messages.0: bad"}, "echo": "prompt is too long"}),
         ModelError),
        (400, error("Budget has been exceeded! Current cost: 2.0", "budget_exceeded"),
         GatewayBudgetError),
        (200, json.dumps({"type": "message", "content": "not a list"}), ModelError),
    ],
)  # fmt: skip
async def test_errors_are_told_apart_as_for_chat_completions(
    fake: Fake, status: int, body: str, raised: type[ModelError]
) -> None:
    """A context limit, in each shape LiteLLM relays Anthropic's, a spent budget and an
    unusable answer; an answer that came back 2xx was charged, and its cost is kept."""
    fake.then(Reply(status=status, body=body, headers={"x-litellm-response-cost": "0.25"}))
    model = claude(fake)
    try:
        with pytest.raises(raised) as caught:
            await model.complete([{"role": "user", "content": TASK}], [])
    finally:
        await model.close()
    assert type(caught.value) is raised
    assert caught.value.cost == (0.25 if status == 200 else None)


async def test_an_overloaded_api_is_tried_again(fake: Fake) -> None:
    waits = Waits()
    model = MessagesModel(fake.url, "claude", KEY, clock=waits)
    fake.then(Reply(status=529, body=error("Overloaded", "overloaded_error")), Reply(text="ok"))
    try:
        assert (await model.complete([{"role": "user", "content": TASK}], [])).text == "ok"
    finally:
        await model.close()
    assert waits.slept == [2.0]


async def test_a_gateway_that_cannot_say_the_model_gets_summaries(fake: Fake) -> None:
    """Without the gateway's model information, the model is asked for summaries,
    which every Claude model takes; the gateway is asked again only if it could not
    answer at all."""
    fake.info_status = 404
    model = claude(fake)
    try:
        await converse(model, [Reply(text="a"), Reply(text="b")], fake)
    finally:
        await model.close()
    assert [b["thinking"]["display"] for b in fake.bodies] == ["summarized", "summarized"]
    assert fake.info_calls == 1


def test_the_setting_is_read_whole() -> None:
    text = json.dumps({"image": "sha256:abc", "version": VERSION, "verified": "2026-10-01",
                       "run": "cm-f30-1"})  # fmt: skip
    assert ThinkingReplay.parse(text) == ThinkingReplay(
        "sha256:abc", VERSION, date(2026, 10, 1), "cm-f30-1"
    )
    unrun = json.dumps({"image": "x", "version": VERSION, "verified": "2026-10-01"})
    for wrong in ("[]", "{", unrun, text.replace("cm-f30-1", ""), text.replace("2026-10", "Oct")):
        with pytest.raises(ValueError, match="the thinking replay setting"):
            ThinkingReplay.parse(wrong)


async def test_notes_and_summaries_are_archived_and_found(fake: Fake, tmp_path: Any) -> None:
    """CM-F10, CM-5, CM-38: a progress note is the answer's text, and a summary, which
    is never sent, is found by search too."""
    projects = ProjectStore(tmp_path / "state")
    try:
        projects.create("p1", "minimal", "sha256:" + "c" * 64)
        projects.open_session("p1", "s1", dedicated=False)
        archive = Archive(projects.control)
        for alias in ("claude", "older"):
            model = claude(fake, alias)
            try:
                _, (turn,) = await converse(model, [LOOKING], fake)
            finally:
                await model.close()
            message = AssistantMessage.of(turn)
            archive.append(
                "p1", "s1", f"run-{alias}", entry_of(message, Appended("assistant", reply=turn))
            )
        noted = archive.search("p1", "Listing the files", session="s1").hits
        summed = archive.search("p1", "two orders", session="s1").hits
    finally:
        projects.close()
    # Hits that hold every word; the search adds others' that hold any word.
    assert [h.message.run for h in noted if h.match != "any word"] == ["run-claude"]
    assert [h.message.run for h in summed if h.match != "any word"] == ["run-older"]
    assert not any("two orders" in text for body in fake.bodies for text in texts(body))


async def test_a_run_on_an_alias_the_setting_names_goes_through_messages(
    harness: Harness,
) -> None:
    """The run service's way in: ``--api main=messages`` sends the run's calls to
    ``/v1/messages``; the next call sends the first answer's progress note as text and
    no thinking; the archive keeps each answer as the gateway sent it and finds the note;
    and the run record says thinking replay was off, and why."""
    harness.gateway.served = {"main": "anthropic/claude-opus-5-5"}
    looking = Scripted("Looking around.", calls=[call("shell", script="echo hello")])
    record = await harness.run(
        "run-1", [looking, Scripted("Done.")], options=["--api", "main=messages"]
    )
    found = harness.gateway
    assert found.main == []  # nothing went to chat completions
    first, second = found.claude
    assert first["thinking"] == {"type": "adaptive", "display": "updates"}
    answer = second["messages"][1]["content"]
    assert answer[0] == {"type": "text", "text": f"{NOTE}Looking around."}
    assert answer[1]["type"] == "tool_use"
    assert thoughts(second) == []
    assert record.final_text == "Done."
    assert {u.thinking_replay for u in record.usage} == {"off: no thinking replay setting"}
    projects = ProjectStore(harness.state)
    try:
        archive = Archive(projects.control)
        kept = archive.messages(PROJECT, SESSION)
        hits = archive.search(PROJECT, "Looking around", session=SESSION).hits
    finally:
        projects.close()
    answers = [a.entry for a in kept if a.entry.kind == "assistant"]
    assert [(a.native_format, a.native) for a in answers] == [
        (MESSAGES, text.encode()) for text in found.claude_answers
    ]
    assert {a.gateway_version for a in answers} == {GATEWAY_VERSION}
    assert hits[0].message.entry.kind == "assistant"


async def test_a_view_rebuilt_from_the_archive_sends_the_request_the_run_sent(
    harness: Harness,
) -> None:
    """CM-F10 through the command line, with replay on: ``--api main=messages`` and
    ``--thinking-replay`` send a run's thinking back on each next call. The archive keeps
    each answer with its alias, model and prefix, so a view rebuilt from it, by a new
    client that holds nothing of the run, sends the run's last request again byte for
    byte, its thinking included; and with a tool output stubbed, as a fold leaves it, the
    thinking before the stub still goes back, and the thinking after it does not, its
    progress note going as text."""
    harness.gateway.served = {"main": "anthropic/claude-opus-5-5"}
    today = datetime.now(UTC).date().isoformat()
    replay = {"image": "sha256:" + "b" * 64, "version": GATEWAY_VERSION, "verified": today,
              "run": "cm-f30-test"}  # fmt: skip
    record = await harness.run(
        "run-1",
        [
            Scripted("Looking.", calls=[call("shell", script="echo hello")]),
            Scripted("Again.", calls=[call("shell", script="echo again")]),
            Scripted("Done."),
        ],
        options=["--api", "main=messages", "--thinking-replay", json.dumps(replay)],
    )
    found = harness.gateway
    assert record.final_text == "Done."
    assert {u.thinking_replay for u in record.usage} == {"on"}
    made = [json.loads(text)["content"] for text in found.claude_answers]
    assert found.claude[1]["messages"][1]["content"] == made[0]  # thinking went back
    projects = ProjectStore(harness.state)
    try:
        kept = Archive(projects.control).messages(PROJECT, SESSION)
    finally:
        projects.close()
    answers = [a for a in kept if a.entry.kind == "assistant"]
    assert {(a.entry.alias, a.entry.model) for a in answers} == {("main", "scripted-main")}
    # The view before the last answer, rebuilt from the archive.
    last = found.claude[-1]
    view: list[Message] = [
        {"role": "system", "content": last["system"][0]["text"]},
        *(a.chat() for a in kept[: kept.index(answers[-1])]),
    ]
    tools = [ToolSpec(t["name"], t["description"], t["input_schema"]) for t in last["tools"]]
    found.script = [Scripted("Done."), Scripted("Done.")]
    model = MessagesModel(
        found.url, "main", API_KEY, replay=ThinkingReplay.parse(json.dumps(replay))
    )
    try:
        await model.complete(view, tools)
        assert found.claude_raw[-1] == found.claude_raw[2]
        assert found.claude_betas[-1] == found.claude_betas[2]
        stub = "[stubbed: read it with history_read]"
        result = next(i for i, m in enumerate(view) if m["role"] == "tool")
        view[result] = {**view[result], "content": stub}
        await model.complete(view, tools)
    finally:
        await model.close()
    sent = json.loads(found.claude_raw[-1])["messages"]
    assert sent[1]["content"] == made[0]
    assert sent[2]["content"][0]["content"] == stub
    assert sent[3]["content"][0] == {"type": "text", "text": f"{NOTE}Again."}
    assert [b["type"] for b in sent[3]["content"]] == ["text", "tool_use"]


async def test_the_setting_chooses_the_claude_adapter_for_each_alias(fake: Fake) -> None:
    """CM-41: an alias the deployment names goes through ``/v1/messages``, with the
    replay setting; every other keeps its client."""
    made = gateway_model(fake.url, "claude", KEY, api="messages", options={"replay": SETTING})
    options = {"messages": {"replay": SETTING}}
    models = ModelGateway(fake.url, KEY, apis={"claude": "messages"}, options=options)
    try:
        assert isinstance(made, MessagesModel)
        assert made.replay == SETTING
        assert type(gateway_model(fake.url, "other", KEY)) is OpenAICompatibleModel
        chosen = models.model("claude")
        assert isinstance(chosen, MessagesModel)
        assert chosen.replay == SETTING
        assert type(models.model("other")) is OpenAICompatibleModel
    finally:
        await models.close()
        await made.close()


@pytest.mark.parametrize(
    ("options", "said"),
    [
        (["--api", "main=messages"], "--api needs --model-url"),
        (["--thinking-replay", "{}"], "the thinking replay setting names"),
    ],
)
def test_the_settings_are_checked(
    options: list[str], said: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["run", "--worker", "http://127.0.0.1:1", "--token-file", "t", "--project", "p",
            "--session", "s", "--environment", "env-1", "--script", "turns.json", *options,
            "task"]  # fmt: skip
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert said in capsys.readouterr().err


def test_the_replay_setting_needs_a_claude_alias(
    capsys: pytest.CaptureFixture[str], tmp_path: Any
) -> None:
    key = tmp_path / "key"
    key.write_text(KEY)
    base = ["run", "--worker", "http://127.0.0.1:1", "--token-file", "t", "--project", "p",
            "--session", "s", "--environment", "env-1", "--model-url", "http://127.0.0.1:2",
            "--api-key-file", str(key)]  # fmt: skip
    setting = json.dumps({"image": "i", "version": VERSION, "verified": "2026-10-01", "run": "r"})
    for options, said in (
        (["--thinking-replay", setting], "--thinking-replay needs an alias with --api"),
        (["--api", "x=responses", "--thinking-replay", setting], "needs an alias with --api"),
    ):
        with pytest.raises(SystemExit):
            cli.main([*base, *options, "task"])
        assert said in capsys.readouterr().err
