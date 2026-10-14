# SPDX-License-Identifier: MIT
"""Versioned USER project macros and immutable math-context pins (LD-28).

Settings writes only append a bounded revision. Current-source refresh is lazy in
the existing math admission loop; neither settings nor source saves render math.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import TYPE_CHECKING, Any, cast

from pydantic import Field, ValidationError

from sandbox_science.contracts.model import Contract

from .errors import CONFLICT, INTERNAL, INVALID_REQUEST, POLICY_DENIED, DocumentError
from .frontmatter import MAX_MACRO, MAX_MACROS, bounded_macros
from .models import Author

if TYPE_CHECKING:
    from .store import DocumentStore

Json = dict[str, Any]
MAX_BYTES = 16 * 1024
MAX_HISTORY = 1024
MAX_CONTEXT_BYTES = 64 * 1024 * 1024
_NAME = re.compile(r"\\(?:[A-Za-z]+|[^A-Za-z\s])\Z")
EMPTY_SHA = hashlib.sha256(b"{}").hexdigest()


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
        "SELECT substr(CAST(definitions AS BLOB),1,16385),substr(CAST(sha256 AS BLOB),1,65) "
        "FROM project_macro_revisions WHERE project=? AND revision=?",
        (project, revision),
    ).fetchone()
    try:
        if row is None or len(row[0]) > MAX_BYTES or len(row[1]) != 64:
            raise ValueError
        value = json.loads(row[0])
        if not isinstance(value, dict) or any(
            not isinstance(v, str) for v in cast(Json, value).values()
        ):
            raise ValueError
        definitions = validate(cast(dict[str, str], value))
        if (
            dump(definitions).encode() != row[0]
            or hashlib.sha256(row[0]).hexdigest() != row[1].decode()
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError, DocumentError):
        raise DocumentError(
            INTERNAL, "retained project macros are invalid or exceed bounds"
        ) from None
    return {
        "project": project,
        "revision": revision,
        "sha256": row[1].decode(),
        "macros": definitions,
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
            replay = db.execute(
                "SELECT substr(CAST(fingerprint AS BLOB),1,65),revision "
                "FROM project_macro_operations "
                "WHERE project=? AND actor=? AND operation=?",
                (project, author.actor, change.operation_id),
            ).fetchone()
            if replay:
                if replay[0] != fingerprint.encode():
                    raise DocumentError(CONFLICT, "operation ID already names another macro change")
                return {**read(db, project, replay[1]), "replayed": True}
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
            if (
                db.execute(
                    "SELECT count(*) FROM project_macro_operations WHERE project=?", (project,)
                ).fetchone()[0]
                >= 4096
            ):
                raise DocumentError(CONFLICT, "project macro operation quota reached")
            db.execute(
                "INSERT INTO project_macro_operations VALUES(?,?,?,?,?)",
                (project, author.actor, change.operation_id, fingerprint, revision),
            )
            return {**read(db, project, revision), "replayed": False}

    @staticmethod
    def _project(project: object) -> None:
        if (
            not isinstance(project, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project) is None
        ):
            raise DocumentError(INVALID_REQUEST, "invalid project macro scope")


def source_saved(db: sqlite3.Connection, kind: str, document: str) -> None:
    if kind == "document.revision_created":
        db.execute(
            "INSERT INTO document_macro_pins SELECT d.document_id,d.head,coalesce(h.revision,0) "
            "FROM documents d LEFT JOIN project_macro_heads h ON h.project=d.project_id "
            "WHERE d.document_id=?",
            (document,),
        )


def pin(
    db: sqlite3.Connection, check: str, project: str, anchors: list[Json] | None = None
) -> None:
    setting = read(db, project)
    encoded = dump(anchors or [])
    if len(encoded.encode()) > 16384:
        raise DocumentError(INTERNAL, "math macro anchors exceed their bound")
    db.execute(
        "INSERT INTO math_macro_pins VALUES(?,?,?,?,?)",
        (check, project, setting["revision"], setting["sha256"], encoded),
    )


def job_context(db: sqlite3.Connection | None, check: str, project: str) -> Json:
    if db is None:
        return {**read(db, project, 0), "known": False, "anchors": []}
    row = db.execute(
        "SELECT revision,substr(CAST(sha256 AS BLOB),1,65),substr(CAST(anchors AS BLOB),1,16385) "
        "FROM math_macro_pins WHERE check_id=? AND project=?",
        (check, project),
    ).fetchone()
    if row is None:
        return {**read(db, project, 0), "known": False, "anchors": []}
    setting = read(db, project, row[0])
    try:
        if row[1] != setting["sha256"].encode() or len(row[2]) > 16384:
            raise ValueError
        anchors = _anchors(row[2])
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
        raise DocumentError(INTERNAL, "retained math macro pin is invalid") from None
    return {**setting, "known": True, "anchors": anchors}


def current(db: sqlite3.Connection, check: str, project: str) -> bool:
    context = job_context(db, check, project)
    return bool(
        context["known"]
        and context["revision"] == head(db, project)
        and db.execute(
            "SELECT 1 FROM math_macro_contexts WHERE check_id=? AND held=0", (check,)
        ).fetchone()
    )


def response_saved(
    db: sqlite3.Connection,
    parent: Json,
    revision: int,
    *,
    legacy: bool = False,
    unknown_anchors: bool = False,
) -> None:
    anchors = (
        [{"unknown": True}]
        if legacy or unknown_anchors
        else finding_anchors(db, parent["project"], parent["finding"])
    )
    db.execute(
        "INSERT OR IGNORE INTO response_macro_anchors VALUES(?,?)",
        (parent["source"], dump(anchors)),
    )
    if not legacy:
        db.execute(
            "INSERT INTO response_macro_revisions VALUES(?,?,?)",
            (parent["source"], revision, head(db, parent["project"])),
        )


def response_anchors(db: sqlite3.Connection, source: str) -> list[Json]:
    row = db.execute(
        "SELECT substr(CAST(anchors AS BLOB),1,16385) FROM response_macro_anchors WHERE source=?",
        (source,),
    ).fetchone()
    if row is None:
        return [{"unknown": True}]
    return _anchors(row[0])


def _anchors(raw: bytes) -> list[Json]:
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or len(cast(list[Any], parsed)) > 8:
            raise ValueError
        for anchor in cast(list[Any], parsed):
            if anchor == {"unknown": True}:
                continue
            if not isinstance(anchor, dict) or set(cast(Json, anchor)) != {
                "document",
                "revision",
                "sha256",
            }:
                raise ValueError
            anchor = cast(Json, anchor)
            if (
                not isinstance(anchor["document"], str)
                or re.fullmatch(r"doc-[0-9a-f]{24}", anchor["document"]) is None
                or type(anchor["revision"]) is not int
                or not 1 <= anchor["revision"] <= 999999999
                or not isinstance(anchor["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", anchor["sha256"]) is None
            ):
                raise ValueError
        return cast(list[Json], parsed)
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
        raise DocumentError(
            INTERNAL, "retained macro anchors are invalid or exceed bounds"
        ) from None


def finding_anchors(db: sqlite3.Connection, project: str, finding: str) -> list[Json]:
    row = db.execute(
        "SELECT substr(CAST(anchors AS BLOB),1,16385) FROM review_findings "
        "WHERE finding_id=? AND project=?",
        (finding, project),
    ).fetchone()
    try:
        if row is None or len(row[0]) > 16384:
            raise ValueError
        anchors = json.loads(row[0])
        if not isinstance(anchors, list) or len(cast(list[Any], anchors)) > 32:
            raise ValueError
        pins: list[Json] = []
        for anchor in cast(list[Json], anchors):
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
                "SELECT substr(CAST(sha256 AS BLOB),1,65) FROM revisions r JOIN documents d "
                "USING(document_id) WHERE d.project_id=? AND r.document_id=? AND r.number=?",
                (project, document, revision),
            ).fetchone()
            if (
                saved is None
                or re.fullmatch(b"[0-9a-f]{64}", saved[0]) is None
                or (sha is not None and sha != saved[0].decode())
            ):
                raise ValueError
            pin = {"document": document, "revision": revision, "sha256": saved[0].decode()}
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
                "SELECT project_revision FROM document_macro_pins WHERE document=? AND revision=?",
                (document, revision),
            ).fetchone()
            known = row is not None
            setting = read(db, project, 0 if row is None else row[0])
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
                "SELECT project_revision FROM response_macro_revisions "
                "WHERE source=? AND body_revision=?",
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


def resolved(db: sqlite3.Connection, check: str, context: Json) -> Json | None:
    row = db.execute(
        "SELECT substr(CAST(definitions AS BLOB),1,65537),"
        "substr(CAST(problems AS BLOB),1,32769),held,substr(CAST(context_key AS BLOB),1,65) "
        "FROM math_macro_contexts WHERE check_id=?",
        (check,),
    ).fetchone()
    if row is None:
        return None
    try:
        if (
            len(row[0]) > 65536
            or len(row[1]) > 32768
            or type(row[2]) is not int
            or row[2] not in (0, 1)
        ):
            raise ValueError
        definitions, problems = json.loads(row[0]), json.loads(row[1])
        if (
            not isinstance(definitions, dict)
            or any(not isinstance(v, str) for v in cast(Json, definitions).values())
            or len(cast(Json, definitions)) > MAX_MACROS
            or any(len(v) > MAX_MACRO for v in cast(Json, definitions).values())
        ):
            raise ValueError
        if not isinstance(problems, list) or any(
            not isinstance(p, str) for p in cast(list[Any], problems)
        ):
            raise ValueError
        content: Json = {
            **context,
            "effective": cast(Json, definitions),
            "problems": cast(list[str], problems),
            "held": bool(row[2]),
        }
        key = hashlib.sha256(dump(content).encode()).hexdigest()
        if row[3] != key.encode():
            raise ValueError
        return {**content, "key": key}
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DocumentError(INTERNAL, "retained effective macro context is invalid") from None


def save_resolution(
    db: sqlite3.Connection,
    check: str,
    context: Json,
    definitions: dict[str, str],
    problems: list[str],
    held: bool,
) -> bool:
    content = {**context, "effective": definitions, "problems": problems, "held": held}
    encoded, warnings = dump(definitions), dump(problems)
    if len(encoded.encode()) > 65536 or len(warnings.encode()) > 32768:
        raise DocumentError(INTERNAL, "effective macro context exceeds its bounds")
    existing = resolved(db, check, context)
    if existing is not None:
        if (
            existing["effective"] != definitions
            or existing["held"] != held
            or existing["problems"] != problems
        ):
            raise DocumentError(INTERNAL, "effective macro context changed within an attempt")
        return True
    size = db.execute(
        "SELECT coalesce(sum(length(CAST(definitions AS BLOB))+"
        "length(CAST(problems AS BLOB))),0) FROM math_macro_contexts"
    ).fetchone()[0]
    if size + len(encoded.encode()) + len(warnings.encode()) > MAX_CONTEXT_BYTES:
        message = "math effective macro context quota reached"
        db.execute(
            "UPDATE math_sources SET blocked=? WHERE document="
            "(SELECT document FROM math_jobs WHERE check_id=?)",
            (message, check),
        )
        db.execute(
            "UPDATE comment_math_sources SET blocked=? WHERE entry_id="
            "(SELECT entry_id FROM math_comment_jobs WHERE check_id=?)",
            (message, check),
        )
        db.execute(
            "UPDATE response_math_sources SET blocked=? WHERE source="
            "(SELECT source FROM math_response_jobs WHERE check_id=?)",
            (message, check),
        )
        return False
    db.execute(
        "INSERT OR IGNORE INTO math_macro_contexts VALUES(?,?,?,?,?)",
        (check, encoded, warnings, int(held), hashlib.sha256(dump(content).encode()).hexdigest()),
    )
    return True


def _sources() -> tuple[tuple[str, str, str, str], ...]:
    return (
        (
            "math_sources",
            "document",
            "j.document=s.document AND NOT EXISTS(SELECT 1 FROM math_comment_jobs c "
            "WHERE c.check_id=j.check_id)",
            "1",
        ),
        (
            "comment_math_sources",
            "entry_id",
            "EXISTS(SELECT 1 FROM math_comment_jobs c WHERE c.check_id=j.check_id "
            "AND c.entry_id=s.entry_id)",
            "s.target>0",
        ),
        (
            "response_math_sources",
            "source",
            "EXISTS(SELECT 1 FROM math_response_jobs r WHERE r.check_id=j.check_id "
            "AND r.source=s.source)",
            "s.target>0",
        ),
    )


def _candidates(table: str, key: str, join: str, condition: str) -> str:
    # All fragments come exclusively from _sources, never caller text.
    return (
        f"SELECT s.{key} FROM {table} s "  # noqa: S608 - fixed schema names
        f"WHERE s.project=? AND s.completed=s.target AND s.retry_requested=0 AND {condition} AND "
        f"EXISTS(SELECT 1 FROM math_jobs j WHERE {join} AND j.revision=s.target) AND "
        f"NOT EXISTS(SELECT 1 FROM math_jobs j JOIN math_macro_pins p USING(check_id) WHERE {join} "
        "AND j.revision=s.target AND p.revision=?) ORDER BY s.rowid LIMIT 32"
    )


def needs_refresh(db: sqlite3.Connection, project: str) -> bool:
    for table, key, join, condition in _sources():
        if db.execute(
            "SELECT 1 FROM (" + _candidates(table, key, join, condition) + ")",  # noqa: S608 - fixed SQL
            (project, head(db, project)),
        ).fetchone():
            return True
    return False


def refresh(db: sqlite3.Connection, project: str) -> None:
    """Admit at most 32 current sources per source kind; write no backlog copies."""
    revision = head(db, project)
    for table, key, join, condition in _sources():
        db.execute(
            f"UPDATE {table} SET retry_requested=1 WHERE {key} IN ("  # noqa: S608 - fixed schema
            + _candidates(table, key, join, condition)
            + ")",
            (project, revision),
        )
