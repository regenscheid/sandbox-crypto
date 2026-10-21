# SPDX-License-Identifier: MIT
"""Production command wiring for explicitly offline mounted workspaces."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Literal, cast

from .checkpoint_operator import operate
from .errors import WorkerError
from .settings import SettingsError, load_settings


def parser(command: argparse.ArgumentParser) -> None:
    """Register the operator-only CLI without adding any worker or agent API."""
    command.add_argument("action", choices=("create", "verify", "restore"))
    command.add_argument("--settings", type=Path, required=True)
    command.add_argument("--environment", required=True)
    command.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    command.add_argument("--checkpoint", type=Path, required=True)
    command.add_argument(
        "--workspace", type=Path, help="complete source root or new restore directory"
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
