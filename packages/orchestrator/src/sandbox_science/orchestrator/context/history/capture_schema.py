# SPDX-License-Identifier: MIT
"""Derived inspector storage, separate from agent-facing archive search."""

from sandbox_science.orchestrator.control.migrations import Schema


def create(schema: Schema) -> None:
    for statement in (
        """CREATE TABLE context_captures (
            project TEXT NOT NULL, session TEXT NOT NULL, run TEXT NOT NULL,
            identity TEXT NOT NULL, omitted TEXT,
            PRIMARY KEY(project,session,run,identity),
            FOREIGN KEY(project,session,run,identity)
                REFERENCES research_model_calls(project,session,run,identity)) STRICT""",
        """CREATE TABLE context_bodies (
            project TEXT NOT NULL, sha256 TEXT NOT NULL, text TEXT NOT NULL, bytes INTEGER NOT NULL,
            PRIMARY KEY(project,sha256)) STRICT""",
        """CREATE TABLE context_parts (
            project TEXT NOT NULL, session TEXT NOT NULL, run TEXT NOT NULL,
            identity TEXT NOT NULL, ordinal INTEGER NOT NULL, sha256 TEXT NOT NULL,
            metadata TEXT NOT NULL CHECK(json_valid(metadata)),
            PRIMARY KEY(project,session,run,identity,ordinal),
            FOREIGN KEY(project,session,run,identity)
                REFERENCES research_model_calls(project,session,run,identity),
            FOREIGN KEY(project,sha256) REFERENCES context_bodies(project,sha256)) STRICT""",
        "CREATE INDEX context_intent_order ON research_model_calls "
        "(project,session,run,created,identity)",
        """CREATE TABLE inspector_chunks (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, message TEXT NOT NULL,
            field TEXT NOT NULL, start INTEGER NOT NULL, text TEXT NOT NULL,
            UNIQUE(project,message,field,start),
            FOREIGN KEY(project,message) REFERENCES archive_messages(project,message_id)) STRICT""",
        """CREATE VIRTUAL TABLE inspector_trigrams USING fts5(
            text, content='', tokenize='trigram case_sensitive 1')""",
        """CREATE TABLE inspector_indexed (
            project TEXT NOT NULL, message TEXT NOT NULL, field TEXT NOT NULL,
            offset INTEGER NOT NULL, complete INTEGER NOT NULL,
            PRIMARY KEY(project,message,field),
            FOREIGN KEY(project,message) REFERENCES archive_messages(project,message_id)) STRICT""",
    ):
        schema.execute(statement)
    for table in ("context_captures", "context_parts", "context_bodies"):
        for action in ("UPDATE", "DELETE"):
            schema.execute(
                f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'captured context is immutable'); END"
            )
