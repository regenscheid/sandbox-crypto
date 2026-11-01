# SPDX-License-Identifier: MIT
"""Immutable user uploads, separate from agent publication provenance.

Original bytes remain authoritative. A Markdown document is an optional derivative;
delivery to a sandbox is observed independently on each explicit attachment send.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from typing import BinaryIO, Final

from pydantic import Field, TypeAdapter

from sandbox_science.contracts import Identifier, new_id
from sandbox_science.contracts.model import Contract
from sandbox_science.contracts.worker import MAX_FILE_WRITE_BYTES

from .artifact_blobs import CHUNK_BYTES, ArtifactBlobs, Blob
from .control.migrations import Schema
from .documents import DocumentError, DocumentStore
from .upload_streams import UploadAdmission, UploadWriter, acquire, storage_errors

MAX_UPLOAD_BYTES: Final = MAX_FILE_WRITE_BYTES
MAX_PROJECT_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 8 * 1024 * 1024 * 1024
MAX_PROJECT_UPLOADS: Final = 1024
MAX_TOTAL_UPLOADS: Final = 4096
_IDENTIFIER: Final = TypeAdapter[str](Identifier)


class UserUpload(Contract):
    upload_id: Identifier
    project: Identifier
    session: Identifier
    filename: str
    media_type: str
    sha256: str
    size: int
    created_at: str
    author: str = "user"
    path: str
    number: int


class UploadPage(Contract):
    uploads: list[UserUpload] = Field(max_length=50)
    next_after: int | None = None


def install(schema: Schema) -> None:
    """Version 66: bounded immutable original uploads with server-derived ownership."""
    schema.execute("""CREATE TABLE user_uploads (
        seq INTEGER PRIMARY KEY,
        project TEXT NOT NULL,
        session TEXT NOT NULL,
        upload_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        record TEXT NOT NULL CHECK (json_valid(record)),
        size INTEGER NOT NULL CHECK (size BETWEEN 0 AND 8388608),
        content BLOB NOT NULL CHECK (length(content) = size),
        UNIQUE(project, session, upload_id),
        FOREIGN KEY(project, session) REFERENCES sessions(project_id, session_id)
    ) STRICT""")
    for action in ("UPDATE", "DELETE"):
        schema.execute(
            f"CREATE TRIGGER user_uploads_no_{action.lower()} BEFORE {action} ON user_uploads "
            "BEGIN SELECT RAISE(ABORT, 'original uploads are immutable'); END"
        )


def expand_limit(schema: Schema) -> None:
    """Version 67: retain every original while widening the immutable size constraint."""
    schema.execute("ALTER TABLE user_uploads RENAME TO user_uploads_previous")
    for action in ("update", "delete"):
        schema.execute(f"DROP TRIGGER user_uploads_no_{action}")
    # Rebuild the table in the enclosing migration transaction, never edit schema text.
    schema.execute("""CREATE TABLE user_uploads (
        seq INTEGER PRIMARY KEY,
        project TEXT NOT NULL,
        session TEXT NOT NULL,
        upload_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        record TEXT NOT NULL CHECK (json_valid(record)),
        size INTEGER NOT NULL CHECK (size BETWEEN 0 AND 104857600),
        content BLOB NOT NULL CHECK (length(content) = size),
        UNIQUE(project, session, upload_id),
        FOREIGN KEY(project, session) REFERENCES sessions(project_id, session_id)
    ) STRICT""")
    schema.execute("INSERT INTO user_uploads SELECT * FROM user_uploads_previous")
    schema.execute("DROP TABLE user_uploads_previous")
    for action in ("UPDATE", "DELETE"):
        schema.execute(
            f"CREATE TRIGGER user_uploads_no_{action.lower()} BEFORE {action} ON user_uploads "
            "BEGIN SELECT RAISE(ABORT, 'original uploads are immutable'); END"
        )


class UserUploads:
    """Scoped metadata and verified originals, with unchanged inline legacy reads."""

    def __init__(self, documents: DocumentStore) -> None:
        self.control = documents.control
        self.blobs = ArtifactBlobs(self.control.path)

    @staticmethod
    def _metadata(project: str, session: str, upload_id: str, filename: str, media: str) -> None:
        for identifier in (project, session, upload_id):
            _IDENTIFIER.validate_python(identifier, strict=True)
        try:
            filename_bytes = filename.encode()
        except UnicodeError:
            raise DocumentError("invalid_request", "upload name must be valid UTF-8") from None
        if (
            not filename.strip()
            or len(filename_bytes) > 240
            or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in filename)
            or filename in (".", "..")
            or re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", media) is None
            or len(media) > 128
        ):
            raise DocumentError("invalid_request", "invalid upload name or type")

    @staticmethod
    def _find(
        db: sqlite3.Connection, project: str, session: str, upload_id: str
    ) -> tuple[str, UserUpload] | None:
        row = db.execute(
            "SELECT digest,record FROM user_upload_records "
            "WHERE project=? AND session=? AND upload_id=?",
            (project, session, upload_id),
        ).fetchone()
        return None if row is None else (row[0], UserUpload.model_validate_json(row[1]))

    def _admit(self, db: sqlite3.Connection, project: str, size: int, retained: bool) -> None:
        for scope, limit, count_limit, transfers in (
            (None, MAX_TOTAL_BYTES, MAX_TOTAL_UPLOADS, 4),
            (project, MAX_PROJECT_BYTES, MAX_PROJECT_UPLOADS, 2),
        ):
            active, staged, pending, charged = db.execute(
                "SELECT count(*),coalesce(sum(bytes),0),coalesce(sum(retained),0),"
                "coalesce(sum(bytes*retained),0) FROM user_upload_transfers "
                "WHERE ? IS NULL OR project=?",
                (scope, scope),
            ).fetchone()
            if active >= transfers or staged + size > transfers * MAX_UPLOAD_BYTES:
                raise DocumentError("unavailable", "upload transfer admission is full")
            count, total = db.execute(
                "SELECT count(*),coalesce(sum(size),0) FROM user_upload_records "
                "WHERE ? IS NULL OR project=?",
                (scope, scope),
            ).fetchone()
            if retained and (count + pending >= count_limit or total + charged + size > limit):
                raise DocumentError("invalid_request", "upload retention quota is full")

    def begin(
        self,
        project: str,
        session: str,
        upload_id: str,
        filename: str,
        media: str,
        expected_size: int | None = None,
    ) -> UploadWriter:
        """Admit before receiving bytes, or reacquire a drained original transfer."""
        with storage_errors():
            return self._begin(project, session, upload_id, filename, media, expected_size)

    def _begin(
        self,
        project: str,
        session: str,
        upload_id: str,
        filename: str,
        media: str,
        expected_size: int | None,
    ) -> UploadWriter:
        self._metadata(project, session, upload_id, filename, media)
        if expected_size is not None and (
            type(expected_size) is not int or not 0 <= expected_size <= MAX_UPLOAD_BYTES
        ):
            raise DocumentError("invalid_request", "invalid upload size")
        lease: BinaryIO | None = None
        admission: UploadAdmission | None = None
        try:
            with self.control.write() as tx:
                db = tx.sql
                if (
                    db.execute(
                        "SELECT 1 FROM sessions WHERE project_id=? AND session_id=?",
                        (project, session),
                    ).fetchone()
                    is None
                ):
                    raise DocumentError("not_found", "no such upload session")
                previous = self._find(db, project, session, upload_id)
                if previous is not None and (
                    (previous[1].filename, previous[1].media_type) != (filename, media)
                    or (expected_size is not None and previous[1].size != expected_size)
                ):
                    raise DocumentError("conflict", "upload ID already names different content")
                row = db.execute(
                    "SELECT identity,filename,media,bytes,sha256 FROM user_upload_transfers "
                    "WHERE project=? AND session=? AND upload_id=?",
                    (project, session, upload_id),
                ).fetchone()
                capacity = expected_size if expected_size is not None else MAX_UPLOAD_BYTES
                if previous is not None:
                    capacity = previous[1].size
                if row is not None:
                    if (row[1], row[2]) != (filename, media) or (
                        expected_size is not None
                        and (
                            expected_size > row[3]
                            or (row[4] is not None and expected_size != row[3])
                        )
                    ):
                        raise DocumentError(
                            "conflict", "upload ID has a different pending original"
                        )
                    admission = UploadAdmission(project, session, upload_id, row[0], row[3], row[4])
                else:
                    self._admit(db, project, capacity, previous is None)
                    admission = UploadAdmission(
                        project, session, upload_id, new_id("upload"), capacity, None
                    )
                lease = acquire(self.blobs.root, project, session, upload_id)
                if row is None:
                    db.execute(
                        "INSERT INTO user_upload_transfers VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            admission.identity,
                            project,
                            session,
                            upload_id,
                            filename,
                            media,
                            capacity,
                            None,
                            int(previous is None),
                        ),
                    )
            return UploadWriter(self, admission, lease, filename, media, expected_size)
        except BaseException:
            if lease is not None:
                try:
                    if admission is not None and admission.sha256 is None:
                        self.release(admission)
                finally:
                    lease.close()
            raise

    def create(
        self, project: str, session: str, upload_id: str, filename: str, media: str, data: bytes
    ) -> UserUpload:
        """Bounded compatibility API; binary HTTP callers use begin/write/commit."""
        with self.begin(project, session, upload_id, filename, media, len(data)) as writer:
            for offset in range(0, len(data), CHUNK_BYTES):
                writer.write(data[offset : offset + CHUNK_BYTES])
            return writer.commit()

    @staticmethod
    def _transfer(db: sqlite3.Connection, admission: UploadAdmission) -> tuple[int, str | None]:
        row = db.execute(
            "SELECT bytes,sha256 FROM user_upload_transfers WHERE identity=? "
            "AND project=? AND session=? AND upload_id=?",
            (admission.identity, admission.project, admission.session, admission.upload_id),
        ).fetchone()
        if row is None:
            raise DocumentError("conflict", "upload transfer reservation is unavailable")
        return row[0], row[1]

    def pin(
        self, admission: UploadAdmission, filename: str, media: str, blob: Blob
    ) -> UserUpload | None:
        """Persist exact bytes before they can be installed, including failed retries."""
        with self.control.write() as tx:
            capacity, pinned = self._transfer(tx.sql, admission)
            if blob.size > capacity or (
                pinned is not None and (pinned, capacity) != (blob.sha256, blob.size)
            ):
                raise DocumentError("conflict", "upload ID has different pinned bytes")
            previous = self._find(tx.sql, admission.project, admission.session, admission.upload_id)
            digest = hashlib.sha256(
                (filename + "\0" + media + "\0" + blob.sha256).encode()
            ).hexdigest()
            if previous is not None:
                if previous[0] != digest or previous[1].size != blob.size:
                    raise DocumentError("conflict", "upload ID already names different content")
                return previous[1]
            tx.sql.execute(
                "UPDATE user_upload_transfers SET bytes=?,sha256=? WHERE identity=?",
                (blob.size, blob.sha256, admission.identity),
            )
        return None

    def finish(
        self, admission: UploadAdmission, filename: str, media: str, blob: Blob
    ) -> UserUpload:
        """Commit immutable metadata and release its exact reservation together."""
        with self.blobs.open(blob):
            pass
        with self.control.write() as tx:
            if self._transfer(tx.sql, admission) != (blob.size, blob.sha256):
                raise DocumentError("conflict", "upload reservation differs from installed content")
            previous = self._find(tx.sql, admission.project, admission.session, admission.upload_id)
            digest = hashlib.sha256(
                (filename + "\0" + media + "\0" + blob.sha256).encode()
            ).hexdigest()
            if previous is not None:
                if previous[0] != digest:
                    raise DocumentError("conflict", "upload ID already names different content")
                found = previous[1]
            else:
                (number,) = tx.sql.execute(
                    "SELECT coalesce(max(seq),0)+1 FROM user_upload_records"
                ).fetchone()
                path = f"shared/uploads/{admission.session}/{admission.upload_id}/source"
                extension = re.search(r"\.[a-zA-Z0-9]{1,10}$", filename)
                if extension:
                    path += extension[0].lower()
                found = UserUpload(
                    upload_id=admission.upload_id,
                    project=admission.project,
                    session=admission.session,
                    filename=filename,
                    media_type=media,
                    sha256=blob.sha256,
                    size=blob.size,
                    created_at=datetime.now(UTC).isoformat(),
                    path=path,
                    number=number,
                )
                tx.sql.execute(
                    "INSERT INTO user_upload_blobs VALUES (?,?,?,?,?,?,?,?)",
                    (
                        number,
                        admission.project,
                        admission.session,
                        admission.upload_id,
                        digest,
                        found.model_dump_json(),
                        blob.size,
                        blob.sha256,
                    ),
                )
            tx.sql.execute(
                "DELETE FROM user_upload_transfers WHERE identity=?", (admission.identity,)
            )
            return found

    def release(self, admission: UploadAdmission) -> None:
        """Only a drained caller may release its own pre-install reservation."""
        with self.control.write() as tx:
            tx.sql.execute(
                "DELETE FROM user_upload_transfers WHERE identity=? AND project=? "
                "AND session=? AND upload_id=?",
                (admission.identity, admission.project, admission.session, admission.upload_id),
            )

    def get(self, project: str, session: str, upload_id: str) -> UserUpload:
        with self.control.read() as tx:
            found = self._find(tx.sql, project, session, upload_id)
        if found is None:
            raise DocumentError("not_found", "no such upload in this session")
        return found[1]

    @contextmanager
    def open_content(self, project: str, session: str, upload_id: str) -> Generator[BinaryIO]:
        """Verify and rewind an owned stream; no transaction outlives its acquisition."""
        found = self.get(project, session, upload_id)
        with self.control.read() as tx:
            external = tx.sql.execute(
                "SELECT sha256,size FROM user_upload_blobs "
                "WHERE project=? AND session=? AND upload_id=?",
                (project, session, upload_id),
            ).fetchone()
            legacy = (
                None
                if external is not None
                else tx.sql.execute(
                    "SELECT seq FROM user_uploads WHERE project=? AND session=? AND upload_id=?",
                    (project, session, upload_id),
                ).fetchone()
            )
        if external is not None:
            if external != (found.sha256, found.size):
                raise DocumentError(
                    "unavailable", "stored upload reference failed integrity verification"
                )
            with ExitStack() as stack:
                try:
                    stream = stack.enter_context(self.blobs.open(Blob(found.sha256, found.size)))
                except (DocumentError, ValueError):
                    raise DocumentError(
                        "unavailable", "stored upload failed integrity verification"
                    ) from None
                yield stream
        else:
            if legacy is None:
                raise DocumentError("unavailable", "stored upload content is unavailable")
            with tempfile.TemporaryFile(mode="w+b") as stream:
                digest, size = hashlib.sha256(), 0
                with (
                    self.control.isolated_read() as tx,
                    tx.sql.blobopen("user_uploads", "content", legacy[0], readonly=True) as content,
                ):
                    if len(content) != found.size or found.size > MAX_UPLOAD_BYTES:
                        raise DocumentError("unavailable", "stored upload length differs")
                    while chunk := content.read(CHUNK_BYTES):
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if (size, digest.hexdigest()) != (found.size, found.sha256):
                    raise DocumentError(
                        "unavailable", "stored upload failed integrity verification"
                    )
                stream.seek(0)
                yield stream

    def content(
        self, project: str, session: str, upload_id: str, *, max_bytes: int = MAX_UPLOAD_BYTES
    ) -> bytes:
        """Bounded convenience for small documents and the existing worker protocol."""
        if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_UPLOAD_BYTES:
            raise DocumentError("invalid_request", "invalid upload content bound")
        found = self.get(project, session, upload_id)
        if found.size > max_bytes:
            raise DocumentError("invalid_request", "upload content exceeds consumer bound")
        with self.open_content(project, session, upload_id) as stream:
            return stream.read(found.size)

    def list(self, project: str, session: str, after: int = 0) -> UploadPage:
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT record FROM user_upload_records WHERE project=? AND session=? AND seq>? "
                "ORDER BY seq LIMIT 51",
                (project, session, after),
            ).fetchall()
        uploads = [UserUpload.model_validate_json(row[0]) for row in rows[:50]]
        return UploadPage(
            uploads=uploads, next_after=uploads[-1].number if len(rows) > 50 else None
        )
