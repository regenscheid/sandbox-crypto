# SPDX-License-Identifier: MIT
"""Renewable research allowances, distinct from worker and security limits."""

from __future__ import annotations

import sqlite3
from typing import Literal

from sandbox_science.orchestrator.delegation.records import DelegationSettings, settings_in

from .records import Ownership
from .resource_usage import elapsed_seconds, retained_bytes

Resource = Literal[
    "tokens",
    "turns",
    "elapsed_seconds",
    "retained_bytes",
    "retention_bytes",
    "retention_revisions",
    "retention_operations",
    "retention_metadata_bytes",
]
Action = Literal[
    "continue", "redirect", "report", "pause", "extend_turns", "extend_time", "extend_storage"
]
EXTENSIONS: dict[str, tuple[Resource, int, str]] = {
    "extend_turns": ("turns", 300, "turn_ceiling"),
    "extend_time": ("elapsed_seconds", 3600, "time_ceiling_seconds"),
    "extend_storage": ("retained_bytes", 1024**3, "storage_ceiling_bytes"),
}


def extension(resource: str) -> str | None:
    return next((action for action, (kind, _, _) in EXTENSIONS.items() if kind == resource), None)


def soft_grant(
    db: sqlite3.Connection, owner: Ownership, resource: str, original: int
) -> tuple[int, int]:
    row = db.execute(
        "SELECT max(json_extract(detail,'$.total')) FROM research_observations "
        "WHERE project=? AND session=? AND work=? AND kind='resource_decision' "
        "AND json_extract(detail,'$.request.action')=?",
        (owner.project, owner.session, owner.work, extension(resource)),
    ).fetchone()
    total = int(row[0]) if row and row[0] is not None else 0
    return max(original, total), total


def initial_allowance(
    db: sqlite3.Connection, owner: Ownership, resource: str, configured: int
) -> int:
    """Pin the initial cumulative allowance across Resume and later default changes."""
    db.execute(
        "INSERT OR IGNORE INTO research_observations VALUES (?,?,?,'resource_allowance',?,0,?)",
        (owner.project, owner.session, owner.work, resource, str(configured)),
    )
    return int(
        db.execute(
            "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
            "AND kind='resource_allowance' AND identity=?",
            (owner.project, owner.session, owner.work, resource),
        ).fetchone()[0]
    )


def ceiling(settings: DelegationSettings, action: str) -> int:
    """Only these operator-owned settings may bound a renewable allowance."""
    return int(getattr(settings, EXTENSIONS[action][2]))


def continuation_allowed(db: sqlite3.Connection, owner: Ownership, reason: str) -> bool:
    """A recorded extension can re-admit paused work; hard capacity never can."""
    settings = settings_in(db, owner.project)
    match reason:
        case "resource_time":
            original, used, resource = (
                settings.research_seconds,
                elapsed_seconds(db, owner),
                "elapsed_seconds",
            )
        case "resource_storage":
            original, used, resource = (
                settings.storage_bytes,
                retained_bytes(db, owner),
                "retained_bytes",
            )
        case _:
            return False
    if original is None:
        return False
    total, revision = soft_grant(db, owner, resource, 0)
    return revision > 0 and total > used
