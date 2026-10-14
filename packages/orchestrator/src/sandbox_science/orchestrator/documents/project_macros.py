# SPDX-License-Identifier: MIT
"""Versioned USER project macros, and the macro contexts texts are checked and shown
with (LD-28).

Settings writes only append a bounded revision. A document revision, a math check, a
response body revision and a review's response pin record the setting revision they
used (decision 7); the effective context (anchors, definitions, problems, whether it
is held) is derived from that and the immutable sources when read. Current-source
refresh is lazy in the existing math admission loop; neither settings nor source saves
render math.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import Field, JsonValue, ValidationError

from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.control import Receipts

from .errors import CONFLICT, INTERNAL, INVALID_REQUEST, POLICY_DENIED, DocumentError
from .frontmatter import MAX_MACRO, MAX_MACROS, bounded_macros
from .models import Author

if TYPE_CHECKING:
    from .store import DocumentStore

Json = dict[str, Any]
MAX_BYTES = 16 * 1024
MAX_HISTORY = 1024
_NAME = re.compile(r"\\(?:[A-Za-z]+|[^A-Za-z\s])\Z")
EMPTY_SHA = hashlib.sha256(b"{}").hexdigest()
_RECEIPTS = Receipts("macros", "operation ID already names another macro change")


class MacroContext(Contract):
    """The macros a text was checked or is shown with: the project's setting revision
    and the anchors it was pinned by, the effective definitions, and why any are held.
    A math check's pinned context also names the ``effective`` definitions it used."""

    project: str
    revision: int
    sha256: str
    macros: dict[str, str]
    known: bool
    anchors: list[dict[str, JsonValue]]
    problems: list[str]
    held: bool
    key: str
    effective: dict[str, str] | None = None


class MacroChange(Contract):
    expected: int = Field(ge=0, le=999999999, strict=True)
    macros: dict[str, str]
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def validate(definitions: dict[str, str]) -> dict[str, str]:
    try:
        size = len(dump(definitions).encode())
    except UnicodeEncodeError:
        raise DocumentError(INVALID_REQUEST, "macro definitions must be valid UTF-8") from None
    if len(definitions) > MAX_MACROS or size > MAX_BYTES:
        raise DocumentError(INVALID_REQUEST, "project macros exceed the count or byte limit")
    if any(
        _NAME.fullmatch(name) is None or len(name) > 128 or len(body) > MAX_MACRO
        for name, body in definitions.items()
    ):
        raise DocumentError(INVALID_REQUEST, "macro names and definitions exceed their bounds")
    problems: list[str] = []
    result = bounded_macros(definitions, problems)
    if problems:
        raise DocumentError(INVALID_REQUEST, "; ".join(problems)[:1000])
    return result


def resolve(project: dict[str, str], document: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Document definitions win; combined expansion/count limits are checked anew."""
    # Preserve document overrides even when two individually bounded sets exceed
    # the total limit. Exclusions remain visible rather than silently unbounded.
    combined = {**document, **{k: v for k, v in project.items() if k not in document}}
    problems: list[str] = []
    if len(combined) > MAX_MACROS:
        combined = dict(list(combined.items())[:MAX_MACROS])
        problems.append(f"only {MAX_MACROS} effective macros are used; document definitions win")
    return bounded_macros(combined, problems), problems


def head(db: sqlite3.Connection | None, project: str) -> int:
    if db is None:
        return 0
    row = db.execute(
        "SELECT revision FROM project_macro_heads WHERE project=?", (project,)
    ).fetchone()
    revision = 0 if row is None else row[0]
    if type(revision) is not int or not 0 <= revision <= 999999999:
        raise DocumentError(INTERNAL, "invalid retained project macro head")
    return revision


def read(db: sqlite3.Connection | None, project: str, revision: int | None = None) -> Json:
    revision = head(db, project) if revision is None else revision
    if type(revision) is not int or not 0 <= revision <= 999999999:
        raise DocumentError(INTERNAL, "invalid project macro revision")
    if revision == 0:
        return {"project": project, "revision": 0, "sha256": EMPTY_SHA, "macros": {}}
    if db is None:
        raise DocumentError(INTERNAL, "pinned project macro revision is missing")
    row = db.execute(
        "SELECT definitions,sha256 FROM project_macro_revisions WHERE project=? AND revision=?",
        (project, revision),
    ).fetchone()
    if row is None:
        raise DocumentError(INTERNAL, "pinned project macro revision is missing")
    return {
        "project": project,
        "revision": revision,
        "sha256": row[1],
        "macros": cast(dict[str, str], json.loads(row[0])),
    }


class ProjectMacros:
    def __init__(self, documents: DocumentStore) -> None:
        self.documents = documents

    def get(self, project: str, revision: int | None = None) -> Json:
        self._project(project)
        if self.documents.control.missing:
            return read(None, project, revision)
        with self.documents.control.read() as tx:
            return read(tx.sql, project, revision)

    def change(self, project: str, value: object, author: Author) -> Json:
        self._project(project)
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only an explicit USER changes project macros")
        try:
            change = MacroChange.model_validate(value)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid project macro change") from None
        definitions = validate(change.macros)
        encoded = dump(definitions)
        fingerprint = hashlib.sha256(dump(change.model_dump()).encode()).hexdigest()
        with self.documents.control.write() as tx:
            db = tx.sql
            replay = _RECEIPTS.find(db, project, author.actor, change.operation_id, fingerprint)
            if replay is not None:
                return {**read(db, project, cast(int, replay["revision"])), "replayed": True}
            current = read(db, project)
            if current["revision"] != change.expected:
                raise DocumentError(
                    CONFLICT, "project macros changed; retain the draft and review current settings"
                )
            revision = change.expected
            if current["macros"] != definitions:
                if revision >= MAX_HISTORY:
                    raise DocumentError(CONFLICT, "project macro history quota reached")
                revision += 1
                db.execute(
                    "INSERT INTO project_macro_revisions VALUES(?,?,?,?,?)",
                    (
                        project,
                        revision,
                        encoded,
                        hashlib.sha256(encoded.encode()).hexdigest(),
                        author.model_dump_json(),
                    ),
                )
                db.execute(
                    "INSERT INTO project_macro_heads VALUES(?,?) ON CONFLICT(project) "
                    "DO UPDATE SET revision=excluded.revision",
                    (project, revision),
                )
            if _RECEIPTS.count(db, project) >= 4096:
                raise DocumentError(CONFLICT, "project macro operation quota reached")
            _RECEIPTS.record(
                db, project, author.actor, change.operation_id, fingerprint, {"revision": revision}
            )
            return {**read(db, project, revision), "replayed": False}

    @staticmethod
    def _project(project: object) -> None:
        if (
            not isinstance(project, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project) is None
        ):
            raise DocumentError(INVALID_REQUEST, "invalid project macro scope")


def pin(db: sqlite3.Connection, check: str, project: str) -> None:
    """Record the setting revision a new math check uses: the current one."""
    db.execute("UPDATE math_jobs SET macro_revision=? WHERE check_id=?", (head(db, project), check))


def job_context(db: sqlite3.Connection | None, check: str, project: str) -> Json:
    """The setting a check used and the anchors its macros come from: an explanation's
    finding's document anchors, or none."""
    row = (
        None
        if db is None
        else db.execute(
            "SELECT j.macro_revision,r.subject_id FROM math_jobs j LEFT JOIN math_text_jobs r "
            "ON r.check_id=j.check_id AND r.subject_type='response' WHERE j.check_id=?",
            (check,),
        ).fetchone()
    )
    if db is None or row is None or row[0] is None:
        return {**read(db, project, 0), "known": False, "anchors": []}
    anchors = [] if row[1] is None else response_anchors(db, row[1])
    return {**read(db, project, row[0]), "known": True, "anchors": anchors}


def current(db: sqlite3.Connection, check: str, project: str) -> bool:
    """Whether a check used the current setting and resolved its context unheld."""
    context = job_context(db, check, project)
    return bool(
        context["known"]
        and context["revision"] == head(db, project)
        and db.execute(
            "SELECT 1 FROM math_jobs WHERE check_id=? AND macro_held=0", (check,)
        ).fetchone()
    )


_SEVERITY_RANK = {"fail": 0, "warn": 1, "note": 2}
"""As the consistency review ranks severities: a lower rank is more severe."""


def response_anchors(db: sqlite3.Connection, source: str) -> list[Json]:
    """An explanation's macro anchors: the documents its finding was anchored to when
    the explanation was given (its decision revision), or unknown when its first body
    revision came from a writer that recorded no macros. A later reflag of the finding
    does not change them."""
    row = db.execute(
        "SELECT f.project,f.finding_id,b.parent_revision,b.macro_revision "
        "FROM text_revisions b JOIN review_findings f ON f.finding_id=b.parent "
        "WHERE b.subject_type='response' AND b.subject_id=? ORDER BY b.revision LIMIT 1",
        (source,),
    ).fetchone()
    return explanation_anchors(db, *row) if row else [{"unknown": True}]


def explanation_anchors(
    db: sqlite3.Connection, project: str, finding: str, decision: int, macros: int | None
) -> list[Json]:
    """The macro anchors of an explanation of ``finding`` given at its revision
    ``decision``, whose first body revision recorded the macro revision ``macros``."""
    if macros is None:
        return [{"unknown": True}]
    anchors = header_anchors(db, finding, decision)
    if anchors is None:
        return finding_anchors(db, project, finding)
    return anchor_pins(db, project, anchors)


def header_anchors(db: sqlite3.Connection, finding: str, revision: int) -> object | None:
    """The anchors a review finding's header held at ``revision``, from its last
    assessment then: a reflag's assessment replaced them when it was at least as severe
    as the header, and kept them otherwise. None when no transition recorded one."""
    row = db.execute(
        "SELECT payload FROM review_transitions WHERE finding_id=? AND revision<=? "
        "AND json_extract(payload,'$.assessment') IS NOT NULL ORDER BY revision DESC LIMIT 1",
        (finding, revision),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = cast(Json, json.loads(row[0]))
        new, old = cast(Json, payload["assessment"]), payload.get("previous_assessment")
        if not isinstance(old, dict):
            return new["anchors"]
        old = cast(Json, old)
        stronger = _SEVERITY_RANK[new["severity"]] <= _SEVERITY_RANK[old["severity"]]
        return new["anchors"] if stronger else old["anchors"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return [{"unknown": True}]


def finding_anchors(db: sqlite3.Connection, project: str, finding: str) -> list[Json]:
    """The documents a finding is anchored to now."""
    row = db.execute(
        "SELECT anchors FROM review_findings WHERE finding_id=? AND project=?",
        (finding, project),
    ).fetchone()
    if row is None:
        return [{"unknown": True}]
    try:
        return anchor_pins(db, project, json.loads(row[0]))
    except (ValueError, RecursionError):
        return [{"unknown": True}]


def anchor_pins(db: sqlite3.Connection, project: str, anchors: object) -> list[Json]:
    """The document revisions a finding's anchors name, each once, in their order."""
    try:
        if not isinstance(anchors, list) or len(cast(list[Any], anchors)) > 32:
            raise ValueError
        pins: list[Json] = []
        for anchor in cast(list[Json], anchors):
            if anchor.get("unknown"):
                raise ValueError
            locator = anchor.get("locator", {})
            if "document" not in locator:
                continue
            document, revision, sha = (
                locator.get("document"),
                locator.get("revision"),
                locator.get("sha256"),
            )
            if (
                not isinstance(document, str)
                or re.fullmatch(r"doc-[0-9a-f]{24}", document) is None
                or type(revision) is not int
                or not 1 <= revision <= 999999999
            ):
                raise ValueError
            saved = db.execute(
                "SELECT sha256 FROM revisions r JOIN documents d "
                "USING(document_id) WHERE d.project_id=? AND r.document_id=? AND r.number=?",
                (project, document, revision),
            ).fetchone()
            if saved is None or (sha is not None and sha != saved[0]):
                raise ValueError
            pin = {"document": document, "revision": revision, "sha256": saved[0]}
            if pin not in pins:
                pins.append(pin)
        if len(pins) > 8:
            raise ValueError
        return pins
    except (ValueError, TypeError, AttributeError, RecursionError):
        # This sentinel retains uncertainty without inventing read authority.
        return [{"unknown": True}]


def anchored_macros(
    documents: DocumentStore, project: str, context: Json
) -> tuple[dict[str, str], list[str], bool]:
    effective: dict[str, str] | None = None
    problems: list[str] = []
    if not context["known"]:
        return context["macros"], ["Historical project macro context is unknown"], True
    for anchor in context["anchors"]:
        if anchor.get("unknown"):
            return context["macros"], ["Anchored macro context is unknown; checks are held"], True
        try:
            revision = documents.revision(project, anchor["document"], anchor["revision"])
            if revision.info.sha256 != anchor["sha256"]:
                raise DocumentError(INTERNAL, "macro anchor does not bind its immutable source")
            mapped = documents.mapped(project, anchor["document"], anchor["revision"])
        except DocumentError:
            return (
                context["macros"],
                ["Anchored document macro context cannot be proved; checks are held"],
                True,
            )
        definitions, warnings = resolve(
            context["macros"],
            mapped.parsed.front_matter.macros if mapped.parsed.front_matter else {},
        )
        problems.extend(warnings)
        if effective is not None and effective != definitions:
            return (
                context["macros"],
                ["Conflicting anchored document macro contexts; checks are held"],
                True,
            )
        effective = definitions
    return context["macros"] if effective is None else effective, problems, False


def view(
    documents: DocumentStore,
    project: str,
    *,
    document: str | None = None,
    revision: int | None = None,
    historical: bool = False,
    anchors: list[Json] | None = None,
    project_revision: int | None = None,
) -> Json:
    with documents.control.read() as tx:
        db = tx.sql
        setting = read(db, project)
        known = True
        if project_revision is not None:
            setting = read(db, project, project_revision)
        elif historical and document is None:
            # No immutable source-to-setting pin is available for legacy prose.
            known, setting = False, read(db, project, 0)
        elif historical and document is not None:
            row = db.execute(
                "SELECT macro_revision FROM revisions WHERE document_id=? AND number=?",
                (document, revision),
            ).fetchone()
            pinned: int | None = None if row is None else row[0]
            known = pinned is not None
            setting = read(db, project, 0 if pinned is None else pinned)
    pins = anchors or []
    if document is not None and revision is not None:
        saved = documents.revision(project, document, revision)
        pins = [{"document": document, "revision": revision, "sha256": saved.info.sha256}]
    context = {**setting, "known": known, "anchors": pins}
    definitions, problems, held = anchored_macros(documents, project, context)
    return {
        **context,
        "macros": definitions,
        "problems": problems,
        "held": held,
        "key": hashlib.sha256(dump(context).encode()).hexdigest(),
    }


def response_view(
    documents: DocumentStore, project: str, source: str, *, body_revision: int | None = None
) -> Json:
    with documents.control.read() as tx:
        db = tx.sql
        anchors = response_anchors(db, source)
        saved = (
            db.execute(
                "SELECT macro_revision FROM text_revisions WHERE subject_type='response' "
                "AND subject_id=? AND revision=?",
                (source, body_revision),
            ).fetchone()
            if body_revision is not None
            else None
        )
    return view(
        documents,
        project,
        anchors=anchors,
        historical=body_revision is not None,
        project_revision=saved[0] if saved else None,
    )


def resolved(context: Json, definitions: dict[str, str], problems: list[str], held: bool) -> Json:
    """A check's effective context: its setting and anchors with the definitions,
    problems and held state they resolve to, and a key over all of them."""
    content: Json = {**context, "effective": definitions, "problems": problems, "held": held}
    return {**content, "key": hashlib.sha256(dump(content).encode()).hexdigest()}


def save_resolution(db: sqlite3.Connection, check: str, held: bool) -> None:
    """Record whether a check's macro context is held; an attempt resolves it once."""
    row = db.execute("SELECT macro_held FROM math_jobs WHERE check_id=?", (check,)).fetchone()
    if row is not None and row[0] is not None and bool(row[0]) != held:
        raise DocumentError(INTERNAL, "effective macro context changed within an attempt")
    db.execute("UPDATE math_jobs SET macro_held=? WHERE check_id=?", (int(held), check))


_TEXT_CHECK: Final = (
    "EXISTS(SELECT 1 FROM math_text_jobs x WHERE x.check_id=j.check_id "
    "AND x.subject_type=s.subject_type AND x.subject_id=s.subject_id)"
)


def _sources() -> tuple[tuple[str, str, str], ...]:
    return (
        (
            "math_sources",
            "j.document=s.document AND NOT EXISTS(SELECT 1 FROM math_text_jobs x "
            "WHERE x.check_id=j.check_id)",
            "1",
        ),
        ("text_math_sources", _TEXT_CHECK, "s.subject_type='comment' AND s.target>0"),
        ("text_math_sources", _TEXT_CHECK, "s.subject_type='response' AND s.target>0"),
    )


def _candidates(table: str, join: str, condition: str) -> str:
    # All fragments come exclusively from _sources, never caller text.
    return (
        f"SELECT s.rowid FROM {table} s "  # noqa: S608 - fixed schema names
        f"WHERE s.project=? AND s.completed=s.target AND s.retry_requested=0 AND {condition} AND "
        f"EXISTS(SELECT 1 FROM math_jobs j WHERE {join} AND j.revision=s.target) AND "
        f"NOT EXISTS(SELECT 1 FROM math_jobs j WHERE {join} "
        "AND j.revision=s.target AND j.macro_revision=?) ORDER BY s.rowid LIMIT 32"
    )


def needs_refresh(db: sqlite3.Connection, project: str) -> bool:
    for table, join, condition in _sources():
        if db.execute(
            "SELECT 1 FROM (" + _candidates(table, join, condition) + ")",  # noqa: S608 - fixed SQL
            (project, head(db, project)),
        ).fetchone():
            return True
    return False


def refresh(db: sqlite3.Connection, project: str) -> None:
    """Admit at most 32 current sources per source kind; write no backlog copies."""
    revision = head(db, project)
    for table, join, condition in _sources():
        db.execute(
            f"UPDATE {table} SET retry_requested=1 WHERE rowid IN ("  # noqa: S608 - fixed schema
            + _candidates(table, join, condition)
            + ")",
            (project, revision),
        )
