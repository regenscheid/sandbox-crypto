# SPDX-License-Identifier: MIT
"""The harness end to end: a scripted model, the tool loop and a real worker service.

The worker runs as a separate process through its own command line, with the
local backend (no isolation), so these tests exercise the HTTP boundary the
orchestrator and the worker share in production.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import ErrorKind, ExecutionContext, ResultEvent, ShellRequest
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import OpenAICompatibleModel, ScriptedModel, ToolSpec
from sandbox_science.orchestrator.tools import Toolbox, operation_id
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

pytestmark = pytest.mark.asyncio

TOKEN = "k" * 40
CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def worker_url(tmp_path: Path) -> Iterator[str]:
    port = _free_port()
    (tmp_path / "worker.token").write_text(TOKEN + "\n")
    (tmp_path / "worker.toml").write_text(
        f'listen = "127.0.0.1:{port}"\ntoken_file = "worker.token"\nstate_dir = "state"\n'
        f'pool_id = "test"\n\n[local]\npython = "{sys.executable}"\n'
    )
    command = Path(sys.executable).parent / "sandbox-science-worker"
    log = (tmp_path / "worker.log").open("w")
    process = subprocess.Popen(  # noqa: S603 - our own command
        [str(command), "serve", "--settings", str(tmp_path / "worker.toml")],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.2):
                break
            if process.poll() is not None:
                raise AssertionError((tmp_path / "worker.log").read_text())
            subprocess.run(["sleep", "0.1"], check=True)  # noqa: S607
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=30)
        log.close()


@pytest_asyncio.fixture
async def client(worker_url: str) -> AsyncIterator[WorkerClient]:
    async with WorkerClient(worker_url, TOKEN) as worker:
        yield worker


FIRST_SLICE = [
    {"tool_calls": [{"name": "shell", "arguments": {"script": "echo hello from $(hostname)"}}]},
    {"tool_calls": [{"name": "python", "arguments": {"code": "x = 41"}}]},
    {"tool_calls": [{"name": "python", "arguments": {"code": "x + 1"}}]},
    {
        "tool_calls": [
            {"name": "shell", "arguments": {"script": "printf 'a,b\\n1,2\\n' > out.csv"}},
            {"name": "publish", "arguments": {"path": "out.csv"}},
        ]
    },
    {"tool_calls": [{"name": "read_file", "arguments": {"path": "out.csv"}}]},
    {"text": "x + 1 is 42, and out.csv is published."},
]


async def test_the_first_slice_through_the_harness(client: WorkerClient) -> None:
    model = ScriptedModel.from_json(FIRST_SLICE)
    transcript = await run_task(model, Toolbox(client, CONTEXT), "demonstrate", run_id="run-1")
    assert transcript.stopped == "answered"
    assert transcript.final_text == "x + 1 is 42, and out.csv is published."
    results = [json.loads(m["content"]) for m in transcript.messages if m["role"] == "tool"]
    shell, assign, add, write, published, read = results
    assert shell["state"] == "succeeded"
    assert shell["stdout"] == f"hello from {socket.gethostname()}\n"
    assert shell["ran_on"]["isolation_tier"] == 0
    assert assign["state"] == "succeeded"
    assert add["outputs"] == ["42"]
    assert add["kernel_generation"] == 1
    assert write["state"] == "succeeded"
    artifact = published["published"]
    assert artifact["sha256"] == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert artifact["produced_by"] == write["attempt_id"]
    assert read["content"] == "a,b\n1,2\n"
    assert await client.artifact_content(artifact["artifact_id"]) == b"a,b\n1,2\n"
    # The model saw every earlier tool result before its next turn.
    assert len(model.requests) == 6
    assert model.requests[-1][-1]["role"] == "tool"


async def test_a_repeated_tool_call_is_one_execution(client: WorkerClient) -> None:
    toolbox = Toolbox(client, CONTEXT)
    (call,) = (
        ScriptedModel.from_json(
            [{"tool_calls": [{"name": "shell", "arguments": {"script": "echo once"}}]}]
        )
        ._turns[0]
        .tool_calls
    )  # pyright: ignore[reportPrivateUsage]
    first = json.loads((await toolbox.call(call, "op-repeat")).text)
    second = json.loads((await toolbox.call(call, "op-repeat")).text)
    assert first["attempt_id"] == second["attempt_id"]


async def test_tool_errors_reach_the_model(client: WorkerClient) -> None:
    script = [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "../etc/passwd"}}]},
        {"tool_calls": [{"name": "python", "arguments": {"code": "1/0"}}]},
        {"tool_calls": [{"name": "no_such_tool", "arguments": {}}]},
        {"text": "done"},
    ]
    transcript = await run_task(
        ScriptedModel.from_json(script), Toolbox(client, CONTEXT), "fail", run_id="run-2"
    )
    confined, raised, unknown = [
        json.loads(m["content"]) for m in transcript.messages if m["role"] == "tool"
    ]
    assert confined["error"]["kind"] == "invalid_request"
    assert raised["error"]["kind"] == "exception"
    assert "ZeroDivisionError" in raised["error"]["traceback"]
    assert unknown["error"]["message"] == "there is no tool named 'no_such_tool'"


async def test_an_unreachable_worker_runs_nothing(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    async with WorkerClient(f"http://127.0.0.1:{_free_port()}", TOKEN) as unreachable:
        toolbox = Toolbox(unreachable, CONTEXT)
        script = [{"tool_calls": [{"name": "shell", "arguments": {"script": f"touch {sentinel}"}}]}]
        (call,) = ScriptedModel.from_json(script)._turns[0].tool_calls  # pyright: ignore[reportPrivateUsage]
        outcome = json.loads((await toolbox.call(call, "op-x")).text)
    assert outcome["error"]["kind"] == "unavailable"
    assert not sentinel.exists()


async def test_the_cli_refuses_an_unisolated_worker(worker_url: str, tmp_path: Path) -> None:
    (tmp_path / "token").write_text(TOKEN)
    (tmp_path / "script.json").write_text(json.dumps([{"text": "hi"}]))
    common = [
        "run", "--worker", worker_url, "--token-file", str(tmp_path / "token"),
        "--project", "p1", "--session", "s1", "--environment", "env1",
        "--script", str(tmp_path / "script.json"),
    ]  # fmt: skip
    refused = await asyncio.to_thread(cli.main, [*common, "task"])
    assert refused == 2
    allowed = await asyncio.to_thread(
        cli.main,
        [*common, "--allow-unisolated", "--transcript", str(tmp_path / "t.json"), "task"],
    )
    assert allowed == 0
    assert json.loads((tmp_path / "t.json").read_text())["stopped"] == "answered"


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_operation_ids_are_path_safe() -> None:
    (call,) = (
        ScriptedModel.from_json([{"tool_calls": [{"name": "shell", "arguments": {}}]}])
        ._turns[0]
        .tool_calls
    )  # pyright: ignore[reportPrivateUsage]
    assert operation_id("run/1 t0", call) == "run-1-t0-call-1"


async def test_the_openai_adapter() -> None:
    seen: list[dict[str, Any]] = []

    async def completions(request: web.Request) -> web.Response:
        seen.append(await request.json())
        assert request.headers["Authorization"] == "Bearer virtual-key"
        return web.json_response(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {"name": "python", "arguments": '{"code": "1+1"}'},
                                }
                            ],
                        },
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    async with TestServer(app) as server:
        model = OpenAICompatibleModel(str(server.make_url("/v1")), "main", "virtual-key")
        tool = ToolSpec("python", "run python", {"type": "object"})
        turn = await model.complete([{"role": "user", "content": "hi"}], [tool])
        await model.close()
    assert turn.tool_calls[0].name == "python"
    assert json.loads(turn.tool_calls[0].arguments) == {"code": "1+1"}
    assert turn.finish_reason == "tool_calls"
    (body,) = seen
    assert body["model"] == "main"
    assert body["tools"][0]["function"]["name"] == "python"
    assert body["tool_choice"] == "auto"


async def test_the_client_resumes_a_dropped_event_stream() -> None:
    """The first stream breaks after two events; the client asks again from there."""
    calls: list[str] = []
    execution = {
        "execution_id": "exe-1",
        "attempt_id": "att-1",
        "operation_id": "op-1",
        "kind": "shell",
        "context": CONTEXT.model_dump(),
        "state": "succeeded",
        "submitted_at": "2026-10-11T00:00:00Z",
        "last_seq": 4,
    }

    def event(seq: int, **fields: Any) -> bytes:
        body = {"execution_id": "exe-1", "seq": seq, "time": "2026-10-11T00:00:00Z", **fields}
        return json.dumps(body).encode() + b"\n"

    async def events(request: web.Request) -> web.StreamResponse:
        after = request.query["after"]
        calls.append(after)
        response = web.StreamResponse()
        await response.prepare(request)
        if after == "0":
            await response.write(event(1, type="state", state="running"))
            await response.write(event(2, type="stream", name="stdout", text="a"))
            assert request.transport is not None
            request.transport.close()  # the connection drops mid-stream
            return response
        await response.write(event(3, type="stream", name="stdout", text="b"))
        await response.write(event(4, type="result", execution=execution))
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/v1/executions/exe-1/events", events)
    async with TestServer(app) as server, WorkerClient(str(server.make_url("")), TOKEN) as client:
        received = [item async for item in client.events("exe-1")]
    assert [item.seq for item in received] == [1, 2, 3, 4]
    assert isinstance(received[-1], ResultEvent)
    assert calls == ["0", "2"]


async def test_worker_errors_keep_their_kind(client: WorkerClient) -> None:
    with pytest.raises(WorkerAPIError) as raised:
        await client.get("exe-missing")
    assert raised.value.kind is ErrorKind.NOT_FOUND
    request = ShellRequest(operation_id="op-dup", context=CONTEXT, script="echo a")
    await client.run(request)
    with pytest.raises(WorkerAPIError) as conflict:
        await client.submit(request.model_copy(update={"script": "echo b"}))
    assert conflict.value.kind is ErrorKind.CONFLICT
    content = base64.b64encode(b"x").decode()
    assert content == "eA=="
