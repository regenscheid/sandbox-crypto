# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Marked Lab22 capture acceptance. Root starts/stops the disposable lab separately.

Require PROJECT=lab22 and an explicitly configured 1 MiB max_artifact_size.
Usage: .venv/bin/python -I .agents/labs/artifact_capture.py init prepare verify
This script starts no service and reads only its disposable lab's synthetic token.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import time
import tomllib
from pathlib import Path
from typing import Any

import aiohttp

LAB = Path.home() / ".local/state/openshell-lab"
PROJECT, ENVIRONMENT = "lab22", "lab22-capture"
URL = "http://127.0.0.1:8100"
LIMIT = 1 << 20
MARKER = LAB / "artifact-capture.created"
EVIDENCE = LAB / "evidence/artifact-capture.json"
STAMP = str(time.time_ns())
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "lost"}
EXACT = b'Lab22 exact\r\n\x00\xff\\quoted"\n'


def marked(*, create: bool = False) -> None:
    """Refuse ordinary project state and an unconfigured publication byte limit."""
    settings = tomllib.loads((LAB / "worker/worker.toml").read_text())
    assert settings.get("max_artifact_size") == LIMIT
    assert any(PROJECT in item["projects"] for item in settings["clients"])
    expected = {"project": PROJECT, "purpose": "disposable bounded artifact capture acceptance"}
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
        print(f"PASS {name}", flush=True)

    def context(self) -> dict[str, str]:
        return {"project_id": PROJECT, "session_id": "capture", "environment_id": ENVIRONMENT}

    async def api(
        self, method: str, path: str, body: Any = None, *, status: int = 200
    ) -> dict[str, Any]:
        marked()
        async with self.http.request(method, URL + path, json=body) as response:
            value = await response.json()
            assert response.status == status, (method, path, response.status, value)
            return value

    async def shell(self, name: str, script: str) -> dict[str, Any]:
        execution = await self.api(
            "POST",
            "/v1/executions",
            {
                "kind": "shell",
                "operation_id": f"lab22-{name}-{STAMP}",
                "context": self.context(),
                "script": script,
                "timeout_seconds": 60,
            },
            status=201,
        )
        async with asyncio.timeout(75):
            while execution["state"] not in TERMINAL:
                await asyncio.sleep(0.1)
                execution = await self.api("GET", f"/v1/executions/{execution['execution_id']}")
        assert execution["state"] == "succeeded", execution
        return execution

    async def publish(self, path: str, *, status: int = 201) -> dict[str, Any]:
        return await self.api(
            "POST", "/v1/artifacts", {"context": self.context(), "path": path}, status=status
        )

    async def content(self, artifact: dict[str, Any], exact: bytes) -> None:
        async with self.http.get(
            URL + f"/v1/artifacts/{artifact['artifact_id']}/content",
            params={"project_id": PROJECT},
        ) as response:
            assert response.status == 200
            body = await response.read()
            assert body == exact
            assert response.headers["X-Content-SHA256"] == hashlib.sha256(body).hexdigest()
        assert artifact["size"] == len(exact)
        assert artifact["sha256"] == hashlib.sha256(exact).hexdigest()

    async def prepare(self) -> None:
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
        self.save("environment", environment)
        prepared = await self.shell(
            "sources",
            "/opt/sandbox-science/bin/python -I -S - <<'PY'\n"
            "from pathlib import Path\nimport base64, os, sys\n"
            f"Path('exact.bin').write_bytes(base64.b64decode({base64.b64encode(EXACT).decode()!r}))\n"
            f"Path('regular-too-large').write_bytes(b'x' * {LIMIT + 1})\n"
            "with Path('sparse-too-large').open('wb') as out: out.truncate(1 << 30)\n"
            "os.symlink('exact.bin', 'link.bin')\nos.mkfifo('pipe')\n"
            "print(sys.version)\nPY",
        )
        self.save("sources", prepared)

    async def verify(self) -> None:
        original = await self.publish("exact.bin")
        await self.content(original, EXACT)
        self.save("exact", original)
        empty = await self.shell("empty", ": > empty")
        self.save("empty-execution", empty)
        empty_ref = await self.publish("empty")
        await self.content(empty_ref, b"")
        self.save("empty", empty_ref)
        refusals = []
        for _ in range(3):
            for path in ("regular-too-large", "sparse-too-large", "link.bin", "pipe"):
                started = time.monotonic()
                async with asyncio.timeout(10):
                    refused = await self.publish(path, status=400)
                assert refused["error"]["kind"] == "invalid_request", refused
                refusals.append(
                    {"path": path, "seconds": time.monotonic() - started, "outcome": refused}
                )
            # Repeated failures must not poison the bounded helper/publication slots.
            healthy = await self.publish("exact.bin")
            await self.content(healthy, EXACT)
        self.save("refusals-and-repeated-success", refusals)
        await self.content(original, EXACT)
        healthy_execution = await self.shell("healthy", "printf 'healthy after refusals\\n'")
        self.save("healthy-execution", healthy_execution)
        read = await self.api(
            "POST", "/v1/files/read", {"context": self.context(), "path": "exact.bin"}
        )
        assert base64.b64decode(read["content_b64"]) == EXACT
        self.save("ordinary-read", read)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parts", nargs="+", choices=("init", "prepare", "verify"))
    args = parser.parse_args()
    if "init" in args.parts:
        assert args.parts[0] == "init"
        marked(create=True)
    else:
        marked()
    token = (LAB / "worker/client-a.token").read_text().strip()
    async with aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {token}"}, timeout=aiohttp.ClientTimeout(total=90)
    ) as http:
        checks = Checks(http)
        for part in args.parts:
            if part == "prepare":
                await checks.prepare()
            elif part == "verify":
                await checks.verify()


if __name__ == "__main__":
    asyncio.run(main())
