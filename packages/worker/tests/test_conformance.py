# SPDX-License-Identifier: MIT
"""Backend conformance: the execution-tools fixtures through the worker API (D52).

The tests reach the worker only through its HTTP API, so they check any backend
adapter without the harness (docs/specs/tools.md, "Acceptance fixtures"). By
default they run against workers with the local backend, which the suite starts:
one over the stdio transport and one over the sandbox daemon, which production
uses.
To check a worker that is already running, such as one with the OpenShell backend
in a lab, set SS_CONFORMANCE_WORKER_URL, SS_CONFORMANCE_TOKEN_FILE and
SS_CONFORMANCE_ENVIRONMENT (docs/WORKER.md, "Conformance suite"); the local runs
are then skipped.

Each test works in a session of its own and names every operation afresh, so runs
against a long-lived worker do not meet each other's state.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import socket
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio

from sandbox_science.contracts import Capabilities, Execution, Location, new_id
from sandbox_science.contracts.worker import MAX_EVENT_BYTES

from .harness import Harness

URL_VARIABLE = "SS_CONFORMANCE_WORKER_URL"
TOKEN_VARIABLE = "SS_CONFORMANCE_TOKEN_FILE"
ENVIRONMENT_VARIABLE = "SS_CONFORMANCE_ENVIRONMENT"
LOCAL = ("local-stdio", "local-daemon")

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]


class Target:
    """A worker under test, reached through its HTTP API, and a session of the test's own."""

    def __init__(
        self, kind: str, http: aiohttp.ClientSession, url: str, token: str, environment_id: str
    ) -> None:
        self.kind = kind
        self.http = http
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.environment_id = environment_id
        self.context = self.new_session()

    def new_session(self) -> dict[str, str]:
        """The context of a new session in the environment under test."""
        return {
            "project_id": "conformance",
            "session_id": new_id("conformance"),
            "environment_id": self.environment_id,
        }

    async def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with self.http.post(self.url + path, json=body, headers=self.headers) as response:
            return response.status, await response.json()

    async def get(self, path: str) -> tuple[int, dict[str, Any]]:
        async with self.http.get(self.url + path, headers=self.headers) as response:
            return response.status, await response.json()

    async def submit(
        self, operation_id: str | None = None, context: dict[str, str] | None = None, **request: Any
    ) -> dict[str, Any]:
        status, execution = await self.post(
            "/v1/executions",
            {
                "operation_id": operation_id or new_id("op"),
                "context": context or self.context,
                **request,
            },
        )
        assert status in (200, 201), execution
        return execution

    async def events(
        self, execution_id: str, after: int = 0
    ) -> AsyncGenerator[dict[str, Any], None]:
        async with self.http.get(
            f"{self.url}/v1/executions/{execution_id}/events",
            params={"after": str(after)},
            headers=self.headers,
            timeout=aiohttp.ClientTimeout(total=None),
        ) as response:
            assert response.status == 200
            # A result event can be longer than aiohttp's own line limit.
            while line := await response.content.readline(max_line_length=MAX_EVENT_BYTES):
                if line.strip():  # an empty line is a heartbeat
                    yield json.loads(line)

    async def finish(self, execution_id: str, timeout: float = 180) -> dict[str, Any]:
        async def follow() -> dict[str, Any]:
            async with contextlib.aclosing(self.events(execution_id)) as events:
                async for event in events:
                    if event["type"] == "result":
                        return event["execution"]
            raise AssertionError("the event stream ended without a result")

        return await asyncio.wait_for(follow(), timeout)

    async def run(self, context: dict[str, str] | None = None, **request: Any) -> dict[str, Any]:
        execution = await self.submit(context=context, **request)
        return await self.finish(execution["execution_id"])

    async def wait_running(self, execution_id: str, timeout: float = 120) -> None:
        async def follow() -> None:
            async with contextlib.aclosing(self.events(execution_id)) as events:
                async for event in events:
                    if event["type"] == "state" and event["state"] == "running":
                        return
            raise AssertionError("the execution never ran")

        await asyncio.wait_for(follow(), timeout)

    async def cancel(self, execution_id: str) -> None:
        status, execution = await self.get(f"/v1/executions/{execution_id}")
        assert status == 200, execution
        status, body = await self.post(
            f"/v1/executions/{execution_id}/cancel", {"context": execution["context"]}
        )
        assert status == 200, body

    async def read(self, path: str) -> tuple[int, dict[str, Any]]:
        return await self.post("/v1/files/read", {"context": self.context, "path": path})

    async def publish(self, path: str) -> dict[str, Any]:
        status, artifact = await self.post("/v1/artifacts", {"context": self.context, "path": path})
        assert status == 201, artifact
        return artifact


def _running_worker() -> tuple[str, str, str] | None:
    """The URL, token file and environment of a running worker to check, if any is named."""
    names = (URL_VARIABLE, TOKEN_VARIABLE, ENVIRONMENT_VARIABLE)
    values = [os.environ.get(name, "") for name in names]
    if not any(values):
        return None
    if not all(values):
        missing = [name for name, value in zip(names, values, strict=True) if not value]
        raise RuntimeError(f"set {' and '.join(missing)} as well, or none of {', '.join(names)}")
    url, token_file, environment_id = values
    return url, token_file, environment_id


RUNNING = _running_worker()
if RUNNING is None:
    TARGETS = [
        *LOCAL,
        pytest.param(
            "external",
            marks=pytest.mark.skip(
                reason=f"set {URL_VARIABLE}, {TOKEN_VARIABLE} and {ENVIRONMENT_VARIABLE} "
                "to check a running worker"
            ),
        ),
    ]
else:
    TARGETS = [
        *(
            pytest.param(
                local, marks=pytest.mark.skip(reason=f"checking the worker at {RUNNING[0]} instead")
            )
            for local in LOCAL
        ),
        "external",
    ]


@pytest_asyncio.fixture(params=TARGETS)
async def worker(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Target]:
    if request.param == "external":
        assert RUNNING is not None
        url, token_file, environment_id = RUNNING
        token = Path(token_file).read_text().strip()
        async with aiohttp.ClientSession() as http:
            yield Target("external", http, url, token, environment_id)
        return
    daemon = request.param == "local-daemon"
    async with await Harness.start(tmp_path, daemon=daemon, pool_id="conformance") as local:
        yield Target("local", local.http, local.url, local.token, "env1")


# Shell calls.


async def test_shell_basics(worker: Target) -> None:
    execution = await worker.run(kind="shell", script="echo hello; echo err >&2; exit 3")
    Execution.model_validate(execution)
    assert execution["state"] == "failed"
    result = execution["result"]
    assert result["exit_code"] == 3
    assert result["stdout"]["text"] == "hello\n"
    assert result["stderr"]["text"] == "err\n"


async def test_results_say_where_they_ran(worker: Target, tmp_path: Path) -> None:
    # Fixture 1's location evidence, the isolation-tier probe (D43) and fixture 12's
    # host-only sentinel. The shell call reports what the code itself sees.
    host_only = tmp_path / "host-only"
    host_only.write_text("on the test's host, outside any sandbox\n")
    script = (
        "uname -n; uname -r; id -u; cat /proc/sys/kernel/random/boot_id 2>/dev/null "
        f"|| echo unknown; test -e {host_only} && echo visible || echo hidden"
    )
    execution = await worker.run(kind="shell", script=script)
    assert execution["state"] == "succeeded", execution["result"]
    lines = execution["result"]["stdout"]["text"].splitlines()
    hostname, release, uid, boot_id, host_file = lines
    location = Location.model_validate(execution["location"])
    evidence = location.isolation_evidence
    assert evidence is not None, "the worker reports no isolation evidence"
    facts = evidence.sandbox
    assert facts is not None, f"the probe failed: {evidence.error}"
    # The probe describes the sandbox that runs the code.
    assert location.hostname == facts.hostname == hostname
    assert (facts.kernel_release, facts.uid) == (release, int(uid))
    assert (facts.boot_id or "unknown") == boot_id
    # The backend gives the tier it declares: the probe found nothing to lower it by,
    # and the evidence agrees with the rule in docs/WORKER.md.
    tier, declared = location.isolation_tier, location.declared_isolation_tier
    assert tier is not None
    assert declared is not None
    assert tier == declared, f"declared {declared}, in effect {tier}: {evidence.findings}"
    if evidence.kernel != "separate":
        assert tier <= 1
        if facts.seccomp != "filter" or facts.no_new_privs is not True:
            assert tier == 0
    if evidence.sentinel != "hidden":
        assert tier == 0
    if tier >= 1:
        assert host_file == "hidden"
    # Capabilities report the same place.
    status, body = await worker.get(f"/v1/capabilities?environment_id={worker.environment_id}")
    assert status == 200, body
    capabilities = Capabilities.model_validate(body)
    assert capabilities.location == location
    assert "python" in capabilities.languages
    if worker.kind == "local":
        # Beside the worker, without isolation, and the probe says so.
        assert location.backend == "local"
        assert (tier, declared) == (0, 0)
        assert (evidence.kernel, evidence.sentinel) == ("shared", "visible")
        assert hostname == socket.gethostname()
        assert host_file == "visible"
        assert capabilities.languages == ("python",)


async def test_argv_runs_without_a_shell(worker: Target) -> None:
    execution = await worker.run(kind="shell", argv=["printf", "%s|", "$HOME", "a b"])
    assert execution["state"] == "succeeded"
    assert execution["result"]["stdout"]["text"] == "$HOME|a b|"


async def test_cancelling_a_shell_call_stops_its_processes(worker: Target) -> None:
    execution = await worker.submit(kind="shell", script="sleep 300 & echo $! > bg.pid; sleep 300")
    await worker.wait_running(execution["execution_id"])
    for _ in range(100):
        status, body = await worker.read("bg.pid")
        if status == 200 and base64.b64decode(body["content_b64"]).strip():
            break
        await asyncio.sleep(0.1)
    await worker.cancel(execution["execution_id"])
    cancelled = await worker.finish(execution["execution_id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["result"]["error"]["kind"] == "cancelled"
    # The background process is gone too, and collected: never left a zombie.
    check = await worker.run(
        kind="shell",
        script=(
            "pid=$(cat bg.pid); for i in $(seq 50); do "
            "state=$(sed -n 's/.*) \\(.\\).*/\\1/p' /proc/$pid/stat 2>/dev/null); "
            'if [ -z "$state" ]; then echo gone; exit 0; fi; '
            "sleep 0.1; done; echo alive"
        ),
    )
    assert check["result"]["stdout"]["text"] == "gone\n"


async def test_output_bounds(worker: Target) -> None:
    # At the default limit, so the result event is longer than an HTTP client's usual
    # line limit (review H1).
    script = "printf 'HEAD\\n'; yes a | head -c 5242870; printf 'TAIL\\n'"
    execution = await worker.run(kind="shell", script=script)
    stdout = execution["result"]["stdout"]
    assert stdout["truncated"] is True
    assert stdout["total_bytes"] == 5242880
    assert stdout["text"].startswith("HEAD\na\n")
    assert stdout["text"].endswith("a\nTAIL\n")
    assert "bytes omitted" in stdout["text"]
    assert 1000000 < len(stdout["text"]) < 1100000


async def test_timeouts(worker: Target) -> None:
    shell = await worker.run(kind="shell", script="sleep 30", timeout_seconds=1)
    assert shell["state"] == "timed_out", shell
    cell = await worker.run(kind="cell", code="import time\ntime.sleep(30)", timeout_seconds=1)
    assert cell["state"] == "timed_out"
    assert cell["result"]["error"]["kind"] == "timed_out"


# Kernels.


async def test_stateful_cells(worker: Target) -> None:
    first = await worker.run(kind="cell", code="x = 41")
    second = await worker.run(kind="cell", code="x + 1")
    assert first["state"] == second["state"] == "succeeded"
    assert second["result"]["outputs"] == [
        {"type": "result", "data": {"text/plain": "42"}, "execution_count": 2}
    ]
    assert first["result"]["kernel"] == second["result"]["kernel"]
    assert second["result"]["kernel"]["generation"] == 1
    assert second["result"]["state_lost"] is False


async def test_cell_exceptions_and_latex_outputs(worker: Target) -> None:
    failed = await worker.run(kind="cell", code="1/0")
    assert failed["state"] == "failed"
    error = failed["result"]["error"]
    assert (error["kind"], error["name"]) == ("exception", "ZeroDivisionError")
    assert any("ZeroDivisionError" in line for line in error["traceback"])
    latex = await worker.run(
        kind="cell",
        code="from IPython.display import Math, display\ndisplay(Math(r'\\frac{1}{2}'))",
    )
    (output,) = latex["result"]["outputs"]
    assert output["type"] == "display"
    assert output["data"]["text/latex"] == "$\\displaystyle \\frac{1}{2}$"


async def test_interrupting_a_cell_keeps_its_state(worker: Target) -> None:
    await worker.run(kind="cell", code="x = 41")
    sleeping = await worker.submit(kind="cell", code="import time\ntime.sleep(60)")
    await worker.wait_running(sleeping["execution_id"])
    await asyncio.sleep(0.5)
    started = asyncio.get_running_loop().time()
    await worker.cancel(sleeping["execution_id"])
    cancelled = await worker.finish(sleeping["execution_id"])
    assert cancelled["state"] == "cancelled"
    assert asyncio.get_running_loop().time() - started < 5
    # Fixture 4: the cell ends with the kernel's KeyboardInterrupt.
    error = cancelled["result"]["error"]
    assert (error["kind"], error["name"]) == ("cancelled", "KeyboardInterrupt")
    assert any("KeyboardInterrupt" in line for line in error["traceback"])
    after = await worker.run(kind="cell", code="x")
    assert after["result"]["outputs"][0]["data"]["text/plain"] == "41"
    assert after["result"]["state_lost"] is False


async def test_queued_cells_can_be_cancelled(worker: Target) -> None:
    first = await worker.submit(kind="cell", code="import time\ntime.sleep(30)")
    await worker.wait_running(first["execution_id"])
    second = await worker.submit(kind="cell", code="'second'")
    await asyncio.sleep(0.3)
    await worker.cancel(second["execution_id"])
    cancelled = await worker.finish(second["execution_id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["started_at"] is None
    await worker.cancel(first["execution_id"])
    assert (await worker.finish(first["execution_id"]))["state"] == "cancelled"


async def test_output_streams_while_the_cell_runs(worker: Target) -> None:
    code = "import time\nfor i in range(5):\n    print(i, flush=True)\n    time.sleep(0.5)"
    execution = await worker.submit(kind="cell", code=code)
    loop = asyncio.get_running_loop()
    arrivals: list[float] = []
    ended: float | None = None
    async with contextlib.aclosing(worker.events(execution["execution_id"])) as events:
        async for event in events:
            if event["type"] == "stream":
                arrivals.append(loop.time())
            elif event["type"] == "result":
                ended = loop.time()
                assert event["execution"]["state"] == "succeeded"
    assert ended is not None
    assert len(arrivals) >= 4, arrivals
    # Printed over two seconds, so streamed as printed, not delivered at the end.
    assert arrivals[-1] - arrivals[0] > 1.0
    assert ended - arrivals[0] > 1.0


async def test_reconnecting_replays_each_event_once(worker: Target) -> None:
    code = "import time\nfor i in range(1, 11):\n    print(i, flush=True)\n    time.sleep(0.3)"
    execution = await worker.submit(kind="cell", code=code)
    execution_id = execution["execution_id"]
    seen: list[dict[str, Any]] = []
    async with contextlib.aclosing(worker.events(execution_id)) as events:
        async for event in events:
            seen.append(event)
            if sum(item["type"] == "stream" for item in seen) == 3:
                break  # the client goes away
    rest = [event async for event in worker.events(execution_id, after=seen[-1]["seq"])]
    sequence = [event["seq"] for event in seen + rest]
    assert sequence == list(range(1, len(sequence) + 1))
    assert sum(event["type"] == "result" for event in seen + rest) == 1
    printed = "".join(event["text"] for event in seen + rest if event["type"] == "stream")
    assert printed.split() == [str(i) for i in range(1, 11)]
    final = rest[-1]["execution"]
    assert final["state"] == "succeeded"
    assert final["last_seq"] == rest[-1]["seq"]


async def test_restart_loses_state_and_says_so(worker: Target) -> None:
    await worker.run(kind="cell", code="x = 41")
    status, kernel = await worker.post(
        "/v1/kernels/restart", {"context": worker.context, "language": "python"}
    )
    assert status == 200, kernel
    assert kernel["generation"] == 2
    after = await worker.run(kind="cell", code="x")
    assert after["result"]["error"]["name"] == "NameError"
    assert after["result"]["kernel"]["generation"] == 2
    assert after["result"]["state_lost"] is True


async def test_conditional_recovery_restart_preserves_newly_live_kernel(worker: Target) -> None:
    await worker.run(kind="cell", code="x = 41")
    status, refusal = await worker.post(
        "/v1/kernels/restart",
        {"context": worker.context, "language": "python", "only_if_absent": True},
    )
    assert status == 409, refusal
    preserved = await worker.run(kind="cell", code="x")
    assert preserved["result"]["outputs"][0]["data"]["text/plain"] == "41"
    assert preserved["result"]["kernel"]["generation"] == 1


async def test_a_run_has_kernels_of_its_own(worker: Target) -> None:
    """PD-37: a cell naming a run runs in that run's kernel, which shares no state with
    the session's or another run's; the session's kernel status lists them all."""
    first = await worker.run(kind="cell", code="x = 'session'")
    await worker.run(kind="cell", code="x = 'a'", run="run-a")
    in_a = await worker.run(kind="cell", code="x", run="run-a")
    in_b = await worker.run(kind="cell", code="x", run="run-b")
    in_session = await worker.run(kind="cell", code="x")
    assert in_a["result"]["outputs"][0]["data"]["text/plain"] == "'a'"
    assert in_b["result"]["error"]["name"] == "NameError"
    assert in_session["result"]["outputs"][0]["data"]["text/plain"] == "'session'"
    kernels = {
        execution["result"]["kernel"]["run"]: execution["result"]["kernel"]["kernel_id"]
        for execution in (in_a, in_b, in_session)
    }
    assert len(set(kernels.values())) == 3
    assert set(kernels) == {"run-a", "run-b", None}
    # The kernel each cell started on names its run too, over the daemon as over stdio.
    assert [execution["kernel"]["run"] for execution in (in_a, in_b, in_session)] == [
        "run-a",
        "run-b",
        None,
    ]
    status, listing = await worker.post("/v1/kernels/status", {"context": worker.context})
    assert status == 200, listing
    assert sorted((k["run"] or "", k["language"]) for k in listing["kernels"]) == [
        ("", "python"),
        ("run-a", "python"),
        ("run-b", "python"),
    ]
    # Restarting a run's kernel restarts only that one.
    status, restarted = await worker.post(
        "/v1/kernels/restart", {"context": worker.context, "language": "python", "run": "run-a"}
    )
    assert status == 200, restarted
    assert (restarted["run"], restarted["kernel_id"]) == ("run-a", kernels["run-a"])
    after = await worker.run(kind="cell", code="x", run="run-a")
    assert after["result"]["error"]["name"] == "NameError"
    assert (after["kernel"]["run"], after["kernel"]["generation"]) == ("run-a", 2)
    session = await worker.run(kind="cell", code="x")
    assert session["result"]["outputs"][0]["data"]["text/plain"] == "'session'"
    # The session's kernel keeps its own generation and start revision: a run's kernel
    # starting is never taken for the session's.
    for observed in (session["kernel"], session["result"]["kernel"]):
        assert {key: observed[key] for key in ("kernel_id", "generation", "start_revision")} == {
            key: first["result"]["kernel"][key]
            for key in ("kernel_id", "generation", "start_revision")
        }
    # A run that ends stops its own kernels only.
    status, closed = await worker.post(
        "/v1/sessions/close", {"context": worker.context, "run": "run-b"}
    )
    assert status == 200, closed
    assert [(k["run"], k["language"]) for k in closed["kernels"]] == [("run-b", "python")]
    status, listing = await worker.post("/v1/kernels/status", {"context": worker.context})
    assert sorted(k["run"] or "" for k in listing["kernels"]) == ["", "run-a"]
    session = await worker.run(kind="cell", code="x")
    assert session["result"]["outputs"][0]["data"]["text/plain"] == "'session'"


async def test_kernel_death_is_reported(worker: Target) -> None:
    before = await worker.run(kind="cell", code="x = 41")
    died = await worker.run(kind="cell", code="import os\nos._exit(1)")
    assert died["state"] == "failed"
    assert died["result"]["error"]["kind"] == "kernel_died"
    after = await worker.run(kind="cell", code="1 + 1")
    assert after["result"]["kernel"]["generation"] == before["result"]["kernel"]["generation"] + 1
    assert after["result"]["state_lost"] is True


# Files and publication.


async def test_publication(worker: Target) -> None:
    shell = await worker.run(kind="shell", script="printf 'a,b\\n1,2\\n' > out.csv")
    assert shell["result"]["written_files"] == ["out.csv"]
    artifact = await worker.publish("out.csv")
    assert artifact["sha256"] == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert artifact["media_type"] == "text/csv"
    assert artifact["size"] == 8
    assert artifact["produced_by"] == shell["attempt_id"]
    async with worker.http.get(
        f"{worker.url}/v1/artifacts/{artifact['artifact_id']}/content", headers=worker.headers
    ) as response:
        assert await response.read() == b"a,b\n1,2\n"
        assert response.headers["X-Content-SHA256"] == artifact["sha256"]


async def test_publication_names_the_last_attempt_that_wrote_the_file(worker: Target) -> None:
    cell = await worker.run(kind="cell", code="open('a.txt', 'w').write('from the cell')")
    shell = await worker.run(kind="shell", script="echo from the shell > b.txt")
    rewrite = await worker.run(kind="shell", script="echo again > a.txt")
    assert cell["result"]["written_files"] == ["a.txt"]
    assert shell["result"]["written_files"] == ["b.txt"]
    assert (await worker.publish("a.txt"))["produced_by"] == rewrite["attempt_id"]
    assert (await worker.publish("b.txt"))["produced_by"] == shell["attempt_id"]
    # The file API is not an execution: a file written through it has no known producer.
    content = base64.b64encode(b"x").decode()
    await worker.post(
        "/v1/files/write", {"context": worker.context, "path": "c.txt", "content_b64": content}
    )
    assert (await worker.publish("c.txt"))["produced_by"] is None


async def test_a_call_reports_the_files_it_deleted(worker: Target) -> None:
    """RA-33: a deleted file is reported, when the whole workspace was compared."""
    name = f"{worker.context['session_id']}-gone.txt"
    made = await worker.run(
        kind="shell", script=f"mkdir -p d && echo x > d/a.txt && echo y > $PROJECT_SHARED/{name}"
    )
    assert made["result"]["deleted_files"] == []
    assert made["result"]["deleted_files_truncated"] is False
    gone = await worker.run(
        kind="cell",
        code="import os\nos.remove('d/a.txt')\n"
        f"os.remove(os.environ['PROJECT_SHARED'] + '/{name}')",
    )
    assert gone["state"] == "succeeded", gone["result"]
    assert gone["result"]["deleted_files"] == ["d/a.txt", f"shared/{name}"]
    assert gone["result"]["written_files"] == []
    assert gone["result"]["deleted_files_truncated"] is False


async def test_shared_files_count_writers_from_any_session(worker: Target) -> None:
    name = f"{worker.context['session_id']}.txt"
    writer = await worker.run(
        context=worker.new_session(), kind="shell", script=f"echo hi > $PROJECT_SHARED/{name}"
    )
    assert writer["state"] == "succeeded", writer["result"]
    assert writer["result"]["written_files"] == [f"shared/{name}"]
    assert (await worker.publish(f"shared/{name}"))["produced_by"] == writer["attempt_id"]


async def test_file_round_trip_and_preconditions(worker: Target) -> None:
    content = base64.b64encode(b"hello").decode()
    status, written = await worker.post(
        "/v1/files/write",
        {"context": worker.context, "path": "notes/a.txt", "content_b64": content},
    )
    assert status == 200, written
    status, read = await worker.read("notes/a.txt")
    assert base64.b64decode(read["content_b64"]) == b"hello"
    assert read["sha256"] == written["sha256"] == hashlib.sha256(b"hello").hexdigest()
    status, listing = await worker.post(
        "/v1/files/list", {"context": worker.context, "path": "notes"}
    )
    assert [entry["name"] for entry in listing["entries"]] == ["a.txt"]
    path = f"shared/{worker.context['session_id']}.txt"
    shared = {"context": worker.context, "path": path, "content_b64": content}
    status, body = await worker.post("/v1/files/write", shared)
    assert status == 400
    assert "expected_sha256 or create_only" in body["error"]["message"]
    status, _ = await worker.post("/v1/files/write", {**shared, "create_only": True})
    assert status == 200
    status, body = await worker.post("/v1/files/write", {**shared, "expected_sha256": "0" * 64})
    assert status == 409
    assert body["error"]["kind"] == "conflict"


async def test_two_sessions_editing_one_shared_file_get_an_explicit_conflict(
    worker: Target,
) -> None:
    # W3: a deliberate edit of a shared file names the version it replaces, so of two
    # sessions that read the same version, the second writer gets `conflict` and
    # nothing is silently overwritten.
    first, second = worker.context, worker.new_session()
    path = f"shared/{first['session_id']}-results.txt"

    async def write(context: dict[str, str], data: bytes, **precondition: Any) -> tuple[int, Any]:
        body = {"context": context, "path": path, "content_b64": base64.b64encode(data).decode()}
        return await worker.post("/v1/files/write", {**body, **precondition})

    status, created = await write(first, b"v1", create_only=True)
    assert status == 200, created
    status, again = await write(second, b"v1 again", create_only=True)
    assert (status, again["error"]["kind"]) == (409, "conflict")
    status, seen = await worker.post("/v1/files/read", {"context": second, "path": path})
    assert seen["sha256"] == created["sha256"]
    status, updated = await write(first, b"v2 by the first", expected_sha256=created["sha256"])
    assert status == 200, updated
    status, stale = await write(second, b"v2 by the second", expected_sha256=seen["sha256"])
    assert (status, stale["error"]["kind"]) == (409, "conflict")
    status, final = await worker.post("/v1/files/read", {"context": second, "path": path})
    assert base64.b64decode(final["content_b64"]) == b"v2 by the first"
    status, retried = await write(second, b"v3 by the second", expected_sha256=final["sha256"])
    assert status == 200, retried


async def test_confinement(worker: Target) -> None:
    made = await worker.run(
        kind="shell", script="ln -s /etc/passwd link; mkdir inside; ln -s inside dirlink"
    )
    assert made["state"] == "succeeded", made["result"]
    for path in ("../x", "/etc/passwd", "a/../b", "link", "dirlink/x"):
        status, body = await worker.read(path)
        assert status == 400, (path, body)
        assert body["error"]["kind"] == "invalid_request"
    content = base64.b64encode(b"x").decode()
    status, body = await worker.post(
        "/v1/files/write", {"context": worker.context, "path": "dirlink/x", "content_b64": content}
    )
    assert (status, body["error"]["kind"]) == (400, "invalid_request")
    status, body = await worker.post(
        "/v1/files/list", {"context": worker.context, "path": "dirlink"}
    )
    assert (status, body["error"]["kind"]) == (400, "invalid_request")
    status, listing = await worker.post(
        "/v1/files/list", {"context": worker.context, "path": "inside"}
    )
    assert (status, listing["entries"]) == (200, [])


# The API itself.


async def test_idempotent_submission(worker: Target) -> None:
    operation = new_id("op")
    first = await worker.submit(operation, kind="shell", script="echo once")
    again = await worker.submit(operation, kind="shell", script="echo once")
    assert again["execution_id"] == first["execution_id"]
    status, body = await worker.post(
        "/v1/executions",
        {
            "operation_id": operation,
            "context": worker.context,
            "kind": "shell",
            "script": "echo twice",
        },
    )
    assert status == 409
    assert body["error"]["kind"] == "conflict"
    finished = await worker.finish(first["execution_id"])
    assert finished["result"]["stdout"]["text"] == "once\n"


async def test_requests_need_the_token(worker: Target) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        async with worker.http.get(
            f"{worker.url}/v1/executions/exe-1", headers=headers
        ) as response:
            assert response.status == 401


async def test_unknown_executions_are_not_found(worker: Target) -> None:
    status, body = await worker.get(f"/v1/executions/{new_id('exe')}")
    assert status == 404
    assert body["error"]["kind"] == "not_found"
