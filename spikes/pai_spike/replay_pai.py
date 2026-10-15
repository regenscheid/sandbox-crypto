# SPDX-License-Identifier: MIT
"""The recorded session through a Pydantic AI agent with Harness context management.

Run it as a module with the labs' control-store copies' directory and a scratch
directory: ``python -m spikes.pai_spike.replay_pai LABS SCRATCH``. It prints one line
of measurements per configuration and writes them, with the probes, as JSON.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import warnings
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext, UsageLimits
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import (
    ClampOversizedMessages,
    ClearToolResults,
    ConversationSearch,
    DeduplicateFileReads,
    StepPersistence,
    SummarizingCompaction,
    TieredCompaction,
    ToolOutputLimits,
)
from pydantic_ai_harness.conversation_search import SnapshotHistorySource
from pydantic_ai_harness.step_persistence import InMemoryStepStore
from pydantic_ai_harness.tool_output_limits import Band, LocalFileStore, Spill

from .fake_litellm import FakeLiteLLM
from .gateway import GatewayRecorder, gateway_client
from .replay import (
    TOOL_SCHEMA_TOKENS,
    Probe,
    Scripter,
    Segment,
    Stats,
    Turn,
    measure,
    present,
    probes,
    segments,
    tokens,
)
from .transcript import Session, load, tool_names

KEY = "test-secret-pydantic-ai-spike-gateway"
FILE_READS = {
    "read_file": "path",
    "document_read": "document_id",
    "review_finding_read": "finding_id",
    "ledger_read": "entry",
    "plan_read": "plan_id",
}


RECOVERY = frozenset({"search_conversation_history", "read_tool_result"})
"""Tools whose fresh results clearing must leave, or the model never sees what it fetched."""


def file_key(call: ToolCallPart) -> str | None:
    key = FILE_READS.get(call.tool_name)
    args = call.args_as_dict() if key else {}
    return f"{call.tool_name}:{json.dumps(args, sort_keys=True)}" if key else None


def model(url: str, name: str, recorder: GatewayRecorder) -> Any:
    client = AsyncOpenAI(
        base_url=f"{url}/v1", api_key=KEY, http_client=gateway_client(), max_retries=0
    )
    return recorder.wrap(OpenAIChatModel(name, provider=LiteLLMProvider(openai_client=client)))


def replay_toolset(names: Sequence[str], scripter: Scripter) -> FunctionToolset[None]:
    async def recorded(ctx: RunContext[None], **_arguments: Any) -> str:
        assert ctx.tool_call_id is not None
        return scripter.results[ctx.tool_call_id]

    schema = {"type": "object", "additionalProperties": True}
    return FunctionToolset(
        [Tool.from_schema(recorded, name, f"The recorded {name}.", schema, takes_ctx=True)
         for name in names]
    )  # fmt: skip


@dataclass
class Outcome:
    name: str
    stats: Stats
    in_final: int
    recovered: int
    """Probes a search by the words before the value found."""
    persisted: int
    """Probes a search by the value itself found: the ceiling of what search can recover."""
    probes: int
    compactions: list[dict[str, Any]]
    corpus: str = ""

    def line(self) -> str:
        return (
            f"{self.name:22} {self.stats.line()} in_final={self.in_final}/{self.probes} "
            f"recovered={self.recovered}/{self.probes} persisted={self.persisted}/{self.probes} "
            f"{self.corpus}"
        )


Capabilities = Callable[[Any, Path], list[AbstractCapability[Any]]]


async def replay(
    name: str,
    session: Session,
    found: list[Segment],
    picked: list[Probe],
    capabilities: Capabilities,
    scratch: Path,
    *,
    recovery_tool: str | None,
) -> Outcome:
    fake = FakeLiteLLM()
    scripter = Scripter(found)
    fake.responder = scripter
    recorder = GatewayRecorder()
    async with TestServer(fake.app()) as server:
        fake.url = str(server.make_url("/")).rstrip("/")
        summarizer = model(fake.url, "summarizer", recorder)
        caps = capabilities(summarizer, scratch / name)
        agent = Agent(
            model(fake.url, "sandbox-orchestrator", recorder),
            instructions=session.system,
            toolsets=[replay_toolset(tool_names(session.messages), scripter)],
            capabilities=caps,
        )
        history = None
        limits = UsageLimits(request_limit=200)
        for segment in found:
            scripter.start(segment)
            result = await agent.run(
                segment.prompt,
                message_history=history,
                conversation_id="replay-session",
                usage_limits=limits,
            )
            history = result.all_messages()
        final = next(c.body for c in reversed(scripter.calls) if not c.summary)
        replayed = list(scripter.calls)
        recovered = persisted = 0
        corpus = ""
        if recovery_tool is not None:
            asks = [(recovery_tool, {"query": p.query}, "") for p in picked]
            asks += [(recovery_tool, {"query": p.value}, "") for p in picked]
            recovery = Segment("Recover the details.", [Turn(None, asks), Turn("Done.", [])])
            scripter.start(recovery)
            await agent.run(
                recovery.prompt,
                message_history=history,
                conversation_id="replay-session",
                usage_limits=limits,
            )
            last = next(c.body for c in reversed(scripter.calls) if not c.summary)
            answers = last["messages"][-2 * len(picked) :]
            for probe, answer in zip(picked, answers[: len(picked)], strict=True):
                recovered += probe.value in json.dumps(answer)
            for probe, answer in zip(picked, answers[len(picked) :], strict=True):
                persisted += probe.value in json.dumps(answer)
            found_in = re.search(r"in (\d+) persisted messages", json.dumps(answers))
            corpus = f"corpus={found_in.group(1)}msgs" if found_in else ""
    stats = measure(replayed)
    compactions = [
        {"call": n, "input_tokens": t, "shared": s}
        for n, (t, s) in enumerate(stats.per_call)
        if n and s < stats.per_call[n - 1][0]
    ]
    return Outcome(
        name,
        stats,
        sum(present(p, final) for p in picked),
        recovered,
        persisted,
        len(picked),
        compactions,
        corpus,
    )


def tiered(target: int, *, keep_messages: int = 20) -> Capabilities:
    def make(summarizer: Any, scratch: Path) -> list[AbstractCapability[Any]]:
        store = InMemoryStepStore()
        return [
            StepPersistence(store=store),
            ConversationSearch(SnapshotHistorySource(store), scope="conversation"),
            TieredCompaction(
                tiers=[
                    ClampOversizedMessages(max_part_tokens=20_000),
                    DeduplicateFileReads(file_key=file_key),
                    ClearToolResults(max_tokens=1, keep_pairs=3, exclude_tools=RECOVERY),
                    SummarizingCompaction(
                        model=summarizer, max_messages=1, keep_messages=keep_messages
                    ),
                ],
                target_tokens=target,
            ),
        ]

    return make


def spill_then(inner: Capabilities, over: int) -> Capabilities:
    def make(summarizer: Any, scratch: Path) -> list[AbstractCapability[Any]]:
        scratch.mkdir(parents=True, exist_ok=True)
        limits = ToolOutputLimits(
            bands=[Band(over=over, action=Spill())], store=LocalFileStore(base_dir=scratch)
        )
        return [limits, *inner(summarizer, scratch)]

    return make


def search_only(summarizer: Any, scratch: Path) -> list[AbstractCapability[Any]]:
    store = InMemoryStepStore()
    return [
        StepPersistence(store=store),
        ConversationSearch(SnapshotHistorySource(store), scope="conversation"),
    ]


def nothing(summarizer: Any, scratch: Path) -> list[AbstractCapability[Any]]:
    return []


async def main(labs: Path, scratch: Path, out: Path) -> None:
    warnings.simplefilter("ignore")
    session = load([(n, labs / f"control-{n}.sqlite3") for n in ("39", "40", "41")])
    found = segments(session)
    picked = probes(found)
    print(f"session: {len(found)} segments, {len(session.messages)} messages, "
          f"{tokens(json.dumps(session.messages)):,} tokens of messages "
          f"+ {TOOL_SCHEMA_TOKENS:,} of schemas; {len(picked)} probes")  # fmt: skip
    configs: list[tuple[str, Capabilities, str | None]] = [
        ("none", nothing, None),
        ("search-only", search_only, "search_conversation_history"),
        ("tiered-100k", tiered(100_000), "search_conversation_history"),
        ("tiered-60k", tiered(60_000), "search_conversation_history"),
        ("tiered-40k", tiered(40_000), "search_conversation_history"),
        ("spill4k+tiered-60k", spill_then(tiered(60_000), 4_000), "search_conversation_history"),
    ]
    outcomes: list[Outcome] = []
    only = set(sys.argv[4:])
    for name, caps, tool in configs:
        if only and name not in only:
            continue
        outcome = await replay(name, session, found, picked, caps, scratch, recovery_tool=tool)
        print(outcome.line(), flush=True)
        outcomes.append(outcome)
    out.write_text(
        json.dumps(
            {
                "probes": [asdict(p) for p in picked],
                "outcomes": [
                    {**asdict(o), "stats": {**asdict(o.stats), "per_call": o.stats.per_call}}
                    for o in outcomes
                ],
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
