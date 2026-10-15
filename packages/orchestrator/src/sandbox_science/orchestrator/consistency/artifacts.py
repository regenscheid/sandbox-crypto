# SPDX-License-Identifier: MIT
"""Durable explicit-publication evidence; no worker, parser or path-read authority."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast

from pydantic import Field, ValidationError

from sandbox_science.contracts.artifact_inventory import (
    ArtifactEvent,
    ArtifactLink,
    ArtifactPublicationSource,
    ArtifactRevisionRef,
    ArtifactSnapshot,
)
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.aio import OwnedExecutor
from sandbox_science.orchestrator.control.feeds import FeedCursor
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    DocumentError,
)

from .store import Json, stored_json

if TYPE_CHECKING:
    from sandbox_science.orchestrator.artifacts import ArtifactInventory

    from .store import ReviewStore

TEXT_BYTES: Final = 64 * 1024
PAGE: Final = 10
ARTIFACT_FEED: Final = FeedCursor("artifact")
"""How far each session's reviewer has read the project's artifact events."""
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
    if revision.deleted:
        available, reason = "unavailable", "the file was deleted"
    elif not revision.content_retained:
        available, reason = "unavailable", f"the bytes were not kept ({revision.content_note})"
    elif parts[0].strip() not in _TEXT_TYPES or any(
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


def pinned_artifact(
    store: ReviewStore, project: str, artifact: str, revision: int, sha256: str | None
) -> tuple[Json, Json] | None:
    """An artifact revision a project ledger entry's evidence pins, copied as
    ``project_artifact`` copies one, whichever session published it (ledger pins are
    project-wide, CR-2 as amended): its locator and projection. None when there is no
    inventory, the revision is not the project's, or its text cannot be copied whole: an
    item that is not complete would leave the review's coverage incomplete, and the
    entry's evidence names the pin anyway."""
    inventory = store.artifacts
    if inventory is None or sha256 is None:
        return None
    try:
        found = inventory.resolve(
            project,
            ArtifactLink(
                project_id=project,
                artifact_id=artifact,
                mode="pinned",
                revision=revision,
                sha256=sha256,
            ),
        )
        locator, payload, ready = project_artifact(store, project, found.reference)
    except (DocumentError, ValidationError):
        return None
    return (locator, payload) if ready else None


@dataclass(frozen=True, slots=True)
class _Observation:
    """One review-feed observation a page of artifact evidence adds."""

    key: str
    locator: Json
    payload: Json
    complete: bool
    artifacts: int


@dataclass(frozen=True, slots=True)
class _Page:
    """A bounded page of a session's artifact evidence, read at ``after``."""

    after: int
    target: int
    events: list[tuple[int, str]]
    unknown: list[tuple[object, ...]]


_UNSEEN_OPERATIONS: Final = (
    "FROM artifact_operations o WHERE project=? AND actor=? AND state!='done' "
    "AND NOT EXISTS(SELECT 1 FROM review_feed f WHERE f.project=o.project "
    "AND f.session=o.actor AND f.source_key='artifact:operation:'||o.operation)"
)
"""A session's unresolved publication operations the review feed has not observed."""


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
        target = tx.sql.execute(
            "SELECT coalesce(max(seq),0) FROM artifact_events WHERE project=?", (project,)
        ).fetchone()[0]
    after, unknown_more, pages = 0, False, 10 if drain else 1
    # A page another ingestor committed first does not count: it moved the cursor, so
    # every retry starts further on.
    while pages:
        page = _read_page(store, project, session, target)
        after, target = page.after, page.target
        events = _project_events(store, project, session, page.events)
        uncertain = _project_uncertain(store, inventory, project, session, page.unknown)
        end = page.events[-1][0] if page.events else after
        if not _commit_page(store, project, session, page, events, uncertain, end):
            continue  # another ingestor owns this prefix; retry a fresh bounded page
        after, pages = end, pages - 1
        unknown_more = len(page.unknown) > PAGE
        if after >= target and not unknown_more:
            break
    # Recheck unseen uncertainty after commit, including concurrently accepted intents.
    with store.documents.control.read() as tx:
        unknown_more = (
            tx.sql.execute(
                f"SELECT 1 {_UNSEEN_OPERATIONS} LIMIT 1",
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


def _read_page(store: ReviewStore, project: str, session: str, target: int) -> _Page:
    """The session's cursor, the next events up to ``target`` (or past it, when a
    coexisting ingestor already covered more), and the unseen unresolved operations."""
    with store.documents.control.read() as tx:
        db = tx.sql
        after = ARTIFACT_FEED.position(db, project, session)
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
            f"{_UNSEEN_OPERATIONS} ORDER BY o.rowid LIMIT 11",
            (project, session),
        ).fetchall()
    return _Page(after, target, [tuple(e) for e in events], [tuple(u) for u in unknown])


def _project_events(
    store: ReviewStore, project: str, session: str, events: list[tuple[int, str]]
) -> list[_Observation]:
    """The observations of this session's artifact events, each checked against its
    project, sequence and worker snapshot."""
    found: list[_Observation] = []
    for seq, raw in events:
        try:
            event = ArtifactEvent.model_validate(stored_json(raw))
        except ValidationError:
            raise DocumentError(CONFLICT, "invalid stored artifact event") from None
        if event.project_id != project or event.seq != seq or event.revision.project_id != project:
            raise DocumentError(CONFLICT, "artifact event scope differs")
        if event.source.context.session_id != session:
            continue
        snapshot = event.worker_snapshot
        if snapshot is not None and (
            snapshot.project_id != project
            or snapshot.sha256 != event.revision.sha256
            or snapshot.size != event.revision.size
        ):
            raise DocumentError(CONFLICT, "artifact event snapshot differs")
        locator, payload, complete = project_artifact(
            store,
            project,
            event.revision,
            event.source,
            snapshot.source_path if snapshot is not None else None,
        )
        found.append(
            _Observation(
                f"artifact:event:{seq}",
                locator,
                payload,
                complete,
                int(event.kind != "artifact.reproduced"),
            )
        )
    return found


def _project_uncertain(
    store: ReviewStore,
    inventory: ArtifactInventory,
    project: str,
    session: str,
    unknown: list[tuple[object, ...]],
) -> list[tuple[tuple[object, ...], _Observation]]:
    """Metadata-only observations of unresolved publications, each with the operation
    row it was read from, which the commit checks is unchanged."""
    found: list[tuple[tuple[object, ...], _Observation]] = []
    for op in unknown[:PAGE]:
        operation, raw, sha = op[0], op[1], op[2]
        request = stored_json(cast(str, raw))
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
            state = inventory.find_operation(tx.sql, source, cast(str, operation), expected)
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
        locator = {"operation": operation, "run": source.run_id}
        found.append(
            (op, _Observation(f"artifact:operation:{operation}", locator, payload, False, 0))
        )
    return found


def _commit_page(
    store: ReviewStore,
    project: str,
    session: str,
    page: _Page,
    events: list[_Observation],
    uncertain: list[tuple[tuple[object, ...], _Observation]],
    end: int,
) -> bool:
    """Insert the page's observations and move the cursor to ``end`` in one change;
    False, and nothing written, when another ingestor moved the cursor meanwhile."""
    with store.documents.control.write() as tx:
        db = tx.sql
        if not ARTIFACT_FEED.advance(db, project, session, page.after, end):
            return False
        for item in events:
            store.record_observation(
                db,
                project,
                session,
                item.key,
                "artifact",
                item.locator,
                item.payload,
                complete=item.complete,
                artifacts=item.artifacts,
                effects=True,
            )
        for op, item in uncertain:
            current = db.execute(
                "SELECT operation,request,digest,state,worker,receipt "
                "FROM artifact_operations WHERE project=? AND actor=? AND operation=?",
                (project, session, op[0]),
            ).fetchone()
            seen = db.execute(
                "SELECT 1 FROM review_feed WHERE project=? AND session=? AND source_key=?",
                (project, session, item.key),
            ).fetchone()
            if current is not None and tuple(current) == op and not seen:
                store.record_observation(
                    db,
                    project,
                    session,
                    item.key,
                    "artifact",
                    item.locator,
                    item.payload,
                    complete=False,
                    effects=True,
                )
    return True


class ArtifactConsumer:
    """Own admitted SQL work through cancellation and drain before the borrowed store closes."""

    def __init__(self, store: ReviewStore, project: str, session: str) -> None:
        self.store, self.project, self.session = store, project, session
        self._owner = OwnedExecutor(8, "artifact-evidence")

    async def ingest(self, *, drain: bool = False) -> Json:
        """Bound queued work; cancellation detaches the caller, not actual SQL ownership."""
        if not self._owner.admits:
            return {"complete": False, "enabled": True, "held": "consumer admission bound"}
        return await self._owner.submit(self._ingest(drain))

    async def _ingest(self, drain: bool) -> Json:
        return await self._owner.thread(
            lambda: ingest_artifacts(self.store, self.project, self.session, drain=drain)
        )

    async def aclose(self) -> None:
        """Drain all actual transactions even if shutdown itself is repeatedly cancelled."""
        await self._owner.aclose()
