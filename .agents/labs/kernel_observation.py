# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Root-owned disposable Lab26 observation-only kernel acceptance.

Use a reviewed frozen worker with PROJECT=lab26. This helper starts no services,
changes no settings and only accepts the marked, owned loopback lab. Stages:
init, active, restarted, reconnected. Root restarts the owned worker without
idle suspension between active/restarted, then enables suspend_after=3 and
restarts it again immediately before reconnected. That stage includes idle
polling in the same process, without an operator pause after the warmup cell.
Startup recovery reconnects remembered endpoints before serving requests; the
restart stage therefore expects an already healthy channel, not a detached one.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from sandbox_science.contracts import ErrorKind, ExecutionContext
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab26"
ENVIRONMENT = "lab26-kernel-observation"
URL = "http://127.0.0.1:8100"
MARKER = LAB / "kernel-observation.created"
EVIDENCE = LAB / "evidence/kernel-observation.json"
PURPOSE = {"version": 1, "project": PROJECT, "purpose": "disposable kernel observation acceptance"}
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}
STAMP = str(time.time_ns())


def marked(*, create: bool = False) -> dict[str, Any]:
    """Refuse another lab before opening a request, database or evidence writer."""
    assert LAB.is_dir()
    assert not LAB.is_symlink()
    assert LAB.stat().st_uid == os.getuid()
    settings = tomllib.loads((LAB / "worker/worker.toml").read_text())
    assert settings["listen"] == "127.0.0.1:8100"
    assert any(c["projects"] == [PROJECT] for c in settings["clients"])
    assert settings["openshell"].get("daemon", True) is True
    assert settings["openshell"]["environments"]["profiles"]["minimal"].startswith(
        "sandbox-science/minimal:"
    )
    if create:
        with MARKER.open("x") as out:
            json.dump(PURPOSE, out)
    assert not MARKER.is_symlink()
    assert json.loads(MARKER.read_text()) == PURPOSE
    return settings


def birth_rows() -> dict[str, Any]:
    """Read bounded birth, epoch and generation rows without private endpoints."""
    marked()
    path = LAB / "worker/state/worker.sqlite3"
    assert path.is_file()
    assert not path.is_symlink()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        births = db.execute(
            "SELECT environment_id,session_id,language,"
            "CASE WHEN typeof(record)='text' AND length(CAST(record AS BLOB))<=2048"
            " THEN record END FROM kernel_start_revisions WHERE environment_id=?"
            " ORDER BY session_id,language LIMIT 7",
            (ENVIRONMENT,),
        ).fetchall()
        assert len(births) <= 6
        assert all(row[3] is not None for row in births)
        epochs = db.execute(
            "SELECT environment_id,substr(incarnation,1,65),epoch,revision,substr(fingerprint,1,33)"
            " FROM kernel_package_epochs WHERE environment_id=? LIMIT 2",
            (ENVIRONMENT,),
        ).fetchall()
        assert len(epochs) <= 1
        generations = db.execute(
            "SELECT environment_id,session_id,language,"
            "CASE WHEN length(CAST(incarnation AS BLOB))<=65 THEN incarnation END,base,last"
            " FROM kernels WHERE environment_id=? ORDER BY session_id,language LIMIT 7",
            (ENVIRONMENT,),
        ).fetchall()
        assert len(generations) <= 6
        assert all(row[3] is not None for row in generations)
    value = {"births": births, "epochs": epochs, "generations": generations}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {"rows": value, "sha256": hashlib.sha256(raw).hexdigest()}


class Checks:
    def __init__(self, http: aiohttp.ClientSession, worker: WorkerClient) -> None:
        self.http = http
        self.worker = worker
        self.values: dict[str, Any] = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {}

    def save(self, name: str, value: Any) -> None:
        marked()
        self.values[name] = value
        EVIDENCE.parent.mkdir(exist_ok=True)
        EVIDENCE.write_text(json.dumps(self.values, indent=2) + "\n")
        print(f"PASS {name}", flush=True)

    @staticmethod
    def context(session: str = "a") -> dict[str, str]:
        assert session in {"a", "empty"}
        return {"project_id": PROJECT, "environment_id": ENVIRONMENT, "session_id": session}

    async def api(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        marked()
        assert path.startswith("/v1/")
        assert "?" not in path
        assert "#" not in path
        async with self.http.request(
            method, URL + path, json=body, allow_redirects=False
        ) as response:
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                assert len(raw) <= 1024 * 1024
            return response.status, json.loads(raw)

    async def success(self, method: str, path: str, body: Any = None) -> Any:
        status, value = await self.api(method, path, body)
        assert status in {200, 201}, (path, status, value)
        return value

    async def observe(self, session: str = "a") -> tuple[int, Any]:
        async with asyncio.timeout(10):
            status, value = await self.api(
                "POST", "/v1/kernels/observe", {"context": self.context(session)}
            )
        assert status in {200, 503}, (status, value)
        if status == 200:
            assert value["environment_id"] == ENVIRONMENT
            assert value["session_id"] == session
            assert len(value["kernels"]) <= 2
            stamp = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
            assert stamp.utcoffset() is not None
        else:
            assert value["error"]["kind"] == "unavailable", value
        return status, value

    async def cell(self, name: str, code: str) -> Any:
        value = await self.success(
            "POST",
            "/v1/executions",
            {
                "kind": "cell",
                "context": self.context(),
                "language": "python",
                "operation_id": f"lab26-{name}-{STAMP}",
                "timeout_seconds": 120,
                "code": code,
            },
        )
        async with asyncio.timeout(150):
            while value["state"] not in TERMINAL:
                await asyncio.sleep(0.1)
                value = await self.success("GET", f"/v1/executions/{value['execution_id']}")
        assert value["state"] == "succeeded", value
        assert value["location"]["backend"] == "openshell"
        assert value["location"]["isolation_tier"] == 3
        self.save(name, value)
        return value

    async def active(self) -> None:
        assert "active-complete" not in self.values
        assert marked()["openshell"]["environments"].get("suspend_after") is None
        env = await self.success(
            "POST",
            "/v1/environments",
            {
                "project_id": PROJECT,
                "environment_id": ENVIRONMENT,
                "profile": "minimal",
                "policy": (LAB / "policy.digest").read_text().strip(),
            },
        )
        self.save("environment-created", env)
        assert (await self.observe())[0] == 503  # no executor was attached by a read
        self.save("before-first-execution", await self.observe())
        first = await self.cell(
            "initial",
            "import json,pathlib\nlab26_value=1729\nprint(json.dumps({"
            "'value':lab26_value,'boot':pathlib.Path('/proc/sys/kernel/random/boot_id')"
            ".read_text().strip()}))",
        )
        self.save("births-before-active-observers", birth_rows())
        observations = [await self.observe() for _ in range(20)]
        assert all(status == 200 for status, _ in observations)
        pins = first["result"]["kernel"]
        assert all(value["kernels"] == [pins] for _, value in observations)
        assert birth_rows() == self.values["births-before-active-observers"]
        self.save("active-observations", observations)
        official = await self.worker.observe_kernels(ExecutionContext(**self.context()))
        assert official.model_dump(mode="json")["kernels"] == [pins]
        self.save("bounded-worker-client-active", official.model_dump(mode="json"))
        empty = await self.observe("empty")
        assert empty[0] == 200
        assert empty[1]["kernels"] == []
        self.save("empty-session", empty)
        status, denied = await self.api(
            "POST",
            "/v1/kernels/observe",
            {"context": {**self.context(), "project_id": "lab26b"}},
        )
        assert status == 403
        self.save("foreign-project-denied", {"status": status, "body": denied})
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as unauth,
            unauth.post(URL + "/v1/kernels/observe", json={"context": self.context()}) as r,
        ):
            assert r.status == 401
            self.save("unauthenticated-denied", {"status": r.status, "body": await r.json()})
        assert birth_rows() == self.values["births-before-active-observers"]
        self.save("births-after-active-observers", birth_rows())
        same = await self.cell("global-preserved", "print(lab26_value)")
        assert same["result"]["stdout"]["text"] == "1729\n"
        assert same["result"]["kernel"] == pins
        self.save("active-complete", True)

    async def restarted(self) -> None:
        assert self.values.get("active-complete") is True
        before = birth_rows()
        env = await self.success("GET", f"/v1/environments/{ENVIRONMENT}")
        assert env["sandbox_id"] == self.values["environment-created"]["sandbox_id"]
        results = []
        for _ in range(15):
            results.append(await self.observe())
            await asyncio.sleep(0.2)
        pins = [self.values["initial"]["result"]["kernel"]]
        assert all(status == 200 and value["kernels"] == pins for status, value in results)
        official = await self.worker.observe_kernels(ExecutionContext(**self.context()))
        assert official.model_dump(mode="json")["kernels"] == pins
        self.save("bounded-worker-client-after-startup-recovery", official.model_dump(mode="json"))
        assert birth_rows() == before
        assert await self.success("GET", f"/v1/environments/{ENVIRONMENT}") == env
        self.save("startup-recovered-observations", results)
        self.save("startup-recovered-births-unchanged", before)
        self.save("startup-recovered-environment-unchanged", env)
        self.save("restarted-complete", True)

    async def reconnected(self) -> None:
        assert self.values.get("restarted-complete") is True
        assert marked()["openshell"]["environments"].get("suspend_after") == 3
        explicit = await self.success("POST", "/v1/kernels/status", {"context": self.context()})
        assert explicit["kernels"] == [self.values["initial"]["result"]["kernel"]]
        observed = await self.observe()
        assert observed[0] == 200
        assert observed[1]["kernels"] == explicit["kernels"]
        self.save("ordinary-status-after-startup-recovery", explicit)
        self.save("observer-after-ordinary-status", observed)
        same = await self.cell(
            "global-after-worker-restart",
            "import json,pathlib\nprint(json.dumps({'value':lab26_value,'boot':"
            "pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))",
        )
        assert json.loads(same["result"]["stdout"]["text"]) == json.loads(
            self.values["initial"]["result"]["stdout"]["text"]
        )
        env = await self.success("GET", f"/v1/environments/{ENVIRONMENT}")
        assert env["sandbox_id"] == self.values["environment-created"]["sandbox_id"]
        self.save("same-VM-after-worker-restarts", env)
        self.save("reconnected-complete", True)
        await self.idle()

    async def idle(self) -> None:
        assert self.values.get("reconnected-complete") is True
        assert marked()["openshell"]["environments"].get("suspend_after") == 3
        observations = []
        started = time.monotonic()
        async with asyncio.timeout(90):
            while True:
                observations.append(
                    {"elapsed": time.monotonic() - started, "observation": await self.observe()}
                )
                env = await self.success("GET", f"/v1/environments/{ENVIRONMENT}")
                if env["state"] == "stopped":
                    break
                await asyncio.sleep(0.2)
        assert any(v["observation"][0] == 200 for v in observations)
        self.save(
            "idle-suspended-during-observation", {"environment": env, "observations": observations}
        )
        before = birth_rows()
        stopped = []
        for _ in range(30):
            stopped.append(await self.observe())
            assert await self.success("GET", f"/v1/environments/{ENVIRONMENT}") == env
            await asyncio.sleep(0.2)
        assert all(status == 503 for status, _ in stopped)
        try:
            await self.worker.observe_kernels(ExecutionContext(**self.context()))
        except WorkerAPIError as error:
            if error.kind is not ErrorKind.UNAVAILABLE:
                raise AssertionError("Unexpected stopped observer error kind") from error
            self.save(
                "bounded-worker-client-unavailable",
                {"kind": error.kind.value, "status": error.status},
            )
        else:
            raise AssertionError("Stopped worker client returned a false empty success")
        assert birth_rows() == before
        self.save("stopped-observations-do-not-wake", stopped)
        self.save(
            "stopped-births-and-environment-unchanged", {"births": before, "environment": env}
        )
        resumed = await self.cell(
            "ordinary-cell-resumes",
            "import json\nprint(json.dumps({'old_global':'lab26_value' in globals()}))",
        )
        assert json.loads(resumed["result"]["stdout"]["text"]) == {"old_global": False}
        assert resumed["kernel_state_lost"] is True
        assert (
            resumed["result"]["kernel"]["generation"]
            > self.values["initial"]["result"]["kernel"]["generation"]
        )
        old = await self.success("GET", f"/v1/executions/{self.values['initial']['execution_id']}")
        assert old == self.values["initial"]
        self.save("prior-terminal-execution-unchanged", old)
        self.save("idle-complete", True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("init", "active", "restarted", "reconnected"))
    args = parser.parse_args()
    marked(create=args.stage == "init")
    if args.stage == "init":
        print("Lab26 marker created; no API request")
        return
    token = (LAB / "worker/client-a.token").read_text().strip()
    async with (
        aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + token}, timeout=aiohttp.ClientTimeout(total=180)
        ) as http,
        WorkerClient(URL, token, session=http) as worker,
    ):
        await getattr(Checks(http, worker), args.stage)()


if __name__ == "__main__":
    asyncio.run(main())
