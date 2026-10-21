# SPDX-License-Identifier: MIT
"""Trusted offline checkpoint operations; never stop, rebind or replay research."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import secrets
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from sandbox_science.contracts import EnvironmentInfo, EnvironmentState
from sandbox_science.contracts.jsonio import strict_loads
from sandbox_science.contracts.workspace_checkpoint import (
    MAX_CHECKPOINT_RECEIPT,
    CheckpointReceipt,
    CheckpointSource,
)

from .backends import OpenShellBackend
from .checkpoint_stream import StreamSummary, decode, encode
from .environments import OpenShellCli
from .settings import Settings
from .state_lock import StateLock

Architecture = Literal["amd64", "arm64"]


def sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read(path: Path, bound: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > bound:
            raise ValueError("checkpoint metadata is not a bounded regular file")
        return handle.read(bound + 1)


def install(path: Path, data: bytes) -> None:
    """Atomically publish immutable, synced metadata without replacing any prior result."""
    temporary = path.with_name(".checkpoint-" + secrets.token_hex(12))
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("checkpoint requires an existing retained worker state")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def retained_source(
    settings: Settings, environment: str, architecture: Architecture
) -> CheckpointSource:
    """Read original allocation records without schema upgrades or hold mutations."""
    with closing(_connect(settings.state_dir / "environments.sqlite3")) as db:
        row = db.execute(
            "SELECT substr(document,1,65537) FROM environments WHERE environment_id=?",
            (environment,),
        ).fetchone()
        if row is None or len(row[0].encode()) > 65536:
            raise ValueError("checkpoint environment is missing or malformed")
        strict_loads(row[0])
        info = EnvironmentInfo.model_validate_json(row[0])
        if (
            info.state not in (EnvironmentState.READY, EnvironmentState.STOPPED)
            or info.sandbox_id is None
        ):
            raise ValueError("checkpoint requires an original retained sandbox binding")
        launch = db.execute(
            "SELECT substr(driver_config,1,65537),require_workspace_prefix"
            " FROM sandbox_launch_settings WHERE sandbox=?",
            (info.sandbox,),
        ).fetchone()
    raw = None if launch is None else str(launch[0])
    required = launch is not None and launch[1] == 1
    if raw is not None:
        if len(raw.encode()) > 65536:
            raise ValueError("checkpoint launch settings exceed their bound")
        document = strict_loads(raw)
        if not isinstance(document, dict):
            raise ValueError("checkpoint launch settings are malformed")
        kubernetes = cast(dict[str, object], document).get("kubernetes")
        if isinstance(kubernetes, dict):
            pod = cast(dict[str, object], kubernetes).get("pod")
            selector = (
                cast(dict[str, object], pod).get("node_selector") if isinstance(pod, dict) else None
            )
            if (
                not isinstance(selector, dict)
                or cast(dict[str, object], selector).get("kubernetes.io/arch") != architecture
            ):
                raise ValueError("checkpoint architecture differs from original placement")
    with closing(_connect(settings.state_dir / "worker.sqlite3")) as db:
        prefix = db.execute(
            "SELECT substr(identity,1,128) FROM workspace_prefixes WHERE environment_id=?",
            (environment,),
        ).fetchone()
        revision = db.execute(
            "SELECT substr(fingerprint,1,128) FROM environment_revisions WHERE environment_id=?",
            (environment,),
        ).fetchone()
    if required and (prefix is None or revision is None):
        raise ValueError("checkpoint required prefix has no known retained baseline")
    return CheckpointSource(
        project_id=info.project_id,
        environment_id=info.environment_id,
        sandbox=info.sandbox,
        sandbox_id=info.sandbox_id,
        profile=info.profile,
        image=info.image,
        policy=info.policy,
        launch_settings=raw,
        architecture=architecture,
        require_workspace_prefix=required,
        prefix_identity=None if prefix is None else str(prefix[0]),
        package_fingerprint=None if revision is None else str(revision[0]),
    )


def _prefix(root: Path, source: CheckpointSource) -> None:
    sentinel = read(root / ".openshell-initialized", 128)
    if sentinel != b"initialized\n":
        raise ValueError("checkpoint workspace initialization sentinel is missing or invalid")
    if source.require_workspace_prefix:
        prefix = root / ".sandbox-science-prefix"
        if prefix.is_symlink() or not prefix.is_dir():
            raise ValueError("checkpoint required prefix is not a directory")
        identity = read(prefix / ".sandbox-science-prefix-id", 128).decode("ascii")
        if identity != source.prefix_identity:
            raise ValueError("checkpoint prefix differs from the retained original")


async def _stopped(settings: Settings, source: CheckpointSource) -> None:
    if settings.openshell is None:
        raise ValueError("offline checkpoints require the OpenShell backend")
    backend = OpenShellBackend(settings.openshell, settings.state_dir)
    description = await OpenShellCli(settings.openshell, backend.cli_env()).sandbox(source.sandbox)
    if (
        description is None
        or description.get("id") != source.sandbox_id
        or description.get("phase") != "Stopped"
    ):
        raise ValueError("original checkpoint sandbox is not confirmed Stopped")


def receipt(directory: Path) -> CheckpointReceipt:
    raw = read(directory / "receipt.json", MAX_CHECKPOINT_RECEIPT)
    strict_loads(raw)
    return CheckpointReceipt.model_validate_json(raw)


def _matches(summary: StreamSummary, expected: CheckpointReceipt) -> None:
    if summary != StreamSummary(
        expected.archive_sha256,
        expected.archive_size,
        expected.members,
        expected.content_bytes,
        expected.metadata_bytes,
    ):
        raise ValueError("checkpoint archive differs from its immutable receipt")


def verify(directory: Path, source: CheckpointSource | None = None) -> CheckpointReceipt:
    """A portable verification reads all bytes and the exact recorded provenance."""
    expected = receipt(directory)
    if source is not None and expected.source != source:
        raise ValueError("checkpoint receipt differs from the original retained binding")
    descriptor = os.open(directory / "archive.bin", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("checkpoint archive is not a regular file")
        _matches(decode(handle, expected.source), expected)
    return expected


def _rename_new(source: Path, target: Path) -> None:
    # Linux renameat2 makes completed directory publication atomic and no-overwrite;
    # checking target.exists() before ordinary rename would race an administrative writer.
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError("checkpoint restore destination already exists")
        raise OSError(code, "atomic checkpoint directory publication failed")


async def operate(
    settings: Settings,
    action: str,
    environment: str,
    architecture: Architecture,
    directory: Path,
    workspace: Path | None,
    exclusive: bool,
) -> CheckpointReceipt:
    """The operator owns mount exclusivity; this command owns only its new output files."""
    with StateLock(settings.state_dir):
        source = retained_source(settings, environment, architecture)
        directory = directory.absolute()
        if action == "verify":
            return verify(directory, source)
        if not exclusive or workspace is None:
            raise ValueError("declare --exclusive-offline-mount and supply --workspace")
        workspace = workspace.absolute()
        root = workspace.resolve()
        if action == "create":
            root = workspace.resolve(strict=True)
            if directory.resolve().is_relative_to(root):
                raise ValueError("checkpoint output must be outside the source workspace")
            journal = directory
        else:
            if workspace.exists() or workspace.is_symlink():
                raise FileExistsError("checkpoint restore requires a new destination")
            journal = workspace.with_name(workspace.name + ".checkpoint-restore")
            if workspace.resolve().is_relative_to(directory.resolve()):
                raise ValueError("checkpoint restore must be outside its archive")
        await _stopped(settings, source)
        journal.mkdir(mode=0o700)
        sync(journal.parent)
        install(
            journal / "intent.json",
            json.dumps(
                {
                    "action": action,
                    "state": "incomplete",
                    "source": source.model_dump(mode="json"),
                    "exclusive_offline_mount_declared": True,
                }
            ).encode(),
        )
        stage = journal / "tree"
        try:
            if action == "create":
                _prefix(root, source)
                with (directory / "archive.bin").open("xb") as handle:
                    os.chmod(directory / "archive.bin", 0o600)
                    summary = encode(root, source, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                _prefix(root, source)
                expected = CheckpointReceipt(
                    source=source,
                    created_at=datetime.now(UTC),
                    archive_sha256=summary.sha256,
                    archive_size=summary.size,
                    members=summary.members,
                    content_bytes=summary.content_bytes,
                    metadata_bytes=summary.metadata_bytes,
                )
                # Validate the actual persisted format, not just the encoder's counters.
                with (directory / "archive.bin").open("rb") as handle:
                    _matches(decode(handle, source), expected)
            else:
                expected = verify(directory, source)
                stage.mkdir(mode=0o700)
                with (directory / "archive.bin").open("rb") as handle:
                    _matches(decode(handle, source, stage), expected)
                _prefix(stage, source)
            if retained_source(settings, environment, architecture) != source:
                raise ValueError("checkpoint retained binding changed during the operation")
            await _stopped(settings, source)
            if action == "restore":
                _rename_new(stage, workspace)
                sync(workspace.parent)
            install(journal / "receipt.json", expected.model_dump_json().encode())
            return expected
        except BaseException as error:
            install(
                journal / "failure.json",
                json.dumps(
                    {
                        "state": "incomplete",
                        "error_type": type(error).__name__,
                        "message": str(error)[:512],
                    }
                ).encode(),
            )
            raise
