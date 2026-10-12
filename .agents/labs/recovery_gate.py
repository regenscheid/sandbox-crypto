# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Lab21 sticky recovery checks. Only marked disposable lab21 state is accepted.

Start lab_up.sh and PROJECT=lab21 worker_up.sh from the reviewed tree first.
Run with .venv/bin/python -I: init prepare success interrupt verify; optionally
use interrupt --restart for worker-offline classification. No tokens are exported.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any

import aiohttp

LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab21"
URL = "http://127.0.0.1:8100"
MARKER = LAB / "recovery-gate.created"
EVIDENCE = LAB / "evidence/recovery-gate.json"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}
STAMP = str(time.time_ns())


def marked(*, create: bool = False) -> None:
    settings = tomllib.loads((LAB / "worker/worker.toml").read_text())
    assert any(PROJECT in item["projects"] for item in settings["clients"])
    assert settings.get("openshell", {}).get("daemon", True) is True
    expected = {"project": PROJECT, "purpose": "disposable CP13 admission acceptance"}
    if create:
        with MARKER.open("x") as out:
            json.dump(expected, out)
    assert json.loads(MARKER.read_text()) == expected


class Checks:
    def __init__(self, http: aiohttp.ClientSession) -> None:
        self.http = http
        self.evidence = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {}

    def save(self, name: str, value: Any) -> None:
        self.evidence[name] = value
        EVIDENCE.parent.mkdir(exist_ok=True)
        EVIDENCE.write_text(json.dumps(self.evidence, indent=2) + "\n")
        print(f"recorded {name}", flush=True)

    def context(self, suffix: str) -> dict[str, str]:
        assert suffix in {"failed", "success", "other"}
        return {
            "project_id": PROJECT,
            "session_id": "main",
            "environment_id": f"{PROJECT}-{suffix}",
        }

    async def api(self, method: str, path: str, body: Any = None, *, status: int = 200) -> Any:
        marked()
        async with self.http.request(method, URL + path, json=body) as response:
            value = await response.json()
            assert response.status == status, (method, path, response.status, value)
            return value

    async def submit(self, name: str, suffix: str, *, kind: str = "cell", **fields: Any) -> Any:
        body = {
            "kind": kind,
            "context": self.context(suffix),
            "operation_id": f"{name}-{STAMP}",
            "timeout_seconds": 900,
            **fields,
        }
        if kind == "cell":
            body["language"] = "python"
        elif kind == "job":
            body.update(purpose=f"Lab21 {name}", expected_progress="Disposable admission marker")
        return await self.api("POST", "/v1/executions", body, status=201)

    async def ended(self, execution: Any) -> Any:
        async with asyncio.timeout(950):
            while execution["state"] not in TERMINAL:
                execution = await self.api("GET", f"/v1/executions/{execution['execution_id']}")
                await asyncio.sleep(0.1)
        return execution

    async def provision(self, name: str, suffix: str, packages: list[str]) -> Any:
        return await self.api(
            "POST",
            "/v1/provisioning",
            {
                "operation_id": f"{name}-{STAMP}",
                "context": self.context(suffix),
                "purpose": "Disposable Lab21 recovery admission probe",
                "conda": packages,
            },
            status=201,
        )

    async def finished(self, operation: Any) -> Any:
        async with asyncio.timeout(900):
            while operation["state"] != "finished":
                try:
                    operation = await self.api(
                        "GET", f"/v1/provisioning/{operation['provision_id']}"
                    )
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)  # explicit disposable worker restart only
                    continue
                assert operation["state"] != "awaiting_permission", operation
                await asyncio.sleep(0.05)
        return operation

    async def write(self, suffix: str, path: str, text: str) -> Any:
        return await self.api(
            "POST",
            "/v1/files/write",
            {
                "context": self.context(suffix),
                "path": path,
                "content_b64": base64.b64encode(text.encode()).decode(),
            },
        )

    async def prepare(self) -> None:
        policy = (LAB / "policy.digest").read_text().strip()
        for suffix in ("failed", "success", "other"):
            value = await self.api(
                "POST",
                "/v1/environments",
                {
                    "project_id": PROJECT,
                    "environment_id": self.context(suffix)["environment_id"],
                    "profile": "minimal",
                    "policy": policy,
                },
                status=201,
            )
            self.save(f"environment-{suffix}", value)
        initial = await self.ended(
            await self.submit("initial-kernel", "failed", code="VALUE=1729; print(VALUE)")
        )
        assert initial["state"] == "succeeded", initial
        self.save("initial-kernel", initial)
        self.save(
            "existing-cell",
            await self.submit(
                "existing-cell",
                "failed",
                code=(
                    "import pathlib,time\np=pathlib.Path('lab21-release.txt')\n"
                    "while not p.exists(): time.sleep(.05)\nprint(VALUE)"
                ),
            ),
        )
        async with asyncio.timeout(30):
            while True:
                live = await self.api(
                    "GET", f"/v1/executions/{self.evidence['existing-cell']['execution_id']}"
                )
                if live["state"] == "running":
                    break
                await asyncio.sleep(0.05)
        self.save("existing-cell-started", live)

    async def success(self) -> None:
        done = await self.finished(await self.provision("success", "success", ["networkx"]))
        assert done["outcome"] in {"completed", "already_satisfied"}, done
        after = await self.ended(await self.submit("after-success", "success", code="print(17)"))
        assert after["state"] == "succeeded", after
        self.save("success-release", {"operation": done, "execution": after})

    async def interrupt(self, restart: bool, offline: bool) -> None:
        sandbox = self.evidence["environment-failed"]["sandbox"]
        cli_env = dict(os.environ)
        cli_env.update(
            XDG_CONFIG_HOME=str(LAB / "config"),
            XDG_STATE_HOME=str(LAB / "state"),
            XDG_DATA_HOME=str(LAB / "data"),
            OPENSHELL_LOCAL_TLS_DIR=str(LAB / "state/openshell/tls"),
            OPENSHELL_TELEMETRY_ENABLED="false",
        )
        monitor = (
            "import pathlib,time,json\np=pathlib.Path('/opt/sandbox-science/conda-meta')\n"
            "old={f.name for f in p.glob('*.json')}\nprint('READY',flush=True)\n"
            "until=time.monotonic()+600\nwhile time.monotonic()<until:\n"
            " new=sorted({f.name for f in p.glob('*.json')}-old)\n"
            " if len(new)>=3: print(json.dumps({'linked_records':new}),flush=True);break\n"
            " time.sleep(.002)\nelse: raise SystemExit('no linking observed')"
        )
        observer = await asyncio.create_subprocess_exec(
            str(Path.home() / ".local/opt/openshell-0.1.3/bin/openshell"),
            "sandbox",
            "exec",
            "-n",
            sandbox,
            "--no-tty",
            "--no-login-shell",
            "--",
            "/opt/sandbox-science/bin/python",
            "-I",
            "-c",
            monitor,
            env=cli_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert observer.stdout is not None
            assert await asyncio.wait_for(observer.stdout.readline(), 30) == b"READY\n"
            operation = await self.provision(
                "interrupt", "failed", ["sympy", "networkx", "matplotlib-base", "scipy"]
            )
            async with asyncio.timeout(600):
                while operation["state"] != "installing":
                    assert operation["state"] not in {"finished", "awaiting_permission"}, operation
                    operation = await self.api(
                        "GET", f"/v1/provisioning/{operation['provision_id']}"
                    )
                    await asyncio.sleep(0.01)
            self.save("hold-operation", operation)
            self.save(
                "queued-cell",
                await self.submit(
                    "queued-cell",
                    "failed",
                    code=(
                        "import pathlib; pathlib.Path('forbidden-cell.txt').write_text('started')"
                    ),
                ),
            )
            self.save(
                "queued-job",
                await self.submit(
                    "queued-job", "failed", kind="job", script="printf started > forbidden-job.txt"
                ),
            )
            linked = json.loads(await asyncio.wait_for(observer.stdout.readline(), 600))
            self.save("link-observer", linked)
            operation = await self.api("GET", f"/v1/provisioning/{operation['provision_id']}")
            assert operation["state"] == "installing", operation
            self.save("before-interrupt", operation)
            if restart or offline:
                process = await asyncio.create_subprocess_exec(
                    "systemctl", "--user", "stop" if offline else "restart", "openshell-lab-worker"
                )
                assert await process.wait() == 0
                if offline:
                    self.save(
                        "worker-offline",
                        {"operation": operation, "requires_explicit_restart": True},
                    )
                    return
            else:
                await self.api("POST", f"/v1/provisioning/{operation['provision_id']}/cancel")
            done = await self.finished(operation)
            assert done["outcome"] == "needs_recovery", done
            self.save("needs-recovery", done)
        finally:
            if observer.returncode is None:
                observer.terminate()
            await observer.communicate()

    async def verify(self) -> None:
        done = await self.finished(self.evidence["hold-operation"])
        assert done["outcome"] == "needs_recovery", done
        self.save("needs-recovery", done)
        for kind, fields in (
            ("shell", {"script": "true"}),
            ("cell", {"language": "python", "code": "1"}),
            ("job", {"script": "true", "purpose": "denied", "expected_progress": "none"}),
            ("pdf", {"operation": "info", "path": "missing.pdf"}),
        ):
            refused = await self.api(
                "POST",
                "/v1/executions",
                {
                    "operation_id": f"denied-{kind}-{STAMP}",
                    "context": self.context("failed"),
                    "kind": kind,
                    **fields,
                },
                status=503,
            )
            assert refused["error"]["kind"] == "unavailable", refused
            self.save(f"refused-{kind}", refused)
        self.save(
            "refused-restart",
            await self.api(
                "POST",
                "/v1/kernels/restart",
                {"context": self.context("failed"), "language": "python"},
                status=503,
            ),
        )
        self.save(
            "refused-provision",
            await self.api(
                "POST",
                "/v1/provisioning",
                {
                    "operation_id": f"denied-provision-{STAMP}",
                    "context": self.context("failed"),
                    "purpose": "Must remain refused",
                    "conda": ["six"],
                },
                status=503,
            ),
        )
        # Release the already-running cell through file recovery access before
        # awaiting its same-kernel queued waiter; otherwise the fixture deadlocks.
        self.save("file-recovery-write", await self.write("failed", "lab21-release.txt", "release"))
        done = await self.ended(self.evidence["existing-cell"])
        assert done["state"] == "succeeded", done
        assert "1729" in done["result"]["stdout"]["text"], done
        self.save("preserved-kernel-cell", done)
        for name in ("queued-cell", "queued-job"):
            done = await self.ended(self.evidence[name])
            assert done["started_at"] is None, done
            assert done["state"] in {"failed", "cancelled"}, done
            self.save(f"ended-{name}", done)
            await self.api("POST", f"/v1/executions/{done['execution_id']}/cancel")
        for path in ("forbidden-cell.txt", "forbidden-job.txt"):
            absent = await self.api(
                "POST",
                "/v1/files/read",
                {"context": self.context("failed"), "path": path},
                status=404,
            )
            self.save(f"absent-{path}", {"http_status": 404, "result": absent})
        recovered = await self.api(
            "POST",
            "/v1/files/read",
            {"context": self.context("failed"), "path": "lab21-release.txt"},
        )
        assert base64.b64decode(recovered["content_b64"]) == b"release", recovered
        self.save("recovery-file-read", recovered)
        self.save("revisions", await self.api("GET", "/v1/environments/lab21-failed/revisions"))
        operation = self.evidence["needs-recovery"]
        for revision in {operation["revision_before"], operation["revision_after"]} - {None}:
            marked()
            async with self.http.get(
                URL + f"/v1/environments/lab21-failed/revisions/{revision}/lockfile"
            ) as response:
                raw = await response.read()
                assert response.status == 200, (revision, response.status, raw[:1000])
                value = json.loads(raw)
                digest = hashlib.sha256(raw).hexdigest()
                assert response.headers.get("X-Content-SHA256") == digest
                self.save(
                    f"lockfile-{revision}",
                    {
                        "http_status": response.status,
                        "body_bytes": len(raw),
                        "body_sha256": digest,
                        "conda_packages": len(value.get("conda", [])),
                        "pip_packages": len(value.get("pip", [])),
                    },
                )
        other = await self.ended(await self.submit("other-healthy", "other", code="print(19)"))
        assert other["state"] == "succeeded", other
        self.save("other-healthy", other)
        self.save(
            "queue-sticky", await self.api("GET", "/v1/environments/lab21-failed/provisioning")
        )
        assert self.evidence["queue-sticky"]["status"] == "needs_recovery"

    async def delete_reuse(self) -> None:
        # This action is selected separately after verify has durably saved its
        # evidence. It targets only the marked failed environment, never peers.
        assert self.evidence["queue-sticky"]["status"] == "needs_recovery"
        before = await self.api("GET", "/v1/environments/lab21-failed")
        assert before["sandbox_id"] == self.evidence["environment-failed"]["sandbox_id"]
        self.save("delete-reuse-before", before)
        for attempt in range(2):
            deleted = await self.api("DELETE", "/v1/environments/lab21-failed")
            assert deleted["state"] == "deleted", deleted
            assert deleted["sandbox_id"] == before["sandbox_id"], deleted
            self.save(f"delete-reuse-deleted-{attempt + 1}", deleted)
        replacement = await self.api(
            "POST",
            "/v1/environments",
            {
                "project_id": PROJECT,
                "environment_id": "lab21-failed",
                "profile": "minimal",
                "policy": (LAB / "policy.digest").read_text().strip(),
            },
            status=201,
        )
        assert replacement["sandbox_id"]
        assert replacement["sandbox_id"] != before["sandbox_id"]
        self.save("delete-reuse-after", replacement)
        execution = await self.ended(await self.submit("after-reuse", "failed", code="print(23)"))
        assert execution["state"] == "succeeded", execution
        assert "23" in execution["result"]["stdout"]["text"], execution
        self.save("delete-reuse-execution", execution)
        other = await self.ended(await self.submit("after-reuse-peer", "other", code="print(29)"))
        assert other["state"] == "succeeded", other
        assert "29" in other["result"]["stdout"]["text"], other
        self.save("delete-reuse-other-healthy", other)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "parts",
        nargs="+",
        choices=("init", "prepare", "success", "interrupt", "verify", "delete-reuse"),
    )
    interruption = parser.add_mutually_exclusive_group()
    interruption.add_argument("--restart", action="store_true")
    interruption.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if "init" in args.parts:
        assert args.parts[0] == "init"
        marked(create=True)
    else:
        marked()
    token = (LAB / "worker/client-a.token").read_text().strip()
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"}, timeout=aiohttp.ClientTimeout(total=1000)
    ) as http:
        checks = Checks(http)
        for part in args.parts:
            if part == "prepare":
                await checks.prepare()
            elif part == "success":
                await checks.success()
            elif part == "delete-reuse":
                await checks.delete_reuse()
            elif part == "interrupt":
                await checks.interrupt(args.restart, args.offline)
            elif part == "verify":
                await checks.verify()


if __name__ == "__main__":
    asyncio.run(main())
