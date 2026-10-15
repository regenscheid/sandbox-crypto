# SPDX-License-Identifier: MIT
"""Ledger statement adapter for the shared mechanical math worker (LG-1, LD-53).

A ledger entry's statement is a text whose revisions are ``text_revisions`` of kind
``statement`` under the entry's ID. The ledger queues each new one in
``text_math_sources`` when it saves it; this adapter claims, loads and attributes it as
the comment and explanation adapters do theirs. A statement belongs to no document, so
its macros are the project's setting alone (LD-28).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any, Final, cast

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.control.texts import TextRevisions
from sandbox_science.orchestrator.documents.engine import attach
from sandbox_science.orchestrator.documents.errors import CONFLICT, INTERNAL, DocumentError
from sandbox_science.orchestrator.documents.identity import Mapped
from sandbox_science.orchestrator.documents.project_macros import pin
from sandbox_science.orchestrator.ledger.models import PREFIXES

from .schedule import LEASE

if TYPE_CHECKING:
    from .jobs import MathJobs

Json = dict[str, Any]

TEXTS: Final = TextRevisions("statement")

_RUNNING: Final = (
    "EXISTS(SELECT 1 FROM math_jobs j JOIN math_text_jobs r USING(check_id) "
    "WHERE r.subject_type='statement' AND r.subject_id=s.subject_id AND j.state='running')"
)
"""Whether a check of the statement ``s`` is running."""


def source_key(entry: str) -> str:
    """The key a statement's math findings and current checks go by."""
    return f"statement:{entry}"


def metadata(db: sqlite3.Connection, check: str) -> Json | None:
    """The statement a check is of, or None for a check of another kind of text."""
    row = db.execute(
        "SELECT r.subject_id,e.project,e.type,e.number,b.identity_floor,j.revision,"
        "j.document IS NULL AND s.project=e.project "
        "FROM math_text_jobs r JOIN math_jobs j USING(check_id) "
        "JOIN text_math_sources s ON s.subject_type='statement' AND s.subject_id=r.subject_id "
        "LEFT JOIN ledger_entries e ON e.entry_id=r.subject_id "
        "LEFT JOIN text_revisions b ON b.subject_type='statement' "
        "AND b.subject_id=r.subject_id AND b.revision=j.revision "
        "WHERE r.check_id=? AND r.subject_type='statement'",
        (check,),
    ).fetchone()
    if row is None:
        return None
    entry, project, kind, number, floor, revision, bound = row
    if project is None or floor is None or bound != 1 or floor > revision:
        raise DocumentError(INTERNAL, "statement math check does not bind its statement")
    return {
        "entry": entry,
        "handle": f"{PREFIXES[kind]}-{number}",
        "project": project,
        "identity_floor": floor,
    }


class StatementMath:
    def __init__(self, jobs: MathJobs) -> None:
        self.jobs, self.store, self.documents = jobs, jobs.store, jobs.documents

    def claim(self, db: sqlite3.Connection, project: str, owner: str, now: float) -> str | None:
        row = db.execute(
            "SELECT s.subject_id,s.target FROM text_math_sources s JOIN ledger_entries e "  # noqa: S608 - fixed SQL
            "ON e.entry_id=s.subject_id AND e.project=s.project "
            "WHERE s.subject_type='statement' AND s.project=? AND s.target>0 "
            "AND (s.completed<s.target OR s.retry_requested=1) AND "
            "(s.blocked IS NULL OR s.blocked NOT LIKE 'Derived body identity%') "
            f"AND NOT {_RUNNING} ORDER BY s.rowid LIMIT 1",
            (project,),
        ).fetchone()
        if row is None:
            return None
        entry, revision = row
        saved = TEXTS.read(db, entry, revision, full=False)
        if saved is None:
            raise DocumentError(INTERNAL, "a queued statement revision is missing")
        if db.execute("SELECT count(*) FROM math_jobs").fetchone()[0] >= self.jobs.limits.jobs:
            db.execute(
                "UPDATE text_math_sources SET blocked='math job history quota reached' "
                "WHERE subject_type='statement' AND subject_id=?",
                (entry,),
            )
            return None
        check = new_id("math")
        db.execute(
            "INSERT INTO "
            "math_jobs(check_id,document,revision,source_sha256,state,owner,lease_until,created) "
            "VALUES(?,NULL,?,?,'running',?,?,?)",
            (check, revision, saved["sha256"], owner, now + LEASE, now),
        )
        db.execute("INSERT INTO math_text_jobs VALUES(?,'statement',?,NULL,NULL)", (check, entry))
        pin(db, check, project)
        db.execute(
            "UPDATE text_math_sources SET retry_requested=0,blocked=NULL "
            "WHERE subject_type='statement' AND subject_id=?",
            (entry,),
        )
        if saved["held"]:
            # A statement whose parse is held is not checked; the attempt says why.
            db.execute(
                "UPDATE math_jobs SET "
                "state='finished',unavailable=1,reason=?,ended=?,owner=NULL,lease_until=NULL "
                "WHERE check_id=?",
                (saved["held"], now, check),
            )
            db.execute(
                "UPDATE text_math_sources SET completed=max(completed,?) "
                "WHERE subject_type='statement' AND subject_id=?",
                (revision, entry),
            )
        return check

    def fence(self, db: sqlite3.Connection, check: str, owner: str, meta: Json) -> tuple[Any, ...]:
        row = db.execute(
            "SELECT j.revision,j.cursor FROM math_jobs j JOIN math_text_jobs r "
            "ON r.check_id=j.check_id AND r.subject_type='statement' "
            "JOIN text_revisions b ON b.subject_type='statement' "
            "AND b.subject_id=r.subject_id AND b.revision=j.revision "
            "WHERE j.check_id=? AND j.state='running' AND j.owner=? AND j.lease_until>? "
            "AND j.document IS NULL AND j.source_sha256=b.sha256",
            (check, owner, self.store.clock()),
        ).fetchone()
        if row is None:
            raise DocumentError(CONFLICT, "statement math ownership or source pin changed")
        if metadata(db, check) != meta:
            raise DocumentError(INTERNAL, "statement math check no longer binds its statement")
        return None, row[0], row[1], None

    def load(self, job: Json) -> Mapped:
        meta = job["statement"]
        if meta["project"] != job["project"]:
            raise DocumentError(INTERNAL, "statement math check is outside its project")
        with self.documents.control.read() as tx:
            saved = TEXTS.read(tx.sql, meta["entry"], job["revision"])
        if saved is None or saved["sha256"] != job["source_sha256"] or saved["held"]:
            raise DocumentError(INTERNAL, "statement math digest does not bind its statement")
        return attach(saved["body"], saved["block_map"], documents=False)

    def attribution(self, check: str, owner: str, blocks: set[str]) -> dict[str, Json]:
        """Each block's last change, as the statement's text revision proves it."""
        with self.documents.control.read() as tx:
            db = tx.sql
            meta = metadata(db, check)
            assert meta is not None  # noqa: S101 - dispatcher selected statement adapter
            _, revision, _, _ = self.fence(db, check, owner, meta)
            saved = TEXTS.read(db, meta["entry"], revision)
        assert saved is not None  # noqa: S101 - fenced on its text revision
        proofs: dict[str, Json] = {}
        for block in blocks:
            value = saved["owners"].get(block)
            if not isinstance(value, dict) or set(cast(Json, value)) != {"revision", "author"}:
                raise DocumentError(INTERNAL, "statement block ownership proof is missing")
            value = cast(Json, value)
            if value["revision"] is not None and (
                type(value["revision"]) is not int or not 1 <= value["revision"] <= revision
            ):
                raise DocumentError(INTERNAL, "statement owner revision is invalid")
            proofs[block] = {"revision": value["revision"], "author": value["author"]}
        return proofs

    def finish(self, db: sqlite3.Connection, job: Json) -> None:
        db.execute(
            "UPDATE text_math_sources SET completed=max(completed,?),blocked=NULL "
            "WHERE subject_type='statement' AND subject_id=?",
            (job["revision"], job["statement"]["entry"]),
        )

    def retry(self, db: sqlite3.Connection, project: str, configuration: str | None) -> None:
        """Check a statement's latest revision again when its latest check found the
        renderer unavailable, or ran with another renderer configuration."""
        db.execute(
            "UPDATE text_math_sources AS s SET retry_requested=1 WHERE "
            "s.subject_type='statement' AND s.project=? AND s.target>0 "
            "AND s.completed=s.target AND EXISTS(SELECT 1 FROM math_jobs j "
            "JOIN math_text_jobs r USING(check_id) WHERE r.subject_type='statement' "
            "AND r.subject_id=s.subject_id AND j.revision=s.target "
            "AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_text_jobs x "
            "USING(check_id) WHERE x.subject_type='statement' AND x.subject_id=r.subject_id "
            "AND k.revision=j.revision) AND j.state='finished' AND "
            "(j.unavailable>0 OR (? IS NOT NULL AND j.configuration!=?)))",
            (project, configuration, configuration),
        )


def remaining(db: sqlite3.Connection, project: str) -> bool:
    """Whether a statement of the project waits for a check or has one running."""
    return (
        db.execute(
            "SELECT 1 FROM text_math_sources s WHERE s.subject_type='statement' "  # noqa: S608 - fixed SQL
            "AND s.project=? AND s.target>0 AND (s.completed<s.target "
            f"OR s.retry_requested=1 OR {_RUNNING}) LIMIT 1",
            (project,),
        ).fetchone()
        is not None
    )


def queue(db: sqlite3.Connection, project: str) -> list[Json]:
    """The project's statements waiting for, running or held from their checks."""
    rows = db.execute(
        "SELECT s.subject_id,e.type,e.number,s.target,s.completed,s.blocked,"  # noqa: S608 - fixed SQL
        f"{_RUNNING} FROM text_math_sources s JOIN ledger_entries e "
        "ON e.entry_id=s.subject_id WHERE s.subject_type='statement' AND s.project=? "
        "AND (s.blocked IS NOT NULL OR s.completed<s.target OR s.retry_requested=1 "
        f"OR {_RUNNING}) ORDER BY s.rowid LIMIT 21",
        (project,),
    )
    return [
        {
            "kind": "statement",
            "entry": entry,
            "handle": f"{PREFIXES[kind]}-{number}",
            "target": target,
            "completed": completed,
            "blocked": blocked or None,
            "running": running,
        }
        for entry, kind, number, target, completed, blocked, running in rows
    ]
