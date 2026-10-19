# SPDX-License-Identifier: MIT
"""Bounded SQL resource measurements; never inspect a host or research filesystem."""

from __future__ import annotations

import json
import sqlite3

from .records import Ownership, check_owner


def sample_elapsed(db: sqlite3.Connection, owner: Ownership, seconds: int, observed: float) -> int:
    """Keep a monotone safe-point sample, excluding offline time across attempts."""
    check_owner(db, owner)
    db.execute(
        "INSERT INTO research_observations VALUES (?,?,?,'resource_elapsed',?,?,?) "
        "ON CONFLICT(project,session,work,kind,identity) DO UPDATE SET "
        "observed=max(observed,excluded.observed),detail=json_object('seconds',"
        "max(json_extract(detail,'$.seconds'),json_extract(excluded.detail,'$.seconds')))",
        (
            owner.project,
            owner.session,
            owner.work,
            owner.run,
            observed,
            json.dumps({"seconds": seconds}),
        ),
    )
    return elapsed_seconds(db, owner)


def elapsed_seconds(db: sqlite3.Connection, owner: Ownership) -> int:
    row = db.execute(
        "SELECT coalesce(sum(json_extract(detail,'$.seconds')),0) FROM research_observations "
        "WHERE project=? AND session=? AND work=? AND kind='resource_elapsed'",
        (owner.project, owner.session, owner.work),
    ).fetchone()
    return int(row[0])


def retained_bytes(db: sqlite3.Connection, owner: Ownership) -> int:
    """Logical retained revision bytes, including prior attempts; metadata is excluded."""
    row = db.execute(
        "SELECT coalesce(sum(coalesce(length(r.data),0)+coalesce(b.size,0)),0) "
        "FROM artifact_revisions r LEFT JOIN artifact_revision_blobs b "
        "ON b.project=r.project AND b.artifact=r.artifact AND b.number=r.number "
        "WHERE r.project=? AND json_extract(r.record,'$.publication.context.session_id')=? "
        "AND EXISTS (SELECT 1 FROM research_attempts a WHERE a.project=r.project "
        "AND a.session=? AND a.work=? AND substr(a.run,instr(a.run,'-')+1)="
        "json_extract(r.record,'$.publication.run_id'))",
        (owner.project, owner.session, owner.session, owner.work),
    ).fetchone()
    return int(row[0])


def research_progress(db: sqlite3.Connection, owner: Ownership) -> int:
    """Persisted evidence changes, not model calls or a claim of scientific novelty."""
    row = db.execute(
        "SELECT (SELECT count(*) FROM artifact_revisions WHERE project=? "
        "AND json_extract(record,'$.publication.context.session_id')=?) + "
        "(SELECT count(*) FROM session_plan_records WHERE project=? AND session=? "
        "AND kind='progress') + (SELECT count(*) FROM research_work WHERE project=? "
        "AND session=? AND root=? AND parent IS NOT NULL AND state='completed')",
        (
            owner.project,
            owner.session,
            owner.project,
            owner.session,
            owner.project,
            owner.session,
            owner.work,
        ),
    ).fetchone()
    return int(row[0])
