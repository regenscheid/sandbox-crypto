# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Disposable Lab25 CP-12 kernel-revision continuation. No real models or keys.

Start lab_up.sh and PROJECT=lab25 worker_up.sh from frozen reviewed source.
Run init, before, then gracefully restart only the owned lab worker and run after.
Only marked lab25 state and the existing loopback worker are accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any

import aiohttp

LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab25"
ENVIRONMENT = "lab25-kernel-revisions"
MARKER = LAB / "kernel-revisions.created"
EVIDENCE = LAB / "evidence/kernel-revisions.json"
URL = "http://127.0.0.1:8100"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}
EXPECTED = {
    "project": PROJECT,
    "purpose": "disposable CP12 kernel revision acceptance",
    "version": 1,
}
STAMP = str(time.time_ns())


def marked(*, create: bool = False) -> None:
    """Refuse unrelated labs before any request or writer is opened."""
    assert LAB.is_dir()
    assert not LAB.is_symlink()
    assert LAB.stat().st_uid == os.getuid()
    settings = tomllib.loads((LAB / "worker/worker.toml").read_text())
    assert settings["listen"] == "127.0.0.1:8100"
    assert any(client["projects"] == [PROJECT] for client in settings["clients"])
    assert settings.get("openshell", {}).get("daemon", True) is True
    assert settings["openshell"]["environments"]["profiles"]["minimal"].startswith(
        "sandbox-science/minimal:"
    )
    if create:
        with MARKER.open("x") as output:
            json.dump(EXPECTED, output)
    assert not MARKER.is_symlink()
    assert json.loads(MARKER.read_text()) == EXPECTED


class Checks:
    def __init__(self, http: aiohttp.ClientSession) -> None:
        self.http = http
        self.evidence = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {}

    def save(self, key: str, value: Any) -> None:
        self.evidence[key] = value
        EVIDENCE.parent.mkdir(exist_ok=True)
        EVIDENCE.write_text(json.dumps(self.evidence, indent=2) + "\n")
        print(f"recorded {key}", flush=True)

    @staticmethod
    def context(session: str) -> dict[str, str]:
        assert session in {"a", "b", "empty"}
        return {"project_id": PROJECT, "session_id": session, "environment_id": ENVIRONMENT}

    async def api(self, method: str, path: str, body: Any = None, *, status: int = 200) -> Any:
        marked()
        async with self.http.request(method, URL + path, json=body) as response:
            value = await response.json()
            assert response.status == status, (method, path, response.status, value)
            return value

    async def cell(self, name: str, session: str, code: str) -> Any:
        value = await self.api(
            "POST",
            "/v1/executions",
            {
                "kind": "cell",
                "context": self.context(session),
                "language": "python",
                "operation_id": f"lab25-{name}-{STAMP}",
                "timeout_seconds": 120,
                "code": code,
            },
            status=201,
        )
        async with asyncio.timeout(150):
            while value["state"] not in TERMINAL:
                await asyncio.sleep(0.1)
                value = await self.api("GET", f"/v1/executions/{value['execution_id']}")
        self.save(name, value)
        return value

    async def status(self, session: str) -> Any:
        value = await self.api("POST", "/v1/kernels/status", {"context": self.context(session)})
        assert value["environment_id"] == ENVIRONMENT
        assert value["session_id"] == session
        return value

    @staticmethod
    def only(value: Any) -> Any:
        assert len(value["kernels"]) == 1, value
        assert value["kernels"][0]["language"] == "python", value
        return value["kernels"][0]

    async def before(self) -> None:
        assert "initial" not in self.evidence, "Use a fresh lab; stages are not replayed implicitly"
        environment = await self.api(
            "POST",
            "/v1/environments",
            {
                "project_id": PROJECT,
                "environment_id": ENVIRONMENT,
                "profile": "minimal",
                "policy": (LAB / "policy.digest").read_text().strip(),
            },
            status=201,
        )
        self.save("environment-before", environment)
        first = await self.cell(
            "initial",
            "a",
            "import json,sys,pathlib,importlib.util\ncp12_value=1729\n"
            "print(json.dumps({'value':cp12_value,'python':sys.version,'prefix':sys.prefix,"
            "'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),"
            "'networkx_present':importlib.util.find_spec('networkx') is not None}))",
        )
        assert first["state"] == "succeeded", first
        stdout = first["result"]["stdout"]["text"]
        observed = json.loads(stdout.strip())
        assert observed["value"] == 1729, observed
        assert not observed["networkx_present"], observed
        assert first["location"]["backend"] == "openshell"
        assert first["location"]["isolation_tier"] == 3
        old = first["result"]["kernel"]
        assert old["start_revision"] is not None, old
        assert old["revision_status"] == "current", old
        operation = await self.api(
            "POST",
            "/v1/provisioning",
            {
                "operation_id": f"lab25-install-{STAMP}",
                "context": self.context("a"),
                "purpose": "Disposable kernel-revision acceptance",
                "conda": ["networkx"],
            },
            status=201,
        )
        self.save("install-requested", operation)
        async with asyncio.timeout(900):
            while operation["state"] != "finished":
                assert operation["state"] != "awaiting_permission", operation
                await asyncio.sleep(0.1)
                operation = await self.api("GET", f"/v1/provisioning/{operation['provision_id']}")
        self.save("install-finished", operation)
        assert operation["outcome"] == "completed", operation
        a = await self.status("a")
        self.save("a-after-install", a)
        current = a["current_revision"]
        assert current > old["start_revision"]
        stale = self.only(a)
        assert stale["start_revision"] == old["start_revision"]
        assert stale["generation"] == old["generation"]
        assert stale["kernel_id"] == old["kernel_id"]
        assert stale["revision_status"] == "predates"
        retained = await self.cell("retained-global", "a", "print(cp12_value)")
        assert retained["state"] == "succeeded"
        assert retained["result"]["stdout"]["text"] == "1729\n"
        assert retained["result"]["kernel"] == stale
        b = await self.cell(
            "new-session",
            "b",
            "import networkx,json\nprint(json.dumps({'networkx_version':networkx.__version__}))",
        )
        assert b["state"] == "succeeded"
        installed = json.loads(b["result"]["stdout"]["text"].strip())
        assert isinstance(installed["networkx_version"], str)
        assert 0 < len(installed["networkx_version"]) <= 128
        assert b["result"]["kernel"]["start_revision"] == current
        assert b["result"]["kernel"]["revision_status"] == "current"
        b_status = await self.status("b")
        assert self.only(b_status)["kernel_id"] != stale["kernel_id"]
        self.save("b-after-install", b_status)
        empty = await self.status("empty")
        assert empty["kernels"] == []
        assert empty["current_revision"] == current
        self.save("empty-session", empty)
        denied = await self.api(
            "POST",
            "/v1/kernels/status",
            {"context": {**self.context("a"), "project_id": "lab25b"}},
            status=403,
        )
        self.save("other-project-refused", denied)
        self.save("before-stage-complete", True)

    async def after(self) -> None:
        assert self.evidence.get("before-stage-complete") is True
        assert "after-stage-complete" not in self.evidence
        a = await self.status("a")
        self.save("a-after-worker-restart", a)
        assert a == self.evidence["a-after-install"], (a, self.evidence["a-after-install"])
        same = await self.cell(
            "after-worker-restart",
            "a",
            "import pathlib,json\nprint(json.dumps({'value':cp12_value,"
            "'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))",
        )
        assert same["state"] == "succeeded"
        identity = json.loads(same["result"]["stdout"]["text"].strip())
        initial = json.loads(self.evidence["initial"]["result"]["stdout"]["text"].strip())
        assert identity == {"value": 1729, "boot": initial["boot"]}
        assert same["result"]["kernel"] == self.only(a)
        environment = await self.api("GET", f"/v1/environments/{ENVIRONMENT}")
        self.save("environment-after", environment)
        assert environment["sandbox_id"] == self.evidence["environment-before"]["sandbox_id"]
        restart = await self.api(
            "POST",
            "/v1/kernels/restart",
            {"context": self.context("a"), "language": "python"},
        )
        self.save("explicit-restart", restart)
        assert restart["start_revision"] == a["current_revision"]
        assert restart["revision_status"] == "current"
        assert restart["generation"] > self.only(a)["generation"]
        gone = await self.cell("old-global-gone", "a", "print(cp12_value)")
        assert gone["state"] == "failed", gone
        assert gone["result"]["error"]["name"] == "NameError", gone
        fresh = await self.cell("new-global", "a", "cp12_value=2026;print(cp12_value)")
        assert fresh["state"] == "succeeded"
        assert fresh["result"]["stdout"]["text"] == "2026\n"
        assert fresh["result"]["kernel"]["start_revision"] == a["current_revision"]
        original = await self.api(
            "GET", f"/v1/executions/{self.evidence['initial']['execution_id']}"
        )
        assert original == self.evidence["initial"], "Old terminal execution changed"
        self.save("old-execution-unchanged", original)
        self.save("after-stage-complete", True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "before", "after"))
    args = parser.parse_args()
    marked(create=args.action == "init")
    if args.action == "init":
        print("Lab25 marker created; no API request")
        return
    token = (LAB / "worker/client-a.token").read_text().strip()
    async with aiohttp.ClientSession(
        headers={"Authorization": "Bearer " + token}, timeout=aiohttp.ClientTimeout(total=180)
    ) as http:
        checks = Checks(http)
        await (checks.before() if args.action == "before" else checks.after())


if __name__ == "__main__":
    asyncio.run(main())
