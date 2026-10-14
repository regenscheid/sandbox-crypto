# SPDX-License-Identifier: MIT
"""Notices leased to a run: offered, held while the run lasts, and acknowledged only
once a saved run record names them as delivered.

A source keeps its notices in a table of its own with the lease columns
``claimed_run``, ``claim_until`` and ``delivered_run``. ``LeasedNotices`` holds the
rules they share (refactoring plan, B-04): a run is offered at most ``MAX_PER_RUN``
notices in all, in batches that fit a character budget; an offer leases them for
``LEASE_SECONDS``, which the run renews while it lasts; a lease that runs out lets
another run take them; a run that ends releases what it did not deliver; and a saved
record acknowledges what it did. The source keeps its own selection and projection.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .store import ControlStore

Json = dict[str, Any]

LEASE_SECONDS: Final = 120
"""How long an offered notice stays leased to its run without a renewal."""

MAX_PER_RUN: Final = 128
"""Notices of one source a run is offered in all."""


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class NoticeTable:
    """Where a source leases its notices: its table, its project and session columns,
    the SQL naming a notice (``{0}`` stands for a table alias and its dot), and the
    columns that key a notice's row with the fields that hold them in its projection."""

    name: str
    project: str
    session: str
    key: str
    columns: tuple[str, ...]
    fields: tuple[str, ...]

    def keyed(self, alias: str = "") -> str:
        return self.key.format(f"{alias}." if alias else "")


class LeasedNotices:
    """One source's leases on its table."""

    def __init__(
        self, control: ControlStore, table: NoticeTable, clock: Callable[[], float]
    ) -> None:
        self.control, self.table, self.clock = control, table, clock

    def offer(
        self,
        run: str,
        seen: set[str],
        *,
        batch: int,
        max_chars: int,
        select: str,
        parameters: Sequence[object],
        project: Callable[[tuple[Any, ...]], Json],
    ) -> list[Json]:
        """Lease the next notices to ``run``, which has been offered ``seen``.

        ``select`` reads the table as ``n`` and ends its condition with ``{leased}``,
        which keeps the notices no other run holds and that the run was not offered; it
        is then ordered and limited by a last ``?``. ``parameters`` are its own, and
        ``project`` makes a row a notice. The notices are offered in order while they fit
        ``max_chars`` together."""
        room = min(batch, MAX_PER_RUN - len(seen))
        if room <= 0:
            return []
        now = self.clock()
        leased = (
            "n.delivered_run IS NULL AND (n.claimed_run IS NULL OR n.claim_until<=? OR "  # noqa: S608
            f"n.claimed_run=?) AND ({self.table.keyed('n')}) NOT IN "
            "(SELECT value FROM json_each(?))"
        )
        with self.control.write() as tx:
            db = tx.sql
            rows = db.execute(
                select.replace("{leased}", leased),
                (*parameters, now, run, _dump(sorted(seen)), room),
            ).fetchall()
            values: list[Json] = []
            for row in rows:
                notice = project(tuple(row))
                if len(_dump([*values, notice])) > max_chars:
                    break
                values.append(notice)
            if values:
                table = self.table
                db.executemany(
                    f"UPDATE {table.name} SET claimed_run=?,claim_until=? WHERE "  # noqa: S608
                    + " AND ".join(f"{column}=?" for column in table.columns),
                    [
                        (run, now + LEASE_SECONDS, *(n[field] for field in table.fields))
                        for n in values
                    ],
                )
        return values

    def renew(self, project: str, session: str, run: str) -> None:
        """Extend the leases ``run`` holds."""
        table = self.table
        with self.control.write() as tx:
            tx.sql.execute(
                f"UPDATE {table.name} SET claim_until=? WHERE {table.project}=? "  # noqa: S608
                f"AND {table.session}=? AND claimed_run=? AND delivered_run IS NULL",
                (self.clock() + LEASE_SECONDS, project, session, run),
            )

    def release(self, project: str, session: str, run: str, keep: set[str] | None = None) -> None:
        """Give up the leases ``run`` holds, except on the notices in ``keep``, which
        its saved record will acknowledge."""
        table = self.table
        with self.control.write() as tx:
            tx.sql.execute(
                f"UPDATE {table.name} SET claimed_run=NULL,claim_until=NULL "  # noqa: S608
                f"WHERE {table.project}=? AND {table.session}=? AND claimed_run=? "
                f"AND delivered_run IS NULL AND ({table.keyed()}) NOT IN "
                "(SELECT value FROM json_each(?))",
                (project, session, run, _dump(sorted(keep or set()))),
            )

    def acknowledge(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        run: str,
        notices: Sequence[str],
        *,
        also: str = "",
        parameters: Sequence[object] = (),
    ) -> None:
        """Mark the notices ``run`` still holds among ``notices`` as delivered to it, in
        the open change ``db``; ``also`` sets more columns, with its ``parameters``."""
        table = self.table
        db.execute(
            f"UPDATE {table.name} SET {also}delivered_run=?,claimed_run=NULL,"  # noqa: S608
            f"claim_until=NULL WHERE {table.project}=? AND {table.session}=? "
            f"AND claimed_run=? AND delivered_run IS NULL AND ({table.keyed()}) IN "
            "(SELECT value FROM json_each(?))",
            (*parameters, run, project, session, run, _dump(list(notices))),
        )
