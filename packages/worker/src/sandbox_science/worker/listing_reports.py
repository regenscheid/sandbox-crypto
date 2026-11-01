# SPDX-License-Identifier: MIT
"""The latest listing the agent runtime's client reported of each project's server (D98).

The worker takes the runtime's reports (``POST /v1/mcp-listings``) and keeps the latest of
each project's server, with the time it took it and whether it drifted from the project's
pin, for the workspace's MCP services view (``GET /v1/mcp-listings``). They are kept in the
worker's store, so the view survives a restart: one row per project and server, the newest
report replacing the last, and at most ``MAX_KEPT`` rows, the least recently reported
forgotten. The drift records themselves are the policy middleware's event store's.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from pydantic import ValidationError

from sandbox_science.contracts.classifications import ListingReport

MAX_KEPT: Final = 4096
"""Projects' servers whose latest listing is kept, the least recently reported forgotten."""
MAX_REPORT_BYTES: Final = 2 * 1024 * 1024
"""A kept report's JSON: 2,048 tools of a 256-character name, a digest and a hint fit."""


def create_schema(db: sqlite3.Connection) -> None:
    """Append-only worker migration: the kept listing reports."""
    db.execute(
        f"""CREATE TABLE mcp_listing_reports (
    project_id TEXT NOT NULL CHECK (length(project_id) BETWEEN 1 AND 128),
    server TEXT NOT NULL CHECK (length(server) BETWEEN 1 AND 128),
    reported_at TEXT NOT NULL CHECK (length(reported_at) BETWEEN 1 AND 64),
    drifted INTEGER NOT NULL CHECK (drifted IN (0, 1)),
    report TEXT NOT NULL CHECK (length(CAST(report AS BLOB)) BETWEEN 2
        AND {MAX_REPORT_BYTES}),
    PRIMARY KEY (project_id, server)
) STRICT"""
    )
    db.execute("CREATE INDEX mcp_listing_reports_age ON mcp_listing_reports(reported_at)")


@dataclass(frozen=True, slots=True)
class KeptListing:
    """A kept report: what the runtime reported, when the worker took it, and whether it
    drifted from the project's pin then."""

    report: ListingReport
    reported_at: datetime
    drifted: bool


class ListingReports:
    """The ``mcp_listing_reports`` table of the worker's store."""

    def __init__(self, db: sqlite3.Connection, *, keep: int = MAX_KEPT) -> None:
        if keep < 1:
            raise ValueError("keep at least one listing")
        self.db, self.keep = db, keep
        self._lock = threading.Lock()

    def put(self, report: ListingReport, drifted: bool, at: datetime) -> None:
        """Keep ``report``, taken ``at`` (an aware time), as its project's server's latest,
        and forget the least recently reported past ``keep``. Blocking."""
        if at.tzinfo is None:
            raise ValueError("a report's time is an aware time")
        now = at.astimezone(UTC).isoformat()
        # Two statements, each its own: the prune only ever forgets, so a stop between them
        # leaves one row too many until the next report.
        with self._lock:
            self.db.execute(
                "INSERT INTO mcp_listing_reports VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(project_id, server) DO UPDATE SET"
                " reported_at=excluded.reported_at, drifted=excluded.drifted,"
                " report=excluded.report",
                (report.project_id, report.server, now, int(drifted), report.model_dump_json()),
            )
            self.db.execute(
                "DELETE FROM mcp_listing_reports WHERE rowid IN (SELECT rowid FROM"
                " mcp_listing_reports ORDER BY reported_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (self.keep,),
            )

    def of(self, project: str) -> Iterator[KeptListing]:
        """The project's kept reports, by server. A row that no longer reads as a report (a
        contract that changed since) is skipped: the next report of its server replaces
        it. Blocking."""
        with self._lock:
            rows = self.db.execute(
                "SELECT reported_at, drifted, report FROM mcp_listing_reports"
                " WHERE project_id = ? ORDER BY server",
                (project,),
            ).fetchall()
        for reported_at, drifted, text in rows:
            try:
                report = ListingReport.model_validate_json(str(text))
                at = datetime.fromisoformat(str(reported_at))
            except (ValidationError, ValueError):
                continue
            if at.tzinfo is None or report.project_id != project:
                continue
            yield KeptListing(report, at, bool(drifted))
