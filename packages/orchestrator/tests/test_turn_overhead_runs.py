# SPDX-License-Identifier: MIT
"""Long confined runs: the overhead savings leave every model request as it was, and the
platform's own time before a call stays bounded at a long transcript."""

from __future__ import annotations

import re
import statistics
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.orchestrator.confinement import host as host_module
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.projects import RunRecord
from sandbox_science.orchestrator.worker_client import dump

from . import loop_harness
from .loop_harness import Gateway, Harness, Reply, call
from .test_confinement_loop import confined

pytestmark = pytest.mark.slow

PER_TOKEN: Final = 3.5
"""The characters per token the scripted gateway reports, about a real model's."""


@asynccontextmanager
async def scripted_gateway() -> AsyncIterator[Gateway]:
    """``loop_harness.gateway``, reporting tokens as a real model would and taking a
    request of a long transcript (aiohttp's default limit is 1 MiB)."""
    found = Gateway(per_token=PER_TOKEN)
    app = web.Application(client_max_size=64 << 20)
    app.router.add_post("/chat/completions", found.handle)
    app.router.add_post("/responses", found.respond)
    app.router.add_post("/messages", found.message)
    app.router.add_get("/models", found.models)
    app.router.add_get("/model/info", found.model_info)
    app.router.add_post("/embeddings", found.embeddings)
    async with TestServer(app) as server:
        found.url = str(server.make_url("/")).rstrip("/")
        yield found


@asynccontextmanager
async def environment(tmp_path: Path) -> AsyncIterator[Harness]:
    """A harness of its own, with its worker and gateway, in ``tmp_path``."""
    tmp_path.mkdir()
    # The fixtures' own functions, outside pytest: a test may need two harnesses.
    worker = asynccontextmanager(cast(Any, loop_harness.worker).__wrapped__)
    harness = cast(Any, loop_harness.harness).__wrapped__
    async with worker(tmp_path) as running, scripted_gateway() as gateway:
        yield await harness(tmp_path, running, gateway)


@pytest_asyncio.fixture
async def long_harness(tmp_path: Path) -> AsyncIterator[Harness]:
    async with environment(tmp_path / "run") as found:
        yield found


def script(turns: int, chars: int) -> list[Reply]:
    """A run of ``turns`` shell calls, each printing ``chars`` characters, then an answer."""
    calls = [
        Reply(calls=[call("shell", script=f"python3 -c \"print('turn {n} ' + 'x' * {chars})\"")])
        for n in range(turns)
    ]
    return [*calls, Reply("Done.")]


_VARYING: Final = re.compile(
    r"msg-[0-9a-f]{16}|run-[0-9a-f]{24}|\d{8}T\d{6}\d*Z|"
    r"(?:exe|att|cap|art|ev|kernel|krn)-[0-9a-f]{16,48}|"
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:\+00:00|Z)?|"
    r"\"(?:duration_ms|elapsed_ms|wall_ms|elapsed_seconds)\":\s*[0-9.]+"
)


def normalized(requests: list[list[dict[str, Any]]]) -> Iterator[str]:
    """Each request's messages as text, every run-specific identifier, time and
    duration named by its first appearance in the run, so that two runs of one script
    compare equal exactly when their requests have the same shape and text."""
    names: dict[str, str] = {}

    def name(found: re.Match[str]) -> str:
        return names.setdefault(found.group(0), f"<{len(names)}>")

    for messages in requests:
        yield _VARYING.sub(name, dump(messages))


async def requests_of(
    harness: Harness, tmp_path: Path, turns: int, chars: int
) -> tuple[list[str], list[RunRecord]]:
    records = await confined(harness, tmp_path, *script(turns, chars), max_turns=turns + 5)
    return list(normalized(harness.gateway.requests["main"])), records


def every_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The arrangement before the savings: every hook asked for every method, and the
    conversation's size counted afresh at each use."""
    monkeypatch.setattr(host_module, "overrides", lambda hook, name: True)

    def recounted(context: AgentContext) -> int:
        return 2 + sum(len(dump(m)) + 2 for m in context.working()) + context.tools_chars

    monkeypatch.setattr(AgentContext, "size", property(recounted))


async def test_a_long_confined_runs_requests_are_the_same_with_and_without_the_savings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt cache needs each request's prefix byte for byte: counting the view's
    size once and leaving out the round trips of hooks that do nothing change no
    request of a run."""
    turns, chars = 40, 6_000
    asked: list[str] = []
    answer = host_module.Prepared.call

    async def counted(self: host_module.Prepared, kind: Any, value: Any) -> Any:
        asked.append(kind.value)
        return await answer(self, kind, value)

    monkeypatch.setattr(host_module.Prepared, "call", counted)
    async with environment(tmp_path / "now") as harness:
        now, _ = await requests_of(harness, tmp_path / "now", turns, chars)
    trips, asked[:] = len(asked), []
    with monkeypatch.context() as before:
        every_round_trip(before)
        async with environment(tmp_path / "before") as harness:
            then, _ = await requests_of(harness, tmp_path / "before", turns, chars)
    # Each turn's hooks took fewer round trips across the boundary.
    assert trips < len(asked) // 2, (trips, len(asked))
    assert len(now) == len(then) == turns + 1
    for index, (ours, theirs) in enumerate(zip(now, then, strict=True)):
        assert ours == theirs, f"request {index} differs"


PRECALL_BOUND_MS: Final = 600
"""The most the platform's own time before a call (``prepared_ms``) may take, in the
median, once a confined run's transcript passes ``LONG_TOKENS``: about five times what
it takes on a developer's machine (about 110 ms), for a loaded CI runner."""
LONG_TOKENS: Final = 300_000


async def test_the_platforms_time_before_a_call_stays_bounded_at_a_long_transcript(
    long_harness: Harness, tmp_path: Path
) -> None:
    """A benchmark: past 300,000 tokens of transcript, the loop's work before each call
    (its hooks, the fold's check and the call's intent) stays within its bound, as each
    call's usage records it."""
    records = await confined(long_harness, tmp_path, *script(95, 12_000), max_turns=100)
    usage = [u for u in records[-1].usage if (u.input_tokens or 0) >= LONG_TOKENS]
    prepared = [u.prepared_ms for u in usage if u.prepared_ms is not None]
    recorded = [u.recorded_ms for u in usage if u.recorded_ms is not None]
    assert len(recorded) == len(prepared)  # each after a turn of tool calls
    assert len(prepared) >= 5, [u.input_tokens for u in records[-1].usage][-5:]
    assert statistics.median(prepared) < PRECALL_BOUND_MS, prepared
