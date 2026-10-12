# SPDX-License-Identifier: MIT
"""Exact finding-explanation source adapter for the shared mechanical worker."""

# Source adapters intentionally share the bounded lease/transaction machinery.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.consistency import response_bodies as bodies

from .blocks import parse
from .engine import attach
from .errors import CONFLICT, INTERNAL, DocumentError
from .identity import Mapped
from .mathjobs import LEASE

if TYPE_CHECKING:
    from .mathjobs import MathJobs

Json = dict[str, Any]


def metadata(db: sqlite3.Connection, check: str) -> Json | None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='math_response_jobs'").fetchone() is None:
        return None
    row = db.execute(
        "SELECT substr(CAST(r.source AS BLOB),1,129),substr(CAST(s.finding_id AS BLOB),1,129),"
        "s.decision_revision,substr(CAST(s.project AS BLOB),1,129),"
        "substr(CAST(s.session AS BLOB),1,129),j.revision,j.document IS NULL,b.identity_floor "
        "FROM math_response_jobs r JOIN math_jobs j USING(check_id) JOIN response_math_sources s "
        "ON s.source=r.source JOIN review_response_bodies b ON b.source=s.source "
        "AND b.revision=j.revision AND b.finding_id=s.finding_id "
        "AND b.decision_revision=s.decision_revision WHERE r.check_id=?",
        (check,),
    ).fetchone()
    if row is None:
        return None
    if (
        any(not isinstance(row[i], bytes) or not 1 <= len(row[i]) <= 128 for i in (0, 1, 3, 4))
        or any(type(row[i]) is not int or not 1 <= row[i] <= 999999999 for i in (2, 5, 7))
        or row[6] != 1
        or row[7] > row[5]
    ):
        raise DocumentError(INTERNAL, "retained response math identity exceeds its bounds")
    try:
        source, finding, project, session = (row[i].decode() for i in (0, 1, 3, 4))
    except UnicodeDecodeError:
        raise DocumentError(INTERNAL, "invalid response math identity") from None
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
            "SELECT substr(CAST(s.source AS BLOB),1,129),substr(CAST(s.finding_id AS BLOB),1,129),"
            "s.decision_revision,CASE WHEN s.completed<s.target THEN s.completed+1 ELSE "
            "s.target END "
            "FROM response_math_sources s JOIN review_findings f ON f.finding_id=s.finding_id "
            "WHERE s.project=? AND s.target>0 "
            "AND (s.completed<s.target OR s.retry_requested=1) AND "
            "(s.blocked IS NULL OR s.blocked NOT LIKE 'Derived body identity%') AND NOT EXISTS("
            "SELECT 1 FROM math_jobs j JOIN math_response_jobs r USING(check_id) WHERE "
            "r.source=s.source AND j.state='running') ORDER BY s.rowid LIMIT 1",
            (project,),
        ).fetchone()
        if row is None:
            return None
        if any(not isinstance(v, bytes) or not 1 <= len(v) <= 128 for v in row[:2]) or any(
            type(v) is not int or not 1 <= v <= 999999999 for v in row[2:]
        ):
            raise DocumentError(INTERNAL, "retained response math cursor exceeds its bounds")
        source, finding, decision, revision = row[0].decode(), row[1].decode(), row[2], row[3]
        parent = bodies.original(db, finding, decision, project=project)
        retained = db.execute(
            "SELECT substr(CAST(session AS BLOB),1,129) FROM response_math_sources WHERE source=?",
            (source,),
        ).fetchone()
        if retained != (parent["session"].encode(),):
            raise DocumentError(INTERNAL, "response queue does not bind its canonical session")
        saved = bodies.read(db, finding, decision, revision, project=project, full=False)
        if source != saved["source"]:
            raise DocumentError(INTERNAL, "response queue does not bind its original field")
        if db.execute("SELECT count(*) FROM math_jobs").fetchone()[0] >= self.jobs.limits.jobs:
            db.execute(
                "UPDATE response_math_sources SET blocked='math job history quota reached' "
                "WHERE source=?",
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
        db.execute("INSERT INTO math_response_jobs VALUES(?,?)", (check, source))
        db.execute(
            "UPDATE response_math_sources SET retry_requested=0,blocked=NULL WHERE source=?",
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
                "UPDATE response_math_sources SET completed=max(completed,?) WHERE source=?",
                (revision, source),
            )
        return check

    def fence(self, db: sqlite3.Connection, check: str, owner: str, meta: Json) -> tuple[Any, ...]:
        row = db.execute(
            "SELECT j.revision,j.cursor FROM math_jobs j JOIN math_response_jobs r "
            "USING(check_id) JOIN review_response_bodies b ON b.source=r.source AND "
            "b.revision=j.revision "
            "WHERE j.check_id=? AND j.state='running' AND j.owner=? AND j.lease_until>? "
            "AND j.document IS NULL AND j.source_sha256=b.sha256",
            (check, owner, self.store.clock()),
        ).fetchone()
        if row is None:
            raise DocumentError(CONFLICT, "response math ownership or source pin changed")
        if (
            any(type(v) is not int for v in row)
            or not 1 <= row[0] <= 999999999
            or not 0 <= row[1] <= 20000
            or metadata(db, check) != meta
        ):
            raise DocumentError(INTERNAL, "retained response math fence exceeds its bounds")
        return None, row[0], row[1], None

    def load(self, job: Json) -> Mapped:
        meta = job["response"]
        with self.documents._read() as db:
            assert db is not None  # noqa: S101 - writer initialized
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
        return attach(parse(saved["body"], documents=False), saved["block_map"])

    def attribution(self, check: str, owner: str, blocks: set[str]) -> dict[str, Json]:
        with self.documents._read() as db:
            assert db is not None  # noqa: S101 - writer initialized
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
            "UPDATE response_math_sources SET completed=max(completed,?),blocked=NULL WHERE "
            "source=?",
            (job["revision"], job["response"]["source"]),
        )

    def retry(self, db: sqlite3.Connection, project: str, configuration: str | None) -> None:
        db.execute(
            "UPDATE response_math_sources SET retry_requested=1 WHERE project=? AND target>0 "
            "AND completed=target AND EXISTS(SELECT 1 FROM math_jobs j JOIN math_response_jobs r "
            "USING(check_id) WHERE r.source=response_math_sources.source AND j.revision=target "
            "AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_response_jobs x "
            "USING(check_id) "
            "WHERE x.source=r.source AND k.revision=j.revision) AND j.state='finished' AND "
            "(j.unavailable>0 OR (? IS NOT NULL AND j.configuration!=?)))",
            (project, configuration, configuration),
        )


def remaining(db: sqlite3.Connection, project: str) -> bool:
    if db.execute(bodies.legacy_query(), (project,)).fetchone() is not None:
        return True
    return (
        db.execute(
            "SELECT 1 FROM response_math_sources s WHERE s.project=? AND "
            "(s.completed<s.target OR s.retry_requested=1 OR EXISTS(SELECT 1 FROM math_jobs j "
            "JOIN math_response_jobs r USING(check_id) WHERE r.source=s.source AND "
            "j.state='running')) LIMIT 1",
            (project,),
        ).fetchone()
        is not None
    )


def queue(db: sqlite3.Connection, project: str, *, session: str | None = None) -> list[Json]:
    if (
        db.execute("SELECT 1 FROM sqlite_master WHERE name='response_math_sources'").fetchone()
        is None
    ):
        return []
    rows = db.execute(
        "SELECT substr(CAST(source AS BLOB),1,129),substr(CAST(finding_id AS BLOB),1,129),"
        "decision_revision,target,completed,substr(CAST(blocked AS BLOB),1,1001),EXISTS(SELECT 1 "
        "FROM math_jobs j JOIN math_response_jobs r USING(check_id) WHERE r.source=s.source "
        "AND j.state='running') FROM response_math_sources s WHERE project=? AND (? IS "
        "NULL OR session=?) AND (blocked IS "
        "NOT NULL "
        "OR completed<target OR retry_requested=1 OR EXISTS(SELECT 1 FROM math_jobs j JOIN "
        "math_response_jobs r USING(check_id) WHERE r.source=s.source AND j.state='running')) "
        "ORDER BY s.rowid LIMIT 21",
        (project, session, session),
    )
    values: list[Json] = []
    for source, finding, decision, target, completed, blocked, running in rows:
        if (
            len(source) > 128
            or re.fullmatch(b"finding-[0-9a-f]{24}", finding) is None
            or any(
                type(v) is not int or not 0 <= v <= 999999999 for v in (decision, target, completed)
            )
            or (blocked is not None and len(blocked) > 1000)
        ):
            raise DocumentError(INTERNAL, "response math queue exceeds its bounds")
        bodies.original(db, finding.decode(), decision, project=project, session=session)
        if source.decode() != bodies.source_key(finding.decode(), decision):
            raise DocumentError(INTERNAL, "response math queue field identity changed")
        values.append(
            {
                "kind": "response",
                "source": source.decode(),
                "finding": finding.decode(),
                "response_revision": decision,
                "target": target,
                "completed": completed,
                "blocked": blocked.decode() if blocked else None,
                "running": running,
            }
        )
    held = db.execute(
        "SELECT d.transition_seq,substr(CAST(d.finding_id AS BLOB),1,129),d.decision_revision "
        "FROM review_response_discovery d JOIN review_findings f USING(finding_id) "
        "WHERE f.project=? AND (? IS NULL OR f.session=?) ORDER BY d.rowid LIMIT 2",
        (project, session, session),
    )
    for seq, finding, decision in held:
        known = re.fullmatch(b"finding-[0-9a-f]{24}", finding) is not None
        values.append(
            {
                "kind": "response",
                "transition_seq": seq,
                "finding": finding.decode() if known else None,
                "response_revision": decision,
                "target": 0,
                "completed": 0,
                "blocked": "Legacy response author/identity is unknown; math checking held",
            }
        )
    return values
