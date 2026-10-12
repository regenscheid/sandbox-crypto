# SPDX-License-Identifier: MIT
"""Durable review windows and immutable evidence in the shared control store.

No read follows a mutable worker path. Executions/transcript entries are copied as bounded
observations; document reads pin immutable revision numbers and verify their hashes.
"""

# Logical control-store modules share the same transaction and connection machinery.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from typing import Any, Final, Literal

from pydantic import ValidationError

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.documents.blocks import check_text
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

from .models import ReviewConfig, ReviewOutcome, ReviewType, Scope
from .rubric import RUBRIC_VERSION

# Heterogeneous evidence is JSON validated by its producing boundary and bounded here.
Json = dict[str, Any]
MAX_ITEM_BYTES: Final = 256 * 1024
MAX_SNAPSHOT_BYTES: Final = 8 * 1024 * 1024
MAX_SNAPSHOT_ITEMS: Final = 256
LEASE_SECONDS: Final = 120

SCHEMA: Final = (
    """CREATE TABLE IF NOT EXISTS review_windows (
      project TEXT NOT NULL,session TEXT NOT NULL,watermark INTEGER NOT NULL DEFAULT 0,
      document_cursor INTEGER NOT NULL DEFAULT 0,last_dispatch REAL,retry_end INTEGER,
      failures INTEGER NOT NULL DEFAULT 0,requested INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(project,session)) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_config (
      project TEXT NOT NULL,number INTEGER NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,
      PRIMARY KEY(project,number)) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_feed (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,session TEXT NOT NULL,
      source_key TEXT NOT NULL,kind TEXT NOT NULL,locator TEXT NOT NULL,payload TEXT NOT NULL,
      sha256 TEXT NOT NULL,complete INTEGER NOT NULL,characters INTEGER NOT NULL,
      structural INTEGER NOT NULL,artifacts INTEGER NOT NULL,cells INTEGER NOT NULL,
      effects INTEGER NOT NULL,created REAL NOT NULL,UNIQUE(project,session,source_key)) STRICT""",
    "CREATE INDEX IF NOT EXISTS review_feed_scope ON review_feed(project,session,seq)",
    """CREATE TABLE IF NOT EXISTS review_runs (
      review_id TEXT PRIMARY KEY,project TEXT NOT NULL,session TEXT NOT NULL,kind TEXT NOT NULL,
      author_run TEXT NOT NULL,owner TEXT NOT NULL,state TEXT NOT NULL,start_seq INTEGER NOT NULL,
      end_seq INTEGER NOT NULL,created REAL NOT NULL,ended REAL,lease_until REAL,
      alias TEXT,resolved_model TEXT,rubric TEXT NOT NULL,config TEXT NOT NULL,
      config_revision TEXT NOT NULL,classifier TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '',
      cap_reached INTEGER NOT NULL DEFAULT 0,input_tokens INTEGER NOT NULL DEFAULT 0,
      output_tokens INTEGER NOT NULL DEFAULT 0,estimated_tokens INTEGER NOT NULL DEFAULT 0,
      calls INTEGER NOT NULL DEFAULT 0,coverage_complete INTEGER NOT NULL,
      confirmation_key TEXT) STRICT""",
    """CREATE UNIQUE INDEX IF NOT EXISTS review_one_running
      ON review_runs(project,session) WHERE state='running'""",
    """CREATE TABLE IF NOT EXISTS review_evidence (
      evidence_id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES review_runs(review_id),
      ordinal INTEGER NOT NULL,kind TEXT NOT NULL,locator TEXT NOT NULL,payload TEXT NOT NULL,
      sha256 TEXT NOT NULL,complete INTEGER NOT NULL,UNIQUE(review_id,ordinal)) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_reads (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      review_id TEXT NOT NULL REFERENCES review_runs(review_id),
      evidence_id TEXT NOT NULL REFERENCES review_evidence(evidence_id),locator TEXT NOT NULL,
      sha256 TEXT NOT NULL,created REAL NOT NULL) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_confirmation_targets (
      review_id TEXT NOT NULL REFERENCES review_runs(review_id),finding_id TEXT NOT NULL,
      revision INTEGER NOT NULL,PRIMARY KEY(review_id,finding_id)) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_grants (
      review_id TEXT NOT NULL REFERENCES review_runs(review_id),
      evidence_id TEXT NOT NULL REFERENCES review_evidence(evidence_id),
      ordinal INTEGER NOT NULL,PRIMARY KEY(review_id,evidence_id),
      UNIQUE(review_id,ordinal)) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_calls (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,review_id TEXT NOT NULL,
      created REAL NOT NULL,input_tokens INTEGER,output_tokens INTEGER,resolved_model TEXT,
      estimated_tokens INTEGER NOT NULL,answered INTEGER NOT NULL DEFAULT 0) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_records (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      review_id TEXT NOT NULL REFERENCES review_runs(review_id),
      kind TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_findings (
      finding_id TEXT PRIMARY KEY,project TEXT NOT NULL,session TEXT NOT NULL,
      review_id TEXT NOT NULL,
      severity TEXT NOT NULL,category TEXT NOT NULL,statement TEXT NOT NULL,anchors TEXT NOT NULL,
      remedy TEXT,status TEXT NOT NULL,revision INTEGER NOT NULL,reflags INTEGER NOT NULL DEFAULT 0,
      dedupe TEXT NOT NULL,supersedes TEXT,created REAL NOT NULL) STRICT""",
    (
        "CREATE INDEX IF NOT EXISTS review_findings_scope ON "
        "review_findings(project,session,created)"
    ),
    """CREATE TABLE IF NOT EXISTS review_transitions (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      finding_id TEXT NOT NULL REFERENCES review_findings(finding_id),
      revision INTEGER NOT NULL,status TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,
      payload TEXT NOT NULL,created REAL NOT NULL,UNIQUE(finding_id,revision)) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_operations (
      project TEXT NOT NULL,actor TEXT NOT NULL,operation_id TEXT NOT NULL,
      fingerprint TEXT NOT NULL,result TEXT NOT NULL,
      PRIMARY KEY(project,actor,operation_id)) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_notices (
      finding_id TEXT NOT NULL,revision INTEGER NOT NULL,project TEXT NOT NULL,
      session TEXT NOT NULL,
      claimed_run TEXT,claim_until REAL,delivered_run TEXT,
      PRIMARY KEY(finding_id,revision)) STRICT""",
    """CREATE TABLE IF NOT EXISTS review_events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,session TEXT NOT NULL,
      kind TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL) STRICT""",
    *tuple(
        f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()} BEFORE {verb} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'review evidence/history is immutable'); END"
        for table in (
            "review_feed",
            "review_evidence",
            "review_grants",
            "review_confirmation_targets",
            "review_reads",
            "review_records",
            "review_transitions",
            "review_config",
            "review_events",
        )
        for verb in ("UPDATE", "DELETE")
    ),
)


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


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


def clipped(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(0, (limit - 30) // 2)
    return value[:half] + "\n… evidence omitted …\n" + value[-half:]


class ReviewStore:
    """The bounded review repository; DocumentStore initializes the shared database first."""

    def __init__(self, documents: DocumentStore, *, clock: Callable[[], float] = time.time) -> None:
        self.documents, self.clock = documents, clock
        if not documents.readonly:
            with documents._write() as db:
                for statement in SCHEMA:
                    db.execute(statement)

    def available(self, db: sqlite3.Connection | None) -> bool:
        return (
            db is not None
            and db.execute("SELECT 1 FROM sqlite_master WHERE name='review_runs'").fetchone()
            is not None
        )

    def scope(self, project: str, session: str) -> None:
        try:
            Scope(project_id=project, session_id=session)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid review scope") from None
        try:
            found = ProjectStore(self.documents.path.parent).get(project)
        except ProjectError:
            raise DocumentError(NOT_FOUND, "no such review project") from None
        if session not in found.sessions:
            raise DocumentError(NOT_FOUND, "no such review session")

    def config(self, project: str) -> tuple[ReviewConfig, str]:
        with self.documents._read() as db:
            row = (
                db.execute(
                    (
                        "SELECT payload FROM review_config WHERE project=? ORDER BY "
                        "number DESC LIMIT 1"
                    ),
                    (project,),
                ).fetchone()
                if self.available(db) and db is not None
                else None
            )
        value = ReviewConfig.model_validate_json(row[0]) if row else ReviewConfig()
        return value, digest(value.model_dump_json())

    def configure(self, project: str, session: str, config: ReviewConfig) -> str:
        self.scope(project, session)
        with self.documents._write() as db:
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
        kind: Literal["transcript", "execution", "document", "artifact"],
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
        with self.documents._write() as db:
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
        explicit incomplete coverage and is resumed at the next boundary.
        """
        self.scope(project, session)
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
            target = db.execute(
                "SELECT coalesce(max(seq),0) FROM events WHERE project_id=?", (project,)
            ).fetchone()[0]
        after = 0
        for _ in range(50 if drain else 1):
            with self.documents._read() as db:
                assert db is not None  # noqa: S101
                current = db.execute(
                    "SELECT document_cursor FROM review_windows WHERE project=? AND session=?",
                    (project, session),
                ).fetchone()
                after = current[0] if current else 0
                rows = db.execute(
                    """SELECT seq,document_id,session_id,kind,
                    json_extract(payload,'$.author_kind'),json_extract(payload,'$.revision'),
                    json_extract(payload,'$.agent_characters'),
                    json_array_length(payload,'$.structural_added')
                    FROM events WHERE project_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT 200""",
                    (project, after, target),
                ).fetchall()
            for _seq, document, owner, kind, author, number, chars, structural in rows:
                if kind == "document.revision_created" and owner == session and author == "agent":
                    revision = self.documents.revision(project, document, number)
                    self.observe(
                        project,
                        session,
                        f"document:{document}:{number}",
                        "document",
                        {
                            "document": document,
                            "revision": number,
                            "sha256": revision.info.sha256,
                            "run": revision.info.author.run_id,
                        },
                        {
                            "document": document,
                            "revision": number,
                            "sha256": revision.info.sha256,
                            "author": revision.info.author.model_dump(mode="json"),
                            "reason": revision.info.reason,
                            "tombstone": revision.info.tombstone,
                        },
                        characters=chars or 0,
                        structural=structural or 0,
                        effects=True,
                    )
            if rows:
                after = rows[-1][0]
                with self.documents._write() as db:
                    self._window(db, project, session)
                    db.execute(
                        "UPDATE review_windows SET document_cursor=max(document_cursor,?) "
                        "WHERE project=? AND session=?",
                        (after, project, session),
                    )
            if not rows or after >= target:
                break
        return {"cursor": after, "target": target, "complete": after >= target}

    def run_effects(self, project: str, session: str, run: str) -> bool:
        """Derive effects from durable run-attributed observations, not callbacks."""
        self.scope(project, session)
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
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
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
            return dict(
                db.execute(
                    "SELECT status,count(*) FROM review_findings WHERE project=? AND session=? "
                    "GROUP BY status",
                    (project, session),
                )
            )

    def progress(self, project: str, session: str) -> Json:
        self.scope(project, session)
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
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
        return {"watermark": watermark, "retry_end": retry, "remaining": remaining}

    def request(self, project: str, session: str) -> None:
        self.scope(project, session)
        with self.documents._write() as db:
            self._window(db, project, session)
            db.execute(
                "UPDATE review_windows SET requested=1 WHERE project=? AND session=?",
                (project, session),
            )

    def due(self, project: str, session: str) -> bool:
        config, _ = self.config(project)
        if not config.enabled:
            return False
        with self.documents._read() as db:
            if not self.available(db) or db is None:
                return False
            row = db.execute(
                (
                    "SELECT watermark,last_dispatch,retry_end,requested FROM "
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
        elapsed = self.clock() - row[1] if row[1] is not None else float("inf")
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
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
            return [
                tuple(row)
                for row in db.execute(
                    "SELECT f.finding_id,f.revision FROM review_findings f WHERE project=? "
                    "AND session=? AND status='claimed fixed' AND NOT EXISTS(SELECT 1 FROM "
                    "review_confirmation_targets t JOIN review_runs r USING(review_id) "
                    "WHERE t.finding_id=f.finding_id AND t.revision=f.revision AND "
                    "r.state NOT IN ('running','failed','dropped')) AND (SELECT count(*) FROM "
                    "review_confirmation_targets t JOIN review_runs r USING(review_id) "
                    "WHERE t.finding_id=f.finding_id AND t.revision=f.revision AND "
                    "r.state IN ('failed','dropped'))<3 ORDER BY f.rowid LIMIT 100",
                    (project, session),
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
    ) -> str | None:
        """Pin a bounded prefix. No unseen overflow item is skipped by the watermark."""
        self.scope(project, session)
        config, config_revision = self.config(project)
        if not config.enabled:
            return None
        targets = self.confirmation_targets(project, session)
        if kind == "confirmation" and not targets:
            return None
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
            window = db.execute(
                "SELECT watermark,retry_end FROM review_windows WHERE project=? AND session=?",
                (project, session),
            ).fetchone()
            watermark, retry = window if window else (0, None)
            end = (
                retry
                if retry is not None
                else db.execute(
                    "SELECT coalesce(max(seq),?) FROM review_feed WHERE project=? AND session=?",
                    (watermark, project, session),
                ).fetchone()[0]
            )
            items: list[tuple[Any, ...]] = []
            size = 0
            for row in db.execute(
                (
                    "SELECT seq,kind,locator,payload,sha256,complete FROM review_feed "
                    "WHERE project=? AND session=? AND seq>? AND seq<=? ORDER BY seq "
                    "LIMIT ?"
                ),
                (project, session, watermark, end, MAX_SNAPSHOT_ITEMS),
            ):
                added = len(row[2].encode()) + len(row[3].encode()) + 256
                if items and size + added > MAX_SNAPSHOT_BYTES:
                    break
                items.append(tuple(row))
                size += added
            if kind == "confirmation":
                # Fix targets and their original evidence take precedence over new
                # feed items; the latter retain their watermark for a later review.
                items, size = [], 0
            selected_end = items[-1][0] if items else watermark
            # Open finding state is copied, so a later user response cannot change
            # what the detached reviewer saw. History remains separately append-only.
            findings = list(
                db.execute(
                    (
                        "SELECT finding_id,severity,category,statement,anchors,remedy,"
                        "status,revision,reflags,supersedes FROM review_findings f WHERE "
                        "project=? AND session=? AND status NOT IN ('confirmed fixed',"
                        "'withdrawn') ORDER BY CASE WHEN finding_id IN "
                        "(SELECT value FROM json_each(?)) THEN 0 WHEN (SELECT "
                        "json_extract(t.payload,'$.action') FROM review_transitions t "
                        "WHERE t.finding_id=f.finding_id ORDER BY t.revision DESC "
                        "LIMIT 1)='rebut' THEN 1 WHEN status='open' "
                        "THEN 2 ELSE 3 END,f.rowid LIMIT 101"
                    ),
                    (project, session, dump([target[0] for target in targets])),
                )
            )
        prepared: list[tuple[str, str, str, str, int]] = []
        grants: dict[str, Json] = {}
        included_findings = 0
        confirmation_targets: list[tuple[str, int]] = []
        for _, item_kind, location, payload, sha, complete in items:
            prepared.append((item_kind, location, payload, sha, complete))
        for row in findings[:100]:
            finding: Json = dict(
                zip(
                    (
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
                    ),
                    row,
                    strict=True,
                )
            )
            finding["anchors"] = json.loads(finding["anchors"])
            with self.documents._read() as db:
                assert db is not None  # noqa: S101
                response = db.execute(
                    "SELECT reason,payload FROM review_transitions WHERE finding_id=? "
                    "AND revision=?",
                    (row[0], row[7]),
                ).fetchone()
            finding["response"] = (
                {
                    "reason": response[0],
                    "payload": json.loads(response[1]),
                }
                if response
                else None
            )
            payload = dump(finding)
            group: list[tuple[str, str, str, str, int]] = []
            group_grants: dict[str, Json] = {}
            evidence_anchors = list(finding["anchors"])
            if finding["response"]:
                response_payload = finding["response"]["payload"]
                evidence_anchors.extend(response_payload.get("anchors", []))
                for locator in response_payload.get("revisions", []) + response_payload.get(
                    "replacements", []
                ):
                    revision = self.documents.revision(
                        project, locator["document"], locator["revision"]
                    )
                    if revision.info.sha256 != locator["sha256"]:
                        raise DocumentError(
                            CONFLICT, "changed revision digest differs from the claimed fix"
                        )
                    encoded = dump(locator)
                    group.append(("document", encoded, encoded, digest(encoded), 1))
            # Cross-snapshot access is an explicit immutable grant. Keeping the
            # original ID makes the retained finding/response anchors readable;
            # it does not give the reviewer ambient access to other reviews.
            for anchored in evidence_anchors:
                evidence_id = anchored["evidence"]
                if evidence_id not in grants and evidence_id not in group_grants:
                    group_grants[evidence_id] = self.evidence(
                        project, session, anchored["review"], evidence_id
                    )
            group.append(
                (
                    "finding",
                    dump({"finding": row[0], "revision": row[7]}),
                    payload,
                    digest(payload),
                    1,
                )
            )
            added = sum(len(p[1].encode()) + len(p[2].encode()) + 256 for p in group)
            added += sum(
                len(dump(p["locator"]).encode()) + len(dump(p["payload"]).encode()) + 256
                for p in group_grants.values()
            )
            if (
                size + added > MAX_SNAPSHOT_BYTES
                or len(prepared) + len(grants) + len(group) + len(group_grants) > MAX_SNAPSHOT_ITEMS
            ):
                continue
            prepared.extend(group)
            if (finding["finding"], finding["revision"]) in targets:
                confirmation_targets.append((finding["finding"], finding["revision"]))
            grants.update(group_grants)
            included_findings += 1
            size += added
        complete = (
            coverage_complete
            and selected_end == end
            and included_findings == len(findings)
            and all(item[4] for item in prepared)
            and all(item["complete"] for item in grants.values())
        )
        confirmation_key = digest(dump(confirmation_targets)) if kind == "confirmation" else None
        review = new_id("rev")
        with self.documents._write() as db:
            self._window(db, project, session)
            actual = db.execute(
                "SELECT watermark,retry_end FROM review_windows WHERE project=? AND session=?",
                (project, session),
            ).fetchone()
            if tuple(actual) != (watermark, retry):
                return None
            if db.execute(
                "SELECT 1 FROM review_runs WHERE project=? AND session=? AND state='running'",
                (project, session),
            ).fetchone():
                return None
            if confirmation_key is not None and self.confirmation_failed(
                db, project, session, confirmation_key
            ):
                return None
            db.execute(
                """INSERT INTO review_runs(review_id,project,session,kind,author_run,owner,state,
              start_seq,end_seq,created,lease_until,alias,rubric,config,config_revision,classifier,
              coverage_complete,confirmation_key)
              VALUES(?,?,?,?,?,?,'running',?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    review,
                    project,
                    session,
                    kind,
                    author_run,
                    owner,
                    watermark + 1,
                    selected_end,
                    self.clock(),
                    self.clock() + LEASE_SECONDS,
                    alias,
                    RUBRIC_VERSION,
                    config.model_dump_json(),
                    config_revision,
                    dump(classifier or {}),
                    int(complete),
                    confirmation_key,
                ),
            )
            for ordinal, (item_kind, location, payload, sha, item_complete) in enumerate(
                prepared, 1
            ):
                db.execute(
                    "INSERT INTO review_evidence VALUES(?,?,?,?,?,?,?,?)",
                    (
                        new_id("ev"),
                        review,
                        ordinal,
                        item_kind,
                        location,
                        payload,
                        sha,
                        item_complete,
                    ),
                )
            if kind == "confirmation":
                for finding_id, revision in confirmation_targets:
                    db.execute(
                        "INSERT INTO review_confirmation_targets VALUES(?,?,?)",
                        (review, finding_id, revision),
                    )
            for ordinal, evidence_id in enumerate(grants, len(prepared) + 1):
                db.execute(
                    "INSERT INTO review_grants VALUES(?,?,?)", (review, evidence_id, ordinal)
                )
            db.execute(
                (
                    "UPDATE review_windows SET last_dispatch=?,requested=0 WHERE "
                    "project=? AND session=?"
                ),
                (self.clock(), project, session),
            )
            self.event(
                db,
                project,
                session,
                "review.dispatched",
                {"review": review, "type": kind, "start": watermark + 1, "end": selected_end},
            )
        return review

    @staticmethod
    def confirmation_failed(db: sqlite3.Connection, project: str, session: str, key: str) -> bool:
        rows = db.execute(
            "SELECT state FROM review_runs WHERE project=? AND session=? "
            "AND confirmation_key=? AND state!='running' ORDER BY rowid DESC LIMIT 3",
            (project, session, key),
        ).fetchall()
        return len(rows) == 3 and all(r[0] in ("failed", "dropped") for r in rows)

    def event(
        self, db: sqlite3.Connection, project: str, session: str, kind: str, payload: Json
    ) -> None:
        db.execute(
            "INSERT INTO review_events(project,session,kind,payload,created) VALUES(?,?,?,?,?)",
            (project, session, kind, dump(payload), self.clock()),
        )

    def _run(self, db: sqlite3.Connection, project: str, session: str, review: str) -> Json:
        identifier(review, "rev")
        row = db.execute(
            "SELECT * FROM review_runs WHERE project=? AND session=? AND review_id=?",
            (project, session, review),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "no such review")
        names = [c[1] for c in db.execute("PRAGMA table_info(review_runs)")]
        return dict(zip(names, row, strict=True))

    def run(self, project: str, session: str, review: str) -> Json:
        self.scope(project, session)
        with self.documents._read() as db:
            if not self.available(db) or db is None:
                raise DocumentError(NOT_FOUND, "no such review")
            found = self._run(db, project, session, review)
        found["config"] = json.loads(found["config"])
        found["classifier"] = json.loads(found["classifier"])
        found.pop("owner", None)
        found.pop("lease_until", None)
        return found

    def renew(self, project: str, session: str, review: str, owner: str) -> None:
        with self.documents._write() as db:
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
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
            self.owner(self._run(db, project, session, review), owner)

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
    ) -> None:
        if outcome == "running":
            raise DocumentError(INVALID_REQUEST, "a review must end with an outcome")
        text(summary)
        with self.documents._write() as db:
            found = self._run(db, project, session, review)
            if found["state"] != "running" or found["owner"] != owner:
                raise DocumentError(CONFLICT, "review is no longer owned by this run")
            if require_expired and found["lease_until"] > self.clock():
                raise DocumentError(CONFLICT, "the review owner renewed its lease")
            if outcome != "dropped":
                self.owner(found, owner)
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
            elif outcome in ("failed", "dropped"):
                window = db.execute(
                    "SELECT failures FROM review_windows WHERE project=? AND session=?",
                    (project, session),
                ).fetchone()
                failures = window[0] + 1
                if failures >= 3:
                    db.execute(
                        (
                            "UPDATE review_windows SET "
                            "watermark=max(watermark,?),retry_end=NULL,failures=0 WHERE "
                            "project=? AND session=?"
                        ),
                        (found["end_seq"], project, session),
                    )
                    self.event(
                        db,
                        project,
                        session,
                        "review.unreviewed",
                        {
                            "review": review,
                            "start": found["start_seq"],
                            "end": found["end_seq"],
                            "failures": failures,
                        },
                    )
                else:
                    db.execute(
                        (
                            "UPDATE review_windows SET retry_end=?,failures=? WHERE "
                            "project=? AND session=?"
                        ),
                        (found["end_seq"], failures, project, session),
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
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
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
        with self.documents._write() as db:
            found = self._run(db, project, session, review)
            self.owner(found, owner)
            db.execute(
                "INSERT INTO review_records(review_id,kind,payload,created) VALUES(?,?,?,?)",
                (review, kind, encoded, self.clock()),
            )

    def reserve_call(
        self, project: str, session: str, review: str, estimated_tokens: int, *, owner: str
    ) -> int | None:
        """Reserve before await, sharing the hourly project and per-review budgets."""
        if type(estimated_tokens) is not int or not 1 <= estimated_tokens <= 1000000000:
            raise DocumentError(INVALID_REQUEST, "invalid reviewer token reservation")
        with self.documents._write() as db:
            found = self._run(db, project, session, review)
            self.owner(found, owner)
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
                    "review_calls(project,review_id,created,estimated_tokens) "
                    "VALUES(?,?,?,?)"
                ),
                (project, review, self.clock(), estimated_tokens),
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
    ) -> None:
        if any(
            value is not None and (type(value) is not int or not 0 <= value <= 1000000000)
            for value in (input_tokens, output_tokens)
        ):
            raise DocumentError(INVALID_REQUEST, "invalid provider usage")
        with self.documents._write() as db:
            self.owner(self._run(db, project, session, review), owner)
            updated = db.execute(
                (
                    "UPDATE review_calls SET "
                    "input_tokens=?,output_tokens=?,resolved_model=?,answered=1 WHERE seq=? AND "
                    "project=? AND review_id=? AND answered=0"
                ),
                (
                    input_tokens,
                    output_tokens,
                    clipped(model, 256) if model else None,
                    call,
                    project,
                    review,
                ),
            )
            if updated.rowcount:
                db.execute(
                    (
                        "UPDATE review_runs SET input_tokens=input_tokens+?,"
                        "output_tokens=output_tokens+?,resolved_model=coalesce(?,resolved_model)"
                        " WHERE review_id=?"
                    ),
                    (
                        input_tokens or 0,
                        output_tokens or 0,
                        clipped(model, 256) if model else None,
                        review,
                    ),
                )
                config = ReviewConfig.model_validate_json(
                    self._run(db, project, session, review)["config"]
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
        if type(limit) is not int or not 1 <= limit <= 20:
            raise DocumentError(INVALID_REQUEST, "manifest limit is 1 to 20")
        found = self.run(project, session, review)
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
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
                    "locator": json.loads(r[3]),
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
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
            self._run(db, project, session, review)
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
        if digest(row[2]) != row[3]:
            raise DocumentError(CONFLICT, "pinned evidence failed its digest check")
        return {
            "evidence": evidence,
            "kind": row[0],
            "locator": json.loads(row[1]),
            "payload": json.loads(row[2]),
            "sha256": row[3],
            "complete": bool(row[4]),
        }

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
        result["locator"]["returned_characters"] = len(result["text"])
        result["locator"]["total_characters"] = len(source)
        if owner is None:
            return result
        with self.documents._write() as db:
            found = self._run(db, project, session, review)
            self.owner(found, owner)
            if not result["text"]:
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
        with self.documents._read() as db:
            assert db is not None  # noqa: S101
            self._run(db, project, session, review)
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
        with self.documents._read() as db:
            if not self.available(db) or db is None:
                return {"reviews": [], "next_after": None, "unreviewed": []}
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
                json.loads(r[0])
                for r in db.execute(
                    (
                        "SELECT payload FROM review_events WHERE project=? AND session=? "
                        "AND kind='review.unreviewed' ORDER BY seq DESC LIMIT 50"
                    ),
                    (project, session),
                )
            ]
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
        }
