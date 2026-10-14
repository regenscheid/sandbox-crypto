# SPDX-License-Identifier: MIT
"""What the execution manager does, through the worker's API, on both transports.

These tests pin the manager's main paths as callers see them: the HTTP API, and the
public methods the environments and the provisioner call (``release``,
``release_if_idle``, ``activity``). They read no private state, so they stay the same
while the manager is split into parts (plan item C-03). Each runs over the stdio
transport and the daemon, which production uses.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.testing import eventually

from .harness import CONTEXT, Harness, context

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(params=["stdio", "daemon"])
async def worker(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, daemon=request.param == "daemon") as started:
        yield started


async def _events(worker: Harness, execution_id: str, after: int = 0) -> list[dict[str, Any]]:
    """Every event of an ended execution from ``after`` on."""
    return [event async for event in worker.events(execution_id, after)]


# The execution journal: one record per operation, events stored in order, replayed.


async def test_a_shell_call_reports_states_output_and_one_result(worker: Harness) -> None:
    execution = await worker.run("op-shell", kind="shell", script="echo out; echo err >&2")
    assert execution["state"] == "succeeded"
    assert execution["attempt_id"].startswith("att-")
    assert execution["result"]["exit_code"] == 0
    assert execution["result"]["stdout"]["text"] == "out\n"
    assert execution["result"]["stderr"]["text"] == "err\n"
    events = await _events(worker, execution["execution_id"])
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert events[-1]["type"] == "result"
    assert [event["type"] for event in events].count("result") == 1
    states = [event["state"] for event in events if event["type"] == "state"]
    assert states == ["queued", "running", "succeeded"]
    streams = {event["name"]: event["text"] for event in events if event["type"] == "stream"}
    assert streams == {"stdout": "out\n", "stderr": "err\n"}
    assert execution["last_seq"] == events[-1]["seq"]


async def test_events_replay_after_any_sequence_number(worker: Harness) -> None:
    execution = await worker.run("op-replay", kind="shell", script="echo a; echo b")
    events = await _events(worker, execution["execution_id"])
    for after in range(len(events)):
        assert await _events(worker, execution["execution_id"], after) == events[after:]
    assert await _events(worker, execution["execution_id"], len(events)) == []


async def test_an_operation_id_names_one_execution(worker: Harness) -> None:
    first = await worker.run("op-once", kind="shell", script="echo once")
    status, again = await worker.post(
        "/v1/executions",
        {"operation_id": "op-once", "context": CONTEXT, "kind": "shell", "script": "echo once"},
    )
    assert (status, again["execution_id"]) == (200, first["execution_id"])
    assert again["state"] == "succeeded"
    status, other = await worker.post(
        "/v1/executions",
        {"operation_id": "op-once", "context": CONTEXT, "kind": "shell", "script": "echo twice"},
    )
    assert status == 409, other
    assert other["error"]["kind"] == "conflict"


async def test_an_execution_is_visible_to_its_project_only(worker: Harness) -> None:
    execution = await worker.run("op-scope", kind="shell", script="true")
    path = f"/v1/executions/{execution['execution_id']}"
    status, body = await worker.get(path + "?project_id=p1")
    assert (status, body["execution_id"]) == (200, execution["execution_id"])
    status, body = await worker.get(path + "?project_id=p2")
    assert status == 404, body
    status, body = await worker.get("/v1/executions/exe-" + "0" * 24)
    assert status == 404, body


# Cells and kernels: one kernel per session and language, numbered by generation.


async def test_cells_share_a_kernel_until_it_restarts(worker: Harness) -> None:
    first = await worker.run("op-c1", kind="cell", code="x = 41")
    assert first["state"] == "succeeded"
    kernel = first["result"]["kernel"]
    assert (kernel["language"], kernel["generation"]) == ("python", 1)
    second = await worker.run("op-c2", kind="cell", code="x + 1")
    assert second["result"]["outputs"][0]["data"]["text/plain"] == "42"
    assert second["result"]["kernel"]["generation"] == 1
    assert second["result"]["state_lost"] is False
    status, listing = await worker.post("/v1/kernels/status", {"context": CONTEXT})
    assert status == 200, listing
    assert [(k["language"], k["generation"]) for k in listing["kernels"]] == [("python", 1)]
    status, restarted = await worker.post(
        "/v1/kernels/restart", {"context": CONTEXT, "language": "python"}
    )
    assert status == 200, restarted
    assert restarted["generation"] == 2
    after = await worker.run("op-c3", kind="cell", code="x")
    assert after["result"]["error"]["name"] == "NameError"
    assert after["result"]["kernel"]["generation"] == 2


async def test_a_failing_cell_fails_with_its_error(worker: Harness) -> None:
    failed = await worker.run("op-raise", kind="cell", code="raise ValueError('no')")
    assert failed["state"] == "failed"
    error = failed["result"]["error"]
    assert error["name"] == "ValueError"
    assert error["message"].endswith("no")


async def test_observing_kernels_never_connects_but_kernel_status_does(worker: Harness) -> None:
    idle = context(environment_id="env-idle")
    status, observed = await worker.post("/v1/kernels/observe", {"context": idle})
    assert status == 503, observed
    assert observed["error"]["kind"] == "unavailable"
    status, listing = await worker.post("/v1/kernels/status", {"context": idle})
    assert (status, listing["kernels"]) == (200, [])
    status, observed = await worker.post("/v1/kernels/observe", {"context": idle})
    assert (status, observed["kernels"]) == (200, [])


async def test_closing_a_session_ends_its_kernels_and_running_calls(worker: Harness) -> None:
    await worker.run("op-k1", kind="cell", code="kept = 1")
    other = context(session_id="s2")
    await worker.run("op-k2", other, kind="cell", code="other = 2")
    sleeping = await worker.submit("op-k3", kind="cell", code="import time; time.sleep(60)")
    await worker.wait_running(sleeping["execution_id"])
    status, closed = await worker.post("/v1/sessions/close", {"context": CONTEXT})
    assert status == 200, closed
    assert [kernel["language"] for kernel in closed["kernels"]] == ["python"]
    ended = await worker.finish(sleeping["execution_id"])
    assert ended["state"] == "cancelled"
    still = await worker.run("op-k4", other, kind="cell", code="other")
    assert still["result"]["outputs"][0]["data"]["text/plain"] == "2", still
    fresh = await worker.run("op-k5", kind="cell", code="kept")
    assert fresh["result"]["error"]["name"] == "NameError"


# Dispatch and cancellation.


async def test_cancelling_a_running_call_ends_it_cancelled(worker: Harness) -> None:
    execution = await worker.submit("op-cancel", kind="shell", script="sleep 60")
    await worker.wait_running(execution["execution_id"])
    cancelling = await worker.cancel(execution["execution_id"])
    assert cancelling["state"] in ("cancelling", "cancelled")
    ended = await worker.finish(execution["execution_id"])
    assert ended["state"] == "cancelled"
    again = await worker.cancel(execution["execution_id"])
    assert again["state"] == "cancelled"


async def test_a_call_past_its_time_limit_times_out(worker: Harness) -> None:
    ended = await worker.run("op-limit", kind="shell", script="sleep 60", timeout_seconds=1)
    assert ended["state"] == "timed_out"


async def test_jobs_are_listed_and_report_their_status(worker: Harness) -> None:
    job = await worker.submit(
        "op-job",
        kind="job",
        script="echo started; sleep 60",
        purpose="characterize jobs",
        expected_progress="none",
    )
    await worker.wait_running(job["execution_id"])
    query = "project_id=p1&session_id=s1&environment_id=env1"
    status, listing = await worker.get(f"/v1/jobs?{query}")
    assert status == 200, listing
    assert [item["execution_id"] for item in listing["jobs"]] == [job["execution_id"]]

    async def started() -> dict[str, Any] | None:
        status, body = await worker.get(f"/v1/jobs/{job['execution_id']}")
        assert status == 200, body
        stdout = body["stdout"]
        return body if stdout is not None and stdout["text"] == "started\n" else None

    report = await eventually(started, describe="the job's first output")
    assert report["execution"]["state"] == "running"
    await worker.cancel(job["execution_id"])
    assert (await worker.finish(job["execution_id"]))["state"] == "cancelled"


# Files, through the environment's executor.


async def test_files_are_written_read_and_listed_in_the_session(worker: Harness) -> None:
    content = base64.b64encode(b"hello\n").decode()
    status, written = await worker.post(
        "/v1/files/write",
        {"context": CONTEXT, "path": "notes/a.txt", "content_b64": content, "create_only": True},
    )
    assert status == 200, written
    status, read = await worker.post("/v1/files/read", {"context": CONTEXT, "path": "notes/a.txt"})
    assert status == 200, read
    assert (read["content_b64"], read["size"]) == (content, 6)
    status, listing = await worker.post("/v1/files/list", {"context": CONTEXT, "path": "notes"})
    assert status == 200, listing
    assert [(entry["name"], entry["type"]) for entry in listing["entries"]] == [("a.txt", "file")]
    status, again = await worker.post(
        "/v1/files/write",
        {"context": CONTEXT, "path": "notes/a.txt", "content_b64": content, "create_only": True},
    )
    assert status == 409, again
    seen = await worker.run("op-cat", kind="shell", script="cat notes/a.txt")
    assert seen["result"]["stdout"]["text"] == "hello\n"


# Publication: a snapshot of a session file becomes a content-addressed artifact.


async def test_a_published_file_is_served_as_its_exact_bytes(worker: Harness) -> None:
    data = b"exact\r\n\x00\xff"
    content = base64.b64encode(data).decode()
    await worker.post(
        "/v1/files/write", {"context": CONTEXT, "path": "out.bin", "content_b64": content}
    )
    status, artifact = await worker.post("/v1/artifacts", {"context": CONTEXT, "path": "out.bin"})
    assert status == 201, artifact
    assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
    assert artifact["size"] == len(data)
    status, again = await worker.get(f"/v1/artifacts/{artifact['artifact_id']}")
    assert (status, again) == (200, artifact)
    response = await worker.client.get(
        f"/v1/artifacts/{artifact['artifact_id']}/content", headers=worker.headers
    )
    assert response.status == 200
    assert await response.read() == data
    status, missing = await worker.post("/v1/artifacts", {"context": CONTEXT, "path": "gone"})
    assert status == 404, missing


# Environment connections: opened on first use, released by the environments' idle check.


async def test_an_environment_connects_on_first_use_and_releases_when_idle(
    worker: Harness,
) -> None:
    manager = worker.manager
    assert await manager.activity("env1") is not None  # the worker can say, before any use
    status, capabilities = await worker.get("/v1/capabilities?environment_id=env1")
    assert status == 200, capabilities
    assert "python" in capabilities["languages"]
    await worker.run("op-a1", kind="cell", code="kept = 1")
    activity = await manager.activity("env1")
    assert activity is not None
    assert (activity.running, activity.kernels) == (0, 1), activity
    assert activity.last_used is not None
    assert not await manager.release_if_idle("env1", time.monotonic() - 60)
    busy = await worker.submit("op-a2", kind="shell", script="sleep 60")
    await worker.wait_running(busy["execution_id"])
    assert not await manager.release_if_idle("env1", time.monotonic() + 60)
    await worker.cancel(busy["execution_id"])
    await worker.finish(busy["execution_id"])
    assert await manager.release_if_idle("env1", time.monotonic())
    after = await worker.run("op-a3", kind="cell", code="kept")
    assert after["kernel_state_lost"] is True
    assert after["result"]["error"]["name"] == "NameError"


async def test_an_explicit_release_loses_the_environments_kernels(worker: Harness) -> None:
    await worker.run("op-r1", kind="cell", code="kept = 1")
    await worker.manager.release("env1")
    after = await worker.run("op-r2", kind="cell", code="kept")
    assert after["result"]["state_lost"] is True


# A worker restart: stdio executors stop with the worker, daemons keep running.


async def test_a_restart_loses_or_keeps_running_work_by_transport(worker: Harness) -> None:
    execution = await worker.submit(
        "op-restart", kind="shell", script="echo before; sleep 1; echo after"
    )
    await worker.wait_running(execution["execution_id"])
    await worker.restart()
    ended = await worker.finish(execution["execution_id"], timeout=30)
    if worker.daemon:
        assert ended["state"] == "succeeded"
        assert ended["attempt_id"] == execution["attempt_id"]
        assert ended["result"]["stdout"]["text"] == "before\nafter\n"
    else:
        assert ended["state"] == "lost"
        assert "the worker stopped" in ended["result"]["error"]["message"]
    again = await worker.run("op-after-restart", kind="shell", script="echo back")
    assert again["result"]["stdout"]["text"] == "back\n"


async def test_a_restart_keeps_kernels_only_on_the_daemon(worker: Harness) -> None:
    await worker.run("op-s1", kind="cell", code="kept = 7")
    await worker.restart()
    after = await worker.run("op-s2", kind="cell", code="kept")
    if worker.daemon:
        assert after["result"]["outputs"][0]["data"]["text/plain"] == "7"
        assert after["kernel_state_lost"] is False
    else:
        assert after["result"]["state_lost"] is True


async def test_sessions_run_side_by_side(worker: Harness) -> None:
    # The first call waits for a file only the second creates.
    gate = worker.root / "gate"
    waiting = await worker.submit(
        "op-wait", kind="shell", script=f"until [ -e {gate} ]; do sleep 0.01; done; echo a"
    )
    await worker.wait_running(waiting["execution_id"])
    opened = await worker.run(
        "op-open", context(session_id="s2"), kind="shell", script=f"touch {gate}"
    )
    assert opened["state"] == "succeeded"
    ended = await worker.finish(waiting["execution_id"], timeout=30)
    assert ended["result"]["stdout"]["text"] == "a\n"
