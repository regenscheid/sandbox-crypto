# SPDX-License-Identifier: MIT
"""Append-only external originals; the legacy table and its content are untouched."""

from .control.migrations import Schema


def install(schema: Schema) -> None:
    """Version 74: immutable external metadata and charged, resumable ingestion."""
    schema.execute("""CREATE TABLE user_upload_blobs (
        seq INTEGER PRIMARY KEY,
        project TEXT NOT NULL, session TEXT NOT NULL, upload_id TEXT NOT NULL,
        digest TEXT NOT NULL, record TEXT NOT NULL CHECK(json_valid(record)),
        size INTEGER NOT NULL CHECK(size BETWEEN 0 AND 104857600),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64),
        UNIQUE(project,session,upload_id),
        FOREIGN KEY(project,session) REFERENCES sessions(project_id,session_id)
    ) STRICT""")
    schema.execute("""CREATE VIEW user_upload_records AS
        SELECT seq,project,session,upload_id,digest,record,size FROM user_uploads
        UNION ALL SELECT seq,project,session,upload_id,digest,record,size FROM user_upload_blobs""")
    for action in ("UPDATE", "DELETE"):
        schema.execute(
            f"CREATE TRIGGER user_upload_blobs_no_{action.lower()} BEFORE {action} "
            "ON user_upload_blobs BEGIN SELECT RAISE(ABORT,'original uploads are immutable'); END"
        )
    for target, other in (
        ("user_uploads", "user_upload_blobs"),
        ("user_upload_blobs", "user_uploads"),
    ):
        schema.execute(
            f"CREATE TRIGGER {target}_shared_identity BEFORE INSERT ON {target} "  # noqa: S608 - both table names are fixed literals above
            f"WHEN EXISTS(SELECT 1 FROM {other} WHERE seq=NEW.seq OR "
            "(project=NEW.project AND session=NEW.session AND upload_id=NEW.upload_id)) "
            "BEGIN SELECT RAISE(ABORT,'original upload identity already exists'); END"
        )
    schema.execute("""CREATE TABLE user_upload_transfers (
        identity TEXT NOT NULL UNIQUE,
        project TEXT NOT NULL, session TEXT NOT NULL, upload_id TEXT NOT NULL,
        filename TEXT NOT NULL, media TEXT NOT NULL,
        bytes INTEGER NOT NULL CHECK(bytes BETWEEN 0 AND 104857600),
        sha256 TEXT CHECK(sha256 IS NULL OR length(sha256)=64),
        retained INTEGER NOT NULL CHECK(retained IN (0,1)),
        PRIMARY KEY(project,session,upload_id),
        FOREIGN KEY(project,session) REFERENCES sessions(project_id,session_id)
    ) STRICT""")
