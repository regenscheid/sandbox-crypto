# SPDX-License-Identifier: MIT
"""Bounded operator status reads for work trees and original unresolved identities."""

from __future__ import annotations

import hashlib

from pydantic import Field

from sandbox_science.contracts import Contract
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import NOT_FOUND
from sandbox_science.orchestrator.documents import DocumentStore

from .records import Work
from .reservations import ModelReservation, reservation


class ResumeStatus(Contract):
    request: str
    root: str
    status: str
    reason: str | None
    created: float


class StatusPage(Contract):
    root: str | None
    unfinished: int
    active: bool
    works: tuple[Work, ...] = Field(max_length=100)
    resumes: tuple[ResumeStatus, ...] = Field(max_length=100)
    next_work: str | None
    next_resume: str | None


class ModelPending(Contract):
    run: str
    identity: str
    kind: str
    alias: str
    created: float
    reservation: ModelReservation | None = None


class OperationPending(Contract):
    seq: int
    run: str
    identity: str


class PendingPage(Contract):
    models: tuple[ModelPending, ...] = Field(max_length=100)
    operations: tuple[OperationPending, ...] = Field(max_length=100)
    saved_kernel: bool
    kernel_identity: str | None = None
    kernel_observation: str | None = None
    next_models: tuple[str, str] | None
    next_operation: int | None


class RecoveryStatus:
    def __init__(self, control: ControlStore | DocumentStore) -> None:
        self.control = control.control if isinstance(control, DocumentStore) else control

    def page(
        self,
        project: str,
        session: str,
        *,
        after_work: str = "",
        after_resume: str = "",
        limit: int = 100,
    ) -> StatusPage:
        if not 1 <= limit <= 100:
            raise ValueError("page limit is 1..100")
        with self.control.read() as tx:
            current = tx.sql.execute(
                "SELECT s.root,coalesce(max(w.state='running'),0) FROM research_sessions s "
                "JOIN research_work w ON w.project=s.project AND w.session=s.session "
                "AND w.root=s.root WHERE s.project=? AND s.session=? GROUP BY s.root",
                (project, session),
            ).fetchone()
            pending = tx.sql.execute(
                "SELECT root FROM research_resumes WHERE project=? AND session=? "
                "AND status IN ('queued','running') LIMIT 1",
                (project, session),
            ).fetchone()
            candidate = tx.sql.execute(
                "SELECT work FROM research_work WHERE project=? AND session=? "
                "AND parent IS NULL AND state IN ('paused','blocked','waiting') "
                "ORDER BY work DESC LIMIT 1",
                (project, session),
            ).fetchone()
            selected = pending or candidate or current
            root = selected[0] if selected else None
            unfinished = tx.sql.execute(
                "SELECT count(*) FROM research_work WHERE project=? AND session=? AND root=? "
                "AND state NOT IN ('completed','stopped')",
                (project, session, root),
            ).fetchone()[0]
            rows = tx.sql.execute(
                "SELECT w.project,w.session,w.work,w.root,w.parent,w.current,w.role,w.kernel,"
                "w.state,w.reason,s.epoch FROM research_work w JOIN research_sessions s "
                "USING(project,session) WHERE w.project=? AND w.session=? AND w.work>? "
                "AND w.root=? "
                "ORDER BY w.work LIMIT ?",
                (project, session, after_work, root, limit),
            ).fetchall()
            fields = tuple(Work.model_fields)
            works = tuple(Work.model_validate(dict(zip(fields, row, strict=True))) for row in rows)
            rows = tx.sql.execute(
                "SELECT request,root,status,reason,created FROM research_resumes "
                "WHERE project=? AND session=? AND request>? ORDER BY request LIMIT ?",
                (project, session, after_resume, limit),
            ).fetchall()
            resumes = tuple(
                ResumeStatus.model_validate(dict(zip(ResumeStatus.model_fields, row, strict=True)))
                for row in rows
            )
        return StatusPage(
            root=root,
            unfinished=int(unfinished),
            active=bool((current and current[1]) or pending),
            works=works,
            resumes=resumes,
            next_work=works[-1].work if len(works) == limit else None,
            next_resume=resumes[-1].request if len(resumes) == limit else None,
        )

    def pending(
        self,
        project: str,
        session: str,
        work: str,
        *,
        model_after: tuple[str, str] = ("", ""),
        operation_after: int = 0,
        limit: int = 100,
    ) -> PendingPage:
        if not 1 <= limit <= 100 or operation_after < 0:
            raise ValueError("invalid pending page cursor/limit")
        with self.control.read() as tx:
            if not tx.sql.execute(
                "SELECT 1 FROM research_work WHERE project=? AND session=? AND work=?",
                (project, session, work),
            ).fetchone():
                raise ControlError(NOT_FOUND, "no research work in this scope")
            rows = tx.sql.execute(
                "SELECT m.run,m.identity,m.kind,m.alias,m.created FROM research_model_calls m "
                "JOIN research_attempts a USING(project,session,run) "
                "WHERE m.project=? AND m.session=? AND a.work=? AND m.usage IS NULL "
                "AND (m.run,m.identity)>(?,?) ORDER BY m.run,m.identity LIMIT ?",
                (project, session, work, *model_after, limit),
            ).fetchall()
            models = tuple(
                ModelPending(
                    run=row[0],
                    identity=row[1],
                    kind=row[2],
                    alias=row[3],
                    created=row[4],
                    reservation=reservation(tx.sql, project, session, work, row[0], row[1]),
                )
                for row in rows
            )
            rows = tx.sql.execute(
                "SELECT i.seq,i.run,i.operation_id FROM tool_intents i "
                "JOIN research_attempts a USING(project,session,run) "
                "WHERE i.project=? AND i.session=? AND a.work=? AND i.seq>? AND NOT EXISTS "
                "(SELECT 1 FROM archive_tool_records r WHERE r.project=i.project "
                "AND r.session=i.session AND r.run=i.run AND r.operation_id=i.operation_id) "
                "ORDER BY i.seq LIMIT ?",
                (project, session, work, operation_after, limit),
            ).fetchall()
            operations = tuple(
                OperationPending.model_validate(
                    dict(zip(OperationPending.model_fields, row, strict=True))
                )
                for row in rows
            )
            kernel = tx.sql.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
                "AND kind='kernels' AND identity='latest'",
                (project, session, work),
            ).fetchone()
        return PendingPage(
            models=models,
            operations=operations,
            saved_kernel=kernel is not None,
            kernel_identity=hashlib.sha256(kernel[0].encode()).hexdigest() if kernel else None,
            kernel_observation=kernel[0] if kernel else None,
            next_models=(models[-1].run, models[-1].identity) if len(models) == limit else None,
            next_operation=operations[-1].seq if len(operations) == limit else None,
        )
