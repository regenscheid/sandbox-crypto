# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Executor-daemon acceptance checks against the disposable lab20 worker.

Start lab_up.sh and PROJECT=lab20 worker_up.sh, then restart the worker from the
branch under test. This deliberately stops/restarts only that transient lab unit.
Usage: .venv/bin/python -I .agents/labs/recovery.py envs running offline kernel noisy timeout
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

LAB = Path.home() / ".local/state/openshell-lab"
ROOT = Path(__file__).resolve().parents[2]
PROJECT, ENVIRONMENT = "lab20", "lab20-a"
URL = "http://127.0.0.1:8100"
STAMP = str(time.time_ns())
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}


class Checks:
    def __init__(self, http: aiohttp.ClientSession) -> None:
        self.http = http
        self.path = LAB / "evidence/recovery.json"
        self.evidence = json.loads(self.path.read_text()) if self.path.exists() else {}

    def context(self) -> dict[str, str]:
        return {"project_id": PROJECT, "session_id": "main", "environment_id": ENVIRONMENT}

    async def api(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        async with self.http.request(method, URL + path, json=body) as response:
            value = await response.json()
            if response.status >= 400:
                raise RuntimeError(f"{method} {path}: HTTP {response.status}: {value}")
            return value

    async def ready(self) -> None:
        async with asyncio.timeout(45):
            while True:
                try:
                    async with self.http.get(URL + f"/v1/environments/{ENVIRONMENT}") as r:
                        assert r.status in {200, 404}, await r.text()
                    return
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)

    def save(self, name: str, value: Any) -> None:
        self.evidence[name] = value
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.evidence, indent=2) + "\n")
        print(f"PASS {name}", flush=True)

    async def submit(self, name: str, *, kind: str = "job", **kwargs: Any) -> dict[str, Any]:
        body = {
            "kind": kind,
            "context": self.context(),
            "operation_id": f"{name}-{STAMP}",
            **kwargs,
        }
        if kind == "job":
            body.update(
                purpose=f"Lab20 {name}",
                expected_progress="The acceptance check asserts its output.",
            )
        return await self.api("POST", "/v1/executions", body)

    async def finish(self, job: dict[str, Any]) -> dict[str, Any]:
        async with asyncio.timeout(60):
            while job["state"] not in TERMINAL:
                await asyncio.sleep(0.1)
                job = await self.api("GET", f"/v1/executions/{job['execution_id']}")
        return job

    async def live(self, job: dict[str, Any], marker: str) -> dict[str, Any]:
        async with asyncio.timeout(30):
            while True:
                value = await self.api("GET", f"/v1/jobs/{job['execution_id']}")
                if marker in (value.get("stdout") or {}).get("text", ""):
                    assert value["execution"]["state"] == "running", value
                    return value
                await asyncio.sleep(0.1)

    async def stop(self) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["/usr/bin/systemctl", "--user", "stop", "openshell-lab-worker"],
            check=True,
        )

    async def start(self) -> None:
        await asyncio.to_thread(
            subprocess.run,
            [
                "/usr/bin/systemd-run",
                "--user",
                "--unit=openshell-lab-worker",
                "--collect",
                "-q",
                f"--working-directory={LAB / 'worker'}",
                str(ROOT / ".venv/bin/sandbox-science-worker"),
                "serve",
                "--settings",
                "worker.toml",
                "--log-level",
                "info",
            ],
            check=True,
        )
        await self.ready()

    async def file(self, path: str) -> bytes:
        chunks: list[bytes] = []
        offset = 0
        while True:
            value = await self.api(
                "POST",
                "/v1/files/read",
                {"context": self.context(), "path": path, "offset": offset, "length": 1024 * 1024},
            )
            data = base64.b64decode(value["content_b64"])
            chunks.append(data)
            offset += len(data)
            if len(data) < 1024 * 1024:
                return b"".join(chunks)

    async def events(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        async with self.http.get(URL + f"/v1/executions/{job['execution_id']}/events") as r:
            assert r.status == 200
            async for line in r.content:
                if line.strip():
                    values.append(json.loads(line))
        assert [event["seq"] for event in values] == list(range(1, job["last_seq"] + 1))
        assert sum(event["type"] == "result" for event in values) == 1
        return values

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
        warm = await self.finish(await self.submit("warm", kind="shell", script="printf ready"))
        assert warm["state"] == "succeeded", warm
        self.save("environment", {"environment": value, "warm": warm})

    async def running(self) -> None:
        path = f"attempts-{STAMP}.txt"
        script = f"printf '%s\\n' $$ >> {path}; printf 'before\\n'; sleep 12; printf 'after\\n'"
        job = await self.submit("running", script=script)
        before = await self.live(job, "before")
        await self.stop()
        await self.start()
        replay = await self.submit("running", script=script)
        assert replay["execution_id"] == job["execution_id"]
        done = await self.finish(replay)
        assert done["state"] == "succeeded", done
        assert done["result"]["stdout"]["text"] == "before\nafter\n", done
        attempts = (await self.file(path)).decode().splitlines()
        assert len(attempts) == 1, attempts
        self.save(
            "running_across_restart",
            {
                "before": before,
                "job": done,
                "attempt_pids": attempts,
                "events": await self.events(done),
            },
        )

    async def offline(self) -> None:
        job = await self.submit(
            "offline", script="printf 'before offline\\n'; sleep 4; printf 'finished offline\\n'"
        )
        await self.live(job, "before offline")
        await self.stop()
        await asyncio.sleep(6)
        await self.start()
        done = await self.finish(job)
        assert done["state"] == "succeeded", done
        assert done["result"]["stdout"]["text"] == "before offline\nfinished offline\n", done
        self.save("completion_while_offline", {"job": done, "events": await self.events(done)})

    async def normal(self) -> None:
        done = await self.finish(
            await self.submit(
                "normal",
                script="python -c \"import sys; sys.stdout.write('z'*600000)\"",
                output_limit_bytes=1024 * 1024,
            )
        )
        assert done["state"] == "succeeded", done
        output = done["result"]["stdout"]
        assert output["text"] == "z" * 600000, len(output["text"])
        assert not output["truncated"], output["truncated"]
        self.save(
            "connected_result_retains_requested_budget",
            {
                "execution_id": done["execution_id"],
                "state": done["state"],
                "inline_bytes": len(output["text"]),
                "truncated": output["truncated"],
            },
        )

    async def refused(self) -> None:
        done = await self.finish(
            await self.submit(
                "refused", kind="shell", script="printf unreachable", cwd="missing-lab20-directory"
            )
        )
        assert done["state"] == "failed", done
        self.save("definite_refusal_ends", done)

    async def cancelled(self) -> None:
        job = await self.submit("cancelled", script="printf 'before cancel\\n'; sleep 60")
        await self.live(job, "before cancel")
        await self.stop()
        await self.start()
        await self.api("POST", f"/v1/executions/{job['execution_id']}/cancel", {})
        done = await self.finish(job)
        assert done["state"] == "cancelled", done
        self.save("cancel_after_reconnect", {"job": done, "events": await self.events(done)})

    async def kernel(self) -> None:
        first = await self.finish(
            await self.submit(
                "kernel-before", kind="cell", code="lab20_value = 17\nprint(lab20_value)"
            )
        )
        assert first["state"] == "succeeded", first
        await self.stop()
        await self.start()
        second = await self.finish(
            await self.submit("kernel-after", kind="cell", code="print(lab20_value + 1)")
        )
        assert second["state"] == "succeeded", second
        assert "18" in second["result"]["stdout"]["text"], second
        self.save("kernel_survives_restart", {"first": first, "second": second})

    async def noisy(self) -> None:
        script = "python -u -c \"import sys,time; print('before noise',flush=True); time.sleep(3); sys.stdout.write('x'*3000000); print('tail',flush=True)\""
        job = await self.submit("noisy", script=script, output_limit_bytes=1024)
        await self.live(job, "before noise")
        await self.stop()
        await asyncio.sleep(8)
        await self.start()
        done = await self.finish(job)
        assert done["state"] == "succeeded", done
        output = await self.file(f".jobs/{job['execution_id']}/stdout.log")
        expected = b"before noise\n" + b"x" * 3000000 + b"tail\n"
        assert output == expected, len(output)
        inline = done["result"]["stdout"]
        assert inline["truncated"], inline
        assert "before noise" in inline["text"], inline
        assert "tail" in inline["text"], inline
        self.save(
            "noisy_completion_while_offline",
            {
                "job": done,
                "log_bytes": len(output),
                "log_sha256": hashlib.sha256(output).hexdigest(),
                "event_count": len(await self.events(done)),
            },
        )

    async def timeout(self) -> None:
        job = await self.submit(
            "timeout", script="printf 'before timeout\\n'; sleep 30", timeout_seconds=3
        )
        await self.live(job, "before timeout")
        await self.stop()
        await asyncio.sleep(5)
        await self.start()
        done = await self.finish(job)
        assert done["state"] == "timed_out", done
        assert "before timeout" in done["result"]["stdout"]["text"], done
        self.save("timeout_while_offline", done)


async def main() -> None:
    parts = sys.argv[1:] or [
        "envs",
        "running",
        "offline",
        "normal",
        "refused",
        "cancelled",
        "kernel",
        "noisy",
        "timeout",
    ]
    if set(parts) - {
        "envs",
        "running",
        "offline",
        "normal",
        "refused",
        "cancelled",
        "kernel",
        "noisy",
        "timeout",
    }:
        raise SystemExit("Unknown check; see this file's usage.")
    async with aiohttp.ClientSession(
        headers={"Authorization": "Bearer " + (LAB / "worker/client-a.token").read_text().strip()},
        timeout=aiohttp.ClientTimeout(total=180),
    ) as http:
        checks = Checks(http)
        await checks.ready()
        for part in parts:
            await getattr(checks, part)()


if __name__ == "__main__":
    asyncio.run(main())
