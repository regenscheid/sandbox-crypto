# SPDX-License-Identifier: MIT
"""A delegated run's processes are held to its write area (delegated agents spec, DA-20,
fixtures DA-F9 and DA-F10), through the worker API on the local backend with real
commands and kernels: a shell command, a job and a Python cell whose context names
``work/<delegation_id>`` write in that area and the run's scratch directory and nowhere
else, cannot reach another run's kernel or signal its processes, and are refused, never
run unconfined, on an executor that cannot hold them."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import ExecutionContext, KernelRestartRequest
from sandbox_science.worker import sandbox_confine
from sandbox_science.worker.errors import WorkerError

from .harness import Harness

_, UNAVAILABLE = sandbox_confine.abi()
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(UNAVAILABLE is not None, reason=f"no confinement here: {UNAVAILABLE}"),
]

AREA = "work/dlg-a"
SIBLING = "work/dlg-b"

ATTEMPTS = {
    "parent": "sources/a.md",
    "new_parent_file": "sources/new.md",
    "input": "inputs/data.csv",
    "sibling": f"{SIBLING}/notes.md",
    "shared": "../../shared/x.txt",
    "executor": "../../.executor/ipython/injected.py",
}
"""Paths a child's code tries to write, relative to the session directory."""


def _in(area: str | None = None) -> dict[str, str]:
    context = {"project_id": "p1", "session_id": "s1", "environment_id": "env1"}
    return context if area is None else {**context, "write_area": area}


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path) as started:
        yield started


@pytest_asyncio.fixture
async def daemon_worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, daemon=True) as started:
        yield started


async def _seed(worker: Harness) -> None:
    """The parent's files, its input copy and a sibling's area, written by the platform."""
    for path in ("sources/a.md", "inputs/data.csv", f"{SIBLING}/notes.md"):
        status, body = await worker.post(
            "/v1/files/write",
            {
                "context": _in(),
                "path": path,
                "content_b64": base64.b64encode(b"original\n").decode(),
            },
        )
        assert status == 200, body


def _unchanged(worker: Harness) -> None:
    session = worker.workspace
    for path in ("sources/a.md", "inputs/data.csv", f"{SIBLING}/notes.md"):
        assert (session / path).read_text() == "original\n", path
    assert not (session / "sources/new.md").exists()
    assert not (session.parent.parent / "shared/x.txt").exists()
    assert not (session.parent.parent / ".executor/ipython/injected.py").exists()


SHELL = """
set -u
pwd
echo "work=$RUN_WORK tmp=$TMPDIR"
echo mine > notes.md && echo wrote-area
echo scratch > "$TMPDIR/t" && echo wrote-tmp
for path in {paths}; do
  if (echo child > "$SESSION_WORKSPACE/$path") 2>/dev/null; then echo "WROTE $path"; fi
done
rm -f "$SESSION_WORKSPACE/sources/a.md" 2>/dev/null && echo REMOVED
mv notes.md "$SESSION_WORKSPACE/sources/moved.md" 2>/dev/null && echo MOVED
ln -s "$SESSION_WORKSPACE/sources" out && (echo child > out/a.md) 2>/dev/null && echo LINKED
echo done
"""


async def test_a_childs_shell_writes_its_area_and_nothing_else(worker: Harness) -> None:
    """DA-F9: the shell starts in the area, writes it and $TMPDIR; the parent's file, a new
    file beside it, the input copy, a sibling's area, shared/ and the executor's files are
    refused, as are removing, moving out and writing through a link."""
    await _seed(worker)
    script = SHELL.format(paths=" ".join(ATTEMPTS.values()))
    execution = await worker.run("child-shell", _in(AREA), kind="shell", script=script)
    assert execution["state"] == "succeeded", execution
    lines = execution["result"]["stdout"]["text"].splitlines()
    area = worker.workspace / AREA
    assert lines[0] == str(area)
    assert lines[1].startswith(f"work={area} tmp=")
    assert lines[2:] == ["wrote-area", "wrote-tmp", "done"], lines
    assert (area / "notes.md").read_text() == "mine\n"
    _unchanged(worker)


async def test_the_sandbox_daemon_holds_a_child_as_the_executor_does(
    daemon_worker: Harness,
) -> None:
    """The daemon passes the request and the executor's ``confinement`` on unchanged."""
    worker = daemon_worker
    await _seed(worker)
    script = 'echo mine > notes.md; (echo child > "$SESSION_WORKSPACE/inputs/data.csv") || true'
    execution = await worker.run("daemon-shell", _in(AREA), kind="shell", script=script)
    assert execution["state"] == "succeeded", execution
    assert (worker.workspace / AREA / "notes.md").read_text() == "mine\n"
    _unchanged(worker)


async def test_the_parent_and_the_platform_are_not_held(worker: Harness) -> None:
    """A request without a write area (a main run's) writes anywhere, as before."""
    await _seed(worker)
    execution = await worker.run(
        "parent-shell", _in(), kind="shell", script="echo parent > sources/a.md && pwd"
    )
    assert execution["state"] == "succeeded", execution
    assert execution["result"]["stdout"]["text"] == f"{worker.workspace}\n"
    assert (worker.workspace / "sources/a.md").read_text() == "parent\n"


async def test_a_childs_job_is_held_too(worker: Harness) -> None:
    await _seed(worker)
    job = await worker.run(
        "child-job",
        _in(AREA),
        kind="job",
        purpose="a held job",
        expected_progress="none",
        script='echo kept > out.txt; echo child > "$SESSION_WORKSPACE/inputs/data.csv"',
    )
    assert job["state"] == "failed", job
    assert (worker.workspace / AREA / "out.txt").read_text() == "kept\n"
    _unchanged(worker)


CELL = """
import json, os, socket
from pathlib import Path
session = Path(os.environ["SESSION_WORKSPACE"])
import ipykernel.connect
own = ipykernel.connect.get_connection_info(unpack=True)["ip"]
found = {"cwd": os.getcwd(), "wrote": [], "tried": 0, "connected": [], "signalled": False}
Path("cell.txt").write_text("mine")
for path in PATHS:
    try:
        (session / path).write_text("child")
        found["wrote"].append(path)
    except OSError:
        pass
for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
    name = line.split()[-1] if len(line.split()) > 7 else ""
    if name.startswith("@ss-kernel-") and not name.startswith(own):
        found["tried"] += 1
        with socket.socket(socket.AF_UNIX) as client:
            try:
                client.connect("\\0" + name[1:])
                found["connected"].append(name)
            except OSError:
                pass
try:
    os.kill(PARENT, 0)
    found["signalled"] = True
except OSError:
    pass
print(json.dumps(found))
"""


async def test_a_childs_kernel_is_held_and_cannot_reach_the_parents(worker: Harness) -> None:
    """DA-F9 for a cell, and DA-F10: the child's own kernel starts in its area, writes
    nothing outside, cannot connect to the parent's kernel's sockets and cannot signal it;
    a restart keeps it held."""
    await _seed(worker)
    parent = await worker.run("parent-cell", _in(), kind="cell", code="import os; os.getpid()")
    assert parent["state"] == "succeeded", parent
    pid = int(parent["result"]["outputs"][0]["data"]["text/plain"])
    code = f"PATHS = {json.dumps(list(ATTEMPTS.values()))}\nPARENT = {pid}\n" + CELL
    for attempt in ("first", "restarted"):
        child = await worker.run(
            f"child-cell-{attempt}", _in(AREA), kind="cell", code=code, run="r-child"
        )
        assert child["state"] == "succeeded", child
        found = json.loads(child["result"]["stdout"]["text"])
        assert found.pop("tried") >= 5  # the parent's kernel's channels
        assert found == {
            "cwd": str(worker.workspace / AREA),
            "wrote": [],
            "connected": [],
            "signalled": False,
        }
        assert (worker.workspace / AREA / "cell.txt").read_text() == "mine"
        if attempt == "first":
            await worker.manager.restart_kernel(
                KernelRestartRequest(
                    context=ExecutionContext.model_validate(_in(AREA)), run="r-child"
                )
            )
    _unchanged(worker)
    again = await worker.run("parent-again", _in(), kind="cell", code="1 + 1")
    assert again["result"]["outputs"][0]["data"]["text/plain"] == "2"  # untouched


async def test_a_childs_cell_needs_its_own_kernel_with_its_own_area(worker: Harness) -> None:
    """A cell naming a write area but no run would use the session's kernel, which is not
    held: refused. A run's kernel keeps the area it started with."""
    no_run = await worker.run("no-run", _in(AREA), kind="cell", code="1")
    assert no_run["state"] == "failed"
    assert no_run["result"]["error"]["kind"] == "invalid_request"
    first = await worker.run("first", _in(AREA), kind="cell", code="1", run="r1")
    assert first["state"] == "succeeded", first
    other = await worker.run("other", _in(SIBLING), kind="cell", code="1", run="r1")
    assert other["result"]["error"]["kind"] == "conflict"
    assert "kernel_confinement" in other["result"]["error"]["message"]
    bare = await worker.run("bare", _in(), kind="cell", code="1", run="r1")
    assert bare["result"]["error"]["kind"] == "conflict"


async def test_an_executor_that_cannot_hold_a_run_runs_none_of_its_code(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-F10's refusal: an executor that says why it cannot, or says nothing (one from
    before), gets no delegated run's shell, job, cell or restart; the parent's still run."""
    await worker.run("connect", _in(), kind="shell", script="true")
    channel = worker.view.channel()
    report: dict[str, Any] = channel.hello["confinement"]
    assert report["protocol"] == 1
    assert report["landlock_abi"] >= sandbox_confine.REQUIRED_ABI
    for hello in ({**report, "unavailable": "Landlock ABI 5"}, None):
        if hello is None:
            monkeypatch.delitem(channel.hello, "confinement")
        else:
            monkeypatch.setitem(channel.hello, "confinement", hello)
        marker = worker.workspace / AREA / "ran"
        for kind, extra in (
            ("shell", {"script": f"touch {marker}"}),
            ("job", {"script": f"touch {marker}", "purpose": "p", "expected_progress": "e"}),
            ("cell", {"code": f"open({str(marker)!r}, 'w')", "run": "r1"}),
        ):
            refused = await worker.run(
                f"refused-{kind}-{hello is None}", _in(AREA), kind=kind, **extra
            )
            assert refused["state"] == "failed", refused
            assert refused["result"]["error"]["kind"] == "unsupported", refused
        assert not marker.exists()
        with pytest.raises(WorkerError) as restart:
            await worker.manager.restart_kernel(
                KernelRestartRequest(context=ExecutionContext.model_validate(_in(AREA)), run="r1")
            )
        assert restart.value.kind == "unsupported"
    parent = await worker.run("parent", _in(), kind="shell", script="echo ok")
    assert parent["result"]["stdout"]["text"] == "ok\n"


def test_the_wrapper_fails_closed(tmp_path: Path) -> None:
    """A ruleset that cannot be made in full runs nothing: a missing directory, or a
    symbolic link in place of one."""
    marker = tmp_path / "ran"
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    script = sandbox_confine.__file__
    for writable in (tmp_path / "missing", tmp_path / "link"):
        argv = [
            sys.executable,
            "-I",
            "-S",
            script,
            "--rw",
            str(writable),
            "--",
            "touch",
            str(marker),
        ]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        assert done.returncode == sandbox_confine.FAILED
        assert "cannot hold this run to its write area" in done.stderr
    assert not marker.exists()
    usage = subprocess.run([sys.executable, script, "--"], capture_output=True, check=False)
    assert usage.returncode == sandbox_confine.FAILED
