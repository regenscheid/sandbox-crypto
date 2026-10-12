# SPDX-License-Identifier: MIT
"""Internal immutable export-input snapshots; no converter or worker authority.

Head and macro selection share one read transaction. Parsing happens after it closes;
the write transaction only rechecks replay/quota and appends the proved pin. Later
source changes therefore neither repin the request nor extend the write-lock lifetime.
"""

from __future__ import annotations

# Scoped components share the control transaction; these are not public request routes.
# pyright: reportPrivateUsage=false
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts.document_exports import (
    MAX_MAP_BYTES,
    MAX_PARSER_BYTES,
    MAX_SNAPSHOT_BYTES,
    MAX_SOURCE_BYTES,
    ExportBudgets,
    ExportInputReceipt,
    ExportInputRequest,
    ExportInputSnapshot,
    ExportMacros,
    ExportMathSpan,
    ExportRequester,
    ExportSource,
    Hold,
    SnapshotID,
    canonical,
)
from sandbox_science.contracts.model import Identifier, new_id
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

from .engine import attach
from .errors import CONFLICT, INTERNAL, INVALID_REQUEST, NOT_FOUND, POLICY_DENIED, DocumentError
from .identity import Mapped
from .models import Author
from .parser import PROFILES
from .project_macros import read as read_macros
from .project_macros import resolve
from .store import DocumentStore

Json = dict[str, Any]
_ID = TypeAdapter[str](Identifier)
_SNAPSHOT_ID = TypeAdapter[str](SnapshotID)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BLOCK = re.compile(r"b[1-9][0-9]{0,18}\Z")
_SPAN = re.compile(r"m[1-9][0-9]{0,18}\Z")
# Deliberately conservative: even code examples and harmless hyperlinks are held.
# This detects unsupported dependency syntax, not arbitrary TeX effects or safety.
_RESOURCES = re.compile(
    r"!\[|\]\s*[([]|(?m:^[ \t>]*(?:[-+*] |[0-9]+[.)] )?\[[^\]\n]+\]:)"
    r"|<(?:[A-Za-z][A-Za-z0-9:-]*(?:\s|/?>)|[!/?])"
    r"|(?:https?://|mailto:|artifact:|project:|document:|ledger:|pdf-source:|project-file:)"
    r"|\\(?:input|include|includegraphics|graphicspath|bibliography|bibliographystyle|"
    r"addbibresource|lstinputlisting|verbatiminput|import|subimport|includepdf|openin|openout|"
    r"read|write|usepackage|documentclass|RequirePackage|IfFileExists|InputIfFileExists|"
    r"catcode|csname|scantokens|directlua|special|def|gdef|edef|xdef|let|futurelet|"
    r"newcommand|renewcommand|providecommand|url|href|newread|newwrite|font)(?![A-Za-z])|\^\^",
    re.IGNORECASE,
)
_COLUMNS = (
    "project",
    "actor",
    "operation",
    "fingerprint",
    "snapshot_id",
    "digest",
    "payload",
    "document",
    "revision",
    "session",
    "deleted",
    "macro_revision",
    "requester",
)
_BYTES = "+".join(f"coalesce(length(CAST({column} AS BLOB)),0)" for column in _COLUMNS)
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS document_export_inputs (
      project TEXT NOT NULL,actor TEXT NOT NULL,operation TEXT NOT NULL,
      fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
      snapshot_id TEXT NOT NULL UNIQUE,digest TEXT NOT NULL CHECK(length(digest)=64),
      document TEXT NOT NULL,revision INTEGER NOT NULL,
      session TEXT,deleted INTEGER NOT NULL CHECK(deleted IN(0,1)),macro_revision INTEGER,
      requester BLOB NOT NULL CHECK(length(requester)<=1024),
      payload BLOB NOT NULL CHECK(length(payload)<=4194304),
      PRIMARY KEY(project,actor,operation),
      FOREIGN KEY(document,revision) REFERENCES revisions(document_id,number)) STRICT""",
    """CREATE TRIGGER IF NOT EXISTS document_export_inputs_no_update
      BEFORE UPDATE ON document_export_inputs
      BEGIN SELECT RAISE(ABORT,'export input snapshots are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS document_export_inputs_no_delete
      BEFORE DELETE ON document_export_inputs
      BEGIN SELECT RAISE(ABORT,'export input snapshots are append-only'); END""",
)


def prepare(db: sqlite3.Connection) -> None:
    """Additive writer preparation; readonly/legacy readers never migrate."""
    for statement in SCHEMA:
        db.execute(statement)


def _exists(db: sqlite3.Connection | None) -> bool:
    return (
        db is not None
        and db.execute("SELECT 1 FROM sqlite_master WHERE name='document_export_inputs'").fetchone()
        is not None
    )


def _bad(message: str = "invalid retained export input state") -> DocumentError:
    return DocumentError(INTERNAL, message)


def _string(value: object, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, bytes) or len(value) > maximum or (not value and not empty):
        raise _bad()
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad() from None


def _counter(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or not (int(positive) <= value <= 2**63 - 1):
        raise _bad()
    return value


def _object(raw: bytes) -> Json:
    try:
        value: object = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return cast(Json, value)
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise _bad() from None


@dataclass(frozen=True)
class _Pinned:
    source: ExportSource
    block_map: bytes | None
    macros: Json | None
    deleted: bool


class ExportInputs:
    """Internal API for a caller whose USER/agent context was already authenticated.

    The explicit ProjectStore supplies independent project/session membership checks.
    This object grants no export, worker, filesystem or policy authority.
    """

    def __init__(
        self,
        documents: DocumentStore,
        projects: ProjectStore,
        *,
        budgets: ExportBudgets | None = None,
    ) -> None:
        self.documents = documents
        self.projects = projects
        self.budgets = budgets or ExportBudgets()

    def _scope(self, project: str, author: Author) -> ExportRequester:
        try:
            _ID.validate_python(project)
            if author.kind == "user":
                requester = ExportRequester(kind="user", user=author.user)
            elif author.kind == "agent" and author.role in ("main", "delegated"):
                requester = ExportRequester(
                    kind="agent",
                    session_id=author.session_id,
                    run_id=author.run_id,
                    role=author.role,
                )
            else:
                raise DocumentError(
                    POLICY_DENIED, "only trusted USER or ordinary agent scope pins export inputs"
                )
            canonical(requester.model_dump(mode="json"))
        except (ValidationError, UnicodeError):
            raise DocumentError(INVALID_REQUEST, "invalid export requester scope") from None
        try:
            record = self.projects.get(project)
        except ProjectError:
            raise DocumentError(NOT_FOUND, "export project is unavailable") from None
        if requester.session_id is not None and requester.session_id not in record.sessions:
            raise DocumentError(POLICY_DENIED, "export requester does not belong to this project")
        return requester

    def prepare(self, project: str, value: object, author: Author) -> ExportInputReceipt:
        """Pin current/current or numbered/saved inputs, with durable exact-op replay."""
        if self.documents.readonly:
            raise DocumentError(POLICY_DENIED, "readonly stores cannot prepare export inputs")
        requester = self._scope(project, author)
        try:
            request = ExportInputRequest.model_validate(value)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid export input request") from None
        fingerprint = hashlib.sha256(canonical(request.model_dump(mode="json"))).hexdigest()
        pinned: _Pinned | None = None
        with self.documents._read() as db:
            if db is None:
                raise DocumentError(NOT_FOUND, "export document is unavailable")
            replay = self._replay(db, project, requester.actor, request.operation_id, fingerprint)
            if replay is None:
                self._quota(db, project, 0)
                pinned = self._pin(db, project, request)
        if replay is not None:
            return self._checked(replay)
        assert pinned is not None  # noqa: S101 - a non-replayed read always pins inputs
        snapshot = self._assemble(project, request, requester, pinned)
        payload = canonical(snapshot.model_dump(mode="json"))
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise DocumentError(CONFLICT, "export input snapshot exceeds the 4 MiB logical limit")
        digest = hashlib.sha256(payload).hexdigest()
        self._scope(project, author)
        with self.documents._write() as db:
            replay = self._replay(db, project, requester.actor, request.operation_id, fingerprint)
            if replay is None:
                columns = (
                    project,
                    requester.actor,
                    request.operation_id,
                    fingerprint,
                    snapshot.snapshot_id,
                    digest,
                    snapshot.source.document_id,
                    snapshot.source.revision,
                    snapshot.source.session_id,
                    int(pinned.deleted),
                    None if pinned.macros is None else pinned.macros["revision"],
                    canonical(requester.model_dump(mode="json")),
                    payload,
                )
                size = sum(
                    len(v if isinstance(v, bytes) else str(v).encode())
                    for v in columns
                    if v is not None
                )
                self._quota(db, project, size)
                db.execute(
                    "INSERT INTO document_export_inputs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", columns
                )
        if replay is not None:
            return self._checked(replay)
        return ExportInputReceipt(snapshot=snapshot, sha256=digest)

    def get(self, project: str, snapshot_id: str) -> ExportInputReceipt:
        """Read one immutable project-scoped snapshot, including on readonly stores."""
        try:
            _ID.validate_python(project)
            _SNAPSHOT_ID.validate_python(snapshot_id)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid export snapshot identity") from None
        with self.documents._read() as db:
            if not _exists(db) or db is None:
                raise DocumentError(NOT_FOUND, "export snapshot is unavailable")
            stored = self._stored(db, "project=? AND snapshot_id=?", (project, snapshot_id), False)
        return self._checked(stored)

    def _replay(
        self, db: sqlite3.Connection, project: str, actor: str, operation: str, fingerprint: str
    ) -> ExportInputReceipt | None:
        if not _exists(db):
            return None
        row = db.execute(
            "SELECT substr(CAST(fingerprint AS BLOB),1,65) FROM document_export_inputs "
            "WHERE project=? AND actor=? AND operation=?",
            (project, actor, operation),
        ).fetchone()
        if row is None:
            return None
        if _string(row[0], 64) != fingerprint:
            raise DocumentError(CONFLICT, "operation ID already names different export inputs")
        return self._stored(
            db, "project=? AND actor=? AND operation=?", (project, actor, operation), True
        )

    def _stored(
        self, db: sqlite3.Connection, where: str, arguments: tuple[str, ...], replayed: bool
    ) -> ExportInputReceipt:
        row = db.execute(
            "SELECT CASE WHEN length(payload)<=4194304 THEN payload END,"  # noqa: S608 - private fixed predicates
            "substr(CAST(digest AS BLOB),1,65),"
            "substr(CAST(project AS BLOB),1,129),substr(CAST(actor AS BLOB),1,137),"
            "substr(CAST(operation AS BLOB),1,129),substr(CAST(snapshot_id AS BLOB),1,29),"
            "substr(CAST(fingerprint AS BLOB),1,65),substr(CAST(document AS BLOB),1,29),revision "
            f"FROM document_export_inputs WHERE {where}",
            arguments,
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "export snapshot is unavailable")
        try:
            if not isinstance(row[0], bytes) or len(row[0]) > MAX_SNAPSHOT_BYTES:
                raise ValueError
            payload = row[0]
            digest, project, actor, operation, identity, fingerprint, document = (
                _string(v, limit)
                for v, limit in zip(row[1:8], (64, 128, 136, 128, 28, 64, 28), strict=True)
            )
            revision = _counter(row[8], positive=True)
            snapshot = ExportInputSnapshot.model_validate_json(payload)
            if (
                canonical(snapshot.model_dump(mode="json")) != payload
                or snapshot.sha256 != digest
                or (
                    snapshot.project_id,
                    snapshot.requested_by.actor,
                    snapshot.request.operation_id,
                    snapshot.snapshot_id,
                    snapshot.source.document_id,
                    snapshot.source.revision,
                )
                != (project, actor, operation, identity, document, revision)
                or hashlib.sha256(canonical(snapshot.request.model_dump(mode="json"))).hexdigest()
                != fingerprint
            ):
                raise ValueError
        except (ValueError, TypeError, RecursionError, ValidationError, UnicodeError):
            raise _bad("invalid retained export snapshot or receipt") from None
        return ExportInputReceipt(snapshot=snapshot, sha256=digest, replayed=replayed)

    def _quota(self, db: sqlite3.Connection, project: str, size: int) -> None:
        for clause, args, limit in (
            ("WHERE project=?", (project,), self.budgets.project),
            ("", (), self.budgets.global_),
        ):
            count, total = db.execute(
                f"SELECT count(*),coalesce(sum({_BYTES}),0) FROM document_export_inputs {clause}",  # noqa: S608 - fixed columns/predicates
                args,
            ).fetchone()
            _counter(count)
            _counter(total)
            if count >= limit.records or total + size > limit.bytes:
                raise DocumentError(
                    CONFLICT, "export input retention quota reached; no inputs were appended"
                )

    @staticmethod
    def _pin(
        db: sqlite3.Connection,
        project: str,
        request: ExportInputRequest,
        retained: tuple[str | None, bool, int | None] | None = None,
    ) -> _Pinned:
        row = db.execute(
            "SELECT head,deleted,substr(CAST(session_id AS BLOB),1,129) FROM documents "
            "WHERE document_id=? AND project_id=?",
            (request.document_id, project),
        ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "export document is unavailable")
        head = _counter(row[0], positive=True)
        if type(row[1]) is not int or row[1] not in (0, 1):
            raise _bad()
        try:
            session = None if row[2] is None else _ID.validate_python(_string(row[2], 128))
        except ValidationError:
            raise _bad("invalid retained document scope") from None
        revision = head if request.revision is None else request.revision
        meta = db.execute(
            "SELECT typeof(text),length(CAST(text AS BLOB)),typeof(block_map),"
            "length(CAST(block_map AS BLOB)),typeof(parser),length(CAST(parser AS BLOB)),"
            "substr(CAST(sha256 AS BLOB),1,65),tombstone FROM revisions "
            "WHERE document_id=? AND number=?",
            (request.document_id, revision),
        ).fetchone()
        if meta is None:
            raise DocumentError(NOT_FOUND, "export revision is unavailable")
        sizes = tuple(_counter(meta[i]) for i in (1, 3, 5))
        if (
            meta[0] != "text"
            or meta[2] != "text"
            or meta[4] != "text"
            or (sizes[0] > MAX_SOURCE_BYTES or sizes[2] > MAX_PARSER_BYTES)
            or type(meta[7]) is not int
            or meta[7] not in (0, 1)
        ):
            raise _bad("retained export source exceeds its type or byte bounds")
        digest = _string(meta[6], 64)
        if _DIGEST.fullmatch(digest) is None:
            raise _bad()
        raw = db.execute(
            "SELECT coalesce(substr(CAST(text AS BLOB),1,1048577),X''),"
            "CASE WHEN length(CAST(block_map AS BLOB))<=4194304 THEN CAST(block_map AS BLOB) END,"
            "substr(CAST(parser AS BLOB),1,4097) FROM revisions WHERE document_id=? AND number=?",
            (request.document_id, revision),
        ).fetchone()
        assert raw is not None  # noqa: S101 - same read snapshot
        text = _string(raw[0], MAX_SOURCE_BYTES, empty=True)
        if hashlib.sha256(raw[0]).hexdigest() != digest:
            raise _bad("retained export source hash does not match its bytes")
        parser_json = _string(raw[2], MAX_PARSER_BYTES)
        parser = _object(raw[2])
        if any(not isinstance(v, str) or len(v) > 256 for v in parser.values()):
            raise _bad("invalid retained parser metadata")
        if (raw[1] is None) != (sizes[1] > MAX_MAP_BYTES):
            raise _bad()
        macros = None
        if retained is not None:
            session = retained[0]
            macros = None if retained[2] is None else read_macros(db, project, retained[2])
        elif request.revision is None:
            macros = read_macros(db, project)
        else:
            pin = db.execute(
                "SELECT project_revision FROM document_macro_pins WHERE document=? AND revision=?",
                (request.document_id, revision),
            ).fetchone()
            if pin is not None:
                macros = read_macros(db, project, pin[0])
        return _Pinned(
            ExportSource(
                document_id=request.document_id,
                revision=revision,
                session_id=session,
                scope="project" if session is None else "session",
                text=text,
                sha256=digest,
                bytes=sizes[0],
                block_map_bytes=sizes[1],
                block_map_sha256=None if raw[1] is None else hashlib.sha256(raw[1]).hexdigest(),
                profile=None,
                parser_json=parser_json,
                parser_sha256=hashlib.sha256(raw[2]).hexdigest(),
            ),
            raw[1],
            macros,
            bool(row[1] or meta[7]) if retained is None else retained[1],
        )

    @staticmethod
    def _assemble(
        project: str, request: ExportInputRequest, requester: ExportRequester, pinned: _Pinned
    ) -> ExportInputSnapshot:
        holds: list[Hold] = []
        spans: tuple[ExportMathSpan, ...] = ()
        mapped = None
        source = pinned.source
        if pinned.block_map is None:
            holds.append("map_limit")
        else:
            block_map = _object(pinned.block_map)
            profile = block_map.get("profile")
            if profile is not None and (
                not isinstance(profile, str) or len(profile.encode()) > 128
            ):
                raise _bad("invalid retained parser profile")
            source = source.model_copy(update={"profile": profile})
            if profile not in PROFILES:
                holds.append("unsupported_profile")
            else:
                try:
                    mapped = attach(source.text, block_map)
                    spans = _spans(mapped, block_map)
                except (KeyError, ValueError, TypeError, IndexError, RecursionError, DocumentError):
                    raise _bad(
                        "retained export map does not bind its source and identities"
                    ) from None
        definitions: dict[str, str] = {}
        if pinned.macros is None:
            holds.append("unknown_historical_macros")
        elif mapped is not None:
            front = mapped.parsed.front_matter
            definitions, problems = resolve(
                pinned.macros["macros"], {} if front is None else front.macros
            )
            if problems or (front is not None and front.problems):
                holds.append("macro_limits")
        # Missing parser evidence cannot establish document overrides even if the
        # project setting itself is known; no default-only context is substituted.
        known = pinned.macros is not None and mapped is not None
        setting = pinned.macros if known and pinned.macros is not None else None
        if _RESOURCES.search(pinned.source.text) or any(
            _RESOURCES.search(v) for v in definitions.values()
        ):
            holds.append("unsupported_resources")
        if pinned.deleted:
            holds.append("source_deleted")
        return ExportInputSnapshot(
            snapshot_id=new_id("xpi"),
            project_id=project,
            request=request,
            requested_by=requester,
            status="held" if holds else "prepared",
            holds=tuple(holds),
            source=source,
            spans=spans,
            spans_complete=mapped is not None,
            macros=ExportMacros(
                known=known,
                project_revision=setting["revision"] if setting is not None else None,
                project_sha256=setting["sha256"] if setting is not None else None,
                definitions=definitions,
                sha256=hashlib.sha256(canonical(definitions)).hexdigest(),
            ),
        )

    def _checked(self, receipt: ExportInputReceipt) -> ExportInputReceipt:
        """Reconstruct derived output from canonical pins, outside all transactions.

        Selection headers are retained independently of the projection. In particular,
        current-head replay uses its original macro revision, never the new head.
        """
        snapshot = receipt.snapshot
        with self.documents._read() as db:
            if db is None:
                raise _bad()
            row = db.execute(
                "SELECT substr(CAST(session AS BLOB),1,129),deleted,macro_revision,"
                "substr(CAST(requester AS BLOB),1,1025) FROM document_export_inputs "
                "WHERE project=? AND snapshot_id=?",
                (snapshot.project_id, snapshot.snapshot_id),
            ).fetchone()
            if row is None or type(row[1]) is not int or row[1] not in (0, 1):
                raise _bad()
            try:
                session = None if row[0] is None else _ID.validate_python(_string(row[0], 128))
                macro_revision = None if row[2] is None else _counter(row[2])
                requester = ExportRequester.model_validate_json(_string(row[3], 1024))
                if requester != snapshot.requested_by:
                    raise ValueError
            except (ValueError, ValidationError):
                raise _bad("invalid retained export selection header") from None
            selected = snapshot.request.model_copy(update={"revision": snapshot.source.revision})
            pinned = self._pin(
                db, snapshot.project_id, selected, (session, bool(row[1]), macro_revision)
            )
        reconstructed = self._assemble(snapshot.project_id, snapshot.request, requester, pinned)
        reconstructed = reconstructed.model_copy(update={"snapshot_id": snapshot.snapshot_id})
        if reconstructed != snapshot:
            raise _bad("export receipt does not match canonical source, spans or macro context")
        return receipt


def _spans(mapped: Mapped, stored: Json) -> tuple[ExportMathSpan, ...]:
    raw_entries = stored["blocks"]
    if not isinstance(raw_entries, list) or len(set(mapped.block_ids)) != len(mapped.block_ids):
        raise ValueError
    entries = cast(list[object], raw_entries)
    for block, raw_entry in zip(mapped.parsed.blocks, entries, strict=True):
        if not isinstance(raw_entry, dict):
            raise ValueError
        entry = cast(Json, raw_entry)
        if _BLOCK.fullmatch(mapped.block_ids[block.index]) is None or (
            entry.get("hash") != block.hash
            or entry.get("start") != block.start
            or entry.get("end") != block.end
            or entry.get("parent") != mapped.parent_id(block.index)
            or canonical(entry.get("attrs")) != canonical(block.attrs)
        ):
            raise ValueError
        raw_spans = entry["spans"]
        if not isinstance(raw_spans, list):
            raise ValueError
        for index, raw_span in zip(block.spans, cast(list[object], raw_spans), strict=True):
            if not isinstance(raw_span, dict):
                raise ValueError
            span = cast(Json, raw_span)
            if (
                span.get("hash") != mapped.parsed.spans[index].hash
                or span.get("kind") != mapped.parsed.spans[index].kind
                or _SPAN.fullmatch(mapped.span_ids[index]) is None
            ):
                raise ValueError
    result: list[ExportMathSpan] = []
    for span in mapped.parsed.spans:
        values = (span.open_start, span.tex_start, span.tex_end, span.close_end)
        positions = (
            None if any(v is None for v in values) else cast(tuple[int, int, int, int], values)
        )
        result.append(
            ExportMathSpan(
                block_id=mapped.block_ids[span.block],
                span_id=mapped.span_ids[span.index],
                kind=span.kind,
                tex=span.tex,
                sha256=span.hash,
                open=span.open,
                close=span.close,
                positions=positions,
            )
        )
    return tuple(result)
