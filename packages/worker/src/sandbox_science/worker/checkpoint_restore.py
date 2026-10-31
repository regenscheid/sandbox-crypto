# SPDX-License-Identifier: MIT
"""Restoring a kept checkpoint into a fresh environment
(docs/KUBERNETES-BUNDLE.md#restoring-a-checkpoint).

The worker's part runs in its container, as the checkpoint commands do:

* ``plan`` reads the kept checkpoint back in full and computes its tree digest, creates the
  target environment through the worker's own API (a new ID, or one the worker records as
  deleted), records a restore that keeps its sandbox stopped with no expiry, suspends it and
  confirms that nothing has run in it;
* ``verify`` compares the tree a restore Job reports with the digest it computed, and only
  then lets the sandbox start;
* ``record`` journals the Job and failures; ``discard`` deletes a target whose restore
  cannot finish.

``write`` is the restore Job's: it empties the target volume (OpenShell's seed, or its own
partial tree from an earlier attempt), writes the checkpoint from the kept bucket checking
every chunk's hash, and reports the tree digest of what is on the volume.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    EnvironmentInfo,
    EnvironmentRequest,
    EnvironmentState,
)
from sandbox_science.contracts.jsonio import strict_loads
from sandbox_science.contracts.workspace_checkpoint import CheckpointReceipt, RestorePlan

from . import checkpoint_remote
from .checkpoint_jobs import Suspend, local_request, local_suspend
from .checkpoint_operator import check_prefix, confirm_stopped, install, retained_source, sync
from .checkpoint_records import CheckpointRecords, RecordError
from .checkpoint_s3 import Storage
from .checkpoint_tree import clear, describe, tree_index
from .settings import Settings

MARKER = ".sandbox-science-restore"
"""At the target volume's root while a restore Job writes it: the restore's ID, so that a
retry of the same restore may empty the volume again. Never part of the restored tree."""
SEED_SENTINEL = ".openshell-initialized"
PREFIX_MARKER = ".sandbox-science-prefix/.sandbox-science-prefix-id"
CREATE_TIMEOUT = 1800.0
Create = Callable[[Settings, EnvironmentRequest], EnvironmentInfo]
Delete = Callable[[Settings, str], None]


def local_create(settings: Settings, request: EnvironmentRequest) -> EnvironmentInfo:
    """Create the environment through the worker serving beside this command
    (``POST /v1/environments``), which waits until its sandbox is ready."""
    status, body = local_request(
        settings,
        "POST",
        "/v1/environments",
        request.model_dump_json().encode(),
        CREATE_TIMEOUT,
    )
    if status != 201:
        # 200 is an environment that existed: never the fresh one a restore needs.
        raise RecordError(f"the worker did not create a new environment ({status}: {body[:300]!r})")
    return EnvironmentInfo.model_validate_json(body)


def local_delete(settings: Settings, environment_id: str) -> None:
    """Delete the environment through the worker serving beside this command."""
    path = f"/v1/environments/{quote(environment_id, safe='')}"
    status, body = local_request(settings, "DELETE", path, b"", CREATE_TIMEOUT)
    if status != 200:
        raise RecordError(f"the worker did not delete the environment ({status}: {body[:300]!r})")


def _known(settings: Settings, environment_id: str) -> EnvironmentInfo | None:
    path = settings.state_dir / "environments.sqlite3"
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT substr(document,1,65537) FROM environments WHERE environment_id=?",
            (environment_id,),
        ).fetchone()
    if row is None:
        return None
    if len(str(row[0]).encode()) > 65536:
        raise RecordError(f"environment {environment_id}'s record exceeds its bound")
    strict_loads(str(row[0]))
    return EnvironmentInfo.model_validate_json(str(row[0]))


def _executions(settings: Settings, environment_id: str) -> int:
    """Executions the worker records in the environment, under any of its sandboxes."""
    path = settings.state_dir / "worker.sqlite3"
    if not path.is_file():
        return 0
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return int(
            db.execute(
                "SELECT count(*) FROM executions WHERE environment_id=?", (environment_id,)
            ).fetchone()[0]
        )


def plan(
    settings: Settings,
    manifest: str,
    target: str,
    kept: Storage,
    actor: str,
    *,
    create: Create = local_create,
    suspend: Suspend = local_suspend,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> RestorePlan:
    """Check the kept checkpoint, create the target and hold it stopped; the Job's input."""
    records = CheckpointRecords(settings.state_dir)
    try:
        found = next((c for c in records.checkpoints() if c.manifest_sha256 == manifest), None)
        if found is None:
            raise RecordError("the worker has no checkpoint with that manifest")
        if found.state != "kept":
            raise RecordError(f"checkpoint {manifest} is {found.state}, not kept")
        recorded = records.receipt(manifest)
    finally:
        records.close()
    source = recorded.source
    # Before anything is created: the kept copy must still be exactly what was verified.
    with tree_index() as tree:
        receipt = checkpoint_remote.verify(
            kept, manifest, source, tree=tree, timeout=timeout, clock=clock
        )
        expected = tree.digest()
        if tree.has(MARKER):
            raise RecordError(f"the checkpoint has an entry {MARKER!r} a restore cannot write")
    if receipt != recorded:
        raise RecordError("the kept copy differs from the verified checkpoint")
    existing = _known(settings, target)
    if existing is not None and existing.state is not EnvironmentState.DELETED:
        raise RecordError(
            f"environment {target} exists ({existing.state.value}): a restore goes into a new"
            " environment or one the worker records as deleted"
        )
    if existing is not None and existing.project_id != source.project_id:
        raise RecordError(f"environment {target} belonged to project {existing.project_id}")
    before = _executions(settings, target)
    info = create(
        settings,
        EnvironmentRequest(
            environment_id=target,
            project_id=source.project_id,
            profile=source.profile,
            policy=source.policy,
        ),
    )
    if info.kind != "session" or info.state not in (
        EnvironmentState.READY,
        EnvironmentState.STOPPED,
    ):
        raise RecordError(f"the created environment is a {info.kind} in state {info.state.value}")
    restored = RestorePlan(
        restore_id=f"rst-{secrets.token_hex(12)}",
        checkpoint_id=found.checkpoint_id,
        manifest_sha256=manifest,
        source=source,
        target=retained_source(settings, target, source.architecture, fresh=True),
        tree_sha256=expected,
        planned_at=clock.now(),
    )
    records = CheckpointRecords(settings.state_dir)
    try:
        # Held first: the sandbox must never start on a partly written workspace.
        records.place_restore(restored, before, clock.now(), actor)
        try:
            try:
                asyncio.run(confirm_stopped(settings, restored.target))
            except ValueError:
                if not suspend(settings, target):
                    raise RecordError(
                        "the new environment is in use; something else is using its ID"
                    ) from None
                asyncio.run(confirm_stopped(settings, restored.target))
            _still_fresh(settings, restored, before)
        except BaseException as error:
            # Nothing was written yet: the target may run as the fresh environment it is.
            records.finish_restore(
                restored.restore_id, "cancelled", clock.now(), actor, {"error": str(error)[:500]}
            )
            raise
    finally:
        records.close()
    return restored


def _still_fresh(settings: Settings, restore: RestorePlan, executions: int) -> None:
    target = restore.target
    if retained_source(settings, target.environment_id, target.architecture, fresh=True) != target:
        raise RecordError("the target environment's binding changed")
    if _executions(settings, target.environment_id) != executions:
        raise RecordError("something ran in the target environment")


def resume(
    settings: Settings, restore_id: str, actor: str, *, clock: Clock = SYSTEM_CLOCK
) -> RestorePlan:
    """An unfinished restore's plan again, for another Job, once its target is still the
    stopped sandbox the plan created with nothing run in it; journaled as a retry."""
    records = CheckpointRecords(settings.state_dir)
    try:
        found = records.restore(restore_id)
        if found.state not in ("planned", "failed"):
            raise RecordError(f"restore {restore_id} is {found.state}")
        _still_fresh(settings, found.plan, found.executions)
        asyncio.run(confirm_stopped(settings, found.plan.target))
        records.operation(clock.now(), actor, "restore", restore_id, "retry", {})
        return found.plan
    finally:
        records.close()


def attempt(settings: Settings, restore_id: str, actor: str, *, clock: Clock = SYSTEM_CLOCK) -> str:
    """A new Job attempt's nonce: the Job prints it beside its tree, and only the latest
    attempt, with no failure recorded after it, is verified."""
    records = CheckpointRecords(settings.state_dir)
    try:
        return records.attempt(restore_id, clock.now(), actor)
    finally:
        records.close()


def verify(
    settings: Settings,
    restore_id: str,
    nonce: str,
    tree: str,
    actor: str,
    *,
    clock: Clock = SYSTEM_CLOCK,
) -> RestorePlan:
    """Let the target start once the tree on its volume is the checkpoint's: the report
    must come from the restore's current Job attempt, its digest must be the one the
    worker computed from the kept copy, and the target must still be the stopped, fresh
    sandbox the plan created. A mismatch is recorded as a failure, which keeps the target
    held and voids the attempt."""
    records = CheckpointRecords(settings.state_dir)
    try:
        found = records.restore(restore_id)
        if found.state not in ("planned", "failed"):
            raise RecordError(f"restore {restore_id} is {found.state}")
        if found.attempt is None or not secrets.compare_digest(found.attempt, nonce):
            raise RecordError("this report is not from the restore's current Job attempt")
        try:
            _still_fresh(settings, found.plan, found.executions)
            asyncio.run(confirm_stopped(settings, found.plan.target))
            if tree != found.tree_sha256:
                raise RecordError("the restored tree differs from the checkpoint")
        except (RecordError, ValueError) as error:
            records.finish_restore(
                restore_id, "failed", clock.now(), actor, {"error": str(error)[:500]}
            )
            raise
        records.finish_restore(
            restore_id, "restored", clock.now(), actor, {"attempt": nonce}, attempt=nonce
        )
        return found.plan
    finally:
        records.close()


def record(
    settings: Settings,
    restore_id: str,
    phase: Literal["job", "failed"],
    actor: str,
    detail: dict[str, object],
    *,
    clock: Clock = SYSTEM_CLOCK,
) -> None:
    """Journal a restore Job (``job``), or a failure outside the worker's own checks."""
    records = CheckpointRecords(settings.state_dir)
    try:
        found = records.restore(restore_id)
        if phase == "job":
            if found.state not in ("planned", "failed"):
                raise RecordError(f"restore {restore_id} is {found.state}")
            records.operation(clock.now(), actor, "restore", restore_id, "job", detail)
        else:
            records.finish_restore(restore_id, "failed", clock.now(), actor, detail)
    finally:
        records.close()


def discard(
    settings: Settings,
    restore_id: str,
    actor: str,
    reason: str,
    *,
    delete: Delete = local_delete,
    clock: Clock = SYSTEM_CLOCK,
) -> bool:
    """End an unfinished restore, deleting its target environment if the environment is
    still bound to the sandbox the restore created (its workspace may hold a partial tree,
    so it never runs); an environment bound to another sandbox since, or gone, is left
    alone and only the restore ends. Whether the target was deleted."""
    records = CheckpointRecords(settings.state_dir)
    try:
        found = records.restore(restore_id)
        if found.state not in ("planned", "failed", "discarding"):
            raise RecordError(f"restore {restore_id} is {found.state}")
        target = found.plan.target
        info = _known(settings, target.environment_id)
        owned = (
            info is not None
            and info.state is not EnvironmentState.DELETED
            and (info.sandbox, info.sandbox_id) == (target.sandbox, target.sandbox_id)
        )
        if owned:
            # Still held, but deletable: the deletion guard refuses only planned and failed.
            records.finish_restore(
                restore_id, "discarding", clock.now(), actor, {"reason": reason[:500]}
            )
            delete(settings, target.environment_id)
        records.finish_restore(
            restore_id,
            "cancelled",
            clock.now(),
            actor,
            {"reason": reason[:500], "deleted": owned},
        )
        return owned
    finally:
        records.close()


# The restore Job


def _read_marker(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read(64).decode(errors="replace").strip()


def write(
    root: Path,
    restore: RestorePlan,
    storage: Storage,
    *,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> tuple[str, CheckpointReceipt | None]:
    """Empty the target volume and write the checkpoint into it; the tree digest of what
    is then on the volume, and the receipt. Refuses a volume that is neither OpenShell's
    seed of a fresh workspace (its sentinel, no prefix identity) nor this restore's own
    earlier attempt (its marker), unless it already holds exactly the plan's tree (an
    earlier attempt finished writing): then it only reports it, with no receipt. Refuses
    a volume whose root another user owns: the Job must write as the sandbox's user."""
    owner = os.stat(root, follow_symlinks=False).st_uid
    if owner != os.getuid():
        raise ValueError(
            f"the target volume belongs to uid {owner} and this Job runs as uid"
            f" {os.getuid()}: set [checkpoint] restore_run_as_user to the sandbox's user"
        )
    marker = root / MARKER
    if marker.is_symlink() or (marker.exists() and _read_marker(marker) != restore.restore_id):
        raise ValueError("the target volume carries another restore's marker")
    if not marker.exists():
        if os.path.lexists(root / PREFIX_MARKER):
            if describe(root) == restore.tree_sha256:
                return restore.tree_sha256, None
            raise ValueError(
                "the target volume is not a fresh workspace: its prefix has an identity"
            )
        if not os.path.lexists(root / SEED_SENTINEL):
            raise ValueError("the target volume is not an initialized workspace")
        install(marker, restore.restore_id.encode() + b"\n")
    clear(root, keep=frozenset({MARKER}))
    receipt = checkpoint_remote.extract(
        storage, restore.manifest_sha256, restore.source, root, timeout=timeout, clock=clock
    )
    check_prefix(root, restore.source)
    sync(root)
    tree = describe(root, exclude=frozenset({MARKER}))
    marker.unlink()
    sync(root)
    return tree, receipt
