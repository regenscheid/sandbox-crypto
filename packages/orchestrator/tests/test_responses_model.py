# SPDX-License-Identifier: MIT
"""GPT through the gateway's ``/v1/responses`` (docs/specs/context-management.md, CM-40,
CM-42, CM-28, CM-29a): CM-F11's wire, checked on the requests themselves, a view rebuilt
from the archive, and the setting that chooses the adapter for an alias (CM-41).

The gateway is a fake LiteLLM in this process, whose answers are scripted; its key is
synthetic. Retries sleep on a clock that only records the waits.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.context.history import Archive
from sandbox_science.orchestrator.model import (
    CHAT_COMPLETIONS,
    AssistantMessage,
    ContextLimitError,
    GatewayBudgetError,
    Message,
    ModelError,
    ModelGateway,
    ModelTurn,
    OpenAICompatibleModel,
    ToolSpec,
    cached_for,
    gateway_model,
    prefix_digest,
    session_cache_key,
)
from sandbox_science.orchestrator.pairing import ABORTED
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.responses import RESPONSES, ResponsesModel

from .loop_harness import (
    API_KEY,
    GATEWAY_VERSION,
    PROJECT,
    SESSION,
    Harness,
    Reply,
    Reviewer,
    call,
    start_job,
)
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_model_limits import Waits

KEY = "test-secret-responses-gateway-key"
Json = dict[str, Any]
SYSTEM = "You are a research agent."
TASK = "Measure the thing."
TOOLS = [
    ToolSpec("shell", "Run a script.", {"type": "object", "properties": {}}),
    ToolSpec("read", "Read a file.", {"type": "object", "properties": {}}),
]


def reasoning(name: str) -> Json:
    """A reasoning item as LiteLLM returns it, its ID rewritten by the gateway."""
    return {
        "id": f"rs_litellm_{name}",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": f"gAAAA-test-{name}-opaque",
    }


def said(text: str, phase: str = "final_answer", name: str = "1") -> Json:
    return {
        "id": f"msg_litellm_{name}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "phase": phase,
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def called(call_id: str, name: str = "shell", **arguments: Any) -> Json:
    return {
        "id": f"fc_litellm_{call_id}",
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }


def response(*items: Json, status: str = "completed", **fields: Any) -> str:
    usage = {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1000},
             "output_tokens": 34, "output_tokens_details": {"reasoning_tokens": 20}}  # fmt: skip
    body = {"id": "resp_litellm_x", "object": "response", "status": status,
            "model": "gpt-test-2026-10", "output": list(items), "usage": usage,
            **fields}  # fmt: skip
    return json.dumps(body)


@dataclass
class Answer:
    body: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict[str, str])


@dataclass
class Fake:
    """A LiteLLM proxy's ``/v1/responses`` and ``/v1/chat/completions``: each request is
    kept as it arrived, and answered with the next scripted answer."""

    url: str = ""
    answers: list[Answer] = field(default_factory=list[Answer])
    bodies: list[Json] = field(default_factory=list[Json])
    paths: list[str] = field(default_factory=list[str])
    headers: list[list[tuple[str, str]]] = field(default_factory=list[list[tuple[str, str]]])

    async def handle(self, request: web.Request) -> web.StreamResponse:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        self.paths.append(request.path)
        self.headers.append(list(request.headers.items()))
        body = cast(Json, await request.json())
        self.bodies.append(body)
        answer = self.answers.pop(0)
        if answer.headers.get("Content-Type") == "text/event-stream":
            stream = web.StreamResponse(status=answer.status, headers=answer.headers)
            await stream.prepare(request)
            payload = answer.body.encode()
            for start in range(0, len(payload), 7):
                await stream.write(payload[start : start + 7])
            await stream.write_eof()
            return stream
        if answer.headers.get("Content-Type") == "application/json":
            return web.Response(status=answer.status, text=answer.body, headers=answer.headers)
        if request.path == "/v1/responses" and body.get("stream") is True and answer.status == 200:
            decoded = json.loads(answer.body)
            kind = f"response.{decoded.get('status', 'completed')}"
            wire = (
                ": keepalive\r\n\r\n"
                'event: response.created\r\ndata: {"type":"response.created"}\r\n\r\n'
                f"event: {kind}\r\ndata: "
                + json.dumps({"type": kind, "response": decoded}, ensure_ascii=False)
                + "\r\n\r\ndata: [DONE]\r\n\r\n"
            )
            return web.Response(
                status=answer.status,
                text=wire,
                headers={**answer.headers, "Content-Type": "text/event-stream"},
            )
        headers = {"Content-Type": "application/json", **answer.headers}
        return web.Response(status=answer.status, text=answer.body, headers=headers)

    def then(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.answers.append(Answer(body, status, headers or {}))


@pytest_asyncio.fixture
async def fake() -> AsyncIterator[Fake]:
    found = Fake()
    app = web.Application()
    app.router.add_post("/v1/responses", found.handle)
    app.router.add_post("/v1/chat/completions", found.handle)
    async with TestServer(app) as server:
        found.url = str(server.make_url("/v1"))
        yield found


@pytest_asyncio.fixture
async def gpt(fake: Fake) -> AsyncIterator[ResponsesModel]:
    model = ResponsesModel(fake.url, "gpt", KEY, clock=Waits(), max_tokens=500)
    yield model
    await model.close()


def tool(call_id: str, text: str) -> Message:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def wire(body: Json) -> None:
    """CM-F11: what every request says, whatever it sends."""
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["stream"] is True
    assert "truncation" not in body
    assert "previous_response_id" not in body


def reasonings(items: Sequence[Json]) -> list[str]:
    return [item["id"] for item in items if item.get("type") == "reasoning"]


def exact(sent: Sequence[Json], made: Sequence[Json]) -> bool:
    """Whether the items were sent as the gateway made them, field for field, in order."""
    return [json.dumps(item) for item in sent] == [json.dumps(item) for item in made]


def prefix_of(body: Json, *names: str) -> str:
    """The digest of a request's parts that hold what the model was given."""
    return prefix_digest({name: body[name] for name in names if name in body})


async def test_every_request_stores_nothing_asks_for_reasoning_and_never_truncates(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """CM-F11, on the request: ``store: false`` and ``include`` are sent, and
    ``truncation`` never is, though the fake returns encrypted reasoning either way."""
    fake.then(response(reasoning("1"), said("Hello.")))
    turn = await gpt.complete(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}], TOOLS
    )
    assert turn.text == "Hello."
    (body,) = fake.bodies
    wire(body)
    assert fake.paths == ["/v1/responses"]
    assert body["model"] == "gpt"
    assert body["instructions"] == SYSTEM
    assert body["input"] == [{"type": "message", "role": "user", "content": TASK}]
    assert body["max_output_tokens"] == 500
    assert body["tool_choice"] == "auto"
    assert [(t["type"], t["name"], t["strict"]) for t in body["tools"]] == [
        ("function", "shell", False),
        ("function", "read", False),
    ]
    assert body["tools"][0]["parameters"] == TOOLS[0].parameters


async def test_a_runs_reasoning_calls_and_outputs_go_back_unchanged(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """CM-F11, CM-40: every reasoning item, call and output since the task is sent back
    as the gateway made it (encrypted content, phase and rewritten IDs), notices
    between them included; and each answer keeps the response it came from."""
    first = [reasoning("a"), said("Looking.", "commentary", "a"), reasoning("b"),
             called("call_A", script="ls"), called("call_B", "read", path="x")]  # fmt: skip
    second = [reasoning("c"), called("call_C", script="cat x")]
    third = [reasoning("d"), said("Done: 42.", name="d")]
    for items in (first, second, third):
        fake.then(response(*items), headers={"x-litellm-version": GATEWAY_VERSION})
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TASK},
    ]
    turn = await gpt.complete(messages, TOOLS)
    assert [(c.id, c.name) for c in turn.tool_calls] == [("call_A", "shell"), ("call_B", "read")]
    assert turn.text == "Looking."
    messages += [AssistantMessage.of(turn), tool("call_A", "a b"), tool("call_B", "x's text")]
    messages.append({"role": "user", "content": "A notice from the platform."})
    turn = await gpt.complete(messages, TOOLS)
    messages += [AssistantMessage.of(turn), tool("call_C", "42")]
    turn = await gpt.complete(messages, TOOLS)
    assert (turn.text, turn.finish_reason) == ("Done: 42.", "stop")
    for body in fake.bodies:
        wire(body)
    task = {"type": "message", "role": "user", "content": TASK}
    outputs = [
        {"type": "function_call_output", "call_id": "call_A", "output": "a b"},
        {"type": "function_call_output", "call_id": "call_B", "output": "x's text"},
    ]
    notice = {"type": "message", "role": "user", "content": "A notice from the platform."}
    last = fake.bodies[2]["input"]
    assert last[0] == task
    assert exact(last[1:6], first)
    assert last[6:9] == [*outputs, notice]
    assert exact(last[9:11], second)
    assert last[11:] == [{"type": "function_call_output", "call_id": "call_C", "output": "42"}]
    assert reasonings(last) == ["rs_litellm_a", "rs_litellm_b", "rs_litellm_c"]
    # Each request began as the one before it: the prefix never changed.
    assert fake.bodies[1]["input"] == last[:9]


async def test_native_items_go_back_only_on_their_alias_and_from_its_model(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """CM-42: an answer's native items go back on the alias it was asked on and while
    that alias answers as the model that made them: as its last answer through the
    client says, or before there is one, the conversation's newest answer on it. Any
    other answer goes as its text and calls, with no reasoning."""
    old = [reasoning("old"), called("call_A", script="ls")]
    fake.then(response(*old))
    first = AssistantMessage.of(await gpt.complete([{"role": "user", "content": TASK}], TOOLS))
    assert (first.alias, first.model) == ("gpt", "gpt-test-2026-10")
    conversation: list[Message] = [{"role": "user", "content": TASK}, first, tool("call_A", "a")]
    # Another alias: plain, though the model is the same.
    mini = ResponsesModel(fake.url, "gpt-mini", KEY, clock=Waits())
    fake.then(response(said("ok")))
    try:
        await mini.complete(conversation, TOOLS)
    finally:
        await mini.close()
    assert reasonings(fake.bodies[-1]["input"]) == []
    # The alias now answers as a newer model: the old answer goes as text and calls.
    new = [reasoning("new"), called("call_B", script="pwd")]
    fake.then(response(*new, model="gpt-test-2026-11"))
    second = AssistantMessage.of(await gpt.complete(conversation, TOOLS))
    assert reasonings(fake.bodies[-1]["input"]) == ["rs_litellm_old"]
    conversation += [second, tool("call_B", "b")]
    fake.then(response(said("ok")))
    await gpt.complete(conversation, TOOLS)
    assert reasonings(fake.bodies[-1]["input"]) == ["rs_litellm_new"]
    # A client that has had no answer goes by the conversation's newest one.
    fresh = ResponsesModel(fake.url, "gpt", KEY, clock=Waits())
    fake.then(response(said("ok")))
    try:
        await fresh.complete(conversation, TOOLS)
    finally:
        await fresh.close()
    assert reasonings(fake.bodies[-1]["input"]) == ["rs_litellm_new"]


async def test_a_reasoning_item_that_led_to_nothing_is_left_out(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """CM-40: a reasoning item goes back with the item after it; a response cut short
    after its reasoning sends that reasoning back no more than the API would take it."""
    made = [reasoning("a"), said("Part", "commentary", "a"), reasoning("b")]
    fake.then(response(*made, status="incomplete",
                       incomplete_details={"reason": "max_output_tokens"}))  # fmt: skip
    turn = await gpt.complete([{"role": "user", "content": TASK}], TOOLS)
    assert (turn.text, turn.finish_reason) == ("Part", "length")
    fake.then(response(said("ok")))
    await gpt.complete(
        [{"role": "user", "content": TASK}, AssistantMessage.of(turn),
         {"role": "user", "content": "Go on."}],
        TOOLS,
    )  # fmt: skip
    sent = fake.bodies[-1]["input"]
    assert exact(sent[1:3], made[:2])
    assert sent[3] == {"type": "message", "role": "user", "content": "Go on."}


async def test_calls_and_outputs_are_paired_before_they_are_sent(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """CM-29a, ported from Codex: a call without an output gets one saying it was
    aborted, right after the call; an output without its call is left out."""
    made = [reasoning("a"), called("call_A", script="ls"), called("call_B", script="pwd")]
    fake.then(response(*made))
    turn = await gpt.complete([{"role": "user", "content": TASK}], TOOLS)
    fake.then(response(said("ok")))
    interrupted: list[Message] = [
        {"role": "user", "content": TASK},
        tool("call_Z", "an output whose call is gone"),
        AssistantMessage.of(turn),
        tool("call_A", "a b"),
        {"role": "user", "content": "The run was interrupted; answer the message again."},
    ]
    await gpt.complete(interrupted, TOOLS)
    sent = fake.bodies[-1]["input"]
    aborted = {"type": "function_call_output", "call_id": "call_B", "output": ABORTED}
    assert sent[1:5] == [*made, aborted]
    assert sent[5] == {"type": "function_call_output", "call_id": "call_A", "output": "a b"}
    assert all(item.get("call_id") != "call_Z" for item in sent)
    calls = [item["call_id"] for item in sent if item["type"] == "function_call"]
    outputs = [item["call_id"] for item in sent if item["type"] == "function_call_output"]
    assert sorted(calls) == sorted(outputs) == ["call_A", "call_B"]


async def test_native_items_never_cross_adapters_or_outlive_an_edit(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """CM-42: an answer from chat completions goes to GPT as text and calls; GPT's goes
    to chat completions as its keys alone, with no reasoning; and an answer whose text
    was changed is no longer the response, so its items are not sent."""
    fake.then(response(reasoning("a"), said("Looking.", name="a"), called("call_A", script="ls")))
    turn = await gpt.complete([{"role": "user", "content": TASK}], TOOLS)
    answer = AssistantMessage.of(turn)
    assert (answer.native_format, answer.native) == (RESPONSES, turn.native)
    # To chat completions: only the message's keys.
    chat = OpenAICompatibleModel(fake.url, "claude", KEY, clock=Waits())
    fake.then(json.dumps({"choices": [{"message": {"content": "ok"}}]}))
    try:
        await chat.complete([{"role": "user", "content": TASK}, answer, tool("call_A", "a")], [])
    finally:
        await chat.close()
    assert fake.paths[-1] == "/v1/chat/completions"
    assert fake.bodies[-1]["messages"][1] == {
        "role": "assistant",
        "content": "Looking.",
        "tool_calls": [{"id": "call_A", "type": "function",
                        "function": {"name": "shell", "arguments": '{"script": "ls"}'}}],
    }  # fmt: skip
    # From chat completions, or changed: text and calls, no reasoning.
    native = json.dumps({"choices": [{"message": {"content": "Looking."}}]}).encode()
    other = AssistantMessage.of(replace(turn, native=native, native_format=CHAT_COMPLETIONS))
    edited = AssistantMessage.of(turn)
    edited["content"] = "Looking, edited."
    plain = [
        {"type": "message", "role": "assistant", "content": "Looking."},
        {"type": "function_call", "call_id": "call_A", "name": "shell",
         "arguments": '{"script": "ls"}'},
    ]  # fmt: skip
    for message, text in ((other, "Looking."), (edited, "Looking, edited.")):
        fake.then(response(said("ok")))
        await gpt.complete([{"role": "user", "content": TASK}, message, tool("call_A", "a")], [])
        sent = fake.bodies[-1]["input"]
        assert sent[1:3] == [{**plain[0], "content": text}, plain[1]]
        assert reasonings(sent) == []


async def test_a_response_is_read_as_a_turn_and_kept_as_the_gateway_sent_it(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """CM-5, CM-28: the turn's text is its messages', a paragraph each; its input tokens
    are OpenAI's ``input_tokens``, which count the cached ones; its cost is the header's,
    or the response's own; and it keeps the response byte for byte, with the gateway's
    version."""
    text = response(
        reasoning("a"),
        said("First part.", "commentary", "a"),
        said("Second part.", name="b"),
        {"id": "msg_litellm_c", "type": "message", "role": "assistant",
         "content": [{"type": "refusal", "refusal": "Not that."}]},
    )  # fmt: skip
    fake.then(text, headers={"x-litellm-response-cost": "0.0125", "x-litellm-version": "1.102.1"})
    turn = await gpt.complete([{"role": "user", "content": TASK}], [])
    assert turn.text == "First part.\n\nSecond part.\n\nNot that."
    assert (turn.input_tokens, turn.output_tokens) == (1200, 34)
    assert (turn.cache_read_input_tokens, turn.cache_creation_input_tokens) == (1000, None)
    assert (turn.cost, turn.model, turn.finish_reason) == (0.0125, "gpt-test-2026-10", "stop")
    assert (turn.native, turn.native_format) == (
        json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":")).encode(),
        RESPONSES,
    )
    assert turn.gateway_version == "1.102.1"


async def test_response_without_cache_detail_keeps_cache_usage_unknown(
    fake: Fake, gpt: ResponsesModel
) -> None:
    fake.then(response(said("Done."), usage={"input_tokens": 10, "output_tokens": 2}))
    turn = await gpt.complete([{"role": "user", "content": TASK}], [])
    assert turn.input_tokens == 10
    assert (turn.cache_read_input_tokens, turn.cache_creation_input_tokens) == (None, None)
    # What it came from: the alias, and the digest of what the model was given.
    assert turn.alias == "gpt"
    assert turn.prefix == prefix_of(fake.bodies[-1], "instructions", "tools", "input")
    usage = {"input_tokens": 10, "output_tokens": 2, "cost": 0.5}
    cut = response(reasoning("a"), status="incomplete", usage=usage,
                   incomplete_details={"reason": "max_output_tokens"})  # fmt: skip
    fake.then(cut)
    turn = await gpt.complete([{"role": "user", "content": TASK}], [])
    assert (turn.text, turn.finish_reason, turn.cost) == (None, "length", 0.5)
    assert turn.gateway_version is None


@pytest.mark.parametrize(
    ("status", "body", "raised"),
    [
        (400, {"error": {"message": "Your input exceeds the context window of this model.",
                         "type": "invalid_request_error", "code": "context_length_exceeded"}},
         ContextLimitError),
        (400, {"error": {"message": "litellm.ContextWindowExceededError: too long"}},
         ContextLimitError),
        (400, {"error": {"message": "Budget has been exceeded! Current cost: 2.0",
                         "type": "budget_exceeded"}}, GatewayBudgetError),
        (200, {"status": "failed", "output": [],
               "error": {"code": "context_length_exceeded", "message": "too long"}},
         ContextLimitError),
        (200, {"status": "failed", "error": {"code": "context_length_exceeded"}},
         ContextLimitError),
        (200, {"status": "failed", "output": [], "error": {"code": "invalid_prompt"}}, ModelError),
        (200, {"output": "not a list"}, ModelError),
    ],
)  # fmt: skip
async def test_errors_are_told_apart_as_for_chat_completions(
    fake: Fake, gpt: ResponsesModel, status: int, body: Json, raised: type[ModelError]
) -> None:
    """A context limit, a spent budget and an unusable answer, as the chat client tells
    them apart; an answer that came back 2xx was charged, and its cost is kept."""
    fake.then(json.dumps(body), status, {"x-litellm-response-cost": "0.25"})
    with pytest.raises(raised) as error:
        await gpt.complete([{"role": "user", "content": TASK}], [])
    assert type(error.value) is raised
    assert error.value.cost == (0.25 if status == 200 else None)


async def test_split_stream_keeps_only_the_completed_response(
    fake: Fake, gpt: ResponsesModel
) -> None:
    final = json.loads(response(reasoning("stream"), called("stream", script="sage -v")))
    events = (
        ": keepalive\r\n\r\n"
        'event: response.output_text.delta\r\ndata: {"type":"response.output_text.delta",'
        '"delta":"partial"}\r\n\r\n'
        "event: response.completed\r\ndata: "
        + json.dumps({"type": "response.completed", "response": final})
        + "\r\n\r\ndata: [DONE]\r\n\r\n"
    )
    fake.then(events, headers={"Content-Type": "text/event-stream"})
    turn = await gpt.complete([{"role": "user", "content": TASK}], TOOLS)
    assert turn.text is None
    assert [(call.id, call.name, call.arguments) for call in turn.tool_calls] == [
        ("stream", "shell", '{"script": "sage -v"}')
    ]
    assert (turn.input_tokens, turn.output_tokens) == (1200, 34)
    assert json.loads(turn.native or b"") == final


@pytest.mark.parametrize(
    "events",
    [
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",'
        '"delta":"partial"}\n\n',
        'event: error\ndata: {"type":"error","message":"upstream failed"}\n\n',
        "event: response.completed\ndata: not-json\n\n",
        'event: response.completed\ndata: {"type":"response.completed"}\n\n',
    ],
)
async def test_stream_without_a_valid_final_response_is_never_a_turn(
    fake: Fake, gpt: ResponsesModel, events: str
) -> None:
    fake.then(
        events,
        headers={"Content-Type": "text/event-stream", "x-litellm-response-cost": "0.25"},
    )
    with pytest.raises(ModelError) as failed:
        await gpt.complete([{"role": "user", "content": TASK}], [])
    assert failed.value.cost == 0.25


async def test_a_nonstreaming_success_is_not_mistaken_for_sse(
    fake: Fake, gpt: ResponsesModel
) -> None:
    fake.then(response(said("done")), headers={"Content-Type": "application/json"})
    with pytest.raises(ModelError, match="did not stream"):
        await gpt.complete([{"role": "user", "content": TASK}], [])


async def test_a_failed_terminal_cannot_become_a_successful_tool_call(
    fake: Fake, gpt: ResponsesModel
) -> None:
    finished = json.loads(response(called("unsafe", script="echo unsafe")))
    fake.then(
        "event: response.failed\ndata: "
        + json.dumps({"type": "response.failed", "response": finished})
        + "\n\n",
        headers={"Content-Type": "text/event-stream"},
    )
    with pytest.raises(ModelError, match="does not match"):
        await gpt.complete([{"role": "user", "content": TASK}], TOOLS)


async def test_incomplete_stream_does_not_dispatch_an_unfinished_call(
    fake: Fake, gpt: ResponsesModel
) -> None:
    unfinished = {**called("partial", script="echo maybe"), "status": "in_progress"}
    fake.then(
        response(
            unfinished,
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
        )
    )
    turn = await gpt.complete([{"role": "user", "content": TASK}], TOOLS)
    assert turn.finish_reason == "length"
    assert turn.tool_calls == ()
    assert turn.native is not None
    assert unfinished in json.loads(turn.native)["output"]

    fake.then(response(said("continued")))
    await gpt.complete(
        [
            {"role": "user", "content": TASK},
            AssistantMessage.of(turn),
            {"role": "user", "content": "Continue."},
        ],
        TOOLS,
    )
    sent = fake.bodies[-1]["input"]
    assert all(item.get("call_id") != "partial" for item in sent)


async def test_a_rate_limit_is_tried_again_and_the_key_is_never_quoted(fake: Fake) -> None:
    waits = Waits()
    model = ResponsesModel(fake.url, "gpt", KEY, clock=waits)
    fake.then(f"slow down, {KEY}", 429, {"Retry-After": "3"})
    fake.then(response(said("ok")))
    fake.then(f"no such model for {KEY}", 404)
    try:
        assert (await model.complete([{"role": "user", "content": TASK}], [])).text == "ok"
        with pytest.raises(ModelError) as error:
            await model.complete([{"role": "user", "content": TASK}], [])
    finally:
        await model.close()
    assert waits.slept == [3.0]
    assert KEY not in str(error.value)
    assert "[the gateway key]" in str(error.value)


async def test_the_chat_client_keeps_its_alias_and_prefix_too(fake: Fake) -> None:
    """Both adapters record what an answer came from, for the archive and for Claude's
    binding of thinking to what came before it (CM-38)."""
    chat = OpenAICompatibleModel(fake.url, "claude", KEY, clock=Waits())
    fake.then(json.dumps({"model": "claude-x", "choices": [{"message": {"content": "ok"}}]}))
    try:
        turn = await chat.complete([{"role": "user", "content": TASK}], TOOLS)
    finally:
        await chat.close()
    answer = AssistantMessage.of(turn)
    assert (answer.alias, answer.model, answer.native_format) == (
        "claude",
        "claude-x",
        CHAT_COMPLETIONS,
    )
    assert answer.prefix == prefix_of(fake.bodies[-1], "messages", "tools")
    # The digest does not depend on how a message's keys were ordered.
    reordered = {"tools": fake.bodies[-1]["tools"], "messages": [{"content": TASK, "role": "user"}]}
    assert prefix_digest(reordered) == answer.prefix


async def test_an_adapter_may_add_headers_but_not_the_key(fake: Fake) -> None:
    """The hook a request's headers go through, which the Claude adapter needs for its
    beta flags; the key's header is the base's alone."""

    class Flagged(ResponsesModel):
        def _headers(self, body: Mapping[str, Any]) -> dict[str, str]:
            return {"anthropic-beta": "a-flag-2026-10-01", "authorization": "Bearer stolen"}

    model = Flagged(fake.url, "gpt", KEY, clock=Waits())
    fake.then(response(said("ok")))
    try:
        await model.complete([{"role": "user", "content": TASK}], [])
    finally:
        await model.close()
    (headers,) = fake.headers
    assert ("anthropic-beta", "a-flag-2026-10-01") in headers
    assert [v for k, v in headers if k.lower() == "authorization"] == [f"Bearer {KEY}"]


def test_an_answer_carries_its_origin_through_copies_but_not_out_of_the_conversation() -> None:
    """``copy`` and ``deepcopy`` keep what an answer came from; ``dict()`` and JSON, as
    another adapter or a record sees it, do not."""
    answer = AssistantMessage(
        "Done.", native=b"{}", native_format=RESPONSES, alias="gpt", model="m",
        gateway_version="1.102.1", prefix="0" * 64,
    )  # fmt: skip
    for kept in (copy.copy(answer), copy.deepcopy(answer)):
        assert isinstance(kept, AssistantMessage)
        assert (kept.native, kept.alias, kept.model, kept.prefix) == (b"{}", "gpt", "m", "0" * 64)
        assert kept == {"role": "assistant", "content": "Done."}
    assert type(dict(answer)) is dict
    assert json.loads(json.dumps(answer)) == {"role": "assistant", "content": "Done."}


async def test_one_setting_chooses_the_adapter_for_each_alias(fake: Fake) -> None:
    """CM-41: the adapter each alias is called through, ``chat`` unless the deployment
    names another, chosen in one place."""
    assert isinstance(gateway_model(fake.url, "gpt", KEY, api="responses"), ResponsesModel)
    assert type(gateway_model(fake.url, "claude", KEY)) is OpenAICompatibleModel
    with pytest.raises(ValueError, match="no adapter 'carrier-pigeon'"):
        gateway_model(fake.url, "x", KEY, api="carrier-pigeon")
    models = ModelGateway(fake.url, KEY, apis={"gpt": "responses"})
    try:
        assert isinstance(models.model("gpt"), ResponsesModel)
        assert type(models.model("claude")) is OpenAICompatibleModel
        assert type(ModelGateway(fake.url, KEY).model("gpt")) is OpenAICompatibleModel
    finally:
        await models.close()


@pytest.mark.parametrize(
    ("options", "said"),
    [
        (["--script", "turns.json", "--api", "main=responses"], "--api needs --model-url"),
        (["--script", "turns.json", "--api", "main=pigeon"], "is not ALIAS=KIND"),
        (["--script", "turns.json", "--api", "=chat"], "is not ALIAS=KIND"),
        (["--model-url", "http://127.0.0.1:1", "--api-key-file", "key",
          "--api", "main=chat", "--api", "main=responses"], "two adapters for one alias"),
    ],
)  # fmt: skip
def test_the_setting_is_refused_when_it_cannot_hold(
    capsys: pytest.CaptureFixture[str], options: list[str], said: str
) -> None:
    argv = ["run", "--worker", "http://127.0.0.1:1", "--token-file", "t", "--project", "p",
            "--session", "s", "--environment", "env-1", *options, "task"]  # fmt: skip
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert said in capsys.readouterr().err


async def test_a_view_rebuilt_from_the_archive_sends_the_request_the_run_sent(
    harness: Harness,
) -> None:
    """CM-F11 through the command line: ``--api main=responses`` sends a run's calls to
    ``/v1/responses`` and its reasoning back on each next call. The archive keeps each
    answer as the gateway sent it, with its alias, model and prefix, so a view rebuilt
    from it sends the run's last request again byte for byte, its reasoning included; and
    with a tool output stubbed, as a fold leaves it, the same request but for that
    output."""
    record = await harness.run(
        "run-1",
        [
            Reply("Looking.", calls=[call("shell", script="echo hello")]),
            Reply("Again.", calls=[call("shell", script="echo again")]),
            Reply("Done."),
        ],
        options=["--api", "main=responses"],
    )
    found = harness.gateway
    assert found.main == []  # nothing went to chat completions
    assert record.final_text == "Done."
    for body in found.responses:
        wire(body)
    projects = ProjectStore(harness.state)
    try:
        kept = Archive(projects.control).messages(PROJECT, SESSION)
    finally:
        projects.close()
    answers = [a for a in kept if a.entry.kind == "assistant"]
    assert [(a.entry.native_format, a.entry.native) for a in answers] == [
        (RESPONSES, json.dumps(json.loads(text), separators=(",", ":")).encode())
        for text in found.responded
    ]
    assert {(a.entry.alias, a.entry.model) for a in answers} == {("main", "scripted-main")}
    assert {a.entry.gateway_version for a in answers} == {GATEWAY_VERSION}
    assert [a.entry.prefix for a in answers] == [
        prefix_of(body, "instructions", "tools", "input") for body in found.responses
    ]
    # The view before the last answer, rebuilt from the archive.
    last = found.responses[-1]
    view: list[Message] = [
        {"role": "system", "content": last["instructions"]},
        *(a.chat() for a in kept[: kept.index(answers[-1])]),
    ]
    tools = [ToolSpec(t["name"], t["description"], t["parameters"]) for t in last["tools"]]
    found.script = [Reply("Done."), Reply("Done.")]
    # Made for the session, as the run's was: its key is in the bytes.
    model = cached_for(ResponsesModel(found.url, "main", API_KEY, clock=Waits()), PROJECT, SESSION)
    assert isinstance(model, ResponsesModel)
    try:
        await model.complete(view, tools)
        assert found.raw[-1] == found.raw[2]
        assert reasonings(found.responses[-1]["input"]) == [
            "rs_litellm_1", "rs_litellm_2"
        ]  # fmt: skip
        # A tool output stubbed: only that output differs.
        stub = "[stubbed: read it with history_read]"
        result = next(i for i, m in enumerate(view) if m["role"] == "tool")
        view[result] = {**view[result], "content": stub}
        await model.complete(view, tools)
    finally:
        await model.close()
    expected = json.loads(found.raw[2])
    outputs = [i for i in expected["input"] if i["type"] == "function_call_output"]
    outputs[0]["output"] = stub
    assert json.dumps(json.loads(found.raw[-1])) == json.dumps(expected)


async def test_a_runs_requests_keep_its_view_and_only_its_start_rebuilds_it(
    harness: Harness,
) -> None:
    """CM-28, CM-29 across a run boundary: the next run starts from its session's view,
    rebuilt from the archive, run 1's reasoning and calls among it; within the run each
    request sends the one before it unchanged, byte for byte, the view included, so the
    cache keeps its prefix. A run's start is a rebuild: the prefix may change there."""
    options = ["--api", "main=responses"]
    looking = Reply("Looking.", calls=[call("shell", script="echo hello")])
    await harness.run("run-1", [looking, Reply("Done.")], options=options)
    found = harness.gateway
    first = len(found.raw)
    again = Reply("Again.", calls=[call("shell", script="echo again")])
    await harness.run("run-2", [again, Reply("Done again.")], options=options)
    one, two = found.responses[:first], found.responses[first:]
    assert len(two) == 2
    # Within run 2, the second request sends the first's instructions, tools and input
    # unchanged, byte for byte as they arrived, its view included.
    assert (two[1]["instructions"], two[1]["tools"]) == (two[0]["instructions"], two[0]["tools"])
    before, after = input_bytes(found.raw[first]), input_bytes(found.raw[first + 1])
    assert after.startswith(before[:-1])  # all but the closing bracket
    # Run 2's start rebuilt run 1 from the archive: here, with no state block or kernel
    # line to add, its items come back as run 1 last sent them, reasoning included, then
    # run 1's answer. Nothing requires that at a run's start; folds will change it.
    view = two[0]["input"]
    assert exact(view[: len(one[-1]["input"])], one[-1]["input"])
    assert reasonings(view) == ["rs_litellm_1", "rs_litellm_2"]
    # Each answer's recorded prefix is of the request that produced it, run by run.
    projects = ProjectStore(harness.state)
    try:
        kept = Archive(projects.control).messages(PROJECT, SESSION)
    finally:
        projects.close()
    answers = [a.entry.prefix for a in kept if a.entry.kind == "assistant"]
    made = [prefix_of(body, "instructions", "tools", "input") for body in found.responses]
    assert answers == made
    assert len(set(made)) == len(made)


def input_bytes(raw: bytes) -> bytes:
    """A request's ``input`` array, as the bytes that arrived."""
    text = raw.decode()
    start = text.index("[", text.index('"input"'))
    _, end = json.JSONDecoder().raw_decode(text, start)
    return text[start:end].encode()


def parts(raw: bytes) -> tuple[str, str, list[str]]:
    """A request as the cache sees it, in the order it renders it: the tools, the
    instructions, and each input item, each as the text that arrived."""
    body = cast(Json, json.loads(raw))
    return (
        json.dumps(body["tools"]),
        json.dumps(body["instructions"]),
        [json.dumps(item) for item in body["input"]],
    )


def only_grew(raws: Sequence[bytes]) -> None:
    """CM-32: each request is the one before it, byte for byte, with items added at the
    end: the same tools and instructions, every earlier item unchanged and in place."""
    assert len(raws) > 1
    for before, after in itertools.pairwise(raws):
        (tools, instructions, items), (tools2, instructions2, items2) = parts(before), parts(after)
        assert (tools2, instructions2) == (tools, instructions)
        assert items2[: len(items)] == items
        assert len(items2) > len(items)
        assert input_bytes(after).startswith(input_bytes(before)[:-1])


async def test_a_runs_requests_only_grow_whatever_the_platform_adds_between_calls(
    harness: Harness,
) -> None:
    """CM-32 on the wire: a run with a plan, a job, a comment and a review arriving
    between its calls, closing rounds, a tool call and parallel calls sends, call after
    call, the tools and instructions it began with and every item it sent before, in
    place; notices and results only ever follow. A call cached on the one before it
    would be read from the prompt cache; nothing early in a request changes."""
    options = ["--api", "main=responses"]
    await harness.run(
        "run-job", [Reply(calls=[start_job()]), Reply("A job is running.")], options=options
    )
    # Parallel calls, their results all after them, in one run.
    parallel = [call("shell", script="echo a"), call("shell", script="echo b")]
    found = harness.gateway
    before = len(found.raw)
    await harness.run(
        "run-2", [Reply(calls=parallel), Reply(calls=parallel), Reply("Done.")], options=options
    )
    assert len(found.raw) - before == 3
    only_grew(found.raw[before:])
    await harness.math_finding()
    harness.configure_reviews(ReviewConfig())
    harness.gateway.reviewer = Reviewer(flags=1)

    async def arrive() -> None:
        harness.user_plan()
        await harness.end_job()
        harness.work_request()

    first = len(found.raw)
    await harness.run(
        "run-1",
        [
            Reply("Answer 1.", before=arrive),
            Reply(calls=[call("plan_read")]),
            Reply("Answer 2."),
            Reply("Answer 3, done."),
            Reply("Answer 4."),
        ],
        reviewer=True,
        options=options,
    )
    assert len(found.raw) - first == 5
    only_grew(found.raw[first:])
    keys = {json.loads(raw).get("prompt_cache_key") for raw in found.raw}
    assert keys == {session_cache_key(PROJECT, SESSION)}


async def test_every_call_of_a_session_sends_the_sessions_cache_key(harness: Harness) -> None:
    """CM-32: the gateway routes a call to a prompt cache by its first tokens, which every
    session of a deployment shares (the system message). A session's calls, whichever of
    its runs makes them, send one ``prompt_cache_key`` of their own, so that each session
    keeps to one cache. The key is a digest, not the session's IDs."""
    options = ["--api", "main=responses"]
    await harness.run("run-1", [Reply(calls=[call("shell", script="echo 1")]), Reply("Done.")],
                      options=options)  # fmt: skip
    await harness.run("run-2", [Reply("Again.")], options=options)
    found = harness.gateway
    keys = [json.loads(raw)["prompt_cache_key"] for raw in found.raw]
    assert len(keys) == 3
    assert set(keys) == {session_cache_key(PROJECT, SESSION)}
    assert keys[0] == "ss-" + hashlib.sha256(f"{PROJECT}\0{SESSION}".encode()).hexdigest()[:32]
    for body in found.responses:
        wire(body)


def test_a_sessions_key_is_its_own_stable_and_short() -> None:
    key = session_cache_key("project", "session")
    assert key == session_cache_key("project", "session")
    assert len(key) <= 64
    others = {
        session_cache_key("project", "other"),
        session_cache_key("other", "session"),
        session_cache_key("projects", "ession"),  # the boundary is part of the key
    }
    assert key not in others
    assert len(others) == 3


async def test_a_model_made_for_a_session_sends_its_key_and_the_others_none(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """CM-32: the key is on the request but not in the digest of what the model was
    given, which it does not change; the model it was made from sends none, its session
    is shared, and a model that is not the gateway's is returned as it was."""
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TASK},
    ]
    keyed = cached_for(gpt, "p", "s")
    assert isinstance(keyed, ResponsesModel)
    assert keyed is not gpt
    for _ in range(2):
        fake.then(response(reasoning("1"), said("Hello.")))
    keyed_turn = await keyed.complete(messages, TOOLS)
    plain_turn = await gpt.complete(messages, TOOLS)
    sent, unkeyed = fake.bodies
    assert sent["prompt_cache_key"] == session_cache_key("p", "s")
    assert "prompt_cache_key" not in unkeyed
    assert (
        keyed_turn.prefix == plain_turn.prefix == prefix_of(sent, "instructions", "tools", "input")
    )
    # The fold's retry without tools is the same session's.
    fake.then(response(said("Fine.")))
    await keyed.choosing("none").complete(messages, TOOLS)
    assert fake.bodies[-1]["prompt_cache_key"] == sent["prompt_cache_key"]
    assert fake.bodies[-1]["tool_choice"] == "none"
    scripted = Scripted()
    assert cached_for(scripted, "p", "s") is scripted


class Scripted:
    """A model that is not the gateway's."""

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        raise AssertionError("not called")


async def test_working_projection_preserves_exact_responses_replay(fake: Fake, gpt: ResponsesModel):
    fake.then(
        response(
            reasoning("kept"),
            called("call_kept", script="ls"),
            instructions="echoed instructions" * 10000,
            tools=[{"echo": "large schema"}],
        )
    )
    before = [{"role": "user", "content": TASK}]
    original = AssistantMessage.of(await gpt.complete(before, TOOLS))
    full_native = original.native
    for answer in (original, original.replay()):
        fake.then(response(said("ok")))
        await gpt.complete([*before, answer, tool("call_kept", "result")], TOOLS)
    assert fake.bodies[-2] == fake.bodies[-1]
    assert reasonings(fake.bodies[-1]["input"]) == ["rs_litellm_kept"]
    assert original.native == full_native
    projected = original.replay().native
    assert projected is not None
    assert len(projected) < 1000
