# SPDX-License-Identifier: MIT
"""Exact auxiliary charge links, committed with each legacy consumer's own write."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from sandbox_science.contracts import Contract
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.control.errors import CONFLICT

from .records import CURRENT

Table = Literal[
    "review_calls", "job_watcher_calls", "skill_embedding_calls", "skill_metadata_calls"
]
KINDS = {
    "review_calls": "reviewer",
    "job_watcher_calls": "watcher",
    "skill_embedding_calls": "embedding",
    "skill_metadata_calls": "skills",
}
KIND = "model_materialization"


class Link(Contract):
    run: str = Field(min_length=1, max_length=200)
    table: Table
    row: int = Field(ge=1)


def existing(db: sqlite3.Connection, table: Table, identity: str | None) -> int | None:
    """Resolve only the captured attempt's known observation, never a similar charge."""
    if identity is None:
        return None
    owner = CURRENT.get()
    if (
        owner is None
        or db.execute(
            "SELECT 1 FROM research_model_calls WHERE project=? AND session=? AND run=? "
            "AND identity=? AND kind=? AND usage IS NOT NULL",
            (owner.project, owner.session, owner.run, identity, KINDS[table]),
        ).fetchone()
        is None
    ):
        raise ControlError(CONFLICT, "paid usage has no observed call in this attempt")
    row = db.execute(
        "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
        "AND kind=? AND identity=?",
        (owner.project, owner.session, owner.work, KIND, identity),
    ).fetchone()
    if row is None:
        return None
    link = Link.model_validate_json(row[0])
    if link.run != owner.run or link.table != table:
        raise ControlError(CONFLICT, "paid usage already belongs to another legacy record")
    return link.row


def link(db: sqlite3.Connection, table: Table, row: int, identity: str | None) -> None:
    if identity is None:
        return
    prior = existing(db, table, identity)
    if prior is not None:
        if prior != row:
            raise ControlError(CONFLICT, "paid usage cannot materialize twice")
        return
    owner = CURRENT.get()
    if owner is None:
        raise ControlError(CONFLICT, "paid usage requires its attempt authority")
    found = db.execute(f"SELECT project FROM {table} WHERE rowid=?", (row,)).fetchone()  # noqa: S608
    if found is None or found[0] != owner.project:
        raise ControlError(CONFLICT, "paid legacy record belongs to another project")
    db.execute(
        "INSERT INTO research_observations VALUES (?,?,?,?,?,?,?)",
        (
            owner.project,
            owner.session,
            owner.work,
            KIND,
            identity,
            datetime.now(UTC).timestamp(),
            Link(run=owner.run, table=table, row=row).model_dump_json(),
        ),
    )


def fallback(table: Table, alias: str) -> str:
    """A fixed SQL fragment; suppression is independent of the requested time window."""
    return (
        "NOT EXISTS (SELECT 1 FROM research_observations paid "  # noqa: S608 - fixed table/alias
        "JOIN research_model_calls canonical ON canonical.project=paid.project "
        "AND canonical.session=paid.session "
        "AND canonical.run=json_extract(paid.detail,'$.run') AND canonical.identity=paid.identity "
        f"WHERE paid.project={alias}.project AND paid.kind='model_materialization' "
        f"AND json_extract(paid.detail,'$.table')='{table}' "
        f"AND json_extract(paid.detail,'$.row')={alias}.rowid AND canonical.usage IS NOT NULL)"
    )
