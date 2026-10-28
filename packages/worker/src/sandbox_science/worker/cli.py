# SPDX-License-Identifier: MIT
"""Command line: ``sandbox-science-worker serve --settings worker.toml``.

```
sandbox-science-worker serve --settings worker.toml [--log-level info]
sandbox-science-worker token --out worker.token     # prints the new token's SHA-256
sandbox-science-worker token --sha256 client.token  # the SHA-256 of an existing token
sandbox-science-worker policy add --settings worker.toml [--project p1] policy.yaml
sandbox-science-worker environments list --settings worker.toml [--state creating]
sandbox-science-worker environments reconcile --settings worker.toml [--no-wait] ENVIRONMENT_ID
sandbox-science-worker checkpoint plan|verify|inventory|... (checkpoint_jobs_cli)
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
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import SYSTEM_CLOCK, Identifier
from sandbox_science.contracts.logs import configure_logging
from sandbox_science.policy.openshell import CompileError, compile_sandbox
from sandbox_science.policy.store import StoreError

from . import checkpoint_cli, checkpoint_jobs_cli
from .admission import DraftWatcher
from .artifact_retention import MAX_RELEASE_PAGE
from .artifacts import Blobs
from .build import build_environments, build_worker
from .environments import EnvironmentRecords, Environments, printable
from .errors import WorkerError
from .settings import MIN_TOKEN, Settings, SettingsError, load_settings, token_digest
from .state_lock import StateLock
from .store import Store
from .suspend import Suspender
from .unknown_creations import CreationReconciler, Reconciliation, when

_log = logging.getLogger("sandbox_science.worker")


async def serve(settings: Settings) -> None:
    # One clock for everything that compares times with another component.
    clock = SYSTEM_CLOCK
    parts = build_worker(settings, settings.token(), clock=clock)
    manager, environments, shared = parts.manager, parts.environments, parts.shared
    # SIGTERM/SIGINT detach sandbox daemons; the stdio development executor stops.
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    runner = web.AppRunner(parts.app, access_log=None)
    background: list[asyncio.Task[None]] = []
    try:
        if shared is not None:
            removed = await shared.sweep()
            if removed:
                _log.info("deleted %d blobs no shared file uses", removed)
        await manager.start()
        await parts.provisioner.start()
        if parts.children is not None:
            # Child environments a previous worker was making fail; the rest stay (CE-12).
            await parts.children.start()
        if environments is not None:
            # Before serving: creations an operator rolled back with the worker stopped
            # still owe the deletion hooks this worker registered (B3).
            environments.serving_since = clock.now()
            for environment_id in await environments.run_pending_hooks():
                _log.info("ran the deletion hooks of environment %s", environment_id)
        await runner.setup()
        site = web.TCPSite(
            runner,
            settings.listen_host,
            settings.listen_port,
            ssl_context=settings.tls.context() if settings.tls else None,
        )
        await site.start()
        _log.info("worker API on %s (%s backend)", settings.listen, manager.backend.name)
        if environments is not None and environments.settings.admission:
            interval = environments.settings.admission_interval
            background.append(asyncio.create_task(DraftWatcher(environments, interval).run()))
        if environments is not None:
            # Creations with an unknown outcome clear themselves on confirmed absence (B3).
            reconciler = CreationReconciler(
                environments, environments.settings.creation_recheck, clock=clock
            )
            background.append(asyncio.create_task(reconciler.run()))
        if environments is not None and environments.settings.suspend_after is not None:
            suspender = Suspender(
                environments, manager, environments.settings.suspend_after, clock=clock
            )
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
        await parts.close()


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


def list_environments(settings: Settings, state: str | None) -> int:
    """Print the environments that are not deleted, of one state or of every state; a
    creation without an identity says how often it was seen absent (B3)."""
    if settings.openshell is None or settings.openshell.environments is None:
        print("error: the settings have no [openshell.environments] table", file=sys.stderr)
        return 1
    path = settings.state_dir / "environments.sqlite3"
    if not path.is_file():
        print(f"error: no environment records at {path}; check state_dir", file=sys.stderr)
        return 1
    records = EnvironmentRecords(path)
    try:
        shown = [info for info in records.listed(None) if state in (None, info.state)]
        if not shown:
            print(f"no {state} environments" if state else "no environments")
        for info in shown:
            line = (
                f"{info.environment_id} {info.state} project {info.project_id} {info.kind}"
                f" sandbox {info.sandbox} ({info.sandbox_id or 'no identity'})"
                f" created {when(info.created_at)}"
            )
            if info.state == "creating" and info.sandbox_id is None:
                seen = records.observations(info)
                line += f"; seen absent {len(seen)} times" + (
                    f", last at {when(seen[-1].observed_at)}" if seen else ""
                )
            print(line)
    finally:
        records.close()
    return 0


def reconcile_environment(settings: Settings, environment_id: str, *, wait: bool) -> int:
    """Reconcile a creation with an unknown outcome on confirmed absence (B3), with the
    worker stopped: it takes the state directory's lock, as ``captures`` does."""
    openshell = settings.openshell
    if openshell is None or openshell.environments is None:
        print("error: the settings have no [openshell.environments] table", file=sys.stderr)
        return 1
    try:
        lock = StateLock(settings.state_dir)
    except SettingsError as exc:
        print(
            f"error: {exc}. The running worker reconciles creating environments itself every"
            f" {openshell.environments.creation_recheck:g} s, and deleting one through its API"
            " follows the same rule; stop it to reconcile one here",
            file=sys.stderr,
        )
        return 1
    with lock:
        if not (settings.state_dir / "environments.sqlite3").is_file():
            print(f"error: no environment records in {settings.state_dir}", file=sys.stderr)
            return 1
        try:
            environments = build_environments(settings)
        except (SettingsError, WorkerError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if environments is None:  # checked above; the builder says so again
            return 1
        # The running worker's deletion hooks are not registered here; it runs them.
        environments.hooks_deferred = True
        try:
            result = asyncio.run(_reconcile(environments, environment_id, wait=wait))
        except WorkerError as error:
            print(f"error: {environment_id}: {error.message}", file=sys.stderr)
            return 1
        finally:
            environments.close()
    print(f"{result.action}: {result.message}")
    return 0 if result.action in ("rolled_back", "adopted") else 1


async def _reconcile(
    environments: Environments, environment_id: str, *, wait: bool
) -> Reconciliation:
    result = await environments.reconcile_creation(environment_id)
    if wait and result.action == "inconclusive" and result.retry_at is not None:
        print(f"waiting: {result.message}", flush=True)
        delay = (result.retry_at - environments.clock.now()).total_seconds()
        # A little past the due time, so the second read is never early.
        await environments.clock.sleep(max(delay, 0.0) + 0.05)
        result = await environments.reconcile_creation(environment_id)
    return result


def show_captures(settings: Settings, clear: Sequence[str]) -> int:
    """Print the capture leases that hold publication admission, after forgetting those in
    ``clear``. Only with the worker stopped: it takes the state directory's lock."""
    try:
        lock = StateLock(settings.state_dir)
    except SettingsError as exc:
        print(f"error: {exc}; stop it first", file=sys.stderr)
        return 1
    with lock:
        path = settings.state_dir / "worker.sqlite3"
        if not path.is_file():
            print(f"error: no worker store at {path}; check state_dir", file=sys.stderr)
            return 1
        store = Store(path)  # brings the store to this worker's version, as serve does
        try:
            status = 0
            for publication_id in clear:
                cleared = store.clear_capture(publication_id, datetime.now(UTC).isoformat())
                if cleared is None:
                    print(f"no capture lease {publication_id}: already cleared or never held")
                    continue
                _log.warning("capture lease cleared on the operator's word: %s", cleared)
                print(f"cleared {cleared}")
            leases = store.capture_leases()
            if not leases:
                print("no capture leases")
            for lease in leases:
                sandbox = f"{lease.sandbox} ({lease.sandbox_id})" if lease.sandbox else "unknown"
                print(
                    f"{lease.publication_id} {lease.environment_id} {lease.phase}"
                    f" capture {lease.capture_id or 'none'}"
                    f" executor {lease.incarnation or 'none'} sandbox {sandbox}"
                )
        finally:
            store.close()
    return status


_IDENTIFIER = TypeAdapter[str](Identifier)


def release_artifacts(settings: Settings, *, apply: bool, limit: int, after: str | None) -> int:
    """Inspect or release one offline page under the worker's exclusive state lease."""
    try:
        with StateLock(settings.state_dir):
            path = settings.state_dir / "worker.sqlite3"
            if not path.is_file():
                raise SettingsError(f"no worker store at {path}; check state_dir")
            store = Store(path)
            try:
                page = store.artifact_retention.release_page(
                    Blobs(settings.state_dir / "artifacts"), apply=apply, limit=limit, after=after
                )
                for entry in page.entries:
                    print(f"{entry.sha256} {entry.size} {entry.status} {entry.reason}".rstrip())
                print(f"next_after={page.next_after or 'none'}")
            finally:
                store.close()
    except (SettingsError, WorkerError, OSError, ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _project(value: str) -> str:
    try:
        return _IDENTIFIER.validate_python(value)
    except ValidationError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a project ID") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox-science-worker")
    commands = parser.add_subparsers(dest="command", required=True)
    checkpoint_cli.parser(
        commands.add_parser("workspace-checkpoint", help="offline complete workspace checkpoint")
    )
    checkpoint_jobs_cli.parser(
        commands.add_parser(
            "checkpoint",
            help="checkpoint Jobs: plan, verify, promote, inventory and cleanup records",
        )
    )
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
    captures_parser = commands.add_parser(
        "captures",
        help="show the capture leases that hold publication admission; the worker is stopped",
    )
    captures_parser.add_argument("--settings", type=Path, required=True)
    captures_parser.add_argument(
        "--clear",
        metavar="PUBLICATION_ID",
        action="append",
        default=[],
        help="forget this lease, once its sandbox cleaned up or is gone (repeatable)",
    )
    environments_parser = commands.add_parser(
        "environments", help="list environments, and reconcile creations with an unknown outcome"
    )
    environments_commands = environments_parser.add_subparsers(
        dest="environments_command", required=True
    )
    list_parser = environments_commands.add_parser(
        "list", help="list the environments that are not deleted"
    )
    list_parser.add_argument("--settings", type=Path, required=True)
    list_parser.add_argument("--state", choices=["creating", "ready", "stopped"])
    reconcile_parser = environments_commands.add_parser(
        "reconcile",
        help="mark a creation whose sandbox is confirmed absent deleted; the worker is stopped",
    )
    reconcile_parser.add_argument("--settings", type=Path, required=True)
    reconcile_parser.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="refuse instead of waiting for the second read that confirms absence",
    )
    reconcile_parser.add_argument("environment_id")
    retention_parser = commands.add_parser(
        "artifact-retention", help="offline capture replica release; dry-run by default"
    )
    retention_parser.add_argument("--settings", type=Path, required=True)
    retention_parser.add_argument("--apply", action="store_true", help="unlink eligible CAS blobs")
    retention_parser.add_argument(
        "--limit", type=int, choices=range(1, MAX_RELEASE_PAGE + 1), default=10
    )
    retention_parser.add_argument("--after", help="continue after this SHA-256 cursor")
    args = parser.parse_args(argv)
    if args.command == "workspace-checkpoint":
        return checkpoint_cli.run(args)
    if args.command == "checkpoint":
        return checkpoint_jobs_cli.run(args)
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
    if args.command in ("policy", "admissions", "captures", "artifact-retention", "environments"):
        try:
            settings = load_settings(args.settings)
        except SettingsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.command == "policy":
            return add_policies(settings, args.policies, args.projects)
        if args.command == "captures":
            return show_captures(settings, args.clear)
        if args.command == "environments" and args.environments_command == "list":
            return list_environments(settings, args.state)
        if args.command == "environments":
            return reconcile_environment(settings, args.environment_id, wait=args.wait)
        if args.command == "artifact-retention":
            return release_artifacts(settings, apply=args.apply, limit=args.limit, after=args.after)
        return show_admissions(settings, args.environment)
    configure_logging("worker", level=args.log_level)
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
    except WorkerError as exc:  # such as a state directory this worker refuses
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    return 0
