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
import os
import re
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

from sandbox_science.contracts import (
    ErrorInfo,
    ErrorKind,
    Execution,
    ExecutionContext,
    IsolationEvidence,
    Location,
    Result,
    ResultEvent,
    RichOutput,
    ShellRequest,
    StreamEvent,
    StreamOutput,
    WriteFileRequest,
)
from sandbox_science.orchestrator import cli, worker_client
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import (
    ModelError,
    OpenAICompatibleModel,
    ScriptedModel,
    ToolCall,
    ToolSpec,
    _parse_completion,  # pyright: ignore[reportPrivateUsage]
)
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome, operation_id
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
    # The host's own kernelspecs (a real SageMath, say) stay out of the tests.
    env = {**os.environ, "JUPYTER_DATA_DIR": str(tmp_path / "jupyter-data")}
    env.pop("JUPYTER_PATH", None)
    process = subprocess.Popen(  # noqa: S603 - our own command
        [str(command), "serve", "--settings", str(tmp_path / "worker.toml")],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
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


async def test_the_cli_refuses_an_unisolated_worker(
    worker_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "token").write_text(TOKEN)
    (tmp_path / "script.json").write_text(json.dumps([{"text": "hi"}]))
    common = [
        "run", "--worker", worker_url, "--token-file", str(tmp_path / "token"),
        "--project", "p1", "--session", "s1", "--environment", "env1",
        "--script", str(tmp_path / "script.json"),
    ]  # fmt: skip
    refused = await asyncio.to_thread(cli.main, [*common, "task"])
    assert refused == 2
    # The refusal gives the probe's evidence.
    error = capsys.readouterr().err
    assert "the worker offers isolation tier 0: " in error
    assert "the sandbox runs on the worker host's kernel (same boot ID)" in error
    allowed = await asyncio.to_thread(
        cli.main,
        [*common, "--allow-unisolated", "--transcript", str(tmp_path / "t.json"), "task"],
    )
    assert allowed == 0
    assert json.loads((tmp_path / "t.json").read_text())["stopped"] == "answered"


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_only_a_probed_tier_counts() -> None:
    declared = Location(
        pool_id="p", backend="openshell", hostname="h", isolation_tier=3, declared_isolation_tier=3
    )
    # A worker that did not probe offers no tier at all, whatever it declares.
    assert cli.tier_in_effect(declared) is None
    assert cli.describe_isolation(declared) == "no isolation evidence"
    probed = declared.model_copy(
        update={"isolation_evidence": IsolationEvidence(kernel="separate", sentinel="hidden")}
    )
    assert cli.tier_in_effect(probed) == 3
    assert cli.describe_isolation(probed) == "isolation tier 3"
    lowered = declared.model_copy(
        update={
            "isolation_tier": 0,
            "isolation_evidence": IsolationEvidence(
                kernel="shared", sentinel="visible", findings=("one thing", "another")
            ),
        }
    )
    assert cli.tier_in_effect(lowered) == 0
    assert cli.describe_isolation(lowered) == "isolation tier 0 (declared 3): one thing; another"


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_the_toolbox_offers_the_kernels_the_environment_has() -> None:
    client = WorkerClient("http://127.0.0.1:9", TOKEN)
    names = {spec.name for spec in Toolbox(client, CONTEXT).specs}
    assert {"python", "restart_kernel"} <= names
    assert "sage" not in names
    both = Toolbox(client, CONTEXT, languages=("python", "sage"))
    assert "sage" in {spec.name for spec in both.specs}
    (restart,) = [spec for spec in both.specs if spec.name == "restart_kernel"]
    assert restart.parameters["properties"]["language"]["enum"] == ["python", "sage"]
    none = {spec.name for spec in Toolbox(client, CONTEXT, languages=()).specs}
    assert not none & {"python", "sage", "restart_kernel"}


async def test_a_tool_the_environment_lacks_is_refused() -> None:
    async with WorkerClient("http://127.0.0.1:9", TOKEN) as client:
        toolbox = Toolbox(client, CONTEXT)
        (call,) = (
            ScriptedModel.from_json(
                [{"tool_calls": [{"name": "sage", "arguments": {"code": "1"}}]}]
            )
            ._turns[0]
            .tool_calls
        )  # pyright: ignore[reportPrivateUsage]
        outcome = json.loads((await toolbox.call(call, "op-sage")).text)
    assert outcome["error"]["message"] == "there is no tool named 'sage'"


def _call(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments))


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_operation_ids_name_the_whole_call() -> None:
    # Review, Low: a long run prefix cut the call ID off, so different calls shared one.
    shell = _call("shell", {"script": "echo a"})
    identifier = operation_id("run/1 t0", shell)
    assert identifier.startswith("run-1-t0-call-1-")
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identifier)
    prefix = "r" * 200
    first = operation_id(prefix, _call("shell", {}, "call_AAAA"))
    second = operation_id(prefix, _call("shell", {}, "call_BBBB"))
    assert first != second
    assert max(len(first), len(second)) <= 128
    assert operation_id("run", shell) == operation_id("run", shell)
    assert operation_id("run", shell) != operation_id("run", _call("shell", {"script": "echo b"}))


async def test_a_reused_run_id_marks_replayed_results(client: WorkerClient) -> None:
    # Review, Low: a reused --run-id silently returned the earlier run's results.
    turn = [{"tool_calls": [{"name": "shell", "arguments": {"script": "date +%s%N"}}]}]
    script = [*turn, {"text": "ok"}]
    runs = [
        await run_task(
            ScriptedModel.from_json(script), Toolbox(client, CONTEXT), "t", run_id="run-1"
        )
        for _ in range(2)
    ]
    before, after = (json.loads(run.messages[3]["content"]) for run in runs)
    assert "replayed" not in before
    assert after["attempt_id"] == before["attempt_id"]
    assert after["replayed"].startswith("this call was made before")
    assert runs[1].tool_records[0]["replayed"] is True
    # A different call under the same run ID runs, instead of meeting a conflict.
    other = [{"tool_calls": [{"name": "shell", "arguments": {"script": "echo other"}}]}]
    third = await run_task(
        ScriptedModel.from_json([*other, {"text": "ok"}]),
        Toolbox(client, CONTEXT),
        "t",
        run_id="run-1",
    )
    assert json.loads(third.messages[3]["content"])["stdout"] == "other\n"


async def test_h1_large_results_reach_the_model(client: WorkerClient) -> None:
    # aiohttp's line limit (512 KiB) ended run_task on any longer result event; spec
    # fixture 10 failed at the default output limit.
    script = [
        {"tool_calls": [{"name": "shell", "arguments": {"script": "yes a | head -c 540000"}}]},
        {"tool_calls": [{"name": "shell", "arguments": {"script": "yes b | head -c 5242880"}}]},
        {"text": "done"},
    ]
    toolbox = Toolbox(client, CONTEXT)
    transcript = await run_task(ScriptedModel.from_json(script), toolbox, "large", run_id="run-2")
    assert transcript.stopped == "answered"
    tools = [message["content"] for message in transcript.messages if message["role"] == "tool"]
    small, large = (json.loads(text) for text in tools)
    assert small["state"] == large["state"] == "succeeded"
    assert large["stdout_total_bytes"] == 5242880
    assert all(len(text) <= toolbox.output_chars for text in tools)


def _event(seq: int, **fields: Any) -> bytes:
    body = {"execution_id": "exe-1", "seq": seq, "time": "2026-10-11T00:00:00Z", **fields}
    return json.dumps(body).encode() + b"\n"


EXECUTION = {
    "execution_id": "exe-1",
    "attempt_id": "att-1",
    "operation_id": "op-1",
    "kind": "shell",
    "context": CONTEXT.model_dump(),
    "state": "succeeded",
    "submitted_at": "2026-10-11T00:00:00Z",
}


async def test_h1_the_client_reads_every_event_line_the_worker_may_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "x" * (20 * 1024 * 1024)

    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(_event(1, type="stream", name="stdout", text=text))
        await response.write(_event(2, type="result", execution=EXECUTION))
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/v1/executions/exe-1/events", events)
    async with TestServer(app) as server, WorkerClient(str(server.make_url("")), TOKEN) as client:
        received = [item async for item in client.events("exe-1")]
        assert [item.seq for item in received] == [1, 2]
        first = received[0]
        assert isinstance(first, StreamEvent)
        assert len(first.text) == len(text)
        # A line over the limit is an error the caller can handle, not a crash.
        monkeypatch.setattr(worker_client, "MAX_EVENT_BYTES", 1024 * 1024)
        with pytest.raises(WorkerAPIError) as raised:
            [item async for item in client.events("exe-1")]
        assert raised.value.kind is ErrorKind.INTERNAL


async def test_h2_heartbeats_keep_a_quiet_stream_open_and_silence_ends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The client had no read timeout, so a stream that went quiet hung it forever.
    monkeypatch.setattr(worker_client, "RECONNECT_DELAY", 0.01)
    asked: list[str] = []

    async def quiet(request: web.Request) -> web.StreamResponse:
        asked.append(request.query["after"])
        response = web.StreamResponse()
        await response.prepare(request)
        for _ in range(10):
            await response.write(b"\n")
            await asyncio.sleep(0.1)
        await response.write(_event(1, type="result", execution=EXECUTION))
        await response.write_eof()
        return response

    async def silent(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await asyncio.sleep(60)
        return response

    app = web.Application()
    app.router.add_get("/v1/executions/exe-1/events", quiet)
    app.router.add_get("/v1/executions/exe-2/events", silent)
    async with (
        TestServer(app) as server,
        WorkerClient(str(server.make_url("")), TOKEN, stream_timeout=0.5) as client,
    ):
        assert [item.seq async for item in client.events("exe-1")] == [1]
        assert asked == ["0"]  # a second of heartbeats, and never a reconnection
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(WorkerAPIError) as raised:
            [item async for item in client.events("exe-2")]
        assert raised.value.kind is ErrorKind.UNAVAILABLE
        assert loop.time() - started < 10


async def test_h2_run_gives_up_when_no_result_comes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_client, "RESULT_GRACE", 0.5)

    async def submit(request: web.Request) -> web.Response:
        return web.json_response({**EXECUTION, "state": "queued"}, status=201)

    async def forever(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        while True:  # heartbeats, and never a result
            await response.write(b"\n")
            await asyncio.sleep(0.1)

    app = web.Application()
    app.router.add_post("/v1/executions", submit)
    app.router.add_get("/v1/executions/exe-1/events", forever)
    request = ShellRequest(operation_id="op-1", context=CONTEXT, script="true", timeout_seconds=0.5)
    async with TestServer(app) as server, WorkerClient(str(server.make_url("")), TOKEN) as client:
        with pytest.raises(WorkerAPIError) as raised:
            await asyncio.wait_for(client.run(request), 10)
    assert raised.value.kind is ErrorKind.UNAVAILABLE


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_m4_tool_results_are_bounded_as_a_whole() -> None:
    # 20,000 displays gave the model 149 KB; a 300 KB exception message, 600 KB.
    toolbox = Toolbox(WorkerClient("http://127.0.0.1:9", TOKEN), CONTEXT)
    outputs = tuple(RichOutput(type="display", data={"text/plain": str(i)}) for i in range(20000))
    error = ErrorInfo(
        kind=ErrorKind.EXCEPTION, message="ValueError: " + "y" * 300000, traceback=("t" * 300000,)
    )
    stdout = StreamOutput(name="stdout", text="z" * 300000, total_bytes=300000)
    execution = Execution.model_validate(
        {
            **EXECUTION,
            "state": "failed",
            "result": Result(outputs=outputs, error=error, stdout=stdout).model_dump(),
        }
    )
    text = toolbox._summary(execution)  # pyright: ignore[reportPrivateUsage]
    assert len(text) <= toolbox.output_chars
    summary = json.loads(text)
    assert summary["outputs"][10] == "[… 19980 outputs omitted …]"
    assert summary["outputs"][-1] == "19999"
    assert "characters omitted" in summary["error"]["message"]


async def test_m5_inputs_that_crashed_the_harness_are_tool_errors() -> None:
    async def read(request: web.Request) -> web.Response:
        # Content that is not base64, as a compromised executor could have sent through.
        body = {"path": "x", "sha256": "0" * 64, "size": 1, "offset": 0, "eof": True}
        return web.json_response({**body, "content_b64": "a"})

    app = web.Application()
    app.router.add_post("/v1/files/read", read)
    async with TestServer(app) as server, WorkerClient(str(server.make_url("")), TOKEN) as client:
        toolbox = Toolbox(client, CONTEXT)
        surrogate = ToolCall("c1", "write_file", '{"path": "a.txt", "content": "\\ud800"}')
        written = json.loads((await toolbox.call(surrogate, "op-a")).text)
        assert written["error"]["kind"] == "invalid_request"
        read_back = json.loads((await toolbox.call(_call("read_file", {"path": "x"}), "op-b")).text)
        assert read_back["error"]["kind"] == "internal"


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
@pytest.mark.parametrize(
    "body",
    [
        {"choices": [{"message": "hi"}]},
        {"choices": [{"message": {"tool_calls": "x"}}]},
        {"choices": [{"message": {"tool_calls": ["x"]}}]},
        {"choices": [{"message": {"tool_calls": [{"function": "x"}]}}]},
        {"choices": "x"},
        [1],
    ],
)
def test_m5_malformed_completions_are_model_errors(body: object) -> None:
    with pytest.raises(ModelError):
        _parse_completion(json.dumps(body))  # pyright: ignore[reportPrivateUsage]


async def test_m5_one_failing_tool_call_does_not_end_the_task() -> None:
    class Failing(Toolbox):
        async def _list(self, args: dict[str, Any], operation: str) -> ToolOutcome:
            raise RuntimeError("something nobody expected")

    script = [{"tool_calls": [{"name": "list_files", "arguments": {}}]}, {"text": "carried on"}]
    async with WorkerClient("http://127.0.0.1:9", TOKEN) as client:
        transcript = await run_task(
            ScriptedModel.from_json(script), Failing(client, CONTEXT), "task", run_id="run-3"
        )
    assert transcript.final_text == "carried on"
    assert json.loads(transcript.messages[3]["content"])["error"]["kind"] == "internal"


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


async def test_the_pdf_tools_read_cite_and_render_in_the_sandbox(client: WorkerClient) -> None:
    pdf_oxide = pytest.importorskip("pdf_oxide")
    capabilities = await client.capabilities(CONTEXT.environment_id)
    assert capabilities.features == ("pdf",)
    toolbox = Toolbox(client, CONTEXT, features=capabilities.features)
    assert {"pdf_info", "pdf_pages", "pdf_search", "pdf_render"} <= {s.name for s in toolbox.specs}
    note = "# A Note on GF(17)\n\nFor every a in GF(17), a^17 = a.\n"
    data = bytes(pdf_oxide.Pdf.from_markdown(note).to_bytes())
    await client.write_file(
        WriteFileRequest(
            context=CONTEXT, path="note.pdf", content_b64=base64.b64encode(data).decode()
        )
    )
    digest = hashlib.sha256(data).hexdigest()

    async def tool(name: str, operation: str, **arguments: Any) -> str:
        return (await toolbox.call(_call(name, arguments), operation)).text

    info = await tool("pdf_info", "op-pdf-info", path="note.pdf")
    assert info.startswith(f"note.pdf (sha256 {digest[:16]}…): 1 pages")
    assert "pages (physical: printed label): 1: 1" in info
    pages = await tool("pdf_pages", "op-pdf-pages", path="note.pdf", pages="1")
    assert "--- page 1 (printed 1) ---" in pages
    assert "[p1:1] A Note on GF(17)" in pages
    hits = await tool("pdf_search", "op-pdf-search", path="note.pdf", query="gf(17)")
    assert hits.startswith("2 hits for 'gf(17)'")
    assert "[p1:1] (printed 1) A Note on GF(17)" in hits
    rendered = await tool(
        "pdf_render", "op-pdf-render", path="note.pdf", page=1, region=[72, 640, 288, 720], dpi=100
    )
    assert "rendered region [72.0, 640.0, 288.0, 720.0] of page 1" in rendered
    assert "300x112 px" in rendered
    png = rendered.split(" to ", 1)[1].split(":", 1)[0]
    published = json.loads(await tool("publish", "op-pdf-publish", path=png))
    assert published["published"]["media_type"] == "image/png"
    missing = json.loads(await tool("pdf_pages", "op-pdf-missing", path="gone.pdf", pages="1"))
    assert missing["error"]["kind"] == "invalid_request"
    assert "does not exist" in missing["error"]["message"]


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_the_pdf_tools_need_the_feature() -> None:
    client = WorkerClient("http://127.0.0.1:9", TOKEN)
    assert not {"pdf_pages", "pdf_render"} & {s.name for s in Toolbox(client, CONTEXT).specs}
