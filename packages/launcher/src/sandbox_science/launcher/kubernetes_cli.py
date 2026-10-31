# SPDX-License-Identifier: MIT
"""Offline check/render of the bundle, and generation of its Secrets' contents (site),
with no cluster, image or subprocess access; and the operator's workspace checkpoint and
cleanup commands, which run ``kubectl`` with the operator's credentials only when given
``--apply`` (docs/KUBERNETES-BUNDLE.md#workspace-checkpoints)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from sandbox_science.contracts.workspace_checkpoint import CheckpointPlan, RestorePlan

from . import kubernetes_site, kubernetes_site_config
from .kubernetes_bundle import release_job, render
from .kubernetes_checkpoint import checkpoint_job, restore_job
from .kubernetes_config import BundleError, load
from .kubernetes_operations import OperationError, Operator, report, run_command, whoami


def write_bundle(path: Path, content: bytes) -> None:
    """Atomically expose a new private file; never replace an existing destination."""
    descriptor, name = tempfile.mkstemp(prefix=".kubernetes-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate site-private TOML, write one deterministic Kubernetes List, generate the
    Secrets' contents into a new private directory, or write the offline worker replica
    release Job for the operator to create by hand."""
    parser = argparse.ArgumentParser(prog="sandbox-science-kubernetes", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "render", "site", "release-job"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name != "check":
            command.add_argument("--output", type=Path, required=True)
    release = commands.choices["release-job"]
    release.add_argument("--preview", action="store_true", help="report without releasing")
    release.add_argument("--after", help="the previous report's next_after")
    job = commands.add_parser("checkpoint-job", help="render one checkpoint Job from a plan")
    job.add_argument("--config", type=Path, required=True)
    job.add_argument("--plan", type=Path, required=True, help="the worker's checkpoint plan")
    job.add_argument("--claim", required=True, help="the sandbox's workspace claim")
    job.add_argument("--node", help="the volume's node (volume.kubernetes.io/selected-node)")
    job.add_argument("--output", type=Path, required=True)
    checkpoint = commands.add_parser(
        "checkpoint", help="checkpoint one environment's workspace (prints unless --apply)"
    )
    cleanup = commands.add_parser(
        "cleanup", help="list leftover volumes and old checkpoints; delete named ones"
    )
    restore = commands.add_parser(
        "restore", help="restore a kept checkpoint into a fresh environment (prints unless --apply)"
    )
    restore_render = commands.add_parser("restore-job", help="render one restore Job from a plan")
    restore_render.add_argument("--config", type=Path, required=True)
    restore_render.add_argument("--plan", type=Path, required=True, help="the worker's plan")
    restore_render.add_argument("--claim", required=True, help="the target's workspace claim")
    restore_render.add_argument("--node", help="the volume's node")
    restore_render.add_argument("--output", type=Path, required=True)
    for command in (checkpoint, cleanup, restore):
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--apply", action="store_true", help="run the steps")
        command.add_argument("--operator", help="who runs this, for the deletion journal")
        command.add_argument("--context", help="the kubeconfig context to use")
    checkpoint.add_argument("--environment", required=True)
    checkpoint.add_argument("--architecture", choices=("amd64", "arm64"), default="amd64")
    checkpoint.add_argument("--keeper-upload", type=Path, help="operator/checkpoint-upload.toml")
    checkpoint.add_argument("--keeper-kept", type=Path, help="operator/checkpoint-kept.toml")
    cleanup.add_argument("--volume", action="append", default=[], help="a PV to delete")
    cleanup.add_argument("--checkpoint", action="append", default=[], help="a manifest SHA-256")
    cleanup.add_argument(
        "--without-checkpoint",
        action="append",
        default=[],
        metavar="VOLUME",
        help="this volume may go without a current verified checkpoint (repeatable)",
    )
    cleanup.add_argument(
        "--retire-checkpoints",
        action="append",
        default=[],
        metavar="ENVIRONMENT",
        help="remove the remaining checkpoints of this deleted environment whose volume is"
        " gone (repeatable)",
    )
    cleanup.add_argument("--keeper-kept", type=Path, help="operator/checkpoint-kept.toml")
    cleanup.add_argument(
        "--expire-uploads",
        type=int,
        metavar="HOURS",
        help="remove upload-bucket objects older than this that no unpromoted checkpoint needs",
    )
    cleanup.add_argument("--keeper-upload", type=Path, help="operator/checkpoint-upload.toml")
    restore.add_argument("--checkpoint", metavar="MANIFEST", help="a kept checkpoint's SHA-256")
    restore.add_argument("--target", metavar="ENVIRONMENT", help="a new or deleted environment")
    restore.add_argument("--resume", metavar="RESTORE_ID", help="retry an unfinished restore")
    restore.add_argument("--discard", metavar="RESTORE_ID", help="delete its target environment")
    restore.add_argument("--reason", default="discarded by the operator")
    restore.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    cleanup.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    if args.command in ("checkpoint", "cleanup", "restore"):
        try:
            return _operate(args)
        except (BundleError, OperationError, ValidationError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    try:
        if args.command in ("checkpoint-job", "restore-job"):
            try:
                if args.command == "checkpoint-job":
                    plan = CheckpointPlan.model_validate_json(args.plan.read_bytes())
                    rendered = checkpoint_job(load(args.config), plan, args.claim, args.node)
                else:
                    restoring = RestorePlan.model_validate_json(args.plan.read_bytes())
                    rendered = restore_job(load(args.config), restoring, args.claim, args.node)
            except (ValidationError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            write_bundle(
                args.output, (json.dumps(rendered, indent=2, sort_keys=True) + "\n").encode()
            )
            print("rendered offline; kubectl create -f the file (it needs the plan's hold)")
            return 0
        if args.command == "site":
            site, bundle = kubernetes_site_config.load(args.config)
            kubernetes_site.write(args.output, kubernetes_site.plan(site, bundle))
            print(
                f"generated the Secrets' contents in {args.output}; read its README.md, "
                "which lists what is left to the operator; no cluster changes made"
            )
            return 0
        if args.command == "release-job":
            try:
                job = release_job(load(args.config), apply=not args.preview, after=args.after)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            write_bundle(args.output, (json.dumps(job, indent=2, sort_keys=True) + "\n").encode())
            print(
                "rendered offline; scale the pool to zero, then kubectl create -f the file "
                "(docs/KUBERNETES-BUNDLE.md#offline-worker-replica-release)"
            )
            return 0
        bundle = render(load(args.config))
        if args.command == "render":
            write_bundle(
                args.output, (json.dumps(bundle, indent=2, sort_keys=True) + "\n").encode()
            )
            print("rendered offline; no images published and no cluster changes made")
        else:
            print(
                "valid offline configuration; Secret contents and cluster compatibility unverified"
            )
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("error: cannot create a new output file; no overwrite is permitted", file=sys.stderr)
        return 1
    return 0


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip() == "delete"
    except EOFError:
        return False


def keeper_sdk_available() -> bool:
    """Whether this environment has the S3 SDK the keeper key's local steps need (promote,
    checkpoint deletion, upload expiry): ``uv sync --extra s3`` installs it."""
    return importlib.util.find_spec("boto3") is not None


def _keeper_ready() -> None:
    if not keeper_sdk_available():
        raise ValueError(
            "the keeper key's steps run on this machine and need the S3 SDK (boto3): run"
            " `uv sync --extra s3` in the checkout first; nothing was changed"
        )


def _operate(args: argparse.Namespace) -> int:
    config = load(args.config)
    if args.apply and (
        (args.command == "checkpoint" and args.keeper_upload is not None)
        or (
            args.command == "cleanup"
            and (
                ((args.checkpoint or args.retire_checkpoints) and args.keeper_kept is not None)
                or (args.expire_uploads is not None and args.keeper_upload is not None)
            )
        )
    ):
        _keeper_ready()
    kubectl = ("kubectl", *(("--context", args.context) if args.context else ()))
    actor = args.operator or (whoami(run_command, kubectl) if args.apply else "OPERATOR")
    operator = Operator(config, actor, run=run_command, kubectl=kubectl)
    if args.command == "checkpoint":
        upload: Path | None = args.keeper_upload
        kept: Path | None = args.keeper_kept
        if (upload is None) != (kept is None):
            raise ValueError("give both --keeper-upload and --keeper-kept, or neither")
        keeper = None if upload is None or kept is None else (upload, kept)
        if not args.apply:
            for line in operator.checkpoint_steps(args.environment, args.architecture, keeper):
                print(line)
            return 0
        manifest = operator.run_checkpoint(args.environment, args.architecture, keeper)
        print(f"checkpoint_manifest={manifest}")
        return 0
    if args.command == "restore":
        return _restore(args, operator)
    found = operator.survey()
    hours: int | None = args.expire_uploads
    expiring = hours is not None
    if not args.volume and not args.checkpoint and not args.retire_checkpoints and not expiring:
        for line in report(found):
            print(line)
        return 0
    # The worker's chunk lists pass through a private directory of the operator's.
    workdir = Path(tempfile.mkdtemp(prefix="sandbox-science-cleanup-")) if args.apply else Path(".")
    steps = operator.delete(
        found,
        args.volume,
        args.checkpoint,
        without_checkpoint=args.without_checkpoint,
        keeper_kept=args.keeper_kept,
        workdir=workdir,
        retire=args.retire_checkpoints,
    )
    if hours is not None:
        steps.extend(operator.expire_uploads(hours, args.keeper_upload, workdir))
    if not args.apply:
        for step in steps:
            print(" ".join(shlex.quote(part) for part in step.command))
        return 0
    if (args.checkpoint or args.retire_checkpoints) and args.keeper_kept is None:
        raise ValueError("deleting a checkpoint needs --keeper-kept and the keeper key")
    if expiring and args.keeper_upload is None:
        raise ValueError("expiring uploads needs --keeper-upload and the keeper key")
    retiring = sum(
        1
        for mark in found.checkpoints
        if mark.retire and mark.checkpoint.environment_id in args.retire_checkpoints
    )
    named = len(args.volume) + len(args.checkpoint) + retiring
    what = f"{named} item(s)" + (
        f" and upload-bucket objects older than {args.expire_uploads} hours" if expiring else ""
    )
    if not args.yes and not _confirm(
        f"Delete {what} for good, journaled as {actor}? Type 'delete': "
    ):
        print("nothing deleted")
        return 1
    operator.apply(steps)
    return 0


def _restore(args: argparse.Namespace, operator: Operator) -> int:
    chosen = [args.checkpoint is not None or args.target is not None, args.resume, args.discard]
    if sum(1 for choice in chosen if choice) != 1:
        raise ValueError("give --checkpoint and --target, or --resume, or --discard")
    if args.discard is not None:
        if not args.apply:
            print(
                " ".join(
                    shlex.quote(part)
                    for part in operator.exec_worker(
                        "restore-discard",
                        "--restore-id",
                        args.discard,
                        "--reason",
                        args.reason,
                        "--by",
                        operator.actor,
                    )
                )
            )
            return 0
        if not args.yes and not _confirm(
            f"Delete the target environment of {args.discard}, journaled as {operator.actor}?"
            " Type 'delete': "
        ):
            print("nothing deleted")
            return 1
        operator.discard_restore(args.discard, args.reason)
        return 0
    manifest: str | None = args.checkpoint
    target: str | None = args.target
    if args.resume is None and (manifest is None or target is None):
        raise ValueError("a restore names --checkpoint and --target")
    if not args.apply:
        if manifest is None or target is None:
            raise ValueError("--resume runs with --apply")
        for line in operator.restore_steps(manifest, target):
            print(line)
        return 0
    plan = operator.run_restore(manifest, target, resume=args.resume)
    print(f"restored={plan.restore_id} environment={plan.target.environment_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
