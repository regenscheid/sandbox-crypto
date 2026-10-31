# SPDX-License-Identifier: MIT
"""Job sandboxes (D93) over a real worker restart: ``sandbox-science-worker serve`` as a
process, killed and started again while a job sandbox's command runs.

The worker runs its OpenShell backend against the fake ``openshell`` CLI of
``test_environments``, extended so that ``sandbox exec``, ``sandbox upload`` and the SSH
bridge run here, each sandbox in a directory of its own: the settings name ``BOX`` as
the sandbox's root, and the stand-ins put each sandbox's own directory in its place. The
sandbox daemons are therefore real local processes that outlive the worker, as they
outlive it in a pod (JS-19). Removing a sandbox from the fake gateway and ending its
processes stands for the pod's loss (JS-20). The in-process tests in
``test_job_sandboxes`` cover each phase in detail; this one checks the same rules hold
for a worker that really stops and starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio

from sandbox_science.testing import WorkerApi, eventually
from sandbox_science.testing.processes import end_left_behind, left_behind

from .test_environments import FAKE_OPENSHELL, IMAGE, TARGETS
from .test_job_sandboxes import granting

pytestmark = pytest.mark.slow

TOKEN = "test-secret-job-sandbox-process-0000000000"
PROJECT = "p1"
SESSION = "env-1"
CONTEXT = {"project_id": PROJECT, "session_id": "s1", "environment_id": SESSION}

STAND_INS = r'''
import json, os, shutil, sys

BOX = os.environ["FAKE_BOX"]
BOXES = os.environ["FAKE_BOXES"]
_argv = sys.argv[1:]


def _here(name, value):
    """A path the worker names in a sandbox, in that sandbox's own directory."""
    return value.replace(BOX, os.path.join(BOXES, name))


def _running(name):
    with open(os.environ["FAKE_OPENSHELL_STATE"]) as handle:
        box = json.load(handle)["sandboxes"].get(name)
    if box is None or box.get("phase", "Ready") != "Ready":
        print("Error:   x sandbox not found or not running", file=sys.stderr)
        sys.exit(1)


if _argv[:2] == ["sandbox", "exec"]:
    _name = _argv[_argv.index("-n") + 1]
    _running(_name)
    _command = [_here(_name, part) for part in _argv[_argv.index("--") + 1 :]]
    os.execvp(_command[0], _command)
if _argv[:2] == ["sandbox", "upload"]:
    _name, _source, _target = _argv[-3:]
    _running(_name)
    _target = _here(_name, _target)
    if _target.endswith("/"):
        os.makedirs(_target, exist_ok=True)
    shutil.copy(_source, _target)
    sys.exit(0)
if _argv[:2] == ["sandbox", "ssh-config"]:
    _running(_argv[2])
    print(f"Host {_argv[2]}")
    sys.exit(0)

# The gateway's own state changes one command at a time: a worker runs several at once.
import fcntl

_lock = open(os.environ["FAKE_OPENSHELL_STATE"] + ".lock", "a")
fcntl.flock(_lock, fcntl.LOCK_EX)
'''

SSH = r"""
import json, os, shlex, sys

# Stands in for ssh to a sandbox: runs the bridge command here, in its own directory.
host, remote = sys.argv[-2], sys.argv[-1]
with open(os.environ["FAKE_OPENSHELL_STATE"]) as handle:
    box = json.load(handle)["sandboxes"].get(host)
if box is None or box.get("phase", "Ready") != "Ready":
    sys.exit(f"ssh: {host} is not running")
here = os.path.join(os.environ["FAKE_BOXES"], host)
command = [part.replace(os.environ["FAKE_BOX"], here) for part in shlex.split(remote)]
os.execvp(command[0], command)
"""

PROFILE = """
[openshell.environments.job_profiles.medium]
description = "6 cores, 16 GiB memory, up to 7 days"
node_selector = { "kubernetes.io/arch" = "amd64" }
runtime_class_name = "kata"
# The stand-ins have no image whose prefix the preflight could find.
require_workspace_prefix = false

[openshell.environments.job_profiles.medium.requests]
cpu_millicores = 6000
memory_mib = 16384
ephemeral_storage_mib = 4096

[openshell.environments.job_profiles.medium.limits]
cpu_millicores = 6000
memory_mib = 16384
ephemeral_storage_mib = 4096
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _inline(table: dict[str, str]) -> str:
    """A TOML inline table of strings."""
    return "{ " + ", ".join(f"{key} = {json.dumps(value)}" for key, value in table.items()) + " }"


def _script(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)


class Site:
    """A worker's settings, fake gateway and sandboxes in ``root``, and the worker process."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.boxes = root / "boxes"
        self.boxes.mkdir()
        self.state = root / "openshell-state.json"
        self.state.write_text(
            json.dumps(
                {
                    "calls": [],
                    "sandboxes": {},
                    "providers": {"github-readonly": "github-token"},
                    "profiles": {"github-token": []},
                    "drafts": {},
                }
            )
        )
        _script(root / "openshell", STAND_INS + FAKE_OPENSHELL)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _script(bin_dir / "ssh", SSH)
        (root / "home").mkdir()
        (root / "pool-ceiling.yaml").write_text(
            "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
        )
        (root / "worker.token").write_text(TOKEN + "\n")
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        box = root / "box"  # the path each sandbox's own directory takes the place of
        env = {
            "FAKE_OPENSHELL_STATE": str(self.state),
            "FAKE_BOX": str(box),
            "FAKE_BOXES": str(self.boxes),
            "HOME": str(root / "home"),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        }
        self.settings = root / "worker.toml"
        self.settings.write_text(
            f'listen = "127.0.0.1:{self.port}"\ntoken_file = "worker.token"\n'
            'state_dir = "state"\npool_id = "test"\n\n'
            f'[openshell]\ncli = "{root / "openshell"}"\npython = "{sys.executable}"\n'
            f'project_root = "{box}/project"\nexecutor_dir = "{box}/.sandbox-science"\n'
            "isolation_tier = 3\nsched_shim = false\n"
            f"env = {_inline(env)}\n\n"
            '[openshell.environments]\npolicy_store = "middleware"\n'
            f'targets = "{TARGETS}"\nceiling = "pool-ceiling.yaml"\n'
            f'profiles = {{minimal = "{IMAGE}"}}\n' + PROFILE
        )
        self.process: subprocess.Popen[bytes] | None = None
        self.started = 0

    def command(self, *args: str) -> list[str]:
        return [str(Path(sys.executable).parent / "sandbox-science-worker"), *args]

    def add_policy(self) -> str:
        policy = granting(self.root, job_profiles=["medium"], job_sandboxes=1)
        done = subprocess.run(
            self.command("policy", "add", "--settings", str(self.settings), str(policy)),
            capture_output=True,
            text=True,
            check=True,
        )
        found = re.search(r"as (sha256:[0-9a-f]{64})", done.stdout)
        assert found is not None, done.stdout + done.stderr
        return found[1]

    async def start(self) -> None:
        self.started += 1
        log = (self.root / f"worker-{self.started}.log").open("wb")
        self.process = subprocess.Popen(
            self.command("serve", "--settings", str(self.settings)),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        for _ in range(400):
            with (
                contextlib.suppress(OSError),
                socket.create_connection(("127.0.0.1", self.port), 0.2),
            ):
                return
            assert self.process.poll() is None, self.log()
            await asyncio.sleep(0.05)
        raise AssertionError("the worker did not start:\n" + self.log())

    def kill(self) -> None:
        """The worker dies at once, as in a crash or an OOM kill: no cleanup runs."""
        assert self.process is not None
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(30)
        self.process = None

    async def stop(self) -> None:
        if self.process is not None:
            self.process.send_signal(signal.SIGTERM)
            await asyncio.to_thread(self.process.wait, 60)
            self.process = None

    def log(self) -> str:
        return "".join(
            path.read_text(errors="replace")[-4000:]
            for path in sorted(self.root.glob("worker-*.log"))
        )

    def gateway(self) -> dict[str, Any]:
        return json.loads(self.state.read_text())

    def lose(self, sandbox: str) -> None:
        """The sandbox's pod goes while the worker is down: gone from the gateway, and
        its processes with it."""
        state = self.gateway()
        del state["sandboxes"][sandbox]
        self.state.write_text(json.dumps(state))
        end_left_behind(self.boxes / sandbox, 0.0)


@pytest_asyncio.fixture
async def site(tmp_path: Path) -> AsyncIterator[Site]:
    site = Site(tmp_path)
    try:
        yield site
    finally:
        await site.stop()
        end_left_behind(site.boxes, 5.0)


@pytest_asyncio.fixture
async def api(site: Site) -> AsyncIterator[WorkerApi]:
    async with aiohttp.ClientSession() as http:
        yield WorkerApi(http, site.url, TOKEN, CONTEXT)


async def _session(site: Site, api: WorkerApi) -> None:
    policy = site.add_policy()
    await site.start()
    status, body = await api.post(
        "/v1/environments",
        {"environment_id": SESSION, "project_id": PROJECT, "profile": "minimal", "policy": policy},
    )
    assert status in (200, 201), body


def _job(root: Path, name: str) -> dict[str, Any]:
    """A job that records each start, waits for its gate, then writes a result."""
    runs, gate = root / f"{name}.runs", root / f"{name}.go"
    script = (
        f"echo started >> {runs}\n"
        f"while [ ! -e {gate} ]; do sleep 0.1; done\n"
        "mkdir -p out\necho 42 > out/result.txt\necho finished\n"
    )
    return {
        "kind": "job",
        "script": script,
        "purpose": "a long search",
        "expected_progress": "nothing until it ends",
        "profile": "medium",
    }


async def _running(site: Site, api: WorkerApi, execution_id: str, name: str) -> dict[str, Any]:
    """Until the job's command runs in its sandbox; returns the job's status."""
    runs = site.root / f"{name}.runs"
    await eventually(runs.exists, timeout=60, describe="the job's command started")
    status = await _status(api, execution_id)
    assert status["execution"]["state"] == "running"
    assert status["execution"]["job"]["sandbox"]["phase"] == "running"
    return status


async def _status(api: WorkerApi, execution_id: str) -> dict[str, Any]:
    code, status = await api.get(f"/v1/jobs/{execution_id}?project_id={PROJECT}")
    assert code == 200, status
    return status


def _created(site: Site) -> list[str]:
    """The sandboxes the worker created, in order: the session's first."""
    return [
        call[call.index("--name") + 1]
        for call in site.gateway()["calls"]
        if call[:2] == ["sandbox", "create"]
    ]


async def _ended(api: WorkerApi, execution_id: str) -> dict[str, Any]:
    async def ended() -> dict[str, Any] | None:
        execution = await api.execution(execution_id)
        return (
            execution
            if execution["state"] in ("succeeded", "failed", "cancelled", "lost")
            else None
        )

    return await eventually(ended, timeout=120, interval=0.1, describe=f"{execution_id} ended")


async def _delete_session(site: Site, api: WorkerApi) -> None:
    """Delete the session environment; its daemon ends, and nothing of the job's is left."""
    async with api.http.delete(
        f"{site.url}/v1/environments/{SESSION}", headers=api.headers
    ) as response:
        assert response.status == 200, await response.text()
    await eventually(
        lambda: not left_behind(site.boxes), timeout=30, describe="no sandbox process left"
    )
    assert site.gateway()["sandboxes"] == {}


async def test_a_job_sandbox_survives_a_worker_kill_and_is_collected_once(
    site: Site, api: WorkerApi
) -> None:
    await _session(site, api)
    job = await api.submit("big-1", **_job(site.root, "big"))
    execution_id = job["execution_id"]
    before = await _running(site, api, execution_id, "big")
    environment = before["execution"]["job"]["sandbox"]["environment_id"]
    site.kill()  # mid-job: the job sandbox's daemon goes on running the command
    await site.start()
    # Taken up by its phase: still running, not lost, and the command not run again.
    after = await _status(api, execution_id)
    assert after["execution"]["state"] == "running", after
    assert after["execution"]["job"]["sandbox"]["phase"] == "running"
    (site.root / "big.go").touch()
    ended = await _ended(api, execution_id)
    assert ended["state"] == "succeeded", ended
    assert (site.root / "big.runs").read_text() == "started\n"
    status = await _status(api, execution_id)
    manifest = status["manifest"]
    assert manifest["collection"] == "complete", manifest
    collected = [file["path"] for file in manifest["collected"]]
    assert len(collected) == len(set(collected))
    result = f".jobs/{execution_id}/files/out/result.txt"
    assert result in collected
    assert sorted(ended["result"]["written_files"]) == sorted(collected)
    code, read = await api.post("/v1/files/read", {"context": CONTEXT, "path": result})
    assert code == 200, read
    assert read["size"] == 3
    # Released at once after a complete collection: the job sandbox is gone.
    assert status["execution"]["job"]["sandbox"]["phase"] == "released"
    assert manifest["environment_id"] == environment
    await _delete_session(site, api)


async def test_a_job_sandbox_lost_while_the_worker_was_down_is_lost_and_not_rerun(
    site: Site, api: WorkerApi
) -> None:
    await _session(site, api)
    job = await api.submit("big-2", **_job(site.root, "lost"))
    execution_id = job["execution_id"]
    await _running(site, api, execution_id, "lost")
    site.kill()
    session, job_sandbox = _created(site)
    assert set(site.gateway()["sandboxes"]) == {session, job_sandbox}
    site.lose(job_sandbox)
    await site.start()
    ended = await _ended(api, execution_id)
    assert ended["state"] == "lost", ended
    assert ended["result"]["error"]["message"].startswith("sandbox_lost"), ended
    (site.root / "lost.go").touch()
    await asyncio.sleep(1.0)  # time enough for a rerun to show, if there were one
    assert (site.root / "lost.runs").read_text() == "started\n"
    status = await _status(api, execution_id)
    assert status["manifest"]["outcome"] == "lost"
    assert status["manifest"]["reason"] == "sandbox_lost"
    assert status["execution"]["job"]["sandbox"]["phase"] == "released"
    assert len(_created(site)) == 2  # no sandbox was made for it again
    await _delete_session(site, api)


async def test_deleting_the_session_environment_releases_a_running_job_sandbox(
    site: Site, api: WorkerApi
) -> None:
    await _session(site, api)
    job = await api.submit("big-3", **_job(site.root, "deleted"))
    execution_id = job["execution_id"]
    await _running(site, api, execution_id, "deleted")
    site.kill()
    await site.start()  # the deletion comes to a worker that took the job up again
    await _delete_session(site, api)
    ended = await _ended(api, execution_id)
    assert ended["state"] == "cancelled", ended
    assert any("session environment was deleted" in n for n in ended["result"]["notices"])
    status = await _status(api, execution_id)
    assert status["execution"]["job"]["sandbox"]["phase"] == "released"
    assert status["manifest"]["collection"] == "incomplete"
    assert (site.root / "deleted.runs").read_text() == "started\n"
