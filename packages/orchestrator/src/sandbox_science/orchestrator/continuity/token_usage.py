# SPDX-License-Identifier: MIT
"""Scoped token totals for a delegated grant, including fold and overview calls.

Canonical observations precede their exact legacy copies. SQL aggregation keeps a
long run's transcript and usage history out of the agent's working memory.
"""

# ruff: noqa: S608 - SQL fragments are fixed; all scope values are bound parameters.

import sqlite3

from .fold_usage import fold_copy
from .reservations import RESERVATIONS

# Callers supply only fixed SQL identifiers; never model or user text.
USAGE_SUM = (
    "coalesce(sum(CASE WHEN json_extract(usage,'$.input_tokens') IS NOT NULL "
    "AND json_extract(usage,'$.output_tokens') IS NOT NULL THEN "
    "json_extract(usage,'$.input_tokens')+json_extract(usage,'$.output_tokens') "
    "ELSE max(coalesce(json_extract(usage,'$.characters'),0)/4, "
    "coalesce(json_extract(usage,'$.input_tokens'),0)+"
    "coalesce(json_extract(usage,'$.output_tokens'),0)) END),0)"
)


def _scope(run: str | None) -> str:
    return (
        "WITH attempts AS (SELECT project,session,work,run FROM research_attempts "
        "WHERE project=? AND session=? AND work=?"
        + (" AND run=?" if run is not None else "")
        + ") "
    )


def _folds() -> str:
    return (
        "SELECT c.usage FROM research_model_calls c JOIN attempts a "
        "USING(project,session,run) WHERE c.kind='fold' AND c.usage IS NOT NULL "
        "UNION ALL SELECT c.item FROM run_collections c JOIN attempts a "
        "USING(project,session,run) WHERE c.collection='fold_calls' AND "
        + fold_copy("c.item", "c.project", "c.session", "c.run", "collection", "c.position")
        + " UNION ALL SELECT u.value FROM runs r JOIN attempts a ON "
        "r.project_id=a.project AND r.session_id=a.session AND r.name=a.run, "
        "json_each(r.record,'$.summaries.folds.calls') u WHERE NOT EXISTS "
        "(SELECT 1 FROM run_collections h WHERE h.project=a.project "
        "AND h.session=a.session AND h.run=a.run AND h.collection='fold_calls') AND "
        + fold_copy("u.value", "a.project", "a.session", "a.run", "summary", "u.key")
        + " UNION ALL SELECT u.value FROM run_events e JOIN attempts a ON "
        "e.project_id=a.project AND e.session_id=a.session AND e.run=a.run, "
        "json_each(e.data,'$.calls') u WHERE e.kind='fold' AND NOT EXISTS "
        "(SELECT 1 FROM runs r WHERE r.project_id=a.project AND r.session_id=a.session "
        "AND r.name=a.run) AND NOT EXISTS (SELECT 1 FROM run_collections h "
        "WHERE h.project=a.project AND h.session=a.session AND h.run=a.run "
        "AND h.collection='fold_calls') AND "
        + fold_copy("u.value", "a.project", "a.session", "a.run", "event", "u.key", "e.seq")
    )


def _reserved(*, folds_only: bool) -> str:
    return (
        "SELECT coalesce(sum(r.input_tokens+r.output_tokens),0) FROM ("
        + RESERVATIONS
        + ") r JOIN attempts a USING(project,session,work,run)"
        + (
            " JOIN research_model_calls c USING(project,session,run,identity) WHERE c.kind='fold'"
            if folds_only
            else ""
        )
    )


def fold_tokens(db: sqlite3.Connection, project: str, session: str, work: str, *, run: str) -> int:
    """Current attempt's fold observations and explicit unresolved reservations."""
    query = (
        _scope(run)
        + "SELECT (SELECT "
        + USAGE_SUM
        + " FROM ("
        + _folds()
        + "))+("
        + _reserved(folds_only=True)
        + ")"
    )
    return int(db.execute(query, (project, session, work, run)).fetchone()[0])


def spent_tokens(
    db: sqlite3.Connection, project: str, session: str, work: str, *, run: str | None = None
) -> int:
    """Observed plus explicitly reserved tokens for one work or its current attempt."""
    responses = (
        "SELECT u.usage FROM archive_model_usage u JOIN attempts a USING(project,session,run) "
        "UNION ALL SELECT c.usage FROM research_model_calls c JOIN attempts a "
        "USING(project,session,run) WHERE c.kind IN ('main','delegated') AND EXISTS "
        "(SELECT 1 FROM research_observations o WHERE o.project=c.project "
        "AND o.session=c.session AND o.work=a.work AND o.kind='resolution:model_usage' "
        "AND json_extract(o.detail,'$.resolution.run')=c.run "
        "AND json_extract(o.detail,'$.resolution.identity')=c.identity)"
    )
    query = (
        _scope(run)
        + "SELECT (SELECT "
        + USAGE_SUM
        + " FROM ("
        + responses
        + " UNION ALL "
        + _folds()
        + "))+("
        + _reserved(folds_only=False)
        + ")"
    )
    values = (project, session, work) + ((run,) if run is not None else ())
    return int(db.execute(query, values).fetchone()[0])
