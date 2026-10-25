# SPDX-License-Identifier: MIT
"""Finding mutation grants and append-only, expected-revision transitions.

A finding's lifecycle in the review store (docs/specs/consistency-review.md, CR-40 to
CR-47): five statuses, each change a record of the finding's history with its actor's
kind, the re-check that queues on a later revision of what the finding reviewed or on
the author's ``fix``, the author's dismissal that the next review accepts or rejects, and
the successor link of a finding a later review replaced."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
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
    ADVISORY_CATEGORIES,
    DISMISSAL_CHARACTERS,
    ConfirmationInput,
    EvidenceAnchor,
    FindingInput,
    FindingResponse,
    ReviewConfig,
)
from .store import RECEIPTS, ReviewStore, agent_text, digest, dump, identifier, stored_json, text

# Heterogeneous persisted JSON is validated at the mutation boundary.
Json = dict[str, Any]
SETTLED: Final = ("fixed", "superseded")
"""The statuses no response or reviewer's re-flag changes; only the user's ``set``."""

_OFF_MATERIAL: Final = frozenset({"title", "status", "links"})
"""The fields of a ledger entry that are not, or not wholly, what a decision rests on."""
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
FIELDS = (
    *_REVIEWED,
    "origin",
    "math_check_id",
    "superseded_by",
    "latest_recheck",
    "dismissal",
    "recheck",
    "at_bound",
)
_JSON_COLUMNS: Final = ("latest_recheck", "dismissal", "recheck")
"""The lifecycle columns that hold JSON, or NULL."""


def actor_kind(actor: str) -> str:
    """Who an actor is, for a finding's history (CR-40): the user, the review controller
    (for a review or a check), the main agent, or the platform."""
    if actor.startswith("user:"):
        return "user"
    if actor.startswith("review:"):
        return "controller"
    if actor.startswith("session:"):
        return "agent"
    return "platform"


def pending_dismissal(found: Json) -> bool:
    """Whether the author dismissed ``found`` and no review has decided the reason yet."""
    dismissal = found.get("dismissal")
    return (
        found["status"] == "dismissed"
        and isinstance(dismissal, dict)
        and cast(Json, dismissal).get("by") == "agent"
        and cast(Json, dismissal).get("review") is None
    )


SEVERITY_RANK: Final = {"fail": 0, "warn": 1, "note": 2}
"""A lower rank is more severe."""

_ASSESSMENT: Final = ("review_id", "severity", "category", "statement", "anchors", "remedy")
"""The fields of a reviewer's assessment of a finding."""


def _work(anchors: list[Json]) -> tuple[set[tuple[object, ...]], tuple[int, int] | None]:
    """What anchors reviewed: each revision of a document (and its block), an artifact or
    a ledger entry (and its field) they name, and the span of archive positions of the
    messages they name."""
    places: set[tuple[object, ...]] = set()
    positions: list[int] = []
    for anchor in anchors:
        location = cast(Json, anchor.get("locator") or {})
        if "document" in location and "revision" in location:
            places.add(("document", location["document"], location["revision"]))
        elif isinstance(location.get("artifact"), dict):
            ref = cast(Json, location["artifact"])
            places.add(("artifact", ref.get("artifact_id"), ref.get("revision")))
        elif "entry" in location and "revision" in location:
            places.add(("ledger", location["entry"], location["revision"]))
        archived = location.get("archive")
        if isinstance(archived, dict) and type(cast(Json, archived).get("position")) is int:
            positions.append(cast(int, cast(Json, archived)["position"]))
    return places, (min(positions), max(positions)) if positions else None


def same_work(old: list[Json], new: list[Json]) -> bool:
    """Whether two findings' anchors review the same work (CR-44): a revision of the same
    artifact, document or ledger entry, or overlapping spans of archive messages."""
    old_places, old_span = _work(old)
    new_places, new_span = _work(new)
    if old_places & new_places:
        return True
    return (
        old_span is not None
        and new_span is not None
        and old_span[0] <= new_span[1]
        and new_span[0] <= old_span[1]
    )


def messages_only(anchors: list[Json]) -> bool:
    """Whether a finding is anchored only to messages, and so never becomes stale and is
    re-checked only on a ``fix`` that names a later message (CR-42, CR-44)."""
    places, _ = _work(anchors)
    return not places


def _unique(values: list[Json]) -> list[Json]:
    """``values`` in order, each once."""
    seen: set[str] = set()
    kept: list[Json] = []
    for value in values:
        if (key := dump(value)) not in seen:
            seen.add(key)
            kept.append(value)
    return kept


@dataclass(frozen=True)
class Marks:
    """What a finding reviewed, as a re-check's evidence must be later than it (CR-42):
    the latest revision of each document, artifact and ledger entry its anchors name, and
    the last archive position its anchors or its review's snapshot hold."""

    documents: dict[str, int]
    artifacts: dict[str, int]
    entries: dict[str, int]
    position: int | None

    def later(self, location: Json) -> tuple[str, Json]:
        """The place, ``("place", ...)``, or the archive message, ``("message", ...)``,
        that ``location`` names, when it is strictly later than what the finding
        reviewed: a later revision of a document, artifact or entry it reviewed, or an
        archive message after its review's snapshot. Execution output counts only as the
        later archive message that holds it, never as a place of its own."""
        archived = location.get("archive")
        if "document" in location and type(location.get("revision")) is int:
            document, revision = str(location["document"]), cast(int, location["revision"])
            if document not in self.documents:
                raise DocumentError(
                    POLICY_DENIED, f"document {document} is not what the finding reviewed"
                )
            if revision <= self.documents[document]:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{document} r{revision} is not later than the revision the finding reviewed",
                )
            pinned = {k: location[k] for k in ("document", "revision", "sha256") if k in location}
            return "place", {"document": pinned}
        if isinstance(location.get("artifact"), dict):
            ref = cast(Json, location["artifact"])
            artifact, revision = str(ref.get("artifact_id")), ref.get("revision")
            if artifact not in self.artifacts:
                raise DocumentError(POLICY_DENIED, "changed artifact is unrelated to this finding")
            if type(revision) is not int or revision <= self.artifacts[artifact]:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"artifact {artifact} r{revision} is not later than the revision the "
                    "finding reviewed",
                )
            return "place", {"artifact": ref}
        if "entry" in location and type(location.get("revision")) is int:
            entry, revision = str(location["entry"]), cast(int, location["revision"])
            if entry not in self.entries:
                raise DocumentError(POLICY_DENIED, "changed entry is unrelated to this finding")
            if revision <= self.entries[entry]:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{location.get('handle') or entry} r{revision} is not later than the "
                    "revision the finding reviewed",
                )
            found = {"entry": entry, "handle": location.get("handle"), "revision": revision}
            if location.get("field") is not None:
                found["field"] = location["field"]
            return "place", {"entry": found}
        if isinstance(archived, dict) and isinstance(cast(Json, archived).get("id"), str):
            message = cast(Json, archived)
            position = message.get("position")
            if self.position is None or type(position) is not int or position <= self.position:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"message {message['id']} is not later than what the finding's review held",
                )
            return "message", {
                "id": message["id"],
                "position": position,
                "run": location.get("run"),
            }
        raise DocumentError(
            INVALID_REQUEST,
            "a re-check's evidence is a later revision of what the finding reviewed, or a "
            "later archive message; an execution's output counts only as the later tool "
            "result message that holds it",
        )


def dedupe_of(category: str, anchors: list[Json]) -> str:
    """A finding's semantic identity. It excludes snapshot-local IDs, so the same pinned
    revision reviewed in another window re-flags rather than duplicating a finding."""
    identities = [
        {k: v for k, v in a.items() if k not in ("evidence", "review", "sha256", "complete")}
        for a in anchors
    ]
    return digest(dump([category, sorted(identities, key=dump)]))


def _first_error(error: ValidationError) -> str:
    """Where a response first breaks its schema, and how, without the value given."""
    first = error.errors(include_input=False, include_url=False)[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}"[:300]


def _incomplete(value: FindingInput, run: Json, anchors: list[Json]) -> bool:
    """Whether the finding rests on missing or incomplete evidence (CR-6): it is ``open``
    and never interrupts the author, and its first history row says so."""
    return (
        value.assessment == "missing_evidence"
        or (value.category == "missing_action" and not run["coverage_complete"])
        or any(
            not anchor["complete"]
            and ("artifact" in anchor["locator"] or "operation" in anchor["locator"])
            for anchor in anchors
        )
    )


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
        for column in _JSON_COLUMNS:
            if result[column] is not None:
                result[column] = json.loads(result[column])
        result["at_bound"] = bool(result["at_bound"])
        result["response_generation"] = db.execute(
            "SELECT coalesce(max(rowid),0) FROM text_revisions WHERE subject_type='response' "
            "AND parent=?",
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
                        "SELECT h.revision,h.status,h.actor,h.reason,"
                        "coalesce(t.payload,'{}'),h.created,h.actor_kind,h.former,h.answer,"
                        "h.review_id,h.recheck FROM review_finding_history h "
                        "LEFT JOIN review_transitions t ON t.seq=h.transition "
                        "WHERE h.finding_id=? AND h.revision<? ORDER BY h.revision DESC, "
                        "h.seq DESC LIMIT 21"
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
                    (
                        "revision",
                        "status",
                        "actor",
                        "reason",
                        "payload",
                        "created",
                        "actor_kind",
                        "former_status",
                        "answer",
                        "review",
                        "recheck",
                    ),
                    (
                        *r[:4],
                        json.loads(r[4]),
                        *r[5:10],
                        None if r[10] is None else json.loads(r[10]),
                    ),
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
        self,
        project: str,
        session: str,
        *,
        after: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> Json:
        """The session's findings, newest first, in ``statuses`` when given (CR-47)."""
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
                        "session=? AND rowid<? AND (? IS NULL OR status IN (SELECT value "
                        "FROM json_each(?))) ORDER BY rowid DESC LIMIT 21"
                    ),
                    (
                        project,
                        session,
                        cursor,
                        None if statuses is None else 1,
                        dump(list(statuses or ())),
                    ),
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
        if anchor.field is not None and item["kind"] != "ledger":
            raise DocumentError(INVALID_REQUEST, "a field anchor needs a ledger entry revision")
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
        notify: bool = True,
        answer: str | None = None,
        recheck: Json | None = None,
        columns: Json | None = None,
    ) -> Json:
        """Move a finding to ``status``, as one record of its history (CR-45): who set it
        and their kind, when, why, and for a re-check its ``answer`` and ``recheck``
        record (its review, revision, evidence anchors and reviewer run). ``columns`` set
        the finding's lifecycle columns (``superseded_by``, ``latest_recheck``,
        ``dismissal``, ``recheck``, ``at_bound``) in the same change. An open review
        finding at or above the project's interrupt severity, and not at its re-flag
        bound, is offered to its session's runs, unless ``notify`` is false: an agent's
        own response leaves it open, and offering it back to the agent that wrote it made
        a run answer it again and again until its turns ran out (lab 40)."""
        revision = found["revision"] + 1
        flags = found["reflags"] + int(reflag)
        changes = dict(columns or {})
        db.execute(
            "UPDATE review_findings SET status=?,revision=?,reflags=? WHERE finding_id=?",
            (status, revision, flags, found["finding_id"]),
        )
        for column, value in changes.items():
            if column not in ("superseded_by", *_JSON_COLUMNS, "at_bound"):
                raise ValueError(f"not a finding lifecycle column: {column}")
            stored = (
                (None if value is None else dump(value))
                if column in _JSON_COLUMNS
                else (int(value) if column == "at_bound" else value)
            )
            db.execute(
                f"UPDATE review_findings SET {column}=? WHERE finding_id=?",  # noqa: S608 - fixed column names
                (stored, found["finding_id"]),
            )
            found[column] = bool(value) if column == "at_bound" else value
        cursor = db.execute(
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
        kind = actor_kind(actor)
        review = actor.removeprefix("review:") if kind == "controller" else None
        db.execute(
            "INSERT INTO review_finding_history(finding_id,revision,transition,status,actor,"
            "actor_kind,reason,answer,review_id,recheck,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                found["finding_id"],
                revision,
                cursor.lastrowid,
                status,
                actor,
                kind,
                reason,
                answer,
                review,
                None if recheck is None else dump(recheck),
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
                "actor_kind": kind,
                **({"answer": answer} if answer is not None else {}),
            },
        )
        configuration = db.execute(
            "SELECT payload FROM review_config WHERE project=? ORDER BY number DESC LIMIT 1",
            (found["project"],),
        ).fetchone()
        threshold = (
            ReviewConfig.model_validate_json(configuration[0]) if configuration else ReviewConfig()
        ).interrupt_severity
        bounded = bool(changes.get("at_bound", found.get("at_bound", False)))
        if (
            notify
            and not bounded
            and found["origin"] == "review"
            and found["category"] not in ADVISORY_CATEGORIES
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
        value = value.model_copy(
            update={
                "statement": agent_text(value.statement, "the statement"),
                "remedy": agent_text(value.remedy, "the remedy") if value.remedy else None,
                # A suggestion is only ever a note, whatever the reviewer called it.
                **({"severity": "note"} if value.category in ADVISORY_CATEGORIES else {}),
            }
        )
        anchors = [self.validate_anchor(project, session, review, a) for a in value.anchors]
        dedupe = dedupe_of(value.category, anchors)
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
            existing = db.execute(
                (
                    "SELECT finding_id FROM review_findings WHERE project=? AND "
                    "session=? AND dedupe=? AND status IN ('open','stale','dismissed') "
                    "ORDER BY rowid LIMIT 1"
                ),
                (project, session, dedupe),
            ).fetchone()
            incomplete = _incomplete(value, run, anchors)
            if existing is None:
                replaced = (
                    self._replaced(db, project, session, value.supersedes, anchors)
                    if value.supersedes is not None
                    else None
                )
                result = self._raise(
                    db, project, session, review, value, anchors, dedupe, incomplete=incomplete
                )
                if replaced is not None:
                    self.transition(
                        db,
                        replaced,
                        "superseded",
                        actor,
                        f"A later review of the same work replaced it with {result['finding_id']}.",
                        {"review": review, "successor": result["finding_id"]},
                        columns={"superseded_by": result["finding_id"], "recheck": None},
                    )
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
                if found["status"] != "open" or found["at_bound"] or self._response_pending(found):
                    # A dismissed or stale finding stands, and one at its bound or awaiting a
                    # re-check is settled for its rounds: a later review's match is kept for
                    # the record, never reopening or re-injecting it (CR-43, CR-44).
                    result = self._defer(db, review, found, assessment)
                    self.record_operation(
                        db, project, actor, value.operation_id, fingerprint, result
                    )
                    return result
                bound = found["reflags"] + 1 >= config.max_reflags and (
                    value.category not in ADVISORY_CATEGORIES
                )
                result = self._reflag(db, found, assessment, actor, bound=bound)
            db.execute(
                (
                    "INSERT INTO review_records(review_id,kind,payload,created) "
                    "VALUES(?,'finding',?,?)"
                ),
                (review, dump(result), self.store.clock()),
            )
            self.record_operation(db, project, actor, value.operation_id, fingerprint, result)
        return result

    def _replaced(
        self,
        db: sqlite3.Connection,
        project: str,
        session: str,
        finding: str,
        anchors: list[Json],
    ) -> Json:
        """The open finding a new one names as the one it replaces, once the controller
        has checked that it is a later review of the same work: the same revision of the
        artifact, block or field, or an overlapping archive span (CR-44)."""
        old = self.load(db, project, session, finding)
        if old["origin"] != "review":
            raise DocumentError(POLICY_DENIED, "only a review's finding can be replaced")
        if old["status"] != "open":
            raise DocumentError(
                POLICY_DENIED, f"only an open finding can be replaced; {finding} is {old['status']}"
            )
        if not same_work(old["anchors"], anchors):
            raise DocumentError(
                POLICY_DENIED,
                "a replacement reviews the same work: the same revision of the artifact, "
                "block or field, or an overlapping span of archive messages",
            )
        return old

    @staticmethod
    def _response_pending(found: Json) -> bool:
        """Whether the finding awaits its queued re-check's decision (CR-42)."""
        return found.get("recheck") is not None and found["status"] in ("open", "stale")

    def _defer(self, db: sqlite3.Connection, review: str, found: Json, assessment: Json) -> Json:
        """Keep a reflag of a finding that is settled for its rounds as a record only. A
        detached review can still describe the old evidence after a response arrives;
        only an exact pinned review_decide may decide a re-check or a dismissal, and
        generic reflags keep its CAS and head."""
        result = {
            "outcome": "response_pending",
            "finding_id": found["finding_id"],
            "status": found["status"],
            "revision": found["revision"],
            "detail": "The finding is settled for its rounds; the match is kept as a record.",
        }
        db.execute(
            "INSERT INTO review_records(review_id,kind,payload,created) "
            "VALUES(?,'deferred_reflag',?,?)",
            (review, dump({**result, "assessment": assessment}), self.store.clock()),
        )
        return result

    def _reflag(
        self, db: sqlite3.Connection, found: Json, assessment: Json, actor: str, *, bound: bool
    ) -> Json:
        """Reflag ``found``. A stronger assessment reaches the canonical header and the
        notice threshold; a weaker one cannot erase it. Both assessments remain in the
        append-only transition. At the bound it stays ``open``, at its re-flag bound, and
        is not injected again (CR-30, CR-47)."""
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
            "open",
            actor,
            assessment["statement"],
            {
                "review": assessment["review_id"],
                "anchors": assessment["anchors"],
                "previous_assessment": previous,
                "assessment": assessment,
                **({"bound": True} if bound else {}),
            },
            reflag=True,
            columns={"at_bound": True} if bound else None,
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
        *,
        incomplete: bool = False,
        supersedes: str | None = None,
        reason: str = "Reviewer raised this finding.",
    ) -> Json:
        """A new reviewer finding, with its first transition and event. One that rests on
        missing evidence is open and never interrupts the author (CR-6)."""
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
                "open",
                0,
                0,
                dedupe,
                supersedes if supersedes is not None else value.supersedes,
                self.store.clock(),
            ),
        )
        found = self.load(db, project, session, finding)
        result = self.transition(
            db,
            found,
            "open",
            f"review:{review}",
            reason,
            {
                "review": review,
                "assessment": {key: found[key] for key in _ASSESSMENT},
                **({"missing_evidence": True} if incomplete else {}),
            },
            notify=not incomplete,
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
        """The main agent's or the user's response (CR-22, CR-23, CR-41, CR-43). A ``fix``
        names the revisions, or the later messages, that address the finding and queues
        its re-check, leaving it as it is until the re-check decides; a ``dismiss`` sets it
        aside with a reason; the user's ``set`` gives it any status with a reason. The
        working agent never makes a finding ``fixed``, and a delegated agent changes none."""
        self.store.scope(project, session)
        try:
            value = FindingResponse.model_validate(raw)
        except ValidationError as error:
            raise DocumentError(
                INVALID_REQUEST, f"invalid finding response: {_first_error(error)}"
            ) from None
        agent = author.kind == "agent"
        if author.kind != "user" and not (
            agent and author.role == "main" and author.session_id == session
        ):
            raise DocumentError(POLICY_DENIED, "only the user and session main agent may respond")
        if agent and value.action == "set":
            raise DocumentError(
                POLICY_DENIED,
                "the author cannot set a finding's status: fix names what addresses it and "
                "queues its re-check, and dismiss sets it aside with a reason",
            )
        if agent:
            dismissing = value.action == "dismiss"
            if dismissing and len(value.explanation) > DISMISSAL_CHARACTERS:
                raise DocumentError(
                    INVALID_REQUEST,
                    f"a dismissal's reason is at most {DISMISSAL_CHARACTERS:,} characters",
                )
            value = value.model_copy(
                update={
                    "explanation": agent_text(
                        value.explanation,
                        "the reason" if dismissing else "the explanation",
                        DISMISSAL_CHARACTERS if dismissing else 8192,
                    )
                }
            )
        text(value.explanation)
        if value.action == "set" and value.status is None:
            raise DocumentError(INVALID_REQUEST, "a set names the status it gives the finding")
        if value.action != "set" and (value.status is not None or value.successor is not None):
            raise DocumentError(INVALID_REQUEST, "only a set names a status or a successor")
        if value.successor is not None and value.status != "superseded":
            raise DocumentError(INVALID_REQUEST, "only a superseded finding names a successor")
        changes = (
            value.revisions or value.artifact_revisions or value.entry_revisions or value.messages
        )
        if value.action == "fix" and not changes and not value.evidence:
            raise DocumentError(
                INVALID_REQUEST,
                "a fix names the revision, or the later message, that addresses the finding",
            )
        if value.action != "fix" and changes:
            raise DocumentError(INVALID_REQUEST, "only a fix names changed revisions or messages")
        target = self.get(project, session, value.finding)
        with self.store.documents.control.read() as tx:
            marks = self.marks(tx.sql, target)
        artifact_pins: list[Json] = []
        for ref in value.artifact_revisions:
            marks.later({"artifact": ref.model_dump(mode="json")})
            self.store.artifact_pin(project, session, ref)
            artifact_pins.append(ref.model_dump(mode="json"))
        entry_pins = self._entry_revisions(project, target, value.entry_revisions)
        pinned: list[Json] = []
        for document, number in value.revisions:
            if type(number) is not int or not 1 <= number <= 999999999:
                raise DocumentError(INVALID_REQUEST, "invalid changed document revision")
            marks.later({"document": document, "revision": number})
            revision = self.store.documents.revision(project, document, number)
            locator = {"document": document, "revision": number, "sha256": revision.info.sha256}
            pinned.append(locator)
        messages = self._messages(project, session, marks, value.messages)
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
        # Each place a fix cites lies later than what the finding reviewed (CR-42).
        places: list[Json] = []
        cited: list[Json] = []
        if value.action == "fix":
            for anchor in anchors:
                kind, found_place = marks.later(cast(Json, anchor["locator"]))
                (places if kind == "place" else cited).append(found_place)
        actor = author.actor
        canonical = value.model_dump(mode="json")
        for optional in ("artifact_revisions", "entry_revisions", "messages"):
            if not getattr(value, optional):
                # Old operation receipts predate these optional fields. Their exact
                # canonical digest remains valid across an upgrade.
                canonical.pop(optional)
        for optional in ("status", "successor"):
            if getattr(value, optional) is None:
                canonical.pop(optional)
        fingerprint = digest(dump([session, canonical, author.role]))
        payload: Json = {
            "action": value.action,
            "explanation": value.explanation,
            "anchors": anchors,
            "revisions": pinned,
            "artifact_revisions": artifact_pins,
            "author": author.model_dump(mode="json"),
        }
        if entry_pins:
            # Only when there are some: a response without them reads as it always did.
            payload["entry_revisions"] = [pin for pin, _ in entry_pins]
        if messages:
            payload["messages"] = messages
        if value.status is not None:
            payload["status"] = value.status
        if value.successor is not None:
            payload["successor"] = value.successor

        from .response_bodies import commit as response_commit
        from .response_bodies import original as response_original

        prepared_body = (
            response_prepare(value.explanation, author)
            if agent and target["origin"] == "review"
            else None
        )
        with self.store.documents.control.write() as tx:
            db = tx.sql
            replay = self._replay(db, project, actor, value.operation_id, fingerprint)
            if replay:
                return replay
            found = self.load(db, project, session, value.finding)
            if found["origin"] == "math" and agent:
                raise DocumentError(
                    POLICY_DENIED,
                    "patch the math source; its mechanical check confirms fixes without a claim",
                )
            if found["category"] in ADVISORY_CATEGORIES and (value.action == "fix" or agent):
                raise DocumentError(
                    POLICY_DENIED,
                    "a suggestion needs no fix or dismissal; it is never re-checked, and the "
                    "user may dismiss it",
                )
            if value.expected != found["revision"]:
                raise DocumentError(CONFLICT, "finding changed since this response was drafted")
            if agent and found["status"] != "open":
                raise DocumentError(
                    CONFLICT,
                    f"the finding is {found['status']}: only an open finding takes a fix or "
                    "a dismissal",
                )
            if value.action == "fix" and found["status"] not in ("open", "stale"):
                raise DocumentError(CONFLICT, "set the finding open before claiming a fix")
            if value.successor is not None:
                if value.successor == value.finding:
                    raise DocumentError(INVALID_REQUEST, "a finding cannot supersede itself")
                self.load(db, project, session, value.successor)
            revision = found["revision"] + 1
            columns: Json = {}
            notify = False
            if value.action == "fix":
                status = found["status"]
                columns["recheck"] = {
                    "cause": "fix",
                    "revision": revision,
                    "response": revision,
                    "places": _unique(
                        [
                            *({"document": p} for p in pinned),
                            *({"artifact": p} for p in artifact_pins),
                            *({"entry": p} for p, _ in entry_pins),
                            *places,
                        ]
                    ),
                    "messages": _unique([*messages, *cited]),
                    "queued": self.store.clock(),
                    "attempts": 0,
                }
            elif value.action == "dismiss" or value.status == "dismissed":
                status = "dismissed"
                columns["dismissal"] = {
                    "by": "agent" if agent else "user",
                    "actor": actor,
                    "reason": value.explanation,
                    "at": self.store.clock(),
                    "response_revision": revision,
                    "review": None,
                }
                columns["recheck"] = None
            else:
                status = cast(str, value.status)
                if status == "open":
                    columns["at_bound"] = False
                    notify = True
                if status in ("fixed", "superseded"):
                    columns["recheck"] = None
                if status == "superseded":
                    columns["superseded_by"] = value.successor
            result = self.transition(
                db,
                found,
                status,
                actor,
                value.explanation,
                payload,
                notify=notify,
                columns=columns,
            )
            if prepared_body is not None:
                response_commit(
                    db,
                    response_original(db, value.finding, result["revision"]),
                    author,
                    prepared_body,
                )
            self.record_operation(db, project, actor, value.operation_id, fingerprint, result)
        # Immutable revision pins become re-check evidence. A crash here cannot lose
        # them: the durable transition payload is also included in the snapshot.
        for locator in pinned:
            self.store.observe(
                project,
                session,
                f"response:{value.finding}:{result['revision']}:{locator['document']}:{locator['revision']}",
                "document",
                locator,
                locator,
            )
        for pin, (locator, item, complete) in entry_pins:
            self.store.observe(
                project,
                session,
                f"response:{value.finding}:{result['revision']}:{pin['entry']}:{pin['revision']}",
                "ledger",
                locator,
                item,
                complete=complete,
            )
        return result

    @staticmethod
    def marks(db: sqlite3.Connection, found: Json) -> Marks:
        """What ``found`` reviewed (``Marks``): its anchors' latest revisions, and the last
        archive position its anchors or its review's snapshot hold."""
        documents: dict[str, int] = {}
        artifacts: dict[str, int] = {}
        entries: dict[str, int] = {}
        positions: list[int] = []

        def keep(into: dict[str, int], key: object, revision: object) -> None:
            if type(revision) is int:
                into[str(key)] = max(into.get(str(key), 0), revision)

        for anchor in found["anchors"]:
            location = cast(Json, anchor.get("locator") or {})
            if "document" in location:
                keep(documents, location["document"], location.get("revision"))
            elif isinstance(location.get("artifact"), dict):
                ref = cast(Json, location["artifact"])
                keep(artifacts, ref.get("artifact_id"), ref.get("revision"))
            elif "entry" in location:
                keep(entries, location["entry"], location.get("revision"))
            archived = location.get("archive")
            if isinstance(archived, dict) and type(cast(Json, archived).get("position")) is int:
                positions.append(cast(int, cast(Json, archived)["position"]))
        if found.get("review_id") is not None:
            (held,) = db.execute(
                "SELECT max(json_extract(locator,'$.archive.position')) FROM review_evidence "
                "WHERE review_id=?",
                (found["review_id"],),
            ).fetchone()
            if type(held) is int:
                positions.append(held)
        return Marks(documents, artifacts, entries, max(positions) if positions else None)

    def _messages(
        self, project: str, session: str, marks: Marks, named: tuple[str, ...]
    ) -> list[Json]:
        """The later archive messages a fix names, each of the finding's session and after
        every message the finding's review held (CR-42)."""
        if not named:
            return []
        from sandbox_science.orchestrator.context.history import Archive
        from sandbox_science.orchestrator.control.errors import ControlError

        archive = Archive(self.store.documents.control)
        found: list[Json] = []
        for message in named:
            try:
                info = archive.message_info(project, message)
            except ControlError:
                raise DocumentError(NOT_FOUND, f"the archive has no message {message}") from None
            position = info["position"]
            if info["session"] != session or type(position) is not int:
                raise DocumentError(NOT_FOUND, f"message {message} is outside this session")
            kind, later = marks.later(
                {"run": info["run"], "archive": {"id": message, "position": position}}
            )
            assert kind == "message"  # noqa: S101 - an archive locator names a message
            found.append(later)
        return found

    def _entry_revisions(
        self, project: str, target: Json, cited: tuple[tuple[str, int], ...]
    ) -> list[tuple[Json, tuple[Json, Json, bool]]]:
        """The changed ledger entry revisions a claimed fix cites, each pinned with the
        evidence a confirmation review reads: a revision of an entry the finding is
        anchored to, later than the one it is anchored to, that changed what an anchor is
        about (the anchored field, and what a decision rests on unless that field is off
        it), as supersession reads an anchor."""
        from sandbox_science.orchestrator.ledger import Ledger

        from .ledger import entry_item

        anchored: dict[str, list[tuple[int, str | None]]] = {}
        for anchor in target["anchors"]:
            location = anchor["locator"]
            if "entry" in location and isinstance(location.get("revision"), int):
                anchored.setdefault(location["entry"], []).append(
                    (location["revision"], anchor.get("field"))
                )
        ledger = Ledger(self.store.documents)
        pins: list[tuple[Json, tuple[Json, Json, bool]]] = []
        for reference, revision in cited:
            view = ledger.entry(project, reference, revision)
            if view.entry_id not in anchored:
                raise DocumentError(POLICY_DENIED, "changed entry is unrelated to this finding")
            if revision <= max(number for number, _ in anchored[view.entry_id]):
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{view.handle} r{revision} is not later than the revision the finding "
                    "is anchored to",
                )
            if not any(
                self._entry_changed(project, view.entry_id, number, field, revision, fix=True)
                for number, field in anchored[view.entry_id]
            ):
                raise DocumentError(
                    INVALID_REQUEST,
                    f"{view.handle} r{revision} does not change what the finding is anchored "
                    "to: the field it names, or the statement, fields, assumptions and "
                    "evidence a decision rests on",
                )
            pin = {"entry": view.entry_id, "handle": view.handle, "revision": revision}
            pins.append((pin, entry_item(view)))
        return pins

    def decide(self, project: str, session: str, review: str, owner: str, raw: object) -> Json:
        """A reviewer's decision on a finding its snapshot holds: a re-check's ``resolved``
        or ``unresolved``, with evidence anchors that code checks exist and lie in the new
        revision or message (CR-42), or the acceptance or rejection of the author's
        dismissal (CR-43). ``resolved`` makes the finding ``fixed``; ``unresolved`` records
        its successor on the new revision, ``open`` with the same category and severity,
        and the finding ``superseded``. A dismissal stands either way."""
        try:
            value = ConfirmationInput.model_validate(raw)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid review decision") from None
        value = value.model_copy(
            update={"explanation": agent_text(value.explanation, "the explanation")}
        )
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
                raise DocumentError(POLICY_DENIED, "the finding is outside this review's snapshot")
            pinned_finding = stored_json(pinned[0])
            # A stale finding of an earlier session is re-checked by a later one's review
            # (CR-44): its target there names it.
            target = db.execute(
                "SELECT 1 FROM review_confirmation_targets WHERE review_id=? AND finding_id=?",
                (review, value.finding),
            ).fetchone()
            found = (
                self.load_in(db, project, value.finding)
                if target is not None
                else self.load(db, project, session, value.finding)
            )
            if found["origin"] == "math":
                raise DocumentError(
                    POLICY_DENIED, "only the mechanical check decides math findings"
                )
            if found["revision"] != value.expected:
                raise DocumentError(CONFLICT, "finding changed during the review")
            recheck = cast(Json | None, found.get("recheck"))
            rechecking = value.decision in ("resolved", "unresolved")
            if rechecking:
                if (
                    run["kind"] != "confirmation"
                    or recheck is None
                    or found["status"] not in ("open", "stale")
                    or db.execute(
                        "SELECT 1 FROM review_confirmation_targets WHERE review_id=? AND "
                        "finding_id=? AND revision=?",
                        (review, value.finding, value.expected),
                    ).fetchone()
                    is None
                ):
                    raise DocumentError(
                        POLICY_DENIED, "only a re-check of this finding may answer it"
                    )
                response_revision = recheck.get("response")
            else:
                if not pending_dismissal(found):
                    raise DocumentError(
                        POLICY_DENIED, "there is no pending dismissal in this finding"
                    )
                response_revision = cast(Json, found["dismissal"]).get("response_revision")
            self._pinned_body(db, review, value.finding, response_revision, pinned_finding)
            if rechecking:
                assert recheck is not None  # noqa: S101 - checked above
                self.confirmation_reads(db, review, pinned_finding, pinned[1])
                self._in_new_places(anchors, recheck, self.marks(db, found))
                result = self._answer(db, run, found, value, anchors, recheck)
            else:
                accepted = value.decision == "accept_dismissal"
                dismissal = {
                    **cast(Json, found["dismissal"]),
                    "review": {
                        "review": review,
                        "decision": "accepted" if accepted else "rejected",
                        "reason": value.explanation,
                        "at": self.store.clock(),
                    },
                }
                result = self.transition(
                    db,
                    found,
                    "dismissed",
                    actor,
                    value.explanation,
                    {"review": review, "anchors": anchors, "decision": value.decision},
                    columns={"dismissal": dismissal},
                )
                db.execute(
                    "INSERT INTO review_records(review_id,kind,payload,created) "
                    "VALUES(?,'dismissal',?,?)",
                    (
                        review,
                        dump(
                            {
                                "finding_id": value.finding,
                                "decision": "accepted" if accepted else "rejected",
                            }
                        ),
                        self.store.clock(),
                    ),
                )
            self.record_operation(db, project, actor, value.operation_id, fingerprint, result)
        return result

    @staticmethod
    def _pinned_body(
        db: sqlite3.Connection,
        review: str,
        finding: str,
        response_revision: object,
        pinned_finding: Json,
    ) -> None:
        """Refuse a decision when the author's explanation, or its macro context, changed
        since the snapshot pinned them, or the context is held or unknown."""
        from .response_bodies import pin as response_pin

        if type(response_revision) is not int:
            return
        current_body = response_pin(db, finding, response_revision)
        if current_body is None:
            return
        body_pin = db.execute(
            "SELECT body_revision,sha256 FROM review_response_pins WHERE review_id=? AND "
            "finding_id=? AND decision_revision=?",
            (review, finding, response_revision),
        ).fetchone()
        if body_pin != (current_body["revision"], current_body["sha256"]):
            raise DocumentError(CONFLICT, "response explanation changed during the review")
        macro = current_body["macro_context"]
        pinned_macro = db.execute(
            "SELECT macro_revision FROM review_response_pins WHERE review_id=? AND finding_id=?",
            (review, finding),
        ).fetchone()
        if pinned_macro != (macro["revision"],):
            raise DocumentError(CONFLICT, "response macro context changed during the review")
        response = cast(Json, pinned_finding.get("response") or {})
        raw_context = response.get("macro_context")
        context = cast(Json, raw_context) if isinstance(raw_context, dict) else {}
        # The snapshot resolved these exact immutable anchors and setting. Overall review
        # coverage is a separate guard; ambiguity must not become a definitive decision.
        if (
            context.get("known") is not True
            or context.get("held") is not False
            or context.get("key") != digest(macro_dump({**macro, "known": True}))
            or any(a.get("unknown") for a in macro["anchors"])
        ):
            raise DocumentError(
                POLICY_DENIED,
                "response macro context is held or unknown; leave the decision to a later review",
            )

    @staticmethod
    def _in_new_places(anchors: list[Json], recheck: Json, marks: Marks) -> None:
        """Each of a re-check's evidence anchors is strictly later than what the finding
        reviewed, and lies in the new revision or message the re-check holds (CR-42): a
        block or line range of a later document revision, a field of a later entry
        revision, a later artifact revision, or a later archive message."""
        places = cast(list[Json], recheck.get("places") or [])
        messages = {str(m.get("id")) for m in cast(list[Json], recheck.get("messages") or [])}
        documents = {
            (p["document"]["document"], p["document"]["revision"])
            for p in places
            if "document" in p
        }
        artifacts = {
            (p["artifact"]["artifact_id"], p["artifact"]["revision"])
            for p in places
            if "artifact" in p
        }
        entries = {(p["entry"]["entry"], p["entry"]["revision"]) for p in places if "entry" in p}
        for anchor in anchors:
            location = cast(Json, anchor["locator"])
            try:
                marks.later(location)
            except DocumentError:
                raise DocumentError(
                    POLICY_DENIED,
                    "a re-check's evidence lies in the new revision or a later message; "
                    f"{anchor['evidence']} is not later than what the finding reviewed",
                ) from None
            artifact = cast(Json, location.get("artifact") or {})
            archived = cast(Json, location.get("archive") or {})
            if (
                (location.get("document"), location.get("revision")) in documents
                or (artifact.get("artifact_id"), artifact.get("revision")) in artifacts
                or (location.get("entry"), location.get("revision")) in entries
                or str(archived.get("id")) in messages
            ):
                continue
            raise DocumentError(
                POLICY_DENIED,
                "a re-check's evidence lies in the new revision or message it holds; "
                f"{anchor['evidence']} is elsewhere",
            )

    def _answer(
        self,
        db: sqlite3.Connection,
        run: Json,
        found: Json,
        value: ConfirmationInput,
        anchors: list[Json],
        recheck: Json,
    ) -> Json:
        """Record a re-check's answer: ``fixed`` on ``resolved``; on ``unresolved`` a
        successor on the new revision and the finding ``superseded`` (CR-42, CR-44)."""
        review = run["review_id"]
        actor = f"review:{review}"
        record: Json = {
            "review": review,
            "answer": value.decision,
            "why": value.explanation,
            "anchors": anchors,
            "places": recheck.get("places", []),
            "messages": recheck.get("messages", []),
            "reviewer": {
                "alias": run["alias"],
                "resolved_model": run["resolved_model"],
                "rubric": run["rubric"],
            },
            "at": self.store.clock(),
        }
        payload = {"review": review, "anchors": anchors, "decision": value.decision}
        if value.decision == "resolved":
            return self.transition(
                db,
                found,
                "fixed",
                actor,
                value.explanation,
                payload,
                answer="resolved",
                recheck=record,
                columns={"latest_recheck": record, "recheck": None},
            )
        successor = self._raise(
            db,
            found["project"],
            found["session"],
            review,
            FindingInput(
                severity=found["severity"],
                category=found["category"],
                statement=value.explanation,
                anchors=value.anchors,
                remedy=found["remedy"],
                operation_id=value.operation_id,
            ),
            anchors,
            dedupe_of(found["category"], anchors),
            supersedes=found["finding_id"],
            reason=f"The re-check of {found['finding_id']} found it unresolved on the new "
            "revision.",
        )
        db.execute(
            "INSERT INTO review_records(review_id,kind,payload,created) VALUES(?,'recheck',?,?)",
            (
                review,
                dump({"finding_id": found["finding_id"], "successor": successor["finding_id"]}),
                self.store.clock(),
            ),
        )
        record["successor"] = successor["finding_id"]
        result = self.transition(
            db,
            found,
            "superseded",
            actor,
            value.explanation,
            {**payload, "successor": successor["finding_id"]},
            answer="unresolved",
            recheck=record,
            columns={
                "latest_recheck": record,
                "recheck": None,
                "superseded_by": successor["finding_id"],
            },
        )
        return {**result, "successor": successor["finding_id"]}

    def inconclusive(self, db: sqlite3.Connection, run: Json, found: Json, why: str) -> Json | None:
        """Record a re-check that could not decide (capped, failed, or no answer with a
        valid anchor) as the finding's latest re-check, ``inconclusive`` and why; the
        finding stays as it is, and its re-check is tried at the next dispatch, at most
        ``RECHECK_ATTEMPTS`` times in each session (CR-42)."""
        recheck = cast(Json | None, found.get("recheck"))
        if recheck is None or found["status"] not in ("open", "stale"):
            return None
        review = run["review_id"]
        record: Json = {
            "review": review,
            "answer": "inconclusive",
            "why": why,
            "anchors": [],
            "places": recheck.get("places", []),
            "messages": recheck.get("messages", []),
            "reviewer": {
                "alias": run["alias"],
                "resolved_model": run["resolved_model"],
                "rubric": run["rubric"],
            },
            "at": self.store.clock(),
        }
        attempts = int(recheck.get("attempts", 0)) + 1
        return self.transition(
            db,
            found,
            found["status"],
            f"review:{review}",
            why,
            {"review": review, "decision": "inconclusive", "attempt": attempts},
            notify=False,
            answer="inconclusive",
            recheck=record,
            columns={"latest_recheck": record, "recheck": {**recheck, "attempts": attempts}},
        )

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
        # A re-check holds the places that replaced what the finding reviewed, as a fix
        # names its changed revisions: each is read before the answer (CR-42).
        places = cast(list[Json], cast(Json, pinned.get("recheck") or {}).get("places", []))
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
        artifact_pins = response.get("artifact_revisions", []) + [
            place["artifact"] for place in places if "artifact" in place
        ]
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
        entries = cast(list[Json], response.get("entry_revisions", [])) + [
            place["entry"] for place in places if "entry" in place
        ]
        if entries:
            ledger_items = [
                (evidence, stored_json(locator), complete)
                for evidence, locator, complete in db.execute(
                    "SELECT evidence_id,locator,complete FROM review_evidence e "
                    "WHERE kind='ledger' AND (review_id=? OR EXISTS(SELECT 1 FROM "
                    "review_grants g WHERE g.review_id=? AND g.evidence_id=e.evidence_id))",
                    (review, review),
                )
            ]
            for pin in entries:
                if not any(
                    complete
                    and covered(evidence)
                    and (locator.get("entry"), locator.get("revision"))
                    == (pin["entry"], pin["revision"])
                    for evidence, locator, complete in ledger_items
                ):
                    raise DocumentError(
                        POLICY_DENIED, "read the pinned changed entry revision before deciding"
                    )
        revisions = cast(list[Json], response.get("revisions", [])) + [
            place["document"] for place in places if "document" in place
        ]
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
        """End bounded author rounds: each open finding delivered and not settled stays
        ``open`` at its re-flag bound, not injected again (CR-32 as CR-47 reads it)."""
        with self.store.documents.control.write() as tx:
            db = tx.sql
            ids = [
                r[0]
                for r in db.execute(
                    (
                        "SELECT finding_id FROM review_findings WHERE project=? AND "
                        "session=? AND status='open' AND at_bound=0 AND category!='math'"
                    ),
                    (project, session),
                )
            ]
            for finding in ids:
                if notices is not None and not any(n.startswith(finding + "-r") for n in notices):
                    continue
                found = self.load(db, project, session, finding)
                self.transition(
                    db,
                    found,
                    "open",
                    "platform:review-bound",
                    reason,
                    {"bound": True},
                    notify=False,
                    columns={"at_bound": True},
                )

    def _ledger_replacement(self, project: str, anchor: Json) -> Json | None:
        """The entry's current revision, when it replaces the anchored one: it is newer,
        it changed what a decision rests on (its statement, fields, ``assumes`` links or
        evidence that can count, LG-17), and it changed the anchored field when the
        anchor names one (CR-28 as amended); None otherwise. A new title, status or other
        links replace nothing."""
        from sandbox_science.orchestrator.ledger import Ledger

        location, field = anchor["locator"], anchor.get("field")
        new = Ledger(self.store.documents).entry(project, location["entry"])
        if new.revision <= location["revision"] or not self._entry_changed(
            project, new.entry_id, location["revision"], field, new.revision, fix=False
        ):
            return None
        return {
            "entry": new.entry_id,
            "handle": new.handle,
            "revision": new.revision,
            "field": field,
        }

    def _entry_changed(
        self,
        project: str,
        entry: str,
        anchored: int,
        field: str | None,
        revision: int,
        *,
        fix: bool,
    ) -> bool:
        """Whether ``revision`` of ``entry`` changed what an anchor on revision
        ``anchored`` is about: the field it names, when it names one, and what a decision
        rests on (its statement, fields, ``assumes`` links and evidence that bears on it,
        LG-17). A fix of a finding on the title, status or links needs only that field
        changed; supersession needs both, so a new title replaces nothing."""
        from sandbox_science.orchestrator.ledger import Ledger, gate

        ledger = Ledger(self.store.documents)
        old, new = ledger.entry(project, entry, anchored), ledger.entry(project, entry, revision)
        if field is not None and getattr(old, field) == getattr(new, field):
            return False
        if fix and field in _OFF_MATERIAL:
            return True
        with self.store.documents.control.read() as tx:
            held = gate.examined(tx.sql, entry, {anchored, revision})
        return held[anchored] != held[revision]

    def _replacements(self, project: str, session: str, finding: Json) -> list[Json]:
        """What replaced what ``finding`` reviewed, each a later revision recorded since:
        of a document block it is anchored to that changed or went, of an artifact, or of
        a ledger entry's field (CR-42; LD-25, LG-25 as CR-28 reads an anchor). Anchors on
        messages and executions have no later revision and replace nothing."""
        places: list[Json] = []
        seen: set[str] = set()
        for a in finding["anchors"]:
            location = a["locator"]
            place: Json | None = None
            if "entry" in location and "revision" in location:
                replacement = self._ledger_replacement(project, a)
                if replacement is not None:
                    place = {"entry": replacement}
            elif isinstance(location.get("artifact"), dict) and self.store.artifacts is not None:
                old_ref = ArtifactRevisionRef.model_validate(location["artifact"])
                latest = self.store.artifacts.resolve(
                    project,
                    ArtifactLink(
                        project_id=project, artifact_id=old_ref.artifact_id, mode="latest"
                    ),
                ).reference
                if latest.revision > old_ref.revision:
                    try:
                        self.store.artifact_pin(project, session, latest)
                    except DocumentError:
                        # No cross-session grant is inferred from a shared artifact head.
                        continue
                    place = {"artifact": latest.model_dump(mode="json")}
            elif "document" in location and type(location.get("revision")) is int:
                document, number = location["document"], location["revision"]
                current = self.store.documents.document(project, document)
                if current.head <= number:
                    continue
                if a.get("block") is not None:
                    old = self.store.documents.mapped(project, document, number)
                    new = self.store.documents.mapped(project, document, current.head)
                    old_i, new_i = old.index(a["block"]), new.index(a["block"])
                    if (
                        old_i is not None
                        and new_i is not None
                        and old.parsed.blocks[old_i].text == new.parsed.blocks[new_i].text
                    ):
                        continue
                place = {
                    "document": {
                        "document": document,
                        "revision": current.head,
                        "sha256": self.store.documents.revision(
                            project, document, current.head
                        ).info.sha256,
                        "block": a.get("block"),
                    }
                }
            if place is not None and (key := dump(place)) not in seen:
                seen.add(key)
                places.append(place)
        return places

    def _reviewed(
        self, project: str, session: str, statuses: tuple[str, ...], *, elsewhere: bool = False
    ) -> list[Json]:
        """The review findings in ``statuses`` whose re-check the platform may queue: not
        math (its check re-checks it, CR-38) and not suggestions; of the session, or with
        ``elsewhere`` of the project's other sessions. All of them, read a page at a time."""
        self.store.scope(project, session)
        found: list[Json] = []
        after = 0
        while True:
            with self.store.documents.control.read() as tx:
                db = tx.sql
                rows = db.execute(
                    "SELECT rowid,finding_id,session FROM review_findings WHERE project=? "
                    "AND (session=?)=? AND origin='review' "
                    "AND category NOT IN ('math','handle_citation') "
                    "AND status IN (SELECT value FROM json_each(?)) AND rowid>? "
                    "ORDER BY rowid LIMIT 100",
                    (project, session, int(not elsewhere), dump(list(statuses)), after),
                ).fetchall()
                found.extend(self.load(db, project, row[2], row[1]) for row in rows)
            if len(rows) < 100:
                return found
            after = rows[-1][0]

    def load_in(self, db: sqlite3.Connection, project: str, finding: str) -> Json:
        """A finding of the project, in whichever of its sessions it was raised."""
        identifier(finding, "finding")
        row = db.execute(
            "SELECT session FROM review_findings WHERE project=? AND finding_id=?",
            (project, finding),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "no such scoped finding")
        return self.load(db, project, row[0], finding)

    @staticmethod
    def rechecked_in(db: sqlite3.Connection, finding: str, session: str) -> int:
        """How many re-checks of ``finding`` the session's reviews answered or recorded as
        inconclusive; attempts count per session, not per queue entry (CR-42, CR-44)."""
        return db.execute(
            "SELECT count(*) FROM review_finding_history h JOIN review_runs r "
            "ON r.review_id=h.review_id WHERE h.finding_id=? AND h.answer IS NOT NULL "
            "AND r.session=?",
            (finding, session),
        ).fetchone()[0]

    def queue_rechecks(self, project: str, session: str) -> int:
        """At a review dispatch in ``session``, queue for its reviews the re-check of each
        of its open or stale findings, and each stale finding of the project's other
        sessions, whose reviewed artifact, block or field has a later revision (CR-42,
        CR-44): a replaced revision no longer supersedes a finding by itself (CR-28 as
        decided), and a stale finding is re-checked by a later session's review. One
        already queued for this session keeps its queue; one queued elsewhere moves here,
        keeping what it held, such as the author's fix. How many it queued."""
        prepared: list[tuple[Json, Json]] = []
        for finding in [
            *self._reviewed(project, session, ("open", "stale")),
            *self._reviewed(project, session, ("stale",), elsewhere=True),
        ]:
            queued = cast(Json | None, finding.get("recheck"))
            if queued is not None and queued.get("session", finding["session"]) == session:
                continue
            places = _unique(
                [
                    *cast(list[Json], (queued or {}).get("places", [])),
                    *self._replacements(project, session, finding),
                ]
            )
            messages = cast(list[Json], (queued or {}).get("messages", []))
            if not places and not messages:
                continue
            prepared.append(
                (
                    finding,
                    {
                        "cause": (queued or {}).get("cause", "revision"),
                        "response": (queued or {}).get("response"),
                        "places": places,
                        "messages": messages,
                        "session": session,
                        "attempts": 0,
                    },
                )
            )
        queued_now = 0
        with self.store.documents.control.write() as tx:
            db = tx.sql
            for finding, recheck in prepared:
                current = self.load(db, project, finding["session"], finding["finding_id"])
                if current["revision"] != finding["revision"] or current["status"] not in (
                    "open",
                    "stale",
                ):
                    continue
                recheck = {
                    **recheck,
                    "revision": current["revision"] + 1,
                    "queued": self.store.clock(),
                }
                self.transition(
                    db,
                    current,
                    current["status"],
                    "platform:recheck",
                    "A later revision of what it reviewed was recorded; its re-check is "
                    f"queued for the next review of session {session}.",
                    {"recheck": "queued", "places": recheck["places"], "session": session},
                    notify=False,
                    columns={"recheck": recheck},
                )
                queued_now += 1
        return queued_now

    def stale(self, project: str, session: str) -> int:
        """At the session's close, each open finding whose reviewed artifact, block or
        field has a newer revision, and of which no re-check ran in the session, becomes
        ``stale`` (CR-44); one anchored only to messages never does. How many."""
        prepared: list[Json] = []
        for finding in self._reviewed(project, session, ("open",)):
            if messages_only(finding["anchors"]):
                continue
            with self.store.documents.control.read() as tx:
                if self.rechecked_in(tx.sql, finding["finding_id"], session):
                    continue  # a re-check ran here, if it could not decide
            if not self._replacements(project, session, finding):
                continue
            prepared.append(finding)
        made = 0
        with self.store.documents.control.write() as tx:
            db = tx.sql
            for finding in prepared:
                current = self.load(db, project, session, finding["finding_id"])
                if current["revision"] != finding["revision"] or current["status"] != "open":
                    continue
                self.transition(
                    db,
                    current,
                    "stale",
                    "platform:session-close",
                    "The session closed: what it reviewed has a newer revision, and no "
                    "re-check of it ran.",
                    {"session_closed": True},
                    notify=False,
                )
                made += 1
        return made
