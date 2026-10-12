# SPDX-License-Identifier: MIT
# ruff: noqa: S101, S607
"""Root-owned disposable Lab27 GUI/passive-kernel composition acceptance.

Start the reviewed lab with PROJECT=lab27. Init seeds only the marked lab's
trusted registry. Serve uses the frozen production GUI and real worker; login
credentials remain in an exclusive private file. Other stages record bounded
public API evidence. Root controls browser actions, worker restarts and settings.
No model, converter or research code runs on the host.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.workspace.app import create_app

SOURCE = Path(__file__).resolve().parents[2]
LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab27"
STATE = LAB / "gui-state"
MARKER = LAB / "kernel-gui.created"
EVIDENCE = LAB / "evidence/kernel-gui.json"
PRIVATE = LAB / "gui-private.json"
PUBLIC = LAB / "evidence/kernel-gui-server.json"
WORKER = "http://127.0.0.1:8100"
SESSIONS = {"a", "b", "c", "empty", "closed"}
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}
STAMP = str(time.time_ns())


def bounded(path: Path, limit: int = 65536) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        assert stat.S_ISREG(info.st_mode)
        assert info.st_uid == os.getuid()
        assert info.st_size <= limit
        raw = os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)
    assert len(raw) <= limit
    return raw.decode()


def guard(head: str, *, create: bool = False) -> dict[str, Any]:
    assert re.fullmatch(r"[0-9a-f]{40}", head)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip() == head
    )
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=SOURCE, text=True)
    assert LAB.is_dir()
    assert not LAB.is_symlink()
    assert LAB.stat().st_uid == os.getuid()
    settings = tomllib.loads(bounded(LAB / "worker/worker.toml"))
    assert settings["listen"] == "127.0.0.1:8100"
    assert any(c["projects"] == [PROJECT] for c in settings["clients"])
    assert settings["openshell"].get("daemon", True) is True
    assert settings["openshell"]["environments"]["profiles"]["minimal"].startswith(
        "sandbox-science/minimal:"
    )
    expected = {
        "version": 1,
        "project": PROJECT,
        "purpose": "disposable GUI kernel observation acceptance",
        "source_head": head,
    }
    if create:
        with MARKER.open("x") as stream:
            json.dump(expected, stream)
    assert json.loads(bounded(MARKER, 2048)) == expected
    return settings


def save(name: str, value: Any) -> None:
    values = json.loads(bounded(EVIDENCE, 1024 * 1024)) if EVIDENCE.exists() else {}
    assert name not in values, "Stages are exclusive; use fresh lab state"
    values[name] = value
    EVIDENCE.parent.mkdir(exist_ok=True)
    partial = EVIDENCE.with_suffix(".partial")
    with partial.open("x") as stream:
        stream.write(json.dumps(values, indent=2) + "\n")
    partial.replace(EVIDENCE)
    print("PASS " + name, flush=True)


def seed(head: str) -> None:
    guard(head, create=True)
    assert not STATE.exists()
    store = ProjectStore(STATE)
    policy = bounded(LAB / "policy.digest", 128).strip()
    store.create(PROJECT, "minimal", policy)
    for session in sorted(SESSIONS):
        store.open_session(PROJECT, session, dedicated=session == "c")
    store.close_session(PROJECT, "closed")
    store.create(PROJECT + "b", "minimal", policy)
    store.open_session(PROJECT + "b", "a", dedicated=False)
    save("registry", store.get(PROJECT).model_dump(mode="json"))


async def serve(head: str) -> None:
    guard(head)
    static = SOURCE / "web/workspace/dist"
    assert (static / "index.html").is_file()
    assert not PRIVATE.exists()
    assert not PUBLIC.exists()
    build = {}
    total = 0
    for path in sorted(static.rglob("*")):
        if path.is_file():
            assert not path.is_symlink()
            assert len(build) < 256
            size = path.stat().st_size
            assert size <= 16 * 1024 * 1024
            total += size
            assert total <= 64 * 1024 * 1024
            with path.open("rb") as stream:
                raw = stream.read(size + 1)
            assert len(raw) == size
            build[str(path.relative_to(static))] = {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
    token = secrets.token_urlsafe(32)
    worker = WorkerClient(WORKER, bounded(LAB / "worker/client-a.token", 4096).strip())
    runner = web.AppRunner(
        create_app(ProjectStore(STATE), static, login_token=token, job_worker=worker),
        access_log=None,
    )
    stopped = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stopped.set)
    port = None
    owns_private = False
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = int(runner.addresses[0][1])
        public = {
            "source_head": head,
            "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "port": port,
            "pid": os.getpid(),
            "build": build,
        }
        fd = os.open(PRIVATE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        owns_private = True
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {"source_head": head, "login_url": f"http://127.0.0.1:{port}/login?token={token}"},
                stream,
            )
        with PUBLIC.open("x") as stream:
            json.dump(public, stream, indent=2)
        print(
            json.dumps({k: public[k] for k in ("source_head", "helper_sha256", "port", "pid")}),
            flush=True,
        )
        await stopped.wait()
    finally:
        try:
            await runner.cleanup()
        finally:
            if owns_private:
                PRIVATE.unlink(missing_ok=True)
            save(
                "workspace-cleanup",
                {
                    "utc": datetime.now(UTC).isoformat(),
                    "private_removed": not PRIVATE.exists(),
                    "port": port,
                },
            )


class Checks:
    def __init__(
        self, head: str, worker: aiohttp.ClientSession, gui: aiohttp.ClientSession, base: str
    ) -> None:
        self.head, self.worker, self.gui, self.base = head, worker, gui, base
        self.store = ProjectStore(STATE)
        self.values = json.loads(bounded(EVIDENCE, 1024 * 1024))

    def context(self, session: str) -> dict[str, str]:
        assert session in SESSIONS
        found = self.store.get(PROJECT).sessions[session]
        return {
            "project_id": PROJECT,
            "session_id": session,
            "environment_id": found.environment_id,
        }

    async def call(
        self,
        http: aiohttp.ClientSession,
        method: str,
        url: str,
        body: Any = None,
        *,
        status: int | None = None,
    ) -> tuple[int, Any]:
        guard(self.head)
        async with http.request(method, url, json=body, allow_redirects=False) as response:
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                assert len(raw) + len(chunk) <= 1024 * 1024
                raw.extend(chunk)
            value = json.loads(raw) if raw else None
            if http is self.gui:
                assert response.headers.get("Cache-Control") == "no-store"
            if status is not None:
                assert response.status == status, (urlsplit(url).path, response.status, value)
            return response.status, value

    async def api(self, method: str, path: str, body: Any = None, *, status: int = 200) -> Any:
        assert path.startswith("/v1/")
        assert "?" not in path
        assert "#" not in path
        return (await self.call(self.worker, method, WORKER + path, body, status=status))[1]

    async def observe(self, session: str, *, status: int | None = 200) -> tuple[int, Any]:
        assert session in SESSIONS
        found = await self.call(
            self.gui,
            "GET",
            self.base + f"/api/v0/projects/{PROJECT}/sessions/{session}/kernels",
            status=status,
        )
        if found[0] == 200:
            value = found[1]
            assert all(value[key] == expected for key, expected in self.context(session).items())
        return found

    async def cell(self, name: str, session: str, code: str) -> Any:
        value = await self.api(
            "POST",
            "/v1/executions",
            {
                "kind": "cell",
                "context": self.context(session),
                "language": "python",
                "operation_id": f"lab27-{name}-{STAMP}",
                "timeout_seconds": 120,
                "code": code,
            },
            status=201,
        )
        async with asyncio.timeout(150):
            while value["state"] not in TERMINAL:
                await asyncio.sleep(0.1)
                value = await self.api("GET", f"/v1/executions/{value['execution_id']}")
        assert value["state"] == "succeeded", value
        save(name, value)
        return value

    @staticmethod
    def kernel(observation: tuple[int, Any]) -> Any:
        assert observation[0] == 200
        assert len(observation[1]["kernels"]) == 1
        return observation[1]["kernels"][0]

    async def initial(self) -> None:
        assert "initial-complete" not in self.values
        for session in ("a", "c"):
            context = self.context(session)
            await self.api(
                "POST",
                "/v1/environments",
                {
                    "project_id": PROJECT,
                    "environment_id": context["environment_id"],
                    "profile": "minimal",
                    "policy": self.values["registry"]["policy"],
                },
                status=201,
            )
        assert (await self.observe("a", status=503))[0] == 503
        code = "import json,sys,pathlib,importlib.util\nlab27_value=VALUE\nprint(json.dumps({'value':lab27_value,'python':sys.version,'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'networkx_present':importlib.util.find_spec('networkx') is not None}))"
        for session, number in (("a", 1729), ("c", 314159)):
            value = await self.cell(
                "initial-" + session, session, code.replace("VALUE", str(number))
            )
            assert json.loads(value["result"]["stdout"]["text"])["networkx_present"] is False
            assert value["location"]["backend"] == "openshell"
            assert value["location"]["isolation_tier"] == 3
            seen = await self.observe(session)
            assert self.kernel(seen)["revision_status"] == "current"
            save("initial-observation-" + session, seen)
        assert self.context("a")["environment_id"] == self.context("b")["environment_id"]
        assert self.context("a")["environment_id"] != self.context("c")["environment_id"]
        assert (await self.observe("empty"))[1]["kernels"] == []
        await self.observe("closed", status=404)
        await self.call(
            self.gui, "GET", self.base + "/api/v0/projects/missing/sessions/a/kernels", status=404
        )
        await self.call(
            self.gui,
            "GET",
            self.base + f"/api/v0/projects/{PROJECT}b/sessions/a/kernels",
            status=503,
        )
        save("initial-complete", True)

    async def install(self) -> None:
        assert self.values.get("initial-complete") is True
        assert "install-complete" not in self.values
        operation = await self.api(
            "POST",
            "/v1/provisioning",
            {
                "operation_id": "lab27-install-" + STAMP,
                "context": self.context("b"),
                "purpose": "Disposable GUI stale-kernel acceptance",
                "conda": ["networkx"],
            },
            status=201,
        )
        async with asyncio.timeout(900):
            while operation["state"] != "finished":
                assert operation["state"] != "awaiting_permission"
                await asyncio.sleep(0.2)
                operation = await self.api("GET", f"/v1/provisioning/{operation['provision_id']}")
        assert operation["outcome"] == "completed"
        save("install-finished", operation)
        a, c = await self.observe("a"), await self.observe("c")
        old = self.kernel(tuple(self.values["initial-observation-a"]))
        stale = self.kernel(a)
        assert stale["revision_status"] == "predates"
        assert stale["start_revision"] == old["start_revision"]
        assert all(stale[key] == old[key] for key in ("kernel_id", "generation"))
        assert self.kernel(c) == self.kernel(tuple(self.values["initial-observation-c"]))
        assert (await self.observe("b"))[1]["kernels"] == []
        original = await self.api(
            "GET", "/v1/executions/" + self.values["initial-a"]["execution_id"]
        )
        assert original == self.values["initial-a"]
        save("after-install-observations", {"a": a, "c": c, "a-original-unchanged": True})
        save("install-complete", True)

    async def fresh(self) -> None:
        assert self.values.get("install-complete") is True
        b = await self.cell(
            "fresh-b",
            "b",
            "import networkx,json\nprint(json.dumps({'version':networkx.__version__}))",
        )
        a, c = await self.observe("a"), await self.observe("c")
        assert b["result"]["kernel"]["revision_status"] == "current"
        assert b["result"]["kernel"]["start_revision"] == a[1]["current_revision"]
        assert self.kernel(a)["revision_status"] == "predates"
        assert self.kernel(c) == self.kernel(tuple(self.values["initial-observation-c"]))
        for session, expected in (("a", 1729), ("c", 314159)):
            value = await self.cell("retained-global-" + session, session, "print(lab27_value)")
            assert value["result"]["stdout"]["text"] == str(expected) + "\n"
        save("fresh-complete", True)

    async def restart(self) -> None:
        assert self.values.get("fresh-complete") is True
        old = self.kernel(await self.observe("a"))
        restart = await self.api(
            "POST", "/v1/kernels/restart", {"context": self.context("a"), "language": "python"}
        )
        assert restart["generation"] > old["generation"]
        assert restart["revision_status"] == "current"
        value = await self.cell(
            "after-explicit-restart",
            "a",
            "import json,pathlib\nprint(json.dumps({'old_global':'lab27_value' in globals(),'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))\nlab27_value=2026",
        )
        identity = json.loads(value["result"]["stdout"]["text"])
        assert identity["old_global"] is False
        assert (
            identity["boot"]
            == json.loads(self.values["initial-a"]["result"]["stdout"]["text"])["boot"]
        )
        save("explicit-restart", restart)
        save("restart-complete", True)

    async def continuity(self) -> None:
        assert self.values.get("restart-complete") is True
        for session, number in (("a", 2026), ("c", 314159)):
            value = await self.cell(
                "worker-continuity-" + session,
                session,
                "import json,pathlib\nprint(json.dumps({'value':lab27_value,'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))",
            )
            identity = json.loads(value["result"]["stdout"]["text"])
            assert identity["value"] == number
            initial = json.loads(self.values["initial-" + session]["result"]["stdout"]["text"])
            assert identity["boot"] == initial["boot"]
            seen = self.kernel(await self.observe(session))
            expected = (
                self.values["explicit-restart"]
                if session == "a"
                else self.kernel(tuple(self.values["initial-observation-c"]))
            )
            assert seen == expected
        save("continuity-complete", True)

    async def idle(self) -> None:
        assert self.values.get("continuity-complete") is True
        assert guard(self.head)["openshell"]["environments"].get("suspend_after") == 3
        for session in ("a", "c"):
            await self.cell("idle-warmup-" + session, session, "print(lab27_value)")
        observations = []
        started = time.monotonic()
        async with asyncio.timeout(90):
            while True:
                observations.append(
                    {
                        "elapsed": time.monotonic() - started,
                        "a": await self.observe("a", status=None),
                    }
                )
                environments = [
                    await self.api("GET", "/v1/environments/" + self.context(s)["environment_id"])
                    for s in ("a", "c")
                ]
                if all(value["state"] == "stopped" for value in environments):
                    break
                await asyncio.sleep(0.2)
        assert any(row["a"][0] == 200 for row in observations)
        save(
            "idle-suspended-during-GUI-observation",
            {"observations": observations, "environments": environments},
        )
        stopped = []
        for _ in range(30):
            stopped.append(await self.observe("a", status=503))
            await asyncio.sleep(0.2)
        save("thirty-stopped-GUI-observations", stopped)
        proof = LAB / "evidence/kernel-gui-gateway-stopped.json"
        async with asyncio.timeout(20):
            while not proof.exists():
                await asyncio.sleep(0.1)
        gateway = json.loads(bounded(proof, 16384))
        assert gateway["source_head"] == self.head
        assert len(gateway["sandboxes"]) == 2
        assert all(s["phase"].lower() == "stopped" for s in gateway["sandboxes"])
        assert {(s["name"], s["id"]) for s in gateway["sandboxes"]} == {
            (e["sandbox"], e["sandbox_id"]) for e in environments
        }
        assert gateway["sql_pins_unchanged_over_thirty_reads"] is True
        save("gateway-stopped-proof", gateway)
        old = self.values["explicit-restart"]["generation"]
        resumed = await self.cell(
            "ordinary-resume",
            "a",
            "import json\nprint(json.dumps({'old_global':'lab27_value' in globals()}))",
        )
        assert json.loads(resumed["result"]["stdout"]["text"])["old_global"] is False
        assert resumed["kernel_state_lost"] is True
        assert resumed["result"]["kernel"]["generation"] > old
        original = await self.api(
            "GET", "/v1/executions/" + self.values["initial-a"]["execution_id"]
        )
        assert original == self.values["initial-a"]
        save("idle-complete", True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=("init", "serve", "initial", "install", "fresh", "restart", "continuity", "idle"),
    )
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    if args.stage == "init":
        seed(args.head)
        return
    guard(args.head)
    if args.stage == "serve":
        await serve(args.head)
        return
    info = json.loads(bounded(PRIVATE, 4096))
    assert info["source_head"] == args.head
    split = urlsplit(info["login_url"])
    assert split.scheme == "http"
    assert split.hostname == "127.0.0.1"
    assert split.port
    base = f"http://127.0.0.1:{split.port}"
    async with (
        aiohttp.ClientSession(
            headers={
                "Authorization": "Bearer " + bounded(LAB / "worker/client-a.token", 4096).strip()
            },
            timeout=aiohttp.ClientTimeout(total=180),
        ) as worker,
        aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=aiohttp.ClientTimeout(total=10)
        ) as gui,
    ):
        async with gui.get(info["login_url"], allow_redirects=False) as response:
            assert response.status == 303
        await getattr(Checks(args.head, worker, gui, base), args.stage)()


if __name__ == "__main__":
    asyncio.run(main())
