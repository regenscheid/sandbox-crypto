# SPDX-License-Identifier: MIT
"""Operator-only backing configuration and referenced-blob archive/eviction."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from os import statvfs
from pathlib import Path

from .artifact_blobs import ArtifactBlobs
from .artifact_config import S3Settings, install
from .artifact_maintenance import load_policy, maintain
from .artifact_references import references as references
from .artifact_retention import RetentionOutbox, drain
from .control import ControlError
from .documents import DocumentError, DocumentStore
from .maintenance_cursor import progress, save, scope
from .worker_client import WorkerClient


async def acknowledge(args: argparse.Namespace) -> int:
    """Drain one endpoint-bound page; no remote capture, file transfer or deletion."""
    if not 1 <= args.limit <= 100 or args.after < 0:
        raise ValueError("invalid acknowledgement page")
    documents = DocumentStore(args.database)
    try:
        token = args.token_file.read_text().strip()
        async with WorkerClient(args.worker, token) as client:
            result = await drain(
                RetentionOutbox(documents.control),
                client,
                args.project,
                limit=args.limit,
                after=args.after,
            )
        print(
            json.dumps(
                {
                    "delivered": result.delivered,
                    "failed": result.failed,
                    "after": result.after,
                    "scope": "configured-worker",
                }
            )
        )
        return int(result.failed != 0)
    finally:
        documents.close()


def volume_use_percent(path: Path) -> float:
    """How full the file system holding ``path`` is, as ``df`` counts it: used blocks over
    used plus those still available to unprivileged writers (the root reserve excluded)."""
    found = statvfs(path)
    used = found.f_blocks - found.f_bfree
    total = used + found.f_bavail
    return 100.0 if total <= 0 else 100.0 * used / total


def maintenance(args: argparse.Namespace) -> int:
    """Run one policy pass, optionally serializing and checkpointing a scheduler.

    With ``--at-use-percent``, a pass runs only once the control volume (the database's
    directory) is at least that full, so a schedule trims "at about N% use" without a
    controller; below it nothing is read, written or advanced."""
    if args.at_use_percent is not None:
        use = volume_use_percent(args.database.parent)
        if use < args.at_use_percent:
            report = {
                "apply": args.apply,
                "skipped": "below_threshold",
                "use_percent": round(use, 1),
                "at_use_percent": args.at_use_percent,
            }
            print(json.dumps(report), flush=True)
            return 0
    policy = load_policy(args.policy)
    if args.cursor_file is None:
        result = maintain(args.database, policy, apply=args.apply, after=args.after or "")
    else:
        with progress(args.cursor_file, scope(args.database, policy, args.apply)) as current:
            result = maintain(args.database, policy, apply=args.apply, after=current.after)
            save(args.cursor_file, current, result.next_after)
    print(json.dumps({"apply": args.apply, **asdict(result)}), flush=True)
    return int(result.failed != 0)


def main(argv: list[str] | None = None) -> int:
    """No credential arguments, remote deletion, orphan scanning or implicit eviction."""
    parser = argparse.ArgumentParser(prog="sandbox-science-artifacts")
    commands = parser.add_subparsers(dest="command", required=True)
    maintenance_parser = commands.add_parser("maintain", help="one bounded physical cache pass")
    maintenance_parser.add_argument("--database", type=Path, required=True)
    maintenance_parser.add_argument("--policy", type=Path, required=True)
    maintenance_parser.add_argument("--apply", action="store_true")
    cursor = maintenance_parser.add_mutually_exclusive_group()
    cursor.add_argument("--after", help="continue after this reference hash")
    cursor.add_argument("--cursor-file", type=Path, help="private persistent scheduled progress")
    maintenance_parser.add_argument(
        "--at-use-percent",
        type=int,
        choices=range(1, 101),
        metavar="1-100",
        help="do nothing while the database's volume is less full than this",
    )
    ack = commands.add_parser("acknowledge", help="retry a bounded durable worker-retention page")
    ack.add_argument("--database", type=Path, required=True)
    ack.add_argument("--worker", required=True)
    ack.add_argument("--token-file", type=Path, required=True)
    ack.add_argument("--project", required=True)
    ack.add_argument("--limit", type=int, default=8)
    ack.add_argument("--after", type=int, default=0)
    configure = commands.add_parser("configure", help="install immutable private S3 settings")
    configure.add_argument("--database", type=Path, required=True)
    configure.add_argument("--bucket", required=True)
    configure.add_argument("--prefix", default="sandbox-science")
    configure.add_argument("--region", default="us-east-1")
    configure.add_argument("--endpoint")
    configure.add_argument(
        "--addressing-style", choices=("auto", "path", "virtual"), default="auto"
    )
    for command in ("archive", "evict"):
        action = commands.add_parser(command, help=f"{command} only database-referenced blobs")
        action.add_argument("--database", type=Path, required=True)
        if command == "evict":
            action.add_argument("--apply", action="store_true", help="unlink verified local copies")
    args = parser.parse_args(argv)
    try:
        if args.command == "acknowledge":
            return asyncio.run(acknowledge(args))
        if args.command == "maintain":
            return maintenance(args)
        store = ArtifactBlobs(args.database)
        if args.command == "configure":
            # Do not create or migrate a database while configuring its backing.
            documents = DocumentStore(args.database, readonly=True)
            try:
                with documents.control.read():
                    pass
            finally:
                documents.close()
            settings = S3Settings(
                bucket=args.bucket,
                prefix=args.prefix,
                region=args.region,
                endpoint=args.endpoint,
                addressing_style=args.addressing_style,
            )
            install(store.root, settings)
            print(json.dumps({"state": "configured"}))
            return 0
        if store.backing is None:
            raise ValueError("configure artifact S3 backing first")
        for blob in references(args.database):
            if args.command == "archive":
                store.archive(blob)
                state = "archived"
            else:
                local = store.evict(blob, apply=args.apply)
                state = ("evicted" if args.apply else "would_evict") if local else "already_remote"
            print(
                json.dumps({"sha256": blob.sha256, "bytes": blob.size, "state": state}), flush=True
            )
    except (ControlError, DocumentError, OSError, ValueError):
        # SDK/configuration errors can contain private endpoint/credential details.
        print(
            "artifact storage command failed; verify configuration and retained copies",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
