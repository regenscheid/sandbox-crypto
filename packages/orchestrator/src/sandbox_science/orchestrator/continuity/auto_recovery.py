# SPDX-License-Identifier: MIT
"""Durable, bounded admission of model failures and approved turn continuations.

This only queues an existing Resume after a saved model failure or a parent
approved a turn extension for paused child work. Recovery still reconciles original operation
identities, checks the project's budget and the work's token grant before dispatch.
Other failures remain for an explicit owner decision.
"""

from __future__ import annotations

import json
from typing import Final

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, new_id
from sandbox_science.orchestrator.control import ControlStore

from .grants import grant_total
from .plans import spent_tokens
from .records import Ownership, tree_cancelled
from .resources import turn_grant, turn_usage

MAX_NO_PROGRESS: Final = 3
RETRY_SECONDS: Final = 30


class AutoRecovery:
    """Queue a saved model-phase failure once per bounded, durable retry decision."""

    def __init__(self, control: ControlStore, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.control, self.clock = control, clock

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
                "AND w.reason IN ('error','unfinished_children') AND s.closed_at IS NULL "
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
                (recently_checked, *after),
            ).fetchall()
        return tuple((str(p), str(s), str(r)) for p, s, r in rows)

    def queue(self, project: str, session: str, root: str) -> str | None:
        """Atomically queue one tree Resume and its retry evidence, if still eligible.

        A durable observation tracks successive decisions. A restart cannot reset
        the backoff or replay a completed request. An answer or material progress
        resets the no-progress streak, while repeated failed attempts stop at three.
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
            if (
                work is None
                or work[0] not in ("paused", "blocked")
                or work[1] not in ("error", "unfinished_children")
            ):
                return None
            unfinished = db.execute(
                "SELECT w.current,w.reason,json_extract(r.record,'$.failure'),w.work "
                "FROM research_work w LEFT JOIN runs r ON r.project_id=w.project "
                "AND r.session_id=w.session AND r.name=w.current "
                "WHERE w.project=? AND w.session=? AND w.root=? "
                "AND w.state NOT IN ('completed','stopped')",
                (project, session, root),
            ).fetchall()
            eligible: list[bool] = []
            for current, reason, phase, work_id in unfinished:
                allowed = reason == "error" and phase == "model"
                if reason == "turn_limit":
                    original = db.execute(
                        "SELECT json_extract(snapshot,'$.request.max_turns') "
                        "FROM research_blueprints "
                        "WHERE project=? AND session=? AND work=?",
                        (project, session, work_id),
                    ).fetchone()
                    owner = Ownership(
                        project=project, session=session, work=work_id, run=current, epoch=1
                    )
                    total, revision = turn_grant(
                        db, owner, original[0] if original and original[0] else 300
                    )
                    allowed = revision > 0 and total > turn_usage(db, owner)
                if reason != "unfinished_children" and not allowed:
                    return None
                eligible.append(allowed)
            if not any(eligible):
                return None
            if tree_cancelled(db, project, session, root):
                return None
            # A tool operation without its original result may have changed the
            # outside world. Only an explicit reconciliation can release it.
            if db.execute(
                "SELECT 1 FROM tool_intents i JOIN research_attempts a "
                "ON a.project=i.project AND a.session=i.session AND a.run=i.run "
                "JOIN research_work w ON w.project=a.project AND w.session=a.session "
                "AND w.work=a.work WHERE w.project=? AND w.session=? AND w.root=? "
                "AND NOT EXISTS (SELECT 1 FROM archive_tool_records t WHERE "
                "t.project=i.project AND t.session=i.session AND t.run=i.run "
                "AND t.operation_id=i.operation_id) LIMIT 1",
                (project, session, root),
            ).fetchone():
                return None
            # A grant is cumulative across attempts. Only a USER grant decision
            # can increase it; do not queue a recovery that would immediately
            # fail before its first model dispatch.
            grants = db.execute(
                "SELECT w.work,json_extract(b.snapshot,'$.request.tokens') "
                "FROM research_work w LEFT JOIN research_blueprints b "
                "ON b.project=w.project AND b.session=w.session AND b.work=w.work "
                "WHERE w.project=? AND w.session=? AND w.root=? "
                "AND w.state NOT IN ('completed','stopped')",
                (project, session, root),
            ).fetchall()
            if any(
                isinstance(original, int)
                and spent_tokens(db, project, session, work)
                >= grant_total(db, project, session, work, original)
                for work, original in grants
            ):
                return None
            progress = db.execute(
                "SELECT (SELECT count(*) FROM research_work WHERE project=? AND session=? "
                "AND root=? AND state='completed'), "
                "(SELECT count(*) FROM archive_model_usage u JOIN research_attempts a "
                "ON a.project=u.project AND a.session=u.session AND a.run=u.run "
                "JOIN research_work w ON w.project=a.project AND w.session=a.session "
                "AND w.work=a.work WHERE w.project=? AND w.session=? AND w.root=?)",
                (project, session, root, project, session, root),
            ).fetchone()
            prior = db.execute(
                "SELECT o.observed,o.detail FROM research_observations o "
                "WHERE o.project=? AND o.session=? AND o.work=? AND o.kind='auto_resume' "
                "ORDER BY o.observed DESC LIMIT 1",
                (project, session, root),
            ).fetchone()
            streak = 1
            if prior is not None:
                earlier = json.loads(str(prior[1]))
                if earlier["progress"] == list(progress):
                    streak = int(earlier["streak"]) + 1
                if streak > MAX_NO_PROGRESS or now - float(prior[0]) < min(
                    RETRY_SECONDS * 2 ** (streak - 2 if streak > 1 else 0), 900
                ):
                    return None
            request = new_id("resume")
            db.execute(
                "INSERT INTO research_resumes VALUES (?,?,?,?,?,'queued',NULL)",
                (project, session, request, root, now),
            )
            db.execute(
                "INSERT INTO research_observations VALUES (?,?,?,'auto_resume',?,?,?)",
                (
                    project,
                    session,
                    root,
                    request,
                    now,
                    json.dumps(
                        {"progress": list(progress), "streak": streak}, separators=(",", ":")
                    ),
                ),
            )
            return request
