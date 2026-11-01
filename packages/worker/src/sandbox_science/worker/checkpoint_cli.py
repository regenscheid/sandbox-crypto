# SPDX-License-Identifier: MIT
"""Production command wiring for explicitly offline mounted workspaces."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Literal, cast

from .checkpoint_operator import operate, retained_source
from .errors import WorkerError
from .settings import SettingsError, load_settings
from .state_lock import StateLock


def parser(command: argparse.ArgumentParser) -> None:
    """Register the operator-only CLI without adding any worker or agent API."""
    command.add_argument("action", choices=("create", "verify", "restore", "upload", "fetch"))
    command.add_argument("--settings", type=Path, required=True)
    command.add_argument("--environment", required=True)
    command.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    command.add_argument("--checkpoint", type=Path, required=True)
    command.add_argument(
        "--workspace", type=Path, help="complete source root or new restore directory"
    )
    command.add_argument("--storage", type=Path, help="private S3 destination settings; no keys")
    command.add_argument("--manifest", help="exact expected SHA-256 of a fetch manifest")
    command.add_argument(
        "--timeout", type=float, default=3600, help="transport operation deadline in seconds"
    )
    command.add_argument(
        "--exclusive-offline-mount",
        action="store_true",
        help="declare no other process or administrator can write this mount",
    )


def run(args: argparse.Namespace) -> int:
    """Report local completion only; neither remote retention nor a complete recovery set."""
    try:
        settings = load_settings(args.settings)
        if args.action in ("upload", "fetch"):
            from .checkpoint_s3 import load
            from .checkpoint_transport import fetch, upload

            if args.storage is None or (args.action == "fetch" and args.manifest is None):
                raise ValueError("transport requires --storage, and fetch requires --manifest")
            destination = load(args.storage)
            with StateLock(settings.state_dir):
                source = retained_source(settings, args.environment, args.architecture)
                if args.action == "upload":
                    identity = upload(args.checkpoint, source, destination, timeout=args.timeout)
                    print(f"remote_checkpoint_manifest={identity} recovery_set=incomplete")
                else:
                    result = fetch(
                        args.checkpoint, source, destination, args.manifest, timeout=args.timeout
                    )
                    print(f"fetched_checkpoint={result.archive_sha256} recovery_set=incomplete")
            return 0
        result = asyncio.run(
            operate(
                settings,
                args.action,
                args.environment,
                cast(Literal["amd64", "arm64"], args.architecture),
                args.checkpoint,
                args.workspace,
                args.exclusive_offline_mount,
            )
        )
    except (SettingsError, WorkerError, OSError, ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        f"local_checkpoint={result.archive_sha256} bytes={result.archive_size} "
        f"members={result.members}"
    )
    print("remote_retention=not_established recovery_set=incomplete")
    return 0
