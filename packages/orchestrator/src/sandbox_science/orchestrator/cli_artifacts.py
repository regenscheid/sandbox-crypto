# SPDX-License-Identifier: MIT
"""Operator-only backing configuration and referenced-blob archive/eviction."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Generator
from pathlib import Path

from .artifact_blobs import ArtifactBlobs, Blob
from .artifact_config import S3Settings, install
from .control import ControlError
from .documents import DocumentError, DocumentStore


def references(database: Path) -> Generator[Blob]:
    """Page immutable references without holding a transaction across object I/O."""
    documents = DocumentStore(database, readonly=True)
    after = ""
    try:
        while True:
            with documents.control.read() as tx:
                rows = tx.sql.execute(
                    "SELECT sha256,min(size),max(size) FROM ("
                    "SELECT sha256,size FROM artifact_revision_blobs UNION ALL "
                    "SELECT sha256,size FROM user_upload_blobs) "
                    "WHERE sha256>? GROUP BY sha256 ORDER BY sha256 LIMIT 100",
                    (after,),
                ).fetchall()
            if not rows:
                return
            for sha256, size, largest in rows:
                if size != largest:
                    raise ValueError("artifact hash has inconsistent referenced sizes")
                yield Blob(sha256, size)
            after = rows[-1][0]
    finally:
        documents.close()


def main(argv: list[str] | None = None) -> int:
    """No credential arguments, remote deletion, orphan scanning or implicit eviction."""
    parser = argparse.ArgumentParser(prog="sandbox-science-artifacts")
    commands = parser.add_subparsers(dest="command", required=True)
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
