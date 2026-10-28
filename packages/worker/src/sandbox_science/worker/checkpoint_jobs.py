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
* ``record_volume`` and the records' ``forget`` journal every deletion, intent first, and
  refuse what the worker's records forbid.
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
            receipt = checkpoint_remote.verify(
                storage, manifest, source, timeout=timeout, clock=clock
            )
            if receipt.capture != hold.plan.capture:
                raise RecordError("the checkpoint was captured otherwise than planned")
            if not (
                hold.placed_at - CLOCK_SKEW
                <= receipt.created_at
                <= hold.plan.hold_expires_at + CLOCK_SKEW
            ):
                raise RecordError("the checkpoint was not captured while its hold held")
            records.verified(hold.plan, manifest, receipt, clock.now(), actor)
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
    if not without_checkpoint and not any(
        c.sandbox == sandbox and c.state == "kept" for c in found.checkpoints
    ):
        reasons.append("it has no verified checkpoint (pass --without-checkpoint to delete anyway)")
    return reasons


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
