# SPDX-License-Identifier: MIT
"""Leased finding notices, acknowledged only by a durably saved author run."""

from __future__ import annotations

from typing import Any, Final

from sandbox_science.orchestrator.control.notices import (
    MAX_PER_RUN,
    LeasedNotices,
    NoticeTable,
)

from .store import Json, ReviewStore, clipped

FINDINGS: Final = NoticeTable(
    "review_notices",
    "project",
    "session",
    "{0}finding_id||'-r'||{0}revision",
    ("finding_id", "revision"),
    ("finding", "revision"),
)
"""Where review findings are leased to the author's runs."""


class FindingNotices:
    table: NoticeTable = FINDINGS

    def __init__(self, store: ReviewStore) -> None:
        self.store = store
        self.leases = LeasedNotices(store.documents.control, self.table, store.clock)

    def pending(
        self, project: str, session: str, run: str, seen: set[str], *, max_chars: int = 20000
    ) -> list[Json]:
        if len(seen) >= MAX_PER_RUN:
            return []
        self.store.scope(project, session)

        def notice(row: tuple[Any, ...]) -> Json:
            finding, revision, severity, statement, review = row
            return {
                "notice_id": f"{finding}-r{revision}",
                "finding": finding,
                "revision": revision,
                "severity": severity,
                "review": review,
                "statement": clipped(statement, 2048),
                "detail": "Read review_finding_read for the full finding and evidence.",
            }

        # The latest state is authoritative. A response made before delivery
        # makes the older open notice unnecessary, without hiding its history.
        return self.leases.offer(
            run,
            seen,
            batch=20,
            max_chars=max_chars,
            select="""SELECT n.finding_id,n.revision,f.severity,f.statement,f.review_id
                FROM review_notices n JOIN review_findings f USING(finding_id)
                WHERE n.project=? AND n.session=?
                AND n.revision=f.revision AND f.status='open' AND f.category!='math'
                AND {leased} ORDER BY n.rowid LIMIT ?""",
            parameters=(project, session),
            project=notice,
        )

    def renew(self, project: str, session: str, run: str) -> None:
        self.leases.renew(project, session, run)

    def release(self, project: str, session: str, run: str, keep: set[str] | None = None) -> None:
        self.leases.release(project, session, run, keep)

    def acknowledge(self, project: str, session: str, run: str, notices: list[str]) -> None:
        with self.store.documents.control.write() as tx:
            self.leases.acknowledge(tx.sql, project, session, run, notices)

    def counts(self, project: str, session: str) -> Json:
        self.store.scope(project, session)
        if self.store.documents.control.missing:
            return {}
        with self.store.documents.control.read() as tx:
            db = tx.sql
            rows = db.execute(
                (
                    "SELECT severity,status,count(*) FROM review_findings WHERE "
                    "project=? AND session=? AND category!='math' AND status NOT IN ('confirmed "
                    "fixed','withdrawn') GROUP BY severity,status"
                ),
                (project, session),
            ).fetchall()
        return {f"{severity}:{status}": count for severity, status, count in rows}
