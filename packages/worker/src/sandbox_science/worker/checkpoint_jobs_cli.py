# SPDX-License-Identifier: MIT
"""``sandbox-science-worker checkpoint ...``: the commands of checkpoint Jobs and cleanup
(docs/WORKER.md#checkpoint-jobs).

```
checkpoint plan --settings S --environment E --architecture A --by WHO   # worker container
checkpoint capture-upload --workspace /mnt/workspace                      # the Job
checkpoint verify --settings S --checkpoint-id ID --manifest H --storage F \
    --stage upload|kept --by WHO
checkpoint promote --from F --to F --manifest H                           # operator, keeper key
checkpoint delete-objects --storage F --manifest H                        # operator, keeper key
checkpoint inventory --settings S [--keep 2]
checkpoint forget --settings S --manifest H --by WHO [--volume-gone] [--done --detail JSON]
checkpoint record-volume --settings S --phase intent|done|failed --volume PV [--sandbox S] --by WHO
checkpoint release-hold --settings S --checkpoint-id ID --by WHO --reason TEXT
```

Each prints one line of ``key=value`` pairs, or JSON for ``plan`` and ``inventory``; errors
go to stderr with exit status 1.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import cast

from sandbox_science.contracts import SYSTEM_CLOCK
from sandbox_science.contracts.jsonio import strict_loads
from sandbox_science.contracts.workspace_checkpoint import CheckpointPlan

from . import checkpoint_jobs, checkpoint_remote
from .checkpoint_capture import capture_upload
from .checkpoint_records import DEFAULT_KEEP, CheckpointRecords
from .checkpoint_s3 import load, parse
from .checkpoint_transport import MAX_CHUNKS
from .errors import WorkerError
from .settings import SettingsError, load_settings

PLAN_VARIABLE = "SS_CHECKPOINT_PLAN"
STORAGE_VARIABLE = "SS_CHECKPOINT_STORAGE"
TERMINATION_LOG = Path("/dev/termination-log")
MAX_DETAIL = 8192


def parser(command: argparse.ArgumentParser) -> None:
    actions = command.add_subparsers(dest="checkpoint_command", required=True)
    plan = actions.add_parser("plan", help="hold an environment stopped for a checkpoint Job")
    plan.add_argument("--environment", required=True)
    plan.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    plan.add_argument("--hold-seconds", type=float, default=checkpoint_jobs.HOLD_SECONDS)
    capture = actions.add_parser(
        "capture-upload", help="(the checkpoint Job) stream a mounted workspace to the store"
    )
    capture.add_argument("--workspace", type=Path, required=True)
    capture.add_argument("--timeout", type=float, default=3600)
    verify = actions.add_parser("verify", help="read a checkpoint back in full and record it")
    verify.add_argument("--checkpoint-id", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--storage", type=Path, required=True)
    verify.add_argument("--stage", choices=("upload", "kept"), required=True)
    verify.add_argument("--keep", type=int, choices=range(1, 101), default=DEFAULT_KEEP)
    verify.add_argument("--timeout", type=float, default=3600)
    promote = actions.add_parser("promote", help="copy a checkpoint into the kept bucket")
    promote.add_argument("--from", dest="source", type=Path, required=True)
    promote.add_argument("--to", dest="target", type=Path, required=True)
    promote.add_argument("--manifest", required=True)
    promote.add_argument("--timeout", type=float, default=3600)
    delete = actions.add_parser("delete-objects", help="remove one checkpoint's objects")
    delete.add_argument("--storage", type=Path, required=True)
    delete.add_argument("--manifest", required=True)
    delete.add_argument("--timeout", type=float, default=3600)
    delete.add_argument(
        "--chunks-file", type=Path, help="the worker's recorded chunk list (checkpoint chunks)"
    )
    listed = actions.add_parser("chunks", help="the chunk list the worker recorded")
    listed.add_argument("--manifest", required=True)
    inventory = actions.add_parser("inventory", help="what a cleanup decides with, as JSON")
    inventory.add_argument("--keep", type=int, choices=range(1, 101), default=DEFAULT_KEEP)
    forget = actions.add_parser("forget", help="journal a checkpoint's deletion")
    forget.add_argument("--manifest", required=True)
    forget.add_argument("--volume-gone", action="store_true")
    forget.add_argument("--done", action="store_true")
    forget.add_argument("--detail", default="{}")
    volume = actions.add_parser("record-volume", help="journal a volume's deletion")
    volume.add_argument("--phase", choices=("intent", "done", "failed"), required=True)
    volume.add_argument("--volume", required=True)
    volume.add_argument("--sandbox")
    volume.add_argument("--without-checkpoint", action="store_true")
    volume.add_argument("--detail", default="{}")
    release = actions.add_parser("release-hold", help="lift a checkpoint's hold")
    release.add_argument("--checkpoint-id", required=True)
    release.add_argument("--reason", required=True)
    for name in (
        "plan",
        "verify",
        "inventory",
        "forget",
        "record-volume",
        "release-hold",
        "chunks",
    ):
        actions.choices[name].add_argument("--settings", type=Path, required=True)
    for name in ("plan", "verify", "forget", "record-volume", "release-hold"):
        actions.choices[name].add_argument(
            "--by", dest="actor", required=True, help="who runs this, as the journal names them"
        )


def _detail(raw: str) -> dict[str, object]:
    if len(raw.encode()) > MAX_DETAIL:
        raise ValueError("--detail exceeds its bound")
    value = strict_loads(raw)
    if not isinstance(value, dict):
        raise ValueError("--detail is a JSON object")
    return cast(dict[str, object], value)


def _chunk_file(path: Path | None) -> list[str] | None:
    """A recorded chunk list, one SHA-256 a line; empty or absent means read the manifest."""
    if path is None:
        return None
    raw = path.read_bytes()
    if len(raw) > 65 * MAX_CHUNKS:
        raise ValueError("the chunk list exceeds its bound")
    names = raw.decode().split()
    if any(re.fullmatch(r"[0-9a-f]{64}", name) is None for name in names):
        raise ValueError("the chunk list holds something other than SHA-256 digests")
    return names or None


def _variable(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"the checkpoint Job sets {name}")
    return value


def _capture(args: argparse.Namespace) -> int:
    planned = CheckpointPlan.model_validate_json(_variable(PLAN_VARIABLE))
    if SYSTEM_CLOCK.now() >= planned.hold_expires_at:
        raise ValueError("the checkpoint's hold has expired; plan it again")
    storage = parse(_variable(STORAGE_VARIABLE))
    identity, receipt, written = capture_upload(
        args.workspace, planned.source, storage, planned.capture, timeout=args.timeout
    )
    line = (
        f"checkpoint_manifest={identity} checkpoint_id={planned.checkpoint_id}"
        f" archive_bytes={receipt.archive_size} members={receipt.members}"
        f" skipped={len(receipt.skipped)} chunks_written={written}"
    )
    print(line, flush=True)
    for entry in receipt.skipped:
        print(f"skipped {entry.kind} {json.dumps(entry.path)}")
    with contextlib.suppress(OSError):
        # Kubernetes keeps this in the Pod's status, where the operator's command reads it.
        TERMINATION_LOG.write_text(f"checkpoint_manifest={identity}\n")
    return 0


def run(args: argparse.Namespace) -> int:
    """Run one ``checkpoint`` subcommand."""
    command: str = args.checkpoint_command
    try:
        if command == "capture-upload":
            return _capture(args)
        if command == "promote":
            receipt, copied = checkpoint_remote.promote(
                load(args.source), load(args.target), args.manifest, timeout=args.timeout
            )
            print(
                f"promoted={args.manifest} archive_bytes={receipt.archive_size}"
                f" objects_copied={copied}"
            )
            return 0
        if command == "delete-objects":
            removed = checkpoint_remote.delete(
                load(args.storage),
                args.manifest,
                chunk_names=_chunk_file(args.chunks_file),
                timeout=args.timeout,
            )
            print(f"deleted={args.manifest} chunks_removed={removed}")
            return 0
        settings = load_settings(args.settings)
        if command == "plan":
            planned = checkpoint_jobs.plan(
                settings,
                args.environment,
                args.architecture,
                args.actor,
                hold_seconds=args.hold_seconds,
            )
            print(checkpoint_jobs.printable_plan(planned))
            return 0
        if command == "verify":
            receipt, prunable = checkpoint_jobs.verify(
                settings,
                args.checkpoint_id,
                args.manifest,
                load(args.storage),
                args.stage,
                args.actor,
                keep=args.keep,
                timeout=args.timeout,
            )
            state = "verified" if args.stage == "upload" else "kept"
            print(
                f"{state}={args.manifest} checkpoint_id={args.checkpoint_id}"
                f" capture={receipt.capture} skipped={len(receipt.skipped)}"
                f" prune_candidates={','.join(prunable) or 'none'}"
            )
            return 0
        if command == "inventory":
            found = checkpoint_jobs.inventory(settings, keep=args.keep)
            print(found.model_dump_json())
            return 0
        if command == "record-volume":
            checkpoint_jobs.record_volume(
                settings,
                args.phase,
                args.volume,
                args.sandbox,
                args.actor,
                without_checkpoint=args.without_checkpoint,
                detail=_detail(args.detail),
            )
            print(f"journaled volume={args.volume} phase={args.phase}")
            return 0
        records = CheckpointRecords(settings.state_dir)
        try:
            now = SYSTEM_CLOCK.now()
            if command == "chunks":
                for name in records.chunk_names(args.manifest):
                    print(name)
            elif command == "release-hold":
                released = records.release(args.checkpoint_id, now, f"{args.actor}: {args.reason}")
                print(f"released={args.checkpoint_id if released else 'none'}")
            elif args.done:
                records.deleted(args.manifest, now, args.actor, _detail(args.detail))
                print(f"journaled checkpoint={args.manifest} phase=done")
            else:
                records.forget(args.manifest, now, args.actor, volume_gone=args.volume_gone)
                print(f"journaled checkpoint={args.manifest} phase=intent")
        finally:
            records.close()
    except (SettingsError, WorkerError, OSError, ValueError, sqlite3.Error) as error:
        message = error.message if isinstance(error, WorkerError) else str(error)
        print(f"error: {message}", file=sys.stderr)
        return 1
    return 0
