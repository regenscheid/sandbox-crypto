# SPDX-License-Identifier: MIT
"""Command line: ``sandbox-science-worker serve --settings worker.toml``.

```
sandbox-science-worker serve --settings worker.toml [--log-level info]
sandbox-science-worker token --out worker.token     # prints the new token's SHA-256
sandbox-science-worker token --sha256 client.token  # the SHA-256 of an existing token
sandbox-science-worker policy add --settings worker.toml [--project p1] policy.yaml
```

``policy add`` is the operator's way to put a project policy revision into the pool's
policy store, where environments can then use it (D73); the worker API cannot. With
``--project``, clients limited to that project may use the revision too (D78).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import Identifier
from sandbox_science.policy.openshell import CompileError, compile_sandbox
from sandbox_science.policy_middleware.errors import StoreError

from .admission import DraftWatcher
from .app import create_app
from .artifacts import Blobs
from .backends import Backend, LocalBackend, OpenShellBackend
from .environments import EnvironmentRecords, Environments, printable
from .errors import WorkerError
from .manager import ExecutionManager
from .package_clients import cache_endpoint
from .settings import MIN_TOKEN, Settings, SettingsError, load_settings, token_digest
from .store import Store
from .suspend import Suspender

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
        return OpenShellBackend(
            settings.openshell,
            settings.state_dir,
            environments.sandbox if environments is not None else None,
            executor_args=_executor_args(settings),
            prepare=environments.resume if environments is not None else None,
            manages=environments.manages if environments is not None else None,
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
        environments.release_idle = manager.release_if_idle
        manager.notices = environments.notices
    # SIGTERM (a systemd stop) and SIGINT shut down cleanly: executors stop, and
    # attempts still running are stored as lost.
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    runner = web.AppRunner(
        create_app(manager, token, environments, settings.clients), access_log=None
    )
    background: list[asyncio.Task[None]] = []
    try:
        await manager.start()
        await runner.setup()
        site = web.TCPSite(runner, settings.listen_host, settings.listen_port)
        await site.start()
        _log.info("worker API on %s (%s backend)", settings.listen, manager.backend.name)
        if environments is not None and environments.settings.admission:
            interval = environments.settings.admission_interval
            background.append(asyncio.create_task(DraftWatcher(environments, interval).run()))
        if environments is not None and environments.settings.suspend_after is not None:
            suspender = Suspender(environments, manager, environments.settings.suspend_after)
            background.append(asyncio.create_task(suspender.run()))
        await stopping.wait()
        _log.info("stopping")
    finally:
        for task in background:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.exception("a background task failed")
        await runner.cleanup()
        await manager.close()
        store.close()
        if environments is not None:
            environments.close()


def add_policies(settings: Settings, paths: list[Path], projects: Sequence[str] = ()) -> int:
    """Store project policy revisions for environments, after compiling each to check it.

    Each revision is granted to ``projects``, whose limited clients may then use it.
    """
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
                    loaded,
                    environments.targets.sandbox,
                    ceiling=environments.ceiling,
                    floor=environments.floor,
                )
            except (OSError, StoreError, CompileError) as exc:
                print(f"error: {path}: {exc}", file=sys.stderr)
                status = 1
                continue
            print(f"stored {path} as {loaded.digest}")
            for project in projects:
                environments.records.grant(project, loaded.digest)
                print(f"  granted to project {project}")
            for finding in compiled.findings:
                print(f"  {finding.severity}: {finding.subject}: {finding.message}")
    finally:
        environments.close()
    return status


def show_admissions(settings: Settings, environment_id: str) -> int:
    """Print an environment's admission decisions (D69), oldest first."""
    if settings.openshell is None or settings.openshell.environments is None:
        print("error: the settings have no [openshell.environments] table", file=sys.stderr)
        return 1
    records = EnvironmentRecords(settings.state_dir / "environments.sqlite3")
    try:
        info = records.get(environment_id)
        if info is None:
            print(f"error: no environment {environment_id!r}", file=sys.stderr)
            return 1
        admitted = ", ".join(info.admitted_hosts) or "none"
        print(f"{environment_id} ({info.state}, sandbox {info.sandbox}): admitted {admitted}")
        for decision in records.admissions(environment_id):
            outcome = "admitted" if decision.admitted else "refused"
            print(
                f"  {decision.decided_at:%Y-%m-%d %H:%M:%S} {printable(decision.host)}:"
                f"{decision.port} {outcome}: {printable(decision.reason, 500)}"
            )
    finally:
        records.close()
    return 0


_IDENTIFIER = TypeAdapter[str](Identifier)


def _project(value: str) -> str:
    try:
        return _IDENTIFIER.validate_python(value)
    except ValidationError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a project ID") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox-science-worker")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", help="run the worker API")
    serve_parser.add_argument("--settings", type=Path, required=True)
    serve_parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning", "error"]
    )
    token_parser = commands.add_parser(
        "token", help="write a new random bearer token, or print a token's SHA-256"
    )
    which = token_parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--out", type=Path, help="write a new token here and print its SHA-256")
    which.add_argument(
        "--sha256", type=Path, metavar="FILE", help="print the SHA-256 of the token in FILE"
    )
    policy_parser = commands.add_parser("policy", help="manage the pool's policy store")
    policy_commands = policy_parser.add_subparsers(dest="policy_command", required=True)
    add_parser = policy_commands.add_parser("add", help="store project policy revisions")
    add_parser.add_argument("--settings", type=Path, required=True)
    add_parser.add_argument(
        "--project",
        dest="projects",
        type=_project,
        action="append",
        default=[],
        help="let clients limited to this project use the revisions (repeatable)",
    )
    add_parser.add_argument("policies", type=Path, nargs="+")
    admissions_parser = commands.add_parser(
        "admissions", help="show an environment's admission decisions (D69)"
    )
    admissions_parser.add_argument("--settings", type=Path, required=True)
    admissions_parser.add_argument("--environment", required=True)
    args = parser.parse_args(argv)
    if args.command == "token":
        if args.out is not None:
            token = secrets.token_urlsafe(32)
            descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(token + "\n")
        else:
            try:
                token = args.sha256.read_text().strip()
            except OSError as exc:
                print(f"error: cannot read {args.sha256}: {exc}", file=sys.stderr)
                return 1
            if len(token) < MIN_TOKEN:
                print(
                    f"error: the token in {args.sha256} is shorter than {MIN_TOKEN} characters",
                    file=sys.stderr,
                )
                return 1
        # The line a [[clients]] entry takes; the digest names the token without revealing it.
        print(f'token_sha256 = "{token_digest(token)}"')
        return 0
    if args.command in ("policy", "admissions"):
        try:
            settings = load_settings(args.settings)
        except SettingsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.command == "policy":
            return add_policies(settings, args.policies, args.projects)
        return show_admissions(settings, args.environment)
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
