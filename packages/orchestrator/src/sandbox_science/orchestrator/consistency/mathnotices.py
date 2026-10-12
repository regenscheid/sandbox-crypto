# SPDX-License-Identifier: MIT
"""Closing-only mechanical notices, separate from reviewer interruption thresholds."""

# Shared transaction and append-only finding machinery.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3

from .findings import Findings
from .notices import FindingNotices
from .store import Json, clipped, dump


class MathNotices(FindingNotices):
    def pending(
        self, project: str, session: str, run: str, seen: set[str], *, max_chars: int = 20000
    ) -> list[Json]:
        if len(seen) >= 128:
            return []
        self.store.scope(project, session)
        now = self.store.clock()
        with self.store.documents._write() as db:
            rows = db.execute(
                """SELECT n.finding_id,n.revision,f.statement,f.math_check_id
              FROM math_notices n JOIN review_findings f USING(finding_id)
              JOIN math_finding_state s USING(finding_id)
              JOIN math_current_checks c ON c.check_id=s.checked_check AND c.source=s.document
              JOIN math_jobs j ON j.check_id=s.checked_check
              JOIN math_observations o ON o.check_id=j.check_id AND o.block=s.block AND
              o.span=s.span
              WHERE n.project=? AND n.session=? AND n.delivered_run IS NULL
              AND n.session=f.session AND n.revision=f.revision AND f.status='open' AND
              s.excluded IS NULL
              AND s.checked_revision=c.revision AND o.status='rejected'
              AND EXISTS(SELECT 1 FROM math_macro_pins p LEFT JOIN project_macro_heads h
              ON h.project=p.project WHERE p.check_id=j.check_id AND p.project=f.project
              AND p.revision=coalesce(h.revision,0))
              AND EXISTS(SELECT 1 FROM math_macro_contexts mc WHERE mc.check_id=j.check_id
              AND mc.held=0)

              AND (n.claimed_run IS NULL OR n.claim_until<=? OR n.claimed_run=?)
              AND (n.finding_id||'-r'||n.revision) NOT IN (SELECT value FROM json_each(?))
              ORDER BY n.rowid LIMIT ?""",
                (project, session, now, run, dump(sorted(seen)), min(20, 128 - len(seen))),
            ).fetchall()
            values: list[Json] = []
            for finding, revision, statement, check in rows:
                notice = {
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
                        "for a response explanation). A "
                        "mechanical check confirms accepted or removed formulas; do not claim "
                        "a model-confirmed fix."
                    ),
                }
                if len(dump([*values, notice])) > max_chars:
                    break
                values.append(notice)
            for notice in values:
                db.execute(
                    (
                        "UPDATE math_notices SET claimed_run=?,claim_until=? WHERE "
                        "finding_id=? AND revision=?"
                    ),
                    (run, now + 120, notice["finding"], notice["revision"]),
                )
        return values

    def renew(self, project: str, session: str, run: str) -> None:
        with self.store.documents._write() as db:
            db.execute(
                (
                    "UPDATE math_notices SET claim_until=? WHERE project=? AND session=? "
                    "AND claimed_run=? AND delivered_run IS NULL"
                ),
                (self.store.clock() + 120, project, session, run),
            )

    def release(self, project: str, session: str, run: str, keep: set[str] | None = None) -> None:
        with self.store.documents._write() as db:
            db.execute(
                (
                    "UPDATE math_notices SET claimed_run=NULL,claim_until=NULL WHERE "
                    "project=? AND session=? AND claimed_run=? AND delivered_run IS NULL "
                    "AND (finding_id||'-r'||revision) NOT IN (SELECT value FROM "
                    "json_each(?))"
                ),
                (project, session, run, dump(sorted(keep or set()))),
            )

    def acknowledge(
        self, project: str, session: str, run: str, notices: list[str], *, finalize: bool = True
    ) -> None:
        """Finalize only after the represented transcript is durably saved."""
        with self.store.documents._write() as db:
            findings = Findings(self.store)
            for finding, revision in (
                self._eligible(db, project, session, run, notices, True) if finalize else []
            ):
                current = findings._get(db, project, session, finding)
                findings._transition(
                    db,
                    current,
                    "unaddressed",
                    "platform:math",
                    "The durably saved closing response left this mechanically rejected "
                    "formula unresolved.",
                    {"action": "ignored_math_notice", "notice_revision": revision, "run": run},
                    reflag=False,
                )
            db.execute(
                (
                    "UPDATE math_notices SET "
                    "delivered_run=?,claimed_run=NULL,claim_until=NULL WHERE project=? AND "
                    "session=? AND claimed_run=? AND delivered_run IS NULL AND "
                    "(finding_id||'-r'||revision) IN (SELECT value FROM json_each(?))"
                ),
                (run, project, session, run, dump(notices)),
            )

    def bound(self, project: str, session: str, run: str, notices: set[str]) -> None:
        """Stage an unresolved closing opportunity without consuming its notice.

        A failed transcript save must leave the exact open finding deliverable.
        ACK rechecks all evidence and applies the decision after durable save.
        """
        with self.store.documents._write() as db:
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
              AND EXISTS(SELECT 1 FROM math_macro_pins p LEFT JOIN project_macro_heads h
              ON h.project=p.project WHERE p.check_id=j.check_id AND p.project=f.project
              AND p.revision=coalesce(h.revision,0))
              AND EXISTS(SELECT 1 FROM math_macro_contexts mc WHERE mc.check_id=j.check_id
              AND mc.held=0)
               AND (n.finding_id||'-r'||n.revision) IN (SELECT value FROM
              json_each(?))
              AND (?=0 OR EXISTS(SELECT 1 FROM math_ignored i WHERE i.finding_id=n.finding_id AND
              i.revision=n.revision AND i.run=?))
              LIMIT 128""",
            (project, session, run, dump(sorted(notices)), int(staged), run),
        ).fetchall()

    def counts(self, project: str, session: str) -> Json:
        self.store.scope(project, session)
        with self.store.documents._read() as db:
            if db is None:
                return {}
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
