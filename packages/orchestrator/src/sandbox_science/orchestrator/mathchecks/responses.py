# SPDX-License-Identifier: MIT
"""Exact finding-explanation source adapter for the shared mechanical worker."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.consistency import response_bodies as bodies
from sandbox_science.orchestrator.documents.engine import attach
from sandbox_science.orchestrator.documents.errors import CONFLICT, INTERNAL, DocumentError
from sandbox_science.orchestrator.documents.identity import Mapped
from sandbox_science.orchestrator.documents.project_macros import pin

from .schedule import LEASE

if TYPE_CHECKING:
    from .jobs import MathJobs

Json = dict[str, Any]


def metadata(db: sqlite3.Connection, check: str) -> Json | None:
    row = db.execute(
        "SELECT r.subject_id,s.parent,s.parent_revision,s.project,s.session,j.revision,"
        "j.document IS NULL,b.identity_floor "
        "FROM math_text_jobs r JOIN math_jobs j USING(check_id) JOIN text_math_sources s "
        "ON s.subject_type='response' AND s.subject_id=r.subject_id "
        "JOIN text_revisions b ON b.subject_type='response' "
        "AND b.subject_id=s.subject_id AND b.revision=j.revision AND b.parent=s.parent "
        "AND b.parent_revision=s.parent_revision "
        "WHERE r.check_id=? AND r.subject_type='response'",
        (check,),
    ).fetchone()
    if row is None:
        return None
    if row[6] != 1 or row[7] > row[5]:
        raise DocumentError(INTERNAL, "response math check does not bind its response body")
    source, finding, project, session = (row[i] for i in (0, 1, 3, 4))
    parent = bodies.original(db, finding, row[2], project=project, session=session)
    if source != parent["source"]:
        raise DocumentError(INTERNAL, "response queue does not bind its canonical parent")
    return {
        "source": source,
        "finding": finding,
        "response_revision": row[2],
        "project": project,
        "session": session,
        "identity_floor": row[7],
    }


class ResponseMath:
    def __init__(self, jobs: MathJobs) -> None:
        self.jobs, self.store, self.documents = jobs, jobs.store, jobs.documents

    def claim(self, db: sqlite3.Connection, project: str, owner: str, now: float) -> str | None:
        row = db.execute(
            "SELECT s.subject_id,s.parent,s.parent_revision,s.target "
            "FROM text_math_sources s JOIN review_findings f ON f.finding_id=s.parent "
            "WHERE s.subject_type='response' AND s.project=? AND s.target>0 "
            "AND (s.completed<s.target OR s.retry_requested=1) AND "
            "(s.blocked IS NULL OR s.blocked NOT LIKE 'Derived body identity%') AND NOT EXISTS("
            "SELECT 1 FROM math_jobs j JOIN math_text_jobs r USING(check_id) WHERE "
            "r.subject_type='response' AND r.subject_id=s.subject_id AND j.state='running') "
            "ORDER BY s.rowid LIMIT 1",
            (project,),
        ).fetchone()
        if row is None:
            return None
        source, finding, decision, revision = row
        parent = bodies.original(db, finding, decision, project=project)
        retained = db.execute(
            "SELECT session FROM text_math_sources WHERE subject_type='response' AND subject_id=?",
            (source,),
        ).fetchone()
        if retained != (parent["session"],):
            raise DocumentError(INTERNAL, "response queue does not bind its canonical session")
        saved = bodies.read(db, finding, decision, revision, project=project, full=False)
        if source != saved["source"]:
            raise DocumentError(INTERNAL, "response queue does not bind its original field")
        if db.execute("SELECT count(*) FROM math_jobs").fetchone()[0] >= self.jobs.limits.jobs:
            db.execute(
                "UPDATE text_math_sources SET blocked='math job history quota reached' "
                "WHERE subject_type='response' AND subject_id=?",
                (source,),
            )
            return None
        check = new_id("math")
        db.execute(
            "INSERT INTO "
            "math_jobs(check_id,document,revision,source_sha256,state,owner,lease_until,created) "
            "VALUES(?,NULL,?,?,'running',?,?,?)",
            (check, revision, saved["sha256"], owner, now + LEASE, now),
        )
        db.execute("INSERT INTO math_text_jobs VALUES(?,'response',?,NULL,NULL)", (check, source))

        pin(db, check, project)
        db.execute(
            "UPDATE text_math_sources SET retry_requested=0,blocked=NULL "
            "WHERE subject_type='response' AND subject_id=?",
            (source,),
        )
        if saved["held"]:
            db.execute(
                "UPDATE math_jobs SET "
                "state='finished',unavailable=1,reason=?,ended=?,owner=NULL,lease_until=NULL "
                "WHERE check_id=?",
                (saved["held"], now, check),
            )
            db.execute(
                "UPDATE text_math_sources SET completed=max(completed,?) "
                "WHERE subject_type='response' AND subject_id=?",
                (revision, source),
            )
        return check

    def fence(self, db: sqlite3.Connection, check: str, owner: str, meta: Json) -> tuple[Any, ...]:
        row = db.execute(
            "SELECT j.revision,j.cursor FROM math_jobs j JOIN math_text_jobs r "
            "ON r.check_id=j.check_id AND r.subject_type='response' "
            "JOIN text_revisions b ON b.subject_type='response' "
            "AND b.subject_id=r.subject_id AND b.revision=j.revision "
            "WHERE j.check_id=? AND j.state='running' AND j.owner=? AND j.lease_until>? "
            "AND j.document IS NULL AND j.source_sha256=b.sha256",
            (check, owner, self.store.clock()),
        ).fetchone()
        if row is None:
            raise DocumentError(CONFLICT, "response math ownership or source pin changed")
        if metadata(db, check) != meta:
            raise DocumentError(INTERNAL, "response math check no longer binds its response body")
        return None, row[0], row[1], None

    def load(self, job: Json) -> Mapped:
        meta = job["response"]
        with self.documents.control.read() as tx:
            db = tx.sql
            saved = bodies.read(
                db,
                meta["finding"],
                meta["response_revision"],
                job["revision"],
                project=job["project"],
                session=meta["session"],
            )
        if (
            saved["sha256"] != job["source_sha256"]
            or saved["source"] != meta["source"]
            or saved["held"]
        ):
            raise DocumentError(INTERNAL, "response math digest does not bind its exact field")
        return attach(saved["body"], saved["block_map"], documents=False)

    def attribution(self, check: str, owner: str, blocks: set[str]) -> dict[str, Json]:
        with self.documents.control.read() as tx:
            db = tx.sql
            meta = metadata(db, check)
            assert meta is not None  # noqa: S101 - selected adapter
            _, revision, _, _ = self.fence(db, check, owner, meta)
            saved = bodies.read(
                db,
                meta["finding"],
                meta["response_revision"],
                revision,
                project=meta["project"],
                session=meta["session"],
            )
        if not blocks <= saved["owners"].keys():
            raise DocumentError(INTERNAL, "response block ownership proof is missing")
        return {b: saved["owners"][b] for b in blocks}

    def finish(self, db: sqlite3.Connection, job: Json) -> None:
        db.execute(
            "UPDATE text_math_sources SET completed=max(completed,?),blocked=NULL WHERE "
            "subject_type='response' AND subject_id=?",
            (job["revision"], job["response"]["source"]),
        )

    def retry(self, db: sqlite3.Connection, project: str, configuration: str | None) -> None:
        db.execute(
            "UPDATE text_math_sources AS s SET retry_requested=1 WHERE "
            "s.subject_type='response' AND s.project=? AND s.target>0 "
            "AND s.completed=s.target AND EXISTS(SELECT 1 FROM math_jobs j "
            "JOIN math_text_jobs r USING(check_id) WHERE r.subject_type='response' "
            "AND r.subject_id=s.subject_id AND j.revision=s.target "
            "AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_text_jobs x "
            "USING(check_id) WHERE x.subject_type='response' AND x.subject_id=r.subject_id "
            "AND k.revision=j.revision) AND j.state='finished' AND "
            "(j.unavailable>0 OR (? IS NOT NULL AND j.configuration!=?)))",
            (project, configuration, configuration),
        )


def remaining(db: sqlite3.Connection, project: str) -> bool:
    if db.execute(bodies.legacy_query(), (project,)).fetchone() is not None:
        return True
    return (
        db.execute(
            "SELECT 1 FROM text_math_sources s WHERE s.subject_type='response' "
            "AND s.project=? AND (s.completed<s.target OR s.retry_requested=1 "
            "OR EXISTS(SELECT 1 FROM math_jobs j JOIN math_text_jobs r USING(check_id) "
            "WHERE r.subject_type='response' AND r.subject_id=s.subject_id "
            "AND j.state='running')) LIMIT 1",
            (project,),
        ).fetchone()
        is not None
    )


def queue(db: sqlite3.Connection, project: str, *, session: str | None = None) -> list[Json]:
    rows = db.execute(
        "SELECT s.subject_id,s.parent,s.parent_revision,s.target,s.completed,s.blocked,"
        "EXISTS(SELECT 1 FROM math_jobs j JOIN math_text_jobs r USING(check_id) "
        "WHERE r.subject_type='response' AND r.subject_id=s.subject_id "
        "AND j.state='running') FROM text_math_sources s WHERE s.subject_type='response' "
        "AND s.project=? AND (? IS NULL OR s.session=?) AND (s.blocked IS NOT NULL "
        "OR s.completed<s.target OR s.retry_requested=1 OR EXISTS(SELECT 1 FROM math_jobs j "
        "JOIN math_text_jobs r USING(check_id) WHERE r.subject_type='response' "
        "AND r.subject_id=s.subject_id AND j.state='running')) "
        "ORDER BY s.rowid LIMIT 21",
        (project, session, session),
    )
    values: list[Json] = []
    for source, finding, decision, target, completed, blocked, running in rows:
        bodies.original(db, finding, decision, project=project, session=session)
        if source != bodies.source_key(finding, decision):
            raise DocumentError(INTERNAL, "response math queue field identity changed")
        values.append(
            {
                "kind": "response",
                "source": source,
                "finding": finding,
                "response_revision": decision,
                "target": target,
                "completed": completed,
                "blocked": blocked or None,
                "running": running,
            }
        )
    held = db.execute(
        "SELECT d.transition_seq,d.finding_id,d.decision_revision "
        "FROM review_response_discovery d JOIN review_findings f USING(finding_id) "
        "WHERE f.project=? AND (? IS NULL OR f.session=?) ORDER BY d.rowid LIMIT 2",
        (project, session, session),
    )
    for seq, finding, decision in held:
        values.append(
            {
                "kind": "response",
                "transition_seq": seq,
                "finding": finding,
                "response_revision": decision,
                "target": 0,
                "completed": 0,
                "blocked": "Legacy response author/identity is unknown; math checking held",
            }
        )
    return values
