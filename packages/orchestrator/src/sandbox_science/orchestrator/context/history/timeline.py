# SPDX-License-Identifier: MIT
"""Bounded, attempt-scoped observations for the context inspector.

Answer times are archive observation times. Fold times are event emission times.
Auxiliary model intents have only a creation time, not a response time; their
observed usage is retained without pretending that creation was completion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.projects import ModelUsage


@dataclass(frozen=True)
class AnswerObservation:
    ordinal: int
    message: str
    observed_at: str
    role: str
    usage: ModelUsage


@dataclass(frozen=True)
class FoldObservation:
    seq: int
    at: str
    state: str | None
    fold: str | None
    span_start: int | None
    span_end: int | None
    freed_estimate: int | None
    tokens_estimate: int | None


@dataclass(frozen=True)
class AuxiliaryObservation:
    identity: str
    kind: str
    intent_at: str
    usage: ModelUsage | None


@dataclass(frozen=True)
class TimelinePage:
    answers: tuple[AnswerObservation, ...]
    folds: tuple[FoldObservation, ...]
    auxiliary: tuple[AuxiliaryObservation, ...]
    answers_after: int
    events_after: int
    more_answers: bool
    more_events: bool
    more_auxiliary: bool


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


class AttemptTimeline:
    """Read only one run attempt; continuations and children have separate timelines."""

    def __init__(self, store: ControlStore | DocumentStore) -> None:
        self.control = store if isinstance(store, ControlStore) else store.control

    def page(
        self,
        project: str,
        session: str,
        run: str,
        *,
        answers_after: int = 0,
        events_after: int = 0,
        limit: int = 20,
        tail: bool = False,
    ) -> TimelinePage:
        if not 1 <= limit <= 50 or answers_after < 0 or events_after < 0:
            raise ValueError("invalid timeline page")
        with self.control.isolated_read() as tx:
            if tail:
                # Start beside current activity instead of replaying hours of
                # sparse tool events before the live graph can catch up.
                answers_after = max(
                    0,
                    tx.sql.execute(
                        "SELECT coalesce(max(ordinal),0) FROM archive_model_usage "
                        "WHERE project=? AND session=? AND run=?",
                        (project, session, run),
                    ).fetchone()[0]
                    - limit,
                )
                events_after = max(
                    0,
                    tx.sql.execute(
                        "SELECT coalesce(max(seq),0) FROM run_events "
                        "WHERE project_id=? AND session_id=? AND run=?",
                        (project, session, run),
                    ).fetchone()[0]
                    - limit,
                )
            answers = tx.sql.execute(
                "SELECT ordinal,message_id,observed,role,usage FROM archive_model_usage "
                "WHERE project=? AND session=? AND run=? AND ordinal>? "
                "ORDER BY ordinal LIMIT ?",
                (project, session, run, answers_after, limit + 1),
            ).fetchall()
            # Bound the event index walk BEFORE filtering fold events. Sparse fold
            # histories return advancing empty pages instead of scanning a full run.
            events = tx.sql.execute(
                "SELECT seq,at,kind,"
                "substr(json_extract(data,'$.state'),1,64),"
                "substr(json_extract(data,'$.fold'),1,128),"
                "json_extract(data,'$.span[0]'),json_extract(data,'$.span[1]'),"
                "json_extract(data,'$.freed'),json_extract(data,'$.tokens') "
                "FROM run_events WHERE project_id=? AND session_id=? AND run=? "
                "AND seq>? ORDER BY seq LIMIT ?",
                (project, session, run, events_after, limit + 1),
            ).fetchall()
            auxiliary = tx.sql.execute(
                "SELECT created,identity,kind,usage FROM research_model_calls "
                "WHERE project=? AND session=? AND run=? AND kind NOT IN ('main','delegated') "
                "ORDER BY created DESC,identity DESC LIMIT ?",
                (project, session, run, limit + 1),
            ).fetchall()
        answer_rows = answers[:limit]
        event_rows = events[:limit]
        auxiliary_rows = auxiliary[:limit]
        return TimelinePage(
            answers=tuple(
                AnswerObservation(
                    row[0], row[1], _iso(row[2]), row[3], ModelUsage.model_validate_json(row[4])
                )
                for row in answer_rows
            ),
            folds=tuple(
                FoldObservation(
                    row[0],
                    row[1],
                    row[3],
                    row[4],
                    row[5] if isinstance(row[5], int) and row[5] >= 0 else None,
                    row[6] if isinstance(row[6], int) and row[6] >= 0 else None,
                    row[7] if isinstance(row[7], int) and not isinstance(row[7], bool) else None,
                    row[8] if isinstance(row[8], int) and row[8] >= 0 else None,
                )
                for row in event_rows
                if row[2] == "fold"
            ),
            auxiliary=tuple(
                AuxiliaryObservation(
                    row[1],
                    row[2],
                    _iso(row[0]),
                    ModelUsage.model_validate_json(row[3]) if row[3] is not None else None,
                )
                for row in auxiliary_rows
            ),
            answers_after=answer_rows[-1][0] if answer_rows else answers_after,
            events_after=event_rows[-1][0] if event_rows else events_after,
            more_answers=len(answers) > limit,
            more_events=len(events) > limit,
            more_auxiliary=len(auxiliary) > limit,
        )
