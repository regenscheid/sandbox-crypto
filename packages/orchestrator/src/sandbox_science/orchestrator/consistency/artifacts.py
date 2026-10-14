# SPDX-License-Identifier: MIT
"""Durable explicit-publication evidence; no worker, parser or path-read authority."""

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field, ValidationError

from sandbox_science.contracts.artifact_inventory import (
    ArtifactEvent,
    ArtifactLink,
    ArtifactPublicationSource,
    ArtifactRevisionRef,
    ArtifactSnapshot,
)
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    DocumentError,
)

from .store import Json, stored_json

if TYPE_CHECKING:
    from .store import ReviewStore

TEXT_BYTES: Final = 64 * 1024
PAGE: Final = 10
_TEXT_TYPES = frozenset(
    ("text/plain", "text/markdown", "text/csv", "text/tab-separated-values", "application/json")
)


class ArtifactProjection(Contract):
    """Exact source copy or an explicit reason why content is not available."""

    projection: Literal[1] = 1
    reference: ArtifactRevisionRef
    source: ArtifactPublicationSource
    availability: Literal["text", "binary", "oversize", "non_utf8", "unavailable"]
    text: str | None = Field(default=None, max_length=TEXT_BYTES)
    reason: str = Field(default="", max_length=256)


def artifact_text(locator: Json, payload: Json) -> str | None:
    """Verify copied text and return source characters, without JSON escaping."""
    try:
        projection = ArtifactProjection.model_validate(payload)
        reference = ArtifactRevisionRef.model_validate(locator["artifact"])
        if reference != projection.reference:
            raise ValueError("artifact source reference differs")
        if projection.availability != "text":
            if projection.text is not None:
                raise ValueError("unavailable artifact contains source text")
            return None
        if projection.text is None:
            raise ValueError("missing copied artifact text")
        raw = projection.text.encode("utf-8")
        if (
            len(raw) > TEXT_BYTES
            or len(raw) != reference.size
            or hashlib.sha256(raw).hexdigest() != reference.sha256
        ):
            raise ValueError("copied artifact text fails integrity verification")
        return projection.text
    except (ValidationError, KeyError, ValueError, UnicodeError):
        raise DocumentError(CONFLICT, "invalid pinned artifact text evidence") from None


def project_artifact(
    store: ReviewStore,
    project: str,
    reference: ArtifactRevisionRef,
    source: ArtifactPublicationSource | None = None,
    path: str | None = None,
) -> tuple[Json, Json, bool]:
    """Copy bounded exact bytes from the control inventory, never a mutable path."""
    inventory = store.artifacts
    if inventory is None or reference.project_id != project:
        raise DocumentError(INVALID_REQUEST, "artifact evidence inventory is unavailable")
    revision = inventory.resolve(
        project,
        ArtifactLink(
            project_id=project,
            artifact_id=reference.artifact_id,
            mode="pinned",
            revision=reference.revision,
            sha256=reference.sha256,
        ),
    )
    if revision.reference != reference:
        raise DocumentError(CONFLICT, "artifact reference differs from canonical revision")
    source = source or revision.publication
    if source.context.project_id != project:
        raise DocumentError(CONFLICT, "artifact publisher project differs")
    if path is not None:
        expected_path = (
            path if path.startswith("shared/") else (f"sessions/{source.context.session_id}/{path}")
        )
        if revision.path != expected_path:
            raise DocumentError(CONFLICT, "artifact event path/session grant differs")
    available: Literal["text", "binary", "oversize", "non_utf8", "unavailable"] = "text"
    reason, copied = "", None
    parts = reference.media_type.casefold().split(";")
    params = [p.strip().replace('"', "") for p in parts[1:]]
    if parts[0].strip() not in _TEXT_TYPES or any(
        p not in ("charset=utf-8", "charset=utf8", "charset=us-ascii") for p in params
    ):
        available, reason = "binary", "media type is outside the raw text evidence allowlist"
    elif reference.size > TEXT_BYTES:
        available, reason = "oversize", "artifact exceeds the 64 KiB source copy limit"
    else:
        try:
            raw = inventory.content(project, reference, max_bytes=TEXT_BYTES)
            copied = raw.decode("utf-8", errors="strict")
            if any(ord(c) < 32 and c not in "\t\r\n" for c in copied):
                available, reason, copied = (
                    "binary",
                    "source contains binary control characters",
                    None,
                )
        except UnicodeError:
            available, reason, copied = "non_utf8", "source is not strict UTF-8", None
        except DocumentError:
            available, reason, copied = (
                "unavailable",
                "retained bytes failed exact bounded verification",
                None,
            )
    payload = ArtifactProjection(
        reference=reference,
        source=source,
        availability=available,
        text=copied,
        reason=reason,
    ).model_dump(mode="json")
    locator = {"artifact": reference.model_dump(mode="json"), "run": source.run_id}
    artifact_text(locator, payload)
    return locator, payload, available == "text"


def ingest_artifacts(
    store: ReviewStore, project: str, session: str, *, drain: bool = False
) -> Json:
    """Consume at most 10/100 pinned events and 10/100 finite uncertainty keys.

    Every page inserts observations and commits its exact source cursor atomically.
    A failed/cancelled caller can replay the same page after restart. Unresolved
    operations are observed once, not used as a permanent global coverage veto.
    """
    store.scope(project, session)
    inventory = store.artifacts
    if inventory is None:
        return {"cursor": 0, "target": 0, "complete": True, "enabled": False}
    with store.documents.control.read() as tx:
        db = tx.sql
        target = db.execute(
            "SELECT coalesce(max(seq),0) FROM artifact_events WHERE project=?",
            (project,),
        ).fetchone()[0]
    after, unknown_more = 0, False
    for _ in range(10 if drain else 1):
        with store.documents.control.read() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT cursor FROM review_artifact_cursors WHERE project=? AND session=?",
                (project, session),
            ).fetchone()
            after = row[0] if row else 0
            actual_head = db.execute(
                "SELECT coalesce(max(seq),0) FROM artifact_events WHERE project=?",
                (project,),
            ).fetchone()[0]
            if type(after) is not int or not 0 <= after <= actual_head:
                raise DocumentError(CONFLICT, "artifact evidence cursor is invalid")
            # A coexisting ingestor may already have covered a newer prefix than
            # this call pinned. This is progress, not a turn-aborting conflict.
            target = max(target, after)
            events = db.execute(
                "SELECT seq,record FROM artifact_events "
                "WHERE project=? AND seq>? AND seq<=? ORDER BY seq LIMIT 10",
                (project, after, target),
            ).fetchall()
            unknown = db.execute(
                "SELECT operation,request,digest,state,worker,receipt "
                "FROM artifact_operations o WHERE project=? AND actor=? AND state!='done' "
                "AND NOT EXISTS(SELECT 1 FROM review_feed f WHERE f.project=o.project "
                "AND f.session=o.actor AND f.source_key='artifact:operation:'||o.operation) "
                "ORDER BY o.rowid LIMIT 11",
                (project, session),
            ).fetchall()
        prepared: list[tuple[str, Json, Json, bool, int]] = []
        for seq, raw in events:
            try:
                event = ArtifactEvent.model_validate(stored_json(raw))
            except ValidationError:
                raise DocumentError(CONFLICT, "invalid stored artifact event") from None
            if (
                event.project_id != project
                or event.seq != seq
                or event.revision.project_id != project
            ):
                raise DocumentError(CONFLICT, "artifact event scope differs")
            if event.source.context.session_id != session:
                continue
            if (
                event.worker_snapshot.project_id != project
                or event.worker_snapshot.sha256 != event.revision.sha256
                or event.worker_snapshot.size != event.revision.size
            ):
                raise DocumentError(CONFLICT, "artifact event snapshot differs")
            locator, payload, complete = project_artifact(
                store, project, event.revision, event.source, event.worker_snapshot.source_path
            )
            prepared.append(
                (
                    f"artifact:event:{seq}",
                    locator,
                    payload,
                    complete,
                    int(event.kind == "artifact.revision_created"),
                )
            )
        uncertain: list[tuple[tuple[object, ...], Json, Json]] = []
        for op in unknown[:PAGE]:
            operation, raw, sha, _phase, _worker, _receipt = op
            request = stored_json(raw)
            try:
                source = ArtifactPublicationSource.model_validate(request["source"])
                encoded, expected = inventory.publication_request(
                    source, request["path"], request["media_type"]
                )
            except (ValidationError, KeyError, ValueError):
                raise DocumentError(CONFLICT, "invalid unresolved artifact intent") from None
            if (source.context.project_id, source.context.session_id) != (project, session):
                raise DocumentError(CONFLICT, "unresolved artifact scope differs")
            if expected != sha or encoded != raw:
                raise DocumentError(CONFLICT, "unresolved artifact fingerprint differs")
            with store.documents.control.read() as tx:
                db = tx.sql
                state = inventory.find_operation(db, source, operation, expected)
            if state is None or state.state == "done":
                continue
            snapshot = None
            if state.worker is not None:
                snapshot = ArtifactSnapshot.model_validate(state.worker.model_dump())
                if snapshot.project_id != project or snapshot.source_path != request["path"]:
                    raise DocumentError(CONFLICT, "unresolved worker snapshot scope differs")
            payload = {
                "publication_operation": operation,
                "availability": "known_worker_snapshot" if snapshot else "unknown_remote_outcome",
                "worker_snapshot": snapshot.model_dump(mode="json") if snapshot else None,
                "source": source.model_dump(mode="json"),
                "reason": "Control bytes are not retained; no source content is granted.",
            }
            uncertain.append((op, {"operation": operation, "run": source.run_id}, payload))
        end = events[-1][0] if events else after
        with store.documents.control.write() as tx:
            db = tx.sql
            db.execute(
                "INSERT OR IGNORE INTO review_artifact_cursors VALUES(?,?,0)", (project, session)
            )
            actual = db.execute(
                "SELECT cursor FROM review_artifact_cursors WHERE project=? AND session=?",
                (project, session),
            ).fetchone()[0]
            if actual != after:
                # Another ingestor owns this prefix; retry a fresh bounded page.
                continue
            for key, locator, payload, complete, count in prepared:
                store.record_observation(
                    db,
                    project,
                    session,
                    key,
                    "artifact",
                    locator,
                    payload,
                    complete=complete,
                    artifacts=count,
                    effects=True,
                )
            for op, locator, payload in uncertain:
                current = db.execute(
                    "SELECT operation,request,digest,state,worker,receipt "
                    "FROM artifact_operations WHERE project=? AND actor=? AND operation=?",
                    (project, session, op[0]),
                ).fetchone()
                key = f"artifact:operation:{op[0]}"
                if (
                    current == op
                    and not db.execute(
                        "SELECT 1 FROM review_feed WHERE project=? AND session=? AND source_key=?",
                        (project, session, key),
                    ).fetchone()
                ):
                    store.record_observation(
                        db,
                        project,
                        session,
                        key,
                        "artifact",
                        locator,
                        payload,
                        complete=False,
                        effects=True,
                    )
            db.execute(
                "UPDATE review_artifact_cursors SET cursor=? WHERE project=? AND session=?",
                (end, project, session),
            )
        after = end
        unknown_more = len(unknown) > PAGE
        if after >= target and not unknown_more:
            break
    # Recheck unseen uncertainty after commit, including concurrently accepted intents.
    with store.documents.control.read() as tx:
        db = tx.sql
        unknown_more = (
            db.execute(
                "SELECT 1 FROM artifact_operations o WHERE project=? AND actor=? AND state!='done' "
                "AND NOT EXISTS(SELECT 1 FROM review_feed f WHERE f.project=o.project "
                "AND f.session=o.actor AND "
                "f.source_key='artifact:operation:'||o.operation) LIMIT 1",
                (project, session),
            ).fetchone()
            is not None
        )
    return {
        "cursor": after,
        "target": target,
        "complete": after >= target and not unknown_more,
        "enabled": True,
    }


class ArtifactConsumer:
    """Own admitted SQL work through cancellation and drain before the borrowed store closes."""

    def __init__(self, store: ReviewStore, project: str, session: str) -> None:
        self.store, self.project, self.session = store, project, session
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="artifact-evidence")
        self._work: set[asyncio.Task[Json]] = set()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    async def ingest(self, *, drain: bool = False) -> Json:
        """Bound queued work; cancellation detaches the caller, not actual SQL ownership."""
        if self._closing or len(self._work) >= 8:
            return {"complete": False, "enabled": True, "held": "consumer admission bound"}
        task = asyncio.create_task(self._ingest(drain))
        self._work.add(task)
        task.add_done_callback(self._finished)
        return await asyncio.shield(task)

    def _finished(self, task: asyncio.Task[Json]) -> None:
        self._work.discard(task)
        if not task.cancelled():
            # A detached caller may never retrieve a later SQL exception. Taking
            # it here does not alter what a live awaiting caller receives.
            task.exception()

    async def _ingest(self, drain: bool) -> Json:
        return await asyncio.get_running_loop().run_in_executor(
            self._executor,
            lambda: ingest_artifacts(self.store, self.project, self.session, drain=drain),
        )

    async def aclose(self) -> None:
        """Drain all actual transactions even if shutdown itself is repeatedly cancelled."""
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._drain())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _drain(self) -> None:
        if self._work:
            await asyncio.gather(*tuple(self._work), return_exceptions=True)
        self._executor.shutdown(wait=True)
