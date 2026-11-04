# SPDX-License-Identifier: MIT
"""What the tool loop does with each kind of reply (run-service spec, RS-6f): an answer the
output limit cut off is continued, and a call it cut off does not run; an empty answer is
asked for once more; the report round's call may not call the tools; a run of read-class
calls in one reply runs at once, its results in the reply's order; and three failed calls
in a row of one tool with one kind of error bring a nudge.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.effects import UnknownEffectError
from sandbox_science.orchestrator.hooks import Notice, RunView, TurnHook
from sandbox_science.orchestrator.images import IMAGE
from sandbox_science.orchestrator.loop import (
    CONCURRENT,
    CONTINUATIONS,
    IN_ORDER,
    OUTPUT_LIMIT,
    run_task,
)
from sandbox_science.orchestrator.model import (
    LENGTH,
    AssistantMessage,
    Message,
    ModelTurn,
    OpenAICompatibleModel,
    ToolCall,
    ToolSpec,
    cut_call,
)
from sandbox_science.orchestrator.notices import CONTINUE, ERROR_STREAK, IMAGES, REPORT
from sandbox_science.orchestrator.question_tool import ASK_USER
from sandbox_science.orchestrator.tool_entry import ToolEntry
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.orchestrator.wraps import STREAK

CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
KEY = "test-secret-loop-replies-gateway-key"


def toolbox() -> Toolbox:
    return Toolbox(cast(WorkerClient, object()), CONTEXT)


class Script:
    """Replays turns, keeping each request."""

    def __init__(self, steps: Sequence[ModelTurn]) -> None:
        self.steps = list(steps)
        self.requests: list[list[Message]] = []

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        self.requests.append(list(messages))
        return self.steps.pop(0)


def said(request: Sequence[Message], framing: str) -> list[Any]:
    """The payloads of the notices in a request with this framing."""
    return [
        json.loads(str(message["content"])[len(framing) :])
        for message in request
        if message["role"] == "user" and str(message["content"]).startswith(framing)
    ]


def calls(*names: str, cut: bool = False) -> ModelTurn:
    made = tuple(ToolCall(f"c{n}-{name}", name, "{}") for n, name in enumerate(names))
    return ModelTurn(None, made, finish_reason=LENGTH if cut else None)


# --- An answer the output limit cut off ----------------------------------------------


async def test_a_cut_answer_is_continued_and_its_parts_are_the_answer() -> None:
    model = Script([ModelTurn("The first half, ", finish_reason=LENGTH), ModelTurn("the rest.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.stopped, transcript.final_text, transcript.answer) == (
        "answered",
        "The first half, the rest.",
        "The first half, the rest.",
    )
    # The partial text stays in the conversation, and the notice asks for the rest.
    second = model.requests[1]
    assert second[-2]["role"] == "assistant"
    assert second[-2]["content"] == "The first half, "
    assert said(second, CONTINUE.text) == [
        {"output_tokens": None, "continuation": 1, "at_most": CONTINUATIONS}
    ]
    assert transcript.summaries["length"] == {"stops": 1, "at": [1], "continued": 1}


async def test_continuations_end_after_two_and_the_text_so_far_is_the_answer() -> None:
    model = Script([ModelTurn(part, finish_reason=LENGTH) for part in ("a", "b", "c")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.final_text, len(model.requests), CONTINUATIONS) == ("abc", 3, 2)
    assert transcript.summaries["length"] == {"stops": 3, "at": [1, 2, 3], "continued": 2}


async def test_a_cut_answer_with_no_turn_left_stands_as_it_is() -> None:
    model = Script([ModelTurn("Cut sh", finish_reason=LENGTH)])
    transcript = await run_task(model, toolbox(), "go", run_id="r", max_turns=1)
    assert (transcript.stopped, transcript.final_text, len(model.requests)) == (
        "answered",
        "Cut sh",
        1,
    )
    assert "continued" not in cast(dict[str, int], transcript.summaries["length"])


async def test_a_call_the_output_limit_cut_off_does_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    async def counted(box: Toolbox, call: ToolCall, *args: object, **kwargs: object) -> object:
        ran.append(call.id)
        return ToolOutcome(json.dumps({"ran": call.id}), {})

    monkeypatch.setattr(Toolbox, "call", counted)
    whole = ToolCall("c1", "read_file", '{"path": "a.txt"}')
    cut = ToolCall("c2", "write_file", '{"path": "b.txt", "content": "half of i')
    model = Script([ModelTurn(None, (whole, cut), finish_reason=LENGTH), ModelTurn("Done.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.final_text, ran) == ("Done.", ["c1"])
    assert [record.get("not_run") for record in transcript.tool_records] == [None, OUTPUT_LIMIT]
    refused = transcript.tool_records[1]
    assert (refused["tool"], refused["error"]) == ("write_file", "invalid_request")
    second = model.requests[1]
    answer = next(message for message in second if message["role"] == "assistant")
    # Its broken arguments are not sent back: an API that reads them would refuse.
    held = [call["function"]["arguments"] for call in answer["tool_calls"]]
    assert held == ['{"path": "a.txt"}', "{}"]
    result = json.loads(str(second[-1]["content"]))
    assert result["error"]["kind"] == "invalid_request"
    assert "reached its output limit inside this call" in result["error"]["message"]
    assert transcript.summaries["length"] == {"stops": 1, "at": [1], "calls_not_run": 1}


def test_only_the_last_call_of_a_cut_answer_is_cut_and_never_a_responses_one() -> None:
    first, last = ToolCall("a", "grep", "{}"), ToolCall("b", "shell", '{"script": "ec')
    assert cut_call(ModelTurn(None, (first, last), finish_reason=LENGTH)) is last
    assert cut_call(ModelTurn(None, (first, last), finish_reason="tool_calls")) is None
    assert cut_call(ModelTurn("Cut", finish_reason=LENGTH)) is None
    # The Responses adapter leaves an unfinished call out of the turn.
    whole = ModelTurn(None, (first,), finish_reason=LENGTH, native_format="responses")
    assert cut_call(whole) is None
    # A cut call with JSON arguments keeps them; one without is held with none.
    kept = AssistantMessage.of(ModelTurn(None, (first, last), finish_reason=LENGTH))
    assert [call["function"]["arguments"] for call in kept["tool_calls"]] == ["{}", "{}"]
    valid = ToolCall("b", "shell", '{"script": "echo"}')
    kept = AssistantMessage.of(ModelTurn(None, (first, valid), finish_reason=LENGTH))
    assert kept["tool_calls"][1]["function"]["arguments"] == '{"script": "echo"}'
    broken = ToolCall("a", "grep", '{"pattern": ')
    kept = AssistantMessage.of(ModelTurn(None, (broken, valid), finish_reason=LENGTH))
    assert kept["tool_calls"][0]["function"]["arguments"] == '{"pattern": '


# --- An empty answer -----------------------------------------------------------------


async def test_an_empty_answer_is_asked_for_once_more_before_its_closing() -> None:
    model = Script([ModelTurn(None), ModelTurn("Done.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.final_text, len(model.requests)) == ("Done.", 2)
    assert transcript.summaries["empty_answers"] == {"retried": 1}


async def test_a_second_empty_answer_goes_to_its_closing() -> None:
    model = Script([ModelTurn("  "), ModelTurn(None), ModelTurn("never asked")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.stopped, transcript.final_text, len(model.requests)) == (
        "answered",
        None,
        2,
    )


# --- The report round ----------------------------------------------------------------


@dataclass
class Completions:
    """A gateway's chat completions, answered from a script; each request kept."""

    script: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    bodies: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    url: str = ""

    async def handle(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        self.bodies.append(cast(dict[str, Any], await request.json()))
        message = self.script.pop(0)
        answer = {
            "model": "scripted",
            "choices": [{"message": {"role": "assistant", **message}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        return web.json_response(answer, headers={"x-litellm-response-cost": "0.01"})


@pytest_asyncio.fixture
async def completions() -> AsyncIterator[Completions]:
    found = Completions()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", found.handle)
    async with TestServer(app) as server:
        found.url = str(server.make_url("/v1"))
        yield found


async def test_the_report_rounds_call_keeps_the_tools_and_may_not_call_them(
    completions: Completions,
) -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "nothing", "arguments": "{}"}}
    completions.script = [{"content": None, "tool_calls": [call]}, {"content": "So far: one."}]
    model = OpenAICompatibleModel(completions.url, "main", KEY)
    try:
        transcript = await run_task(model, toolbox(), "go", run_id="r", max_turns=1)
    finally:
        await model.close()
    assert (transcript.stopped, transcript.final_text) == ("turn_limit", "So far: one.")
    working, report = completions.bodies
    assert working["tool_choice"] == "auto"
    assert report["tool_choice"] == "none"
    # The tools stay, so the prompt cache still covers them.
    assert report["tools"] == working["tools"]
    assert str(report["messages"][-1]["content"]).startswith(REPORT.text)


# --- Read-class calls at once --------------------------------------------------------


class Calls:
    """Stands in for ``Toolbox.call``: each call waits for its gate, if it has one, and
    is noted as it starts and ends."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.started: list[str] = []
        self.ended: list[str] = []
        self.running = self.most = 0
        self.raising: dict[str, BaseException] = {}
        self.images: dict[str, tuple[Notice, str]] = {}

    async def __call__(
        self, box: Toolbox, call: ToolCall, *args: object, **kwargs: object
    ) -> ToolOutcome:
        self.started.append(call.id)
        self.running += 1
        self.most = max(self.most, self.running)
        try:
            if (gate := self.gates.get(call.id)) is not None:
                await asyncio.wait_for(gate.wait(), 5)
            await asyncio.sleep(0)
            if (error := self.raising.get(call.id)) is not None:
                raise error
            return ToolOutcome(
                json.dumps({"ran": call.id}), {"ran": call.id}, image=self.images.get(call.id)
            )
        finally:
            self.running -= 1
            self.ended.append(call.id)


def standing_in(monkeypatch: pytest.MonkeyPatch, made: Calls) -> None:
    async def call(box: Toolbox, call: ToolCall, *args: object, **kwargs: object) -> ToolOutcome:
        return await made(box, call)

    monkeypatch.setattr(Toolbox, "call", call)


class Observed(TurnHook):
    name = "observed"

    def __init__(self) -> None:
        self.records: list[str] = []

    async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
        self.records.append(str(record["ran"]))


def reads(*names: str) -> ModelTurn:
    return ModelTurn(None, tuple(ToolCall(f"c{n}", name, "{}") for n, name in enumerate(names)))


async def test_read_class_calls_in_a_row_run_at_once_and_report_in_the_replys_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = Calls()
    # c0 ends only once c1 has started, which it can only if the two run at once; c1 ends
    # first, and its result still comes second.
    made.gates["c0"] = asyncio.Event()
    original = made.__call__

    async def call(box: Toolbox, call: ToolCall, *args: object, **kwargs: object) -> ToolOutcome:
        if call.id == "c1":
            made.gates["c0"].set()
        return await original(box, call, *args, **kwargs)

    monkeypatch.setattr(Toolbox, "call", call)
    hook = Observed()
    model = Script([reads("read_file", "grep", "shell", "list_files", "glob"), ModelTurn("Done.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r", hooks=[hook])
    assert transcript.final_text == "Done."
    assert made.ended[:2] == ["c1", "c0"]
    order = ["c0", "c1", "c2", "c3", "c4"]
    assert [record["ran"] for record in transcript.tool_records] == order
    assert hook.records == order
    results = [m["tool_call_id"] for m in model.requests[1] if m["role"] == "tool"]
    assert results == order
    # The shell call, of the effect class, ran alone: after the reads before it ended,
    # before the reads after it started.
    assert made.started.index("c2") > max(made.ended.index("c0"), made.ended.index("c1"))
    assert made.ended.index("c2") < min(made.started.index("c3"), made.started.index("c4"))


async def test_at_most_eight_calls_run_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    made = Calls()
    standing_in(monkeypatch, made)
    model = Script([reads(*["grep"] * 12), ModelTurn("Done.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.final_text, made.most, CONCURRENT) == ("Done.", 8, 8)
    assert [record["ran"] for record in transcript.tool_records] == [f"c{n}" for n in range(12)]


async def test_read_class_calls_that_keep_their_order_run_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = Calls()
    standing_in(monkeypatch, made)
    box = toolbox()
    asking = ToolSpec(ASK_USER, "Ask the user.", {"type": "object"})

    async def never(request: object) -> ToolOutcome:
        raise AssertionError("Toolbox.call stands in")

    box.entries[ASK_USER] = ToolEntry(asking, "read", never)
    assert ASK_USER in IN_ORDER
    model = Script([reads("grep", ASK_USER, "grep"), ModelTurn("Done.")])
    await run_task(model, box, "go", run_id="r")
    assert made.most == 1


async def test_a_toolbox_that_relays_its_calls_runs_them_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = Calls()

    class Relayed(Toolbox):
        """As a confined run's toolbox, which hands each call to its host."""

        async def call(self, call: ToolCall, operation: str, **kwargs: Any) -> ToolOutcome:
            return await made(self, call)

    model = Script([reads("grep", "grep", "read_file"), ModelTurn("Done.")])
    transcript = await run_task(
        model, Relayed(cast(WorkerClient, object()), CONTEXT), "go", run_id="r"
    )
    assert (transcript.final_text, made.most) == ("Done.", 1)


@pytest.mark.parametrize("names", [("grep", "grep", "grep"), ("shell", "shell", "shell")])
async def test_a_call_that_raised_fails_the_run_with_the_results_before_it(
    monkeypatch: pytest.MonkeyPatch, names: tuple[str, str, str]
) -> None:
    made = Calls()
    made.raising["c1"] = UnknownEffectError("worker_unknown_effect", "the worker is gone")
    standing_in(monkeypatch, made)
    model = Script([reads(*names), ModelTurn("never asked")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert (transcript.stopped, transcript.failure) == ("error", "tool")
    assert [record["ran"] for record in transcript.tool_records] == ["c0"]


async def test_images_follow_the_turns_results_in_the_replys_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = Calls()
    made.gates["c0"] = asyncio.Event()
    for n in (0, 1):
        notice = Notice("images", (f"i{n}",), IMAGES.text, {"path": f"f{n}.png"})
        made.images[f"c{n}"] = (notice, f"token-{n}")
    original = made.__call__

    async def call(box: Toolbox, call: ToolCall, *args: object, **kwargs: object) -> ToolOutcome:
        outcome = await original(box, call, *args, **kwargs)
        if call.id == "c1":
            made.gates["c0"].set()
        return outcome

    monkeypatch.setattr(Toolbox, "call", call)
    model = Script([reads("read_file", "read_file"), ModelTurn("Done.")])
    await run_task(model, toolbox(), "go", run_id="r")
    tail = model.requests[1][-4:]
    assert [m["role"] for m in tail] == ["tool", "tool", "user", "user"]
    assert [m.get(IMAGE) for m in tail[2:]] == ["token-0", "token-1"]


# --- Error streaks -------------------------------------------------------------------


def failing(n: int, name: str = "no_such_tool") -> ModelTurn:
    return ModelTurn(None, (ToolCall(f"f{n}", name, "{}"),))


async def test_three_failed_calls_of_one_tool_bring_one_nudge() -> None:
    model = Script([*(failing(n) for n in range(5)), ModelTurn("Done.")])
    transcript = await run_task(model, toolbox(), "go", run_id="r")
    assert transcript.final_text == "Done."
    nudged = [said(request, ERROR_STREAK.text) for request in model.requests]
    # Seen first by the call after the third failure, and never again in the streak.
    assert [len(found) for found in nudged] == [0, 0, 0, 1, 1, 1]
    assert nudged[3] == [{"tool": "no_such_tool", "error": "invalid_request", "failed_calls": 3}]
    assert STREAK == 3


async def test_a_polling_tools_failures_bring_no_nudge() -> None:
    model = Script([*(failing(n, "wait_jobs") for n in range(4)), ModelTurn("Done.")])
    await run_task(model, toolbox(), "go", run_id="r")
    assert not said(model.requests[-1], ERROR_STREAK.text)


async def test_the_streak_counts_the_calls_of_one_reply_in_its_order() -> None:
    model = Script([calls("no_such_tool", "no_such_tool", "no_such_tool"), ModelTurn("Done.")])
    await run_task(model, toolbox(), "go", run_id="r")
    request = model.requests[1]
    assert said(request, ERROR_STREAK.text) == [
        {"tool": "no_such_tool", "error": "invalid_request", "failed_calls": 3}
    ]
    # After all of the reply's results.
    assert [m["role"] for m in request[-4:]] == ["tool", "tool", "tool", "user"]
