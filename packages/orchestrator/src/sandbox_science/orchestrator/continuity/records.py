# SPDX-License-Identifier: MIT
"""Durable work identities and attempts; one fenced owner per research session."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Literal

from pydantic import Field

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, Contract, Identifier, new_id
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import BUSY, CONFLICT, NOT_FOUND
from sandbox_science.orchestrator.control.guard import guarded
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.projects import RunName, RunRecord, run_id_of, run_name

from .effort import EffortAudit, ResumeEffort, read_override

State = Literal["running", "waiting", "paused", "blocked", "completed", "stopped"]
ChildProgress = Literal["active", "blocked", "settled"]
PAGE = 100


class Work(Contract):
    """One research responsibility, independent of the attempt currently carrying it."""

    project: Identifier
    session: Identifier
    work: RunName
    root: RunName
    parent: RunName | None
    current: RunName
    role: Literal["main", "delegated"]
    kernel: Identifier | None
    state: State
    reason: str | None
    epoch: Annotated[int, Field(ge=1)]


class Ownership(Contract):
    """The session epoch and exact attempt that may mutate its work's state."""

    project: Identifier
    session: Identifier
    work: RunName
    run: RunName
    epoch: Annotated[int, Field(ge=1)]


CURRENT: ContextVar[Ownership | None] = ContextVar("research_work_owner", default=None)
_COLUMNS = (
    "w.project,w.session,w.work,w.root,w.parent,w.current,w.role,w.kernel,w.state,w.reason,s.epoch"
)


def _pending_children(db: sqlite3.Connection, project: str, session: str, work: str) -> bool:
    # A completed computation is still an obligation until a parent response consumed
    # its outcome. Receipts survive the final-save and acknowledgement crash windows.
    return (
        db.execute(
            "SELECT 1 FROM research_work WHERE project=? AND session=? AND parent=? "
            "AND state NOT IN ('completed','stopped') UNION ALL "
            "SELECT 1 FROM delegations d JOIN research_work a "
            "ON a.project=d.project_id AND a.session=d.session_id "
            "AND (EXISTS (SELECT 1 FROM research_attempts p WHERE p.project=a.project "
            "AND p.session=a.session AND p.run=d.parent AND p.work=a.work) OR EXISTS "
            "(SELECT 1 FROM research_continuations p WHERE p.project=a.project "
            "AND p.session=a.session AND p.run=d.parent AND p.work=a.work)) "
            "WHERE a.project=? AND a.session=? AND a.work=? AND (d.ended IS NULL OR "
            "(NOT EXISTS (SELECT 1 FROM research_attempts ack WHERE ack.project=a.project "
            "AND ack.session=a.session AND ack.work=a.work AND ack.run=d.acknowledged) "
            "AND NOT EXISTS (SELECT 1 FROM response_receipts r "
            "JOIN research_attempts p ON p.project=r.project AND p.session=r.session "
            "AND p.run=r.run WHERE p.project=a.project AND p.session=a.session "
            "AND p.work=a.work AND r.source='delegations' AND r.notice=(CASE WHEN d.results > 1 "
            "THEN d.delegation_id || '.' || d.results ELSE d.delegation_id END)))) "
            "UNION ALL "
            "SELECT 1 FROM research_child_settlements c JOIN research_attempts a "
            "ON a.project=c.project AND a.session=c.session AND a.run=c.run "
            "JOIN research_work w ON w.project=a.project AND w.session=a.session AND w.work=a.work "
            "WHERE w.project=? AND w.session=? AND w.parent=? AND c.settled=0 "
            "AND EXISTS (SELECT 1 FROM runs r WHERE r.project_id=c.project "
            "AND r.session_id=c.session AND r.name=c.run) LIMIT 1",
            (project, session, work, project, session, work, project, session, work),
        ).fetchone()
        is not None
    )


def _work(row: tuple[object, ...] | None) -> Work:
    if row is None:
        raise ControlError(NOT_FOUND, "research work was not found in this scope")
    return Work.model_validate(dict(zip(Work.model_fields, row, strict=True)))


def tree_cancelled(db: sqlite3.Connection, project: str, session: str, root: str) -> bool:
    """Whether any attempt in this tree has an explicit USER cancellation."""
    return (
        db.execute(
            "SELECT 1 FROM run_cancels c JOIN research_attempts a "
            "ON a.project=c.project_id AND a.session=c.session_id AND a.run=c.run "
            "JOIN research_work w ON w.project=a.project AND w.session=a.session "
            "AND w.work=a.work WHERE w.project=? AND w.session=? AND w.root=? LIMIT 1",
            (project, session, root),
        ).fetchone()
        is not None
    )


def _auto_claim_refusal(
    db: sqlite3.Connection, project: str, session: str, root: str, request: str
) -> str | None:
    """Recheck late USER changes inside the automatic request's claim transaction."""
    if (
        db.execute(
            "SELECT 1 FROM research_observations WHERE project=? AND session=? AND work=? "
            "AND kind='auto_resume' AND identity=?",
            (project, session, root, request),
        ).fetchone()
        is None
    ):
        return None  # explicit Resume retains its own operator-selected semantics
    if (
        db.execute(
            "SELECT 1 FROM research_work w JOIN research_sessions s USING(project,session) "
            "WHERE w.project=? AND w.session=? AND w.work=? AND w.root=s.root "
            "AND w.state IN ('paused','blocked')",
            (project, session, root),
        ).fetchone()
        is None
    ):
        return "automatic Resume invalidated by Stop or a newer research tree"
    if db.execute(
        "SELECT 1 FROM session_messages WHERE project_id=? AND session_id=? "
        "AND state='queued' LIMIT 1",
        (project, session),
    ).fetchone():
        return "automatic Resume invalidated by a new user message"
    if tree_cancelled(db, project, session, root):
        return "automatic Resume invalidated by cancellation"
    return None


def check_owner(db: sqlite3.Connection, owner: Ownership | None = None) -> None:
    """Fence a state mutation inside its transaction, including delayed thread work."""
    owner = owner or CURRENT.get()
    if owner is None:
        return
    row = db.execute(
        "SELECT 1 FROM research_work w JOIN research_sessions s USING(project,session) "
        "WHERE w.project=? AND w.session=? AND w.work=? AND w.current=? "
        "AND s.epoch=? AND s.root=w.root",
        (owner.project, owner.session, owner.work, owner.run, owner.epoch),
    ).fetchone()
    if row is None:
        raise ControlError(CONFLICT, "research attempt lost ownership; no new effect is admitted")


@contextmanager
def authority(owner: Ownership) -> Generator[None, None, None]:
    """Bind an attempt's authority to its async task and copied thread contexts."""
    token = CURRENT.set(owner)
    try:
        with guarded(lambda db: check_owner(db, owner)):
            yield
    finally:
        CURRENT.reset(token)


class WorkRecords:
    """Small transactional operations; history and tree enumeration are paged."""

    def __init__(
        self, control: ControlStore | DocumentStore, *, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self.control = control.control if isinstance(control, DocumentStore) else control
        self.clock = clock

    def get(self, project: str, session: str, work: str) -> Work:
        with self.control.read() as tx:
            return _work(
                tx.sql.execute(
                    f"SELECT {_COLUMNS} FROM research_work w "  # noqa: S608
                    "JOIN research_sessions s USING(project,session) "
                    "WHERE w.project=? AND w.session=? AND w.work=?",
                    (project, session, work),
                ).fetchone()
            )

    def for_run(self, project: str, session: str, run: str) -> Work | None:
        with self.control.read() as tx:
            row = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM research_work w "  # noqa: S608
                "JOIN research_sessions s USING(project,session) "
                "JOIN research_attempts a ON a.project=w.project AND a.session=w.session "
                "AND a.work=w.work WHERE a.project=? AND a.session=? AND a.run=?",
                (project, session, run),
            ).fetchone()
        if row is None:
            with self.control.read() as tx:
                row = tx.sql.execute(
                    f"SELECT {_COLUMNS} FROM research_work w "  # noqa: S608
                    "JOIN research_sessions s USING(project,session) JOIN research_observations o "
                    "ON o.project=w.project AND o.session=w.session AND o.work=w.work "
                    "WHERE w.project=? AND w.session=? AND o.kind='resume_lease' "
                    "AND json_extract(o.detail,'$.run')=?",
                    (project, session, run),
                ).fetchone()
        return _work(row) if row is not None else None

    def owner(self, project: str, session: str, run: str) -> Ownership | None:
        """Original authority, never the epoch of a later attempt."""
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT work,epoch FROM research_attempts WHERE project=? AND session=? AND run=?",
                (project, session, run),
            ).fetchone()
        return (
            None
            if row is None
            else Ownership(project=project, session=session, work=row[0], run=run, epoch=row[1])
        )

    def admit(
        self,
        project: str,
        session: str,
        run: str,
        *,
        parent: str | None = None,
        work: str | None = None,
        predecessor: str | None = None,
        epoch: int | None = None,
    ) -> Ownership:
        """Create a responsibility or claim one prepared by explicit Resume."""
        with self.control.write() as tx:
            db = tx.sql
            if work is not None:
                row = db.execute(
                    "SELECT w.current,w.state,w.root,s.epoch,s.root FROM research_work w "
                    "JOIN research_sessions s USING(project,session) "
                    "WHERE w.project=? AND w.session=? AND w.work=?",
                    (project, session, work),
                ).fetchone()
                if (
                    row is None
                    or row[0] != predecessor
                    or row[1] != "waiting"
                    or row[2] != row[4]
                    or row[3] != epoch
                ):
                    raise ControlError(CONFLICT, "continuation was not claimed by this Resume")
                chosen = int(row[3])
                db.execute(
                    "UPDATE research_work SET current=?,state='running',reason=NULL "
                    "WHERE project=? AND session=? AND work=?",
                    (run, project, session, work),
                )
            else:
                prepared = (
                    db.execute(
                        "SELECT w.work,a.epoch FROM research_work w JOIN research_attempts a "
                        "ON a.project=w.project AND a.session=w.session AND a.run=w.current "
                        "JOIN research_sessions s ON s.project=w.project AND s.session=w.session "
                        "WHERE w.project=? AND w.session=? AND w.current=? AND w.state='waiting' "
                        "AND a.epoch=s.epoch AND w.root=s.root AND w.parent=(SELECT work "
                        "FROM research_attempts WHERE project=? AND session=? AND run=?)",
                        (project, session, run, project, session, parent),
                    ).fetchone()
                    if parent is not None
                    else None
                )
                if prepared is not None:
                    db.execute(
                        "UPDATE research_work SET state='running' "
                        "WHERE project=? AND session=? AND work=?",
                        (project, session, prepared[0]),
                    )
                    return Ownership(
                        project=project,
                        session=session,
                        work=prepared[0],
                        run=run,
                        epoch=prepared[1],
                    )
                work = run
                inherited = (
                    None
                    if parent is None
                    else db.execute(
                        "SELECT w.work,w.root,s.epoch,s.root FROM research_work w "
                        "JOIN research_sessions s USING(project,session) "
                        "WHERE w.project=? AND w.session=? AND w.current=? "
                        "AND w.state IN ('running','waiting')",
                        (project, session, parent),
                    ).fetchone()
                )
                if parent is not None and (inherited is None or inherited[1] != inherited[3]):
                    raise ControlError(CONFLICT, "the parent no longer owns this research tree")
                if inherited is None:
                    pending_restore = db.execute(
                        "SELECT work,identity FROM research_observations WHERE project=? "
                        "AND session=? AND kind='kernel_restore' "
                        "AND json_extract(detail,'$.state')='intent' "
                        "AND json_extract(detail,'$.run') IS NULL "
                        "ORDER BY observed LIMIT 1",
                        (project, session),
                    ).fetchone()
                    if pending_restore is not None:
                        raise ControlError(
                            CONFLICT,
                            "lost_kernel: unresolved reconstruction blocks new main work",
                            {"work": pending_restore[0], "identity": pending_restore[1]},
                        )
                    db.execute(
                        "INSERT INTO research_sessions VALUES (?,?,?,1) "
                        "ON CONFLICT(project,session) DO UPDATE SET root=excluded.root,"
                        "epoch=epoch+1",
                        (project, session, work),
                    )
                    chosen = int(
                        db.execute(
                            "SELECT epoch FROM research_sessions WHERE project=? AND session=?",
                            (project, session),
                        ).fetchone()[0]
                    )
                    root, parent_work = work, None
                else:
                    parent_work, root, chosen = inherited[:3]
                db.execute(
                    "INSERT INTO research_work VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        project,
                        session,
                        work,
                        root,
                        parent_work,
                        run,
                        "main" if parent is None else "delegated",
                        None if parent is None else run_id_of(work),
                        "running",
                    ),
                )
            db.execute(
                "INSERT INTO research_attempts VALUES (?,?,?,?,?,?,NULL)",
                (project, session, run, work, predecessor, chosen),
            )
        return Ownership(project=project, session=session, work=work, run=run, epoch=chosen)

    def finish(self, record: RunRecord, *, persisted: bool = True) -> None:
        """A saved attempt closes only its own work; unfinished descendants veto completion."""
        project, session = record.context.project_id, record.context.session_id
        run = run_name(record.started_at, record.run_id)
        with self.control.write() as tx:
            db = tx.sql
            found = db.execute(
                "SELECT work FROM research_attempts WHERE project=? AND session=? AND run=?",
                (project, session, run),
            ).fetchone()
            if found is None:
                return  # historical runs have no invented work authority
            work = str(found[0])
            db.execute(
                "UPDATE research_child_settlements SET saved_status=? "
                "WHERE project=? AND session=? AND run=? AND saved_status IS NULL",
                (0 if persisted else 1, project, session, run),
            )
            children = _pending_children(db, project, session, work)
            state = (
                "stopped"
                if record.stopped == "cancelled"
                else "completed"
                if persisted
                and record.stopped == "answered"
                and not record.failure
                and not children
                else "paused"
            )
            reason = (
                "persistence_error"
                if not persisted
                else "unfinished_children"
                if children and record.stopped == "answered"
                else record.stopped
            )
            db.execute(
                "UPDATE research_attempts SET outcome=? WHERE project=? AND session=? AND run=? "
                "AND outcome IS NULL",
                (record.stopped, project, session, run),
            )
            db.execute(
                "UPDATE research_work SET state=?,reason=? WHERE project=? AND session=? "
                "AND work=? AND current=? AND state!='stopped'",
                (state, reason, project, session, work, run),
            )

    def interrupt_run(self, project: str, session: str, run: str, *, stopped: bool = False) -> None:
        """Fence unfinished execution without replacing a saved failure's diagnosis.

        Only an explicit stop supersedes paused or blocked work. Late cleanup of
        a superseded attempt cannot interrupt its successor.
        """
        with self.control.write() as tx:
            row = tx.sql.execute(
                "SELECT w.root FROM research_attempts a JOIN research_work w "
                "ON w.project=a.project AND w.session=a.session AND w.work=a.work "
                "JOIN research_sessions s ON s.project=a.project AND s.session=a.session "
                "WHERE a.project=? AND a.session=? AND a.run=? AND w.current=a.run "
                "AND a.epoch=s.epoch UNION ALL SELECT o.work FROM research_observations o "
                "JOIN research_sessions s USING(project,session) JOIN research_resumes r "
                "ON r.project=o.project AND r.session=o.session AND r.request=o.identity "
                "WHERE o.project=? AND o.session=? AND o.kind='resume_lease' "
                "AND json_extract(o.detail,'$.run')=? AND json_extract(o.detail,'$.epoch')=s.epoch "
                "AND r.status='running' AND r.root=s.root LIMIT 1",
                (project, session, run, project, session, run),
            ).fetchone()
            if row is None:
                return
            state, reason = ("stopped", "user_stop") if stopped else ("paused", "interrupted")
            tx.sql.execute(
                "UPDATE research_work SET state=?,reason=? WHERE project=? "
                "AND session=? AND root=? AND (state IN ('running','waiting') "
                "OR (? AND state IN ('paused','blocked')))",
                (state, reason, project, session, row[0], stopped),
            )
            tx.sql.execute(
                "UPDATE research_resumes SET status='blocked',reason=? WHERE project=? "
                "AND session=? AND root=? AND status='running'",
                (reason, project, session, row[0]),
            )

    def reject_queued(self, project: str, session: str, request: str, reason: str) -> None:
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE research_resumes SET status='blocked',reason=? "
                "WHERE project=? AND session=? AND request=? AND status='queued'",
                (reason[:1000], project, session, request),
            )

    def interrupt(
        self, project: str, session: str, root: str, *, detail: str | None = None
    ) -> None:
        """Record process loss without pretending incomplete work is done or stopped.

        ``detail``, the error that ended a Resume, is kept in its request's reason for the
        user; the work's own reason stays ``interrupted``, which automatic recovery reads.
        """
        reason = "interrupted" if detail is None else f"interrupted: {detail}"[:1000]
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE research_work SET state='paused',reason='interrupted' "
                "WHERE project=? AND session=? AND root=? AND state IN ('running','waiting')",
                (project, session, root),
            )
            tx.sql.execute(
                "UPDATE research_resumes SET status='blocked',reason=? "
                "WHERE project=? AND session=? AND root=? AND status='running'",
                (reason, project, session, root),
            )

    def page(self, project: str, session: str, root: str, *, after: str = "") -> list[Work]:
        with self.control.read() as tx:
            rows = tx.sql.execute(
                f"SELECT {_COLUMNS} FROM research_work w "  # noqa: S608
                "JOIN research_sessions s USING(project,session) "
                "WHERE w.project=? AND w.session=? AND w.root=? AND w.work>? "
                "ORDER BY w.work LIMIT ?",
                (project, session, root, after, PAGE),
            ).fetchall()
        return [_work(row) for row in rows]

    def attempts(self, project: str, session: str, work: str, *, after: str = "") -> list[str]:
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT run FROM research_attempts WHERE project=? AND session=? AND work=? "
                "AND run>? ORDER BY run LIMIT ?",
                (project, session, work, after, PAGE),
            ).fetchall()
        return [row[0] for row in rows]

    def block(self, project: str, session: str, work: str, reason: str) -> None:
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE research_work SET state='blocked',reason=? WHERE project=? AND session=? "
                "AND work=? AND state NOT IN ('completed','stopped')",
                (reason[:1000], project, session, work),
            )

    def request(
        self,
        project: str,
        session: str,
        root: str | None = None,
        *,
        effort: ResumeEffort | None = None,
        operator: str | None = None,
    ) -> str | None:
        """Queue at most one explicit Resume for a session, including without held chat."""
        audit = (
            None
            if effort is None
            else EffortAudit.model_validate({**effort.model_dump(), "operator": operator})
        )
        with self.control.write() as tx:
            db = tx.sql
            existing = db.execute(
                "SELECT request,root FROM research_resumes WHERE project=? AND session=? "
                "AND status IN ('queued','running')",
                (project, session),
            ).fetchone()
            if existing is not None:
                if root is not None and existing[1] != root:
                    raise ControlError(BUSY, "another research tree Resume is already pending")
                if read_override(db, project, session, existing[1], existing[0]) != audit:
                    raise ControlError(CONFLICT, "pending Resume effort override differs")
                return str(existing[0])
            if root is None:
                found = db.execute(
                    "SELECT work FROM research_work WHERE project=? AND session=? "
                    "AND parent IS NULL AND state IN ('paused','blocked','waiting') "
                    "ORDER BY work DESC LIMIT 1",
                    (project, session),
                ).fetchone()
                if found is None:
                    if audit is not None:
                        raise ControlError(
                            CONFLICT, "Resume effort requires unfinished research work"
                        )
                    return None
                root = str(found[0])
            eligible = db.execute(
                "SELECT root,state FROM research_work WHERE project=? AND session=? AND work=?",
                (project, session, root),
            ).fetchone()
            if eligible is None or eligible[0] != root or eligible[1] in ("completed", "stopped"):
                raise ControlError(CONFLICT, "this root is absent, completed or explicitly stopped")
            request = new_id("resume")
            db.execute(
                "INSERT INTO research_resumes VALUES (?,?,?,?,?,'queued',NULL)",
                (project, session, request, root, self.clock.now().timestamp()),
            )
            if audit is not None:
                db.execute(
                    "INSERT INTO research_observations VALUES (?,?,?,'resume_effort',?,?,?)",
                    (
                        project,
                        session,
                        root,
                        request,
                        self.clock.now().timestamp(),
                        audit.model_dump_json(),
                    ),
                )
        return request

    def queued(self) -> list[tuple[str, str, str, str]]:
        with self.control.read() as tx:
            return tx.sql.execute(
                "SELECT project,session,request,root FROM research_resumes "
                "WHERE status='queued' ORDER BY created LIMIT ?",
                (PAGE,),
            ).fetchall()

    def resume_source(
        self, project: str, session: str, request: str
    ) -> Literal["operator", "automatic"]:
        """Return trusted provenance for a persisted Resume request."""
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT EXISTS (SELECT 1 FROM research_observations o WHERE "
                "o.project=r.project AND o.session=r.session AND o.work=r.root "
                "AND o.kind='auto_resume' AND o.identity=r.request) "
                "FROM research_resumes r WHERE r.project=? AND r.session=? AND r.request=?",
                (project, session, request),
            ).fetchone()
        if row is None:
            raise ControlError(NOT_FOUND, "Resume request does not exist")
        return "automatic" if row[0] else "operator"

    def claim(self, project: str, session: str, request: str, *, lease_run: str) -> tuple[str, int]:
        """Advance the session fence while its caller holds the session run lease."""
        refusal: str | None = None
        epoch: int | None = None
        with self.control.write() as tx:
            db = tx.sql
            if (
                db.execute(
                    "SELECT 1 FROM run_leases WHERE project_id=? AND session_id=? AND run=?",
                    (project, session, lease_run),
                ).fetchone()
                is None
            ):
                raise ControlError(CONFLICT, "Resume requires its session run lease")
            row = db.execute(
                "SELECT root FROM research_resumes WHERE project=? AND session=? "
                "AND request=? AND status='queued'",
                (project, session, request),
            ).fetchone()
            if row is None:
                raise ControlError(CONFLICT, "Resume is no longer queued")
            root = str(row[0])
            refusal = _auto_claim_refusal(db, project, session, root, request)
            if refusal is None:
                epoch = self._accept_claim(db, project, session, request, root, lease_run)
            else:
                db.execute(
                    "UPDATE research_resumes SET status='blocked',reason=? "
                    "WHERE project=? AND session=? AND request=? AND status='queued'",
                    (refusal, project, session, request),
                )
        if refusal is not None:
            raise ControlError(CONFLICT, refusal)
        if epoch is None:
            raise ControlError(CONFLICT, "Resume did not claim an epoch")
        return root, epoch

    def _accept_claim(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        request: str,
        root: str,
        lease_run: str,
    ) -> int:
        db.execute(
            "UPDATE research_sessions SET epoch=epoch+1,root=? WHERE project=? AND session=?",
            (root, project, session),
        )
        epoch = int(
            db.execute(
                "SELECT epoch FROM research_sessions WHERE project=? AND session=?",
                (project, session),
            ).fetchone()[0]
        )
        db.execute(
            "UPDATE research_resumes SET status='running' WHERE project=? "
            "AND session=? AND request=?",
            (project, session, request),
        )
        db.execute(
            "INSERT INTO research_observations VALUES (?,?,?,'resume_lease',?,?,?)",
            (
                project,
                session,
                root,
                request,
                self.clock.now().timestamp(),
                json.dumps({"run": lease_run, "epoch": epoch}),
            ),
        )
        db.execute(
            "UPDATE research_work SET state='waiting',reason=NULL WHERE project=? "
            "AND session=? AND root=? AND state NOT IN ('completed','stopped')",
            (project, session, root),
        )
        # Claim owns input admission too: an operator may have disconnected
        # immediately after saving the Resume request. Never unhold another
        # tree, a terminal branch, or an input already consumed canonically.
        db.execute(
            "UPDATE session_messages AS m SET state='queued',updated_at=? "
            "WHERE m.project_id=? AND m.session_id=? AND m.state='held' AND EXISTS "
            "(SELECT 1 FROM research_attempts a JOIN research_work w "
            "USING(project,session,work) WHERE a.project=m.project_id "
            "AND a.session=m.session_id AND a.run=m.offered AND w.root=? "
            "AND w.state NOT IN ('completed','stopped')) AND NOT EXISTS "
            "(SELECT 1 FROM response_receipts r WHERE r.project=m.project_id "
            "AND r.session=m.session_id AND r.source='messages' AND r.notice=m.message_id)",
            (self.clock.now().isoformat(), project, session, root),
        )
        return epoch

    def complete_request(
        self, project: str, session: str, request: str, *, error: str | None = None
    ) -> None:
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE research_resumes SET status=?,reason=? WHERE project=? AND session=? "
                "AND request=? AND status='running'",
                (
                    "blocked" if error else "finished",
                    error[:1000] if error else None,
                    project,
                    session,
                    request,
                ),
            )

    def stop(self, project: str, session: str, work: str) -> None:
        """Durably stop the selected subtree without changing immutable attempt evidence."""
        with self.control.write() as tx:
            tx.sql.execute(
                "WITH RECURSIVE tree(work) AS (SELECT work FROM research_work "
                "WHERE project=? AND session=? AND work=? UNION ALL SELECT w.work "
                "FROM research_work w JOIN tree p ON w.parent=p.work WHERE w.project=? "
                "AND w.session=?) UPDATE research_work SET state='stopped',reason='user_stop' "
                "WHERE project=? AND session=? AND work IN tree AND state!='completed'",
                (project, session, work, project, session, project, session),
            )

    def never_started(self, project: str, session: str, run: str, reason: str) -> None:
        """End a delegated run's reserved work that never started, such as one whose own
        environment never became ready (CE-4): stopped, with nothing to resume. Work that
        started is left as its own record leaves it."""
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE research_work SET state='stopped',reason=? WHERE project=? "
                "AND session=? AND state='waiting' AND parent IS NOT NULL AND work=("
                "SELECT a.work FROM research_attempts a WHERE a.project=? AND a.session=? "
                "AND a.run=? AND a.outcome IS NULL) AND NOT EXISTS (SELECT 1 FROM runs r "
                "WHERE r.project_id=? AND r.session_id=? AND r.name=?)",
                (reason[:1000], project, session, project, session, run, project, session, run),
            )

    def unfinished(self, project: str, session: str, work: str) -> bool:
        with self.control.read() as tx:
            return _pending_children(tx.sql, project, session, work)

    def retains_kernel(self, project: str, session: str, kernel: str) -> bool:
        """Check an observed kernel individually; never load an unbounded tree-wide set.
        A returned task that awaits its parent's review keeps its kernels (PD-46)."""
        with self.control.read() as tx:
            return (
                tx.sql.execute(
                    "SELECT 1 FROM research_work WHERE project=? AND session=? "
                    "AND kernel=? AND state NOT IN ('completed','stopped') UNION ALL "
                    "SELECT 1 FROM research_work w JOIN research_attempts a "
                    "ON a.project=w.project AND a.session=w.session AND a.work=w.work "
                    "JOIN delegations d ON d.project_id=a.project AND d.session_id=a.session "
                    "AND d.run=a.run WHERE w.project=? AND w.session=? AND w.kernel=? "
                    "AND d.review='awaiting' LIMIT 1",
                    (project, session, kernel, project, session, kernel),
                ).fetchone()
                is not None
            )

    def awaits_review(self, project: str, session: str, work: str) -> bool:
        """Whether the work is a returned task's that awaits its parent's review, whose
        kernels and environment are kept (PD-46)."""
        with self.control.read() as tx:
            return (
                tx.sql.execute(
                    "SELECT 1 FROM research_attempts a JOIN delegations d "
                    "ON d.project_id=a.project AND d.session_id=a.session AND d.run=a.run "
                    "WHERE a.project=? AND a.session=? AND a.work=? AND d.review='awaiting' "
                    "LIMIT 1",
                    (project, session, work),
                ).fetchone()
                is not None
            )
