# SPDX-License-Identifier: MIT
"""Disposable actual-workspace clipboard fixture; synthetic repair peer only."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import socket
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app
from sandbox_science.workspace.mathrepair import gateway_service

PROJECT = "math-copy-demo"
KEY = "test-secret-math-copy-peer-not-a-credential"
TEXT = r"""# Formula copying acceptance

Inline: $x + y$.

$$
\frac{1}{2}
$$

Before $a^2$ middle $\beta$ after.

Heuristic: $\bm{x}$.

Model: $\frac{1$.

Fallback: $\begin{pmatrix} 1 & 2$.

Skipped: $\def\x{y}$.

> $$
> x + y
> $$

| Name | Formula | Tail |
| --- | --- | --- |
| first | $a^2$ | last |
| second | $\beta$ | end |

Ordinary code: `$not_math$`.

```tex
$also_not_math$
```
"""


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--static", type=Path, required=True)
    parser.add_argument("--info", type=Path, required=True)
    parser.add_argument("--cleanup", type=Path, required=True)
    args = parser.parse_args()
    stopped = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stopped.set)
    runners: list[web.AppRunner] = []
    ports: list[int] = []
    calls = 0
    owned_info = False
    with tempfile.TemporaryDirectory(prefix="math-copy-browser-") as directory:
        fixture = Path(directory)
        (fixture / ".math-copy-fixture").write_text("synthetic-math-copy-browser-v1\n")
        projects = ProjectStore(fixture)
        projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        projects.open_session(PROJECT, "a", dedicated=False)
        documents = DocumentStore(store_path(fixture), producer="browser-fixture")
        try:
            document = documents.create(
                PROJECT, name="copying", title="Formula copying acceptance", text=TEXT, author=USER
            ).document_id
        finally:
            documents.close()

        async def repair(request: web.Request) -> web.Response:
            nonlocal calls
            if request.headers.get("Authorization") != f"Bearer {KEY}":
                raise web.HTTPUnauthorized()
            body = json.dumps(await request.json())
            calls += 1
            answer = r"\begin{pmatrix} 1 & 2" if "pmatrix" in body else "x^2"
            return web.json_response(
                {"choices": [{"message": {"role": "assistant", "content": answer}}]}
            )

        async def start(app: web.Application) -> int:
            runner = web.AppRunner(app, access_log=None)
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            port = int(runner.addresses[0][1])
            ports.append(port)
            return port

        try:
            peer = web.Application()
            peer.router.add_post("/chat/completions", repair)
            peer_port = await start(peer)
            login = secrets.token_urlsafe(32)
            app = create_app(
                projects,
                args.static,
                login_token=login,
                math_repair=gateway_service(
                    f"http://127.0.0.1:{peer_port}", "synthetic-copy-reviewer", KEY
                ),
            )
            port = await start(app)
            info = {
                "fixture": directory,
                "project": PROJECT,
                "document": document,
                "port": port,
                "peer_port": peer_port,
                "login_url": f"http://127.0.0.1:{port}/login?token={login}",
            }
            descriptor = os.open(args.info, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            owned_info = True
            with os.fdopen(descriptor, "w") as private:
                private.write(json.dumps(info) + "\n")
            print(json.dumps({k: v for k, v in info.items() if k != "login_url"}), flush=True)
            await stopped.wait()
        finally:
            cleanup = await asyncio.gather(
                *(runner.cleanup() for runner in reversed(runners)), return_exceptions=True
            )
            if owned_info:
                args.info.unlink(missing_ok=True)
            for result in cleanup:
                if isinstance(result, BaseException):
                    raise result
    for port in ports:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("Owned fixture port is still listening")
    with args.cleanup.open("x") as record:
        record.write(
            json.dumps(
                {
                    "checked_utc": datetime.now(UTC).isoformat(),
                    "runners_closed": len(runners),
                    "ports_free": ports,
                    "fixture_removed": not fixture.exists(),
                    "private_info_removed": not args.info.exists(),
                    "synthetic_repair_requests": calls,
                },
                indent=2,
            )
            + "\n"
        )


if __name__ == "__main__":
    asyncio.run(main())
