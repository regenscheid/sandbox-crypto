# SPDX-License-Identifier: MIT
"""Immutable user uploads, separate from agent publication provenance.

Original bytes remain authoritative. A Markdown document is an optional derivative;
delivery to a sandbox is observed independently on each explicit attachment send.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Final

from pydantic import Field, TypeAdapter

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.model import Contract

from .control.migrations import Schema
from .documents import DocumentError, DocumentStore

MAX_UPLOAD_BYTES: Final = 100 * 1024 * 1024
MAX_PROJECT_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 8 * 1024 * 1024 * 1024
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
    """Public upload boundary; readers and writers retain their store's authority."""

    def __init__(self, documents: DocumentStore) -> None:
        self.control = documents.control

    def create(
        self, project: str, session: str, upload_id: str, filename: str, media: str, data: bytes
    ) -> UserUpload:
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
            or len(data) > MAX_UPLOAD_BYTES
            or re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", media) is None
            or len(media) > 128
        ):
            raise DocumentError("invalid_request", "invalid upload name, type or size")
        sha256 = hashlib.sha256(data).hexdigest()
        digest = hashlib.sha256((filename + "\0" + media + "\0" + sha256).encode()).hexdigest()
        extension = re.search(r"\.[a-zA-Z0-9]{1,10}$", filename)
        path = f"shared/uploads/{session}/{upload_id}/source"
        if extension:
            path += extension[0].lower()
        with self.control.write() as tx:
            db = tx.sql
            previous = db.execute(
                "SELECT digest,record FROM user_uploads "
                "WHERE project=? AND session=? AND upload_id=?",
                (project, session, upload_id),
            ).fetchone()
            if previous:
                if previous[0] != digest:
                    raise DocumentError("conflict", "upload ID already names different content")
                return UserUpload.model_validate_json(previous[1])
            for scope, limit, count_limit in (
                (None, MAX_TOTAL_BYTES, 4096),
                (project, MAX_PROJECT_BYTES, 1024),
            ):
                count, size = db.execute(
                    "SELECT count(*),coalesce(sum(size),0) FROM user_uploads "
                    "WHERE ? IS NULL OR project=?",
                    (scope, scope),
                ).fetchone()
                if count >= count_limit or size + len(data) > limit:
                    raise DocumentError("invalid_request", "upload retention quota is full")
            (number,) = db.execute("SELECT coalesce(max(seq),0)+1 FROM user_uploads").fetchone()
            found = UserUpload(
                upload_id=upload_id,
                project=project,
                session=session,
                filename=filename,
                media_type=media,
                sha256=sha256,
                size=len(data),
                created_at=datetime.now(UTC).isoformat(),
                path=path,
                number=number,
            )
            db.execute(
                "INSERT INTO user_uploads VALUES (?,?,?,?,?,?,?,?)",
                (
                    number,
                    project,
                    session,
                    upload_id,
                    digest,
                    found.model_dump_json(),
                    len(data),
                    data,
                ),
            )
            return found

    def get(self, project: str, session: str, upload_id: str) -> UserUpload:
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT record FROM user_uploads WHERE project=? AND session=? AND upload_id=?",
                (project, session, upload_id),
            ).fetchone()
        if row is None:
            raise DocumentError("not_found", "no such upload in this session")
        return UserUpload.model_validate_json(row[0])

    def content(self, project: str, session: str, upload_id: str) -> bytes:
        found = self.get(project, session, upload_id)
        # A bounded original, read on an ephemeral connection like other evidence blobs.
        with self.control.isolated_read() as tx:
            row = tx.sql.execute(
                "SELECT content FROM user_uploads WHERE project=? AND session=? AND upload_id=?",
                (project, session, upload_id),
            ).fetchone()
        data = bytes(row[0]) if row is not None else b""
        if len(data) != found.size or hashlib.sha256(data).hexdigest() != found.sha256:
            raise DocumentError("unavailable", "stored upload failed integrity verification")
        return data

    def list(self, project: str, session: str, after: int = 0) -> UploadPage:
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT record FROM user_uploads WHERE project=? AND session=? AND seq>? "
                "ORDER BY seq LIMIT 51",
                (project, session, after),
            ).fetchall()
        uploads = [UserUpload.model_validate_json(row[0]) for row in rows[:50]]
        return UploadPage(
            uploads=uploads, next_after=uploads[-1].number if len(rows) > 50 else None
        )
