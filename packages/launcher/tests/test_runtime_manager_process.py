# SPDX-License-Identifier: MIT
"""KRB-26: the real manager, the real TLS control with both listeners, and the fake
OpenShell CLI whose exec runs the real runtime (whose model is never called before
its handshake). The operator's requests go through the real command."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.launcher.runtime_manager.control import ControlClient, ControlError, Health
from sandbox_science.testing import eventually

from .conftest import openshell_stand_in
from .manager_support import (
    MANAGER_TOKEN,
    REVISION,
    LocalTls,
    alive,
    live_runtimes,
    localhost_tls,
    private,
    settings_document,
)

pytestmark = pytest.mark.slow

WORKER = "test-secret-worker-capability-0123456789abcdef"


async def worker_ready(request: web.Request) -> web.Response:
    if request.headers.get("Authorization") != "Bearer " + WORKER:
        raise web.HTTPNotFound()
    return web.json_response(
        {"protocol": PROTOCOL_VERSION, "ready": True},
        headers={PROTOCOL_HEADER: str(PROTOCOL_VERSION)},
    )


class Processes:
    """Control and manager processes, killed at the end whatever happened."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.started: list[subprocess.Popen[bytes]] = []

    def start(self, name: str, *argv: str) -> subprocess.Popen[bytes]:
        log = (self.tmp_path / f"{name}.log").open("ab")
        process = subprocess.Popen(
            [sys.executable, *argv], stdout=log, stderr=subprocess.STDOUT, cwd=self.tmp_path
        )
        log.close()
        self.started.append(process)
        return process

    def log(self, name: str) -> str:
        return (self.tmp_path / f"{name}.log").read_text()

    async def stop(self, process: subprocess.Popen[bytes]) -> int:
        process.send_signal(signal.SIGTERM)
        return await asyncio.to_thread(process.wait, 30)

    def close(self) -> None:
        for process in self.started:
            if process.poll() is None:
                process.kill()
                process.wait()
        for pid in live_runtimes(self.tmp_path):
            os.kill(pid, signal.SIGKILL)


@pytest.fixture
async def worker() -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_get("/v1/ready", worker_ready)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def control_settings(tmp_path: Path, tls: LocalTls, worker: str, ports: tuple[int, int]) -> Path:
    import hashlib

    digest = hashlib.sha256(MANAGER_TOKEN.encode()).hexdigest()
    memory = tmp_path / "control-memory"
    memory.mkdir(mode=0o700)
    settings = {
        "state_dir": str(tmp_path / "control-state"),
        "listen": f"127.0.0.1:{ports[0]}",
        "manager_listen": f"127.0.0.1:{ports[1]}",
        "tls": {"certificate": str(tls.certificate), "private_key": str(tls.key)},
        "worker": worker,
        "worker_token_file": str(private(tmp_path / "control-secret" / "worker.token", WORKER)),
        "runtime_token_file": str(memory / "runtime.token"),
        "generation": "runtime-placeholder",
        "generation_file": str(tmp_path / "control-state" / "generation"),
        "manager_token_sha256_file": str(
            private(tmp_path / "control-secret" / "manager.sha256", digest)
        ),
        "projects": ["p"],
        "main": "main",
        "reviewer": "review",
        "image_revision": REVISION,
    }
    return private(tmp_path / "control-settings.json", json.dumps(settings))


async def test_manager_bootstraps_reconnects_revokes_orphans_and_replaces(
    tmp_path: Path, worker: str, unused_tcp_port_factory: Callable[[], int]
) -> None:
    tls = localhost_tls(tmp_path / "tls")
    ports = (unused_tcp_port_factory(), unused_tcp_port_factory())
    control_file = control_settings(tmp_path, tls, worker, ports)
    state = tmp_path / "openshell.json"
    (tmp_path / "image.json").write_text(
        json.dumps({"profile": "orchestrator", "revision": REVISION})
    )
    cli = openshell_stand_in(
        tmp_path / "bin" / "openshell", FAKE_OPENSHELL=state, FAKE_JOURNAL=tmp_path / "journal"
    )
    document: dict[str, Any] = settings_document(
        tmp_path, manager_url=f"https://localhost:{ports[1]}", ca=tls.certificate, cli=cli
    )
    document |= {
        "control_url": f"https://localhost:{ports[0]}",
        "control_models_file": str(control_file),  # the whole control.json serves as the copy
        "start_timeout": 60,
        "stop_timeout": 15,
    }
    manager_file = private(tmp_path / "runtime-manager.json", json.dumps(document))
    root = Path(document["state_dir"])
    client = ControlClient(
        document["manager_url"],
        ca_file=tls.certificate,
        token_file=Path(document["manager_token_file"]),
    )
    processes = Processes(tmp_path)

    def fake(**changes: object) -> None:
        found = json.loads(state.read_text()) if state.exists() else {"sandboxes": {}}
        state.write_text(json.dumps(found | {"runtime": sys.executable} | changes))

    async def health() -> Health | None:
        try:
            return await asyncio.to_thread(client.health)
        except ControlError:
            return None

    async def runtime_ready() -> Health | None:
        found = await health()
        return found if found is not None and found.runtime_ready else None

    def issues() -> list[dict[str, Any]]:
        journal = tmp_path / "control-state" / "runtime-credentials.json"
        return json.loads(journal.read_text())["issues"]

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sandbox_science.launcher.runtime_manager", *argv],
            env=os.environ | {"SANDBOX_SCIENCE_RUNTIME_MANAGER_SETTINGS": str(manager_file)},
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    async def command(*argv: str) -> subprocess.CompletedProcess[str]:
        # In a thread: the fake worker answering control runs on this event loop.
        return await asyncio.to_thread(run, *argv)

    control_argv = ("-m", "sandbox_science.orchestrator.confinement.server")
    manager_argv = ("-m", "sandbox_science.launcher.runtime_manager")
    try:
        control = processes.start("control", *control_argv, "--settings", str(control_file))
        await eventually(health, timeout=30)
        fake(behaviour="exec-orphans")
        manager = processes.start(
            "manager", *manager_argv, "--settings", str(manager_file), "serve"
        )
        await eventually(lambda: (root / "status.json").exists(), timeout=30)
        done = await command("request", "bootstrap")
        assert done.returncode == 0, done.stderr + processes.log("manager")
        first = await eventually(runtime_ready, timeout=60)
        record = json.loads((root / "sandbox.json").read_text())
        assert [issue["state"] for issue in issues()] == ["issued"]

        # A manager restart with an orphaned runtime: SIGTERM keeps the sandbox, the
        # runtime survives the CLI, and the next manager revokes it before attaching.
        assert await processes.stop(manager) == 0
        (orphan,) = live_runtimes(tmp_path)
        assert (await eventually(health)).runtime_connected
        fake(behaviour="")
        manager = processes.start(
            "manager", *manager_argv, "--settings", str(manager_file), "serve"
        )
        await eventually(lambda: not alive(orphan), timeout=60)
        await eventually(
            lambda: len(issues()) == 2 and issues()[-1]["state"] == "issued", timeout=60
        )
        await eventually(runtime_ready, timeout=60)
        assert json.loads((root / "sandbox.json").read_text()) == record

        # A control restart: the runtime ends, and the manager reconnects to the new control.
        assert await processes.stop(control) == 0
        control = processes.start("control", *control_argv, "--settings", str(control_file))
        restarted = await eventually(runtime_ready, timeout=90)
        assert restarted.control_instance != first.control_instance
        assert [issue["state"] for issue in issues()] == ["superseded"] * 2 + ["issued"]
        assert json.loads((root / "sandbox.json").read_text()) == record

        # An explicit replace: a new sandbox; control's history is kept.
        done = await command("request", "replace")
        assert done.returncode == 0, done.stderr + processes.log("manager")
        await eventually(runtime_ready, timeout=60)
        replaced = json.loads((root / "sandbox.json").read_text())
        assert replaced["name"] != record["name"]
        assert record["name"] not in json.loads(state.read_text())["sandboxes"]
        assert [issue["state"] for issue in issues()] == ["superseded"] * 3 + ["issued"]

        shown = await command("request", "status")
        assert shown.returncode == 0, shown.stderr
        assert json.loads(shown.stdout)["sandbox"]["name"] == replaced["name"]
        refused = await command("request", "stop")
        assert refused.returncode != 0
        assert "--delete" in refused.stderr

        # SIGTERM never deletes; teardown is an explicit stop --delete.
        assert await processes.stop(manager) == 0
        assert json.loads((root / "sandbox.json").read_text()) == replaced
        manager = processes.start(
            "manager", *manager_argv, "--settings", str(manager_file), "serve"
        )
        await eventually(runtime_ready, timeout=60)
        done = await command("request", "stop", "--delete")
        assert done.returncode == 0, done.stderr + processes.log("manager")
        assert not (root / "sandbox.json").exists()
        assert json.loads(state.read_text())["sandboxes"] == {}
        await eventually(lambda: not live_runtimes(tmp_path), timeout=30)
        assert await processes.stop(manager) == 0
        assert await processes.stop(control) == 0
    finally:
        processes.close()
