# SPDX-License-Identifier: MIT
"""Finding mutation grants and append-only, expected-revision transitions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import new_id
from sandbox_science.contracts.artifact_inventory import ArtifactLink, ArtifactRevisionRef
from sandbox_science.orchestrator.documents.comment_bodies import (
    prepare as response_prepare,
)
from sandbox_science.orchestrator.documents.errors import (
    CONFLICT,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.project_macros import (
    dump as macro_dump,
)

from .models import (
    ConfirmationInput,
    EvidenceAnchor,
    FindingInput,
    FindingResponse,
    ReviewConfig,
)
from .store import RECEIPTS, ReviewStore, digest, dump, identifier, stored_json, text

# Heterogeneous persisted JSON is validated at the mutation boundary.
Json = dict[str, Any]
RESOLVED = ("confirmed fixed", "withdrawn")
_REVIEWED = (
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
"""The columns a reviewer's finding sets; a math finding sets ``origin`` and
``math_check_id`` too."""
FIELDS = (*_REVIEWED, "origin", "math_check_id")


SEVERITY_RANK: Final = {"fail": 0, "warn": 1, "note": 2}
"""A lower rank is more severe."""

_ASSESSMENT: Final = ("review_id", "severity", "category", "statement", "anchors", "remedy")
"""The fields of a reviewer's assessment of a finding."""


def _status_for(value: FindingInput, run: Json, anchors: list[Json]) -> str:
    """``inconclusive`` when the finding rests on missing or incomplete evidence."""
    incomplete = (
        value.assessment == "missing_evidence"
        or (value.category == "missing_action" and not run["coverage_complete"])
        or any(
            not anchor["complete"]
            and ("artifact" in anchor["locator"] or "operation" in anchor["locator"])
            for anchor in anchors
        )
    )
    return "inconclusive" if incomplete else "open"


class Findings:
    """Reviewers raise findings; main agents respond; confirmation/user grants resolve."""

    def __init__(self, store: ReviewStore) -> None:
        self.store = store

    def load(self, db: sqlite3.Connection, project: str, session: str, finding: str) -> Json:
        identifier(finding, "finding")
        row = db.execute(
            "SELECT "  # noqa: S608 - fixed column names, all user values bound
            + ",".join(FIELDS)
            + " FROM review_findings WHERE project=? AND session=? AND finding_id=?",
            (project, session, finding),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "no such scoped finding")
        result: Json = dict(zip(FIELDS, row, strict=True))
        result["anchors"] = json.loads(result["anchors"])
        result["response_generation"] = db.execute(
            "SELECT coalesce(max(rowid),0) FROM review_response_bodies WHERE finding_id=?",
            (finding,),
        ).fetchone()[0]
        result["source"] = (
            {"kind": "math", "check": result["math_check_id"]}
            if result["origin"] == "math"
            else {"kind": "review", "review": result["review_id"]}
        )
        if result["origin"] == "math":
            state = db.execute(
                "SELECT checked_revision,owner_revision,excluded FROM math_finding_state "
                "WHERE finding_id=?",
                (finding,),
            ).fetchone()
            if state:
                result["source"].update(
                    dict(
                        zip(("checked_revision", "owner_revision", "excluded"), state, strict=True)
                    )
                )
        return result

    def get(self, project: str, session: str, finding: str, *, before: int | None = None) -> Json:
        self.store.scope(project, session)
        if before is not None and (type(before) is not int or before < 1 or before > 999999999):
            raise DocumentError(INVALID_REQUEST, "invalid finding history cursor")
        if self.store.documents.control.missing:
            raise DocumentError(NOT_FOUND, "no such finding")
        with self.store.documents.control.read() as tx:
            db = tx.sql
            result = self.load(db, project, session, finding)
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
        from .response_bodies import pin as response_pin

        with self.store.documents.control.read() as tx:
            db = tx.sql
            for raw_entry in result["history"]:
                entry = cast(Json, raw_entry)
                body = response_pin(db, finding, entry["revision"])
                if body is not None:
                    entry["response_body"] = body
        result["history_before"] = selected[-1][0] if len(rows) > len(selected) else None
        return result

    def listing(
        self, project: str, session: str, *, after: str | None = None, unresolved: bool = False
    ) -> Json:
        self.store.scope(project, session)
        if self.store.documents.control.missing:
            return {"findings": [], "next_after": None}
        with self.store.documents.control.read() as tx:
            db = tx.sql
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
                value = self.load(db, project, session, finding)
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

    @staticmethod
    def _replay(
        db: sqlite3.Connection, project: str, actor: str, operation: str, fingerprint: str
    ) -> Json | None:
        found = RECEIPTS.find(db, project, actor, operation, fingerprint)
        return None if found is None else {**found, "replayed": True}

    @staticmethod
    def record_operation(
        db: sqlite3.Connection,
        project: str,
        actor: str,
        operation: str,
        fingerprint: str,
        result: Json,
    ) -> None:
        RECEIPTS.record(db, project, actor, operation, fingerprint, result)

    def transition(
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
        if (
            found["origin"] == "review"
            and status == "open"
            and SEVERITY_RANK[found["severity"]] <= SEVERITY_RANK[threshold]
        ):
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
        """A reviewer's finding: a new one, a reflag of the open one with the same
        semantic identity, or, while that one awaits its response's decision, a
        deferred reflag."""
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
        with self.store.documents.control.write() as tx:
            db = tx.sql
            replay = self._replay(db, project, actor, value.operation_id, fingerprint)
            if replay:
                return replay
            run = self.store.load_run(db, project, session, review)
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
                superseded = self.load(db, project, session, value.supersedes)
                if superseded["origin"] == "math":
                    raise DocumentError(POLICY_DENIED, "math findings cannot be superseded")
            existing = db.execute(
                (
                    "SELECT finding_id FROM review_findings WHERE project=? AND "
                    "session=? AND dedupe=? AND status NOT IN ('confirmed "
                    "fixed','withdrawn','superseded') ORDER BY rowid LIMIT 1"
                ),
                (project, session, dedupe),
            ).fetchone()
            status = _status_for(value, run, anchors)
            if existing is None:
                result = self._raise(db, project, session, review, value, anchors, dedupe, status)
            else:
                found = self.load(db, project, session, existing[0])
                assessment = {
                    "review_id": review,
                    "severity": value.severity,
                    "category": value.category,
                    "statement": value.statement,
                    "anchors": anchors,
                    "remedy": value.remedy,
                }
                if self._response_pending(db, found):
                    result = self._defer(db, review, found, assessment)
                    self.record_operation(
                        db, project, actor, value.operation_id, fingerprint, result
                    )
                    return result
                if found["reflags"] + 1 >= config.max_reflags:
                    status = "unaddressed"
                result = self._reflag(db, found, assessment, status, actor)
            db.execute(
                (
                    "INSERT INTO review_records(review_id,kind,payload,created) "
                    "VALUES(?,'finding',?,?)"
                ),
                (review, dump(result), self.store.clock()),
            )
            self.record_operation(db, project, actor, value.operation_id, fingerprint, result)
        return result

    @staticmethod
    def _response_pending(db: sqlite3.Connection, found: Json) -> bool:
        """Whether the finding awaits the decision on a fix or rebuttal."""
        latest = db.execute(
            "SELECT payload FROM review_transitions WHERE finding_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (found["finding_id"],),
        ).fetchone()
        return found["status"] == "claimed fixed" or json.loads(latest[0]).get("action") == "rebut"

    def _defer(self, db: sqlite3.Connection, review: str, found: Json, assessment: Json) -> Json:
        """Keep a reflag of a finding whose response is pending as a record only. A
        detached review can still describe the old evidence after a response arrives;
        only an exact pinned review_decide may resolve that response, and generic
        reflags keep its CAS and head."""
        result = {
            "outcome": "response_pending",
            "finding_id": found["finding_id"],
            "status": found["status"],
            "revision": found["revision"],
            "detail": "The pending response needs its pinned reviewer decision.",
        }
        db.execute(
            "INSERT INTO review_records(review_id,kind,payload,created) "
            "VALUES(?,'deferred_reflag',?,?)",
            (review, dump({**result, "assessment": assessment}), self.store.clock()),
        )
        return result

    def _reflag(
        self, db: sqlite3.Connection, found: Json, assessment: Json, status: str, actor: str
    ) -> Json:
        """Reflag ``found``. A stronger assessment reaches the canonical header and the
        notice threshold; a weaker one cannot erase it. Both assessments remain in the
        append-only transition."""
        previous = {key: found[key] for key in _ASSESSMENT}
        if SEVERITY_RANK[assessment["severity"]] <= SEVERITY_RANK[found["severity"]]:
            db.execute(
                "UPDATE review_findings SET review_id=?,severity=?,statement=?,"
                "anchors=?,remedy=? WHERE finding_id=?",
                (
                    assessment["review_id"],
                    assessment["severity"],
                    assessment["statement"],
                    dump(assessment["anchors"]),
                    assessment["remedy"],
                    found["finding_id"],
                ),
            )
            found.update(assessment)
        return self.transition(
            db,
            found,
            status,
            actor,
            assessment["statement"],
            {
                "review": assessment["review_id"],
                "anchors": assessment["anchors"],
                "previous_assessment": previous,
                "assessment": assessment,
            },
            reflag=True,
        )

    def _raise(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        review: str,
        value: FindingInput,
        anchors: list[Json],
        dedupe: str,
        status: str,
    ) -> Json:
        """A new reviewer finding, with its first transition and event."""
        finding = new_id("finding")
        db.execute(
            "INSERT INTO review_findings("
            + ",".join(_REVIEWED)
            + ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        found = self.load(db, project, session, finding)
        result = self.transition(
            db,
            found,
            status,
            f"review:{review}",
            "Reviewer raised this finding.",
            {"review": review, "assessment": {key: found[key] for key in _ASSESSMENT}},
        )
        self.store.event(
            db,
            project,
            session,
            "finding.created",
            {"finding": finding, "review": review, "severity": value.severity},
        )
        return result

    def raise_math(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        *,
        statement: str,
        anchors: list[Json],
        remedy: str | None,
        dedupe: str,
        check: str,
    ) -> str:
        """A new mechanical math finding, open, in the caller's change; its ID."""
        finding = new_id("finding")
        db.execute(
            "INSERT INTO review_findings(finding_id,project,session,review_id,severity,"
            "category,statement,anchors,remedy,status,revision,reflags,dedupe,supersedes,"
            "created,origin,math_check_id) "
            "VALUES(?,?,?,NULL,'warn','math',?,?,?,'open',0,0,?,NULL,?,'math',?)",
            (
                finding,
                project,
                session,
                statement,
                dump(anchors),
                remedy,
                dedupe,
                self.store.clock(),
                check,
            ),
        )
        return finding

    @staticmethod
    def reassess(
        db: sqlite3.Connection,
        finding: str,
        *,
        session: str,
        statement: str,
        anchors: list[Json],
        remedy: str | None,
        check: str,
    ) -> None:
        """Point a math finding at its latest failing check, in the caller's change."""
        db.execute(
            "UPDATE review_findings SET session=?,statement=?,anchors=?,remedy=?,"
            "math_check_id=? WHERE finding_id=?",
            (session, statement, dump(anchors), remedy, check, finding),
        )

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
        if (
            value.action == "fix"
            and not value.evidence
            and not value.revisions
            and not value.artifact_revisions
        ):
            raise DocumentError(INVALID_REQUEST, "a claimed fix needs changed evidence references")
        target = self.get(project, session, value.finding)
        relevant_artifacts = {
            a["locator"]["artifact"]["artifact_id"]
            for a in target["anchors"]
            if isinstance(a["locator"].get("artifact"), dict)
        }
        artifact_pins: list[Json] = []
        for ref in value.artifact_revisions:
            if ref.artifact_id not in relevant_artifacts:
                raise DocumentError(POLICY_DENIED, "changed artifact is unrelated to this finding")
            self.store.artifact_pin(project, session, ref)
            artifact_pins.append(ref.model_dump(mode="json"))
        pinned: list[Json] = []
        for document, number in value.revisions:
            if type(number) is not int or not 1 <= number <= 999999999:
                raise DocumentError(INVALID_REQUEST, "invalid changed document revision")
            revision = self.store.documents.revision(project, document, number)
            locator = {"document": document, "revision": number, "sha256": revision.info.sha256}
            pinned.append(locator)
        anchors: list[Json] = []
        for a in value.evidence:
            with self.store.documents.control.read() as tx:
                db = tx.sql
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
        canonical = value.model_dump(mode="json")
        if not value.artifact_revisions:
            # Old operation receipts predate this optional field. Their exact
            # canonical digest remains valid across an upgrade.
            canonical.pop("artifact_revisions")
        fingerprint = digest(dump([session, canonical, author.role]))
        payload = {
            "action": value.action,
            "explanation": value.explanation,
            "anchors": anchors,
            "revisions": pinned,
            "artifact_revisions": artifact_pins,
            "author": author.model_dump(mode="json"),
        }

        from .response_bodies import commit as response_commit
        from .response_bodies import original as response_original

        prepared_body = (
            response_prepare(value.explanation, author)
            if author.kind == "agent" and target["origin"] == "review"
            else None
        )
        with self.store.documents.control.write() as tx:
            db = tx.sql
            replay = self._replay(db, project, actor, value.operation_id, fingerprint)
            if replay:
                return replay
            found = self.load(db, project, session, value.finding)
            if found["origin"] == "math" and author.kind != "user":
                raise DocumentError(
                    POLICY_DENIED,
                    "patch the math source; its mechanical check confirms fixes without a claim",
                )
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
            result = self.transition(db, found, status, actor, value.explanation, payload)
            if prepared_body is not None:
                response_commit(
                    db,
                    response_original(db, value.finding, result["revision"]),
                    author,
                    prepared_body,
                )
            self.record_operation(db, project, actor, value.operation_id, fingerprint, result)
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
        with self.store.documents.control.write() as tx:
            db = tx.sql
            replay = self._replay(db, project, actor, value.operation_id, fingerprint)
            if replay:
                return replay
            run = self.store.load_run(db, project, session, review)
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
            pinned_finding = stored_json(pinned[0])
            found = self.load(db, project, session, value.finding)
            if found["origin"] == "math":
                raise DocumentError(
                    POLICY_DENIED, "only the mechanical check decides math findings"
                )
            if found["revision"] != value.expected:
                raise DocumentError(CONFLICT, "finding changed during confirmation")
            from .response_bodies import pin as response_pin

            current_body = response_pin(db, value.finding, value.expected)
            if current_body is not None:
                body_pin = db.execute(
                    "SELECT body_revision,sha256 "
                    "FROM review_response_pins WHERE review_id=? AND finding_id=? AND "
                    "decision_revision=?",
                    (review, value.finding, value.expected),
                ).fetchone()
                if body_pin != (current_body["revision"], current_body["sha256"]):
                    raise DocumentError(
                        CONFLICT, "response explanation changed during confirmation"
                    )

                macro = current_body["macro_context"]
                pinned_macro = db.execute(
                    "SELECT macro_revision FROM review_response_pins "
                    "WHERE review_id=? AND finding_id=?",
                    (review, value.finding),
                ).fetchone()
                if pinned_macro != (macro["revision"],):
                    raise DocumentError(
                        CONFLICT, "response macro context changed during confirmation"
                    )
                if value.decision != "inconclusive":
                    response = cast(Json, pinned_finding.get("response") or {})
                    raw_context = response.get("macro_context")
                    context = cast(Json, raw_context) if isinstance(raw_context, dict) else {}
                    # The snapshot resolved these exact immutable anchors and
                    # setting. Overall review coverage is a separate guard;
                    # ambiguity must not become a definitive source decision.
                    if (
                        context.get("known") is not True
                        or context.get("held") is not False
                        or context.get("key") != digest(macro_dump({**macro, "known": True}))
                        or any(a.get("unknown") for a in macro["anchors"])
                    ):
                        raise DocumentError(
                            POLICY_DENIED,
                            "response macro context is held or unknown; decide inconclusive",
                        )
            config = ReviewConfig.model_validate_json(run["config"])
            if value.decision in ("confirm", "reject"):
                self.confirmation_reads(db, review, pinned_finding, pinned[1])
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
            result = self.transition(
                db,
                found,
                status,
                actor,
                value.explanation,
                {"review": review, "anchors": anchors, "decision": value.decision},
                reflag=reflag,
            )
            self.record_operation(db, project, actor, value.operation_id, fingerprint, result)
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
            if location.get("returned_characters", 0) > 0 or location.get("empty_source"):
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
            return (total > 0 and end >= total) or any(r.get("empty_source") for r in ranges)

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
        for anchor in anchors:
            item = db.execute(
                "SELECT kind,complete FROM review_evidence WHERE evidence_id=?",
                (anchor["evidence"],),
            ).fetchone()
            if item is not None and item[0] == "artifact" and not item[1]:
                raise DocumentError(
                    POLICY_DENIED, "artifact content is unavailable for confirmation"
                )
        artifact_pins = response.get("artifact_revisions", [])
        for ref in artifact_pins:
            available_artifacts = db.execute(
                "SELECT evidence_id,locator,complete "
                "FROM review_evidence e WHERE kind='artifact' AND "
                "(review_id=? OR EXISTS(SELECT 1 FROM review_grants g WHERE "
                "g.review_id=? AND g.evidence_id=e.evidence_id)) LIMIT 257",
                (review, review),
            ).fetchall()
            if not any(
                complete and covered(evidence) and stored_json(locator).get("artifact") == ref
                for evidence, locator, complete in available_artifacts
            ):
                raise DocumentError(
                    POLICY_DENIED, "read the exact changed artifact before deciding"
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
        with self.store.documents.control.write() as tx:
            db = tx.sql
            ids = [
                r[0]
                for r in db.execute(
                    (
                        "SELECT finding_id FROM review_findings WHERE project=? AND "
                        "session=? AND status='open' AND category!='math'"
                    ),
                    (project, session),
                )
            ]
            for finding in ids:
                if notices is not None and not any(n.startswith(finding + "-r") for n in notices):
                    continue
                found = self.load(db, project, session, finding)
                self.transition(db, found, "unaddressed", "platform:review-bound", reason, {})

    def supersede(self, project: str, session: str) -> None:
        """Only replaced anchored blocks count; unrelated document edits do not."""
        self.store.scope(project, session)
        with self.store.documents.control.read() as tx:
            db = tx.sql
            found = [
                self.load(db, project, session, r[0])
                for r in db.execute(
                    (
                        "SELECT finding_id FROM review_findings WHERE project=? AND "
                        "session=? AND status='open' AND category!='math' LIMIT 100"
                    ),
                    (project, session),
                )
            ]
        prepared: list[tuple[Json, list[Json], list[Json]]] = []
        for finding in found:
            replacements: list[Json] = []
            artifact_replacements: list[Json] = []
            replaceable = True
            for a in finding["anchors"]:
                location = a["locator"]
                if "artifact" in location and self.store.artifacts is not None:
                    old_ref = ArtifactRevisionRef.model_validate(location["artifact"])
                    latest = self.store.artifacts.resolve(
                        project,
                        ArtifactLink(
                            project_id=project,
                            artifact_id=old_ref.artifact_id,
                            mode="latest",
                        ),
                    ).reference
                    if latest.revision <= old_ref.revision:
                        replaceable = False
                        break
                    try:
                        self.store.artifact_pin(project, session, latest)
                    except DocumentError:
                        # No cross-session grant is inferred from a shared artifact head.
                        replaceable = False
                        break
                    artifact_replacements.append(latest.model_dump(mode="json"))
                    continue
                if "document" not in location:
                    replaceable = False
                    break
                document, number = location["document"], location["revision"]
                current = self.store.documents.document(project, document)
                if current.head <= number:
                    replaceable = False
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
                        replaceable = False
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
            if replaceable and (replacements or artifact_replacements):
                prepared.append((finding, replacements, artifact_replacements))
        with self.store.documents.control.write() as tx:
            db = tx.sql
            for finding, replacements, artifact_replacements in prepared:
                current = self.load(db, project, session, finding["finding_id"])
                if current["status"] == "open" and current["revision"] == finding["revision"]:
                    self.transition(
                        db,
                        current,
                        "superseded",
                        "platform:supersession",
                        "The anchored revisions changed; no fix was confirmed.",
                        {
                            "replacements": replacements,
                            "artifact_replacements": artifact_replacements,
                        },
                    )
