# SPDX-License-Identifier: MIT
"""Durable review windows and immutable evidence in the shared control store.

No read follows a mutable worker path. Executions/transcript entries are copied as bounded
observations; document reads pin immutable revision numbers and verify their hashes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from pydantic import ValidationError

from sandbox_science.contracts import new_id
from sandbox_science.contracts.artifact_inventory import ArtifactRevisionRef
from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.control import EventLog, Receipts
from sandbox_science.orchestrator.control.feeds import FeedCursor
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.documents.agent_text import (
    agent_text as checked_agent_text,
)
from sandbox_science.orchestrator.documents.blocks import check_text
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.project_macros import head, response_view
from sandbox_science.orchestrator.pricing import CostSource, source_of
from sandbox_science.orchestrator.projects import ProjectError, recorded
from sandbox_science.orchestrator.session_plans import SessionPlans

from .models import ReviewConfig, ReviewOutcome, ReviewType, Scope
from .prompt_bundle import (
    MAX_PROVENANCE_BYTES,
    ProvenanceRead,
    ReviewerProvenance,
    encode_provenance,
)
from .rubric import RUBRIC_VERSION

if TYPE_CHECKING:
    from .actions import Earlier


def _reported_cost(cost: object) -> bool:
    """Whether ``cost`` is one the gateway could report: none, or a finite amount, not
    negative."""
    return cost is None or (
        type(cost) in (int, float) and math.isfinite(cast(float, cost)) and cast(float, cost) >= 0
    )


# Heterogeneous evidence is JSON validated by its producing boundary and bounded here.
Json = dict[str, Any]
MANIFEST_PAGE: Final = 50
"""The most items one manifest page lists."""

MAX_ITEM_BYTES: Final = 256 * 1024
MAX_SNAPSHOT_BYTES: Final = 8 * 1024 * 1024
MAX_SNAPSHOT_ITEMS: Final = 256
ITEM_OVERHEAD: Final = 256
"""Bytes a snapshot's budget counts for each item beyond its locator and payload."""
PRIOR_ITEMS: Final = 8
PRIOR_BYTES: Final = 128 * 1024
"""Recent exact evidence carried into the next bounded review for boundary checks."""
MAX_SNAPSHOT_FINDINGS: Final = 100
"""Open findings a snapshot copies; one more is read to tell whether some were left out."""
CONFIRMATION_ATTEMPTS: Final = 3
"""Failed or dropped confirmations of the same targets before they are given up."""
LEASE_SECONDS: Final = 120
FAILURE_COOLDOWN_SECONDS: Final = 300
"""Minimum wait between attempts after repeated reviewer failures."""


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One item a review snapshot pins."""

    kind: str
    locator: str
    payload: str
    sha256: str
    complete: int

    @property
    def size(self) -> int:
        return len(self.locator.encode()) + len(self.payload.encode()) + ITEM_OVERHEAD


@dataclass(frozen=True, slots=True)
class FindingGroup:
    """An open finding's items (its response's changed revisions and artifacts, then the
    finding itself), the earlier evidence it grants, and its response body's pin."""

    finding: str
    revision: int
    items: list[EvidenceItem]
    grants: dict[str, Json]
    body_pin: Json | None


@dataclass
class SnapshotBudget:
    """The bytes and items a snapshot has used of ``MAX_SNAPSHOT_BYTES`` and
    ``MAX_SNAPSHOT_ITEMS``, or of the ``share`` of them it may use: half is the share
    open findings take before feed rows (the review of #449)."""

    size: int = 0
    items: int = 0
    share: int = 1

    @staticmethod
    def cost(group: FindingGroup) -> int:
        return sum(item.size for item in group.items) + sum(
            len(dump(grant["locator"]).encode())
            + len(dump(grant["payload"]).encode())
            + ITEM_OVERHEAD
            for grant in group.grants.values()
        )

    def fits(self, group: FindingGroup) -> bool:
        return (
            all(len(item.payload.encode()) <= MAX_ITEM_BYTES for item in group.items)
            and self.size + self.cost(group) <= MAX_SNAPSHOT_BYTES // self.share
            and self.items + len(group.items) + len(group.grants)
            <= MAX_SNAPSHOT_ITEMS // self.share
        )

    def add(self, group: FindingGroup) -> None:
        self.size += self.cost(group)
        self.items += len(group.items) + len(group.grants)


@dataclass(frozen=True, slots=True)
class _Window:
    """The author-work window a dispatch reads: its watermark, a failed window's retry
    end, and the end it covers."""

    watermark: int
    retry: int | None
    end: int
    failures: int


@dataclass
class _Plan:
    """What a dispatch pins, before the change that commits it."""

    kind: ReviewType
    window: _Window
    selected_end: int
    items: list[EvidenceItem]
    grants: dict[str, Json]
    targets: list[tuple[str, int]]
    response_pins: dict[str, Json]
    complete: bool
    span_complete: bool
    confirmation_key: str | None


RECEIPTS: Final = Receipts("review", "operation ID already names another finding mutation")
"""The receipts of the findings' and the responses' changes, which share one scope."""

DOCUMENT_FEED: Final = FeedCursor("document")
"""How far each session's reviewer has read the project's document events."""


def _length(value: object) -> int | None:
    """The length of a revision event's ``structural_added`` list, or None when the event
    has none (an event that is not a revision's)."""
    return len(cast(list[object], value)) if isinstance(value, list) else None


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def stored_json(value: str) -> Json:
    """A JSON object the store holds, as it was written (decision 3: no second check)."""
    return cast(Json, json.loads(value))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def identifier(value: object, prefix: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(re.escape(prefix) + r"-[0-9a-f]{24}", value) is None
    ):
        raise DocumentError(INVALID_REQUEST, f"invalid {prefix} identifier")


def text(value: str, limit: int = 8192) -> str:
    check_text(value)
    if not value or len(value) > limit or len(value.encode()) > limit * 4:
        raise DocumentError(INVALID_REQUEST, "review text exceeds its bound")
    return value


def agent_text(value: str, what: str, limit: int = 8192) -> str:
    """Review text an agent wrote, which the user decides on and agents read again, as it
    is kept: its line ends line feeds, within its bound, and with no character the user
    may not see (threat model, A3)."""
    kept = checked_agent_text(value, what)
    return text(kept, limit)


def clipped(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(0, (limit - 30) // 2)
    return value[:half] + "\n… evidence omitted …\n" + value[-half:]


_FINDING_FIELDS: Final = (
    "finding",
    "severity",
    "category",
    "statement",
    "anchors",
    "remedy",
    "status",
    "revision",
    "reflags",
    "supersedes",
)
"""An open finding as a snapshot copies it."""


def _revised(found: Json) -> Json:
    """A document observation's revision and the blocks it changed, as an actions item
    lists them."""
    return {
        "document": found.get("document"),
        "revision": found.get("revision"),
        **cast(Json, found.get("blocks") or {}),
    }


def _archived(items: list[EvidenceItem]) -> Json:
    """The first and last archive positions of the messages a snapshot's transcript
    items copy, when they name them (CR-14 as amended); nothing otherwise."""
    found = [
        cast(Json, stored_json(item.locator).get("archive") or {}).get("position")
        for item in items
        if item.kind == "transcript"
    ]
    positions = [position for position in found if isinstance(position, int)]
    return {"archive": {"from": min(positions), "to": max(positions)}} if positions else {}


def _confirmation_key(targets: list[tuple[str, int]], pins: dict[str, Json]) -> str:
    """What a confirmation is of: its targets, and each response body and macro context
    it pinned. Three failures of the same key give the targets up."""
    return digest(
        dump(
            [
                targets,
                sorted(
                    (finding, pin["revision"], pin["sha256"], digest(dump(pin["macro_context"])))
                    for finding, pin in pins.items()
                ),
            ]
        )
    )


class ReviewStore:
    """The bounded review repository; DocumentStore initializes the shared database first."""

    def __init__(
        self,
        documents: DocumentStore,
        *,
        clock: Callable[[], float] = time.time,
        artifacts: ArtifactInventory | None = None,
        defaults: ReviewConfig | None = None,
    ) -> None:
        self.documents, self.clock, self.artifacts = documents, clock, artifacts
        self.defaults = defaults or ReviewConfig()
        """The settings of a project that has none of its own: the deployment's, which the
        run service takes from its options."""
        self.review_log = EventLog(documents.control, "review", documents.producer)
        self.finding_log = EventLog(documents.control, "finding", documents.producer)
        if artifacts is None and not documents.control.missing:
            self.artifacts = ArtifactInventory(documents)
        if artifacts is not None and artifacts.documents is not documents:
            raise ValueError("artifact evidence must share the control connection")
        self._plans: SessionPlans | None = None

    def _session_plans(self) -> SessionPlans | None:
        """The session plans a snapshot's plan item reads, made once, since making them
        checks their budgets in a change of their own; None when they cannot be made."""
        if self._plans is None:
            try:
                self._plans = SessionPlans(self.documents)
            except DocumentError:
                return None
        return self._plans

    def scope(self, project: str, session: str) -> None:
        try:
            Scope(project_id=project, session_id=session)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid review scope") from None
        try:
            found = recorded(self.documents.path.parent, project)
        except ProjectError:
            raise DocumentError(NOT_FOUND, "no such review project") from None
        if session not in found.sessions:
            raise DocumentError(NOT_FOUND, "no such review session")

    def config(self, project: str) -> tuple[ReviewConfig, str]:
        row = None
        if not self.documents.control.missing:
            with self.documents.control.read() as tx:
                row = tx.sql.execute(
                    "SELECT payload FROM review_config WHERE project=? ORDER BY number DESC "
                    "LIMIT 1",
                    (project,),
                ).fetchone()
        value = ReviewConfig.model_validate_json(row[0]) if row else self.defaults
        return value, digest(value.model_dump_json())

    def configure(self, project: str, session: str, config: ReviewConfig) -> str:
        self.scope(project, session)
        with self.documents.control.write() as tx:
            db = tx.sql
            number = db.execute(
                "SELECT coalesce(max(number),0)+1 FROM review_config WHERE project=?", (project,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO review_config VALUES(?,?,?,?)",
                (
                    project,
                    number,
                    config.model_dump_json(),
                    self.clock(),
                ),
            )
        return digest(config.model_dump_json())

    def _window(self, db: sqlite3.Connection, project: str, session: str) -> None:
        db.execute(
            "INSERT OR IGNORE INTO review_windows(project,session) VALUES(?,?)", (project, session)
        )

    def observe(
        self,
        project: str,
        session: str,
        key: str,
        kind: Literal[
            "transcript",
            "execution",
            "document",
            "artifact",
            "ledger",
            "source",
            "action",
            "delegation",
        ],
        locator: Json,
        payload: Json,
        *,
        complete: bool = True,
        characters: int = 0,
        structural: int = 0,
        artifacts: int = 0,
        cells: int = 0,
        effects: bool = False,
    ) -> int:
        """Copy one platform observation. A duplicate source key has one immutable value."""
        self.scope(project, session)
        with self.documents.control.write() as tx:
            db = tx.sql
            return self.record_observation(
                db,
                project,
                session,
                key,
                kind,
                locator,
                payload,
                complete=complete,
                characters=characters,
                structural=structural,
                artifacts=artifacts,
                cells=cells,
                effects=effects,
            )

    def record_observation(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        key: str,
        kind: str,
        locator: Json,
        payload: Json,
        *,
        complete: bool = True,
        characters: int = 0,
        structural: int = 0,
        artifacts: int = 0,
        cells: int = 0,
        effects: bool = False,
    ) -> int:
        """Insert an immutable observation inside the caller's owned transaction."""
        if (
            not key
            or len(key) > 256
            or any(type(n) is not int or n < 0 for n in (characters, structural, artifacts, cells))
        ):
            raise DocumentError(INVALID_REQUEST, "invalid review observation")
        encoded, location = dump(payload), dump(locator)
        if len(encoded.encode()) > MAX_ITEM_BYTES or len(location.encode()) > 4096:
            raise DocumentError(INVALID_REQUEST, "review observation exceeds its bound")
        sha = digest(encoded)
        self._window(db, project, session)
        previous = db.execute(
            "SELECT seq,sha256 FROM review_feed WHERE project=? AND session=? AND source_key=?",
            (project, session, key),
        ).fetchone()
        if previous:
            if previous[1] != sha:
                raise DocumentError(CONFLICT, "observation key names different evidence")
            return previous[0]
        cursor = db.execute(
            """INSERT INTO review_feed(project,session,source_key,kind,locator,payload,
            sha256,complete,characters,structural,artifacts,cells,effects,created)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project,
                session,
                key,
                kind,
                location,
                encoded,
                sha,
                int(complete),
                characters,
                structural,
                artifacts,
                cells,
                int(effects),
                self.clock(),
            ),
        )
        assert cursor.lastrowid is not None  # noqa: S101
        return cursor.lastrowid

    def ingest_documents(self, project: str, session: str, *, drain: bool = False) -> Json:
        """Consume bounded pages through a pinned event head, preserving overflow.

        Terminal boundaries drain at most 10,000 events. Any remaining backlog is
        explicit incomplete coverage and is resumed at the next boundary. Each page's
        observations and its cursor commit together.
        """
        self.scope(project, session)
        with self.documents.control.read() as tx:
            target = self.documents.event_log.head(tx.sql, project)
        after, pages = 0, 50 if drain else 1
        # A page another ingestor wrote first does not count: it moved the cursor, so
        # every retry starts further on, and the loop ends at the target at the latest.
        while pages:
            after, rows = self._document_page(project, session, target)
            if not rows:
                break
            observations = [
                (self._document_observation(project, document, number), chars, structural)
                for _seq, document, owner, kind, author, number, chars, structural in rows
                if kind == "document.revision_created"
                and owner == session
                and author in ("agent", "worker")
            ]
            end = rows[-1][0]
            with self.documents.control.write() as tx:
                db = tx.sql
                if not DOCUMENT_FEED.advance(db, project, session, after, end):
                    continue  # another ingestor wrote this page; read on from its cursor
                for (key, locator, payload), chars, structural in observations:
                    self.record_observation(
                        db,
                        project,
                        session,
                        key,
                        "document",
                        locator,
                        payload,
                        characters=chars or 0,
                        structural=structural or 0,
                        effects=True,
                    )
                self._window(db, project, session)
            after, pages = end, pages - 1
            if after >= target:
                break
        return {"cursor": after, "target": target, "complete": after >= target}

    def _document_page(
        self, project: str, session: str, target: int
    ) -> tuple[int, list[tuple[Any, ...]]]:
        """The session's document cursor and the next 200 project events up to ``target``."""
        with self.documents.control.read() as tx:
            db = tx.sql
            after = DOCUMENT_FEED.position(db, project, session)
            events = self.documents.event_log.page(db, project, after, 200, through=target)
        return after, [
            (
                event.seq,
                event.subject,
                event.session,
                event.kind,
                event.payload.get("author_kind"),
                event.payload.get("revision"),
                event.payload.get("agent_characters"),
                _length(event.payload.get("structural_added")),
            )
            for event in events
        ]

    def _document_observation(
        self, project: str, document: str, number: int
    ) -> tuple[str, Json, Json]:
        """The source key, locator and payload of an agent's document revision."""
        from .actions import changed_blocks

        revision = self.documents.revision(project, document, number)
        common = {"document": document, "revision": number, "sha256": revision.info.sha256}
        return (
            f"document:{document}:{number}",
            {**common, "run": revision.info.author.run_id},
            {
                **common,
                "author": revision.info.author.model_dump(mode="json"),
                "reason": revision.info.reason,
                "tombstone": revision.info.tombstone,
                "blocks": changed_blocks(self.documents, project, document, number),
            },
        )

    def run_effects(self, project: str, session: str, run: str) -> bool:
        """Derive effects from durable run-attributed observations, not callbacks."""
        self.scope(project, session)
        with self.documents.control.read() as tx:
            db = tx.sql
            return (
                db.execute(
                    "SELECT 1 FROM review_feed WHERE project=? AND session=? AND effects=1 "
                    "AND json_extract(locator,'$.run')=? LIMIT 1",
                    (project, session, run),
                ).fetchone()
                is not None
            )

    def finding_states(self, project: str, session: str) -> Json:
        self.scope(project, session)
        with self.documents.control.read() as tx:
            db = tx.sql
            return dict(
                db.execute(
                    "SELECT status,count(*) FROM review_findings WHERE project=? AND session=? "
                    "AND category NOT IN ('math','handle_citation') "
                    "GROUP BY status",
                    (project, session),
                )
            )

    def progress(self, project: str, session: str) -> Json:
        self.scope(project, session)
        with self.documents.control.read() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT watermark,retry_end FROM review_windows WHERE project=? AND session=?",
                (project, session),
            ).fetchone()
            watermark, retry = row if row else (0, None)
            remaining = (
                db.execute(
                    "SELECT 1 FROM review_feed WHERE project=? AND session=? AND seq>? LIMIT 1",
                    (project, session, watermark),
                ).fetchone()
                is not None
            )
            historical_holes = (
                db.execute(
                    "SELECT 1 FROM review_runs WHERE project=? AND session=? "
                    "AND kind IN ('checkpoint','terminal') AND end_seq<=? "
                    "AND (state='inconclusive' OR (state IN ('findings','passed') AND "
                    "coverage_complete=0 AND NOT EXISTS (SELECT 1 FROM control_events e "
                    "WHERE e.project=review_runs.project AND e.session=review_runs.session "
                    "AND e.subject_type='review' AND e.subject_id=review_runs.review_id "
                    "AND e.kind='review.dispatched' AND "
                    "json_extract(e.payload,'$.span_complete')=1))) LIMIT 1",
                    (project, session, watermark),
                ).fetchone()
                is not None
                or db.execute(
                    "SELECT 1 FROM control_events WHERE project=? AND session=? "
                    "AND kind='review.unreviewed' AND "
                    "json_extract(payload,'$.start') IS NOT NULL LIMIT 1",
                    (project, session),
                ).fetchone()
                is not None
            )
        return {
            "watermark": watermark,
            "retry_end": retry,
            "remaining": remaining,
            "historical_holes": historical_holes,
        }

    def request(self, project: str, session: str) -> None:
        self.scope(project, session)
        with self.documents.control.write() as tx:
            db = tx.sql
            self._window(db, project, session)
            db.execute(
                "UPDATE review_windows SET requested=1 WHERE project=? AND session=?",
                (project, session),
            )

    def _cooling(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        failures: int,
        retry_end: int | None,
    ) -> bool:
        if failures < 3 or retry_end is None:
            return False
        row = db.execute(
            "SELECT max(ended) FROM review_runs WHERE project=? AND session=? "
            "AND end_seq=? AND state IN ('failed','dropped','capped')",
            (project, session, retry_end),
        ).fetchone()
        return bool(
            row is not None
            and row[0] is not None
            and self.clock() - row[0] < FAILURE_COOLDOWN_SECONDS
        )

    def due(self, project: str, session: str) -> bool:
        config, _ = self.config(project)
        if not config.enabled:
            return False
        if self.documents.control.missing:
            return False
        with self.documents.control.read() as tx:
            db = tx.sql
            row = db.execute(
                (
                    "SELECT watermark,last_dispatch,retry_end,requested,failures FROM "
                    "review_windows WHERE project=? AND session=?"
                ),
                (project, session),
            ).fetchone()
            if row is None:
                return False
            if db.execute(
                "SELECT 1 FROM review_runs WHERE project=? AND session=? AND state='running'",
                (project, session),
            ).fetchone():
                return False
            chars, structure, artifacts, cells = db.execute(
                """SELECT coalesce(sum(characters),0),coalesce(sum(structural),0),
                coalesce(sum(artifacts),0),coalesce(sum(cells),0) FROM review_feed
                WHERE project=? AND session=? AND seq>?""",
                (project, session, row[0]),
            ).fetchone()
            cooling = self._cooling(db, project, session, row[4], row[2])
        elapsed = self.clock() - row[1] if row[1] is not None else float("inf")
        if cooling:
            return False
        return elapsed >= config.min_interval_seconds and (
            row[2] is not None
            or row[3]
            or chars >= config.text_characters
            or structure > 0
            or artifacts >= config.artifact_revisions
            or (cells >= config.kernel_cells and elapsed >= config.kernel_interval_seconds)
        )

    def confirmation_targets(self, project: str, session: str) -> list[tuple[str, int]]:
        """A revision is queued once, with at most two retries after failure/drop.

        An ended inconclusive/capped review leaves the finding unconfirmed but does
        not repeatedly schedule the same claim. A new response revision can queue it.
        """
        self.scope(project, session)
        with self.documents.control.read() as tx:
            db = tx.sql
            revision = head(db, project)
            # A review of a target counts when it pinned the response's latest body and,
            # for a response with a body, the current project macro setting.
            reviews = (
                "SELECT count(*) FROM review_confirmation_targets t JOIN review_runs r "
                "USING(review_id) WHERE t.finding_id=f.finding_id AND t.revision=f.revision "
                "AND coalesce((SELECT body FROM latest WHERE latest.finding=f.finding_id),0)="
                "coalesce((SELECT p.body_revision FROM review_response_pins p "
                "WHERE p.review_id=t.review_id AND p.finding_id=f.finding_id),0) "
                "AND (NOT EXISTS(SELECT 1 FROM text_revisions b WHERE b.subject_type='response' "
                "AND b.parent=f.finding_id AND b.parent_revision=f.revision) OR EXISTS("
                "SELECT 1 FROM review_response_pins m WHERE m.review_id=t.review_id AND "
                "m.finding_id=f.finding_id AND m.macro_revision=:macros)) AND r.state "
            )
            return [
                tuple(row)
                for row in db.execute(
                    "WITH latest(finding,body) AS (SELECT f.finding_id,(SELECT b.revision "  # noqa: S608 - fixed SQL
                    "FROM text_revisions b WHERE b.subject_type='response' AND "
                    "b.parent=f.finding_id AND b.parent_revision=f.revision "
                    "ORDER BY b.revision DESC LIMIT 1) "
                    "FROM review_findings f WHERE f.project=:project AND f.session=:session) "
                    "SELECT f.finding_id,f.revision FROM review_findings f WHERE "
                    "project=:project AND session=:session "
                    "AND category NOT IN ('math','handle_citation') "
                    "AND status='claimed fixed' "
                    f"AND ({reviews}NOT IN ('running','failed','dropped'))=0 "
                    f"AND ({reviews}IN ('failed','dropped'))<:attempts "
                    "ORDER BY f.rowid LIMIT 100",
                    {
                        "project": project,
                        "session": session,
                        "macros": revision,
                        "attempts": CONFIRMATION_ATTEMPTS,
                    },
                )
            ]

    def dispatch(
        self,
        project: str,
        session: str,
        kind: ReviewType,
        *,
        author_run: str,
        owner: str,
        alias: str | None,
        classifier: Json | None = None,
        coverage_complete: bool = True,
        viewed: Sequence[tuple[str, str]] = (),
    ) -> str | None:
        """Pin a bounded prefix. No unseen overflow item is skipped by the watermark.
        ``viewed`` are the earlier runs' calls whose results the run's view held, each its
        run's ID and its own: their actions are listed as context (CR-2 as amended).

        A confirmation whose fix groups do not fit the snapshot's bounds leaves those
        responses unconfirmed and serves author work instead, as incomplete coverage,
        rather than make an empty confirmation that would starve the feed.
        """
        self.scope(project, session)
        config, config_revision = self.config(project)
        if not config.enabled:
            return None
        targets = self.confirmation_targets(project, session)
        if kind == "confirmation" and not targets:
            return None
        plan = self._plan(
            project, session, kind, targets, coverage_complete, author_run, viewed, config
        )
        if plan.kind == "confirmation" and not plan.targets:
            # Serve author work instead, with the configuration and targets as they are
            # now: the review may have been disabled or reconfigured meanwhile.
            self.scope(project, session)
            config, config_revision = self.config(project)
            if not config.enabled:
                return None
            targets = self.confirmation_targets(project, session)
            plan = self._plan(
                project, session, "terminal", targets, False, author_run, viewed, config
            )
        review = new_id("rev")
        with self.documents.control.write() as tx:
            committed = self._commit_snapshot(
                tx.sql,
                project,
                session,
                review,
                plan,
                author_run=author_run,
                owner=owner,
                alias=alias,
                config=config,
                config_revision=config_revision,
                classifier=classifier,
            )
        return review if committed else None

    def _plan(
        self,
        project: str,
        session: str,
        kind: ReviewType,
        targets: list[tuple[str, int]],
        coverage_complete: bool,
        author_run: str,
        viewed: Sequence[tuple[str, str]] = (),
        config: ReviewConfig | None = None,
    ) -> _Plan:
        """The items, grants and pins a review of ``kind`` would take now, for the run
        ``author_run``, whose view held the earlier runs' calls ``viewed``.

        Open findings take their slots first, confirmation targets and rebuttals before
        the others, in as much as half the snapshot of an author window, or all of a
        confirmation's, so that feed rows, the actions item and the plan item cannot crowd
        them out (the review of #449). The feed's next rows take what is left, and a
        finding that did not fit that share takes what they leave."""
        from .actions import Earlier, plan_item

        effective = config or self.config(project)[0]

        plans = self._session_plans()
        with self.documents.control.read() as tx:
            db = tx.sql
            window = self._dispatch_window(db, project, session)
            findings = self._open_findings(db, project, session, targets)
            # The session plan as this read sees it, which a narrated plan change is
            # checked against (lab 42's B2).
            planned = None if plans is None else plan_item(plans, db, project, session, author_run)
            # A terminal review's answer may say what the run did in its windows reviewed
            # before; their actions come in as context (lab 42's B5).
            earlier = (
                self._earlier(db, project, session, window, author_run)
                if kind == "terminal"
                else None
            )
            # And any answer may tell what an earlier run did: the actions of the earlier
            # runs' calls its view held come in as context too (CR-2 as amended).
            runs = self._viewed(db, project, session, viewed) if kind != "confirmation" else []
            if runs:
                earlier = replace(earlier or Earlier([], [], 0, 0), runs=runs)
        shown: list[EvidenceItem] = []
        if planned is not None:
            encoded = dump(planned[1])
            shown.append(EvidenceItem("plan", dump(planned[0]), encoded, digest(encoded), 1))
        grants: dict[str, Json] = {}
        admitted: dict[int, FindingGroup] = {}
        # The plan item counts within the findings' share, so that a confirmation's
        # findings, which may take the whole snapshot, leave room for it.
        reserved = SnapshotBudget(
            size=sum(item.size for item in shown),
            items=len(shown),
            share=1 if kind == "confirmation" else 2,
        )
        for index, row in enumerate(findings[:MAX_SNAPSHOT_FINDINGS]):
            group = self._finding_group(project, session, row, grants)
            if reserved.fits(group):
                admitted[index] = group
                grants.update(group.grants)
                reserved.add(group)
        # Fix targets and their original evidence take precedence over new feed items;
        # those keep their watermark for a later review.
        if kind == "confirmation":
            selected_end, pinned = window.watermark, []
        else:
            room = (MAX_SNAPSHOT_BYTES - reserved.size, MAX_SNAPSHOT_ITEMS - reserved.items)
            # A cap retries less evidence, rather than spending the same token budget
            # on the same oversized window three times. The remaining feed stays queued.
            shrink = 4 ** min(window.failures, 2)
            room = (
                min(room[0], max(1, effective.max_feed_bytes // shrink)),
                min(room[1], max(1, effective.max_feed_items // shrink)),
            )
            with self.documents.control.read() as tx:
                selected_end, pinned = self._feed_items(
                    tx.sql,
                    project,
                    session,
                    window,
                    earlier,
                    room,
                    ceiling_items=MAX_SNAPSHOT_ITEMS - reserved.items,
                )
        budget = SnapshotBudget(
            size=sum(item.size for item in pinned) + reserved.size,
            items=len(pinned) + reserved.items,
        )
        pinned += shown
        for index, row in enumerate(findings[:MAX_SNAPSHOT_FINDINGS]):
            if index in admitted:
                continue
            group = self._finding_group(project, session, row, grants)
            if budget.fits(group):
                admitted[index] = group
                grants.update(group.grants)
                budget.add(group)
        included: list[tuple[str, int]] = []
        response_pins: dict[str, Json] = {}
        for index in sorted(admitted):
            group = admitted[index]
            pinned.extend(group.items)
            if group.body_pin is not None:
                response_pins[group.finding] = group.body_pin
            if (group.finding, group.revision) in targets:
                included.append((group.finding, group.revision))
        groups = len(admitted)
        if kind == "confirmation" and included:
            # What the author did since the findings were raised, to check a claimed fix
            # against (lab 41): the actions item, as an author window's.
            item = self._fix_actions(project, session, [finding for finding, _ in included])
            if item is not None:
                pinned.append(item)
        # A confirmation pins finding groups rather than consuming author work.
        # A failed author window stays independently queued, with its exact end.
        span_complete = (
            coverage_complete
            and (set(included) == set(targets) if kind == "confirmation" else True)
            and groups == len(findings)
            and all(item.complete for item in pinned)
            and all(grant["complete"] for grant in grants.values())
        )
        complete = span_complete and (kind == "confirmation" or selected_end == window.end)
        return _Plan(
            kind=kind,
            window=window,
            selected_end=selected_end,
            items=pinned,
            grants=grants,
            targets=included,
            response_pins=response_pins,
            complete=complete,
            span_complete=span_complete,
            confirmation_key=_confirmation_key(included, response_pins)
            if kind == "confirmation"
            else None,
        )

    def _fix_actions(self, project: str, session: str, findings: list[str]) -> EvidenceItem | None:
        """The actions item of what the session's runs did since the reviews that raised
        ``findings`` ended, and the blocks each document revision since changed; None when
        there are none."""
        from .actions import MAX_ACTIONS, actions_item

        with self.documents.control.read() as tx:
            db = tx.sql
            (start,) = db.execute(
                "SELECT coalesce(min(r.end_seq),0) FROM review_findings f JOIN review_runs r "
                "ON r.review_id=f.review_id WHERE f.finding_id IN (SELECT value FROM json_each(?))",
                (json.dumps(findings),),
            ).fetchone()
            rows = db.execute(
                "SELECT seq,source_key,kind,locator,payload FROM review_feed WHERE project=? "
                "AND session=? AND seq>? AND (kind='action' OR (kind='document' "
                "AND source_key GLOB 'document:*')) ORDER BY seq LIMIT ?",
                (project, session, start, 2 * MAX_ACTIONS),
            ).fetchall()
        if not rows:
            return None
        actions = [
            (stored_json(locator), stored_json(payload))
            for _, _, kind, locator, payload in rows
            if kind == "action"
        ]
        revised = [
            _revised(stored_json(payload)) for _, _, kind, _, payload in rows if kind == "document"
        ]
        located, listed = actions_item(actions, revised, start + 1, rows[-1][0])
        encoded = dump(listed)
        return EvidenceItem("actions", dump(located), encoded, digest(encoded), 1)

    @staticmethod
    def _dispatch_window(db: sqlite3.Connection, project: str, session: str) -> _Window:
        """The session's watermark, its failed window's retry end, and the end a review
        covers: that retry end, or the feed's last item."""
        found = db.execute(
            "SELECT watermark,retry_end,failures FROM review_windows WHERE project=? AND session=?",
            (project, session),
        ).fetchone()
        watermark, retry, failures = found if found else (0, None, 0)
        end = (
            retry
            if retry is not None
            else db.execute(
                "SELECT coalesce(max(seq),?) FROM review_feed WHERE project=? AND session=?",
                (watermark, project, session),
            ).fetchone()[0]
        )
        return _Window(watermark, retry, end, failures)

    @staticmethod
    def _earlier(
        db: sqlite3.Connection, project: str, session: str, window: _Window, run: str
    ) -> Earlier | None:
        """Run ``run``'s actions and document revisions in its windows reviewed before
        ``window``: those the feed holds from the run's first observation to the
        watermark, its delegated runs' included, the latest twice ``MAX_ACTIONS`` of them;
        None when the run had no observation before."""
        from .actions import MAX_ACTIONS, Earlier

        (first,) = db.execute(
            "SELECT min(seq) FROM review_feed WHERE project=? AND session=? AND seq<=? "
            "AND json_extract(locator,'$.run')=?",
            (project, session, window.watermark, run),
        ).fetchone()
        if first is None:
            return None
        # The run's own actions, its delegated runs' (brought for its review), and the
        # document revisions they made: not another run's, nor work brought for one.
        rows = [
            (kind, stored_json(locator), payload)
            for kind, locator, payload in db.execute(
                "SELECT kind,locator,payload FROM (SELECT seq,kind,locator,payload FROM "
                "review_feed WHERE project=? AND session=? AND seq>=? AND seq<=? AND "
                "((kind='action' AND (json_extract(locator,'$.run')=? OR "
                "json_extract(locator,'$.for_run')=?)) OR (kind='document' AND "
                "source_key GLOB 'document:*')) ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                (project, session, first, window.watermark, run, run, 2 * MAX_ACTIONS),
            )
        ]
        runs = {run} | {loc.get("run") for _, loc, _ in rows if loc.get("for_run") == run}
        return Earlier(
            [(loc, stored_json(found)) for kind, loc, found in rows if kind == "action"],
            [
                _revised(stored_json(found))
                for kind, loc, found in rows
                if kind == "document" and loc.get("run") in runs
            ],
            first,
            window.watermark,
        )

    @staticmethod
    def _viewed(
        db: sqlite3.Connection, project: str, session: str, viewed: Sequence[tuple[str, str]]
    ) -> list[tuple[Json, Json]]:
        """The actions of the earlier runs' calls ``viewed``, each its run's ID and its
        own, as the feed holds them, in order, the latest ``MAX_ACTIONS``."""
        from .actions import MAX_ACTIONS

        wanted = set(viewed)
        if not wanted:
            return []
        found: list[tuple[Json, Json]] = []
        for locator, payload in db.execute(
            "SELECT locator,payload FROM review_feed WHERE project=? AND session=? AND "
            "kind='action' AND json_extract(locator,'$.run') IN (SELECT value FROM "
            "json_each(?)) ORDER BY seq",
            (project, session, json.dumps(sorted({run for run, _ in wanted}))),
        ):
            located, action = stored_json(locator), stored_json(payload)
            if "delegation" not in located and (located.get("run"), action.get("call")) in wanted:
                found.append((located, action))
        return found[-MAX_ACTIONS:]

    @staticmethod
    def _feed_items(
        db: sqlite3.Connection,
        project: str,
        session: str,
        window: _Window,
        earlier: Earlier | None = None,
        room: tuple[int, int] | None = None,
        *,
        ceiling_items: int = MAX_SNAPSHOT_ITEMS,
    ) -> tuple[int, list[EvidenceItem]]:
        """The feed's next items after the watermark, as many as ``room``, the bytes and
        items the snapshot leaves them, allows, and the last one's sequence number. What a
        tool call read for a quote (a raw ``source``) is not an item: the transcript holds
        the result the agent saw, and only a source a quote cites comes in, pinned beside
        its entry (``quoted:``), so a long read that no quote cites leaves the coverage
        whole (CR-4). The actions item lists the window's actions, as many as
        ``MAX_ACTIONS``, and ``earlier``'s as context; the rows it folds take no item.

        A long delegation can cross windows. Each later piece brings the pinned outcome
        back as context, so an execution is never presented without its claim."""
        from .actions import MAX_ACTIONS, actions_item

        max_bytes, max_items = room or (MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_ITEMS)
        items: list[EvidenceItem] = []
        actions: list[tuple[Json, Json]] = []
        revised: list[Json] = []
        size, new_items, end = 0, 0, window.watermark
        first = db.execute(
            "SELECT source_key FROM review_feed WHERE project=? AND session=? AND "
            "seq>? AND seq<=? ORDER BY seq LIMIT 1",
            (project, session, window.watermark, window.end),
        ).fetchone()
        if window.watermark and first is not None:
            # A claim and its contradicting observation can straddle two windows.
            # Carry recent items as context; only rows after the watermark
            # advance coverage. A prior ledger entry is never a newly reviewed
            # ledger item, even if the reviewer uses it to find a contradiction.
            carried: list[EvidenceItem] = []
            prior_size = 0
            prior_limit = max(1, PRIOR_ITEMS // (4 ** min(window.failures, 2)))
            byte_limit = max(1, PRIOR_BYTES // (4 ** min(window.failures, 2)))
            for kind, locator, payload, sha, complete in db.execute(
                "SELECT kind,locator,payload,sha256,complete FROM review_feed WHERE "
                "project=? AND session=? AND seq<=? AND "
                "kind!='action' AND "
                "(kind!='source' OR source_key GLOB 'quoted:*') "
                "ORDER BY seq DESC LIMIT ?",
                (project, session, window.watermark, prior_limit),
            ):
                item = EvidenceItem(
                    "context" if kind == "ledger" else kind,
                    dump({"prior_kind": kind, "locator": stored_json(locator)})
                    if kind == "ledger"
                    else locator,
                    payload,
                    sha,
                    complete,
                )
                if prior_size + item.size > byte_limit:
                    if not carried:
                        marker = dump(
                            {
                                "prior_kind": kind,
                                "sha256": sha,
                                "bytes": item.size,
                                "omitted": "prior item exceeds carry allowance",
                            }
                        )
                        carried.append(EvidenceItem("context", locator, marker, digest(marker), 1))
                    break
                if len(carried) >= max(0, ceiling_items - 1):
                    break
                carried.append(item)
                prior_size += item.size
            items.extend(reversed(carried))
            size += prior_size
        if first is not None:
            parts = str(first[0]).split(":", 2)
            if len(parts) == 3 and parts[0] == "delegated":
                prior_row = db.execute(
                    "SELECT kind,locator,payload,sha256,complete FROM review_feed WHERE "
                    "project=? AND session=? AND source_key=? AND seq<=?",
                    (project, session, ":".join(parts[:2]), window.watermark),
                ).fetchone()
                if prior_row is not None:
                    kind, locator, payload, sha, complete = prior_row
                    context = EvidenceItem(kind, locator, payload, sha, complete)
                    if context not in items:
                        items.append(context)
                        size += context.size
        # A raw source's row is passed over without reading its payload, which may be
        # 256 KiB: it takes no item and only moves the window's end.
        for seq, key, kind, locator, payload, sha, complete in db.execute(
            "SELECT seq,source_key,kind,locator,CASE WHEN kind='source' AND source_key NOT "
            "GLOB 'quoted:*' THEN '' ELSE payload END,sha256,complete FROM review_feed "
            "WHERE project=? AND session=? AND seq>? AND seq<=? ORDER BY seq",
            (project, session, window.watermark, window.end),
        ):
            key = str(key)
            raw = kind == "source" and not key.startswith("quoted:")
            folded = raw or kind == "action"
            item = None if folded else EvidenceItem(kind, locator, payload, sha, complete)
            # Always take one new row, even when its pinned payload alone exceeds the
            # target. Otherwise a large first item would strand the feed forever.
            full = (
                len(actions) >= min(MAX_ACTIONS, max_items)
                if kind == "action"
                else item is not None
                and end > window.watermark
                and (
                    size + item.size > max_bytes - min(16 * 1024, max_bytes // 8)
                    or new_items >= max_items
                    or len(items) >= ceiling_items
                )
            )
            if full:
                break
            end = seq
            if raw:
                continue
            if item is None:
                # One derived item lists the window's actions, after the others.
                actions.append((stored_json(locator), stored_json(payload)))
                continue
            items.append(item)
            size += item.size
            new_items += 1
            if kind == "document" and key.startswith("document:"):
                revised.append(_revised(stored_json(payload)))
        if (
            actions
            or revised
            or (earlier is not None and (earlier.actions or earlier.documents or earlier.runs))
        ):
            located, listed = actions_item(actions, revised, window.watermark + 1, end, earlier)
            encoded = dump(listed)
            items.append(EvidenceItem("actions", dump(located), encoded, digest(encoded), 1))
        # A delegated outcome is a claim about its whole batch. The partial pieces
        # remain reviewable for concrete contradictions, but cannot pass as complete.
        if first is not None:
            initial = str(first[0]).split(":", 2)
            last = db.execute(
                "SELECT source_key FROM review_feed WHERE project=? AND session=? AND "
                "seq<=? AND seq>? ORDER BY seq DESC LIMIT 1",
                (project, session, end, window.watermark),
            ).fetchone()
            following = db.execute(
                "SELECT source_key FROM review_feed WHERE project=? AND session=? AND "
                "seq>? AND seq<=? ORDER BY seq LIMIT 1",
                (project, session, end, window.end),
            ).fetchone()
            tail = str(last[0]).split(":", 2) if last is not None else []
            unfinished = (
                len(tail) >= 2
                and tail[0] == "delegated"
                and following is not None
                and str(following[0]).startswith(":".join(tail[:2]) + ":")
            )
            if (len(initial) == 3 and initial[0] == "delegated") or unfinished:
                items = [
                    replace(item, complete=0) if item.kind == "delegation" else item
                    for item in items
                ]
        return end, items

    @staticmethod
    def _open_findings(
        db: sqlite3.Connection, project: str, session: str, targets: list[tuple[str, int]]
    ) -> list[tuple[Any, ...]]:
        """The session's open findings, confirmation targets first, then rebutted, then
        open, then the rest; one more than a snapshot copies."""
        # Open finding state is copied, so a later user response cannot change what the
        # detached reviewer saw. History remains separately append-only.
        return [
            tuple(row)
            for row in db.execute(
                "SELECT finding_id,severity,category,statement,anchors,remedy,"
                "status,revision,reflags,supersedes FROM review_findings f WHERE "
                "project=? AND session=? AND category!='math' "
                "AND status NOT IN ('confirmed fixed','withdrawn') ORDER BY CASE WHEN "
                "finding_id IN (SELECT value FROM json_each(?)) THEN 0 WHEN (SELECT "
                "json_extract(t.payload,'$.action') FROM review_transitions t "
                "WHERE t.finding_id=f.finding_id ORDER BY t.revision DESC "
                "LIMIT 1)='rebut' THEN 1 WHEN status='open' THEN 2 ELSE 3 END,f.rowid "
                "LIMIT ?",
                (
                    project,
                    session,
                    dump([target[0] for target in targets]),
                    MAX_SNAPSHOT_FINDINGS + 1,
                ),
            )
        ]

    def _finding_group(
        self, project: str, session: str, row: tuple[Any, ...], granted: dict[str, Json]
    ) -> FindingGroup:
        """A finding's group: the revisions and artifacts its response claims, the
        finding with its response, and grants for the evidence its anchors name that
        ``granted`` does not hold yet."""
        from .response_bodies import pin as response_pin  # response_bodies imports this module

        finding: Json = dict(zip(_FINDING_FIELDS, row, strict=True))
        finding["anchors"] = json.loads(finding["anchors"])
        with self.documents.control.read() as tx:
            db = tx.sql
            response = db.execute(
                "SELECT reason,payload FROM review_transitions WHERE finding_id=? AND revision=?",
                (finding["finding"], finding["revision"]),
            ).fetchone()
            body_pin = response_pin(db, finding["finding"], finding["revision"])
        finding["response"] = (
            {
                "reason": response[0],
                "payload": {
                    key: value
                    for key, value in json.loads(response[1]).items()
                    if key not in ("previous_assessment", "assessment")
                },
            }
            if response
            else None
        )
        if body_pin is not None and finding["response"] is not None:
            finding["response"]["body"] = body_pin
            finding["response"]["macro_context"] = response_view(
                self.documents, project, body_pin["source"]
            )
        items: list[EvidenceItem] = []
        anchors = list(finding["anchors"])
        if finding["response"]:
            claimed = finding["response"]["payload"]
            anchors.extend(claimed.get("anchors", []))
            for locator in claimed.get("revisions", []) + claimed.get("replacements", []):
                revision = self.documents.revision(
                    project, locator["document"], locator["revision"]
                )
                if revision.info.sha256 != locator["sha256"]:
                    raise DocumentError(
                        CONFLICT, "changed revision digest differs from the claimed fix"
                    )
                encoded = dump(locator)
                items.append(EvidenceItem("document", encoded, encoded, digest(encoded), 1))
            if claimed.get("entry_revisions"):
                from sandbox_science.orchestrator.ledger import EntryView, Ledger

                from .ledger import context_items, entry_item, pinned_items
                from .quotes import quoted_items

                heads: set[str] = set()
                for pin in claimed["entry_revisions"]:
                    view = Ledger(self.documents).entry(project, pin["entry"], pin["revision"])
                    shown: list[tuple[EntryView, Json]] = [(view, {})]
                    if view.head != view.revision and view.entry_id not in heads:
                        # The entry as it reads now, so that a confirmation sees a revert.
                        heads.add(view.entry_id)
                        shown.append(
                            (
                                Ledger(self.documents).entry(project, view.entry_id),
                                {"current": True},
                            )
                        )
                    for each, more in shown:
                        location, item, ready = entry_item(each)
                        encoded = dump(item)
                        items.append(
                            EvidenceItem(
                                "ledger",
                                dump({**location, **more}),
                                encoded,
                                digest(encoded),
                                int(ready),
                            )
                        )
                        # What its evidence pins comes with it (CR-2 as amended).
                        for locator, projection in pinned_items(self, project, each):
                            encoded = dump(projection)
                            items.append(
                                EvidenceItem("artifact", dump(locator), encoded, digest(encoded), 1)
                            )
                        # And what else its evidence pins, as context (lab 40's B3).
                        for kind, _, locator, payload in context_items(self, project, each):
                            encoded = dump(payload)
                            items.append(
                                EvidenceItem(kind, dump(locator), encoded, digest(encoded), 1)
                            )
                        # And so do the recorded sources of its quotes (CR-4).
                        with self.documents.control.read() as tx:
                            quoted = quoted_items(tx.sql, project, session, each)
                        for locator, record in quoted:
                            encoded = dump(record)
                            items.append(
                                EvidenceItem("source", dump(locator), encoded, digest(encoded), 1)
                            )
            claimed_pins: set[tuple[str, int]] = set()
            for raw in claimed.get("artifact_revisions", []) + claimed.get(
                "artifact_replacements", []
            ):
                ref = ArtifactRevisionRef.model_validate(raw)
                claimed_pins.add((ref.artifact_id, ref.revision))
                location, projection, ready = self.artifact_pin(project, session, ref)
                encoded = dump(projection)
                items.append(
                    EvidenceItem("artifact", dump(location), encoded, digest(encoded), int(ready))
                )
            # The files the fix names by path, so that what it says of them is checked
            # against them (lab 41: a "fix" that misstated results/ell.json was confirmed).
            from .artifacts import cited_artifacts

            texts = [str(finding["response"].get("reason") or ""), dump(claimed)]
            for locator in claimed.get("revisions", []) + claimed.get("replacements", []):
                texts.append(
                    self.documents.revision(project, locator["document"], locator["revision"]).text
                )
            for locator, projection in cited_artifacts(self, project, session, texts):
                pin = cast(Json, locator.get("artifact") or {})
                if (pin.get("artifact_id"), pin.get("revision")) in claimed_pins:
                    continue
                encoded = dump(projection)
                cited = {**locator, "cited_by": {"finding": finding["finding"]}}
                items.append(EvidenceItem("artifact", dump(cited), encoded, digest(encoded), 1))
        # Cross-snapshot access is an explicit immutable grant. Keeping the original ID
        # makes the retained finding/response anchors readable; it does not give the
        # reviewer ambient access to other reviews.
        grants: dict[str, Json] = {}
        for anchored in anchors:
            evidence = anchored["evidence"]
            if evidence not in granted and evidence not in grants:
                grants[evidence] = self.evidence(project, session, anchored["review"], evidence)
        payload = dump(finding)
        held = bool(
            finding["response"] and finding["response"].get("macro_context", {}).get("held")
        )
        items.append(
            EvidenceItem(
                "finding",
                dump({"finding": finding["finding"], "revision": finding["revision"]}),
                payload,
                digest(payload),
                int(not held),
            )
        )
        return FindingGroup(finding["finding"], finding["revision"], items, grants, body_pin)

    def _commit_snapshot(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        review: str,
        plan: _Plan,
        *,
        author_run: str,
        owner: str,
        alias: str | None,
        config: ReviewConfig,
        config_revision: str,
        classifier: Json | None,
    ) -> bool:
        """Commit the planned review, unless its window, a pinned response body or its
        macro context changed since, a review is already running, or its confirmation
        failed too often."""
        from .response_bodies import pin as response_pin  # response_bodies imports this module

        self._window(db, project, session)
        window = plan.window
        actual = db.execute(
            "SELECT watermark,retry_end,failures FROM review_windows WHERE project=? AND session=?",
            (project, session),
        ).fetchone()
        if tuple(actual) != (window.watermark, window.retry, window.failures):
            return False
        if plan.kind != "confirmation" and self._cooling(
            db, project, session, window.failures, window.retry
        ):
            return False
        for finding, pinned in plan.response_pins.items():
            current = response_pin(db, finding, pinned["response_revision"])
            if current is None or (current["revision"], current["sha256"]) != (
                pinned["revision"],
                pinned["sha256"],
            ):
                return False
            if current["macro_context"] != pinned["macro_context"]:
                return False
        if db.execute(
            "SELECT 1 FROM review_runs WHERE project=? AND session=? AND state='running'",
            (project, session),
        ).fetchone():
            return False
        if plan.confirmation_key is not None and self.confirmation_failed(
            db, project, session, plan.confirmation_key
        ):
            return False
        if self._deferred(db, project, session, config):
            return False
        db.execute(
            """INSERT INTO review_runs(review_id,project,session,kind,author_run,owner,state,
          start_seq,end_seq,created,lease_until,alias,rubric,config,config_revision,classifier,
          coverage_complete,confirmation_key)
          VALUES(?,?,?,?,?,?,'running',?,?,?,?,?,?,?,?,?,?,?)""",
            (
                review,
                project,
                session,
                plan.kind,
                author_run,
                owner,
                window.watermark + 1,
                plan.selected_end,
                self.clock(),
                self.clock() + LEASE_SECONDS,
                alias,
                RUBRIC_VERSION,
                config.model_dump_json(),
                config_revision,
                dump(classifier or {}),
                int(plan.complete),
                plan.confirmation_key,
            ),
        )
        for ordinal, item in enumerate(plan.items, 1):
            db.execute(
                "INSERT INTO review_evidence VALUES(?,?,?,?,?,?,?,?)",
                (
                    new_id("ev"),
                    review,
                    ordinal,
                    item.kind,
                    item.locator,
                    item.payload,
                    item.sha256,
                    item.complete,
                ),
            )
        if plan.kind == "confirmation":
            for finding, revision in plan.targets:
                db.execute(
                    "INSERT INTO review_confirmation_targets VALUES(?,?,?)",
                    (review, finding, revision),
                )
        for finding, pinned in plan.response_pins.items():
            db.execute(
                "INSERT INTO review_response_pins VALUES(?,?,?,?,?,?)",
                (
                    review,
                    finding,
                    pinned["response_revision"],
                    pinned["revision"],
                    pinned["sha256"],
                    pinned["macro_context"]["revision"],
                ),
            )
        for ordinal, evidence in enumerate(plan.grants, len(plan.items) + 1):
            db.execute("INSERT INTO review_grants VALUES(?,?,?)", (review, evidence, ordinal))
        db.execute(
            "UPDATE review_windows SET last_dispatch=?,requested=0 WHERE project=? AND session=?",
            (self.clock(), project, session),
        )
        self.event(
            db,
            project,
            session,
            "review.dispatched",
            {
                "review": review,
                "type": plan.kind,
                "start": window.watermark + 1,
                "end": plan.selected_end,
                "span_complete": plan.span_complete,
                **_archived(plan.items),
            },
        )
        return True

    def _deferred(
        self, db: sqlite3.Connection, project: str, session: str, config: ReviewConfig
    ) -> bool:
        """Whether the project's reviewer has no calls left this hour (lab 38): then no
        review starts, and the session's work waits as one pending review, covering all of
        it since the last, which the next safe point or run boundary starts once calls are
        left. The first deferral of a pending review is recorded."""
        hourly = db.execute(
            "SELECT count(*) FROM review_calls WHERE project=? AND created>?",
            (project, self.clock() - 3600),
        ).fetchone()[0]
        if hourly < config.calls_per_hour:
            return False
        first = db.execute(
            "UPDATE review_windows SET requested=1 WHERE project=? AND session=? AND requested=0",
            (project, session),
        ).rowcount
        if first:
            # No review exists yet: the event is the session's.
            self.review_log.append(
                db,
                "review.deferred",
                project,
                session,
                session,
                {"reason": "calls_per_hour", "calls": hourly, "limit": config.calls_per_hour},
                datetime.fromtimestamp(self.clock(), UTC),
            )
        return True

    def off(self, project: str, session: str, reason: str) -> bool:
        """Record that the session's reviews are off, and why, once: not when its latest
        such record has had no review dispatched since; whether it recorded it."""
        self.scope(project, session)
        with self.documents.control.write() as tx:
            db = tx.sql
            said = self.review_log.recent(db, project, session, "review.off", 1)
            started = self.review_log.recent(db, project, session, "review.dispatched", 1)
            if said and (not started or started[0].seq < said[0].seq):
                return False
            self.review_log.append(
                db,
                "review.off",
                project,
                session,
                session,
                {"reason": reason},
                datetime.fromtimestamp(self.clock(), UTC),
            )
        return True

    @staticmethod
    def confirmation_failed(db: sqlite3.Connection, project: str, session: str, key: str) -> bool:
        rows = db.execute(
            "SELECT state FROM review_runs WHERE project=? AND session=? "
            "AND confirmation_key=? AND state!='running' ORDER BY rowid DESC LIMIT ?",
            (project, session, key, CONFIRMATION_ATTEMPTS),
        ).fetchall()
        return len(rows) == CONFIRMATION_ATTEMPTS and all(
            r[0] in ("failed", "dropped") for r in rows
        )

    def event(
        self, db: sqlite3.Connection, project: str, session: str, kind: str, payload: Json
    ) -> None:
        """Record a review's (``review.*``) or a finding's (``finding.*``) event in the
        open change ``db``; its payload names the review or the finding."""
        at = datetime.fromtimestamp(self.clock(), UTC)
        if kind.startswith("finding."):
            self.finding_log.append(db, kind, project, session, payload["finding"], payload, at)
        else:
            self.review_log.append(db, kind, project, session, payload["review"], payload, at)

    def load_run(self, db: sqlite3.Connection, project: str, session: str, review: str) -> Json:
        identifier(review, "rev")
        cursor = db.execute(
            "SELECT * FROM review_runs WHERE project=? AND session=? AND review_id=?",
            (project, session, review),
        )
        row = cursor.fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "no such review")
        names = [column[0] for column in cursor.description]
        return dict(zip(names, row, strict=True))

    def run(self, project: str, session: str, review: str) -> Json:
        self.scope(project, session)
        if self.documents.control.missing:
            raise DocumentError(NOT_FOUND, "no such review")
        with self.documents.control.read() as tx:
            db = tx.sql
            found = self.load_run(db, project, session, review)
        found["config"] = json.loads(found["config"])
        found["classifier"] = json.loads(found["classifier"])
        found.pop("owner", None)
        found.pop("lease_until", None)
        return found

    @staticmethod
    def _span_complete(db: sqlite3.Connection, project: str, session: str, review: str) -> bool:
        row = db.execute(
            "SELECT json_extract(payload,'$.span_complete') FROM control_events "
            "WHERE project=? AND session=? AND kind='review.dispatched' AND "
            "subject_type='review' AND subject_id=? ORDER BY seq DESC LIMIT 1",
            (project, session, review),
        ).fetchone()
        return bool(row is not None and row[0] == 1)

    def span_complete(self, project: str, session: str, review: str) -> bool:
        """Whether the immutable dispatch covered every item in its selected prefix."""
        self.scope(project, session)
        with self.documents.control.read() as tx:
            return self._span_complete(tx.sql, project, session, review)

    def renew(self, project: str, session: str, review: str, owner: str) -> None:
        with self.documents.control.write() as tx:
            db = tx.sql
            result = db.execute(
                (
                    "UPDATE review_runs SET lease_until=? WHERE project=? AND "
                    "session=? AND review_id=? AND owner=? AND state='running' AND lease_until>?"
                ),
                (self.clock() + LEASE_SECONDS, project, session, review, owner, self.clock()),
            )
            if not result.rowcount:
                raise DocumentError(CONFLICT, "review owner lease is no longer live")

    def owner(self, found: Json, owner: str) -> None:
        if (
            found["state"] != "running"
            or found["owner"] != owner
            or found["lease_until"] <= self.clock()
        ):
            raise DocumentError(POLICY_DENIED, "this reviewer no longer owns a live grant")

    def grant(self, project: str, session: str, review: str, owner: str) -> None:
        with self.documents.control.read() as tx:
            db = tx.sql
            self.owner(self.load_run(db, project, session, review), owner)

    def finish(
        self,
        project: str,
        session: str,
        review: str,
        owner: str,
        outcome: ReviewOutcome,
        summary: str,
        *,
        cap_reached: bool = False,
        require_expired: bool = False,
        retry: bool = False,
    ) -> None:
        """End a review. Failed, dropped and capped windows retain their watermark.

        Three failures defer further attempts; they never silently certify or skip the
        failed evidence.
        """
        if outcome == "running":
            raise DocumentError(INVALID_REQUEST, "a review must end with an outcome")
        # The reviewer's own summary was checked when it gave it (runner.py); the
        # platform's can quote an error, which must not keep the review from ending.
        text(summary)
        with self.documents.control.write() as tx:
            db = tx.sql
            found = self.load_run(db, project, session, review)
            if found["state"] != "running" or found["owner"] != owner:
                raise DocumentError(CONFLICT, "review is no longer owned by this run")
            if require_expired and found["lease_until"] > self.clock():
                raise DocumentError(CONFLICT, "the review owner renewed its lease")
            if outcome != "dropped":
                self.owner(found, owner)
            if outcome == "passed" and not self._span_complete(db, project, session, review):
                outcome = "inconclusive"
            cap_reached = cap_reached or bool(found["cap_reached"])
            if cap_reached and outcome in ("passed", "findings", "inconclusive"):
                outcome = "capped"
            db.execute(
                (
                    "UPDATE review_runs SET "
                    "state=?,summary=?,cap_reached=?,ended=?,lease_until=NULL WHERE "
                    "review_id=?"
                ),
                (outcome, summary, int(cap_reached), self.clock(), review),
            )
            if found["kind"] == "confirmation":
                if outcome in ("failed", "dropped") and self.confirmation_failed(
                    db, project, session, found["confirmation_key"]
                ):
                    self.event(
                        db,
                        project,
                        session,
                        "review.unreviewed",
                        {
                            "review": review,
                            "type": "confirmation",
                            "failures": 3,
                            "confirmation_key": found["confirmation_key"],
                        },
                    )
            elif outcome in ("failed", "dropped") or retry:
                window = db.execute(
                    "SELECT failures FROM review_windows WHERE project=? AND session=?",
                    (project, session),
                ).fetchone()
                failures = min(window[0] + 1, 3)
                db.execute(
                    "UPDATE review_windows SET retry_end=?,failures=? WHERE "
                    "project=? AND session=?",
                    (found["end_seq"], failures, project, session),
                )
                if failures == 3:
                    self.event(
                        db,
                        project,
                        session,
                        "review.deferred",
                        {
                            "review": review,
                            "start": found["start_seq"],
                            "end": found["end_seq"],
                            "reason": summary,
                            "retry_after_seconds": FAILURE_COOLDOWN_SECONDS,
                        },
                    )
            else:
                db.execute(
                    (
                        "UPDATE review_windows SET "
                        "watermark=max(watermark,?),retry_end=NULL,failures=0 WHERE "
                        "project=? AND session=?"
                    ),
                    (found["end_seq"], project, session),
                )
            self.event(db, project, session, "review.ended", {"review": review, "outcome": outcome})

    def recover(self, project: str, session: str) -> int:
        """Drop only expired owners, never another live harness process's review."""
        self.scope(project, session)
        with self.documents.control.read() as tx:
            db = tx.sql
            rows = list(
                db.execute(
                    (
                        "SELECT review_id,owner FROM review_runs WHERE project=? AND "
                        "session=? AND state='running' AND lease_until<=?"
                    ),
                    (project, session, self.clock()),
                )
            )
        for review, owner in rows:
            try:
                self.finish(
                    project,
                    session,
                    review,
                    owner,
                    "dropped",
                    "The review owner lease expired before a durable outcome.",
                    require_expired=True,
                )
            except DocumentError as error:
                if error.kind != CONFLICT:
                    raise
        return len(rows)

    def record(
        self, project: str, session: str, review: str, kind: str, payload: Json, *, owner: str
    ) -> None:
        encoded = dump(payload)
        if len(encoded.encode()) > MAX_ITEM_BYTES:
            raise DocumentError(INVALID_REQUEST, "review record exceeds its bound")
        with self.documents.control.write() as tx:
            db = tx.sql
            found = self.load_run(db, project, session, review)
            self.owner(found, owner)
            db.execute(
                "INSERT INTO review_records(review_id,kind,payload,created) VALUES(?,?,?,?)",
                (review, kind, encoded, self.clock()),
            )

    def reserve_call(
        self,
        project: str,
        session: str,
        review: str,
        estimated_tokens: int,
        *,
        owner: str,
        prompt_provenance: ReviewerProvenance | None = None,
    ) -> int | None:
        """Reserve before await, sharing the hourly project and per-review budgets."""
        if type(estimated_tokens) is not int or not 1 <= estimated_tokens <= 1000000000:
            raise DocumentError(INVALID_REQUEST, "invalid reviewer token reservation")
        pin = encode_provenance(prompt_provenance) if prompt_provenance is not None else None
        with self.documents.control.write() as tx:
            db = tx.sql
            found = self.load_run(db, project, session, review)
            self.owner(found, owner)
            if (
                prompt_provenance is not None
                and found["rubric"] != prompt_provenance.rubric_version
            ):
                raise DocumentError(CONFLICT, "reviewer rubric version changed before reservation")
            config = ReviewConfig.model_validate_json(found["config"])
            count = db.execute(
                "SELECT count(*) FROM review_calls WHERE project=? AND created>?",
                (project, self.clock() - 3600),
            ).fetchone()[0]
            if (
                found["state"] != "running"
                or count >= config.calls_per_hour
                or found["calls"] >= config.max_turns
                or max(found["estimated_tokens"], found["input_tokens"] + found["output_tokens"])
                + estimated_tokens
                > config.token_budget
            ):
                return None
            cursor = db.execute(
                (
                    "INSERT INTO "
                    "review_calls(project,review_id,created,estimated_tokens,prompt_provenance) "
                    "VALUES(?,?,?,?,?)"
                ),
                (project, review, self.clock(), estimated_tokens, pin),
            )
            db.execute(
                (
                    "UPDATE review_runs SET "
                    "calls=calls+1,estimated_tokens=estimated_tokens+? WHERE "
                    "review_id=?"
                ),
                (estimated_tokens, review),
            )
            return cursor.lastrowid

    def refusal(self, project: str, session: str, review: str, estimated_tokens: int) -> str:
        """Why ``reserve_call`` refused a call of ``estimated_tokens``: the limit, with what
        the review and the project used of it, for its summary (lab 38)."""
        found = self.run(project, session, review)
        config = ReviewConfig.model_validate(found["config"])
        with self.documents.control.read() as tx:
            hourly = tx.sql.execute(
                "SELECT count(*) FROM review_calls WHERE project=? AND created>?",
                (project, self.clock() - 3600),
            ).fetchone()[0]
        used = max(found["estimated_tokens"], found["input_tokens"] + found["output_tokens"])
        calls = f"after {found['calls']} {'call' if found['calls'] == 1 else 'calls'}"
        if found["state"] != "running":
            return f"The review was no longer running ({found['state']}) {calls}."
        if hourly >= config.calls_per_hour:
            return (
                f"The project's reviewer call cap was reached: {hourly} of "
                f"{config.calls_per_hour} calls in the last hour, {calls} of this review."
            )
        if found["calls"] >= config.max_turns:
            return (
                f"The review's call limit was reached: {config.max_turns} calls without an outcome."
            )
        return (
            f"The review's token budget was reached: {used:,} of {config.token_budget:,} tokens "
            f"used {calls}, and the next call needed about {estimated_tokens:,} more."
        )

    def call_provenance(self, project: str, session: str, review: str, call: int) -> ProvenanceRead:
        """Read one scoped bounded pin, including readonly pre-migration history."""
        self.scope(project, session)
        identifier(review, "rev")
        if type(call) is not int or not 1 <= call <= 2**63 - 1:
            raise DocumentError(INVALID_REQUEST, "invalid reviewer reservation ID")
        if self.documents.control.missing:
            return ProvenanceRead("absent")
        with self.documents.control.read() as tx:
            row = tx.sql.execute(
                "SELECT CASE WHEN c.prompt_provenance IS NULL THEN 0 "
                "WHEN typeof(c.prompt_provenance)='text' "
                "AND length(CAST(c.prompt_provenance AS BLOB))<=? THEN 1 ELSE 2 END,"
                "CASE WHEN typeof(c.prompt_provenance)='text' "
                "AND length(CAST(c.prompt_provenance AS BLOB))<=? "
                "THEN CAST(c.prompt_provenance AS BLOB) ELSE NULL END,"
                "r.rubric FROM review_calls c JOIN review_runs r "
                "ON c.review_id=r.review_id AND c.project=r.project "
                "WHERE r.project=? AND r.session=? AND r.review_id=? AND c.seq=? LIMIT 1",
                (MAX_PROVENANCE_BYTES, MAX_PROVENANCE_BYTES, project, session, review, call),
            ).fetchone()
        if row is None:
            return ProvenanceRead("absent")
        if row[0] == 0:
            return ProvenanceRead("legacy")
        if row[0] == 1:
            try:
                pin = ReviewerProvenance.model_validate_json(row[1])
                if pin.rubric_version == row[2]:
                    return ProvenanceRead("complete", pin)
            except ValueError:
                pass
        return ProvenanceRead("corrupt")

    def call_usage(
        self,
        project: str,
        session: str,
        review: str,
        call: int,
        *,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        owner: str,
        cost: float | None = None,
        cost_source: CostSource | None = None,
        intent_id: str | None = None,
    ) -> None:
        """What the call used and cost, with where the cost came from (the gateway, or
        its model information): the project's cost and its daily limit count it
        (run-service spec RS-6a)."""
        if any(
            value is not None and (type(value) is not int or not 0 <= value <= 1000000000)
            for value in (input_tokens, output_tokens)
        ) or not _reported_cost(cost):
            raise DocumentError(INVALID_REQUEST, "invalid provider usage")
        with self.documents.control.write() as tx:
            db = tx.sql
            self.owner(self.load_run(db, project, session, review), owner)
            updated = db.execute(
                (
                    "UPDATE review_calls SET "
                    "input_tokens=?,output_tokens=?,resolved_model=?,cost=?,cost_source=?,"
                    "answered=1 "
                    "WHERE seq=? AND project=? AND review_id=? AND answered=0"
                ),
                (
                    input_tokens,
                    output_tokens,
                    clipped(model, 256) if model else None,
                    cost,
                    source_of(cost, cost_source),
                    call,
                    project,
                    review,
                ),
            )
            from sandbox_science.orchestrator.continuity.materializations import link

            link(db, "review_calls", call, intent_id)
            if updated.rowcount:
                # What the call used replaces its estimate, so the budget counts the
                # provider's usage rather than the estimates' overreach (lab 38).
                estimate = db.execute(
                    "SELECT estimated_tokens FROM review_calls WHERE seq=?", (call,)
                ).fetchone()[0]
                used = estimate if input_tokens is None else input_tokens + (output_tokens or 0)
                db.execute(
                    (
                        "UPDATE review_runs SET input_tokens=input_tokens+?,"
                        "output_tokens=output_tokens+?,resolved_model=coalesce(?,resolved_model),"
                        "estimated_tokens=max(estimated_tokens-?,0)+? WHERE review_id=?"
                    ),
                    (
                        input_tokens or 0,
                        output_tokens or 0,
                        clipped(model, 256) if model else None,
                        estimate,
                        used,
                        review,
                    ),
                )
                config = ReviewConfig.model_validate_json(
                    self.load_run(db, project, session, review)["config"]
                )
                db.execute(
                    "UPDATE review_runs SET cap_reached=1 WHERE review_id=? "
                    "AND input_tokens+output_tokens>=?",
                    (review, config.token_budget),
                )

    def manifest(
        self, project: str, session: str, review: str, *, after: int = 0, limit: int = 10
    ) -> Json:
        if type(after) is not int or after < 0 or after > 1000000:
            raise DocumentError(INVALID_REQUEST, "invalid manifest cursor")
        if type(limit) is not int or not 1 <= limit <= MANIFEST_PAGE:
            raise DocumentError(INVALID_REQUEST, f"manifest limit is 1 to {MANIFEST_PAGE}")
        found = self.run(project, session, review)
        with self.documents.control.read() as tx:
            db = tx.sql
            rows = list(
                db.execute(
                    (
                        "SELECT evidence_id,ordinal,kind,locator,sha256,complete FROM ("
                        "SELECT evidence_id,ordinal,kind,locator,sha256,complete FROM "
                        "review_evidence WHERE review_id=? UNION ALL "
                        "SELECT e.evidence_id,g.ordinal,e.kind,e.locator,e.sha256,e.complete "
                        "FROM review_grants g JOIN review_evidence e USING(evidence_id) "
                        "WHERE g.review_id=?) WHERE ordinal>? ORDER BY ordinal LIMIT ?"
                    ),
                    (review, review, after, limit + 1),
                )
            )
        return {
            "review": found,
            "evidence": [
                {
                    "evidence": r[0],
                    "ordinal": r[1],
                    "kind": r[2],
                    "locator": stored_json(r[3]),
                    "sha256": r[4],
                    "complete": bool(r[5]),
                }
                for r in rows[:limit]
            ],
            "next_after": rows[limit - 1][1] if len(rows) > limit else None,
        }

    def evidence(self, project: str, session: str, review: str, evidence: str) -> Json:
        self.scope(project, session)
        identifier(evidence, "ev")
        with self.documents.control.read() as tx:
            db = tx.sql
            self.load_run(db, project, session, review)
            row = db.execute(
                (
                    "SELECT kind,locator,payload,sha256,complete FROM review_evidence "
                    "WHERE evidence_id=? AND (review_id=? OR EXISTS(SELECT 1 FROM "
                    "review_grants g WHERE g.review_id=? AND "
                    "g.evidence_id=review_evidence.evidence_id))"
                ),
                (evidence, review, review),
            ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "evidence is outside this snapshot")
        location = stored_json(row[1])
        payload = stored_json(row[2])
        if row[0] == "artifact" and payload.get("projection") == 1:
            from .artifacts import artifact_text

            if (artifact_text(location, payload) is not None) != bool(row[4]):
                raise DocumentError(
                    CONFLICT, "artifact availability differs from evidence coverage"
                )
        return {
            "evidence": evidence,
            "kind": row[0],
            "locator": location,
            "payload": payload,
            "sha256": row[3],
            "complete": bool(row[4]),
        }

    def artifact_pin(
        self,
        project: str,
        session: str,
        reference: ArtifactRevisionRef,
    ) -> tuple[Json, Json, bool]:
        """Grant only an exact canonical artifact already observed for this session."""
        self.scope(project, session)
        if reference.project_id != project:
            raise DocumentError(NOT_FOUND, "artifact evidence is outside this project")
        with self.documents.control.read() as tx:
            db = tx.sql
            row = db.execute(
                "SELECT locator,payload,sha256,complete FROM review_feed "
                "WHERE project=? AND session=? AND kind='artifact' "
                "AND CASE WHEN json_valid(locator) THEN "
                "json_extract(locator,'$.artifact.artifact_id') END=? "
                "AND CASE WHEN json_valid(locator) THEN "
                "json_extract(locator,'$.artifact.revision') END=? ORDER BY seq LIMIT 1",
                (project, session, reference.artifact_id, reference.revision),
            ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "exact artifact revision has no session evidence grant")
        location, payload = stored_json(row[0]), stored_json(row[1])
        if ArtifactRevisionRef.model_validate(location.get("artifact")) != reference:
            raise DocumentError(
                CONFLICT, "claimed artifact reference differs from session evidence"
            )
        from .artifacts import artifact_text

        if (artifact_text(location, payload) is not None) != bool(row[3]):
            raise DocumentError(CONFLICT, "artifact availability differs from evidence coverage")
        return location, payload, bool(row[3])

    def read_evidence(
        self,
        project: str,
        session: str,
        review: str,
        evidence: str,
        *,
        start: int = 0,
        count: int = 16000,
        block: object = None,
        owner: str | None = None,
    ) -> Json:
        if (
            type(start) is not int
            or type(count) is not int
            or not 0 <= start <= 100000000
            or not 1 <= count <= 16000
            or (
                block is not None
                and (not isinstance(block, str) or re.fullmatch(r"b[0-9]{1,9}", block) is None)
            )
        ):
            raise DocumentError(INVALID_REQUEST, "invalid evidence slice")
        item = self.evidence(project, session, review, evidence)
        locator = item["locator"]
        if item["kind"] == "document":
            revision = self.documents.revision(project, locator["document"], locator["revision"])
            if revision.info.sha256 != locator["sha256"]:
                raise DocumentError(CONFLICT, "pinned document digest changed")
            source = revision.text
            if not source:
                source = dump(
                    {"empty_document_revision": locator, "tombstone": revision.info.tombstone}
                )
            if block is not None:
                mapped = self.documents.mapped(project, locator["document"], locator["revision"])
                index = mapped.index(block)
                if index is None:
                    raise DocumentError(NOT_FOUND, "block is outside the pinned revision")
                b = mapped.parsed.blocks[index]
                source = mapped.parsed.text[b.start : b.end]
        else:
            if block is not None:
                raise DocumentError(INVALID_REQUEST, "block locators apply to documents only")
            source = dump(item["payload"])
            if item["kind"] == "artifact" and item["payload"].get("projection") == 1:
                from .artifacts import artifact_text

                copied = artifact_text(item["locator"], item["payload"])
                if copied is not None:
                    source = copied
                    # An empty source remains exact; exposure is recorded separately.

        result: Json = {
            "evidence": evidence,
            "sha256": item["sha256"],
            "locator": {**locator, "block": block, "start": start},
            "text": source[start : start + count],
            "start": start,
            "next_start": start + count if start + count < len(source) else None,
            "total_characters": len(source),
            "complete": item["complete"],
        }
        if item["kind"] == "artifact" and item["payload"].get("projection") == 1:
            result["source_sha256"] = locator.get("artifact", {}).get("sha256")
            result["availability"] = item["payload"].get("availability")
            result["locator"]["empty_source"] = source == "" and item["complete"]
        result["locator"]["returned_characters"] = len(result["text"])
        result["locator"]["total_characters"] = len(source)
        if owner is None:
            return result
        with self.documents.control.write() as tx:
            db = tx.sql
            found = self.load_run(db, project, session, review)
            self.owner(found, owner)
            if not result["text"] and not result["locator"].get("empty_source"):
                return result
            db.execute(
                (
                    "INSERT INTO "
                    "review_reads(review_id,evidence_id,locator,sha256,created) "
                    "VALUES(?,?,?,?,?)"
                ),
                (review, evidence, dump(result["locator"]), item["sha256"], self.clock()),
            )
        return result

    def records(self, project: str, session: str, review: str, *, after: int = 0) -> Json:
        if type(after) is not int or not 0 <= after <= 999999999:
            raise DocumentError(INVALID_REQUEST, "invalid review record cursor")
        with self.documents.control.read() as tx:
            db = tx.sql
            self.load_run(db, project, session, review)
            values: list[Json] = []
            size = 0
            more = False
            for seq, kind, payload, created in db.execute(
                "SELECT seq,kind,payload,created FROM review_records WHERE review_id=? "
                "AND seq>? ORDER BY seq LIMIT 21",
                (review, after),
            ):
                added = len(payload.encode()) + 256
                if len(values) == 20 or (values and size + added > 1024 * 1024):
                    more = True
                    break
                values.append(
                    {
                        "sequence": seq,
                        "kind": kind,
                        "payload": json.loads(payload),
                        "created": created,
                    }
                )
                size += added
        return {"records": values, "next_after": values[-1]["sequence"] if more else None}

    def listing(self, project: str, session: str, *, after: str | None = None) -> Json:
        self.scope(project, session)
        if after is not None:
            identifier(after, "rev")
        if self.documents.control.missing:
            return {
                "reviews": [],
                "next_after": None,
                "unreviewed": [],
                "waiting": None,
                "off": None,
            }
        with self.documents.control.read() as tx:
            db = tx.sql
            cursor = db.execute("SELECT coalesce(max(rowid),0)+1 FROM review_runs").fetchone()[0]
            if after is not None:
                row = db.execute(
                    "SELECT rowid FROM review_runs WHERE project=? AND session=? AND review_id=?",
                    (project, session, after),
                ).fetchone()
                if row is None:
                    raise DocumentError(NOT_FOUND, "no such review cursor")
                cursor = row[0]
            ids = [
                r[0]
                for r in db.execute(
                    (
                        "SELECT review_id FROM review_runs WHERE project=? AND session=? "
                        "AND rowid<? ORDER BY rowid DESC LIMIT 51"
                    ),
                    (project, session, cursor),
                )
            ]
            unreviewed = [
                event.payload
                for event in self.review_log.recent(db, project, session, "review.unreviewed", 50)
            ]
            # A deferral no review was dispatched after: the session's work is waiting.
            deferred = self.review_log.recent(db, project, session, "review.deferred", 1)
            started = self.review_log.recent(db, project, session, "review.dispatched", 1)
            waiting = (
                {**deferred[0].payload, "since": deferred[0].time.timestamp()}
                if deferred and (not started or started[0].seq < deferred[0].seq)
                else None
            )
            # Reviews off, and none dispatched since: the session's work goes unreviewed.
            stopped = self.review_log.recent(db, project, session, "review.off", 1)
            off = (
                {**stopped[0].payload, "since": stopped[0].time.timestamp()}
                if stopped and (not started or started[0].seq < stopped[0].seq)
                else None
            )
        values: list[Json] = []
        size = 0
        for review in ids[:50]:
            value = self.run(project, session, review)
            added = len(dump(value).encode()) + 32
            if values and size + added > 1024 * 1024:
                break
            values.append(value)
            size += added
        return {
            "reviews": values,
            "next_after": values[-1]["review_id"] if len(ids) > len(values) else None,
            "unreviewed": unreviewed,
            "waiting": waiting,
            "off": off,
        }
