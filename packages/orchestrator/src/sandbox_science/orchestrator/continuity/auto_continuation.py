# SPDX-License-Identifier: MIT
"""Idempotent continuation of an answered root whose research plan remains active.

A plan continues only while one of its steps can still be worked: pending or in progress.
When the agent has marked every unfinished step blocked, as it does when no justified step
remains, the session waits for the user instead of starting run after run that rereads the
plan and finds nothing to do (Mac, 2026-10-21); a plan has no status of its own for that.
"""

from __future__ import annotations

import json
import sqlite3

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.projects import run_id_of
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import (
    CompactPlan,
    PlanAuthority,
    PlanScope,
    PlanSummary,
    SessionPlans,
)

from .records import tree_cancelled

PAGE = 100
AUTHOR = "research-supervisor"


class AutoContinuation:
    """Queue one new root input per active-plan state after a settled answer."""

    def __init__(self, control: ControlStore, documents: DocumentStore, runs: RunState) -> None:
        self.control, self.plans, self.runs = control, SessionPlans(documents), runs

    def candidates(
        self, *, after: tuple[str, str, str] = ("", "", "")
    ) -> tuple[tuple[str, str, str], ...]:
        """Page idle, answered roots; the plan is checked at the final write."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT w.project,w.session,w.work FROM research_work w "
                "JOIN research_sessions rs USING(project,session) "
                "JOIN research_attempts a ON a.project=w.project AND a.session=w.session "
                "AND a.run=w.current AND a.work=w.work "
                "JOIN sessions s ON s.project_id=w.project AND s.session_id=w.session "
                "WHERE w.parent IS NULL AND w.root=rs.root AND w.state='completed' "
                "AND a.outcome='answered' AND s.closed_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM research_work child WHERE child.project=w.project "
                "AND child.session=w.session AND child.root=w.root AND child.state!='completed') "
                "AND NOT EXISTS (SELECT 1 FROM research_resumes q WHERE q.project=w.project "
                "AND q.session=w.session AND q.status IN ('queued','running')) "
                "AND NOT EXISTS (SELECT 1 FROM run_leases l WHERE l.project_id=w.project "
                "AND l.session_id=w.session) "
                "AND NOT EXISTS (SELECT 1 FROM session_messages m WHERE m.project_id=w.project "
                "AND m.session_id=w.session AND m.state IN ('queued','held')) "
                "AND (w.project,w.session,w.work)>(?,?,?) "
                "ORDER BY w.project,w.session,w.work LIMIT ?",
                (*after, PAGE),
            ).fetchall()
        return tuple((str(p), str(s), str(root)) for p, s, root in rows)

    @staticmethod
    def _authority(project: str, session: str, root: str) -> PlanAuthority:
        return PlanAuthority(
            scope=PlanScope(project_id=project, session_id=session),
            role="main",
            run_id=run_id_of(root),
        )

    @staticmethod
    def _identity(plan: PlanSummary) -> tuple[str, int, int, int, int]:
        return (
            plan.head.plan_id,
            plan.head.version,
            plan.status_revision,
            plan.progress_cursor,
            plan.event_cursor,
        )

    @staticmethod
    def _workable(plan: CompactPlan | None) -> bool:
        """Whether a step can still be worked: its current status pending or in progress. A
        step changed since its latest progress counts as pending, as the plan view shows it."""
        if plan is None:
            return False
        for step in plan.steps:
            latest = step.latest
            current = latest.status if latest is not None and step.matches_current_step else None
            if current in (None, "pending", "in_progress"):
                return True
        return False

    @staticmethod
    def _message(plan: PlanSummary) -> str:
        ref = plan.head
        return (
            f"The active research plan {ref.plan_id} version {ref.version} remains open "
            "after the previous root answered. Read the current plan, recent child "
            "outcomes, documents and artifacts, then pursue the next justified research "
            "step. Record progress and evidence in the plan. Do not repeat settled "
            "work or claim an unverified result. "
            "If no useful step remains, update the plan's status and explain why."
        )

    @staticmethod
    def _eligible(db: sqlite3.Connection, project: str, session: str, root: str) -> bool:
        if not AutoContinuation._eligible_without_lease(db, project, session, root):
            return False
        return (
            db.execute(
                "SELECT 1 FROM run_leases WHERE project_id=? AND session_id=? LIMIT 1",
                (project, session),
            ).fetchone()
            is None
            and db.execute(
                "SELECT 1 FROM session_messages WHERE project_id=? AND session_id=? "
                "AND state IN ('queued','held') LIMIT 1",
                (project, session),
            ).fetchone()
            is None
        )

    def queue(self, project: str, session: str, root: str) -> str | None:
        """Atomically recheck the tree and plan, then queue a new root input once."""
        authority = self._authority(project, session, root)
        plan = self.plans.summary(authority)
        if plan is None or plan.status != "active":
            return None
        if not self._workable(self.plans.compact_current(authority)):
            return None
        identity = json.dumps(self._identity(plan), separators=(",", ":"))
        message_id = new_id("msg")
        content = self._message(plan)

        def eligible(db: sqlite3.Connection) -> bool:
            if not self._eligible(db, project, session, root):
                return False
            current = self.plans.summary_in(db, authority)
            return (
                current is not None
                and current == plan
                and current.status == "active"
                and self._workable(self.plans.compact_in(db, authority))
                and db.execute(
                    "SELECT 1 FROM research_observations WHERE project=? AND session=? "
                    "AND kind='auto_continue' AND identity=?",
                    (project, session, identity),
                ).fetchone()
                is None
            )

        def record(db: sqlite3.Connection) -> None:
            db.execute(
                "INSERT INTO research_observations VALUES (?,?,?,'auto_continue',?,?,?)",
                (
                    project,
                    session,
                    root,
                    identity,
                    self.runs.clock.now().timestamp(),
                    json.dumps(
                        {"message_id": message_id, "plan": self._identity(plan)},
                        separators=(",", ":"),
                    ),
                ),
            )

        return (
            message_id
            if self.runs.post_if(
                project,
                session,
                message_id,
                content,
                author=AUTHOR,
                eligible=eligible,
                on_queue=record,
            )
            else None
        )

    def claim_queued(self, project: str, session: str, run: str) -> tuple[str, ...]:
        """Select offered input and withdraw stale continuations in one lease-held write.

        A message arriving after this claim belongs to the live run's next input,
        rather than retroactively changing the task it already selected.
        """
        with self.control.write() as tx:
            db = tx.sql
            if (
                db.execute(
                    "SELECT 1 FROM run_leases WHERE project_id=? AND session_id=? AND run=?",
                    (project, session, run),
                ).fetchone()
                is None
            ):
                return ()
            queued = db.execute(
                "SELECT message_id FROM session_messages WHERE project_id=? "
                "AND session_id=? AND state='queued' ORDER BY number",
                (project, session),
            ).fetchall()
            if not queued:
                return ()
            marked: list[tuple[str, str, str]] = []
            other_input = False
            for (message_id,) in queued:
                observation = db.execute(
                    "SELECT o.work,o.detail FROM research_observations o WHERE o.project=? "
                    "AND o.session=? AND o.kind='auto_continue' "
                    "AND json_extract(o.detail,'$.message_id')=? LIMIT 1",
                    (project, session, message_id),
                ).fetchone()
                if observation is None:
                    other_input = True
                else:
                    marked.append((str(message_id), str(observation[0]), str(observation[1])))
            for message_id, root, raw in marked:
                authority = self._authority(project, session, root)
                current = self.plans.summary_in(db, authority)
                pinned = tuple(json.loads(raw)["plan"])
                valid = (
                    not other_input
                    and self._eligible_without_lease(db, project, session, root)
                    and current is not None
                    and current.status == "active"
                    and self._identity(current) == pinned
                    and self._workable(self.plans.compact_in(db, authority))
                )
                if not valid:
                    db.execute(
                        "UPDATE session_messages SET state='refused',reason='superseded',"
                        "updated_at=? WHERE message_id=? AND state='queued'",
                        (self.runs.clock.now().isoformat(), message_id),
                    )
            selected = tuple(
                str(row[0])
                for row in db.execute(
                    "SELECT message_id FROM session_messages WHERE project_id=? "
                    "AND session_id=? AND state='queued' ORDER BY number",
                    (project, session),
                ).fetchall()
            )
            db.executemany(
                "UPDATE session_messages SET offered=? WHERE project_id=? AND session_id=? "
                "AND message_id=? AND state='queued'",
                [(run, project, session, message_id) for message_id in selected],
            )
            return selected

    @staticmethod
    def _eligible_without_lease(
        db: sqlite3.Connection, project: str, session: str, root: str
    ) -> bool:
        """Validate the saved checkpoint after the next run acquired its lease."""
        if tree_cancelled(db, project, session, root):
            return False
        return (
            db.execute(
                "SELECT 1 FROM research_work w JOIN research_sessions rs USING(project,session) "
                "JOIN research_attempts a ON a.project=w.project AND a.session=w.session "
                "AND a.run=w.current AND a.work=w.work JOIN sessions s "
                "ON s.project_id=w.project AND s.session_id=w.session "
                "WHERE w.project=? AND w.session=? AND w.work=? AND w.parent IS NULL "
                "AND w.root=rs.root AND w.state='completed' AND a.outcome='answered' "
                "AND s.closed_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM research_work child WHERE child.project=w.project "
                "AND child.session=w.session AND child.root=w.root AND child.state!='completed') "
                "AND NOT EXISTS (SELECT 1 FROM research_resumes q WHERE q.project=w.project "
                "AND q.session=w.session AND q.status IN ('queued','running')) LIMIT 1",
                (project, session, root),
            ).fetchone()
            is not None
        )
