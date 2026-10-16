# SPDX-License-Identifier: MIT
"""GPT through the gateway's ``/v1/responses`` (docs/specs/context-management.md, CM-40,
CM-42, CM-28, CM-29a): CM-F11's wire, checked on the requests themselves, and the setting
that chooses the adapter for an alias (CM-41).

The gateway is a fake LiteLLM in this process, whose answers are scripted; its key is
synthetic. Retries sleep on a clock that only records the waits.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.history import Archive
from sandbox_science.orchestrator.model import (
    CHAT_COMPLETIONS,
    AssistantMessage,
    ContextLimitError,
    GatewayBudgetError,
    Message,
    ModelError,
    ModelGateway,
    OpenAICompatibleModel,
    ToolSpec,
    gateway_model,
)
from sandbox_science.orchestrator.pairing import ABORTED
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.responses import RESPONSES, ResponsesModel

from .loop_harness import GATEWAY_VERSION, PROJECT, SESSION, Harness, Reply, call
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

    async def handle(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        self.paths.append(request.path)
        self.bodies.append(cast(Json, await request.json()))
        answer = self.answers.pop(0)
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
    assert "truncation" not in body
    assert "previous_response_id" not in body


def reasonings(items: Sequence[Json]) -> list[str]:
    return [item["id"] for item in items if item.get("type") == "reasoning"]


def exact(sent: Sequence[Json], made: Sequence[Json]) -> bool:
    """Whether the items were sent as the gateway made them, field for field, in order."""
    return [json.dumps(item) for item in sent] == [json.dumps(item) for item in made]


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
    messages += [AssistantMessage(turn), tool("call_A", "a b"), tool("call_B", "x's text")]
    messages.append({"role": "user", "content": "A notice from the platform."})
    turn = await gpt.complete(messages, TOOLS)
    messages += [AssistantMessage(turn), tool("call_C", "42")]
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


async def test_after_a_rebuild_only_the_tails_reasoning_goes_with_the_items_it_led_to(
    fake: Fake, gpt: ResponsesModel
) -> None:
    """CM-F11: after a rebuild the answers before the boundary are text and carry no
    reasoning; in the tail, each reasoning item is kept or dropped with the item after
    it, and calls and outputs stay paired."""
    made = [reasoning("a"), said("Looking.", "commentary", "a"), reasoning("b"),
            called("call_A", script="ls"), reasoning("c"),
            called("call_B", "read", path="x")]  # fmt: skip
    fake.then(response(*made))
    turn = await gpt.complete([{"role": "user", "content": TASK}], TOOLS)
    whole = AssistantMessage(turn)
    for _ in range(3):
        fake.then(response(said("ok")))
    # Before the boundary: the answer as text, as a fold leaves the conversation.
    folded: list[Message] = [
        {"role": "user", "content": "[the state block]"},
        dict(whole),
        tool("call_A", "a b"),
        tool("call_B", "x's text"),
        {"role": "user", "content": "The raw tail begins."},
    ]
    await gpt.complete(folded, TOOLS)
    sent = fake.bodies[-1]["input"]
    assert reasonings(sent) == []
    assert all("id" not in item for item in sent)
    assert [item["type"] for item in sent[1:4]] == ["message", "function_call", "function_call"]
    # A tail that keeps only call B: its reasoning goes with it, the text's and A's do not.
    only_b = AssistantMessage(replace(turn, text=None, tool_calls=turn.tool_calls[1:]))
    await gpt.complete([{"role": "user", "content": "[state]"}, only_b, tool("call_B", "x")], TOOLS)
    sent = fake.bodies[-1]["input"]
    assert exact(sent[1:3], made[4:])
    assert sent[3] == {"type": "function_call_output", "call_id": "call_B", "output": "x"}
    assert len(sent) == 4
    # One that keeps the text and call A: B's reasoning goes with B, and A's output stays.
    no_b = AssistantMessage(replace(turn, tool_calls=turn.tool_calls[:1]))
    await gpt.complete(
        [{"role": "user", "content": "[state]"}, no_b, tool("call_A", "a"), tool("call_B", "x")],
        TOOLS,
    )
    sent = fake.bodies[-1]["input"]
    assert exact(sent[1:5], made[:4])
    assert sent[5:] == [{"type": "function_call_output", "call_id": "call_A", "output": "a"}]
    for body in fake.bodies:
        wire(body)


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
        AssistantMessage(turn),
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
    answer = AssistantMessage(turn)
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
    other = AssistantMessage(replace(turn, native=native, native_format=CHAT_COMPLETIONS))
    edited = AssistantMessage(turn)
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
    assert (turn.cost, turn.model, turn.finish_reason) == (0.0125, "gpt-test-2026-10", "stop")
    assert (turn.native, turn.native_format) == (text.encode(), RESPONSES)
    assert turn.gateway_version == "1.102.1"
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
        (200, {"status": "failed", "output": [], "error": {"code": "server_error"}}, ModelError),
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


async def test_the_setting_chooses_the_adapter_for_each_alias(fake: Fake) -> None:
    """CM-41: an alias the deployment names goes through ``/v1/responses``; every other
    keeps chat completions, as by default."""
    assert isinstance(gateway_model(fake.url, "gpt", KEY, responses=True), ResponsesModel)
    assert type(gateway_model(fake.url, "claude", KEY)) is OpenAICompatibleModel
    models = ModelGateway(fake.url, KEY, responses=frozenset({"gpt"}))
    try:
        assert isinstance(models.model("gpt"), ResponsesModel)
        assert type(models.model("claude")) is OpenAICompatibleModel
        assert type(ModelGateway(fake.url, KEY).model("gpt")) is OpenAICompatibleModel
    finally:
        await models.close()


def test_the_setting_needs_the_gateway(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["run", "--worker", "http://127.0.0.1:1", "--token-file", "t", "--project", "p",
            "--session", "s", "--environment", "env-1", "--script", "turns.json",
            "--responses-api", "main", "task"]  # fmt: skip
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "--responses-api needs --model-url" in capsys.readouterr().err


async def test_a_run_on_an_alias_the_setting_names_goes_through_responses(
    harness: Harness,
) -> None:
    """The run service's way in: ``--responses-api main`` sends the run's calls to
    ``/v1/responses``, replays its reasoning on the next call, and archives each answer
    as the gateway sent it."""
    record = await harness.run(
        "run-1",
        [Reply("Looking.", calls=[call("shell", script="echo hello")]), Reply("Done.")],
        options=["--responses-api", "main"],
    )
    found = harness.gateway
    assert found.main == []  # nothing went to chat completions
    first, second = found.responses
    for body in (first, second):
        wire(body)
    made = json.loads(found.responded[0])["output"]
    assert reasonings(made) == ["rs_litellm_1"]
    sent = second["input"]
    start = sent.index(made[0])  # after the task and the notices the run had
    assert exact(sent[start : start + len(made)], made)
    assert sent[start + len(made)]["type"] == "function_call_output"
    assert record.final_text == "Done."
    projects = ProjectStore(harness.state)
    try:
        kept = Archive(projects.control).messages(PROJECT, SESSION)
    finally:
        projects.close()
    answers = [a.entry for a in kept if a.entry.kind == "assistant"]
    assert [(a.native_format, a.native) for a in answers] == [
        (RESPONSES, text.encode()) for text in found.responded
    ]
    assert {a.gateway_version for a in answers} == {GATEWAY_VERSION}
