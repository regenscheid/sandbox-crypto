# SPDX-License-Identifier: MIT
"""Leased finding notices, acknowledged only by a durably saved author run."""

# Logical modules share the control store's transaction machinery.
# pyright: reportPrivateUsage=false
from __future__ import annotations

from .store import Json, ReviewStore, clipped, dump


class FindingNotices:
    def __init__(self, store: ReviewStore) -> None:
        self.store = store

    def pending(
        self, project: str, session: str, run: str, seen: set[str], *, max_chars: int = 20000
    ) -> list[Json]:
        if len(seen) >= 128:
            return []
        self.store.scope(project, session)
        now = self.store.clock()
        with self.store.documents._write() as db:
            # The latest state is authoritative. A response made before delivery
            # makes the older open notice unnecessary, without hiding its history.
            rows = db.execute(
                """SELECT n.finding_id,n.revision,f.severity,f.statement,f.review_id
                FROM review_notices n JOIN review_findings f USING(finding_id)
                WHERE n.project=? AND n.session=? AND n.delivered_run IS NULL
                AND n.revision=f.revision AND f.status='open' AND f.category!='math'
                AND (n.claimed_run IS NULL OR n.claim_until<=? OR n.claimed_run=?)
                AND (n.finding_id||'-r'||n.revision) NOT IN (SELECT value FROM json_each(?))
                ORDER BY n.rowid LIMIT ?""",
                (project, session, now, run, dump(sorted(seen)), min(20, 128 - len(seen))),
            ).fetchall()
            values: list[Json] = []
            for finding, revision, severity, statement, review in rows:
                notice = {
                    "notice_id": f"{finding}-r{revision}",
                    "finding": finding,
                    "revision": revision,
                    "severity": severity,
                    "review": review,
                    "statement": clipped(statement, 2048),
                    "detail": "Read review_finding_read for the full finding and evidence.",
                }
                if len(dump([*values, notice])) > max_chars:
                    break
                values.append(notice)
            for n in values:
                db.execute(
                    (
                        "UPDATE review_notices SET claimed_run=?,claim_until=? WHERE "
                        "finding_id=? AND revision=?"
                    ),
                    (run, now + 120, n["finding"], n["revision"]),
                )
        return values

    def renew(self, project: str, session: str, run: str) -> None:
        with self.store.documents._write() as db:
            db.execute(
                (
                    "UPDATE review_notices SET claim_until=? WHERE project=? AND "
                    "session=? AND claimed_run=? AND delivered_run IS NULL"
                ),
                (self.store.clock() + 120, project, session, run),
            )

    def release(self, project: str, session: str, run: str, keep: set[str] | None = None) -> None:
        with self.store.documents._write() as db:
            db.execute(
                (
                    "UPDATE review_notices SET claimed_run=NULL,claim_until=NULL "
                    "WHERE project=? AND session=? AND claimed_run=? AND "
                    "delivered_run IS NULL AND (finding_id||'-r'||revision) NOT "
                    "IN (SELECT value FROM json_each(?))"
                ),
                (project, session, run, dump(sorted(keep or set()))),
            )

    def acknowledge(self, project: str, session: str, run: str, notices: list[str]) -> None:
        with self.store.documents._write() as db:
            db.execute(
                (
                    "UPDATE review_notices SET "
                    "delivered_run=?,claimed_run=NULL,claim_until=NULL WHERE "
                    "project=? AND session=? AND claimed_run=? AND delivered_run "
                    "IS NULL AND (finding_id||'-r'||revision) IN (SELECT value "
                    "FROM json_each(?))"
                ),
                (run, project, session, run, dump(notices)),
            )

    def counts(self, project: str, session: str) -> Json:
        self.store.scope(project, session)
        with self.store.documents._read() as db:
            if not self.store.available(db) or db is None:
                return {}
            rows = db.execute(
                (
                    "SELECT severity,status,count(*) FROM review_findings WHERE "
                    "project=? AND session=? AND category!='math' AND status NOT IN ('confirmed "
                    "fixed','withdrawn') GROUP BY severity,status"
                ),
                (project, session),
            ).fetchall()
        return {f"{severity}:{status}": count for severity, status, count in rows}
