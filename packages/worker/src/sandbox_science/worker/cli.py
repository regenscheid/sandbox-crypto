# SPDX-License-Identifier: MIT
"""Command line: ``sandbox-science-worker serve --settings worker.toml``.

```
sandbox-science-worker serve --settings worker.toml [--log-level info]
sandbox-science-worker token --out worker.token
sandbox-science-worker policy add --settings worker.toml policy.yaml
```

``policy add`` is the operator's way to put a project policy revision into the pool's
policy store, where environments can then use it (D73); the worker API cannot.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import signal
import sys
from pathlib import Path

from aiohttp import web

from sandbox_science.policy.openshell import CompileError, compile_sandbox
from sandbox_science.policy_middleware.errors import StoreError

from .app import create_app
from .artifacts import Blobs
from .backends import Backend, LocalBackend, OpenShellBackend
from .environments import Environments
from .errors import WorkerError
from .manager import ExecutionManager
from .package_clients import cache_endpoint
from .settings import Settings, SettingsError, load_settings
from .store import Store

_log = logging.getLogger("sandbox_science.worker")


def _executor_args(settings: Settings) -> list[str]:
    """Options for every executor, from the worker's settings."""
    if settings.kernel_idle_timeout is None:
        return []
    return ["--kernel-idle", repr(settings.kernel_idle_timeout)]


def _environments(settings: Settings) -> Environments | None:
    openshell = settings.openshell
    if openshell is None or openshell.environments is None:
        return None
    cli_env = OpenShellBackend(openshell, settings.state_dir).cli_env()
    environments = Environments(openshell, openshell.environments, settings.state_dir, cli_env)
    target = environments.targets.sandbox.package_cache
    if openshell.package_cache is not None and cache_endpoint(openshell.package_cache) != (
        target.host,
        target.port,
    ):
        environments.close()
        # The policies compiled for these sandboxes admit the cache only there.
        raise SettingsError(
            f"package_cache {openshell.package_cache} is not where the targets file puts "
            f"the package cache ({target.host}:{target.port})"
        )
    return environments


def _backend(settings: Settings, environments: Environments | None) -> Backend:
    if settings.openshell is not None:
        resolve = environments.sandbox if environments is not None else None
        return OpenShellBackend(
            settings.openshell, settings.state_dir, resolve, executor_args=_executor_args(settings)
        )
    if settings.local is None:
        raise SettingsError("no backend is configured")
    _log.warning("the local backend runs research code without any isolation (tier 0)")
    return LocalBackend(settings.local, settings.state_dir, _executor_args(settings))


async def serve(settings: Settings) -> None:
    token = settings.token()
    environments = _environments(settings)
    store = Store(settings.state_dir / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        _backend(settings, environments),
        Blobs(settings.state_dir / "artifacts"),
        pool_id=settings.pool_id,
        max_artifact_size=settings.max_artifact_size,
        state_dir=settings.state_dir,
    )
    if environments is not None:
        environments.release = manager.release
    # SIGTERM (a systemd stop) and SIGINT shut down cleanly: executors stop, and
    # attempts still running are stored as lost.
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    runner = web.AppRunner(create_app(manager, token, environments), access_log=None)
    try:
        await manager.start()
        await runner.setup()
        site = web.TCPSite(runner, settings.listen_host, settings.listen_port)
        await site.start()
        _log.info("worker API on %s (%s backend)", settings.listen, manager.backend.name)
        await stopping.wait()
        _log.info("stopping")
    finally:
        await runner.cleanup()
        await manager.close()
        store.close()
        if environments is not None:
            environments.close()


def add_policies(settings: Settings, paths: list[Path]) -> int:
    """Store project policy revisions for environments, after compiling each to check it."""
    openshell = settings.openshell
    if openshell is None or openshell.environments is None:
        print("error: the settings have no [openshell.environments] table", file=sys.stderr)
        return 1
    try:
        environments = Environments(openshell, openshell.environments, settings.state_dir, {})
    except WorkerError as error:
        print(f"error: {error.message}", file=sys.stderr)
        return 1
    status = 0
    try:
        for path in paths:
            try:
                loaded = environments.store.add_policy(path.read_bytes())
                compiled = compile_sandbox(
                    loaded, environments.targets.sandbox, ceiling=environments.ceiling
                )
            except (OSError, StoreError, CompileError) as exc:
                print(f"error: {path}: {exc}", file=sys.stderr)
                status = 1
                continue
            print(f"stored {path} as {loaded.digest}")
            for finding in compiled.findings:
                print(f"  {finding.severity}: {finding.subject}: {finding.message}")
    finally:
        environments.close()
    return status


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
    policy_parser = commands.add_parser("policy", help="manage the pool's policy store")
    policy_commands = policy_parser.add_subparsers(dest="policy_command", required=True)
    add_parser = policy_commands.add_parser("add", help="store project policy revisions")
    add_parser.add_argument("--settings", type=Path, required=True)
    add_parser.add_argument("policies", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.command == "token":
        descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
        return 0
    if args.command == "policy":
        try:
            return add_policies(load_settings(args.settings), args.policies)
        except SettingsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
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
    except KeyboardInterrupt:  # before the signal handlers are in place
        return 0
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
