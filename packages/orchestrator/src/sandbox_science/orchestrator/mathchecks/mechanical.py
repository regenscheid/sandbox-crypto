# SPDX-License-Identifier: MIT
"""Private mechanical math lifecycle. Model reviewers cannot invoke this authority."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from pydantic import ValidationError

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore, digest, dump
from sandbox_science.orchestrator.documents.comment_bodies import source_key
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.project_macros import current

Json = dict[str, Any]


class MechanicalFindings:
    def __init__(self, store: ReviewStore) -> None:
        self.store = store
        self.findings = Findings(store)

    def eligible(self, project: str, proof: Json) -> Author | None:
        if proof["author"] is None:
            return None
        author = Author.model_validate(proof["author"])
        if (
            author.kind not in ("agent", "worker")
            or author.role not in ("main", "delegated")
            or author.session_id is None
        ):
            return None
        # A worker with an established session can route to its main agent; an
        # unknown run is never fabricated. Foreign/legacy unprovable scope is excluded.

        try:
            self.store.scope(project, author.session_id)
        except DocumentError:
            return None
        return author

    def _current(self, db: sqlite3.Connection, job: Json) -> bool:
        if not current(db, job["check_id"], job["project"]):
            return False
        return (
            db.execute(
                "SELECT 1 FROM math_current_checks WHERE check_id=? AND source=? AND revision=?",
                (job["check_id"], source_key(job), job["revision"]),
            ).fetchone()
            is not None
        )

    def _previous_owner(self, db: sqlite3.Connection, job: Json, item: Json) -> int | None:
        # The old accepted-result path did not update the owner cache. Use the
        # immutable last available assessment instead, including across intervening
        # unavailable attempts. The global observation quota bounds this SQL scan;
        # only a bounded owner projection crosses into Python, never a body.
        row = db.execute(
            (
                "SELECT CASE WHEN json_type(o.payload,'$.owner')='object' THEN "
                "json_extract(o.payload,'$.owner') END,j.revision FROM math_jobs j "
                "JOIN math_observations o USING(check_id) "
                "LEFT JOIN math_text_jobs x USING(check_id) WHERE j.document IS ? "
                "AND x.subject_type IS ? AND x.subject_id IS ? AND o.block=? AND o.span=? "
                "AND o.status IN ('accepted','rejected') AND j.revision<=? "
                "AND j.rowid<(SELECT rowid FROM math_jobs WHERE check_id=?) "
                "ORDER BY j.rowid DESC LIMIT 1"
            ),
            (
                job["document"],
                *(
                    ("comment", job["comment"]["entry_id"])
                    if "comment" in job
                    else ("response", job["response"]["source"])
                    if "response" in job
                    else ("statement", job["statement"]["entry"])
                    if "statement" in job
                    else (None, None)
                ),
                item["block"],
                item["span"],
                job["revision"],
                job["check_id"],
            ),
        ).fetchone()
        if not row or row[0] is None:
            return None
        try:
            raw: object = json.loads(row[0])
            if not isinstance(raw, dict):
                return None
            proof = cast(Json, raw)
            if set(proof) != {"revision", "author"}:
                return None
            value = proof["revision"]
            if type(value) is not int or type(row[1]) is not int or not 1 <= value <= row[1]:
                return None
            Author.model_validate(proof["author"])
        except (ValueError, TypeError, RecursionError, ValidationError):
            return None
        return value

    def _user_resolved(self, db: sqlite3.Connection, current: Json) -> bool:
        if current["status"] == "dismissed":
            return True
        if current["status"] != "fixed":
            return False
        # Unknown retained decision metadata conservatively preserves resolution.
        valid = (
            "CASE WHEN typeof(payload)='text' AND length(CAST(payload AS BLOB))<=32768 "
            "THEN json_valid(payload) ELSE 0 END"
        )
        row = db.execute(
            (
                f"SELECT CASE WHEN {valid} THEN CASE WHEN "  # noqa: S608 - fixed SQL guards
                "typeof(actor)='text' AND length(CAST(actor AS BLOB))<=128 THEN "
                "NOT (actor='platform:math' AND coalesce(json_extract(payload,'$.action'),'') "
                "IN ('mechanical_accept','mechanical_removal')) ELSE 1 END ELSE 1 END "
                "FROM review_transitions WHERE finding_id=? "
                f"AND CASE WHEN {valid} THEN "
                "coalesce(json_extract(payload,'$.action'),'') "
                "!='mechanical_dependency_refresh' ELSE 1 END ORDER BY revision DESC LIMIT 1"
            ),
            (current["finding_id"],),
        ).fetchone()
        return row is None or bool(row[0])

    def observe(self, db: sqlite3.Connection, job: Json, item: Json) -> None:
        """Apply one span's check result: accept, reflag or raise its finding."""
        # Old results remain immutable observations, but cannot alter current
        # findings, owner exclusion, CAS heads or closing notices.
        if not self._current(db, job):
            return
        row = db.execute(
            (
                "SELECT finding_id,checked_revision,input_sha256,owner_revision FROM "
                "math_finding_state WHERE document=? AND block=? AND span=?"
            ),
            (source_key(job), item["block"], item["span"]),
        ).fetchone()
        if row and row[1] > job["revision"]:
            return
        author = self.eligible(job["project"], item["owner"])
        answer = item["check"]
        configuration = {
            k: answer[k] for k in ("bundle_sha256", "launcher_sha256", "engine", "runtime")
        }
        identity = digest(
            dump(
                [
                    answer["input_sha256"],
                    configuration,
                    item["owner"]["revision"],
                    job["macro_context"]["revision"],
                    job["macro_context"]["sha256"],
                ]
            )
        )
        if row is None:
            if answer["status"] == "rejected" and author is not None:
                self._raise(db, job, item, author, identity)
            return
        found = db.execute(
            "SELECT project,session FROM review_findings WHERE finding_id=?", (row[0],)
        ).fetchone()
        assert found is not None  # noqa: S101 - finding state has a checked foreign key
        current = self.findings.load(db, found[0], found[1], row[0])
        exclusion = (
            None
            if author
            else (
                "Current last-changing block owner is USER, non-author, unknown, or "
                "has no established session. Historical evidence remains unresolved."
            )
        )
        db.execute(
            (
                "UPDATE math_finding_state SET "
                "checked_revision=?,checked_check=?,excluded=? WHERE finding_id=?"
            ),
            (job["revision"], job["check_id"], exclusion, row[0]),
        )
        if answer["status"] == "accepted":
            self._accept(db, job, item, current, identity)
        elif answer["status"] == "rejected" and author is not None and identity != row[2]:
            self._reflag(db, job, item, current, author, identity)

    def _accept(
        self, db: sqlite3.Connection, job: Json, item: Json, current: Json, identity: str
    ) -> None:
        """The exact current formula renders: the finding is fixed (CR-38, CR-41)."""
        db.execute(
            "UPDATE math_finding_state SET input_sha256=?,"
            "owner_revision=coalesce(?,owner_revision) "
            "WHERE finding_id=?",
            (identity, item["owner"]["revision"], current["finding_id"]),
        )
        if current["status"] not in ("fixed", "dismissed"):
            self.findings.transition(
                db,
                current,
                "fixed",
                "platform:math",
                "The exact current formula was accepted by the configured renderer.",
                {
                    "check": job["check_id"],
                    "document_revision": job["revision"],
                    "action": "mechanical_accept",
                },
            )

    def _reflag(
        self,
        db: sqlite3.Connection,
        job: Json,
        item: Json,
        current: Json,
        author: Author,
        identity: str,
    ) -> None:
        """The formula still fails with a new identity: point the finding at this check,
        and reflag it when a newer proved edit of its block made it fail."""
        answer = item["check"]
        anchors = self._anchors(job, item)
        assert author.session_id is not None  # noqa: S101 - eligible author has a session
        Findings.reassess(
            db,
            current["finding_id"],
            session=author.session_id,
            statement="A saved formula was rejected by the configured math renderer: "
            + (answer["error"] or "parse error"),
            anchors=anchors,
            remedy=answer["remedy"],
            check=job["check_id"],
        )
        previous_session = current["session"]
        current["session"] = author.session_id
        previous_owner = self._previous_owner(db, job, item)
        block_edit = previous_owner is not None and item["owner"]["revision"] != previous_owner
        # Two re-flags bring it to its bound: open, and not offered again (CR-30, CR-47).
        status = "open"
        bound = current["at_bound"] or current["reflags"] + int(block_edit) >= 2
        if not block_edit and (current["at_bound"] or self._user_resolved(db, current)):
            status, bound = current["status"], current["at_bound"]
        result = self.findings.transition(
            db,
            current,
            status,
            "platform:math",
            answer["error"] or "Renderer rejected the formula.",
            {
                "check": job["check_id"],
                "anchors": anchors,
                "action": "mechanical_reflag" if block_edit else "mechanical_dependency_refresh",
                "previous_session": previous_session,
                "previous_owner_revision": previous_owner,
                "block_edit_proven": block_edit,
                "owner": item["owner"],
            },
            reflag=block_edit and current["reflags"] < 2,
            columns={"at_bound": True} if bound and not current["at_bound"] else None,
        )
        db.execute(
            "UPDATE math_finding_state SET input_sha256=?,owner_revision=? WHERE finding_id=?",
            (identity, item["owner"]["revision"], current["finding_id"]),
        )
        if status == "open" and not bound:
            self._notice(db, job, author, current["finding_id"], result["revision"])

    def _raise(
        self, db: sqlite3.Connection, job: Json, item: Json, author: Author, identity: str
    ) -> None:
        """A first failing check of this span: a new open math finding for its author."""
        answer = item["check"]
        anchors = self._anchors(job, item)
        assert author.session_id is not None  # noqa: S101 - eligible author has a session
        finding = self.findings.raise_math(
            db,
            job["project"],
            author.session_id,
            statement="A saved formula was rejected by the configured math renderer: "
            + (answer["error"] or "parse error"),
            anchors=anchors,
            remedy=answer["remedy"],
            dedupe=digest(dump([source_key(job), item["block"], item["span"]])),
            check=job["check_id"],
        )
        db.execute(
            "INSERT INTO math_finding_state VALUES(?,?,?,?,?,?,?,?,NULL)",
            (
                finding,
                source_key(job),
                item["block"],
                item["span"],
                job["revision"],
                identity,
                item["owner"]["revision"],
                job["check_id"],
            ),
        )
        current = self.findings.load(db, job["project"], author.session_id, finding)
        result = self.findings.transition(
            db,
            current,
            "open",
            "platform:math",
            answer["error"] or "Renderer rejected the formula.",
            {
                "check": job["check_id"],
                "anchors": anchors,
                "owner": item["owner"],
                "action": "mechanical_rejection",
            },
        )
        self._notice(db, job, author, finding, result["revision"])

    def _anchors(self, job: Json, item: Json) -> list[Json]:
        locator = {"document": job["document"], "revision": job["revision"]}
        if "comment" in job:
            locator = {
                "document": job["document"],
                "revision": job["comment"]["macro_revision"],
                "comment": job["comment"]["entry_id"],
                "body_revision": job["revision"],
            }
        if "response" in job:
            locator = {
                "finding": job["response"]["finding"],
                "response_revision": job["response"]["response_revision"],
                "field": "explanation",
                "body_revision": job["revision"],
            }
        if "statement" in job:
            # A ledger entry's field (LG-25): its statement, at its text revision.
            locator = {
                "entry": job["statement"]["entry"],
                "handle": job["statement"]["handle"],
                "field": "statement",
                "body_revision": job["revision"],
            }
        return [
            {
                "check": job["check_id"],
                "locator": locator,
                "block": item["block"],
                "math": item["span"],
                "sha256": job["source_sha256"],
                "complete": True,
            }
        ]

    def _notice(
        self, db: sqlite3.Connection, job: Json, author: Author, finding: str, revision: int
    ) -> None:
        db.execute(
            (
                "INSERT INTO "
                "math_notices(finding_id,revision,project,session,authored_run) "
                "VALUES(?,?,?,?,?)"
            ),
            (finding, revision, job["project"], author.session_id, author.run_id),
        )

    def removed(self, db: sqlite3.Connection, job: Json, live: set[tuple[str, str]]) -> None:
        """Only a complete current parse establishes removal, even for a USER edit."""
        if not self._current(db, job):
            return
        rows = db.execute(
            (
                "SELECT s.finding_id,s.block,s.span,f.session FROM math_finding_state "
                "s JOIN review_findings f USING(finding_id) WHERE s.document=? AND "
                "s.checked_revision<=? AND s.checked_revision>=? AND f.status NOT IN ('fixed',"
                "'dismissed')"
            ),
            (
                source_key(job),
                job["revision"],
                next(
                    (
                        cast(Json, job[kind])["identity_floor"]
                        for kind in ("response", "comment", "statement")
                        if kind in job
                    ),
                    0,
                ),
            ),
        ).fetchall()
        for finding, block, span, session in rows:
            if (block, span) in live:
                continue
            current = self.findings.load(db, job["project"], session, finding)
            self.findings.transition(
                db,
                current,
                "fixed",
                "platform:math",
                "The formula was removed from the complete current saved source revision.",
                {
                    "check": job["check_id"],
                    "document_revision": job["revision"],
                    "action": "mechanical_removal",
                },
            )
            db.execute(
                (
                    "UPDATE math_finding_state SET "
                    "checked_revision=?,checked_check=?,excluded=NULL WHERE finding_id=?"
                ),
                (job["revision"], job["check_id"], finding),
            )
