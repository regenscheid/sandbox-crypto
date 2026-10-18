# SPDX-License-Identifier: MIT
"""Immutable continuation identities and bounded dependency admission."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime

from pydantic import AwareDatetime

from sandbox_science.contracts import Contract, new_id
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT
from sandbox_science.orchestrator.control.guard import guarded
from sandbox_science.orchestrator.projects import RunName, run_name

from .records import Work, WorkRecords
from .token_usage import spent_tokens as spent_tokens


class Plan(Contract):
    work: RunName
    run: RunName
    predecessor: RunName
    started: AwareDatetime


class Plans:
    def __init__(self, control: ControlStore, project: str, session: str, request: str) -> None:
        self.control, self.project, self.session, self.request = control, project, session, request

    @contextmanager
    def authority(self, epoch: int, lease: str) -> Generator[None, None, None]:
        """Fence delayed recovery writes inside their actual control transaction."""

        def check(db: sqlite3.Connection) -> None:
            if (
                db.execute(
                    "SELECT 1 FROM research_resumes r "
                    "JOIN research_sessions s USING(project,session) "
                    "JOIN run_leases l ON l.project_id=r.project AND l.session_id=r.session "
                    "WHERE r.project=? AND r.session=? AND r.request=? AND r.status='running' "
                    "AND r.root=s.root AND s.epoch=? AND l.run=?",
                    (self.project, self.session, self.request, epoch, lease),
                ).fetchone()
                is None
            ):
                raise ControlError(CONFLICT, "Resume lost its session ownership")

        with guarded(check):
            yield

    def create(
        self, work: Work, *, run: str | None = None, started: datetime | None = None
    ) -> Plan:
        started = started or datetime.now(UTC)
        plan = Plan(
            work=work.work,
            run=run or run_name(started, new_id("run")),
            predecessor=work.current,
            started=started,
        )
        with self.control.write() as tx:
            tx.sql.execute(
                "INSERT OR IGNORE INTO research_continuations VALUES (?,?,?,?,?,?,?)",
                (
                    self.project,
                    self.session,
                    self.request,
                    plan.work,
                    plan.run,
                    plan.predecessor,
                    plan.started.isoformat(),
                ),
            )
        return self.get(work.work)

    def get(self, work: str) -> Plan:
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT work,run,predecessor,started FROM research_continuations "
                "WHERE project=? AND session=? AND request=? AND work=?",
                (self.project, self.session, self.request, work),
            ).fetchone()
        if row is None:
            raise ControlError(CONFLICT, "work has no continuation in this Resume")
        return Plan(work=row[0], run=row[1], predecessor=row[2], started=row[3])

    def prepare(self, root: str, *, run: str, started: datetime) -> None:
        works = WorkRecords(self.control)
        after = ""
        while page := works.page(self.project, self.session, root, after=after):
            for work in page:
                if work.state not in ("completed", "stopped"):
                    self.create(
                        work,
                        run=run if work.work == root else None,
                        started=started if work.work == root else None,
                    )
            after = page[-1].work

    def ready(self) -> tuple[str, ...]:
        """Only leaves whose children have settled this Resume; never resurrect a stop."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT p.work FROM research_continuations p JOIN research_work w "
                "ON w.project=p.project AND w.session=p.session AND w.work=p.work "
                "WHERE p.project=? AND p.session=? AND p.request=? AND w.state='waiting' "
                "AND w.current=p.predecessor AND NOT EXISTS (SELECT 1 FROM research_work c "
                "JOIN research_continuations cp ON cp.project=c.project AND cp.session=c.session "
                "AND cp.work=c.work AND cp.request=p.request WHERE c.project=w.project "
                "AND c.session=w.session AND c.parent=w.work AND c.state IN ('waiting','running') "
                "AND (c.current=cp.predecessor OR c.state='running')) "
                "AND NOT EXISTS (SELECT 1 FROM research_work c JOIN delegations d "
                "ON d.project_id=c.project AND d.session_id=c.session AND d.run=c.current "
                "WHERE c.project=w.project AND c.session=w.session AND c.parent=w.work "
                "AND d.ended IS NULL) AND NOT EXISTS (SELECT 1 FROM research_child_settlements cs "
                "JOIN research_attempts ca ON ca.project=cs.project AND ca.session=cs.session "
                "AND ca.run=cs.run JOIN research_work cw ON cw.project=ca.project "
                "AND cw.session=ca.session AND cw.work=ca.work WHERE cw.project=w.project "
                "AND cw.session=w.session AND cw.parent=w.work AND cs.settled=0 "
                "AND EXISTS (SELECT 1 FROM runs rr WHERE rr.project_id=cs.project "
                "AND rr.session_id=cs.session AND rr.name=cs.run)) "
                "ORDER BY p.work LIMIT 4",
                (self.project, self.session, self.request),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def spent_tokens(self, work: str) -> int:
        with self.control.read() as tx:
            return spent_tokens(tx.sql, self.project, self.session, work)

    def unfinished(self) -> str | None:
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT count(*),min(w.reason) FROM research_continuations p "
                "JOIN research_work w ON w.project=p.project "
                "AND w.session=p.session AND w.work=p.work "
                "WHERE p.project=? AND p.session=? AND p.request=? "
                "AND w.state NOT IN ('completed','stopped')",
                (self.project, self.session, self.request),
            ).fetchone()
        return f"{row[0]} unfinished branches: {row[1] or 'waiting'}" if row[0] else None
