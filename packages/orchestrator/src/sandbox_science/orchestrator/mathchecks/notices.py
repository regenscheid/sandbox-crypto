# SPDX-License-Identifier: MIT
"""Closing-only mechanical notices, separate from reviewer interruption thresholds."""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.notices import FindingNotices
from sandbox_science.orchestrator.consistency.store import Json, clipped, dump
from sandbox_science.orchestrator.control.notices import MAX_PER_RUN, NoticeTable

MATH: Final = NoticeTable(
    "math_notices",
    "project",
    "session",
    "{0}finding_id||'-r'||{0}revision",
    ("finding_id", "revision"),
    ("finding", "revision"),
)
"""Where math findings are leased to the author's runs."""


class MathNotices(FindingNotices):
    table: NoticeTable = MATH

    def pending(
        self, project: str, session: str, run: str, seen: set[str], *, max_chars: int = 20000
    ) -> list[Json]:
        if len(seen) >= MAX_PER_RUN:
            return []
        self.store.scope(project, session)

        def notice(row: tuple[Any, ...]) -> Json:
            finding, revision, statement, check = row
            return {
                "notice_id": f"{finding}-r{revision}",
                "finding": finding,
                "revision": revision,
                "severity": "warn",
                "origin": "math",
                "check": check,
                "statement": clipped(statement, 2048),
                "detail": (
                    "Read review_finding_read, then use the ordinary source "
                    "edit tool (document_comment_edit for comments, review_response_edit "
                    "for a response explanation, ledger_change for a ledger statement). A "
                    "mechanical check confirms accepted or removed formulas; do not claim "
                    "a model-confirmed fix."
                ),
            }

        return self.leases.offer(
            run,
            seen,
            batch=20,
            max_chars=max_chars,
            select="""SELECT n.finding_id,n.revision,f.statement,f.math_check_id
              FROM math_notices n JOIN review_findings f USING(finding_id)
              JOIN math_finding_state s USING(finding_id)
              JOIN math_current_checks c ON c.check_id=s.checked_check AND c.source=s.document
              JOIN math_jobs j ON j.check_id=s.checked_check
              JOIN math_observations o ON o.check_id=j.check_id AND o.block=s.block AND
              o.span=s.span
              WHERE n.project=? AND n.session=?
              AND n.session=f.session AND n.revision=f.revision AND f.status='open' AND
              s.excluded IS NULL
              AND s.checked_revision=c.revision AND o.status='rejected'
              AND j.macro_held=0 AND j.macro_revision=coalesce((SELECT h.revision FROM
              project_macro_heads h WHERE h.project=f.project),0)
              AND {leased} ORDER BY n.rowid LIMIT ?""",
            parameters=(project, session),
            project=notice,
        )

    def acknowledge(
        self, project: str, session: str, run: str, notices: list[str], *, finalize: bool = True
    ) -> None:
        """Finalize only after the represented transcript is durably saved."""
        with self.store.documents.control.write() as tx:
            db = tx.sql
            findings = Findings(self.store)
            for finding, revision in (
                self._eligible(db, project, session, run, notices, True) if finalize else []
            ):
                current = findings.load(db, project, session, finding)
                findings.transition(
                    db,
                    current,
                    "unaddressed",
                    "platform:math",
                    "The durably saved closing response left this mechanically rejected "
                    "formula unresolved.",
                    {"action": "ignored_math_notice", "notice_revision": revision, "run": run},
                    reflag=False,
                )
            self.leases.acknowledge(db, project, session, run, notices)

    def bound(self, project: str, session: str, run: str, notices: set[str]) -> None:
        """Stage an unresolved closing opportunity without consuming its notice.

        A failed transcript save must leave the exact open finding deliverable.
        ACK rechecks all evidence and applies the decision after durable save.
        """
        with self.store.documents.control.write() as tx:
            db = tx.sql
            for finding, revision in self._eligible(db, project, session, run, notices, False):
                db.execute(
                    "INSERT INTO math_ignored VALUES(?,?,?) ON CONFLICT(finding_id,revision) "
                    "DO UPDATE SET run=excluded.run",
                    (finding, revision, run),
                )

    def _eligible(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        run: str,
        notices: list[str] | set[str],
        staged: bool,
    ) -> list[tuple[str, int]]:
        return db.execute(
            """SELECT n.finding_id,n.revision FROM math_notices n
              JOIN review_findings f USING(finding_id) JOIN math_finding_state s USING(finding_id)
              JOIN math_current_checks c ON c.check_id=s.checked_check AND c.source=s.document
              JOIN math_jobs j ON j.check_id=s.checked_check
              JOIN math_observations o ON o.check_id=j.check_id AND o.block=s.block AND
              o.span=s.span
              WHERE n.project=? AND n.session=? AND n.claimed_run=?
              AND n.delivered_run IS NULL
              AND n.session=f.session AND n.revision=f.revision AND f.status='open' AND
              s.excluded IS NULL
              AND s.checked_revision=c.revision AND o.status='rejected'
              AND j.macro_held=0 AND j.macro_revision=coalesce((SELECT h.revision FROM
              project_macro_heads h WHERE h.project=f.project),0)
               AND (n.finding_id||'-r'||n.revision) IN (SELECT value FROM
              json_each(?))
              AND (?=0 OR EXISTS(SELECT 1 FROM math_ignored i WHERE i.finding_id=n.finding_id AND
              i.revision=n.revision AND i.run=?))
              LIMIT 128""",
            (project, session, run, dump(sorted(notices)), int(staged), run),
        ).fetchall()

    def counts(self, project: str, session: str) -> Json:
        self.store.scope(project, session)
        if self.store.documents.control.missing:
            return {}
        with self.store.documents.control.read() as tx:
            db = tx.sql
            rows = db.execute(
                "SELECT f.status,s.excluded IS NOT NULL,count(*) FROM review_findings f "
                "JOIN math_finding_state s USING(finding_id) WHERE f.project=? AND f.session=? "
                "AND f.status NOT IN ('confirmed fixed','withdrawn') "
                "GROUP BY f.status,s.excluded IS NOT NULL",
                (project, session),
            ).fetchall()
        return {
            status + (":excluded" if excluded else ""): count for status, excluded, count in rows
        }
