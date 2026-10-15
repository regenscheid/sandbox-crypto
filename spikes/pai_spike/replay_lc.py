# SPDX-License-Identifier: MIT
"""Option D on the same session: LangChain v1 agents (``create_agent``) and deepagents
(``create_deep_agent``), against the same fake gateway, measured the same way.

Run in the LangChain venv: ``python -m spikes.pai_spike.replay_lc LABS SCRATCH``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestServer
from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    SummarizationMiddleware,
)
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from .fake_litellm import FakeLiteLLM
from .replay import (
    SUMMARIZER,
    Probe,
    Scripter,
    Segment,
    Stats,
    Turn,
    measure,
    present,
    probes,
    segments,
)
from .transcript import load

KEY = "test-secret-langchain-spike-gateway"
RENAMED = {"read_file": "worker_read_file", "write_file": "worker_write_file"}
"""Our tools whose names deepagents' own file tools take."""


def renamed(found: list[Segment]) -> list[Segment]:
    return [
        Segment(
            s.prompt,
            [Turn(t.text, [(RENAMED.get(n, n), a, r) for n, a, r in t.calls]) for t in s.turns],
        )
        for s in found
    ]


class Replay(AgentMiddleware):
    """Answers our recorded tools' calls with their recorded results; the framework's
    own tools (deepagents' grep, read_file) run for real."""

    def __init__(self, scripter: Scripter, names: set[str]) -> None:
        super().__init__()
        self.scripter, self.names = scripter, names

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        call = request.tool_call
        if call["name"] in self.names and call["id"] in self.scripter.results:
            return ToolMessage(
                self.scripter.results[call["id"]], tool_call_id=call["id"], name=call["name"]
            )
        return await handler(request)


def chat(url: str, name: str, *, window: int | None = None) -> ChatOpenAI:
    extra: dict[str, Any] = {"profile": {"max_input_tokens": window}} if window else {}
    return ChatOpenAI(
        model=name, base_url=f"{url}/v1", api_key=KEY, max_retries=0, stream_usage=True, **extra
    )


@dataclass
class Outcome:
    name: str
    stats: Stats
    in_final: int
    recovered: int
    persisted: int
    probes: int

    def line(self) -> str:
        return (
            f"{self.name:22} {self.stats.line()} in_final={self.in_final}/{self.probes} "
            f"recovered={self.recovered}/{self.probes} persisted={self.persisted}/{self.probes}"
        )


Build = Callable[[ChatOpenAI, ChatOpenAI, list[Any], str, list[AgentMiddleware]], Any]


async def replay(
    name: str, system: str, found: list[Segment], picked: list[Probe], build: Build,
    *, recovery: bool,
) -> Outcome:  # fmt: skip
    fake = FakeLiteLLM()
    scripter = Scripter(found)
    fake.responder = scripter
    names = {n for s in found for t in s.turns for n, _a, _r in t.calls}
    tools = [
        StructuredTool.from_function(
            coroutine=_never, name=n, description=f"The recorded {n}.",
            args_schema={"type": "object", "properties": {}, "additionalProperties": True},
        )
        for n in sorted(names)
    ]  # fmt: skip
    async with TestServer(fake.app()) as server:
        fake.url = str(server.make_url("/")).rstrip("/")
        agent = build(
            chat(fake.url, "sandbox-orchestrator", window=70_588),
            chat(fake.url, SUMMARIZER),
            tools,
            system,
            [Replay(scripter, names)],
        )
        config = {"configurable": {"thread_id": "replay-session"}, "recursion_limit": 500}
        for segment in found:
            scripter.start(segment)
            await agent.ainvoke({"messages": [HumanMessage(segment.prompt)]}, config)
        final = next(c.body for c in reversed(scripter.calls) if not c.summary)
        replayed = list(scripter.calls)
        recovered = persisted = 0
        if recovery:
            asks = [("grep", {"pattern": p.query.split()[-1], "path": "/",
                              "output_mode": "content"}, "") for p in picked]  # fmt: skip
            asks += [("grep", {"pattern": p.value, "path": "/", "output_mode": "content"}, "")
                     for p in picked]  # fmt: skip
            scripter.start(Segment("Recover the details.", [Turn(None, asks), Turn("Done.", [])]))
            await agent.ainvoke({"messages": [HumanMessage("Recover the details.")]}, config)
            last = next(c.body for c in reversed(scripter.calls) if not c.summary)
            answers = last["messages"][-2 * len(picked) :]
            for probe, answer in zip(picked, answers[: len(picked)], strict=True):
                recovered += probe.value in json.dumps(answer)
            for probe, answer in zip(picked, answers[len(picked) :], strict=True):
                persisted += probe.value in json.dumps(answer)
    return Outcome(
        name, measure(replayed), sum(present(p, final) for p in picked), recovered, persisted,
        len(picked),
    )  # fmt: skip


async def _never(**_arguments: Any) -> str:
    raise AssertionError("the replay middleware answers recorded tools")


def plain(*middleware: AgentMiddleware) -> Build:
    def build(model: Any, summarizer: Any, tools: list[Any], system: str, replay: list[Any]) -> Any:
        return create_agent(
            model, tools, system_prompt=system, middleware=[*middleware, *replay],
            checkpointer=InMemorySaver(),
        )  # fmt: skip

    return build


def summarizing(trigger: int) -> Build:
    def build(model: Any, summarizer: Any, tools: list[Any], system: str, replay: list[Any]) -> Any:
        middleware = SummarizationMiddleware(
            summarizer, trigger=("tokens", trigger), keep=("messages", 20)
        )
        return create_agent(
            model, tools, system_prompt=system, middleware=[middleware, *replay],
            checkpointer=InMemorySaver(),
        )  # fmt: skip

    return build


def deep(evict_tokens: int | None) -> Build:
    def build(model: Any, summarizer: Any, tools: list[Any], system: str, replay: list[Any]) -> Any:
        from deepagents import create_deep_agent
        from deepagents.backends import StateBackend
        from deepagents.middleware.filesystem import FilesystemMiddleware
        from deepagents.middleware.summarization import SummarizationMiddleware as Summarize

        backend = StateBackend()
        # The core summarization, with the summarizer's alias so its calls are told apart,
        # and the thresholds the factory would derive from a 70,588-token window.
        extra: list[Any] = [
            Summarize(
                model=summarizer, backend=backend, trigger=("tokens", 60_000),
                keep=("tokens", 7_000), trim_tokens_to_summarize=None,
                truncate_args_settings={"trigger": ("tokens", 60_000), "keep": ("tokens", 7_000)},
            )
        ]  # fmt: skip
        if evict_tokens is not None:
            extra.append(
                FilesystemMiddleware(backend=backend, tool_token_limit_before_evict=evict_tokens)
            )
        return create_deep_agent(
            model, tools, system_prompt=system, backend=backend,
            middleware=[*extra, *replay], checkpointer=InMemorySaver(),
        )  # fmt: skip

    return build


async def main(labs: Path) -> None:
    warnings.simplefilter("ignore")
    session = load([(n, labs / f"control-{n}.sqlite3") for n in ("39", "40", "41")])
    found = renamed(segments(session))
    picked = probes(found)
    only = set(sys.argv[2:])
    configs: list[tuple[str, Build, bool]] = [
        ("D0 create_agent", plain(), False),
        ("D1 clear_tool_uses 100k", plain(ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=100_000)])), False),
        ("D1 clear_tool_uses 60k", plain(ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=60_000)])), False),
        ("D2 summarization 60k", summarizing(60_000), False),
        ("D3 deepagents 60k", deep(None), True),
        ("D4 deepagents evict 1k", deep(1_000), True),
    ]  # fmt: skip
    for name, build, recovery in configs:
        if only and name.split()[0] not in only:
            continue
        try:
            outcome = await replay(name, session.system, found, picked, build, recovery=recovery)
        except Exception as error:  # a configuration that cannot replay is a finding too
            print(f"{name:22} failed: {type(error).__name__}: {str(error)[:300]}", flush=True)
            continue
        print(outcome.line(), flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
