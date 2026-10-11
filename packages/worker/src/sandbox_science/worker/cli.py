# SPDX-License-Identifier: MIT
"""Command line: ``sandbox-science-worker serve --settings worker.toml``.

```
sandbox-science-worker serve --settings worker.toml [--log-level info]
sandbox-science-worker token --out worker.token
```
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

from aiohttp import web

from .app import create_app
from .artifacts import Blobs
from .backends import Backend, LocalBackend, OpenShellBackend
from .manager import ExecutionManager
from .settings import Settings, SettingsError, load_settings
from .store import Store

_log = logging.getLogger("sandbox_science.worker")


def _backend(settings: Settings) -> Backend:
    if settings.openshell is not None:
        return OpenShellBackend(settings.openshell)
    if settings.local is None:
        raise SettingsError("no backend is configured")
    _log.warning("the local backend runs research code without any isolation (tier 0)")
    return LocalBackend(settings.local, settings.state_dir)


async def serve(settings: Settings) -> None:
    token = settings.token()
    store = Store(settings.state_dir / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        _backend(settings),
        Blobs(settings.state_dir / "artifacts"),
        pool_id=settings.pool_id,
        max_artifact_size=settings.max_artifact_size,
    )
    await manager.start()
    runner = web.AppRunner(create_app(manager, token), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.listen_host, settings.listen_port)
    await site.start()
    _log.info("worker API on %s (%s backend)", settings.listen, manager.backend.name)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await manager.close()
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox-science-worker")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", help="run the worker API")
    serve_parser.add_argument("--settings", type=Path, required=True)
    serve_parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )
    token_parser = commands.add_parser("token", help="write a new random bearer token")
    token_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "token":
        descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
        return 0
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = load_settings(args.settings)
        settings.token()
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        return 0
    return 0
