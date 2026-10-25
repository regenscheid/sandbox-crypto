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
        self,
        project: str,
        session: str,
        run: str,
        seen: set[str],
        *,
        max_chars: int = 20000,
        raised_for: str | None = None,
    ) -> list[Json]:
        """The notices to lease to ``run``: open findings not at their re-flag bound.
        With ``raised_for``, a run's ID, only those the reviews of that run's work raised
        or decided: the others reach it as counts (the owner, 2026-10-16; CR-16, CR-21 as
        amended). A dismissed, stale or bounded finding is not injected (CR-43, CR-44)."""
        if len(seen) >= MAX_PER_RUN:
            return []
        self.store.scope(project, session)

        def notice(row: tuple[Any, ...]) -> Json:
            finding, revision, severity, statement, review, status = row
            return {
                "notice_id": f"{finding}-r{revision}",
                "finding": finding,
                "revision": revision,
                "severity": severity,
                "review": review,
                "status": status,
                "statement": clipped(statement, 2048),
                "detail": "Read review_finding_read for the full finding and evidence.",
            }

        # The latest state is authoritative. A response made before delivery
        # makes the older open notice unnecessary, without hiding its history.
        # A run is offered only the open findings reviews of its own work raised or
        # decided, the rest reaching it as counts (the owner, 2026-10-16; CR-16, CR-21 as
        # amended).
        return self.leases.offer(
            run,
            seen,
            batch=20,
            max_chars=max_chars,
            select="""SELECT n.finding_id,n.revision,f.severity,f.statement,f.review_id,
                f.status
                FROM review_notices n JOIN review_findings f USING(finding_id)
                JOIN review_transitions t ON t.finding_id=n.finding_id
                AND t.revision=n.revision
                WHERE n.project=? AND n.session=?
                AND n.revision=f.revision AND f.status='open' AND f.at_bound=0
                AND f.category NOT IN ('math','handle_citation')
                AND (? IS NULL OR EXISTS(SELECT 1 FROM
                review_runs r WHERE t.actor='review:'||r.review_id AND r.author_run=?))
                AND {leased} ORDER BY n.rowid LIMIT ?""",
            parameters=(project, session, raised_for, raised_for),
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
        """The session's open review findings by severity (CR-16, CR-21, CR-47), keyed
        ``severity:open``; math findings and suggestions are counted elsewhere or not."""
        self.store.scope(project, session)
        if self.store.documents.control.missing:
            return {}
        with self.store.documents.control.read() as tx:
            db = tx.sql
            rows = db.execute(
                (
                    "SELECT severity,status,count(*) FROM review_findings WHERE "
                    "project=? AND session=? AND category NOT IN ('math','handle_citation') "
                    "AND status='open' GROUP BY severity,status"
                ),
                (project, session),
            ).fetchall()
        return {f"{severity}:{status}": count for severity, status, count in rows}
