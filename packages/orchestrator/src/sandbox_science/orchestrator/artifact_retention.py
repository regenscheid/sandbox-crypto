# SPDX-License-Identifier: MIT
"""Exact, endpoint-bound delivery of durable control artifact receipts."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass

from pydantic import TypeAdapter

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.artifact_inventory import ArtifactPublicationSource, ArtifactReceipt
from sandbox_science.contracts.artifact_retention import (
    MAX_RETENTION_ACK_BYTES,
    ArtifactRetentionAcknowledgement,
)
from sandbox_science.contracts.canonical import canonical_json
from sandbox_science.contracts.model import StrictContract

from .aio import threaded
from .control import ControlStore
from .documents.errors import CONFLICT, INVALID_REQUEST, DocumentError
from .worker_client import WorkerClient

_log = logging.getLogger(__name__)
_IDENTIFIER = TypeAdapter[str](Identifier)


class _PublicationRequest(StrictContract):
    source: ArtifactPublicationSource
    path: str
    media_type: str | None


def _bounded(length: object, body: object, bound: int) -> bytes:
    if type(length) is not int or not 0 < length <= bound or type(body) is not bytes:
        raise DocumentError(INVALID_REQUEST, "invalid artifact retention record")
    if len(body) != length:
        raise DocumentError(INVALID_REQUEST, "truncated artifact retention record")
    return body


def acknowledgement(receipt: ArtifactReceipt) -> ArtifactRetentionAcknowledgement:
    """A retry-stable identity of the complete snapshot and retained control revision."""
    if receipt.state != "recorded" or receipt.worker_snapshot is None or receipt.revision is None:
        raise DocumentError(INVALID_REQUEST, "only retained artifact receipts can be acknowledged")
    fields = {
        "snapshot": receipt.worker_snapshot.model_dump(mode="json"),
        "revision": receipt.revision.model_dump(mode="json"),
    }
    identity = hashlib.sha256(b"artifact-retention-v1\x00" + canonical_json(fields)).hexdigest()
    result = ArtifactRetentionAcknowledgement(
        acknowledgement_id="retain-" + identity,
        snapshot=receipt.worker_snapshot,
        revision=receipt.revision,
    )
    if len(result.model_dump_json().encode()) > MAX_RETENTION_ACK_BYTES:
        raise DocumentError(INVALID_REQUEST, "artifact retention acknowledgement exceeds its bound")
    return result


def enqueue(
    db: sqlite3.Connection, source: ArtifactPublicationSource, receipt: ArtifactReceipt
) -> None:
    """Use only inside the successful content/revision/receipt commit transaction."""
    ack = acknowledgement(receipt)
    if ack.snapshot.project_id != source.context.project_id:
        raise DocumentError(CONFLICT, "artifact retention project differs from publication")
    db.execute(
        "INSERT INTO artifact_retention_outbox "
        "(project,actor,operation,endpoint,acknowledgement) VALUES (?,?,?,?,?)",
        (
            source.context.project_id,
            source.context.session_id,
            receipt.operation_id,
            source.worker_endpoint_sha256,
            ack.model_dump_json(),
        ),
    )


@dataclass(frozen=True)
class PendingRetention:
    sequence: int
    acknowledgement: ArtifactRetentionAcknowledgement


@dataclass(frozen=True)
class RetentionDrain:
    """A bounded page; after permits an operator to process past a failed delivery."""

    delivered: int = 0
    failed: int = 0
    after: int = 0


class RetentionOutbox:
    """A local repository; no network I/O happens while a SQL transaction is open."""

    def __init__(self, control: ControlStore) -> None:
        self.control = control

    def pending(
        self,
        project: str,
        endpoint: str,
        *,
        after: int = 0,
        limit: int = 8,
        operation: tuple[str, str] | None = None,
    ) -> tuple[PendingRetention, ...]:
        _IDENTIFIER.validate_python(project)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid artifact retention page")
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT b.sequence,"
                "length(CAST(b.actor AS BLOB)),substr(CAST(b.actor AS BLOB),1,129),"
                "length(CAST(b.operation AS BLOB)),substr(CAST(b.operation AS BLOB),1,129),"
                "length(CAST(o.state AS BLOB)),substr(CAST(o.state AS BLOB),1,9),"
                "length(CAST(b.acknowledgement AS BLOB)),"
                "substr(CAST(b.acknowledgement AS BLOB),1,16385),"
                "length(CAST(o.receipt AS BLOB)),substr(CAST(o.receipt AS BLOB),1,16385),"
                "length(CAST(o.request AS BLOB)),substr(CAST(o.request AS BLOB),1,8193) "
                "FROM artifact_retention_outbox b JOIN artifact_operations o "
                "ON (o.project,o.actor,o.operation)=(b.project,b.actor,b.operation) "
                "WHERE b.project=? AND b.endpoint=? AND b.delivered=0 AND b.sequence>? "
                "AND (? IS NULL OR (b.actor=? AND b.operation=?)) ORDER BY b.sequence LIMIT ?",
                (
                    project,
                    endpoint,
                    after,
                    None if operation is None else operation[0],
                    None if operation is None else operation[0],
                    None if operation is None else operation[1],
                    limit,
                ),
            ).fetchall()
        results: list[PendingRetention] = []
        for (
            sequence,
            actor_len,
            actor_body,
            op_len,
            op_body,
            state_len,
            state_body,
            a_len,
            a_body,
            r_len,
            r_body,
            q_len,
            q_body,
        ) in rows:
            actor = _IDENTIFIER.validate_python(
                _bounded(actor_len, actor_body, 128).decode("ascii")
            )
            identity = _IDENTIFIER.validate_python(_bounded(op_len, op_body, 128).decode("ascii"))
            state = _bounded(state_len, state_body, 8).decode("ascii")
            ack = ArtifactRetentionAcknowledgement.model_validate_json(
                _bounded(a_len, a_body, MAX_RETENTION_ACK_BYTES)
            )
            receipt = ArtifactReceipt.model_validate_json(_bounded(r_len, r_body, 16384))
            request = _PublicationRequest.model_validate_json(_bounded(q_len, q_body, 8192))
            if (
                state != "done"
                or receipt.operation_id != identity
                or request.source.context.project_id != project
                or request.source.context.session_id != actor
                or request.source.worker_endpoint_sha256 != endpoint
                or request.path != ack.snapshot.source_path
                or ack.snapshot.project_id != project
                or acknowledgement(receipt) != ack
            ):
                raise DocumentError(CONFLICT, "stored artifact retention receipt differs")
            results.append(PendingRetention(sequence, ack))
        return tuple(results)

    def settle(self, project: str, endpoint: str, pending: PendingRetention, *, error: str) -> None:
        """Only the unchanged pending body can receive this exact delivery outcome."""
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE artifact_retention_outbox SET delivered=?,"
                "attempts=min(attempts+1,4611686018427387903),last_error=? "
                "WHERE project=? AND endpoint=? AND sequence=? AND delivered=0 "
                "AND acknowledgement=?",
                (
                    int(not error),
                    error[:256],
                    project,
                    endpoint,
                    pending.sequence,
                    pending.acknowledgement.model_dump_json(),
                ),
            )


async def drain(
    outbox: RetentionOutbox,
    client: WorkerClient,
    project: str,
    *,
    limit: int = 8,
    after: int = 0,
    operation: tuple[str, str] | None = None,
    timeout: float = 5.0,
) -> RetentionDrain:
    """Attempt a page once, stopping on failure; never hold up research for retries.

    HTTP attempts share a five-second budget measured by the client's clock. A
    failed page remains pending and has an explicit cursor for later manual processing.
    Cancellation may lose a receipt, but can only replay the same immutable ACK.
    """
    endpoint = client.base_url
    digest = hashlib.sha256(endpoint.encode()).hexdigest()
    delivered = 0
    try:
        if not 0 < timeout <= 5:
            raise ValueError("invalid artifact retention timeout")
        deadline = client.clock.monotonic() + timeout
        rows = await threaded(
            outbox.pending, project, digest, limit=limit, after=after, operation=operation
        )
        for row in rows:
            remaining = deadline - client.clock.monotonic()
            if remaining <= 0:
                break
            after = row.sequence
            try:
                if client.base_url != endpoint:
                    raise ValueError("artifact retention endpoint changed")
                response = await client.acknowledge_artifact_retention(
                    row.acknowledgement, timeout=min(remaining, 5)
                )
                if client.base_url != endpoint or response != row.acknowledgement:
                    raise ValueError("artifact retention acknowledgement differs")
            except Exception as error:
                # Cleanup delivery cannot turn a committed publication into a held
                # scientific result. Keep its original intent and a bounded reason.
                reason = "worker acknowledgement failed: " + type(error).__name__
                await threaded(outbox.settle, project, digest, row, error=reason)
                _log.warning("artifact retention remains pending (%s)", type(error).__name__)
                return RetentionDrain(delivered, 1, after)
            await threaded(outbox.settle, project, digest, row, error="")
            delivered += 1
        return RetentionDrain(delivered, 0, after)
    except Exception as error:
        _log.error("artifact retention outbox remains pending (%s)", type(error).__name__)
        return RetentionDrain(delivered, 1, after)
