# SPDX-License-Identifier: MIT
"""Durable, bounded admission of transient failures and approved turn continuations (D100).

This only queues an existing tree Resume after a failure classified transient
(``transient.py``: network, gateway 5xx/524, model timeout, a stream without a final
answer, worker or bridge loss, a persistence error) or a parent approved a turn
extension for paused child work. Each decision is persisted; increasing backoff, a
bounded attempt count and a no-progress check end the retries with a durable reason on
the root. Recovery still reconciles original operation identities by their recorded
identity before dispatch and never replays an unknown effect; a pending effect no
adapter can reconcile, a permanent failure, a budget refusal or a Stop is not retried.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Final

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, new_id
from sandbox_science.orchestrator.control import ControlStore

from .records import Ownership, tree_cancelled
from .recovery_tools import tool_reconcilable
from .resource_policy import continuation_allowed
from .resources import turn_grant, turn_usage
from .transient import REASON_PREFIX, TRANSIENT, Prior, RetryPolicy, decide

MAX_NO_PROGRESS: Final = 3
RETRY_SECONDS: Final = 30
STOPPED: Final = "retries_exhausted"
"""The root's reason once automatic retries end; the user's one Resume still continues."""
_APPROVED: Final = ("turn_limit", "resource_time", "resource_storage")


class AutoRecovery:
    """Queue a saved model-phase failure once per bounded, durable retry decision."""

    def __init__(
        self,
        control: ControlStore,
        *,
        clock: Clock = SYSTEM_CLOCK,
        interrupted: bool = False,
        policy: RetryPolicy | None = None,
    ) -> None:
        """``interrupted`` also retries work interrupted without a recorded transient
        cause, such as a process restart (opt-in infrastructure recovery)."""
        self.control, self.clock = control, clock
        self.interrupted = interrupted
        self.policy = policy or RetryPolicy(
            base_seconds=RETRY_SECONDS, max_no_progress=MAX_NO_PROGRESS
        )

    def candidates(
        self, *, after: tuple[str, str, str] = ("", "", "")
    ) -> tuple[tuple[str, str, str], ...]:
        """Page roots that might need recovery without reading transcripts or payloads."""
        recently_checked = self.clock.now().timestamp() - RETRY_SECONDS
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT w.project,w.session,w.work FROM research_work w "
                "JOIN research_sessions rs USING(project,session) "
                "JOIN sessions s ON s.project_id=w.project AND s.session_id=w.session "
                "WHERE w.parent IS NULL AND w.state IN ('paused','blocked') "
                "AND (w.reason IN ('error','unfinished_children','turn_limit',"
                "'resource_time','resource_storage','persistence_error') "
                "OR w.reason LIKE 'transient:%' OR (w.reason='interrupted' AND (? OR EXISTS "
                "(SELECT 1 FROM research_observations t WHERE t.project=w.project "
                "AND t.session=w.session AND t.work=w.work AND t.kind='transient_failure')))) "
                "AND s.closed_at IS NULL "
                "AND w.root=rs.root "
                "AND NOT EXISTS (SELECT 1 FROM research_resumes q WHERE q.project=w.project "
                "AND q.session=w.session AND q.status IN ('queued','running')) "
                "AND NOT EXISTS (SELECT 1 FROM run_leases l WHERE l.project_id=w.project "
                "AND l.session_id=w.session) "
                "AND NOT EXISTS (SELECT 1 FROM session_messages m WHERE m.project_id=w.project "
                "AND m.session_id=w.session AND m.state='queued') "
                "AND NOT EXISTS (SELECT 1 FROM research_observations o WHERE o.project=w.project "
                "AND o.session=w.session AND o.work=w.work AND o.kind='auto_resume' "
                "AND o.observed>?) "
                "AND (w.project,w.session,w.work)>(?,?,?) "
                "ORDER BY w.project,w.session,w.work LIMIT 100",
                (self.interrupted, recently_checked, *after),
            ).fetchall()
        return tuple((str(p), str(s), str(r)) for p, s, r in rows)

    def queue(self, project: str, session: str, root: str) -> str | None:
        """Atomically queue one tree Resume and its retry evidence, if still eligible.

        A durable observation tracks successive decisions. A restart cannot reset
        the backoff or replay a completed request. An answer, new evidence, completed
        work or a plan change resets the no-progress streak; repeated attempts without
        progress, or too many in one episode, block the root with ``retries_exhausted``.
        """
        now = self.clock.now().timestamp()
        with self.control.write() as tx:
            db = tx.sql
            existing = db.execute(
                "SELECT 1 FROM research_resumes WHERE project=? AND session=? "
                "AND status IN ('queued','running')",
                (project, session),
            ).fetchone()
            if existing is not None:
                return None
            if db.execute(
                "SELECT 1 FROM run_leases WHERE project_id=? AND session_id=?",
                (project, session),
            ).fetchone():
                return None
            # A new user message may correct the research direction; let the
            # ordinary chat path offer that instruction before restarting work.
            if db.execute(
                "SELECT 1 FROM session_messages WHERE project_id=? AND session_id=? "
                "AND state='queued' LIMIT 1",
                (project, session),
            ).fetchone():
                return None
            if db.execute(
                "SELECT 1 FROM sessions WHERE project_id=? AND session_id=? "
                "AND closed_at IS NOT NULL",
                (project, session),
            ).fetchone():
                return None
            work = db.execute(
                "SELECT w.state,w.reason FROM research_work w "
                "JOIN research_sessions s USING(project,session) WHERE w.project=? "
                "AND w.session=? AND w.work=? AND w.root=? AND w.root=s.root "
                "AND w.parent IS NULL",
                (project, session, root, root),
            ).fetchone()
            if work is None or work[0] not in ("paused", "blocked"):
                return None
            unfinished = db.execute(
                "SELECT w.current,w.reason,json_extract(r.record,'$.failure'),w.work,"
                "json_type(r.record,'$.summaries.failure'),"
                "json_extract(r.record,'$.summaries.failure.transient'),r.name IS NOT NULL "
                "FROM research_work w LEFT JOIN runs r ON r.project_id=w.project "
                "AND r.session_id=w.session AND r.name=w.current "
                "WHERE w.project=? AND w.session=? AND w.root=? "
                "AND w.state NOT IN ('completed','stopped')",
                (project, session, root),
            ).fetchall()
            causes: list[str] = []
            for current, reason, phase, work_id, classified, kind, saved in unfinished:
                cause = self._cause(
                    db, project, session, work_id, reason, phase, classified is not None, kind
                )
                if cause == "persistence" and saved:
                    # The record was kept and only an acknowledgement failed: its outcome
                    # is canonical and reconciliation delivers it. A new paid attempt for
                    # already answered work is not a retry of the failure.
                    return None
                if cause is None and reason in _APPROVED:
                    if not self._approved(db, project, session, work_id, current, reason):
                        return None
                    cause = reason
                elif cause is None and reason not in ("unfinished_children", "interrupted"):
                    return None  # a permanent failure, a budget, a block: not retried
                if cause is not None:
                    causes.append(cause)
            if not causes:
                return None
            if tree_cancelled(db, project, session, root):
                return None
            # Recovery reconciles supported operations by original identity before any
            # dispatch. It never replays their call or admits an unknown adapter: such a
            # pending effect is irreconcilable here and stops automatic recovery.
            pending = db.execute(
                "SELECT i.tool,i.run,i.operation_id,CASE i.tool WHEN 'resource_decide' "
                "THEN i.arguments ELSE '' END FROM tool_intents i JOIN research_attempts a "
                "ON a.project=i.project AND a.session=i.session AND a.run=i.run "
                "JOIN research_work w ON w.project=a.project AND w.session=a.session "
                "AND w.work=a.work WHERE w.project=? AND w.session=? AND w.root=? "
                "AND NOT EXISTS (SELECT 1 FROM archive_tool_records t WHERE "
                "t.project=i.project AND t.session=i.session AND t.run=i.run "
                "AND t.operation_id=i.operation_id)",
                (project, session, root),
            )
            if any(
                not tool_reconcilable(db, project, session, run, operation, name, arguments)
                for name, run, operation, arguments in pending
            ):
                return None
            progress = _progress(db, project, session, root)
            row = db.execute(
                "SELECT o.observed,o.detail FROM research_observations o "
                "WHERE o.project=? AND o.session=? AND o.work=? AND o.kind='auto_resume' "
                "ORDER BY o.observed DESC LIMIT 1",
                (project, session, root),
            ).fetchone()
            prior = None
            if row is not None:
                earlier = json.loads(str(row[1]))
                streak = int(earlier["streak"])
                prior = Prior(
                    observed=float(row[0]),
                    progress=tuple(int(value) for value in earlier["progress"]),
                    streak=streak,
                    attempts=int(earlier.get("attempts", streak)),
                )
            decision = decide(self.policy, prior, progress, now)
            cause = ",".join(sorted(set(causes)))
            if decision.action == "wait":
                return None
            detail = json.dumps(
                {
                    "progress": list(progress),
                    "streak": decision.streak,
                    "attempts": decision.attempts,
                    "delay": decision.delay,
                    "cause": cause,
                    **({"stopped": decision.reason} if decision.reason else {}),
                },
                separators=(",", ":"),
            )
            if decision.action == "stop":
                # Durable: the root says why automatic recovery ended, and the scan
                # no longer offers it. The user's one Resume still continues the tree.
                db.execute(
                    "INSERT INTO research_observations VALUES (?,?,?,'auto_resume_stopped',?,?,?)",
                    (project, session, root, new_id("stop"), now, detail),
                )
                db.execute(
                    "UPDATE research_work SET state='blocked',reason=? WHERE project=? "
                    "AND session=? AND work=? AND state IN ('paused','blocked')",
                    (
                        f"{STOPPED}: {decision.reason}; last cause {cause}"[:1000],
                        project,
                        session,
                        root,
                    ),
                )
                return None
            request = new_id("resume")
            db.execute(
                "INSERT INTO research_resumes VALUES (?,?,?,?,?,'queued',NULL)",
                (project, session, request, root, now),
            )
            db.execute(
                "INSERT INTO research_observations VALUES (?,?,?,'auto_resume',?,?,?)",
                (project, session, root, request, now, detail),
            )
            return request

    def _cause(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        work: str,
        reason: str | None,
        phase: object,
        classified: bool,
        kind: object,
    ) -> str | None:
        """The transient kind of one unfinished work's pause, or ``None``."""
        if reason == "error":
            if classified:  # the loop classified the failure (D100)
                return str(kind) if kind in TRANSIENT else None
            return "model" if phase == "model" else None  # legacy model-phase record
        if reason == "persistence_error":
            return "persistence"
        if reason is not None and reason.startswith(REASON_PREFIX):
            named = reason.removeprefix(REASON_PREFIX).split(":", 1)[0]
            return named if named in TRANSIENT else None
        if reason == "interrupted":
            lost = db.execute(
                "SELECT json_extract(detail,'$.kind') FROM research_observations "
                "WHERE project=? AND session=? AND work=? AND kind='transient_failure' "
                # A loss before the latest Resume was already retried; a later
                # interruption, such as a process restart, is not that loss.
                "AND observed>coalesce((SELECT max(created) FROM research_resumes "
                "WHERE project=? AND session=?),0) ORDER BY observed DESC LIMIT 1",
                (project, session, work, project, session),
            ).fetchone()
            if lost is not None and lost[0] in TRANSIENT:
                return str(lost[0])
            return "interrupted" if self.interrupted else None
        return None

    @staticmethod
    def _approved(
        db: sqlite3.Connection, project: str, session: str, work: str, current: str, reason: str
    ) -> bool:
        """A parent or operator approved more turns, time or storage for paused work."""
        owner = Ownership(project=project, session=session, work=work, run=current, epoch=1)
        if reason == "turn_limit":
            original = db.execute(
                "SELECT json_extract(snapshot,'$.request.max_turns') "
                "FROM research_blueprints WHERE project=? AND session=? AND work=?",
                (project, session, work),
            ).fetchone()
            total, revision = turn_grant(
                db, owner, original[0] if original and original[0] else 300
            )
            return revision > 0 and total > turn_usage(db, owner)
        return continuation_allowed(db, owner, reason)


def _progress(db: sqlite3.Connection, project: str, session: str, root: str) -> tuple[int, ...]:
    """Completed work, answers, tool evidence and plan changes; any change is progress."""
    row = db.execute(
        "SELECT (SELECT count(*) FROM research_work WHERE project=? AND session=? "
        "AND root=? AND state='completed'), "
        "(SELECT count(*) FROM archive_model_usage u JOIN research_attempts a "
        "ON a.project=u.project AND a.session=u.session AND a.run=u.run "
        "JOIN research_work w ON w.project=a.project AND w.session=a.session "
        "AND w.work=a.work WHERE w.project=? AND w.session=? AND w.root=?), "
        "(SELECT count(*) FROM archive_tool_records t JOIN research_attempts a "
        "ON a.project=t.project AND a.session=t.session AND a.run=t.run "
        "JOIN research_work w ON w.project=a.project AND w.session=a.session "
        "AND w.work=a.work WHERE w.project=? AND w.session=? AND w.root=?), "
        "(SELECT count(*) FROM session_plan_records WHERE project=? AND session=?)",
        (project, session, root, project, session, root, project, session, root, project, session),
    ).fetchone()
    return tuple(int(value) for value in row)


def mark_transient(
    control: ControlStore,
    project: str,
    session: str,
    run: str,
    kind: str,
    *,
    clock: Clock = SYSTEM_CLOCK,
) -> None:
    """Record that ``run``'s work was interrupted by a transient loss (bridge, worker).

    Automatic recovery then retries the interrupted tree; a process restart without such
    a record still waits for the user's Resume unless infrastructure recovery is on.
    """
    if kind not in TRANSIENT:
        raise ValueError(f"not a transient failure kind: {kind}")
    with control.write() as tx:
        row = tx.sql.execute(
            "SELECT w.root FROM research_attempts a JOIN research_work w "
            "ON w.project=a.project AND w.session=a.session AND w.work=a.work "
            "WHERE a.project=? AND a.session=? AND a.run=? UNION ALL "
            "SELECT o.work FROM research_observations o WHERE o.project=? AND o.session=? "
            "AND o.kind='resume_lease' AND json_extract(o.detail,'$.run')=? LIMIT 1",
            (project, session, run, project, session, run),
        ).fetchone()
        if row is None:
            return
        tx.sql.execute(
            "INSERT INTO research_observations VALUES (?,?,?,'transient_failure',?,?,?) "
            "ON CONFLICT(project,session,work,kind,identity) DO NOTHING",
            (
                project,
                session,
                row[0],
                run,
                clock.now().timestamp(),
                json.dumps({"kind": kind, "run": run}, separators=(",", ":")),
            ),
        )
