# SPDX-License-Identifier: MIT
"""The worker's part in checkpoint Jobs and cleanup (docs/WORKER.md#checkpoint-jobs).

The operator runs these in the worker's container (``kubectl exec``), beside the serving
worker, which keeps no Kubernetes rights:

* ``plan`` holds an environment stopped, suspending it through the worker's own API if it
  runs, and hands out the retained binding a checkpoint Job writes into its archive;
* ``verify`` reads an uploaded checkpoint back in full against the worker's binding and
  records it: ``verified`` in the upload bucket, ``kept`` (counted) in the kept bucket;
* ``inventory`` lists what a cleanup decides with: environments, hand-backs, checkpoints
  and holds;
* ``record_volume`` and ``forget`` journal every deletion, intent first, and refuse what the
  worker's records forbid; ``forget --retire`` removes the last checkpoints of a deleted
  environment whose volume is gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import secrets
import sqlite3
import ssl
from collections.abc import Callable
from contextlib import closing
from datetime import timedelta
from typing import Literal
from urllib.parse import quote

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Clock,
    EnvironmentInfo,
    EnvironmentState,
    SuspendResult,
)
from sandbox_science.contracts.jsonio import strict_loads
from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.contracts.workspace_checkpoint import (
    CheckpointInventory,
    CheckpointPlan,
    CheckpointReceipt,
    InventoryCheckpoint,
    InventoryEnvironment,
)

from . import checkpoint_remote
from .checkpoint_operator import Architecture, confirm_stopped, retained_source
from .checkpoint_records import DEFAULT_KEEP, CheckpointRecords, RecordError
from .checkpoint_s3 import Storage
from .settings import Settings

HOLD_SECONDS = 6 * 3600
"""How long a plan holds its environment stopped unless verified or released first."""
CLOCK_SKEW = timedelta(minutes=5)
"""Tolerance between the Job's node clock and the worker's, for the capture's time."""
MAX_RESPONSE = 1 << 20
Suspend = Callable[[Settings, str], bool]


def local_suspend(settings: Settings, environment_id: str, timeout: float = 300) -> bool:
    """Ask the worker serving beside this command to suspend the environment, as an
    operator would through ``POST /v1/environments/{id}/suspend``, with the pool's own
    token. The TLS peer must present exactly the worker's certificate from the settings:
    only that process holds its key."""
    path = f"/v1/environments/{quote(environment_id, safe='')}/suspend"
    connection: http.client.HTTPConnection
    if settings.tls is None:
        connection = http.client.HTTPConnection("127.0.0.1", settings.listen_port, timeout=timeout)
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # the exact certificate is compared below
        connection = http.client.HTTPSConnection(
            "127.0.0.1", settings.listen_port, timeout=timeout, context=context
        )
    with closing(connection):
        connection.connect()
        if settings.tls is not None:
            # The served certificate is the file's first; a chain may follow it.
            pem = settings.tls.certificate.read_text()
            start, end = pem.find(ssl.PEM_HEADER), pem.find(ssl.PEM_FOOTER)
            if not 0 <= start < end:
                raise RecordError("the worker's certificate file holds no certificate")
            expected = ssl.PEM_cert_to_DER_cert(pem[start : end + len(ssl.PEM_FOOTER)])
            socket = connection.sock
            if not isinstance(socket, ssl.SSLSocket) or socket.getpeercert(True) != expected:
                raise RecordError("the local worker presented another certificate")
        connection.request(
            "POST",
            path,
            body=b"",
            headers={
                "Authorization": f"Bearer {settings.token()}",
                PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE)
    if response.status != 200:
        raise RecordError(f"the worker did not suspend the environment ({response.status})")
    return SuspendResult.model_validate_json(body).suspended


def source_digest(plan: CheckpointPlan) -> str:
    return hashlib.sha256(plan.source.model_dump_json().encode()).hexdigest()


def _environment(settings: Settings, environment_id: str) -> EnvironmentInfo:
    path = settings.state_dir / "environments.sqlite3"
    if not path.is_file():
        raise RecordError("no environment records in the state directory")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT substr(document,1,65537) FROM environments WHERE environment_id=?",
            (environment_id,),
        ).fetchone()
    if row is None or len(str(row[0]).encode()) > 65536:
        raise RecordError(f"no environment {environment_id}")
    strict_loads(str(row[0]))
    return EnvironmentInfo.model_validate_json(str(row[0]))


def plan(
    settings: Settings,
    environment_id: str,
    architecture: Architecture,
    actor: str,
    *,
    hold_seconds: float = HOLD_SECONDS,
    suspend: Suspend = local_suspend,
    clock: Clock = SYSTEM_CLOCK,
) -> CheckpointPlan:
    """Hold the environment, have its sandbox stopped, and return the Job's input."""
    if not 60 <= hold_seconds <= 7 * 86400:
        raise RecordError("a checkpoint hold lasts between a minute and a week")
    source = retained_source(settings, environment_id, architecture)
    now = clock.now()
    draft = CheckpointPlan(
        checkpoint_id=f"ckpt-{secrets.token_hex(12)}",
        source=source,
        source_sha256="0" * 64,
        capture="suspended_pvc",
        hold_expires_at=now + timedelta(seconds=hold_seconds),
    )
    result = draft.model_copy(update={"source_sha256": source_digest(draft)})
    records = CheckpointRecords(settings.state_dir)
    try:
        # Held first: nothing may start the sandbox again once it is stopped.
        records.place(result, now, actor)
        try:
            try:
                asyncio.run(confirm_stopped(settings, source))
            except ValueError:
                if _environment(settings, environment_id).kind != "session":
                    raise RecordError(
                        "only a session environment is suspended for a checkpoint; this one "
                        "must already be stopped"
                    ) from None
                if not suspend(settings, environment_id):
                    raise RecordError(
                        "the environment is in use (a job, a publication or busy kernels); "
                        "try again later"
                    ) from None
                asyncio.run(confirm_stopped(settings, source))
            if retained_source(settings, environment_id, architecture) != source:
                raise RecordError("the environment's binding changed while it was planned")
        except BaseException as error:
            records.release(result.checkpoint_id, clock.now(), f"plan failed: {error}")
            raise
    finally:
        records.close()
    return result


def verify(
    settings: Settings,
    checkpoint_id: str,
    manifest: str,
    storage: Storage,
    stage: Literal["upload", "kept"],
    actor: str,
    *,
    keep: int = DEFAULT_KEEP,
    timeout: float = 3600,
    clock: Clock = SYSTEM_CLOCK,
) -> tuple[CheckpointReceipt, list[str]]:
    """Verify a checkpoint in ``storage`` and record it; the receipt and the manifests of
    this workspace's kept checkpoints beyond ``keep``, which an operator may remove."""
    records = CheckpointRecords(settings.state_dir)
    try:
        hold = records.hold(checkpoint_id)
        source = hold.plan.source
        if stage == "upload":
            if retained_source(settings, source.environment_id, source.architecture) != source:
                raise RecordError("the environment's binding changed since it was planned")
            names: list[str] = []
            receipt = checkpoint_remote.verify(
                storage, manifest, source, chunk_names=names, timeout=timeout, clock=clock
            )
            if receipt.capture != hold.plan.capture:
                raise RecordError("the checkpoint was captured otherwise than planned")
            # Within the hold as it held: from its placing to its expiry, or to its release
            # if it was lifted early (after which the sandbox may have run again).
            if not (
                hold.placed_at - CLOCK_SKEW
                <= receipt.created_at
                <= hold.plan.hold_expires_at + CLOCK_SKEW
            ) or (hold.released_at is not None and receipt.created_at > hold.released_at):
                raise RecordError("the checkpoint was not captured while its hold held")
            records.verified(hold.plan, manifest, receipt, clock.now(), actor, names)
            records.release(checkpoint_id, clock.now(), "verified")
        else:
            try:
                recorded = records.receipt(manifest)
            except RecordError:
                raise RecordError("verify the checkpoint in the upload bucket first") from None
            receipt = checkpoint_remote.verify(
                storage, manifest, source, timeout=timeout, clock=clock
            )
            if receipt != recorded:
                raise RecordError("the kept copy differs from the verified upload")
            records.kept(checkpoint_id, manifest, clock.now(), actor)
        return receipt, records.rotation(source.sandbox, keep)[1]
    finally:
        records.close()


def _children(settings: Settings) -> dict[str, Literal["pending", "retained"]]:
    path = settings.state_dir / "worker.sqlite3"
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        try:
            rows = db.execute(
                "SELECT environment_id, phase FROM child_environments"
                " WHERE phase IN ('collecting', 'retained')"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}  # a store from before child environments has none
    return {str(e): "pending" if p == "collecting" else "retained" for e, p in rows}


def inventory(
    settings: Settings, *, keep: int = DEFAULT_KEEP, clock: Clock = SYSTEM_CLOCK
) -> CheckpointInventory:
    path = settings.state_dir / "environments.sqlite3"
    if not path.is_file():
        raise RecordError("no environment records in the state directory")
    children = _children(settings)
    records = CheckpointRecords(settings.state_dir)
    try:
        starts = records.last_starts()
    finally:
        records.close()
    environments: list[InventoryEnvironment] = []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        for (document,) in db.execute(
            "SELECT substr(document,1,65537) FROM environments ORDER BY environment_id"
        ):
            if len(str(document).encode()) > 65536:
                raise RecordError("an environment record exceeds its bound")
            strict_loads(str(document))
            info = EnvironmentInfo.model_validate_json(str(document))
            environments.append(
                InventoryEnvironment(
                    environment_id=info.environment_id,
                    project_id=info.project_id,
                    session_id=info.session_id,
                    kind=info.kind,
                    state=info.state.value,
                    sandbox=info.sandbox,
                    sandbox_id=info.sandbox_id,
                    hand_back=children.get(info.environment_id),
                    last_started_at=starts.get(info.environment_id),
                )
            )
    records = CheckpointRecords(settings.state_dir)
    try:
        now = clock.now()
        return CheckpointInventory(
            generated_at=now,
            keep=keep,
            environments=tuple(environments),
            checkpoints=tuple(records.checkpoints()),
            holds=tuple(records.holds(now)),
        )
    finally:
        records.close()


def checkpoint_cover(found: CheckpointInventory, sandbox: str) -> str | None:
    """Why the workspace of ``sandbox`` has no current verified checkpoint, or None if it
    has one: a kept checkpoint newer than the last time its sandbox was started, since any
    work in it after a checkpoint needed a start."""
    kept = [c.created_at for c in found.checkpoints if c.sandbox == sandbox and c.state == "kept"]
    if not kept:
        return "it has no verified checkpoint"
    newest = max(kept)
    for info in found.environments:
        if info.sandbox == sandbox and info.last_started_at and info.last_started_at > newest:
            return (
                f"environment {info.environment_id} was started at "
                f"{info.last_started_at:%Y-%m-%d %H:%M} UTC, after its newest verified "
                f"checkpoint ({newest:%Y-%m-%d %H:%M} UTC)"
            )
    return None


def volume_refusals(
    found: CheckpointInventory, sandbox: str, *, without_checkpoint: bool
) -> list[str]:
    """Why the worker's records forbid deleting the volume of ``sandbox``; empty if none."""
    reasons: list[str] = []
    for info in found.environments:
        if info.sandbox != sandbox:
            continue
        if info.state != EnvironmentState.DELETED.value:
            reasons.append(f"environment {info.environment_id} is {info.state}")
        if info.hand_back is not None:
            reasons.append(f"environment {info.environment_id}'s hand-back is {info.hand_back}")
    if any(hold.sandbox == sandbox for hold in found.holds):
        reasons.append("a checkpoint holds it")
    if not without_checkpoint and (missing := checkpoint_cover(found, sandbox)) is not None:
        reasons.append(f"{missing} (pass --without-checkpoint for this volume to delete anyway)")
    return reasons


def retire_refusals(
    found: CheckpointInventory, checkpoint: InventoryCheckpoint, environment_id: str
) -> list[str]:
    """Why the worker's records forbid retiring ``checkpoint``, one of the latest kept
    checkpoints of ``environment_id``; empty if none. Retiring needs the environment
    recorded deleted and nothing referencing its workspace: no hand-back and no hold (no
    other environment can record its sandbox). Whether the volume is gone is the operator's survey
    (``--volume-gone``): the worker holds no Kubernetes right to look."""
    reasons: list[str] = []
    if checkpoint.environment_id != environment_id:
        reasons.append(
            f"it is a checkpoint of environment {checkpoint.environment_id}, not {environment_id}"
        )
    info = next((e for e in found.environments if e.environment_id == environment_id), None)
    if info is None:
        reasons.append(f"the worker has no record of environment {environment_id}")
    elif info.state != EnvironmentState.DELETED.value:
        reasons.append(f"environment {environment_id} is {info.state}")
    if info is not None and info.hand_back is not None:
        reasons.append(f"environment {environment_id}'s hand-back is {info.hand_back}")
    if any(
        hold.environment_id == environment_id or hold.sandbox == checkpoint.sandbox
        for hold in found.holds
    ):
        reasons.append("a checkpoint holds it")
    return reasons


def forget(
    settings: Settings,
    manifest: str,
    actor: str,
    *,
    volume_gone: bool,
    retire: str | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> InventoryCheckpoint:
    """Journal a checkpoint's deletion (``CheckpointRecords.forget``). With ``retire``,
    one of the latest kept checkpoints of that deleted environment, refused unless the
    operator found its volume gone and ``retire_refusals`` finds nothing."""
    if retire is not None:
        if not volume_gone:
            raise RecordError("retiring a checkpoint needs its volume gone (--volume-gone)")
        found = inventory(settings, clock=clock)
        checkpoint = next((c for c in found.checkpoints if c.manifest_sha256 == manifest), None)
        if checkpoint is None:
            raise RecordError("no checkpoint with that manifest")
        if reasons := retire_refusals(found, checkpoint, retire):
            raise RecordError("; ".join(reasons))
    records = CheckpointRecords(settings.state_dir)
    try:
        return records.forget(manifest, clock.now(), actor, volume_gone=volume_gone, retire=retire)
    finally:
        records.close()


def record_volume(
    settings: Settings,
    phase: Literal["intent", "done", "failed"],
    volume: str,
    sandbox: str | None,
    actor: str,
    *,
    without_checkpoint: bool,
    detail: dict[str, object],
    clock: Clock = SYSTEM_CLOCK,
) -> None:
    """Journal a volume deletion. Its intent is refused when the worker's own records
    still tie the volume to work; an unknown sandbox needs ``without_checkpoint``."""
    if phase == "intent":
        if sandbox is None:
            if not without_checkpoint:
                raise RecordError(
                    "a volume of no known sandbox has no checkpoint: pass --without-checkpoint"
                )
        elif reasons := volume_refusals(
            inventory(settings, clock=clock), sandbox, without_checkpoint=without_checkpoint
        ):
            raise RecordError("; ".join(reasons))
    records = CheckpointRecords(settings.state_dir)
    try:
        records.journal(
            clock.now(),
            actor,
            "volume",
            volume,
            phase,
            {**detail, "sandbox": sandbox, "without_checkpoint": without_checkpoint},
        )
    finally:
        records.close()


def printable_plan(result: CheckpointPlan) -> str:
    return json.dumps(result.model_dump(mode="json"), sort_keys=True)
