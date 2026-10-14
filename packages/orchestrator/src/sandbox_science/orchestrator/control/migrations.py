# SPDX-License-Identifier: MIT
"""The control store's schema: one ordered list of migrations and one version.

The version is SQLite's ``user_version``, and a control store carries
``APPLICATION_ID`` as its ``application_id``. Version 2 is the baseline,
``baseline.sql``, and each later step in ``MIGRATIONS`` takes the database one version
further. A writer runs the steps its database lacks when it first opens the store, all
in one ``BEGIN IMMEDIATE`` transaction, so a step that fails leaves the database as it
was. A reader only reads the current version. Any other file is refused, and checked
through a read-only connection first, so it is left unchanged: a store in a newer
version, a file that is not a control store, and a development store from before the
baseline (version 1), whose documents are exported and imported again into a fresh
store (docs/LIVING-DOCUMENTS.md).

To change the schema, append a step to ``MIGRATIONS``. Never edit a step that has
shipped, or ``baseline.sql``: databases made with them keep them. A step gets a
``Schema``, which runs one statement at a time and refuses transaction control, since
the step runs inside the writer's transaction. The steps run with foreign keys off, so
that a step can rebuild a table (create the new one, copy the rows, drop the old one,
rename: ``rebuild``); ``migrate`` checks every foreign key before the change commits.
A row the new schema refuses leaves the store at its version, with a ``ControlError``
(``unavailable``) that names the table and the check.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Final, cast

from .errors import BUSY, INTERNAL, NOT_FOUND, UNAVAILABLE, ControlError

BASELINE: Final = 2
"""The version ``baseline.sql`` creates. Version 1 was the development stores before it."""

APPLICATION_ID: Final = 0x53536373
"""The ``application_id`` of a control store ("SScs"), which ``baseline.sql`` sets."""

_TRANSACTION: Final = re.compile(
    r"(?:\s|--[^\n]*\n)*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|VACUUM)\b", re.IGNORECASE
)


class Schema:
    """What a migration step may do: run statements in the writer's transaction."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        if _TRANSACTION.match(sql):
            raise ControlError(INTERNAL, "a migration step runs inside the writer's transaction")
        return self._db.execute(sql, parameters)


type Migration = Callable[[Schema], None]


class RefusedRowError(Exception):
    """A row of the store that a migration step's new definition refuses."""


_KEEP: Final = (
    "To keep its documents, export them with the harness version that made the store and "
    'import them into a fresh store (docs/LIVING-DOCUMENTS.md, "A store a migration '
    'refuses").'
)


def _broken_key(db: sqlite3.Connection | Schema, table: str | None = None) -> str | None:
    """The first row that names no row of the table its foreign key points to, among the
    rows of ``table`` and of the rows that point to it, or anywhere when ``table`` is None."""
    for child, rowid, parent, _ in db.execute("PRAGMA foreign_key_check").fetchall():
        if table is None or table in (child, parent):
            return f"row {rowid} of {child} names no row of {parent}"
    return None


def _baseline(schema: Schema) -> None:
    script = resources.files(__package__).joinpath("baseline.sql").read_text(encoding="utf-8")
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            schema.execute(statement)
            statement = ""
    if statement.strip():
        raise ControlError(INTERNAL, "the control store's baseline schema is incomplete")


def _projects(schema: Schema) -> None:
    """Version 3: projects, their sessions, and the sessions' runs (projects.py)."""
    for statement in (
        """CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    policy TEXT NOT NULL,
    default_environment_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT""",
        """CREATE TABLE sessions (
    project_id TEXT NOT NULL REFERENCES projects (project_id),
    session_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    dedicated INTEGER NOT NULL CHECK (dedicated IN (0, 1)),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    kernel_notice_lifetime TEXT,
    PRIMARY KEY (project_id, session_id)
) STRICT""",
        """CREATE TABLE runs (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (project_id, session_id, name),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id)
) STRICT""",
        """CREATE TRIGGER runs_no_update BEFORE UPDATE ON runs
BEGIN SELECT RAISE(ABORT, 'run records are written once'); END""",
        """CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'run records are written once'); END""",
    ):
        schema.execute(statement)


def rebuild(schema: Schema, table: str, create: str) -> None:
    """Make ``table`` again as ``create`` (``CREATE TABLE <table> (...)``) defines it, with
    its rows and rowids, its indexes and triggers, the other tables' triggers that name it,
    and the store's views: SQLite cannot add a constraint to a table. A row the new
    definition refuses fails the step with ``RefusedRowError``."""
    if not create.startswith(f"CREATE TABLE {table} ("):
        raise ControlError(INTERNAL, f"a rebuild of {table} needs its CREATE TABLE")
    attached = schema.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
        "AND sql IS NOT NULL ORDER BY rowid",
        (table,),
    ).fetchall()
    named = re.compile(rf"\b{re.escape(table)}\b")
    others = [
        (name, sql)
        for name, sql in schema.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name<>? "
            "ORDER BY rowid",
            (table,),
        ).fetchall()
        if named.search(sql)
    ]
    views = schema.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY rowid"
    ).fetchall()
    for name, _ in views:
        schema.execute(f'DROP VIEW "{name}"')
    for name, _ in others:
        schema.execute(f'DROP TRIGGER "{name}"')
    columns = ", ".join(
        f'"{row[1]}"' for row in schema.execute(f'PRAGMA table_info("{table}")').fetchall()
    )
    schema.execute(create.replace(f"CREATE TABLE {table} (", f"CREATE TABLE {table}__new (", 1))
    try:
        schema.execute(
            f"INSERT INTO {table}__new (rowid, {columns}) "  # noqa: S608 - the store's own names
            f"SELECT rowid, {columns} FROM {table} ORDER BY rowid"
        )
    except sqlite3.IntegrityError as error:  # a row the new definition refuses
        raise RefusedRowError(f"a row of {table} fails its check ({error})") from None
    schema.execute(f"DROP TABLE {table}")
    schema.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
    for (sql,) in attached:
        schema.execute(sql)
    for _, sql in others:
        schema.execute(sql)
    for _, sql in views:
        schema.execute(sql)
    # The rows keep their keys, so only a new foreign key or a new key can break one.
    if (broken := _broken_key(schema, table)) is not None:
        raise RefusedRowError(f"the new definition of {table} breaks a foreign key: {broken}")


def _comment_checks(schema: Schema) -> None:
    """Version 4: comments and their bodies and math checks check their own values when
    they are written (decision 3), so reads take them as they are."""
    for table, create in (
        (
            "annotation_threads",
            """CREATE TABLE annotation_threads (
    thread_id TEXT PRIMARY KEY CHECK (length(CAST(thread_id AS BLOB)) BETWEEN 1 AND 128),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    anchor TEXT NOT NULL CHECK (json_valid(anchor)),
    opener TEXT NOT NULL
        CHECK (json_valid(opener) AND length(CAST(opener AS BLOB)) <= 8192),
    created_at REAL NOT NULL) STRICT""",
        ),
        (
            "annotation_entries",
            """CREATE TABLE annotation_entries (
    entry_id TEXT PRIMARY KEY CHECK (length(CAST(entry_id AS BLOB)) BETWEEN 1 AND 128),
    thread_id TEXT NOT NULL REFERENCES annotation_threads(thread_id),
    number INTEGER NOT NULL CHECK (number BETWEEN 1 AND 999999999),
    kind TEXT NOT NULL
        CHECK (kind IN ('create', 'reply', 'edit', 'edit_body', 'resolve', 'reopen')),
    author TEXT NOT NULL
        CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 8192),
    body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) <= 16384),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    created_at REAL NOT NULL, UNIQUE(thread_id, number)) STRICT""",
        ),
        (
            "annotation_operations",
            """CREATE TABLE annotation_operations (
    project_id TEXT NOT NULL, actor TEXT NOT NULL, operation_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL
        CHECK (length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
    result TEXT NOT NULL CHECK (json_valid(result)),
    PRIMARY KEY(project_id,actor,operation_id)) STRICT""",
        ),
        (
            "annotation_body_revisions",
            """CREATE TABLE annotation_body_revisions (
    entry_id TEXT NOT NULL REFERENCES annotation_entries(entry_id),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    history_entry TEXT NOT NULL REFERENCES annotation_entries(entry_id),
    body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) <= 16384),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    block_map TEXT NOT NULL
        CHECK (json_valid(block_map) AND length(CAST(block_map AS BLOB)) <= 16777216),
    author TEXT NOT NULL
        CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 8192),
    owners TEXT NOT NULL
        CHECK (json_valid(owners) AND length(CAST(owners AS BLOB)) <= 16777216),
    next_block INTEGER NOT NULL CHECK (next_block BETWEEN 0 AND 999999999),
    next_span INTEGER NOT NULL CHECK (next_span BETWEEN 0 AND 999999999),
    PRIMARY KEY(entry_id,revision)) STRICT""",
        ),
        (
            "comment_math_sources",
            """CREATE TABLE comment_math_sources (
    entry_id TEXT PRIMARY KEY REFERENCES annotation_entries(entry_id),
    document TEXT NOT NULL REFERENCES documents(document_id),
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    target INTEGER NOT NULL CHECK (target BETWEEN 0 AND 999999999),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed BETWEEN 0 AND 999999999),
    macro_revision INTEGER NOT NULL DEFAULT 0 CHECK (macro_revision BETWEEN 0 AND 999999999),
    retry_requested INTEGER NOT NULL DEFAULT 0 CHECK (retry_requested IN (0, 1)),
    blocked TEXT CHECK (blocked IS NULL OR length(CAST(blocked AS BLOB)) <= 4096)) STRICT""",
        ),
        (
            "math_comment_jobs",
            """CREATE TABLE math_comment_jobs (
    check_id TEXT PRIMARY KEY REFERENCES math_jobs(check_id),
    entry_id TEXT NOT NULL REFERENCES annotation_entries(entry_id),
    macro_revision INTEGER NOT NULL CHECK (macro_revision BETWEEN 0 AND 999999999),
    macro_sha256 TEXT NOT NULL
        CHECK (length(macro_sha256) = 64 AND macro_sha256 NOT GLOB '*[^0-9a-f]*')) STRICT""",
        ),
    ):
        rebuild(schema, table, create)


def _response_checks(schema: Schema) -> None:
    """Version 5: responses to findings, their bodies and their math checks check their
    own values when they are written (decision 3), so reads take them as they are."""
    for table, create in (
        (
            "review_response_bodies",
            """CREATE TABLE review_response_bodies (
    source TEXT NOT NULL CHECK (length(CAST(source AS BLOB)) BETWEEN 1 AND 128),
    finding_id TEXT NOT NULL CHECK (length(CAST(finding_id AS BLOB)) BETWEEN 1 AND 128),
    decision_revision INTEGER NOT NULL CHECK (decision_revision BETWEEN 1 AND 999999999),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) <= 32768),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    block_map TEXT NOT NULL
        CHECK (json_valid(block_map) AND length(CAST(block_map AS BLOB)) <= 16777216),
    author TEXT NOT NULL
        CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 8192),
    owners TEXT NOT NULL
        CHECK (json_valid(owners) AND length(CAST(owners AS BLOB)) <= 16777216),
    next_block INTEGER NOT NULL CHECK (next_block BETWEEN 0 AND 999999999),
    next_span INTEGER NOT NULL CHECK (next_span BETWEEN 0 AND 999999999),
    identity_floor INTEGER NOT NULL CHECK (identity_floor BETWEEN 1 AND revision),
    held TEXT CHECK (held IS NULL OR length(CAST(held AS BLOB)) <= 4096),
    PRIMARY KEY(source,revision),
    FOREIGN KEY(finding_id,decision_revision) REFERENCES review_transitions(finding_id,revision))
    STRICT""",
        ),
        (
            "response_math_sources",
            """CREATE TABLE response_math_sources (
    source TEXT PRIMARY KEY CHECK (length(CAST(source AS BLOB)) BETWEEN 1 AND 128),
    finding_id TEXT NOT NULL CHECK (length(CAST(finding_id AS BLOB)) BETWEEN 1 AND 128),
    decision_revision INTEGER NOT NULL CHECK (decision_revision BETWEEN 1 AND 999999999),
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    session TEXT NOT NULL CHECK (length(CAST(session AS BLOB)) BETWEEN 1 AND 128),
    target INTEGER NOT NULL CHECK (target BETWEEN 0 AND 999999999),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed BETWEEN 0 AND 999999999),
    retry_requested INTEGER NOT NULL DEFAULT 0 CHECK (retry_requested IN (0, 1)),
    blocked TEXT CHECK (blocked IS NULL OR length(CAST(blocked AS BLOB)) <= 4096),
    UNIQUE(finding_id,decision_revision),
    FOREIGN KEY(finding_id,decision_revision) REFERENCES review_transitions(finding_id,revision))
    STRICT""",
        ),
        (
            "review_response_discovery",
            """CREATE TABLE review_response_discovery (
    transition_seq INTEGER PRIMARY KEY,
    finding_id TEXT NOT NULL CHECK (length(CAST(finding_id AS BLOB)) BETWEEN 1 AND 128),
    decision_revision INTEGER NOT NULL CHECK (decision_revision BETWEEN 1 AND 999999999),
    reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) <= 4096),
    UNIQUE(finding_id,decision_revision),
    FOREIGN KEY(finding_id,decision_revision) REFERENCES review_transitions(finding_id,revision))
    STRICT""",
        ),
        (
            "review_response_pins",
            """CREATE TABLE review_response_pins (
    review_id TEXT NOT NULL REFERENCES review_runs(review_id),
    finding_id TEXT NOT NULL CHECK (length(CAST(finding_id AS BLOB)) BETWEEN 1 AND 128),
    decision_revision INTEGER NOT NULL CHECK (decision_revision BETWEEN 1 AND 999999999),
    body_revision INTEGER NOT NULL CHECK (body_revision BETWEEN 0 AND 999999999),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(review_id,finding_id),
    FOREIGN KEY(finding_id,decision_revision) REFERENCES review_transitions(finding_id,revision))
    STRICT""",
        ),
        (
            "math_response_jobs",
            """CREATE TABLE math_response_jobs (
    check_id TEXT PRIMARY KEY REFERENCES math_jobs(check_id),
    source TEXT NOT NULL REFERENCES response_math_sources(source)) STRICT""",
        ),
    ):
        rebuild(schema, table, create)


def _math_checks(schema: Schema) -> None:
    """Version 6: math checks, their observations and owners, and project macros and
    their pins check their own values when they are written (decision 3)."""
    for table, create in (
        (
            "math_jobs",
            """CREATE TABLE math_jobs (
    check_id TEXT PRIMARY KEY CHECK (length(CAST(check_id AS BLOB)) BETWEEN 1 AND 128),
    document TEXT REFERENCES documents(document_id),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    source_sha256 TEXT NOT NULL
        CHECK (length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK (state IN ('running', 'finished')),
    owner TEXT CHECK (owner IS NULL OR length(CAST(owner AS BLOB)) <= 128),
    lease_until REAL CHECK (lease_until IS NULL OR lease_until BETWEEN -1e18 AND 1e18),
    cursor INTEGER NOT NULL DEFAULT 0 CHECK (cursor BETWEEN 0 AND 20000),
    spans INTEGER CHECK (spans IS NULL OR spans BETWEEN 0 AND 20000),
    available INTEGER NOT NULL DEFAULT 0 CHECK (available BETWEEN 0 AND 1000000000),
    rejected INTEGER NOT NULL DEFAULT 0 CHECK (rejected BETWEEN 0 AND 1000000000),
    unavailable INTEGER NOT NULL DEFAULT 0 CHECK (unavailable BETWEEN 0 AND 1000000000),
    configuration TEXT CHECK (configuration IS NULL
        OR (json_valid(configuration) AND length(CAST(configuration AS BLOB)) <= 4096)),
    runtime TEXT CHECK (runtime IS NULL OR length(CAST(runtime AS BLOB)) <= 256),
    reason TEXT CHECK (reason IS NULL OR length(CAST(reason AS BLOB)) <= 4096),
    attribution_cursor INTEGER
        CHECK (attribution_cursor IS NULL OR attribution_cursor BETWEEN 0 AND revision),
    created REAL NOT NULL CHECK (created BETWEEN -1e18 AND 1e18),
    ended REAL CHECK (ended IS NULL OR ended BETWEEN -1e18 AND 1e18)) STRICT""",
        ),
        (
            "math_sources",
            """CREATE TABLE math_sources (
    document TEXT PRIMARY KEY REFERENCES documents(document_id),
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    target INTEGER NOT NULL CHECK (target BETWEEN 0 AND 999999999),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed BETWEEN 0 AND 999999999),
    blocked TEXT CHECK (blocked IS NULL OR length(CAST(blocked AS BLOB)) <= 4096),
    retry_requested INTEGER NOT NULL DEFAULT 0 CHECK (retry_requested IN (0, 1))) STRICT""",
        ),
        (
            "math_block_owners",
            """CREATE TABLE math_block_owners (
    document TEXT NOT NULL REFERENCES documents(document_id), block TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    author TEXT NOT NULL CHECK (json_valid(author)),
    PRIMARY KEY(document,block)) STRICT""",
        ),
        (
            "math_observations",
            """CREATE TABLE math_observations (
    check_id TEXT NOT NULL REFERENCES math_jobs(check_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    block TEXT NOT NULL, span TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
        CHECK (json_valid(payload) AND length(CAST(payload AS BLOB)) <= 32768),
    bytes INTEGER NOT NULL CHECK (bytes >= 0),
    PRIMARY KEY(check_id,ordinal)) STRICT""",
        ),
        (
            "math_job_owners",
            """CREATE TABLE math_job_owners (
    check_id TEXT NOT NULL REFERENCES math_jobs(check_id), block TEXT NOT NULL,
    revision INTEGER CHECK (revision IS NULL OR revision BETWEEN 1 AND 999999999),
    author TEXT CHECK (author IS NULL
        OR (json_valid(author) AND length(CAST(author AS BLOB)) <= 8192)),
    bytes INTEGER NOT NULL CHECK (bytes >= 0),
    PRIMARY KEY(check_id,block)) STRICT""",
        ),
        (
            "project_macro_revisions",
            """CREATE TABLE project_macro_revisions (
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    definitions TEXT NOT NULL
        CHECK (json_valid(definitions) AND length(CAST(definitions AS BLOB)) <= 16384),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    author TEXT NOT NULL CHECK (json_valid(author)),
    PRIMARY KEY(project,revision)) STRICT""",
        ),
        (
            "project_macro_operations",
            """CREATE TABLE project_macro_operations (
    project TEXT NOT NULL, actor TEXT NOT NULL, operation TEXT NOT NULL,
    fingerprint TEXT NOT NULL
        CHECK (length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 0 AND 999999999),
    PRIMARY KEY(project,actor,operation)) STRICT""",
        ),
        (
            "math_macro_pins",
            """CREATE TABLE math_macro_pins (
    check_id TEXT PRIMARY KEY REFERENCES math_jobs(check_id),
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 0 AND 999999999),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    anchors TEXT NOT NULL
        CHECK (json_valid(anchors) AND length(CAST(anchors AS BLOB)) <= 16384)) STRICT""",
        ),
        (
            "response_macro_anchors",
            """CREATE TABLE response_macro_anchors (
    source TEXT PRIMARY KEY,
    anchors TEXT NOT NULL
        CHECK (json_valid(anchors) AND length(CAST(anchors AS BLOB)) <= 16384)) STRICT""",
        ),
        (
            "math_macro_contexts",
            """CREATE TABLE math_macro_contexts (
    check_id TEXT PRIMARY KEY REFERENCES math_jobs(check_id),
    definitions TEXT NOT NULL
        CHECK (json_valid(definitions) AND length(CAST(definitions AS BLOB)) <= 65536),
    problems TEXT NOT NULL
        CHECK (json_valid(problems) AND length(CAST(problems AS BLOB)) <= 32768),
    held INTEGER NOT NULL CHECK (held IN (0, 1)),
    context_key TEXT NOT NULL
        CHECK (length(context_key) = 64 AND context_key NOT GLOB '*[^0-9a-f]*')) STRICT""",
        ),
        (
            "review_macro_pins",
            """CREATE TABLE review_macro_pins (
    review_id TEXT NOT NULL, finding_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision BETWEEN 0 AND 999999999),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(review_id,finding_id)) STRICT""",
        ),
    ):
        rebuild(schema, table, create)


def _evidence_checks(schema: Schema) -> None:
    """Version 7: artifacts, the reviewer's feed and evidence, and session plans check
    their own values when they are written (decision 3); artifact revisions with a
    trigger on insert, so their bytes are not copied."""
    for table, create in (
        (
            "artifact_objects",
            """CREATE TABLE artifact_objects (
    id TEXT PRIMARY KEY CHECK (length(CAST(id AS BLOB)) BETWEEN 1 AND 128),
    project TEXT NOT NULL, path TEXT NOT NULL,
    head INTEGER NOT NULL CHECK (head >= 0),
    UNIQUE(project,path)) STRICT""",
        ),
        (
            "artifact_operations",
            """CREATE TABLE artifact_operations (
    project TEXT NOT NULL, actor TEXT NOT NULL, operation TEXT NOT NULL,
    digest TEXT NOT NULL CHECK (length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*'),
    request TEXT NOT NULL
        CHECK (json_valid(request) AND length(CAST(request AS BLOB)) <= 8192),
    state TEXT NOT NULL CHECK (state IN ('intent', 'transfer', 'done')),
    worker TEXT CHECK (worker IS NULL
        OR (json_valid(worker) AND length(CAST(worker AS BLOB)) <= 16384)),
    receipt TEXT CHECK (receipt IS NULL
        OR (json_valid(receipt) AND length(CAST(receipt AS BLOB)) <= 16384)),
    PRIMARY KEY(project,actor,operation)) STRICT""",
        ),
        (
            "artifact_events",
            """CREATE TABLE artifact_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
    record TEXT NOT NULL
        CHECK (json_valid(record) AND length(CAST(record AS BLOB)) <= 16384)) STRICT""",
        ),
        (
            "review_feed",
            """CREATE TABLE review_feed (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, session TEXT NOT NULL,
    source_key TEXT NOT NULL CHECK (length(CAST(source_key AS BLOB)) BETWEEN 1 AND 1024),
    kind TEXT NOT NULL CHECK (length(CAST(kind AS BLOB)) BETWEEN 1 AND 32),
    locator TEXT NOT NULL
        CHECK (json_valid(locator) AND length(CAST(locator AS BLOB)) <= 4096),
    payload TEXT NOT NULL
        CHECK (json_valid(payload) AND length(CAST(payload AS BLOB)) <= 262144),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    characters INTEGER NOT NULL CHECK (characters >= 0),
    structural INTEGER NOT NULL CHECK (structural >= 0),
    artifacts INTEGER NOT NULL CHECK (artifacts >= 0),
    cells INTEGER NOT NULL CHECK (cells >= 0),
    effects INTEGER NOT NULL CHECK (effects IN (0, 1)),
    created REAL NOT NULL, UNIQUE(project,session,source_key)) STRICT""",
        ),
        (
            "review_evidence",
            """CREATE TABLE review_evidence (
    evidence_id TEXT PRIMARY KEY CHECK (length(CAST(evidence_id AS BLOB)) BETWEEN 1 AND 128),
    review_id TEXT NOT NULL REFERENCES review_runs(review_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (length(CAST(kind AS BLOB)) BETWEEN 1 AND 32),
    locator TEXT NOT NULL
        CHECK (json_valid(locator) AND length(CAST(locator AS BLOB)) <= 4096),
    payload TEXT NOT NULL
        CHECK (json_valid(payload) AND length(CAST(payload AS BLOB)) <= 262144),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    UNIQUE(review_id,ordinal)) STRICT""",
        ),
        (
            "session_plan_records",
            """CREATE TABLE session_plan_records (
    project TEXT NOT NULL, session TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
    number INTEGER NOT NULL CHECK (number >= 0), key TEXT NOT NULL,
    record TEXT NOT NULL
        CHECK (json_valid(record) AND length(CAST(record AS BLOB)) <= 81920),
    bytes INTEGER NOT NULL CHECK (bytes = length(CAST(record AS BLOB)) + 512),
    PRIMARY KEY(project,session,kind,id,number)) STRICT""",
        ),
        (
            "session_plan_projections",
            """CREATE TABLE session_plan_projections (
    project TEXT NOT NULL, session TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
    record TEXT NOT NULL
        CHECK (json_valid(record) AND length(CAST(record AS BLOB)) <= 81920),
    bytes INTEGER NOT NULL CHECK (bytes = length(CAST(record AS BLOB)) + 512),
    PRIMARY KEY(project,session,kind,id)) STRICT""",
        ),
    ):
        rebuild(schema, table, create)
    # artifact_revisions holds the artifacts' bytes: a rebuild would copy them all, hold the
    # write lock as long, and leave the file twice its size. Its rows are immutable (a
    # trigger refuses updates), so a check of each insert does what a CHECK would.
    row = schema.execute(
        "SELECT rowid FROM artifact_revisions WHERE NOT (number >= 1 AND json_valid(record) "
        "AND length(CAST(record AS BLOB)) <= 16384) LIMIT 1"
    ).fetchone()
    if row is not None:
        raise RefusedRowError(
            f"a row of artifact_revisions fails its check (row {row[0]}: its number, or its "
            "record's JSON or 16 KiB bound)"
        )
    schema.execute(
        "CREATE TRIGGER artifact_revisions_check BEFORE INSERT ON artifact_revisions "
        "WHEN NOT (NEW.number >= 1 AND json_valid(NEW.record) "
        "AND length(CAST(NEW.record AS BLOB)) <= 16384) BEGIN "
        "SELECT RAISE(ABORT, 'CHECK constraint failed: artifact_revisions'); END"
    )


_ID: Final = "length(CAST({0} AS BLOB)) BETWEEN 1 AND 128"


def _run_service(schema: Schema) -> None:
    """Version 8: a session's chat messages, its run lease and its runs' events
    (docs/specs/run-service.md, RS-4, RS-5, RS-13)."""
    for statement in (
        f"""CREATE TABLE session_messages (
    number INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE CHECK ({_ID.format("message_id")}),
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    text TEXT NOT NULL CHECK (length(CAST(text AS BLOB)) BETWEEN 1 AND 65536),
    author TEXT NOT NULL CHECK ({_ID.format("author")}),
    created_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'delivered', 'refused', 'held')),
    run TEXT CHECK (run IS NULL OR length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    offered TEXT CHECK (offered IS NULL OR length(CAST(offered AS BLOB)) BETWEEN 1 AND 256),
    reason TEXT CHECK (reason IS NULL OR length(CAST(reason AS BLOB)) <= 1024),
    updated_at TEXT NOT NULL,
    CHECK ((state = 'delivered') = (run IS NOT NULL)),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id)
) STRICT""",
        "CREATE INDEX session_messages_by_state "
        "ON session_messages (project_id, session_id, state, number)",
        """CREATE TRIGGER session_messages_kept BEFORE DELETE ON session_messages
BEGIN SELECT RAISE(ABORT, 'messages are kept'); END""",
        """CREATE TABLE run_leases (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run TEXT NOT NULL CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    holder TEXT NOT NULL CHECK (length(CAST(holder AS BLOB)) BETWEEN 1 AND 128),
    started_at TEXT NOT NULL,
    renewed_at REAL NOT NULL,
    PRIMARY KEY (project_id, session_id),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id)
) STRICT""",
        """CREATE TABLE run_events (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run TEXT NOT NULL CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    seq INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 999999999),
    at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN
        ('started', 'user', 'model', 'assistant', 'tool', 'notice', 'waiting', 'stopped')),
    data TEXT NOT NULL CHECK (json_valid(data) AND json_type(data) = 'object'
        AND length(CAST(data AS BLOB)) <= 65536),
    PRIMARY KEY (project_id, session_id, run, seq),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id)
) STRICT""",
        """CREATE TRIGGER run_events_no_update BEFORE UPDATE ON run_events
BEGIN SELECT RAISE(ABORT, 'run events are written once'); END""",
        """CREATE TRIGGER run_events_no_delete BEFORE DELETE ON run_events
BEGIN SELECT RAISE(ABORT, 'run events are written once'); END""",
    ):
        schema.execute(statement)


def _run_service_control(schema: Schema) -> None:
    """Version 9: what the run service reads besides messages: the user's cancels, its
    own heartbeat, and each project's model budget (run-service spec RS-3, RS-6a, RS-11)."""
    for statement in (
        """CREATE TABLE run_cancels (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run TEXT NOT NULL CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    requested_at TEXT NOT NULL,
    PRIMARY KEY (project_id, session_id, run),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id)
) STRICT""",
        """CREATE TABLE service_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    holder TEXT NOT NULL CHECK (length(CAST(holder AS BLOB)) BETWEEN 1 AND 128),
    renewed_at REAL NOT NULL
) STRICT""",
        """CREATE TABLE project_budgets (
    project_id TEXT PRIMARY KEY REFERENCES projects (project_id),
    main_tokens INTEGER NOT NULL CHECK (main_tokens BETWEEN 0 AND 1000000000000),
    updated_at TEXT NOT NULL
) STRICT""",
        # What a project's runs spent: their assistant events, by run name (RS-6a).
        "CREATE INDEX run_events_by_kind ON run_events (project_id, kind, run)",
    ):
        schema.execute(statement)


def _immutable_update(schema: Schema, trigger: str, sql: str) -> None:
    """Run ``sql`` on an append-only table, lifting its update ``trigger`` for it."""
    (create,) = schema.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
    ).fetchone()
    schema.execute(f'DROP TRIGGER "{trigger}"')
    schema.execute(sql)
    schema.execute(create)


_EMPTY_SETTING: Final = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
"""The SHA-256 of the empty project macro setting, ``{}`` (revision 0)."""

_UNPLACED: Final = (
    # (what a row is, the query that finds the first one version 10 cannot carry)
    (
        "a document macro pin of no revision",
        "SELECT p.document FROM document_macro_pins p WHERE NOT EXISTS(SELECT 1 FROM revisions r "
        "WHERE r.document_id=p.document AND r.number=p.revision)",
    ),
    (
        "a document macro pin out of range",
        "SELECT document FROM document_macro_pins WHERE typeof(project_revision)!='integer' "
        "OR project_revision NOT BETWEEN 0 AND 999999999",
    ),
    (
        "an explanation macro revision of no body revision",
        "SELECT p.source FROM response_macro_revisions p WHERE NOT EXISTS(SELECT 1 FROM "
        "review_response_bodies b WHERE b.source=p.source AND b.revision=p.body_revision)",
    ),
    (
        "an explanation macro revision out of range",
        "SELECT source FROM response_macro_revisions WHERE typeof(project_revision)!='integer' "
        "OR project_revision NOT BETWEEN 0 AND 999999999",
    ),
    (
        "a review macro pin of no response pin",
        "SELECT p.review_id FROM review_macro_pins p WHERE NOT EXISTS(SELECT 1 FROM "
        "review_response_pins r WHERE r.review_id=p.review_id AND r.finding_id=p.finding_id)",
    ),
    (
        "a review macro pin whose digest is not its setting's",
        "SELECT p.review_id FROM review_macro_pins p JOIN review_findings f USING(finding_id) "
        "WHERE p.sha256 IS NOT (CASE WHEN p.revision=0 THEN ? ELSE (SELECT r.sha256 FROM "
        "project_macro_revisions r WHERE r.project=f.project AND r.revision=p.revision) END)",
    ),
    (
        "a math macro pin of no check",
        "SELECT p.check_id FROM math_macro_pins p WHERE NOT EXISTS(SELECT 1 FROM math_jobs j "
        "WHERE j.check_id=p.check_id)",
    ),
    (
        "a math macro pin of another project than its check's",
        "SELECT p.check_id FROM math_macro_pins p JOIN math_job_scope s USING(check_id) "
        "WHERE p.project IS NOT s.project",
    ),
    (
        "a math macro pin whose digest is not its setting's",
        "SELECT check_id FROM math_macro_pins p WHERE p.sha256 IS NOT (CASE WHEN p.revision=0 "
        "THEN ? ELSE (SELECT r.sha256 FROM project_macro_revisions r WHERE r.project=p.project "
        "AND r.revision=p.revision) END)",
    ),
    (
        "a math macro pin with anchors for a check of no explanation",
        "SELECT check_id FROM math_macro_pins p WHERE json(anchors)!='[]' AND NOT EXISTS("
        "SELECT 1 FROM math_response_jobs r WHERE r.check_id=p.check_id)",
    ),
    (
        "a math macro context of no check",
        "SELECT c.check_id FROM math_macro_contexts c WHERE NOT EXISTS(SELECT 1 FROM math_jobs j "
        "WHERE j.check_id=c.check_id)",
    ),
)


def _macro_revisions(schema: Schema) -> None:
    """Version 10: a math check, a document revision, a response body revision and a
    review's response pin record the project macro revision they used (decision 7);
    the effective macro context is derived when read, and the six tables that pinned
    it go (B-12). A pin this cannot carry, or whose anchors differ from those derived
    for it, refuses the migration rather than being dropped."""
    for what, query in _UNPLACED:
        parameters = (_EMPTY_SETTING,) if "?" in query else ()
        found = schema.execute(query + " LIMIT 1", parameters).fetchone()
        if found is not None:
            raise RefusedRowError(f"{what} ({found[0]})")
    revision = "INTEGER CHECK (macro_revision IS NULL OR macro_revision BETWEEN 0 AND 999999999)"
    for table in ("revisions", "math_jobs", "review_response_bodies", "review_response_pins"):
        schema.execute(f"ALTER TABLE {table} ADD COLUMN macro_revision {revision}")
    schema.execute(
        "ALTER TABLE math_jobs ADD COLUMN macro_held INTEGER "
        "CHECK (macro_held IS NULL OR macro_held IN (0, 1))"
    )
    schema.execute(
        "UPDATE math_jobs SET macro_revision=(SELECT p.revision FROM math_macro_pins p "
        "WHERE p.check_id=math_jobs.check_id),macro_held=(SELECT c.held FROM "
        "math_macro_contexts c WHERE c.check_id=math_jobs.check_id)"
    )
    # Only the rows with a pin are rewritten.
    _immutable_update(
        schema,
        "revisions_no_update",
        "UPDATE revisions SET macro_revision=(SELECT p.project_revision FROM "
        "document_macro_pins p WHERE p.document=revisions.document_id "
        "AND p.revision=revisions.number) WHERE EXISTS(SELECT 1 FROM document_macro_pins p "
        "WHERE p.document=revisions.document_id AND p.revision=revisions.number)",
    )
    _immutable_update(
        schema,
        "review_response_bodies_update",
        "UPDATE review_response_bodies SET macro_revision=(SELECT p.project_revision FROM "
        "response_macro_revisions p WHERE p.source=review_response_bodies.source "
        "AND p.body_revision=review_response_bodies.revision) WHERE EXISTS(SELECT 1 FROM "
        "response_macro_revisions p WHERE p.source=review_response_bodies.source "
        "AND p.body_revision=review_response_bodies.revision)",
    )
    _immutable_update(
        schema,
        "review_response_pins_update",
        "UPDATE review_response_pins SET macro_revision=(SELECT p.revision FROM "
        "review_macro_pins p WHERE p.review_id=review_response_pins.review_id "
        "AND p.finding_id=review_response_pins.finding_id) WHERE EXISTS(SELECT 1 FROM "
        "review_macro_pins p WHERE p.review_id=review_response_pins.review_id "
        "AND p.finding_id=review_response_pins.finding_id)",
    )
    _same_anchors(schema)
    # A check records the revision it used and whether it was held once, for good.
    schema.execute(
        "CREATE TRIGGER math_jobs_macros_kept BEFORE UPDATE OF macro_revision,macro_held "
        "ON math_jobs WHEN (OLD.macro_revision IS NOT NULL "
        "AND NEW.macro_revision IS NOT OLD.macro_revision) OR (OLD.macro_held IS NOT NULL "
        "AND NEW.macro_held IS NOT OLD.macro_held) "
        "BEGIN SELECT RAISE(ABORT,'a check keeps its macro revision and held state'); END"
    )
    for table in (
        "math_macro_pins",
        "document_macro_pins",
        "response_macro_anchors",
        "response_macro_revisions",
        "math_macro_contexts",
        "review_macro_pins",
    ):
        schema.execute(f"DROP TABLE {table}")


def _same_anchors(schema: Schema) -> None:
    """Refuse the migration when an explanation's pinned anchors, or an explanation
    check's, differ from those version 10 derives for it (its finding's anchors at the
    explanation's decision revision)."""
    # The derivation is the documents' own; it reads only through ``execute``. The
    # explanation's first body revision is read here, from the table version 10 has.
    from sandbox_science.orchestrator.documents.project_macros import explanation_anchors

    db = cast(sqlite3.Connection, schema)

    def response_anchors(db: sqlite3.Connection, source: str) -> object:
        row = db.execute(
            "SELECT f.project,f.finding_id,b.decision_revision,b.macro_revision "
            "FROM review_response_bodies b JOIN review_findings f USING(finding_id) "
            "WHERE b.source=? ORDER BY b.revision LIMIT 1",
            (source,),
        ).fetchone()
        return explanation_anchors(db, *row) if row else [{"unknown": True}]

    pinned = [
        *schema.execute("SELECT source,anchors FROM response_macro_anchors ORDER BY rowid"),
        *schema.execute(
            "SELECT r.source,p.anchors FROM math_macro_pins p JOIN math_response_jobs r "
            "USING(check_id) ORDER BY p.rowid"
        ),
    ]
    for source, raw in pinned:
        if json.loads(raw) != response_anchors(db, source):
            raise RefusedRowError(
                f"the pinned macro anchors of {source} differ from its finding's anchors "
                "when it was explained"
            )


def _run_tree(schema: Schema) -> None:
    """Version 11: a run's place in its session's tree of runs: its role, and for a
    delegated run its parent run and its delegation (plans-and-delegation spec, PD-32)."""
    for statement in (
        "ALTER TABLE runs ADD COLUMN role TEXT NOT NULL DEFAULT 'main' "
        "CHECK (role IN ('main', 'delegated'))",
        "ALTER TABLE runs ADD COLUMN parent TEXT "
        "CHECK (parent IS NULL OR length(CAST(parent AS BLOB)) BETWEEN 1 AND 256)",
        "ALTER TABLE runs ADD COLUMN delegation TEXT "
        "CHECK (delegation IS NULL OR length(CAST(delegation AS BLOB)) BETWEEN 1 AND 128)",
        # A column check sees one column, so a trigger ties the three together.
        "CREATE TRIGGER runs_place BEFORE INSERT ON runs "
        "WHEN (NEW.role = 'main') != (NEW.parent IS NULL) "
        "OR (NEW.parent IS NULL) != (NEW.delegation IS NULL) "
        "BEGIN SELECT RAISE(ABORT, "
        "'a delegated run names its parent and its delegation; a main run neither'); END",
    ):
        schema.execute(statement)


_MACRO_RECEIPT: Final = "json_object('revision',revision)"
_RECEIPTS: Final = (
    # scope, table, project column, operation column, result
    ("document", "requests", "project_id", "operation_id", "result"),
    ("comment", "annotation_operations", "project_id", "operation_id", "result"),
    ("review", "review_operations", "project", "operation_id", "result"),
    ("macros", "project_macro_operations", "project", "operation", _MACRO_RECEIPT),
)


def _receipts(schema: Schema) -> None:
    """Version 12: the operation receipts of documents, comments, reviews and project
    macros move into one table, ``operation_receipts``, each under its scope (B-04). A
    macro receipt's revision becomes the result ``{"revision": n}``; a document receipt's
    ``created_at``, which nothing reads, is not kept. A receipt the new table refuses
    refuses the migration."""
    schema.execute(
        """CREATE TABLE operation_receipts (
    scope TEXT NOT NULL CHECK (length(scope) BETWEEN 1 AND 32 AND scope NOT GLOB '*[^a-z]*'),
    project TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL
        CHECK (length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
    result TEXT NOT NULL CHECK (json_valid(result) AND json_type(result) = 'object'),
    PRIMARY KEY (scope, project, actor, operation_id)
) STRICT"""
    )
    for scope, table, project, operation, result in _RECEIPTS:
        try:
            schema.execute(
                "INSERT INTO operation_receipts "  # noqa: S608 - the store's own names
                f"SELECT ?,{project},actor,{operation},fingerprint,{result} FROM {table} "
                "ORDER BY rowid",
                (scope,),
            )
        except sqlite3.IntegrityError as error:
            raise RefusedRowError(f"a receipt of {table} fails its check ({error})") from None
        schema.execute(f"DROP TABLE {table}")


_EVENTS: Final = (
    # The documents' events keep their numbers, which the reviewer's document cursors hold.
    "INSERT INTO control_events(seq,time,kind,producer,project,session,subject_type,"
    "subject_id,payload) SELECT seq,time,kind,producer,project_id,session_id,'document',"
    "document_id,payload FROM events ORDER BY seq",
    # The reviewer's events follow them: nothing kept their numbers or read their producer.
    "INSERT INTO control_events(time,kind,producer,project,session,subject_type,subject_id,"
    "payload) SELECT strftime('%Y-%m-%dT%H:%M:%f',created,'unixepoch')||'+00:00',kind,"
    "'unrecorded',project,session,CASE WHEN kind LIKE 'finding.%' THEN 'finding' "
    "ELSE 'review' END,CASE WHEN kind LIKE 'finding.%' THEN json_extract(payload,'$.finding') "
    "ELSE json_extract(payload,'$.review') END,payload FROM review_events ORDER BY seq",
)


def _event_log(schema: Schema) -> None:
    """Version 13: the documents' and the reviewer's events move into one log,
    ``control_events``, each naming its subject (B-04). The documents' events keep their
    numbers; the reviewer's follow, their time from the seconds they kept, their
    producer not recorded. An event the log refuses refuses the migration."""
    for statement in (
        """CREATE TABLE control_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (length(CAST(kind AS BLOB)) BETWEEN 1 AND 64),
    producer TEXT NOT NULL CHECK (length(CAST(producer AS BLOB)) BETWEEN 1 AND 64),
    project TEXT NOT NULL,
    session TEXT,
    subject_type TEXT NOT NULL
        CHECK (length(subject_type) BETWEEN 1 AND 32 AND subject_type NOT GLOB '*[^a-z_]*'),
    subject_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) = 'object')
) STRICT""",
        "CREATE INDEX control_events_subjects ON control_events(project,subject_type,seq)",
    ):
        schema.execute(statement)
    for statement, table in zip(_EVENTS, ("events", "review_events"), strict=True):
        try:
            schema.execute(statement)
        except sqlite3.IntegrityError as error:
            raise RefusedRowError(f"an event of {table} fails its check ({error})") from None
        schema.execute(f"DROP TABLE {table}")
    for statement in (
        """CREATE TRIGGER control_events_no_update BEFORE UPDATE ON control_events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END""",
        """CREATE TRIGGER control_events_no_delete BEFORE DELETE ON control_events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END""",
    ):
        schema.execute(statement)


def _feed_cursors(schema: Schema) -> None:
    """Version 14: the reviewer's cursors on the document and artifact events move into
    one table, ``feed_cursors``, each under its feed (B-04); ``review_artifact_cursors``
    and ``review_windows.document_cursor`` go, every cursor copied, 0 or not. A cursor
    the table refuses refuses the migration."""
    schema.execute(
        """CREATE TABLE feed_cursors (
    feed TEXT NOT NULL CHECK (length(feed) BETWEEN 1 AND 32 AND feed NOT GLOB '*[^a-z_]*'),
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    cursor INTEGER NOT NULL CHECK (cursor BETWEEN 0 AND 9223372036854775807),
    PRIMARY KEY (feed, project, session)
) STRICT"""
    )
    for table, statement in (
        (
            "review_artifact_cursors",
            "INSERT INTO feed_cursors SELECT 'artifact',project,session,cursor "
            "FROM review_artifact_cursors ORDER BY rowid",
        ),
        (
            "review_windows",
            "INSERT INTO feed_cursors SELECT 'document',project,session,document_cursor "
            "FROM review_windows ORDER BY rowid",
        ),
    ):
        try:
            schema.execute(statement)
        except sqlite3.IntegrityError as error:
            raise RefusedRowError(f"a cursor of {table} fails its check ({error})") from None
    schema.execute("DROP TABLE review_artifact_cursors")
    schema.execute("ALTER TABLE review_windows DROP COLUMN document_cursor")


_TEXTS: Final = (
    # An explanation's rows keep their rowids, which a finding's response generation reads.
    (
        "review_response_bodies",
        "INSERT INTO text_revisions(rowid,subject_type,subject_id,revision,parent,"
        "parent_revision,origin,body,sha256,block_map,author,owners,next_block,next_span,"
        "identity_floor,held,macro_revision) SELECT rowid,'response',source,revision,"
        "finding_id,decision_revision,NULL,body,sha256,block_map,author,owners,next_block,"
        "next_span,identity_floor,held,macro_revision FROM review_response_bodies "
        "ORDER BY rowid",
    ),
    # A comment body kept its identity floor and held reason in its block map.
    (
        "annotation_body_revisions",
        "INSERT INTO text_revisions(subject_type,subject_id,revision,parent,parent_revision,"
        "origin,body,sha256,block_map,author,owners,next_block,next_span,identity_floor,held,"
        "macro_revision) SELECT 'comment',entry_id,revision,NULL,NULL,history_entry,body,"
        "sha256,block_map,author,owners,next_block,next_span,CASE WHEN "
        "json_type(block_map,'$.identity_floor')='integer' THEN "
        "json_extract(block_map,'$.identity_floor') END,CASE WHEN "
        "json_type(block_map,'$.held')='text' THEN json_extract(block_map,'$.held') END,NULL "
        "FROM annotation_body_revisions ORDER BY rowid",
    ),
)

_CURRENT_CHECKS: Final = """CREATE VIEW math_current_checks AS
    SELECT j.check_id,j.document AS source,j.revision FROM math_jobs j
    JOIN documents d ON d.document_id=j.document
    WHERE NOT EXISTS(SELECT 1 FROM math_comment_jobs c WHERE c.check_id=j.check_id)
    AND d.head=j.revision AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k
    WHERE k.document=j.document AND k.revision=d.head AND NOT EXISTS
    (SELECT 1 FROM math_comment_jobs c WHERE c.check_id=k.check_id))
    UNION ALL
    SELECT j.check_id,'comment:'||c.entry_id,j.revision FROM math_jobs j
    JOIN math_comment_jobs c USING(check_id) JOIN documents d ON d.document_id=j.document
    JOIN comment_math_sources s ON s.entry_id=c.entry_id
    JOIN text_revisions b ON b.subject_type='comment' AND b.subject_id=c.entry_id
    AND b.revision=j.revision
    JOIN annotation_entries original ON original.entry_id=c.entry_id
    JOIN annotation_threads t ON t.thread_id=original.thread_id
    WHERE t.document_id=j.document AND s.document=j.document AND s.project=d.project_id
    AND original.kind IN ('create','reply')
    AND s.target=j.revision AND d.head=c.macro_revision AND b.origin=(
      SELECT e.entry_id FROM annotation_entries e WHERE e.thread_id=original.thread_id
      AND (e.entry_id=original.entry_id OR (e.kind='edit' AND original.number=1)
      OR (e.kind='edit_body' AND json_extract(e.payload,'$.entry')=original.entry_id))
      ORDER BY e.number DESC LIMIT 1)
    AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_comment_jobs x
    USING(check_id) WHERE x.entry_id=c.entry_id AND k.revision=j.revision
    AND x.macro_revision=c.macro_revision) UNION ALL
    SELECT j.check_id,r.source,j.revision FROM math_jobs j JOIN math_response_jobs r
    USING(check_id) JOIN response_math_sources s ON s.source=r.source
    JOIN review_findings f ON f.finding_id=s.finding_id
    JOIN text_revisions b ON b.subject_type='response' AND b.subject_id=s.source
    AND b.revision=j.revision
    WHERE j.document IS NULL AND f.origin='review' AND s.project=f.project AND s.session=f.session
    AND b.parent=s.finding_id AND b.parent_revision=s.decision_revision
    AND s.target=j.revision AND j.source_sha256=b.sha256
    AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_response_jobs x USING(check_id)
                WHERE x.source=r.source AND k.revision=j.revision)"""


def _text_revisions(schema: Schema) -> None:
    """Version 15: the revisions of comment bodies and of finding explanations move into
    one table, ``text_revisions``, each under its kind of text (B-04). An explanation
    names its finding and decision revision as its parent and keeps its rowid; a
    comment body names the entry that recorded it as its origin, and its identity floor
    and held reason, which its block map held, become columns. A revision the table
    refuses, or an explanation whose source does not name its finding and decision,
    refuses the migration."""
    found = schema.execute(
        "SELECT source FROM review_response_bodies WHERE source IS NOT "
        "'response:'||finding_id||':r'||decision_revision||':explanation' LIMIT 1"
    ).fetchone()
    if found is not None:
        raise RefusedRowError(f"the explanation {found[0]} does not name its finding")
    schema.execute(
        """CREATE TABLE text_revisions (
    subject_type TEXT NOT NULL
        CHECK (length(subject_type) BETWEEN 1 AND 32 AND subject_type NOT GLOB '*[^a-z_]*'),
    subject_id TEXT NOT NULL CHECK (length(CAST(subject_id AS BLOB)) BETWEEN 1 AND 128),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    parent TEXT CHECK (parent IS NULL OR length(CAST(parent AS BLOB)) BETWEEN 1 AND 128),
    parent_revision INTEGER
        CHECK (parent_revision IS NULL OR parent_revision BETWEEN 1 AND 999999999),
    origin TEXT CHECK (origin IS NULL OR length(CAST(origin AS BLOB)) BETWEEN 1 AND 128),
    body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) <= 32768),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    block_map TEXT NOT NULL
        CHECK (json_valid(block_map) AND length(CAST(block_map AS BLOB)) <= 16777216),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 8192),
    owners TEXT NOT NULL
        CHECK (json_valid(owners) AND length(CAST(owners AS BLOB)) <= 16777216),
    next_block INTEGER NOT NULL CHECK (next_block BETWEEN 0 AND 999999999),
    next_span INTEGER NOT NULL CHECK (next_span BETWEEN 0 AND 999999999),
    identity_floor INTEGER
        CHECK (identity_floor IS NULL OR identity_floor BETWEEN 1 AND revision),
    held TEXT CHECK (held IS NULL OR length(CAST(held AS BLOB)) <= 4096),
    macro_revision INTEGER
        CHECK (macro_revision IS NULL OR macro_revision BETWEEN 0 AND 999999999),
    PRIMARY KEY (subject_type, subject_id, revision)
) STRICT"""
    )
    schema.execute(
        "CREATE INDEX text_revisions_parents ON text_revisions(subject_type,parent,parent_revision)"
    )
    schema.execute("DROP VIEW math_current_checks")
    for table, statement in _TEXTS:
        try:
            schema.execute(statement)
        except sqlite3.IntegrityError as error:
            raise RefusedRowError(f"a revision of {table} fails its check ({error})") from None
        schema.execute(f"DROP TABLE {table}")
    for statement in (
        """CREATE TRIGGER text_revisions_no_update BEFORE UPDATE ON text_revisions
BEGIN SELECT RAISE(ABORT, 'text revisions are immutable'); END""",
        """CREATE TRIGGER text_revisions_no_delete BEFORE DELETE ON text_revisions
BEGIN SELECT RAISE(ABORT, 'text revisions are immutable'); END""",
        _CURRENT_CHECKS,
    ):
        schema.execute(statement)


_TEXT_MATH: Final = (
    # Each kind keeps its order, which claims follow.
    (
        "comment_math_sources",
        "INSERT INTO text_math_sources(subject_type,subject_id,project,session,parent,"
        "parent_revision,target,completed,macro_revision,retry_requested,blocked) "
        "SELECT 'comment',entry_id,project,NULL,document,NULL,target,completed,"
        "macro_revision,retry_requested,blocked FROM comment_math_sources ORDER BY rowid",
    ),
    (
        "response_math_sources",
        "INSERT INTO text_math_sources(subject_type,subject_id,project,session,parent,"
        "parent_revision,target,completed,macro_revision,retry_requested,blocked) "
        "SELECT 'response',source,project,session,finding_id,decision_revision,target,"
        "completed,0,retry_requested,blocked FROM response_math_sources ORDER BY rowid",
    ),
    (
        "math_comment_jobs",
        "INSERT INTO math_text_jobs SELECT check_id,'comment',entry_id,macro_revision,"
        "macro_sha256 FROM math_comment_jobs ORDER BY rowid",
    ),
    (
        "math_response_jobs",
        "INSERT INTO math_text_jobs SELECT check_id,'response',source,NULL,NULL "
        "FROM math_response_jobs ORDER BY rowid",
    ),
)

_UNQUEUED: Final = (
    (
        "SELECT c.check_id,c.entry_id FROM math_comment_jobs c WHERE NOT EXISTS(SELECT 1 "
        "FROM comment_math_sources s WHERE s.entry_id=c.entry_id) ORDER BY c.rowid LIMIT 1",
        "the math check {} of comment entry {} has no math queue row",
    ),
    (
        "SELECT r.check_id,r.source FROM math_response_jobs r WHERE NOT EXISTS(SELECT 1 "
        "FROM response_math_sources s WHERE s.source=r.source) ORDER BY r.rowid LIMIT 1",
        "the math check {} of explanation {} has no math queue row",
    ),
)

_JOB_SCOPE: Final = """CREATE VIEW math_job_scope AS
    SELECT j.check_id,s.project,NULL AS session FROM math_jobs j
    JOIN math_sources s USING(document)
    UNION ALL SELECT j.check_id,f.project,f.session FROM math_jobs j JOIN math_text_jobs r
    ON r.check_id=j.check_id AND r.subject_type='response' JOIN text_math_sources s
    ON s.subject_type='response' AND s.subject_id=r.subject_id JOIN review_findings f
    ON f.finding_id=s.parent WHERE j.document IS NULL AND s.project=f.project
    AND s.session=f.session"""

_TEXT_CHECKS: Final = """CREATE VIEW math_current_checks AS
    SELECT j.check_id,j.document AS source,j.revision FROM math_jobs j
    JOIN documents d ON d.document_id=j.document
    WHERE NOT EXISTS(SELECT 1 FROM math_text_jobs x WHERE x.check_id=j.check_id)
    AND d.head=j.revision AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k
    WHERE k.document=j.document AND k.revision=d.head AND NOT EXISTS
    (SELECT 1 FROM math_text_jobs x WHERE x.check_id=k.check_id))
    UNION ALL
    SELECT j.check_id,'comment:'||c.subject_id,j.revision FROM math_jobs j
    JOIN math_text_jobs c ON c.check_id=j.check_id AND c.subject_type='comment'
    JOIN documents d ON d.document_id=j.document
    JOIN text_math_sources s ON s.subject_type='comment' AND s.subject_id=c.subject_id
    JOIN text_revisions b ON b.subject_type='comment' AND b.subject_id=c.subject_id
    AND b.revision=j.revision
    JOIN annotation_entries original ON original.entry_id=c.subject_id
    JOIN annotation_threads t ON t.thread_id=original.thread_id
    WHERE t.document_id=j.document AND s.parent=j.document AND s.project=d.project_id
    AND original.kind IN ('create','reply')
    AND s.target=j.revision AND d.head=c.macro_revision AND b.origin=(
      SELECT e.entry_id FROM annotation_entries e WHERE e.thread_id=original.thread_id
      AND (e.entry_id=original.entry_id OR (e.kind='edit' AND original.number=1)
      OR (e.kind='edit_body' AND json_extract(e.payload,'$.entry')=original.entry_id))
      ORDER BY e.number DESC LIMIT 1)
    AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_text_jobs x
    USING(check_id) WHERE x.subject_type='comment' AND x.subject_id=c.subject_id
    AND k.revision=j.revision AND x.macro_revision=c.macro_revision) UNION ALL
    SELECT j.check_id,r.subject_id,j.revision FROM math_jobs j JOIN math_text_jobs r
    ON r.check_id=j.check_id AND r.subject_type='response'
    JOIN text_math_sources s ON s.subject_type='response' AND s.subject_id=r.subject_id
    JOIN review_findings f ON f.finding_id=s.parent
    JOIN text_revisions b ON b.subject_type='response' AND b.subject_id=s.subject_id
    AND b.revision=j.revision
    WHERE j.document IS NULL AND f.origin='review' AND s.project=f.project AND s.session=f.session
    AND b.parent=s.parent AND b.parent_revision=s.parent_revision
    AND s.target=j.revision AND j.source_sha256=b.sha256
    AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_text_jobs x USING(check_id)
                WHERE x.subject_type='response' AND x.subject_id=r.subject_id
                AND k.revision=j.revision)"""


def _text_math(schema: Schema) -> None:
    """Version 16: the math-check queues of comment bodies and explanations and their
    checks' links move into one queue, ``text_math_sources``, and one link table,
    ``math_text_jobs``, each under its kind of text (B-04). A comment's queue names its
    document as its parent, and its checks the document revision and hash their macros
    came from; an explanation's queue names its finding and decision revision. The two
    views over them are made again. A check whose text has no queue row, or a row the new
    tables refuse, refuses the migration."""
    for query, what in _UNQUEUED:
        found = schema.execute(query).fetchone()
        if found is not None:
            raise RefusedRowError(what.format(*found))
    for statement in (
        """CREATE TABLE text_math_sources (
    subject_type TEXT NOT NULL
        CHECK (length(subject_type) BETWEEN 1 AND 32 AND subject_type NOT GLOB '*[^a-z_]*'),
    subject_id TEXT NOT NULL CHECK (length(CAST(subject_id AS BLOB)) BETWEEN 1 AND 128),
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    session TEXT CHECK (session IS NULL OR length(CAST(session AS BLOB)) BETWEEN 1 AND 128),
    parent TEXT CHECK (parent IS NULL OR length(CAST(parent AS BLOB)) BETWEEN 1 AND 128),
    parent_revision INTEGER
        CHECK (parent_revision IS NULL OR parent_revision BETWEEN 1 AND 999999999),
    target INTEGER NOT NULL CHECK (target BETWEEN 0 AND 999999999),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed BETWEEN 0 AND 999999999),
    macro_revision INTEGER NOT NULL DEFAULT 0 CHECK (macro_revision BETWEEN 0 AND 999999999),
    retry_requested INTEGER NOT NULL DEFAULT 0 CHECK (retry_requested IN (0, 1)),
    blocked TEXT CHECK (blocked IS NULL OR length(CAST(blocked AS BLOB)) <= 4096),
    PRIMARY KEY (subject_type, subject_id)
) STRICT""",
        """CREATE TABLE math_text_jobs (
    check_id TEXT PRIMARY KEY REFERENCES math_jobs(check_id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    macro_revision INTEGER
        CHECK (macro_revision IS NULL OR macro_revision BETWEEN 0 AND 999999999),
    macro_sha256 TEXT CHECK (macro_sha256 IS NULL
        OR (length(macro_sha256) = 64 AND macro_sha256 NOT GLOB '*[^0-9a-f]*')),
    -- A comment's check names the document revision and hash its macros came from.
    CHECK ((subject_type = 'comment') = (macro_revision IS NOT NULL AND macro_sha256 IS NOT NULL)),
    FOREIGN KEY (subject_type, subject_id) REFERENCES text_math_sources(subject_type, subject_id)
) STRICT""",
        "CREATE INDEX math_text_jobs_subjects ON math_text_jobs(subject_type,subject_id)",
        # One queue row per explanation decision, and the legacy discovery's lookup.
        "CREATE UNIQUE INDEX text_math_sources_parents "
        "ON text_math_sources(subject_type,parent,parent_revision)",
        "DROP VIEW math_current_checks",
        "DROP VIEW math_job_scope",
    ):
        schema.execute(statement)
    for table, statement in _TEXT_MATH:
        try:
            schema.execute(statement)
        except sqlite3.IntegrityError as error:
            raise RefusedRowError(f"a row of {table} fails its check ({error})") from None
    for table in (
        "math_comment_jobs",
        "math_response_jobs",
        "comment_math_sources",
        "response_math_sources",
    ):
        schema.execute(f"DROP TABLE {table}")
    schema.execute(_JOB_SCOPE)
    schema.execute(_TEXT_CHECKS)


MIGRATIONS: Final[tuple[Migration, ...]] = (
    _baseline,
    _projects,
    _comment_checks,
    _response_checks,
    _math_checks,
    _evidence_checks,
    _run_service,
    _run_service_control,
    _macro_revisions,
    _run_tree,
    _receipts,
    _event_log,
    _feed_cursors,
    _text_revisions,
    _text_math,
)
"""``MIGRATIONS[0]`` creates the baseline in an empty database, and ``MIGRATIONS[i]``
takes a database in version ``BASELINE + i - 1`` to ``BASELINE + i``."""

VERSION: Final = BASELINE + len(MIGRATIONS) - 1
"""The version this code reads and writes."""


def _busy(error: sqlite3.OperationalError) -> bool:
    return any(word in str(error).lower() for word in ("locked", "busy"))


def check(path: Path, db: sqlite3.Connection, *, readonly: bool) -> int:
    """The version of the database ``db`` opens, if this code can read it (a reader) or
    bring it to ``VERSION`` (a writer); a refusal otherwise. Only reads."""
    not_ours = ControlError(UNAVAILABLE, f"{path.name} is not a control store")
    try:
        # One statement, so one snapshot: read separately, another process's first
        # commit could land in between, and its schema show without its version.
        version, application, objects = db.execute(
            "SELECT (SELECT user_version FROM pragma_user_version), "
            "(SELECT application_id FROM pragma_application_id), "
            "(SELECT count(*) FROM sqlite_master)"
        ).fetchone()
    except sqlite3.OperationalError as error:
        if _busy(error):
            raise ControlError(BUSY, "another process held the control store: try again") from None
        raise not_ours from None
    except sqlite3.DatabaseError:
        raise not_ours from None
    if version == 0:
        if objects:
            raise not_ours
        if readonly:
            raise ControlError(NOT_FOUND, "nothing has been stored yet")
        return version
    if application != APPLICATION_ID:
        if 0 < version < BASELINE and application == 0:
            raise ControlError(
                UNAVAILABLE,
                f"{path.name} is a development control store from before "
                f"schema version {BASELINE}, which this version neither reads nor converts. "
                "Export its documents with the harness that made it (document export), move "
                "the file aside, and import them into the new store this version creates "
                "(see docs/LIVING-DOCUMENTS.md)",
            )
        raise not_ours
    if version > VERSION:
        raise ControlError(
            UNAVAILABLE,
            f"the control store is in schema version {version}, newer than this version's "
            f"{VERSION}: upgrade the harness and the workspace",
        )
    if version < BASELINE:
        raise not_ours
    if readonly and version < VERSION:
        raise ControlError(
            UNAVAILABLE,
            f"the control store is in schema version {version}: open it with the harness or "
            f"the workspace first, which bring it to version {VERSION}",
        )
    return version


def migrate(db: sqlite3.Connection, path: Path) -> None:
    """Bring the database to ``VERSION``, inside the writer's open transaction."""
    if not db.in_transaction:
        raise ControlError(INTERNAL, "migrating the control store needs a write transaction")
    version = check(path, db, readonly=False)
    pending = MIGRATIONS if version == 0 else MIGRATIONS[version - BASELINE + 1 :]
    schema = Schema(db)
    stays = f"{path.name} stays at version {version}"
    for step in pending:
        try:
            step(schema)
        except RefusedRowError as refused:
            raise ControlError(
                UNAVAILABLE, f"{stays}: version {VERSION} refuses it, as {refused}. {_KEEP}"
            ) from None
        if not db.in_transaction:
            raise ControlError(INTERNAL, "a migration step ended the writer's transaction")
    if pending:
        if (broken := _broken_key(db)) is not None:
            raise ControlError(
                UNAVAILABLE, f"{stays}: version {VERSION} refuses it, as {broken}. {_KEEP}"
            )
        db.execute(f"PRAGMA user_version = {VERSION}")
