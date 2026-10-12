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
import os
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

    def context(self, environment: str = ENVIRONMENT) -> dict[str, str]:
        return {"project_id": PROJECT, "session_id": "main", "environment_id": environment}

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

    async def submit(
        self, name: str, *, kind: str = "job", environment: str = ENVIRONMENT, **kwargs: Any
    ) -> dict[str, Any]:
        body = {
            "kind": kind,
            "context": self.context(environment),
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

    async def operator(self, *command: str, sandbox: str | None = None) -> str:
        """Use the lab gateway directly for failure injection in its disposable VM."""
        if sandbox is None:
            sandbox = (await self.api("GET", f"/v1/environments/{ENVIRONMENT}"))["sandbox"]
        return await self.gateway(
            "sandbox", "exec", sandbox, "--no-tty", "--no-login-shell", "--", *command
        )

    async def gateway(self, *arguments: str) -> str:
        """Run the operator CLI only against this disposable gateway."""
        env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(LAB / "config"),
            "XDG_STATE_HOME": str(LAB / "state"),
            "XDG_DATA_HOME": str(LAB / "data"),
            "OPENSHELL_LOCAL_TLS_DIR": str(LAB / "state/openshell/tls"),
            "OPENSHELL_TELEMETRY_ENABLED": "false",
        }
        done = await asyncio.to_thread(
            subprocess.run,
            [str(Path.home() / ".local/opt/openshell-0.1.3/bin/openshell"), *arguments],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

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

    async def daemon_loss(self) -> None:
        before_environment = await self.api("GET", f"/v1/environments/{ENVIRONMENT}")
        marker = f"lab20-preserved-{STAMP}"
        prepare = """import pathlib,sysconfig
pathlib.Path('daemon-recycle-workspace.txt').write_text(MARKER)
prefix=pathlib.Path(sysconfig.get_path('purelib'))/'lab20_recycle_marker.py'
prefix.write_text('VALUE = '+repr(MARKER)+'\\n')
print(MARKER)
""".replace("MARKER", repr(marker))
        prepared = await self.finish(
            await self.submit(
                "prepare-daemon-loss",
                kind="shell",
                argv=["/opt/sandbox-science/bin/python", "-I", "-c", prepare],
            )
        )
        assert prepared["state"] == "succeeded", prepared
        kernel = await self.finish(
            await self.submit("kernel-before-loss", kind="cell", code="lab20_lost_kernel = 99")
        )
        assert kernel["state"] == "succeeded", kernel
        child_file = f"daemon-loss-child-{STAMP}.pid"
        script = (
            'python -u -c "import subprocess,time,pathlib; '
            "child=subprocess.Popen(['setsid','sleep','180']); "
            f"pathlib.Path('{child_file}').write_text(str(child.pid)); "
            "print('escaped child ready',flush=True); time.sleep(180)\""
        )
        job = await self.submit("daemon-loss", script=script)
        await self.live(job, "escaped child ready")
        child = int(await self.file(child_file))
        old_child = json.loads(
            await self.operator(
                "/opt/sandbox-science/bin/python",
                "-I",
                "-c",
                "import json,pathlib; "
                "print(json.dumps({'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),"
                "'start':pathlib.Path('/proc/"
                + str(child)
                + "/stat').read_text().rsplit(')',1)[1].split()[19]}))",
            )
        )
        other_job = await self.submit(
            "other-daemon-loss", script="printf 'second job ready\\n'; sleep 180"
        )
        await self.live(other_job, "second job ready")
        # Only inspect command arguments and process identity: bootstrap tokens are
        # supplied on stdin and are never read or included in the evidence.
        kill = """import json,os,pathlib,signal
found=[]
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
    try:
        words=path.read_bytes().split(b'\\0')
        if any(word.endswith(b'/daemon.py') for word in words) and b'--serve' in words:
            pid=int(path.parent.name)
            stamp=(path.parent/'stat').read_text().rsplit(')',1)[1].split()[19]
            found.append((pid,stamp))
    except (OSError,ValueError):
        pass
assert len(found)==1,found
pid,stamp=found[0]
assert pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]==stamp
os.kill(pid,signal.SIGKILL)
print(json.dumps({'daemon_pid':pid,'start_time':stamp}))
"""
        killed = json.loads(
            await self.operator("/opt/sandbox-science/bin/python", "-I", "-c", kill)
        )
        done = await self.finish(job)
        assert done["state"] == "lost", done
        other_done = await self.finish(other_job)
        assert other_done["state"] == "lost", other_done
        fresh = await self.finish(
            await self.submit("after-loss", kind="shell", script="printf fresh")
        )
        assert fresh["state"] == "succeeded", fresh
        after_environment = await self.api("GET", f"/v1/environments/{ENVIRONMENT}")
        assert before_environment["sandbox_id"] == after_environment["sandbox_id"]
        assert before_environment["sandbox"] == after_environment["sandbox"]
        preserved = await self.finish(
            await self.submit(
                "preserved-after-loss",
                kind="shell",
                argv=[
                    "/opt/sandbox-science/bin/python",
                    "-I",
                    "-c",
                    "import pathlib,lab20_recycle_marker; "
                    "assert lab20_recycle_marker.VALUE == " + repr(marker) + "; "
                    "assert pathlib.Path('daemon-recycle-workspace.txt').read_text() == "
                    + repr(marker)
                    + "; print('workspace and Python prefix preserved')",
                ],
            )
        )
        assert preserved["state"] == "succeeded", preserved
        lost_kernel = await self.finish(
            await self.submit("kernel-after-loss", kind="cell", code="print(lab20_lost_kernel)")
        )
        assert lost_kernel["state"] == "failed", lost_kernel
        assert "NameError" in json.dumps(lost_kernel), lost_kernel
        self.save(
            "daemon_loss_before_child_check",
            {"killed": killed, "child_pid": child, "job": done, "fresh": fresh},
        )
        observed = json.loads(
            await self.operator(
                "/opt/sandbox-science/bin/python",
                "-I",
                "-c",
                "import json,pathlib; "
                "p=pathlib.Path('/proc/" + str(child) + "/stat'); "
                "stat=p.read_text().rsplit(')',1)[1].split() if p.exists() else None; "
                "print(json.dumps({'boot':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),"
                "'start':stat[19] if stat else None,'state':stat[0] if stat else None}))",
            )
        )
        assert (
            observed["boot"] != old_child["boot"]
            or observed["start"] != old_child["start"]
            or observed["state"] in {None, "Z", "X"}
        ), observed
        self.save(
            "daemon_loss_reaps_escaped_child",
            {
                "killed": killed,
                "child_pid": child,
                "job": done,
                "other_job": other_done,
                "fresh": fresh,
                "old_child": old_child,
                "child_after_recovery": observed,
                "environment_before": before_environment,
                "environment_after": after_environment,
                "preserved_files": preserved,
                "kernel_after_loss": lost_kernel,
            },
        )

    async def sandbox_loss(self) -> None:
        healthy_id = "lab20-b"
        healthy = await self.api(
            "POST",
            "/v1/environments",
            {
                "environment_id": healthy_id,
                "project_id": PROJECT,
                "profile": "minimal",
                "policy": (LAB / "policy.digest").read_text().strip(),
            },
        )
        first = await self.finish(
            await self.submit(
                "healthy-before-loss",
                kind="cell",
                environment=healthy_id,
                code="healthy_kernel = 27; print(healthy_kernel)",
            )
        )
        assert first["state"] == "succeeded", first
        removed = await self.api("GET", f"/v1/environments/{ENVIRONMENT}")
        job = await self.submit("sandbox-loss", script="printf 'before sandbox loss\\n'; sleep 180")
        await self.live(job, "before sandbox loss")
        await self.stop()
        # Delete this exact disposable VM, never an aggregate selector.
        assert removed["environment_id"] == "lab20-a"
        deleted = await self.gateway("sandbox", "delete", removed["sandbox"])
        await self.start()
        done = await self.finish(job)
        assert done["state"] == "lost", done
        second = await self.finish(
            await self.submit(
                "healthy-after-loss",
                kind="cell",
                environment=healthy_id,
                code="print(healthy_kernel + 1)",
            )
        )
        assert second["state"] == "succeeded", second
        assert "28" in second["result"]["stdout"]["text"], second
        after = await self.api("GET", f"/v1/environments/{healthy_id}")
        assert healthy["sandbox_id"] == after["sandbox_id"], after
        self.save(
            "confirmed_sandbox_loss_preserves_other_environment",
            {
                "removed_environment": removed,
                "delete": deleted,
                "job": done,
                "events": await self.events(done),
                "healthy_before": healthy,
                "healthy_after": after,
                "first_cell": first,
                "second_cell": second,
            },
        )


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
        "daemon_loss",
        "sandbox_loss",
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
        "daemon_loss",
        "sandbox_loss",
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
