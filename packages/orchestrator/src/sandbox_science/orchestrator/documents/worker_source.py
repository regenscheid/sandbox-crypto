# SPDX-License-Identifier: MIT
"""Internal captured-code edits; no transport, helper, file access or mirror authority.

The caller must attest the binding, retained publication and capture provenance. These
objects are deliberately not request bodies: no public route mints this authority.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from sandbox_science.contracts.document_edits import SHA256, DirectEditCandidate
from sandbox_science.contracts.model import Contract, Identifier, new_id
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

from .blocks import BYTE_ORDER_MARK, MAX_TEXT_BYTES, check_text, normalize_newlines
from .errors import (
    BUSY,
    CONFLICT,
    INTERNAL,
    INVALID_REQUEST,
    NOT_FOUND,
    POLICY_DENIED,
    DocumentError,
)
from .models import Author, ChangeResult
from .store import DocumentStore, _Assessed, _key, _Row, _Submission  # pyright: ignore[reportPrivateUsage]

MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_RAW_SLICE = 64 * 1024
MAX_RECEIPT_BYTES = 64 * 1024

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS worker_source_receipts (
    project TEXT NOT NULL, actor TEXT NOT NULL, operation_id TEXT NOT NULL,
    edit_id TEXT NOT NULL, binding TEXT NOT NULL, environment TEXT NOT NULL,
    incarnation TEXT NOT NULL, document TEXT NOT NULL, fingerprint TEXT NOT NULL,
    candidate TEXT NOT NULL, author TEXT NOT NULL, raw BLOB NOT NULL,
    raw_bytes INTEGER NOT NULL CHECK(raw_bytes>=0 AND raw_bytes<=4194304),
    receipt TEXT NOT NULL CHECK(length(CAST(receipt AS BLOB))<=65536),
    CHECK(length(raw)=raw_bytes),
    PRIMARY KEY(project,actor,operation_id), UNIQUE(project,actor,edit_id)) STRICT""",
    "CREATE INDEX IF NOT EXISTS worker_source_environment "
    "ON worker_source_receipts(environment,project)",
    "CREATE INDEX IF NOT EXISTS worker_source_binding ON worker_source_receipts(binding)",
    """CREATE TRIGGER IF NOT EXISTS worker_source_receipts_immutable_update
    BEFORE UPDATE ON worker_source_receipts
    BEGIN SELECT RAISE(ABORT,'immutable worker receipt'); END""",
    """CREATE TRIGGER IF NOT EXISTS worker_source_receipts_immutable_delete
    BEFORE DELETE ON worker_source_receipts
    BEGIN SELECT RAISE(ABORT,'immutable worker receipt'); END""",
)


class TrustedWorkerBinding(Contract):
    """Trusted transport context and exact retained publication, never code-supplied.

    The future transport must prove this incarnation (including historical captures),
    worker identity, publication and optional execution/run attribution. Local project
    and session/environment ownership is independently checked by the adapter.
    """

    project_id: Identifier
    worker_binding: Identifier
    environment_id: Identifier
    incarnation: Identifier
    document_id: str = Field(pattern=r"^doc-[0-9a-f]{24}$")
    catalog_seq: int = Field(ge=1, le=2**63 - 1, strict=True)
    published_revision: int = Field(ge=1, le=2**63 - 1, strict=True)
    published_sha256: SHA256
    session_id: Identifier | None = None
    execution_id: Identifier | None = None
    attempt_id: Identifier | None = None
    run_id: Identifier | None = None

    @model_validator(mode="after")
    def _context(self) -> Self:
        if self.run_id is not None and self.session_id is None:
            raise ValueError("run attribution requires an established session")
        if self.attempt_id is not None and self.execution_id is None:
            raise ValueError("attempt attribution requires an established execution")
        return self

    def author(self, operation_id: str) -> Author:
        return Author(
            kind="worker",
            role="delegated",
            worker_binding=self.worker_binding,
            environment_id=self.environment_id,
            incarnation=self.incarnation,
            session_id=self.session_id,
            execution_id=self.execution_id,
            attempt_id=self.attempt_id,
            run_id=self.run_id,
            tool_call=operation_id,
        )


@dataclass(frozen=True)
class VerifiedCapture:
    """Immutable bytes whose capture completeness/closure the trusted caller proved.

    Notification-only monitoring and unreadable peer processes cannot prove closure;
    those callers must pass writer_closed=False and keep their source/capture held.
    """

    capture_id: str
    raw: bytes
    complete: bool
    writer_closed: bool


class Quota(Contract):
    bytes: int = Field(gt=0, le=2**40, strict=True)
    records: int = Field(gt=0, le=1_000_000, strict=True)


class RawBudgets(Contract):
    """Logical retained-data budgets, enforced atomically; these are not disk quotas.

    All retained captures count, including unresolved refusals and suggestions. This
    first slice has no eviction or cleanup, so it cannot evict an unresolved edit.
    """

    environment: Quota = Quota(bytes=16 * 1024 * 1024, records=128)
    binding: Quota = Quota(bytes=32 * 1024 * 1024, records=256)
    project: Quota = Quota(bytes=64 * 1024 * 1024, records=512)
    global_: Quota = Quota(bytes=256 * 1024 * 1024, records=2048)


class WorkerEditReceipt(Contract):
    status: Literal["recorded", "held"]
    candidate_sha256: SHA256
    raw_sha256: SHA256
    raw_bytes: int
    author: Author
    change: ChangeResult | None = None
    refusal_code: str | None = None
    message: str | None = None
    retained_exact: bool = False
    receipt_sha256: SHA256 | None = None
    source_event_seq: int | None = None
    publication_pending: bool = False
    safe_to_restore: Literal[False] = False
    """A receipt never grants sandbox publication or proves later writer closure."""
    replayed: bool = False
    details_omitted: bool = False
    """Full change detail remains in the immutable patch/revision, outside this receipt."""


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class WorkerSourceAdapter:
    """Commit exact captured edits and their receipts in the existing document store."""

    def __init__(
        self, documents: DocumentStore, projects: ProjectStore, *, budgets: RawBudgets | None = None
    ) -> None:
        self.documents = documents
        self.projects = projects
        self.budgets = budgets or RawBudgets()
        if documents.path.parent.resolve() != projects.root.parent.resolve():
            raise DocumentError(
                POLICY_DENIED, "worker documents and projects need the same state root"
            )
        if documents.readonly:
            raise DocumentError(POLICY_DENIED, "worker edit adapter needs a writable control store")
        documents.initialize()
        with documents._write() as db:  # pyright: ignore[reportPrivateUsage]
            for statement in SCHEMA:
                db.execute(statement)

    def _scope(self, bound: TrustedWorkerBinding) -> None:
        try:
            project = self.projects.get(bound.project_id)
        except ProjectError:
            raise DocumentError(NOT_FOUND, "no such worker project") from None
        if bound.environment_id not in project.environments():
            raise DocumentError(POLICY_DENIED, "worker environment is outside the project")
        if bound.session_id is not None:
            session = project.sessions.get(bound.session_id)
            if session is None or session.environment_id != bound.environment_id:
                raise DocumentError(POLICY_DENIED, "worker session does not own this environment")

    @staticmethod
    def _replay(
        db: sqlite3.Connection,
        bound: TrustedWorkerBinding,
        candidate: DirectEditCandidate,
        fingerprint: str,
    ) -> WorkerEditReceipt | None:
        row = db.execute(
            "SELECT substr(fingerprint,1,65),substr(CAST(receipt AS BLOB),1,?) "
            "FROM worker_source_receipts WHERE project=? AND actor=? "
            "AND (operation_id=? OR edit_id=?)",
            (
                MAX_RECEIPT_BYTES + 1,
                bound.project_id,
                bound.author(candidate.operation_id).actor,
                candidate.operation_id,
                candidate.edit_id,
            ),
        ).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise DocumentError(CONFLICT, "worker operation/edit ID already names another payload")
        if len(row[1]) > MAX_RECEIPT_BYTES:
            raise DocumentError(INTERNAL, "retained worker receipt exceeds its bound")
        try:
            receipt = WorkerEditReceipt.model_validate_json(row[1])
        except ValidationError:
            raise DocumentError(INTERNAL, "invalid retained worker receipt") from None
        canonical = receipt.model_dump(mode="json")
        canonical["receipt_sha256"] = None
        if (
            receipt.status != "recorded"
            or not receipt.retained_exact
            or receipt.replayed
            or receipt.candidate_sha256 != fingerprint
            or receipt.raw_sha256 != candidate.raw_sha256
            or receipt.raw_bytes != candidate.raw_bytes
            or receipt.author != bound.author(candidate.operation_id)
            or receipt.receipt_sha256 != sha(encoded(canonical).encode())
        ):
            raise DocumentError(INTERNAL, "retained worker receipt identity or digest differs")
        return receipt.model_copy(update={"replayed": True})

    def _quota(self, db: sqlite3.Connection, bound: TrustedWorkerBinding, length: int) -> bool:
        for condition, params, quota in (
            (
                "project=? AND environment=?",
                (bound.project_id, bound.environment_id),
                self.budgets.environment,
            ),
            ("binding=?", (bound.worker_binding,), self.budgets.binding),
            ("project=?", (bound.project_id,), self.budgets.project),
            ("1", (), self.budgets.global_),
        ):
            used, count = db.execute(
                # The clauses above are fixed platform strings, never candidate data.
                "SELECT coalesce(sum(raw_bytes),0),count(*) FROM worker_source_receipts WHERE "  # noqa: S608
                + condition,
                params,
            ).fetchone()
            if used + length > quota.bytes or count + 1 > quota.records:
                return False
        return True

    def apply(
        self, bound: TrustedWorkerBinding, candidate: DirectEditCandidate, capture: VerifiedCapture
    ) -> WorkerEditReceipt:
        """An internal attested text-to-patch call; uncertain captures remain held."""
        try:
            bound = TrustedWorkerBinding.model_validate(bound.model_dump())
            candidate = DirectEditCandidate.model_validate(candidate.model_dump())
        except (ValidationError, AttributeError):
            raise DocumentError(INVALID_REQUEST, "invalid worker edit identity") from None
        self._scope(bound)
        if candidate.document_id != bound.document_id or candidate.catalog_seq != bound.catalog_seq:
            raise DocumentError(POLICY_DENIED, "candidate does not name the attested publication")
        if (
            type(capture) is not VerifiedCapture
            or type(capture.raw) is not bytes
            or (type(capture.complete) is not bool or type(capture.writer_closed) is not bool)
            or capture.capture_id != candidate.capture_id
        ):
            raise DocumentError(INVALID_REQUEST, "invalid immutable capture")
        fingerprint = sha(
            encoded(
                {
                    "binding": bound.model_dump(mode="json"),
                    "candidate": candidate.model_dump(mode="json"),
                }
            ).encode()
        )
        request = {"worker_source": fingerprint}
        author = bound.author(candidate.operation_id)
        held = WorkerEditReceipt(
            status="held",
            candidate_sha256=fingerprint,
            raw_sha256=candidate.raw_sha256,
            raw_bytes=candidate.raw_bytes,
            author=author,
        )
        if (
            not capture.complete
            or not capture.writer_closed
            or candidate.raw_bytes > MAX_CAPTURE_BYTES
            or len(capture.raw) > MAX_CAPTURE_BYTES
        ):
            return held.model_copy(
                update={
                    "message": "capture is incomplete, too large, or writer closure is uncertain"
                }
            )
        if len(capture.raw) != candidate.raw_bytes or sha(capture.raw) != candidate.raw_sha256:
            raise DocumentError(
                INVALID_REQUEST, "capture bytes differ from the candidate digest/length"
            )
        with self.documents._read() as reading:  # pyright: ignore[reportPrivateUsage]
            assert reading is not None  # noqa: S101
            replay = self._replay(reading, bound, candidate, fingerprint)
            if replay is not None:
                return replay
            self._operation_available(reading, bound, candidate, request)
            row = self.documents._find(reading, bound.project_id, candidate.document_id)  # pyright: ignore[reportPrivateUsage]
        refusal: str | None = None
        submission: _Submission | None = None
        prepared: _Assessed | None = None
        if candidate.base_revision is None:
            refusal = "unknown_base"
        elif (candidate.base_revision, candidate.base_written_sha256) != (
            bound.published_revision,
            bound.published_sha256,
        ):
            refusal = "base_publication_mismatch"
        else:
            try:
                base = self.documents.revision(
                    bound.project_id, candidate.document_id, candidate.base_revision
                )
                if base.info.sha256 != candidate.base_written_sha256 or base.info.tombstone:
                    refusal = "base_revision_mismatch"
                elif row.info.deleted:
                    refusal = "document_deleted"
                else:
                    text = capture.raw.decode("utf-8", errors="strict")
                    if len(capture.raw) > MAX_TEXT_BYTES:
                        refusal = "document_too_large"
                    else:
                        text = normalize_newlines(text).removeprefix(BYTE_ORDER_MARK)
                        check_text(text)
                        submission, prepared = self.documents._prepare_source(  # pyright: ignore[reportPrivateUsage]
                            bound.project_id,
                            candidate.document_id,
                            base=candidate.base_revision,
                            text=text,
                            author=author,
                            reason="captured worker source edit",
                        )
                        if prepared is not None and prepared.error is not None:
                            refusal = "invalid_source"
            except UnicodeDecodeError:
                refusal = "invalid_utf8"
            except DocumentError as error:
                if error.kind not in (INVALID_REQUEST, NOT_FOUND):
                    raise
                refusal = (
                    "invalid_source" if error.kind == INVALID_REQUEST else "base_revision_missing"
                )
        with self.documents._write() as db:  # pyright: ignore[reportPrivateUsage]
            self._scope(bound)
            replay = self._replay(db, bound, candidate, fingerprint)
            if replay is not None:
                return replay
            self._operation_available(db, bound, candidate, request)
            current = self.documents._find(db, bound.project_id, candidate.document_id)  # pyright: ignore[reportPrivateUsage]
            if _key(current) != _key(row) or (
                refusal is None and (prepared is None or prepared.key != _key(current))
            ):
                raise DocumentError(
                    BUSY, "document changed during preparation; retry the same worker operation"
                )
            if not self._quota(db, bound, len(capture.raw)):
                return held.model_copy(
                    update={
                        "message": "retained raw edit logical quota reached; source must stay held"
                    }
                )
            before = db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
            if refusal is not None:
                change = self._reject(db, current, bound, candidate, author, refusal)
            else:
                assert submission is not None  # noqa: S101
                assert prepared is not None  # noqa: S101
                change = self.documents._act(  # pyright: ignore[reportPrivateUsage]
                    db, current, submission, prepared, suggest=current.info.takes_suggestions
                )
            after = db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
            # Keep receipts small; full maps/warnings/conflicts stay in the source
            # revision or retained patch instead of multiplying on every retry.
            compact = ChangeResult(
                outcome=change.outcome,
                document_id=change.document_id,
                revision=change.revision,
                base=change.base,
                applied_to=change.applied_to,
                rebased=change.rebased,
                patch_id=change.patch_id,
                message=change.message[:2048] if change.message else None,
            )
            receipt = held.model_copy(
                update={
                    "status": "recorded",
                    "change": compact,
                    "refusal_code": refusal,
                    "retained_exact": True,
                    "source_event_seq": after if after > before else None,
                    "publication_pending": change.outcome == "applied",
                    "details_omitted": bool(
                        change.changes
                        or change.normalized
                        or change.warnings
                        or change.conflicts
                        or change.unnamed
                        or (change.message and len(change.message) > 2048)
                    ),
                }
            )
            receipt = receipt.model_copy(
                update={"receipt_sha256": sha(encoded(receipt.model_dump(mode="json")).encode())}
            )
            receipt_json = receipt.model_dump_json()
            if len(receipt_json.encode()) > MAX_RECEIPT_BYTES:
                raise DocumentError(INTERNAL, "worker receipt exceeds its bound")
            self.documents._remember(  # pyright: ignore[reportPrivateUsage]
                db, bound.project_id, author, candidate.operation_id, request, compact
            )
            db.execute(
                "INSERT INTO worker_source_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bound.project_id,
                    author.actor,
                    candidate.operation_id,
                    candidate.edit_id,
                    bound.worker_binding,
                    bound.environment_id,
                    bound.incarnation,
                    candidate.document_id,
                    fingerprint,
                    candidate.model_dump_json(),
                    author.model_dump_json(),
                    capture.raw,
                    len(capture.raw),
                    receipt_json,
                ),
            )
            return receipt

    def _operation_available(
        self,
        db: sqlite3.Connection,
        bound: TrustedWorkerBinding,
        candidate: DirectEditCandidate,
        request: dict[str, str],
    ) -> None:
        # Share the ordinary document operation namespace with any future helper.
        row = db.execute(
            "SELECT substr(fingerprint,1,65) FROM requests WHERE project_id=? AND actor=? "
            "AND operation_id=?",
            (bound.project_id, bound.author(candidate.operation_id).actor, candidate.operation_id),
        ).fetchone()
        if row is not None:
            # DocumentStore uses compact unsorted JSON; this request has one key.
            if row[0] != sha(encoded(request).encode()):
                raise DocumentError(CONFLICT, "worker operation ID was used for another change")
            raise DocumentError(
                INTERNAL, "worker operation has a document result but no atomic receipt"
            )

    def _reject(
        self,
        db: sqlite3.Connection,
        row: _Row,
        bound: TrustedWorkerBinding,
        candidate: DirectEditCandidate,
        author: Author,
        code: str,
    ) -> ChangeResult:
        # The current head is a review anchor only, never an inferred apply-base.
        submission = _Submission(new_id("patch"), row.info.head, [], [], code, author)
        details = {
            "raw_rejection": True,
            "refusal_code": code,
            "reported_base": candidate.base_revision,
            "review_base": row.info.head,
            "raw_sha256": candidate.raw_sha256,
            "raw_bytes": candidate.raw_bytes,
            "operation_id": candidate.operation_id,
            "capture_id": candidate.capture_id,
            "worker_binding": bound.worker_binding,
        }
        return self.documents._reject(  # pyright: ignore[reportPrivateUsage]
            db,
            row,
            submission,
            "refused",
            details,
            ChangeResult(
                outcome="refused",
                document_id=row.info.document_id,
                revision=row.info.head,
                base=candidate.base_revision,
                patch_id=submission.patch_id,
                message=f"retained raw worker edit: {code}",
            ),
        )

    def read_raw(
        self,
        bound: TrustedWorkerBinding,
        operation_id: str,
        *,
        start: int = 0,
        count: int = MAX_RAW_SLICE,
    ) -> bytes:
        """A bounded internal read; no public route or arbitrary file path is exposed."""
        try:
            bound = TrustedWorkerBinding.model_validate(bound.model_dump())
        except (ValidationError, AttributeError):
            raise DocumentError(INVALID_REQUEST, "invalid worker binding") from None
        self._scope(bound)
        if (
            type(start) is not int
            or type(count) is not int
            or not 0 <= start < 2**63 - 1
            or not 1 <= count <= MAX_RAW_SLICE
        ):
            raise DocumentError(INVALID_REQUEST, "invalid raw byte range")
        if type(operation_id) is not str:
            raise DocumentError(INVALID_REQUEST, "invalid operation ID")
        self.documents._operation_id(operation_id)  # pyright: ignore[reportPrivateUsage]
        with self.documents._read() as db:  # pyright: ignore[reportPrivateUsage]
            assert db is not None  # noqa: S101
            row = db.execute(
                "SELECT substr(raw,?,?) FROM worker_source_receipts WHERE project=? AND actor=? "
                "AND operation_id=? AND document=?",
                (
                    start + 1,
                    count,
                    bound.project_id,
                    bound.author(operation_id).actor,
                    operation_id,
                    bound.document_id,
                ),
            ).fetchone()
        if row is None:
            raise DocumentError(NOT_FOUND, "no retained worker edit in this binding")
        return bytes(row[0])
