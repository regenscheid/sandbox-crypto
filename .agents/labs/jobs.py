# SPDX-License-Identifier: MIT
# ruff: noqa: S101
# Assertions are acceptance checks against disposable lab data, as in the test suite.
"""D83 checks against the disposable babbage lab worker, never real project data.

Usage: .venv/bin/python -I .agents/labs/jobs.py [envs basics output limits close delete idle restart]
Start lab_up.sh and PROJECT=lab19 worker_up.sh first. Restart deliberately stops
the disposable worker; it must be the last part. Evidence contains no token.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

LAB = Path.home() / ".local/state/openshell-lab"
PROJECT = "lab19"
ENVIRONMENT = "lab19-a"
URL = "http://127.0.0.1:8100"
STAMP = str(time.time_ns())
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}


class Checks:
    def __init__(self, http: aiohttp.ClientSession) -> None:
        self.http = http
        previous = LAB / "evidence/jobs.json"
        self.evidence: dict[str, Any] = (
            json.loads(previous.read_text()) if previous.is_file() else {}
        )

    def context(self, session: str = "main") -> dict[str, str]:
        return {"project_id": PROJECT, "session_id": session, "environment_id": ENVIRONMENT}

    async def api(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with self.http.request(method, URL + path, json=body) as response:
            value = await response.json()
            if response.status >= 400:
                raise RuntimeError(f"{method} {path}: HTTP {response.status}: {value}")
            return value

    async def submit(
        self, name: str, script: str, *, session: str = "main", job: bool = True, **kw: Any
    ) -> dict[str, Any]:
        body = {
            "kind": "job" if job else "shell",
            "operation_id": f"{name}-{STAMP}",
            "context": self.context(session),
            "script": script,
            **kw,
        }
        if job:
            body.update(purpose=f"Lab 19: {name}", expected_progress="The lab asserts its output.")
        return await self.api("POST", "/v1/executions", body)

    async def finish(self, execution: dict[str, Any], limit: float = 45) -> dict[str, Any]:
        async with asyncio.timeout(limit):
            while execution["state"] not in TERMINAL:
                await asyncio.sleep(0.1)
                execution = await self.api("GET", f"/v1/executions/{execution['execution_id']}")
        return execution

    async def shell(self, name: str, script: str, session: str = "main") -> dict[str, Any]:
        execution = await self.finish(await self.submit(name, script, session=session, job=False))
        assert execution["state"] == "succeeded", execution
        return execution

    def save(self, name: str, value: Any) -> None:
        self.evidence[name] = value
        target = LAB / "evidence/jobs.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(self.evidence, indent=2) + "\n")
        print(f"PASS {name}", flush=True)

    async def envs(self) -> None:
        value = await self.api(
            "POST",
            "/v1/environments",
            {
                "environment_id": ENVIRONMENT,
                "project_id": PROJECT,
                "profile": "minimal",
                "policy": (LAB / "policy.digest").read_text().strip(),
            },
        )
        await self.shell("warm", "printf 'executor ready\\n'")
        self.save("environment", value)

    async def basics(self) -> None:
        started = time.monotonic()
        submitted = await self.submit("parallel", "sleep 5; printf 'done\\n'")
        elapsed = time.monotonic() - started
        assert elapsed < 2, elapsed
        replay = await self.submit("parallel", "sleep 5; printf 'done\\n'")
        assert replay["execution_id"] == submitted["execution_id"]
        prefix: list[dict[str, Any]] = []
        async with self.http.get(URL + f"/v1/executions/{submitted['execution_id']}/events") as r:
            assert r.status == 200
            async for line in r.content:
                if line.strip():
                    prefix.append(json.loads(line))
                    break
        foreground = await self.shell("foreground", "printf 'foreground\\n'")
        still_running = await self.api("GET", f"/v1/executions/{submitted['execution_id']}")
        assert still_running["state"] not in TERMINAL, still_running
        # Another HTTP connection can reconcile the same job without its original stream.
        headers = dict(self.http.headers)
        async with (
            aiohttp.ClientSession(headers=headers) as reconnected,
            reconnected.get(URL + f"/v1/executions/{submitted['execution_id']}") as r,
        ):
            assert r.status == 200
            assert (await r.json())["execution_id"] == submitted["execution_id"]
        done = await self.finish(submitted)
        assert done["state"] == "succeeded", done
        assert done["result"]["stdout"]["text"] == "done\n", done
        suffix: list[dict[str, Any]] = []
        async with self.http.get(
            URL + f"/v1/executions/{submitted['execution_id']}/events?after={prefix[-1]['seq']}"
        ) as r:
            assert r.status == 200
            async for line in r.content:
                if line.strip():
                    suffix.append(json.loads(line))
        assert [event["seq"] for event in prefix + suffix] == list(range(1, done["last_seq"] + 1))
        assert suffix[-1]["type"] == "result", suffix
        self.save(
            "parallel_and_reconnect",
            {
                "submit_seconds": elapsed,
                "job": done,
                "foreground": foreground,
                "events": prefix + suffix,
            },
        )

    async def output(self) -> None:
        script = (
            "python -u -c \"import time; print('head', flush=True); "
            "time.sleep(1); print('x' * 16000); print('tail', flush=True)\""
        )
        submitted = await self.submit("output", script, output_limit_bytes=1024)
        async with asyncio.timeout(10):
            while True:
                live = await self.api("GET", f"/v1/jobs/{submitted['execution_id']}")
                if "head" in (live.get("stdout") or {}).get("text", ""):
                    break
                await asyncio.sleep(0.05)
        assert live["execution"]["state"] not in TERMINAL, live
        done = await self.finish(submitted)
        assert done["state"] == "succeeded", done
        summary = done["result"]["stdout"]
        assert summary["truncated"]
        assert "head" in summary["text"]
        assert "tail" in summary["text"]
        path = f".jobs/{done['execution_id']}/stdout.log"
        raw = await self.api("POST", "/v1/files/read", {"context": self.context(), "path": path})
        text = base64.b64decode(raw["content_b64"]).decode()
        assert text == "head\n" + "x" * 16000 + "\ntail\n", (len(text), raw)
        self.save("live_and_whole_output", {"live": live, "job": done, "log": raw})

    async def limits(self) -> None:
        timeout = await self.finish(
            await self.submit("timeout", "printf 'before timeout\\n'; sleep 30", timeout_seconds=1)
        )
        assert timeout["state"] == "timed_out", timeout
        assert "before timeout" in timeout["result"]["stdout"]["text"], timeout
        jobs: list[dict[str, Any]] = []
        session = f"quota-{STAMP}"
        try:
            jobs = await asyncio.gather(
                *(self.submit(f"quota-{i}", "sleep 30", session=session) for i in range(4))
            )
            body = {
                "kind": "job",
                "operation_id": f"quota-over-{STAMP}",
                "context": self.context(session),
                "script": "sleep 30",
                "purpose": "Lab 19: exceed the session quota",
                "expected_progress": "Must be refused before execution.",
            }
            async with self.http.post(URL + "/v1/executions", json=body) as r:
                refusal = await r.json()
                assert r.status == 403, refusal
                assert refusal["error"]["kind"] == "policy_denied", refusal
                for job in jobs:
                    assert job["execution_id"] in refusal["error"]["message"], refusal
            listing = await self.api(
                "GET",
                f"/v1/jobs?project_id={PROJECT}&session_id={session}&environment_id={ENVIRONMENT}",
            )
            assert {job["execution_id"] for job in listing["jobs"]} == {
                job["execution_id"] for job in jobs
            }
            self.save(
                "timeout_and_quota", {"timeout": timeout, "refusal": refusal, "list": listing}
            )
        finally:
            for job in jobs:
                await self.api("POST", f"/v1/executions/{job['execution_id']}/cancel")
            for job in jobs:
                done = await self.finish(job)
                assert done["state"] == "cancelled", done

    async def close(self) -> None:
        script = (
            'python -u -c "import os, subprocess, time; '
            "p = subprocess.Popen(['sleep','30'], start_new_session=True); "
            "open('job-child.pid','w').write(str(p.pid)); "
            "print('started', flush=True); time.sleep(30)\""
        )
        job = await self.submit("close", script, session="closing")
        await asyncio.sleep(0.75)
        closed = await self.api("POST", "/v1/sessions/close", {"context": self.context("closing")})
        done = await self.finish(job)
        assert done["state"] == "cancelled", done
        check = (
            'python -c "from pathlib import Path; '
            "p=Path('/proc')/Path('job-child.pid').read_text()/'stat'; "
            "assert not p.exists() or p.read_text().rsplit(')',1)[1].split()[0]=='Z'; "
            "print('child stopped')\""
        )
        no_child = await self.shell("child-stopped", check, session="closing")
        self.save(
            "session_close_and_orphan_cleanup", {"closed": closed, "job": done, "check": no_child}
        )

    async def delete(self) -> None:
        environment = "lab19-delete"
        await self.api(
            "POST",
            "/v1/environments",
            {
                "environment_id": environment,
                "project_id": PROJECT,
                "profile": "minimal",
                "policy": (LAB / "policy.digest").read_text().strip(),
            },
        )
        job = await self.api(
            "POST",
            "/v1/executions",
            {
                "kind": "job",
                "operation_id": f"delete-{STAMP}",
                "context": {**self.context(), "environment_id": environment},
                "script": "sleep 30",
                "purpose": "Lab 19: delete only this disposable environment",
                "expected_progress": "Cancellation before the sandbox is deleted.",
            },
        )
        deleted = await self.api("DELETE", f"/v1/environments/{environment}")
        done = await self.finish(job)
        assert done["state"] == "cancelled", done
        recreated = await self.api(
            "POST",
            "/v1/environments",
            {
                "environment_id": environment,
                "project_id": PROJECT,
                "profile": "minimal",
                "policy": (LAB / "policy.digest").read_text().strip(),
            },
        )
        next_job = await self.api(
            "POST",
            "/v1/executions",
            {
                "kind": "job",
                "operation_id": f"recreated-{STAMP}",
                "context": {**self.context(), "environment_id": environment},
                "script": "printf 'recreated\\n'",
                "purpose": "Lab 19: recreate the deleted environment",
                "expected_progress": "A new job succeeds after recreation.",
            },
        )
        next_done = await self.finish(next_job)
        assert next_done["state"] == "succeeded", next_done
        self.save(
            "environment_delete_cancels_accepted_job",
            {"environment": deleted, "job": done, "recreated": recreated, "next_job": next_done},
        )

    async def idle(self) -> None:
        # The operator must set suspend_after=3 in this disposable worker's settings.
        # Observe both busy and stopped states so a disabled suspender cannot pass.
        job = await self.submit("idle", "sleep 12; printf 'kept alive\\n'")
        await asyncio.sleep(7)
        busy = await self.api("GET", f"/v1/environments/{ENVIRONMENT}")
        assert busy["state"] == "ready", busy
        state = await self.api("GET", f"/v1/executions/{job['execution_id']}")
        assert state["state"] not in TERMINAL, state
        done = await self.finish(job)
        assert done["state"] == "succeeded", done
        async with asyncio.timeout(30):
            while True:
                await asyncio.sleep(1)
                stopped = await self.api("GET", f"/v1/environments/{ENVIRONMENT}")
                if stopped["state"] == "stopped":
                    break
        self.save("job_prevents_idle_suspend", {"busy": busy, "job": done, "idle": stopped})
        await self.shell("resume-after-idle", "printf 'resumed\\n'")

    async def restart(self) -> None:
        job = await self.submit("restart", "printf 'before restart\\n'; sleep 30")
        async with asyncio.timeout(10):
            while True:
                live = await self.api("GET", f"/v1/jobs/{job['execution_id']}")
                if "before restart" in (live.get("stdout") or {}).get("text", ""):
                    break
                await asyncio.sleep(0.1)
        assert live["execution"]["state"] == "running", live
        # Only the unit created by worker_up.sh in this disposable lab may be stopped.
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", "openshell-lab-worker"], check=True)
        root = Path(__file__).resolve().parents[2]
        subprocess.run(  # noqa: S603 -- executable from this checkout, disposable lab paths
            [
                "/usr/bin/systemd-run",
                "--user",
                "--unit=openshell-lab-worker",
                "--collect",
                "-q",
                f"--working-directory={LAB / 'worker'}",
                str(root / ".venv/bin/sandbox-science-worker"),
                "serve",
                "--settings",
                "worker.toml",
                "--log-level",
                "info",
            ],
            check=True,
        )
        async with asyncio.timeout(30):
            while True:
                try:
                    done = await self.api("GET", f"/v1/executions/{job['execution_id']}")
                    break
                except (aiohttp.ClientError, RuntimeError):
                    await asyncio.sleep(0.2)
        assert done["state"] == "lost", done
        raw = await self.api(
            "POST",
            "/v1/files/read",
            {"context": self.context(), "path": f".jobs/{job['execution_id']}/stdout.log"},
        )
        assert "before restart" in base64.b64decode(raw["content_b64"]).decode(), raw
        self.save(
            "worker_restart_explicitly_lost_before_daemon", {"job": done, "retained_log": raw}
        )


async def main() -> None:
    if not (LAB / "worker/client-a.token").is_file():
        raise SystemExit("Start the disposable lab19 worker before running these checks.")
    parts = sys.argv[1:] or ["envs", "basics", "output", "limits", "close", "delete", "restart"]
    if set(parts) - {"envs", "basics", "output", "limits", "close", "delete", "idle", "restart"}:
        raise SystemExit("Unknown part; see the usage at the top of this file.")
    token = (LAB / "worker/client-a.token").read_text().strip()
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=180),
    ) as http:
        async with asyncio.timeout(30):
            while True:
                try:
                    async with http.get(URL + f"/v1/environments/{ENVIRONMENT}") as response:
                        assert response.status in {200, 404}, await response.text()
                    break
                except aiohttp.ClientConnectorError:
                    await asyncio.sleep(0.1)
        checks = Checks(http)
        for part in parts:
            await getattr(checks, part)()


if __name__ == "__main__":
    asyncio.run(main())
