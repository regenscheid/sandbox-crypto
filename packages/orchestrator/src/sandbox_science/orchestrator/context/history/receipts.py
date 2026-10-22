# SPDX-License-Identifier: MIT
"""Successful-response input receipts, written atomically with the assistant row."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Set

from sandbox_science.orchestrator.control import ControlStore

from .collections import Members

PAGE = 100


def save_receipts(
    db: sqlite3.Connection,
    scope: tuple[str, str, str],
    message: str,
    offered: Mapping[str, Set[str]],
) -> None:
    """An already-consumed notice retains its first incorporating response."""
    for source, ids in offered.items():
        if isinstance(ids, Members):
            if ids.scope != scope:
                raise ValueError("response receipts require the same run scope")
            db.execute(
                "INSERT OR IGNORE INTO response_receipts SELECT ?,?,?,?,value,? "
                "FROM run_members WHERE project=? AND session=? AND run=? AND collection=?",
                (*scope, source, message, *scope, ids.name),
            )
            continue
        db.executemany(
            "INSERT OR IGNORE INTO response_receipts VALUES (?,?,?,?,?,?)",
            ((*scope, source, notice, message) for notice in ids),
        )


class Receipts:
    def __init__(self, control: ControlStore, project: str, session: str, run: str) -> None:
        self.control, self.scope = control, (project, session, run)

    def sources(self) -> tuple[str, ...]:
        with self.control.read() as tx:
            return tuple(
                row[0]
                for row in tx.sql.execute(
                    "SELECT DISTINCT source FROM response_receipts WHERE project=? AND session=? "
                    "AND run=? ORDER BY source",
                    self.scope,
                )
            )

    def page(self, source: str, *, after: str = "", limit: int = PAGE) -> tuple[str, ...]:
        if not 1 <= limit <= PAGE:
            raise ValueError("receipt pages contain 1 to 100 identities")
        with self.control.read() as tx:
            return tuple(
                row[0]
                for row in tx.sql.execute(
                    "SELECT notice FROM response_receipts WHERE project=? AND session=? AND run=? "
                    "AND source=? AND notice>? ORDER BY notice LIMIT ?",
                    (*self.scope, source, after, limit),
                )
            )

    def contains(self, source: str, notice: str) -> bool:
        with self.control.read() as tx:
            return (
                tx.sql.execute(
                    "SELECT 1 FROM response_receipts WHERE project=? AND session=? AND run=? "
                    "AND source=? AND notice=?",
                    (*self.scope, source, notice),
                ).fetchone()
                is not None
            )

    def ids(self, source: str) -> Iterator[str]:
        after = ""
        while page := self.page(source, after=after):
            yield from page
            after = page[-1]
