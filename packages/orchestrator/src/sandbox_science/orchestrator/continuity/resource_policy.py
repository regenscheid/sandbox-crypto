# SPDX-License-Identifier: MIT
"""Soft caps and their check-ins (D102), distinct from worker and security limits.

A soft cap on a work's tokens, turns, active time or cost is a check-in, never a stop:
at the cap a child waits for its parent's decision and the root checks itself against its
plan. Each extension is one bounded step; after ``max_idle_extensions`` extensions in a
row without new evidence, a parent must stop or redirect, and the root asks the user.
Retained artifact bytes keep their earlier allowance, and retention capacity stays hard.
"""

from __future__ import annotations

import sqlite3
from typing import Final, Literal

from sandbox_science.orchestrator.delegation.records import DelegationSettings, settings_in

from .records import Ownership
from .resource_usage import elapsed_seconds, retained_bytes

Resource = Literal[
    "tokens",
    "turns",
    "elapsed_seconds",
    "cost",
    "loop",
    "retained_bytes",
    "retention_bytes",
    "retention_revisions",
    "retention_operations",
    "retention_metadata_bytes",
]
"""``cost`` is in hundredths of the gateway's currency; ``loop`` is an early check-in a
loop heuristic asked for, its ``used`` the count that tripped it."""
Action = Literal[
    "extend",
    "redirect",
    "stop",
    "continue",
    "report",
    "pause",
    "extend_turns",
    "extend_time",
    "extend_storage",
    "extend_tokens",
    "extend_cost",
]
"""``extend``, ``redirect`` and ``stop`` are the check-in's answers. The others are kept
for earlier records and callers: ``report`` is ``stop``, and each ``extend_*`` is
``extend`` for its resource."""
EXTENSIONS: Final[dict[str, tuple[Resource, int, str | None]]] = {
    "extend_turns": ("turns", 300, "turn_ceiling"),
    "extend_time": ("elapsed_seconds", 3600, "time_ceiling_seconds"),
    "extend_storage": ("retained_bytes", 1024**3, "storage_ceiling_bytes"),
    "extend_tokens": ("tokens", 20_000_000, "max_token_grant"),
    "extend_cost": ("cost", 2_500, None),
}
"""Each resource's one bounded step and the operator setting that caps its cumulative
total, if any. A cost step is 25.00 in the gateway's currency."""
CAPS: Final[frozenset[str]] = frozenset({"tokens", "turns", "elapsed_seconds", "cost"})
"""The soft caps that check in rather than stop."""
STOPPING: Final[frozenset[str]] = frozenset({"stop", "report"})


def extension(resource: str) -> str | None:
    return next((action for action, (kind, _, _) in EXTENSIONS.items() if kind == resource), None)


GRANTED: Final = (
    "(json_extract({d},'$.resource')={r} OR json_extract({d},'$.request.action')=CASE {r} "
    "WHEN 'turns' THEN 'extend_turns' WHEN 'elapsed_seconds' THEN 'extend_time' "
    "WHEN 'retained_bytes' THEN 'extend_storage' WHEN 'tokens' THEN 'extend_tokens' "
    "WHEN 'cost' THEN 'extend_cost' END)"
)
"""A fixed SQL condition: decision ``{d}``'s detail grants resource ``{r}``. Earlier
decisions name only their action; later ones also name their resource."""


def soft_grant(
    db: sqlite3.Connection, owner: Ownership, resource: str, original: int
) -> tuple[int, int]:
    """The cumulative cap after recorded extensions, and the latest extended total (0
    without one), which names the cap's revision."""
    row = db.execute(
        "SELECT max(json_extract(detail,'$.total')) FROM research_observations "  # noqa: S608
        "WHERE project=? AND session=? AND work=? AND kind='resource_decision' "
        "AND json_extract(detail,'$.request.action') NOT IN ('stop','report','pause') AND "
        + GRANTED.format(d="detail", r="?"),
        (owner.project, owner.session, owner.work, resource, resource),
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


def ceiling(settings: DelegationSettings, action: str) -> int | None:
    """Only these operator-owned settings may bound a soft cap's cumulative total."""
    name = EXTENSIONS[action][2]
    return None if name is None else int(getattr(settings, name))


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


def idle_extensions(db: sqlite3.Connection, owner: Ownership, progress: int) -> int:
    """Extensions in a row, latest first, after which the work recorded no new evidence:
    a redirect or a stop ends the run of them."""
    rows = db.execute(
        "SELECT json_extract(detail,'$.request.action'),"
        "coalesce(json_extract(detail,'$.progress'),0) FROM research_observations "
        "WHERE project=? AND session=? AND work=? AND kind='resource_decision' "
        "ORDER BY observed DESC LIMIT 101",
        (owner.project, owner.session, owner.work),
    ).fetchall()
    count = 0
    for action, recorded in rows:
        if action in ("redirect", "stop", "report", "pause") or int(recorded) < progress:
            break
        count += 1
    return count
