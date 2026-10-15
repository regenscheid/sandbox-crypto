# SPDX-License-Identifier: MIT
"""Step 5: the prototype end to end, against the in-process worker the loop tests use
(the real worker app on the local backend) and the fake LiteLLM proxy.

It needs the labs' control-store copies (``SPIKE_LABS``, outside git); without them it
replays a synthetic session of large tool results instead.
"""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.tools import Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

from .fake_litellm import FakeLiteLLM
from .gateway import GatewayRecorder
from .prototype import LIMITS, RunRecord, agent
from .replay import Scripter, Segment, Turn, segments, tokens
from .transcript import Session, load, tool_names

TOKEN = "test-secret-prototype-worker-0123456789"
KEY = "test-secret-prototype-gateway"
CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="env")
TARGET = 30_000


@pytest_asyncio.fixture
async def worker_url(tmp_path: Path) -> AsyncIterator[str]:
    root = tmp_path / "worker"
    root.mkdir()
    store = Store(root / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(python=sys.executable), root),
        Blobs(root / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=root,
    )
    await manager.start()
    try:
        async with TestServer(create_app(manager, TOKEN)) as server:
            yield str(server.make_url("/")).rstrip("/")
    finally:
        await manager.close()
        store.close()


def session() -> Session:
    labs = os.environ.get("SPIKE_LABS")
    if labs:
        return load([(n, Path(labs) / f"control-{n}.sqlite3") for n in ("40",)])
    # A synthetic stand-in: tool results of a few thousand characters each.
    from .transcript import Run

    own = []
    for n in range(60):
        own += [
            {"role": "user", "content": f"Step {n}: compute the next table."},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{n}", "type": "function",
                 "function": {"name": "python", "arguments": json.dumps({"code": f"t{n}()"})}}]},
            {"role": "tool", "tool_call_id": f"c{n}", "content": json.dumps(
                {"rows": [[n, k, k * k] for k in range(300)]})},
            {"role": "assistant", "content": f"Table {n} has 300 rows."},
        ]  # fmt: skip
    return Session("You are a research assistant.", [Run("synthetic", "s", None, own, "")])


async def test_prototype_keeps_requests_under_target_and_recovers_a_cleared_detail(
    worker_url: str, tmp_path: Path
) -> None:
    warnings.simplefilter("ignore")
    recorded = session()
    found = segments(recorded)
    scripter = Scripter(found, schema_tokens=0)
    fake = FakeLiteLLM(responder=scripter)
    recorder = GatewayRecorder()
    record = RunRecord("proto-run")
    async with TestServer(fake.app()) as server, WorkerClient(worker_url, TOKEN) as client:
        fake.url = str(server.make_url("/")).rstrip("/")
        toolbox = Toolbox(client, CONTEXT)
        the_agent = agent(
            fake.url, KEY, toolbox, recorded.system, record, recorder, scripter.results,
            tool_names(recorded.messages), target=TARGET,
        )  # fmt: skip
        config = {"conversation_id": "proto-session", "usage_limits": LIMITS}

        # 1. Our real tools, through the worker. The detail to lose and find again is
        # one only a tool result holds: a random code the shell made, not the model.
        live = Segment(
            "Make a launch code in notes/code.txt, then show me the notes.",
            [
                Turn(None, [("write_file", {"path": "notes/README",
                                            "content": "Launch codes live here.\n"}, "")]),
                Turn(None, [("shell", {"script": "mkdir -p notes; head -c 32 /dev/urandom "
                                       "| sha256sum | cut -d' ' -f1 | sed 's/^/launch code /' "
                                       "| tee notes/code.txt"}, "")]),
                Turn(None, [("read_file", {"path": "notes/code.txt"}, "")]),
                Turn("Done.", []),
            ],
        )  # fmt: skip
        scripter.start(live, live=True)
        result = await the_agent.run(live.prompt, **config)
        history = result.all_messages()
        shown = scripter.calls[2].body["messages"][-1]["content"]
        found_code = re.search(r"launch code ([0-9a-f]{64})", shown)
        assert found_code is not None, shown
        secret = found_code.group(1)

        # 2. A long recorded session pushes the conversation past the target.
        for segment in found:
            scripter.start(segment)
            result = await the_agent.run(segment.prompt, message_history=history, **config)
            history = result.all_messages()
        last_main = next(c.body for c in reversed(scripter.calls) if not c.summary)
        assert secret not in json.dumps(last_main)  # cleared from the live context

        # 3. The model searches its history and gets the detail back.
        ask = Segment(
            "What was the launch code?",
            [Turn(None, [("search_conversation_history", {"query": "launch code notes"}, "")]),
             Turn("Found it.", [])],
        )  # fmt: skip
        scripter.start(ask)
        await the_agent.run(ask.prompt, message_history=history, **config)

    answer = next(c.body for c in reversed(scripter.calls) if not c.summary)["messages"][-1]
    assert answer["role"] == "tool"
    assert secret in answer["content"]

    record.take_calls(recorder)
    record.write(tmp_path / "run-record.json")
    mains = [c for c in record.calls if c["role"] == "main"]
    # Exactly what each call sent: the fake gateway received the same bodies.
    assert [c["sent"] for c in record.calls] == [c.body for c in scripter.calls]
    assert all(c["cost"] == 0.001 for c in record.calls)
    # Under the target, by the gateway's own token count (what the fake reports).
    largest = max(tokens(json.dumps(c["sent"])) for c in mains)
    print(f"\ncalls={len(record.calls)} main={len(mains)} largest={largest:,} "
          f"compactions={len(record.compactions)} tools={len(record.tools)}")  # fmt: skip
    for event in record.compactions[:5]:
        print("compaction", event)
    assert largest <= TARGET * 1.1
    assert record.compactions
    assert [t["tool"] for t in record.tools] == ["write_file", "shell", "read_file"]
    assert record.tools[1]["state"] == "succeeded" if "state" in record.tools[1] else True
    if os.environ.get("SPIKE_RECORD_OUT"):
        record.write(Path(os.environ["SPIKE_RECORD_OUT"]))


@pytest.fixture(autouse=True)
def _no_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYDANTIC_AI_NO_BANNER", "1")
