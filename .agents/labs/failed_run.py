# SPDX-License-Identifier: MIT
"""Disposable CLI/browser acceptance: synthetic HTTP peers, no research execution."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from aiohttp import web

from sandbox_science.orchestrator.projects import ProjectStore

PROJECT = "failed-run-demo"
SESSION = "a"
MARKER = "synthetic-failed-run-browser-v1\n"
MODEL_KEY = "test-secret-failed-run-model-not-a-credential"
WORKER_KEY = "test-secret-failed-run-worker-not-a-credential"
HOSTILE = "synthetic-provider-body-must-not-be-retained"
CONTENT = b"Synthetic retained observation: 6 * 7 = 42.\n"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


async def create() -> None:
    root = Path(tempfile.mkdtemp(prefix="failed-run-browser-"))
    (root / ".failed-run-fixture").write_text(MARKER)
    projects = ProjectStore(root)
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    for name, value in (("model-key", MODEL_KEY), ("worker-key", WORKER_KEY)):
        path = root / name
        path.write_text(value)
        path.chmod(0o600)
    counts = {"model": 0, "file": 0}

    async def peer(request: web.Request) -> web.Response:
        model = request.path == "/chat/completions"
        expected = MODEL_KEY if model else WORKER_KEY
        if request.headers.get("Authorization") != f"Bearer {expected}":
            raise web.HTTPUnauthorized()
        if model:
            counts["model"] += 1
            body = await request.json()
            if counts["model"] == 1:
                return web.json_response(
                    {
                        "model": "synthetic-main",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "I will inspect the synthetic observation.",
                                    "tool_calls": [
                                        {
                                            "id": "read-observation",
                                            "type": "function",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": '{"purpose_note":"Reading the synthetic observation","path":"observation.txt"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ],
                    }
                )
            require(counts["model"] == 2, "Unexpected model call count")
            require(
                any(
                    message.get("role") == "tool" and "6 * 7 = 42" in message["content"]
                    for message in body["messages"]
                ),
                "The completed tool result did not reach the second model call",
            )
            return web.Response(
                status=429, text=f"{HOSTILE} {MODEL_KEY} {WORKER_KEY} <script>unsafe</script>"
            )
        if request.path.startswith("/v1/environments/"):
            return web.json_response(
                {"error": {"kind": "unsupported", "message": "Synthetic unmanaged peer"}},
                status=400,
            )
        if request.path == "/v1/capabilities":
            return web.json_response(
                {
                    "protocol": 1,
                    "languages": [],
                    "location": {
                        "pool_id": "synthetic-peer",
                        "backend": "local",
                        "hostname": "synthetic-no-execution",
                        "isolation_tier": 0,
                        "declared_isolation_tier": 0,
                    },
                }
            )
        if request.path == "/v1/jobs":
            return web.json_response({"jobs": []})
        if request.path == "/v1/files/read":
            counts["file"] += 1
            body = await request.json()
            require(body["path"] == "observation.txt", "Unexpected synthetic file read")
            return web.json_response(
                {
                    "path": body["path"],
                    "sha256": hashlib.sha256(CONTENT).hexdigest(),
                    "size": len(CONTENT),
                    "offset": 0,
                    "content_b64": base64.b64encode(CONTENT).decode(),
                    "eof": True,
                }
            )
        raise web.HTTPNotFound()

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", peer)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    process: asyncio.subprocess.Process | None = None
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        url = f"http://127.0.0.1:{port}"
        transcript_path = root / "cli-transcript.json"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "from sandbox_science.orchestrator.cli import main; raise SystemExit(main())",
            "run", "--worker", url, "--token-file", str(root / "worker-key"),
            "--project", PROJECT, "--session", SESSION, "--state-dir", str(root),
            "--model-url", url, "--api-key-file", str(root / "model-key"),
            "--model", "synthetic-main", "--allow-unisolated", "--run-id", "partial-error",
            "--transcript", str(transcript_path),
            "Inspect the synthetic observation; preserve partial work if the model fails.",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    finally:
        try:
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        finally:
            await runner.cleanup()
    if process is None:
        raise ValueError("CLI process was not created")
    require(process.returncode == 1, "The failed CLI must exit 1")
    require(counts == {"model": 2, "file": 1}, "Unexpected protocol call counts")
    entries = projects.runs(PROJECT, SESSION)
    require(len(entries) == 1, "Exactly one failed run must be retained")
    record = projects.run(PROJECT, SESSION, entries[0].name)
    require(record.stopped == "error" and record.failure == "model", "Wrong failure phase")
    require(record.final_text is None and len(record.tools) == 1, "Wrong partial tool record")
    require(len(record.messages) == 4, "Wrong partial message count")
    require("6 * 7 = 42" in str(record.messages[-1]["content"]), "Missing tool observation")
    require(
        json.loads(transcript_path.read_text()) == record.model_dump(mode="json"),
        "CLI transcript and stored run differ",
    )
    exported = stdout + stderr + transcript_path.read_bytes() + record.to_json().encode()
    for forbidden in (MODEL_KEY, WORKER_KEY, HOSTILE, "<script>unsafe</script>"):
        require(forbidden.encode() not in exported, "Unsafe provider data reached output")
    (root / "cli-stdout.txt").write_bytes(stdout)
    (root / "cli-stderr.txt").write_bytes(stderr)
    summary = {
        "fixture": "synthetic HTTP protocol peers; no research execution or real model",
        "project": PROJECT,
        "session": SESSION,
        "run": entries[0].name,
        "exit_code": process.returncode,
        "counts": counts,
        "stopped": record.stopped,
        "failure": record.failure,
        "messages": len(record.messages),
        "completed_tools": len(record.tools),
        "final_text": record.final_text,
        "hostile_body_and_synthetic_keys_absent": True,
        "peers_stopped": True,
    }
    (root / "acceptance.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"state_dir": str(root), **summary}, indent=2))


if __name__ == "__main__":
    asyncio.run(create())
