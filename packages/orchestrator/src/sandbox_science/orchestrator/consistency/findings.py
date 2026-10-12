# SPDX-License-Identifier: MIT
"""Finding mutation grants and append-only, expected-revision transitions."""

# Logical modules share control-store transactions and immutable evidence internals.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from pydantic import ValidationError

from sandbox_science.contracts import new_id
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author

from .models import ConfirmationInput, EvidenceAnchor, FindingInput, FindingResponse, ReviewConfig
from .store import ReviewStore, digest, dump, identifier, text

# Heterogeneous persisted JSON is validated at the mutation boundary.
Json = dict[str, Any]
RESOLVED = ("confirmed fixed", "withdrawn")
FIELDS = (
    "finding_id",
    "project",
    "session",
    "review_id",
    "severity",
    "category",
    "statement",
    "anchors",
    "remedy",
    "status",
    "revision",
    "reflags",
    "dedupe",
    "supersedes",
    "created",
)


class Findings:
    """Reviewers raise findings; main agents respond; confirmation/user grants resolve."""

    def __init__(self, store: ReviewStore) -> None:
        self.store = store

    def _get(self, db: sqlite3.Connection, project: str, session: str, finding: str) -> Json:
        identifier(finding, "finding")
        row = db.execute(
            "SELECT * FROM review_findings WHERE project=? AND session=? AND finding_id=?",
            (project, session, finding),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "no such scoped finding")
        result: Json = dict(zip(FIELDS, row, strict=True))
        result["anchors"] = json.loads(result["anchors"])
        return result

    def get(self, project: str, session: str, finding: str, *, before: int | None = None) -> Json:
        self.store.scope(project, session)
        if before is not None and (type(before) is not int or before < 1 or before > 999999999):
            raise DocumentError(INVALID_REQUEST, "invalid finding history cursor")
        with self.store.documents._read() as db:
            if not self.store.available(db) or db is None:
                raise DocumentError(NOT_FOUND, "no such finding")
            result = self._get(db, project, session, finding)
            rows = list(
                db.execute(
                    (
                        "SELECT revision,status,actor,reason,payload,created FROM "
                        "review_transitions WHERE finding_id=? AND revision<? ORDER BY "
                        "revision DESC LIMIT 21"
                    ),
                    (finding, before or result["revision"] + 1),
                )
            )
        selected: list[tuple[Any, ...]] = []
        size = len(dump(result).encode())
        for row in rows[:20]:
            added = sum(len(str(v).encode()) for v in row) + 256
            if selected and size + added > 1024 * 1024:
                break
            selected.append(tuple(row))
            size += added
        result["history"] = [
            dict(
                zip(
                    ("revision", "status", "actor", "reason", "payload", "created"),
                    (*r[:4], json.loads(r[4]), r[5]),
                    strict=True,
                )
            )
            for r in reversed(selected)
        ]
        result["history_before"] = selected[-1][0] if len(rows) > len(selected) else None
        return result

    def listing(
        self, project: str, session: str, *, after: str | None = None, unresolved: bool = False
    ) -> Json:
        self.store.scope(project, session)
        with self.store.documents._read() as db:
            if not self.store.available(db) or db is None:
                return {"findings": [], "next_after": None}
            cursor = db.execute("SELECT coalesce(max(rowid),0)+1 FROM review_findings").fetchone()[
                0
            ]
            if after is not None:
                identifier(after, "finding")
                row = db.execute(
                    (
                        "SELECT rowid FROM review_findings WHERE project=? AND "
                        "session=? AND finding_id=?"
                    ),
                    (project, session, after),
                ).fetchone()
                if row is None:
                    raise DocumentError(NOT_FOUND, "no such finding cursor")
                cursor = row[0]
            ids = [
                r[0]
                for r in db.execute(
                    (
                        "SELECT finding_id FROM review_findings WHERE project=? AND "
                        "session=? AND rowid<? AND (?=0 OR status NOT IN ('confirmed "
                        "fixed','withdrawn')) ORDER BY rowid DESC LIMIT 21"
                    ),
                    (project, session, cursor, int(unresolved)),
                )
            ]
            # Listing excludes transition bodies; full bounded history has its own cursor.
            values: list[Json] = []
            size = 0
            for finding in ids[:20]:
                value = self._get(db, project, session, finding)
                added = len(dump(value).encode()) + 32
                if values and size + added > 1024 * 1024:
                    break
                values.append(value)
                size += added
        return {
            "findings": values,
            "next_after": values[-1]["finding_id"] if len(ids) > len(values) else None,
        }

    def validate_anchor(
        self, project: str, session: str, review: str, anchor: EvidenceAnchor
    ) -> Json:
        item = self.store.evidence(project, session, review, anchor.evidence)
        locator = item["locator"]
        if anchor.block is not None or anchor.math is not None:
            if item["kind"] != "document" or anchor.block is None:
                raise DocumentError(
                    INVALID_REQUEST, "a block/math anchor needs a pinned document block"
                )
            mapped = self.store.documents.mapped(project, locator["document"], locator["revision"])
            index = mapped.index(anchor.block)
            if index is None:
                raise DocumentError(INVALID_REQUEST, "block is outside the evidence revision")
            if anchor.math is not None:
                span = mapped.span_index(anchor.math)
                if span is None or mapped.parsed.spans[span].block != index:
                    raise DocumentError(INVALID_REQUEST, "math is outside the evidence block")
        if (
            anchor.stream is not None
            or anchor.first_line is not None
            or anchor.last_line is not None
        ):
            if (
                item["kind"] != "execution"
                or anchor.stream is None
                or anchor.first_line is None
                or anchor.last_line is None
            ):
                raise DocumentError(
                    INVALID_REQUEST, "a log anchor needs a stream and both line numbers"
                )
            if item["payload"].get(anchor.stream + "_truncated"):
                raise DocumentError(
                    INVALID_REQUEST, "line numbers are unavailable for clipped output"
                )
            output = item["payload"].get(anchor.stream, "")
            lines = len(output.splitlines()) if isinstance(output, str) else 0
            if not anchor.first_line <= anchor.last_line <= lines:
                raise DocumentError(
                    INVALID_REQUEST, "log range is outside the pinned output projection"
                )
            if anchor.block is not None:
                raise DocumentError(INVALID_REQUEST, "a log anchor cannot also name a block")
        return {
            **anchor.model_dump(mode="json", exclude_none=True),
            "review": review,
            "locator": locator,
            "sha256": item["sha256"],
            "complete": item["complete"],
        }

    def _replay(
        self, db: sqlite3.Connection, project: str, actor: str, operation: str, fingerprint: str
    ) -> Json | None:
        row = db.execute(
            (
                "SELECT fingerprint,result FROM review_operations WHERE project=? "
                "AND actor=? AND operation_id=?"
            ),
            (project, actor, operation),
        ).fetchone()
        if row:
            if row[0] != fingerprint:
                raise DocumentError(CONFLICT, "operation ID already names another finding mutation")
            return {**json.loads(row[1]), "replayed": True}
        return None

    def _operation(
        self,
        db: sqlite3.Connection,
        project: str,
        actor: str,
        operation: str,
        fingerprint: str,
        result: Json,
    ) -> None:
        db.execute(
            "INSERT INTO review_operations VALUES(?,?,?,?,?)",
            (project, actor, operation, fingerprint, dump(result)),
        )

    def _transition(
        self,
        db: sqlite3.Connection,
        found: Json,
        status: str,
        actor: str,
        reason: str,
        payload: Json,
        *,
        reflag: bool = False,
    ) -> Json:
        revision = found["revision"] + 1
        flags = found["reflags"] + int(reflag)
        db.execute(
            "UPDATE review_findings SET status=?,revision=?,reflags=? WHERE finding_id=?",
            (status, revision, flags, found["finding_id"]),
        )
        db.execute(
            (
                "INSERT INTO "
                "review_transitions(finding_id,revision,status,actor,reason,payload,created)"
                " VALUES(?,?,?,?,?,?,?)"
            ),
            (
                found["finding_id"],
                revision,
                status,
                actor,
                reason,
                dump(payload),
                self.store.clock(),
            ),
        )
        self.store.event(
            db,
            found["project"],
            found["session"],
            "finding.transitioned",
            {
                "finding": found["finding_id"],
                "status": status,
                "revision": revision,
                "actor": actor,
            },
        )
        configuration = db.execute(
            "SELECT payload FROM review_config WHERE project=? ORDER BY number DESC LIMIT 1",
            (found["project"],),
        ).fetchone()
        threshold = (
            ReviewConfig.model_validate_json(configuration[0]) if configuration else ReviewConfig()
        ).interrupt_severity
        ranks = {"fail": 0, "warn": 1, "note": 2}
        if status == "open" and ranks[found["severity"]] <= ranks[threshold]:
            db.execute(
                (
                    "INSERT OR IGNORE INTO "
                    "review_notices(finding_id,revision,project,session) "
                    "VALUES(?,?,?,?)"
                ),
                (found["finding_id"], revision, found["project"], found["session"]),
            )
        return {
            "finding_id": found["finding_id"],
            "status": status,
            "revision": revision,
            "reflags": flags,
        }

    def create(self, project: str, session: str, review: str, owner: str, raw: object) -> Json:
        try:
            value = FindingInput.model_validate(raw)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid finding") from None
        text(value.statement)
        text(value.remedy) if value.remedy else None
        anchors = [self.validate_anchor(project, session, review, a) for a in value.anchors]
        # Semantic identity excludes snapshot-local IDs, so the same pinned revision
        # reviewed in another window re-flags rather than duplicating a finding.
        identities = [
            {k: v for k, v in a.items() if k not in ("evidence", "review", "sha256", "complete")}
            for a in anchors
        ]
        dedupe = digest(dump([value.category, sorted(identities, key=dump)]))
        fingerprint = digest(dump(value.model_dump(mode="json")))
        actor = f"review:{review}"
        with self.store.documents._write() as db:
            replay = self._replay(db, project, actor, value.operation_id, fingerprint)
            if replay:
                return replay
            run = self.store._run(db, project, session, review)
            self.store.owner(run, owner)
            config = ReviewConfig.model_validate_json(run["config"])
            count = db.execute(
                "SELECT count(*) FROM review_records WHERE review_id=? AND kind='finding'",
                (review,),
            ).fetchone()[0]
            if count >= config.max_findings:
                db.execute("UPDATE review_runs SET cap_reached=1 WHERE review_id=?", (review,))
                return {"outcome": "finding_cap", "cap_reached": True}
            if value.supersedes is not None:
                self._get(db, project, session, value.supersedes)
            existing = db.execute(
                (
                    "SELECT finding_id FROM review_findings WHERE project=? AND "
                    "session=? AND dedupe=? AND status NOT IN ('confirmed "
                    "fixed','withdrawn','superseded') ORDER BY rowid LIMIT 1"
                ),
                (project, session, dedupe),
            ).fetchone()
            incomplete = value.assessment == "missing_evidence" or (
                value.category == "missing_action" and not run["coverage_complete"]
            )
            status = "inconclusive" if incomplete else "open"
            if existing:
                found = self._get(db, project, session, existing[0])
                if found["reflags"] + 1 >= config.max_reflags:
                    status = "unaddressed"
                result = self._transition(
                    db,
                    found,
                    status,
                    actor,
                    value.statement,
                    {"review": review, "anchors": anchors},
                    reflag=True,
                )
            else:
                finding = new_id("finding")
                db.execute(
                    "INSERT INTO review_findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        finding,
                        project,
                        session,
                        review,
                        value.severity,
                        value.category,
                        value.statement,
                        dump(anchors),
                        value.remedy,
                        status,
                        0,
                        0,
                        dedupe,
                        value.supersedes,
                        self.store.clock(),
                    ),
                )
                found = self._get(db, project, session, finding)
                result = self._transition(
                    db, found, status, actor, "Reviewer raised this finding.", {"review": review}
                )
                self.store.event(
                    db,
                    project,
                    session,
                    "finding.created",
                    {"finding": finding, "review": review, "severity": value.severity},
                )
            db.execute(
                (
                    "INSERT INTO review_records(review_id,kind,payload,created) "
                    "VALUES(?,'finding',?,?)"
                ),
                (review, dump(result), self.store.clock()),
            )
            self._operation(db, project, actor, value.operation_id, fingerprint, result)
        return result

    def respond(self, project: str, session: str, raw: object, author: Author) -> Json:
        self.store.scope(project, session)
        try:
            value = FindingResponse.model_validate(raw)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid finding response") from None
        text(value.explanation)
        if author.kind != "user" and not (
            author.kind == "agent" and author.role == "main" and author.session_id == session
        ):
            raise DocumentError(POLICY_DENIED, "only the user and session main agent may respond")
        if author.kind != "user" and value.action in ("confirm", "dismiss", "reopen"):
            raise DocumentError(POLICY_DENIED, "the author cannot confirm or dismiss findings")
        if value.action == "fix" and not value.evidence and not value.revisions:
            raise DocumentError(INVALID_REQUEST, "a claimed fix needs changed evidence references")
        pinned: list[Json] = []
        for document, number in value.revisions:
            if type(number) is not int or not 1 <= number <= 999999999:
                raise DocumentError(INVALID_REQUEST, "invalid changed document revision")
            revision = self.store.documents.revision(project, document, number)
            locator = {"document": document, "revision": number, "sha256": revision.info.sha256}
            pinned.append(locator)
        anchors: list[Json] = []
        for a in value.evidence:
            with self.store.documents._read() as db:
                assert db is not None  # noqa: S101
                row = db.execute(
                    (
                        "SELECT e.review_id FROM review_evidence e JOIN review_runs r "
                        "USING(review_id) WHERE evidence_id=? AND project=? AND session=?"
                    ),
                    (a.evidence, project, session),
                ).fetchone()
            if row is None:
                raise DocumentError(NOT_FOUND, "response evidence is outside this session")
            anchors.append(self.validate_anchor(project, session, row[0], a))
        actor = author.actor
        fingerprint = digest(dump([session, value.model_dump(mode="json"), author.role]))
        payload = {
            "action": value.action,
            "explanation": value.explanation,
            "anchors": anchors,
            "revisions": pinned,
            "author": author.model_dump(mode="json"),
        }
        with self.store.documents._write() as db:
            replay = self._replay(db, project, actor, value.operation_id, fingerprint)
            if replay:
                return replay
            found = self._get(db, project, session, value.finding)
            if value.expected != found["revision"]:
                raise DocumentError(CONFLICT, "finding changed since this response was drafted")
            if value.action in ("fix", "rebut") and found["status"] in RESOLVED:
                raise DocumentError(CONFLICT, "reopen the resolved finding before responding")
            status = {
                "fix": "claimed fixed",
                "rebut": "open",
                "confirm": "confirmed fixed",
                "dismiss": "withdrawn",
                "reopen": "open",
            }[value.action]
            result = self._transition(db, found, status, actor, value.explanation, payload)
            self._operation(db, project, actor, value.operation_id, fingerprint, result)
        # Immutable revision pins become confirmation evidence. A crash here cannot
        # lose them: the durable transition payload is also included in the snapshot.
        for locator in pinned:
            self.store.observe(
                project,
                session,
                f"response:{value.finding}:{result['revision']}:{locator['document']}:{locator['revision']}",
                "document",
                locator,
                locator,
            )
        return result

    def confirm(self, project: str, session: str, review: str, owner: str, raw: object) -> Json:
        try:
            value = ConfirmationInput.model_validate(raw)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid confirmation decision") from None
        text(value.explanation)
        anchors = [self.validate_anchor(project, session, review, a) for a in value.anchors]
        actor = f"review:{review}"
        fingerprint = digest(dump(value.model_dump(mode="json")))
        with self.store.documents._write() as db:
            replay = self._replay(db, project, actor, value.operation_id, fingerprint)
            if replay:
                return replay
            run = self.store._run(db, project, session, review)
            self.store.owner(run, owner)
            pinned = db.execute(
                (
                    "SELECT payload,evidence_id FROM review_evidence WHERE review_id=? AND "
                    "kind='finding' AND json_extract(locator,'$.finding')=? AND "
                    "json_extract(locator,'$.revision')=?"
                ),
                (review, value.finding, value.expected),
            ).fetchone()
            if pinned is None:
                raise DocumentError(
                    POLICY_DENIED, "finding response is outside the confirmation snapshot"
                )
            found = self._get(db, project, session, value.finding)
            if found["revision"] != value.expected:
                raise DocumentError(CONFLICT, "finding changed during confirmation")
            config = ReviewConfig.model_validate_json(run["config"])
            if value.decision in ("confirm", "reject"):
                self.confirmation_reads(db, review, json.loads(pinned[0]), pinned[1])
            if value.decision in ("confirm", "reject", "inconclusive"):
                if run["kind"] != "confirmation" or found["status"] != "claimed fixed":
                    raise DocumentError(
                        POLICY_DENIED, "only a confirmation review may check a claimed fix"
                    )
                status = {
                    "confirm": "confirmed fixed",
                    "reject": "open",
                    "inconclusive": "inconclusive",
                }[value.decision]
            else:
                latest = db.execute(
                    (
                        "SELECT payload FROM review_transitions WHERE finding_id=? ORDER "
                        "BY revision DESC LIMIT 1"
                    ),
                    (value.finding,),
                ).fetchone()
                if json.loads(latest[0]).get("action") != "rebut":
                    raise DocumentError(
                        POLICY_DENIED, "there is no pending rebuttal in this finding"
                    )
                status = "withdrawn" if value.decision == "accept_rebuttal" else "open"
            reflag = value.decision in ("reject", "reject_rebuttal")
            if reflag and found["reflags"] + 1 >= config.max_reflags:
                status = "unaddressed"
            result = self._transition(
                db,
                found,
                status,
                actor,
                value.explanation,
                {"review": review, "anchors": anchors, "decision": value.decision},
                reflag=reflag,
            )
            self._operation(db, project, actor, value.operation_id, fingerprint, result)
        return result

    @staticmethod
    def confirmation_reads(
        db: sqlite3.Connection, review: str, pinned: Json, finding_evidence: str
    ) -> None:
        """Require nonempty audited exposure, without claiming model understanding."""
        reads: dict[str, list[Json]] = {}
        for evidence, locator in db.execute(
            "SELECT evidence_id,locator FROM review_reads WHERE review_id=? "
            "ORDER BY seq LIMIT 1000",
            (review,),
        ):
            location = json.loads(locator)
            if location.get("returned_characters", 0) > 0:
                reads.setdefault(evidence, []).append(location)

        def covered(evidence: str, block: str | None = None) -> bool:
            ranges = [
                location for location in reads.get(evidence, []) if location.get("block") == block
            ]
            if not ranges:
                return False
            total = max(location.get("total_characters", 0) for location in ranges)
            end = 0
            for location in sorted(ranges, key=lambda value: value["start"]):
                if location["start"] > end:
                    return False
                end = max(end, location["start"] + location["returned_characters"])
            return total > 0 and end >= total

        response_entry = cast(Json, pinned.get("response") or {})
        response = cast(Json, response_entry.get("payload", {}))
        anchors = cast(list[Json], pinned["anchors"] + response.get("anchors", []))
        if not covered(finding_evidence) or any(
            not (covered(anchor["evidence"], anchor.get("block")) or covered(anchor["evidence"]))
            for anchor in anchors
        ):
            raise DocumentError(
                POLICY_DENIED,
                "read the pinned finding and original/response evidence before deciding",
            )
        revisions = cast(list[Json], response.get("revisions", []))
        if revisions:
            available = list(
                db.execute(
                    "SELECT evidence_id,locator FROM review_evidence e WHERE kind='document' "
                    "AND (review_id=? OR EXISTS(SELECT 1 FROM review_grants g WHERE "
                    "g.review_id=? AND g.evidence_id=e.evidence_id))",
                    (review, review),
                )
            )
            for revision in revisions:
                if not any(
                    covered(evidence)
                    and all(
                        json.loads(locator).get(key) == revision[key]
                        for key in ("document", "revision", "sha256")
                    )
                    for evidence, locator in available
                ):
                    raise DocumentError(
                        POLICY_DENIED, "read the pinned changed revision evidence before deciding"
                    )

    def bound(
        self, project: str, session: str, reason: str, *, notices: set[str] | None = None
    ) -> None:
        """End bounded author rounds; a claimed fix stays explicitly unconfirmed."""
        with self.store.documents._write() as db:
            ids = [
                r[0]
                for r in db.execute(
                    (
                        "SELECT finding_id FROM review_findings WHERE project=? AND "
                        "session=? AND status='open'"
                    ),
                    (project, session),
                )
            ]
            for finding in ids:
                if notices is not None and not any(n.startswith(finding + "-r") for n in notices):
                    continue
                found = self._get(db, project, session, finding)
                self._transition(db, found, "unaddressed", "platform:review-bound", reason, {})

    def supersede(self, project: str, session: str) -> None:
        """Only replaced anchored blocks count; unrelated document edits do not."""
        self.store.scope(project, session)
        with self.store.documents._read() as db:
            assert db is not None  # noqa: S101
            found = [
                self._get(db, project, session, r[0])
                for r in db.execute(
                    (
                        "SELECT finding_id FROM review_findings WHERE project=? AND "
                        "session=? AND status='open' LIMIT 100"
                    ),
                    (project, session),
                )
            ]
        prepared: list[tuple[Json, list[Json]]] = []
        for finding in found:
            replacements: list[Json] = []
            for a in finding["anchors"]:
                location = a["locator"]
                if "document" not in location:
                    replacements = []
                    break
                document, number = location["document"], location["revision"]
                current = self.store.documents.document(project, document)
                if current.head <= number:
                    replacements = []
                    break
                old, new = (
                    self.store.documents.mapped(project, document, number),
                    self.store.documents.mapped(project, document, current.head),
                )
                if a.get("block") is not None:
                    old_i, new_i = old.index(a["block"]), new.index(a["block"])
                    if (
                        old_i is not None
                        and new_i is not None
                        and old.parsed.blocks[old_i].text == new.parsed.blocks[new_i].text
                    ):
                        replacements = []
                        break
                replacements.append(
                    {
                        "document": document,
                        "revision": current.head,
                        "sha256": self.store.documents.revision(
                            project, document, current.head
                        ).info.sha256,
                        "block": a.get("block"),
                    }
                )
            if replacements:
                prepared.append((finding, replacements))
        with self.store.documents._write() as db:
            for finding, replacements in prepared:
                current = self._get(db, project, session, finding["finding_id"])
                if current["status"] == "open" and current["revision"] == finding["revision"]:
                    self._transition(
                        db,
                        current,
                        "superseded",
                        "platform:supersession",
                        "The anchored revisions changed; no fix was confirmed.",
                        {"replacements": replacements},
                    )
