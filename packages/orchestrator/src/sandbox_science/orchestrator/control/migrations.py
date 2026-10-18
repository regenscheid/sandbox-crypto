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
from typing import Any, Final, cast

from .errors import BUSY, INTERNAL, NOT_FOUND, UNAVAILABLE, ControlError

BASELINE: Final = 2
"""The version ``baseline.sql`` creates. Version 1 was the development stores before it."""

_Json = dict[str, Any]

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


# The derivation of an explanation's macro anchors as version 10 shipped it
# (documents/project_macros.py at #221), kept here so that a later change to the live
# code does not change what this step accepts.
_V10_SEVERITY_RANK: Final = {"fail": 0, "warn": 1, "note": 2}


def _v10_explanation_anchors(
    db: sqlite3.Connection, project: str, finding: str, decision: int, macros: int | None
) -> list[_Json]:
    if macros is None:
        return [{"unknown": True}]
    anchors = _v10_header_anchors(db, finding, decision)
    if anchors is None:
        return _v10_finding_anchors(db, project, finding)
    return _v10_anchor_pins(db, project, anchors)


def _v10_header_anchors(db: sqlite3.Connection, finding: str, revision: int) -> object | None:
    row = db.execute(
        "SELECT payload FROM review_transitions WHERE finding_id=? AND revision<=? "
        "AND json_extract(payload,'$.assessment') IS NOT NULL ORDER BY revision DESC LIMIT 1",
        (finding, revision),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = cast(_Json, json.loads(row[0]))
        new, old = cast(_Json, payload["assessment"]), payload.get("previous_assessment")
        if not isinstance(old, dict):
            return new["anchors"]
        old = cast(_Json, old)
        stronger = _V10_SEVERITY_RANK[new["severity"]] <= _V10_SEVERITY_RANK[old["severity"]]
        return new["anchors"] if stronger else old["anchors"]
    except (ValueError, KeyError, TypeError, RecursionError):
        return [{"unknown": True}]


def _v10_finding_anchors(db: sqlite3.Connection, project: str, finding: str) -> list[_Json]:
    row = db.execute(
        "SELECT anchors FROM review_findings WHERE finding_id=? AND project=?",
        (finding, project),
    ).fetchone()
    if row is None:
        return [{"unknown": True}]
    try:
        return _v10_anchor_pins(db, project, json.loads(row[0]))
    except (ValueError, RecursionError):
        return [{"unknown": True}]


def _v10_anchor_pins(db: sqlite3.Connection, project: str, anchors: object) -> list[_Json]:
    try:
        if not isinstance(anchors, list) or len(cast(list[Any], anchors)) > 32:
            raise ValueError
        pins: list[_Json] = []
        for anchor in cast(list[_Json], anchors):
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
        return [{"unknown": True}]


def _same_anchors(schema: Schema) -> None:
    """Refuse the migration when an explanation's pinned anchors, or an explanation
    check's, differ from those version 10 derives for it (its finding's anchors at the
    explanation's decision revision)."""
    db = cast(sqlite3.Connection, schema)

    def response_anchors(db: sqlite3.Connection, source: str) -> object:
        row = db.execute(
            "SELECT f.project,f.finding_id,b.decision_revision,b.macro_revision "
            "FROM review_response_bodies b JOIN review_findings f USING(finding_id) "
            "WHERE b.source=? ORDER BY b.revision LIMIT 1",
            (source,),
        ).fetchone()
        return _v10_explanation_anchors(db, *row) if row else [{"unknown": True}]

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


_TYPES: Final = "'issue','claim','result','assumption','decision','task','reference'"


def _ledger(schema: Schema) -> None:
    """Version 17: the project ledger's entries and their revisions (docs/specs/ledger.md,
    LG-1, LG-2). An entry keeps its identity, type, handle number and origin; its head
    moves one revision at a time; revisions are written once. A revision's statement is
    a text revision of kind ``statement`` under the entry's ID."""
    for statement in (
        f"""CREATE TABLE ledger_entries (
    entry_id TEXT PRIMARY KEY
        CHECK (length(entry_id) = 30 AND entry_id GLOB 'entry-*'
        AND substr(entry_id, 7) NOT GLOB '*[^0-9a-f]*'),
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    type TEXT NOT NULL CHECK (type IN ({_TYPES})),
    number INTEGER NOT NULL CHECK (number BETWEEN 1 AND 999999999),
    origin TEXT CHECK (origin IS NULL OR origin IN ('published', 'project')),
    head INTEGER NOT NULL CHECK (head BETWEEN 1 AND 999999999),
    position INTEGER NOT NULL UNIQUE CHECK (position >= 1),
    CHECK ((type IN ('claim', 'result')) = (origin IS NOT NULL)),
    UNIQUE (project, type, number)
) STRICT""",
        "CREATE INDEX ledger_entries_order ON ledger_entries(project,position)",
        """CREATE TRIGGER ledger_entries_kept
BEFORE UPDATE OF entry_id,project,type,number,origin,position
ON ledger_entries BEGIN SELECT RAISE(ABORT, 'a ledger entry keeps its identity'); END""",
        """CREATE TRIGGER ledger_entries_head BEFORE UPDATE OF head ON ledger_entries
WHEN NEW.head != OLD.head + 1
BEGIN SELECT RAISE(ABORT, 'a ledger entry moves one revision at a time'); END""",
        """CREATE TRIGGER ledger_entries_no_delete BEFORE DELETE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger entries are kept'); END""",
        """CREATE TABLE ledger_revisions (
    entry_id TEXT NOT NULL REFERENCES ledger_entries(entry_id),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    title TEXT NOT NULL CHECK (length(CAST(title AS BLOB)) BETWEEN 1 AND 2048),
    status TEXT NOT NULL CHECK (length(status) BETWEEN 1 AND 32 AND status NOT GLOB '*[^a-z_]*'),
    statement INTEGER NOT NULL CHECK (statement BETWEEN 1 AND 999999999),
    fields TEXT NOT NULL CHECK (json_valid(fields) AND json_type(fields) = 'object'
        AND length(CAST(fields AS BLOB)) <= 65536),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 8192),
    reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) BETWEEN 1 AND 16384),
    operation_id TEXT NOT NULL CHECK (length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 128),
    provenance TEXT CHECK (provenance IS NULL OR (json_valid(provenance)
        AND json_type(provenance) = 'object' AND length(CAST(provenance AS BLOB)) <= 4096)),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    PRIMARY KEY (entry_id, revision)
) STRICT""",
        """CREATE TRIGGER ledger_revisions_no_update BEFORE UPDATE ON ledger_revisions
BEGIN SELECT RAISE(ABORT, 'ledger revisions are immutable'); END""",
        """CREATE TRIGGER ledger_revisions_no_delete BEFORE DELETE ON ledger_revisions
BEGIN SELECT RAISE(ABORT, 'ledger revisions are immutable'); END""",
    ):
        schema.execute(statement)


_LINKS: Final = (
    "'answers','assumes','builds_on','restates','compares_to','supersedes','dominates',"
    "'split_into','affects','concerns'"
)


def _ledger_links(schema: Schema) -> None:
    """Version 18: the links each ledger revision holds (docs/specs/ledger.md, LG-5): in
    order, each with its kind and its target, an entry, a review finding or a document
    block at a revision, and the revision it pins. Written once, with the revision."""
    for statement in (
        f"""CREATE TABLE ledger_links (
    entry_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 64),
    kind TEXT NOT NULL CHECK (kind IN ({_LINKS})),
    target TEXT NOT NULL CHECK (target IN ('entry', 'finding', 'block')),
    target_id TEXT NOT NULL CHECK (length(CAST(target_id AS BLOB)) BETWEEN 1 AND 128),
    target_revision INTEGER
        CHECK (target_revision IS NULL OR target_revision BETWEEN 1 AND 999999999),
    block TEXT CHECK (block IS NULL OR length(CAST(block AS BLOB)) BETWEEN 1 AND 128),
    note TEXT CHECK (note IS NULL OR length(CAST(note AS BLOB)) BETWEEN 1 AND 16384),
    CHECK ((target = 'block') = (block IS NOT NULL)),
    CHECK (target != 'block' OR target_revision IS NOT NULL),
    PRIMARY KEY (entry_id, revision, ordinal),
    FOREIGN KEY (entry_id, revision) REFERENCES ledger_revisions(entry_id, revision)
) STRICT""",
        "CREATE INDEX ledger_links_targets ON ledger_links(target,target_id)",
        """CREATE TRIGGER ledger_links_no_update BEFORE UPDATE ON ledger_links
BEGIN SELECT RAISE(ABORT, 'ledger revisions are immutable'); END""",
        """CREATE TRIGGER ledger_links_no_delete BEFORE DELETE ON ledger_links
BEGIN SELECT RAISE(ABORT, 'ledger revisions are immutable'); END""",
    ):
        schema.execute(statement)


_STATEMENT_SCOPE: Final = (
    _JOB_SCOPE  # noqa: S608 - fixed SQL
    + """
    UNION ALL SELECT j.check_id,e.project,NULL FROM math_jobs j JOIN math_text_jobs r
    ON r.check_id=j.check_id AND r.subject_type='statement' JOIN ledger_entries e
    ON e.entry_id=r.subject_id WHERE j.document IS NULL"""
)

_STATEMENT_CHECKS: Final = (
    _TEXT_CHECKS  # noqa: S608 - fixed SQL
    + """ UNION ALL
    SELECT j.check_id,'statement:'||r.subject_id,j.revision FROM math_jobs j
    JOIN math_text_jobs r ON r.check_id=j.check_id AND r.subject_type='statement'
    JOIN text_math_sources s ON s.subject_type='statement' AND s.subject_id=r.subject_id
    JOIN ledger_entries e ON e.entry_id=s.subject_id
    JOIN text_revisions b ON b.subject_type='statement' AND b.subject_id=s.subject_id
    AND b.revision=j.revision
    WHERE j.document IS NULL AND s.project=e.project AND s.target=j.revision
    AND j.source_sha256=b.sha256
    AND j.rowid=(SELECT max(k.rowid) FROM math_jobs k JOIN math_text_jobs x USING(check_id)
                WHERE x.subject_type='statement' AND x.subject_id=r.subject_id
                AND k.revision=j.revision)"""
)


def _statement_math(schema: Schema) -> None:
    """Version 19: ledger statements are checked for math errors (LG-1, LD-53). The views
    of a check's scope and of each text's current check gain a branch for statements:
    a statement's check belongs to its entry's project and to no session, and is current
    when it is the latest of the statement's latest text revision."""
    for statement in (
        "DROP VIEW math_current_checks",
        "DROP VIEW math_job_scope",
        _STATEMENT_SCOPE,
        _STATEMENT_CHECKS,
    ):
        schema.execute(statement)


_RESERVED: Final = 8 * 1024 + 2 * 16 * 1024
"""Metadata an operation in flight holds back: its request and the worker reference and
receipt it may still record (``orchestrator/artifacts.py``)."""
_RECEIPT: Final = 16 * 1024
"""What an operation with a pinned worker reference may still record: its receipt."""


def _operation_cost(row: str) -> str:
    """The metadata an artifact operation counts: what it stored once it is done, or
    held without a worker reference, which it never gets; what it stored and the receipt
    it may still record, with a pinned worker reference; otherwise its reservation."""
    stored = (
        f"length(CAST({row}.request AS BLOB))+coalesce(length(CAST({row}.worker AS BLOB)),0)"
        f"+coalesce(length(CAST({row}.receipt AS BLOB)),0)"
    )
    return (
        f"(CASE WHEN {row}.state='done' OR ({row}.state='intent' AND {row}.receipt IS NOT NULL) "
        f"THEN {stored} WHEN {row}.state='transfer' THEN {stored}+{_RECEIPT} "
        f"ELSE {_RESERVED} END)"
    )


_USAGE_ROWS: Final = "INSERT OR IGNORE INTO artifact_usage(project) VALUES (NEW.project),('');"
_USAGE_OF: Final = "WHERE project IN (NEW.project,'')"


def _artifact_usage(schema: Schema) -> None:
    """Version 20: the artifacts' retention figures are kept as they change, per project
    and overall (``project = ''``), by triggers on the artifact tables, so checking the
    budget no longer reads every revision (research artifacts, RA-32)."""
    schema.execute(
        """CREATE TABLE artifact_usage (
    project TEXT PRIMARY KEY,
    revisions INTEGER NOT NULL DEFAULT 0 CHECK (revisions >= 0),
    bytes INTEGER NOT NULL DEFAULT 0 CHECK (bytes >= 0),
    operations INTEGER NOT NULL DEFAULT 0 CHECK (operations >= 0),
    metadata INTEGER NOT NULL DEFAULT 0 CHECK (metadata >= 0)) STRICT"""
    )
    record = "length(CAST(NEW.record AS BLOB))"
    for statement in (
        "CREATE TRIGGER artifact_usage_revision AFTER INSERT ON artifact_revisions BEGIN "  # noqa: S608 - constant SQL
        f"{_USAGE_ROWS} UPDATE artifact_usage SET revisions=revisions+1,"
        f"bytes=bytes+length(NEW.data),metadata=metadata+{record} {_USAGE_OF}; END",
        "CREATE TRIGGER artifact_usage_event AFTER INSERT ON artifact_events BEGIN "  # noqa: S608 - constant SQL
        f"{_USAGE_ROWS} UPDATE artifact_usage SET metadata=metadata+{record} {_USAGE_OF}; END",
        "CREATE TRIGGER artifact_usage_object AFTER INSERT ON artifact_objects BEGIN "  # noqa: S608 - constant SQL
        f"{_USAGE_ROWS} UPDATE artifact_usage SET metadata=metadata"
        f"+length(CAST(NEW.path AS BLOB))+length(NEW.id)+length(NEW.project) {_USAGE_OF}; END",
        "CREATE TRIGGER artifact_usage_operation AFTER INSERT ON artifact_operations BEGIN "  # noqa: S608 - constant SQL
        f"{_USAGE_ROWS} UPDATE artifact_usage SET operations=operations+1,metadata=metadata"
        f"+{_operation_cost('NEW')} {_USAGE_OF}; END",
        "CREATE TRIGGER artifact_usage_operation_change AFTER UPDATE ON artifact_operations "  # noqa: S608 - constant SQL
        f"BEGIN UPDATE artifact_usage SET metadata=metadata"
        f"-{_operation_cost('OLD')}+{_operation_cost('NEW')} {_USAGE_OF}; END",
    ):
        schema.execute(statement)
    for scope in ("project", "''"):
        schema.execute(
            "INSERT INTO artifact_usage(project) "  # noqa: S608 - constant SQL
            f"SELECT {scope} FROM artifact_objects UNION SELECT {scope} FROM artifact_operations"
        )
    schema.execute(
        f"""UPDATE artifact_usage SET
    revisions=(SELECT count(*) FROM artifact_revisions r
        WHERE artifact_usage.project IN (r.project,'')),
    bytes=(SELECT coalesce(sum(length(data)),0) FROM artifact_revisions r
        WHERE artifact_usage.project IN (r.project,'')),
    operations=(SELECT count(*) FROM artifact_operations o
        WHERE artifact_usage.project IN (o.project,'')),
    metadata=(SELECT coalesce(sum(length(CAST(record AS BLOB))),0) FROM artifact_revisions r
        WHERE artifact_usage.project IN (r.project,''))
    +(SELECT coalesce(sum(length(CAST(record AS BLOB))),0) FROM artifact_events e
        WHERE artifact_usage.project IN (e.project,''))
    +(SELECT coalesce(sum(length(CAST(path AS BLOB))+length(id)+length(project)),0)
        FROM artifact_objects b WHERE artifact_usage.project IN (b.project,''))
    +(SELECT coalesce(sum({_operation_cost("o")}),0) FROM artifact_operations o
        WHERE artifact_usage.project IN (o.project,''))"""  # noqa: S608 - constant SQL
    )


def _file_settings(schema: Schema) -> None:
    """Version 21: each project's settings for the files its executions write (research
    artifacts, RA-29, RA-39): ``all`` or ``results``, and its ignore patterns. A project
    without a row has the defaults."""
    schema.execute(
        """CREATE TABLE project_file_settings (
    project TEXT PRIMARY KEY CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    recording TEXT NOT NULL CHECK (recording IN ('all', 'results')),
    ignore TEXT NOT NULL CHECK (json_valid(ignore) AND json_type(ignore) = 'array'
        AND length(CAST(ignore AS BLOB)) <= 65536),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 4096)
) STRICT"""
    )


def _ledger_evidence(schema: Schema) -> None:
    """Version 22: the evidence links each ledger revision holds (docs/specs/ledger.md,
    LG-8 to LG-11): each with its ID, kept from revision to revision, its role and type,
    its pinned target (an entry, artifact or document revision, a block or math span,
    or a review), the revision of the entry an independent check examined, its scope
    note and the revision that added it. Written once, with the revision."""
    for statement in (
        """CREATE TABLE ledger_evidence (
    entry_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 128),
    evidence_id TEXT NOT NULL
        CHECK (length(evidence_id) = 33 AND evidence_id GLOB 'evidence-*'
        AND substr(evidence_id, 10) NOT GLOB '*[^0-9a-f]*'),
    role TEXT NOT NULL
        CHECK (role IN ('supports', 'contradicts', 'derivation', 'independent_check')),
    type TEXT NOT NULL CHECK (type IN ('proof', 'exact_computation', 'finite_test',
        'numerical_experiment', 'source_claim', 'synthetic', 'unclassified')),
    target TEXT NOT NULL CHECK (target IN ('entry', 'artifact', 'block', 'review')),
    target_id TEXT NOT NULL CHECK (length(CAST(target_id AS BLOB)) BETWEEN 1 AND 128),
    target_revision INTEGER
        CHECK (target_revision IS NULL OR target_revision BETWEEN 1 AND 999999999),
    block TEXT CHECK (block IS NULL OR length(CAST(block AS BLOB)) BETWEEN 1 AND 128),
    span TEXT CHECK (span IS NULL OR length(CAST(span AS BLOB)) BETWEEN 1 AND 128),
    sha256 TEXT CHECK (sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
    checked INTEGER CHECK (checked IS NULL OR checked BETWEEN 1 AND 999999999),
    scope TEXT CHECK (scope IS NULL OR length(CAST(scope AS BLOB)) BETWEEN 1 AND 16384),
    added INTEGER NOT NULL CHECK (added BETWEEN 1 AND revision),
    CHECK ((role = 'independent_check') = (checked IS NOT NULL)),
    CHECK ((target = 'block') = (block IS NOT NULL)),
    CHECK (span IS NULL OR block IS NOT NULL),
    CHECK ((target = 'review') = (target_revision IS NULL)),
    CHECK ((target = 'artifact') = (sha256 IS NOT NULL)),
    PRIMARY KEY (entry_id, revision, ordinal),
    UNIQUE (entry_id, revision, evidence_id),
    FOREIGN KEY (entry_id, revision) REFERENCES ledger_revisions(entry_id, revision)
) STRICT""",
        "CREATE INDEX ledger_evidence_targets ON ledger_evidence(target,target_id)",
        """CREATE TRIGGER ledger_evidence_no_update BEFORE UPDATE ON ledger_evidence
BEGIN SELECT RAISE(ABORT, 'ledger revisions are immutable'); END""",
        """CREATE TRIGGER ledger_evidence_no_delete BEFORE DELETE ON ledger_evidence
BEGIN SELECT RAISE(ABORT, 'ledger revisions are immutable'); END""",
    ):
        schema.execute(statement)


_ANCHORED_ENTRIES: Final = (
    "SELECT {finding},json_extract(a.value,'$.locator.entry') FROM json_each("
    "CASE WHEN json_valid({anchors}) THEN {anchors} ELSE '[]' END) a "
    "WHERE json_type(a.value,'$.locator.entry')='text'"
)
"""The ledger entries a finding's anchors name."""


def _ledger_review_index(schema: Schema) -> None:
    """Version 23: what the ledger reads of the reviews, findable without scanning them
    all (docs/specs/ledger.md, LG-17, LG-25): ``review_finding_entries`` keeps the ledger
    entries each finding is anchored to, written by triggers as findings are written and
    their anchors change, and filled from the findings there are; and the review
    evidence that holds a ledger entry is indexed by the entry."""
    new = _ANCHORED_ENTRIES.format(finding="NEW.finding_id", anchors="NEW.anchors")
    existing = _ANCHORED_ENTRIES.format(finding="f.finding_id", anchors="f.anchors").replace(
        "FROM json_each(", "FROM review_findings f, json_each("
    )
    fill = "INSERT OR IGNORE INTO review_finding_entries(finding_id,entry_id) "
    for statement in (
        """CREATE TABLE review_finding_entries (
    finding_id TEXT NOT NULL REFERENCES review_findings(finding_id),
    entry_id TEXT NOT NULL CHECK (length(CAST(entry_id AS BLOB)) BETWEEN 1 AND 128),
    PRIMARY KEY (entry_id, finding_id)
) STRICT, WITHOUT ROWID""",
        "CREATE INDEX review_finding_entries_findings ON review_finding_entries(finding_id)",
        "CREATE TRIGGER review_finding_entries_insert AFTER INSERT ON review_findings "
        f"BEGIN {fill}{new}; END",
        "CREATE TRIGGER review_finding_entries_update AFTER UPDATE OF anchors "  # noqa: S608
        "ON review_findings BEGIN DELETE FROM review_finding_entries "
        f"WHERE finding_id=NEW.finding_id; {fill}{new}; END",
        fill + existing,
        "CREATE INDEX review_evidence_ledger "
        "ON review_evidence(json_extract(locator,'$.entry')) WHERE kind='ledger'",
    ):
        schema.execute(statement)


def _numbered(column: str, prefix: str) -> str:
    """A check that ``column`` is ``prefix-N``, N from 1 to 999999999."""
    start = len(prefix) + 2
    return (
        f"{column} GLOB '{prefix}-[1-9]*' AND length({column}) <= {start + 8} "
        f"AND substr({column}, {start}) NOT GLOB '*[^0-9]*'"
    )


def _memory(schema: Schema) -> None:
    """Version 24: the project memory (docs/specs/project-memory.md, PM-1 to PM-3, PM-11):
    each project's entries, numbered ``mem-N``, with their place on the Memory page; their
    versions, written once, one for each change of the memory that touched the entry (a
    move keeps the entry's revision, which counts the changes of its text); the agents'
    proposals, numbered ``prop-N``, each decided once; and the memory's version, which
    counts the changes of its accepted entries, and its counters, which never go back."""
    entry, proposal = _numbered("entry_id", "mem"), _numbered("proposal_id", "prop")
    for statement in (
        """CREATE TABLE memory_heads (
    project TEXT PRIMARY KEY CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    version INTEGER NOT NULL CHECK (version BETWEEN 0 AND 999999999),
    entries INTEGER NOT NULL CHECK (entries BETWEEN 0 AND 999999999),
    proposals INTEGER NOT NULL CHECK (proposals BETWEEN 0 AND 999999999)
) STRICT""",
        """CREATE TRIGGER memory_heads_no_delete BEFORE DELETE ON memory_heads
BEGIN SELECT RAISE(ABORT, 'a project memory is kept'); END""",
        """CREATE TRIGGER memory_heads_forward BEFORE UPDATE ON memory_heads
WHEN NEW.project != OLD.project OR NEW.version < OLD.version
    OR NEW.entries < OLD.entries OR NEW.proposals < OLD.proposals
BEGIN SELECT RAISE(ABORT, 'a project memory version or counter never goes back'); END""",
        f"""CREATE TABLE memory_entries (
    project TEXT NOT NULL REFERENCES memory_heads(project),
    entry_id TEXT NOT NULL CHECK ({entry}),
    head INTEGER NOT NULL CHECK (head BETWEEN 1 AND 999999999),
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 999999999),
    removed INTEGER NOT NULL CHECK (removed IN (0, 1)),
    PRIMARY KEY (project, entry_id)
) STRICT""",
        "CREATE INDEX memory_entries_order ON memory_entries(project,removed,position)",
        """CREATE TRIGGER memory_entries_kept BEFORE UPDATE OF project,entry_id ON memory_entries
BEGIN SELECT RAISE(ABORT, 'a memory entry keeps its identity'); END""",
        """CREATE TRIGGER memory_entries_head BEFORE UPDATE OF head ON memory_entries
WHEN NEW.head != OLD.head + 1
BEGIN SELECT RAISE(ABORT, 'a memory entry moves one revision at a time'); END""",
        """CREATE TRIGGER memory_entries_removed BEFORE UPDATE OF removed ON memory_entries
WHEN NEW.removed < OLD.removed
BEGIN SELECT RAISE(ABORT, 'a removed memory entry stays removed'); END""",
        """CREATE TRIGGER memory_entries_no_delete BEFORE DELETE ON memory_entries
BEGIN SELECT RAISE(ABORT, 'memory entries are kept'); END""",
        f"""CREATE TABLE memory_versions (
    project TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    version INTEGER NOT NULL CHECK (version BETWEEN 1 AND 999999999),
    change TEXT NOT NULL CHECK (change IN ('added', 'edited', 'removed', 'moved')),
    text TEXT CHECK (text IS NULL OR length(text) BETWEEN 1 AND 1000),
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 999999999),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 4096),
    proposal TEXT CHECK (proposal IS NULL OR ({_numbered("proposal", "prop")})),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    CHECK ((change = 'removed') = (text IS NULL)),
    PRIMARY KEY (project, entry_id, version),
    FOREIGN KEY (project, entry_id) REFERENCES memory_entries(project, entry_id)
) STRICT""",
        "CREATE INDEX memory_versions_version ON memory_versions(project,version)",
        """CREATE TRIGGER memory_versions_no_update BEFORE UPDATE ON memory_versions
BEGIN SELECT RAISE(ABORT, 'memory versions are immutable'); END""",
        """CREATE TRIGGER memory_versions_no_delete BEFORE DELETE ON memory_versions
BEGIN SELECT RAISE(ABORT, 'memory versions are immutable'); END""",
        f"""CREATE TABLE memory_proposals (
    project TEXT NOT NULL REFERENCES memory_heads(project),
    proposal_id TEXT NOT NULL CHECK ({proposal}),
    action TEXT NOT NULL CHECK (action IN ('add', 'change', 'remove')),
    entry_id TEXT CHECK (entry_id IS NULL OR ({entry})),
    base INTEGER CHECK (base IS NULL OR base BETWEEN 1 AND 999999999),
    text TEXT CHECK (text IS NULL OR length(text) BETWEEN 1 AND 1000),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    proposer TEXT NOT NULL
        CHECK (json_valid(proposer) AND length(CAST(proposer AS BLOB)) <= 4096),
    run_id TEXT NOT NULL CHECK (length(CAST(run_id AS BLOB)) BETWEEN 1 AND 128),
    session TEXT NOT NULL CHECK (length(CAST(session AS BLOB)) BETWEEN 1 AND 128),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    state TEXT NOT NULL CHECK (state IN ('waiting', 'accepted', 'declined')),
    decided TEXT CHECK (decided IS NULL OR length(decided) BETWEEN 1 AND 64),
    decider TEXT CHECK (decider IS NULL
        OR (json_valid(decider) AND length(CAST(decider AS BLOB)) <= 4096)),
    CHECK ((action = 'add') = (entry_id IS NULL)),
    CHECK ((entry_id IS NULL) = (base IS NULL)),
    CHECK ((action = 'remove') = (text IS NULL)),
    CHECK ((state = 'waiting') = (decided IS NULL)),
    CHECK ((decided IS NULL) = (decider IS NULL)),
    PRIMARY KEY (project, proposal_id)
) STRICT""",
        "CREATE INDEX memory_proposals_state ON memory_proposals(project,state)",
        "CREATE INDEX memory_proposals_runs ON memory_proposals(project,run_id)",
        """CREATE TRIGGER memory_proposals_kept BEFORE UPDATE OF
project,proposal_id,action,entry_id,base,text,reason,proposer,run_id,session,created
ON memory_proposals BEGIN SELECT RAISE(ABORT, 'a memory proposal is written once'); END""",
        """CREATE TRIGGER memory_proposals_decided BEFORE UPDATE OF state,decided,decider
ON memory_proposals WHEN OLD.state != 'waiting'
BEGIN SELECT RAISE(ABORT, 'a memory proposal is decided once'); END""",
        """CREATE TRIGGER memory_proposals_no_delete BEFORE DELETE ON memory_proposals
BEGIN SELECT RAISE(ABORT, 'memory proposals are kept'); END""",
    ):
        schema.execute(statement)


def _ledger_flags(schema: Schema) -> None:
    """Version 25: the ledger's flags for reconsideration (docs/specs/ledger.md, LG-28,
    LG-29): each on an entry at the revision it was raised on, with its cause, what it
    came from, and the flag it spread from; open until the user or the main agent clears
    it with a reason. One open flag per entry, cause and source; a flag is never deleted,
    and only its clearing is ever written to it. The user reopens a cleared flag as a new
    flag that names it (``reopens``)."""
    for statement in (
        """CREATE TABLE ledger_flags (
    flag_id TEXT PRIMARY KEY
        CHECK (length(flag_id) = 29 AND flag_id GLOB 'flag-*'
        AND substr(flag_id, 6) NOT GLOB '*[^0-9a-f]*'),
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    entry_id TEXT NOT NULL REFERENCES ledger_entries(entry_id),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    cause TEXT NOT NULL CHECK (cause IN ('assumption_revised', 'assumption_withdrawn',
        'dependency_status', 'evidence_changed', 'spread')),
    source TEXT NOT NULL CHECK (json_valid(source) AND json_type(source) = 'object'
        AND length(CAST(source AS BLOB)) <= 4096),
    via TEXT REFERENCES ledger_flags(flag_id),
    reopens TEXT REFERENCES ledger_flags(flag_id),
    key TEXT NOT NULL CHECK (length(CAST(key AS BLOB)) BETWEEN 1 AND 512),
    raised TEXT NOT NULL CHECK (length(raised) BETWEEN 1 AND 64),
    cleared TEXT CHECK (cleared IS NULL OR length(cleared) BETWEEN 1 AND 64),
    cleared_by TEXT CHECK (cleared_by IS NULL OR (json_valid(cleared_by)
        AND length(CAST(cleared_by AS BLOB)) <= 8192)),
    reason TEXT CHECK (reason IS NULL OR length(CAST(reason AS BLOB)) BETWEEN 1 AND 16384),
    operation_id TEXT
        CHECK (operation_id IS NULL OR length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 128),
    CHECK ((cause = 'spread') = (via IS NOT NULL)),
    CHECK ((cleared IS NULL) = (cleared_by IS NULL)
        AND (cleared IS NULL) = (reason IS NULL)
        AND (cleared IS NULL) = (operation_id IS NULL))
) STRICT""",
        "CREATE UNIQUE INDEX ledger_flags_open ON ledger_flags(entry_id,key) WHERE cleared IS NULL",
        "CREATE INDEX ledger_flags_project ON ledger_flags(project,cleared,raised)",
        "CREATE INDEX ledger_flags_entries ON ledger_flags(entry_id,cleared)",
        """CREATE TRIGGER ledger_flags_kept BEFORE UPDATE ON ledger_flags
WHEN OLD.cleared IS NOT NULL OR NEW.flag_id IS NOT OLD.flag_id OR NEW.project IS NOT OLD.project
OR NEW.entry_id IS NOT OLD.entry_id OR NEW.revision IS NOT OLD.revision
OR NEW.cause IS NOT OLD.cause OR NEW.source IS NOT OLD.source OR NEW.via IS NOT OLD.via
OR NEW.reopens IS NOT OLD.reopens OR NEW.key IS NOT OLD.key OR NEW.raised IS NOT OLD.raised
BEGIN SELECT RAISE(ABORT, 'a ledger flag is only cleared, once'); END""",
        """CREATE TRIGGER ledger_flags_no_delete BEFORE DELETE ON ledger_flags
BEGIN SELECT RAISE(ABORT, 'ledger flags are kept'); END""",
    ):
        schema.execute(statement)


_NAME: Final = (
    "length({column}) BETWEEN 1 AND 128 AND {column} GLOB '[A-Za-z0-9]*' "
    "AND {column} NOT GLOB '*[^A-Za-z0-9._-]*'"
)
"""A check that ``column`` is an identifier: printable, path-safe, short."""


def _frontier_categories(schema: Schema) -> None:
    """Version 26: the project's frontier categories (docs/specs/ledger.md, LG-18):
    each named in its project, in the order they were defined, at its current revision;
    and every revision, written once: the target and kind of result it compares, its
    measures, each with a unit and a direction, its condition keys, who wrote it, why,
    and under which operation. Every definition the model takes fits: 4096 characters
    of target, kind and reason, and 16 measures, each unit 512 characters, as JSON with
    every character escaped."""
    name = _NAME.format(column="category")
    for statement in (
        f"""CREATE TABLE frontier_categories (
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    category TEXT NOT NULL CHECK ({name}),
    head INTEGER NOT NULL CHECK (head BETWEEN 1 AND 999999999),
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 999999999),
    PRIMARY KEY (project, category)
) STRICT""",
        "CREATE UNIQUE INDEX frontier_categories_order ON frontier_categories(project,position)",
        """CREATE TRIGGER frontier_categories_forward BEFORE UPDATE ON frontier_categories
WHEN NEW.project IS NOT OLD.project OR NEW.category IS NOT OLD.category
    OR NEW.position IS NOT OLD.position OR NEW.head != OLD.head + 1
BEGIN SELECT RAISE(ABORT, 'a frontier category moves on one revision at a time'); END""",
        """CREATE TRIGGER frontier_categories_no_delete BEFORE DELETE ON frontier_categories
BEGIN SELECT RAISE(ABORT, 'frontier categories are kept'); END""",
        """CREATE TABLE frontier_category_revisions (
    project TEXT NOT NULL,
    category TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    target TEXT NOT NULL CHECK (length(CAST(target AS BLOB)) BETWEEN 1 AND 16384),
    kind TEXT NOT NULL CHECK (length(CAST(kind AS BLOB)) BETWEEN 1 AND 16384),
    measures TEXT NOT NULL CHECK (json_valid(measures) AND json_type(measures) = 'array'
        AND json_array_length(measures) BETWEEN 1 AND 16
        AND length(CAST(measures AS BLOB)) <= 65536),
    conditions TEXT NOT NULL CHECK (json_valid(conditions) AND json_type(conditions) = 'array'
        AND json_array_length(conditions) <= 16 AND length(CAST(conditions AS BLOB)) <= 4096),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 8192),
    reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) BETWEEN 1 AND 16384),
    operation_id TEXT NOT NULL CHECK (length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 128),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    PRIMARY KEY (project, category, revision),
    FOREIGN KEY (project, category) REFERENCES frontier_categories(project, category)
) STRICT""",
        """CREATE TRIGGER frontier_category_revisions_no_update
BEFORE UPDATE ON frontier_category_revisions
BEGIN SELECT RAISE(ABORT, 'frontier category revisions are immutable'); END""",
        """CREATE TRIGGER frontier_category_revisions_no_delete
BEFORE DELETE ON frontier_category_revisions
BEGIN SELECT RAISE(ABORT, 'frontier category revisions are immutable'); END""",
    ):
        schema.execute(statement)


def _frontier_changes(schema: Schema) -> None:
    """Version 27: the history of the frontier (docs/specs/ledger.md, LG-24): each change
    to the best known results of a category under one set of conditions, with the set as
    the change left it, what it added and removed, and its cause; written once, in the
    order the changes committed."""
    for statement in (
        """CREATE TABLE frontier_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    category TEXT NOT NULL,
    conditions TEXT NOT NULL CHECK (json_valid(conditions) AND json_type(conditions) = 'object'
        AND length(CAST(conditions AS BLOB)) <= 8192),
    best TEXT NOT NULL CHECK (json_valid(best) AND json_type(best) = 'array'),
    added TEXT NOT NULL CHECK (json_valid(added) AND json_type(added) = 'array'),
    removed TEXT NOT NULL CHECK (json_valid(removed) AND json_type(removed) = 'array'),
    cause TEXT NOT NULL CHECK (json_valid(cause) AND json_type(cause) = 'object'
        AND length(CAST(cause AS BLOB)) <= 16384),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    FOREIGN KEY (project, category) REFERENCES frontier_categories(project, category)
) STRICT""",
        "CREATE INDEX frontier_changes_groups ON frontier_changes(project,category,conditions,seq)",
        "CREATE INDEX frontier_changes_order ON frontier_changes(project,category,seq)",
        """CREATE TRIGGER frontier_changes_no_update BEFORE UPDATE ON frontier_changes
BEGIN SELECT RAISE(ABORT, 'frontier changes are kept as written'); END""",
        """CREATE TRIGGER frontier_changes_no_delete BEFORE DELETE ON frontier_changes
BEGIN SELECT RAISE(ABORT, 'frontier changes are kept as written'); END""",
    ):
        schema.execute(statement)


def _delegations(schema: Schema) -> None:
    """Version 28: delegations (plans-and-delegation spec, PD-11, PD-17, PD-35): each
    one's parent run, its child run, its brief and, once the child ends, its outcome
    (how it ended, its answer, and the plan fence's refusal if there was one), written
    once; and the run of the session that received it. Each project's setting
    says how deep its runs may delegate; a project without a row does not delegate."""
    for statement in (
        """CREATE TABLE delegations (
    delegation_id TEXT PRIMARY KEY
        CHECK (length(delegation_id) = 28 AND delegation_id GLOB 'dlg-*'
        AND substr(delegation_id, 5) NOT GLOB '*[^0-9a-f]*'),
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    parent TEXT NOT NULL CHECK (length(CAST(parent AS BLOB)) BETWEEN 1 AND 256),
    run TEXT NOT NULL UNIQUE CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    kind TEXT NOT NULL CHECK (kind IN ('check')),
    brief TEXT NOT NULL CHECK (json_valid(brief) AND json_type(brief) = 'object'
        AND length(CAST(brief AS BLOB)) <= 65536),
    started_at TEXT NOT NULL CHECK (length(started_at) BETWEEN 1 AND 64),
    ended TEXT CHECK (ended IS NULL
        OR ended IN ('returned', 'failed', 'cancelled', 'limit_reached', 'interrupted')),
    stopped TEXT CHECK (stopped IS NULL OR length(stopped) BETWEEN 1 AND 64),
    report TEXT CHECK (report IS NULL OR length(CAST(report AS BLOB)) <= 65536),
    ended_at TEXT CHECK (ended_at IS NULL OR length(ended_at) BETWEEN 1 AND 64),
    refused TEXT CHECK (refused IS NULL OR length(CAST(refused AS BLOB)) BETWEEN 1 AND 2048),
    acknowledged TEXT
        CHECK (acknowledged IS NULL OR length(CAST(acknowledged AS BLOB)) BETWEEN 1 AND 256),
    CHECK ((ended IS NULL) = (ended_at IS NULL) AND (ended IS NULL) = (stopped IS NULL)),
    CHECK (ended IS NOT NULL OR (report IS NULL AND refused IS NULL AND acknowledged IS NULL)),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id)
) STRICT""",
        "CREATE INDEX delegations_waiting ON delegations (project_id, session_id, acknowledged)",
        """CREATE TRIGGER delegations_kept BEFORE UPDATE ON delegations
WHEN NEW.delegation_id IS NOT OLD.delegation_id OR NEW.project_id IS NOT OLD.project_id
OR NEW.session_id IS NOT OLD.session_id OR NEW.parent IS NOT OLD.parent
OR NEW.run IS NOT OLD.run OR NEW.kind IS NOT OLD.kind OR NEW.brief IS NOT OLD.brief
OR NEW.started_at IS NOT OLD.started_at OR (OLD.ended IS NOT NULL AND (
NEW.ended IS NOT OLD.ended OR NEW.stopped IS NOT OLD.stopped
OR NEW.report IS NOT OLD.report OR NEW.ended_at IS NOT OLD.ended_at
OR NEW.refused IS NOT OLD.refused))
OR (OLD.acknowledged IS NOT NULL AND NEW.acknowledged IS NOT OLD.acknowledged)
BEGIN SELECT RAISE(ABORT, 'a delegation''s outcome is written once'); END""",
        """CREATE TRIGGER delegations_no_delete BEFORE DELETE ON delegations
BEGIN SELECT RAISE(ABORT, 'delegations are kept'); END""",
        """CREATE TABLE project_delegation (
    project_id TEXT PRIMARY KEY REFERENCES projects (project_id),
    depth INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 2),
    updated_at TEXT NOT NULL
) STRICT""",
    ):
        schema.execute(statement)


def _delegation_results(schema: Schema) -> None:
    """Version 29: a delegation's outcome gains the child's result as its output schema
    validated it, the validation errors, whether the child read the entry it checked,
    and the model alias and the models it ran on (PD-17, PD-18, PD-29), all written once
    with the rest of the outcome. A result belongs only to a delegation that returned.
    The columns have no CHECK, which ``ALTER TABLE`` would have every row pass: triggers
    check them as a row is written, and the rows written before have none of them."""
    outcome = (
        "(NEW.result IS NULL OR (NEW.ended = 'returned' AND json_valid(NEW.result) "
        "AND json_type(NEW.result) = 'object' AND length(CAST(NEW.result AS BLOB)) <= 32768)) "
        "AND (NEW.errors IS NULL OR (json_valid(NEW.errors) AND json_type(NEW.errors) = "
        "'array' AND length(CAST(NEW.errors AS BLOB)) <= 16384)) "
        "AND (NEW.alias IS NULL OR length(CAST(NEW.alias AS BLOB)) BETWEEN 1 AND 256) "
        "AND (NEW.models IS NULL OR (json_valid(NEW.models) AND json_type(NEW.models) = "
        "'array' AND length(CAST(NEW.models AS BLOB)) <= 4096)) "
        "AND (NEW.read_entry IS NULL OR NEW.read_entry IN (0, 1)) "
        "AND (NEW.ended IS NOT NULL OR (NEW.result IS NULL AND NEW.errors IS NULL "
        "AND NEW.alias IS NULL AND NEW.models IS NULL AND NEW.read_entry IS NULL))"
    )
    for statement in (
        "ALTER TABLE delegations ADD COLUMN result TEXT",
        "ALTER TABLE delegations ADD COLUMN errors TEXT",
        "ALTER TABLE delegations ADD COLUMN alias TEXT",
        "ALTER TABLE delegations ADD COLUMN models TEXT",
        "ALTER TABLE delegations ADD COLUMN read_entry INTEGER",
        f"""CREATE TRIGGER delegations_outcome_insert BEFORE INSERT ON delegations
WHEN NOT ({outcome}) BEGIN SELECT RAISE(ABORT, 'CHECK constraint failed: delegations'); END""",
        f"""CREATE TRIGGER delegations_outcome_update BEFORE UPDATE ON delegations
WHEN NOT ({outcome}) BEGIN SELECT RAISE(ABORT, 'CHECK constraint failed: delegations'); END""",
        "DROP TRIGGER delegations_kept",
        """CREATE TRIGGER delegations_kept BEFORE UPDATE ON delegations
WHEN NEW.delegation_id IS NOT OLD.delegation_id OR NEW.project_id IS NOT OLD.project_id
OR NEW.session_id IS NOT OLD.session_id OR NEW.parent IS NOT OLD.parent
OR NEW.run IS NOT OLD.run OR NEW.kind IS NOT OLD.kind OR NEW.brief IS NOT OLD.brief
OR NEW.started_at IS NOT OLD.started_at OR (OLD.ended IS NOT NULL AND (
NEW.ended IS NOT OLD.ended OR NEW.stopped IS NOT OLD.stopped
OR NEW.report IS NOT OLD.report OR NEW.ended_at IS NOT OLD.ended_at
OR NEW.refused IS NOT OLD.refused OR NEW.result IS NOT OLD.result
OR NEW.errors IS NOT OLD.errors OR NEW.alias IS NOT OLD.alias
OR NEW.models IS NOT OLD.models OR NEW.read_entry IS NOT OLD.read_entry))
OR (OLD.acknowledged IS NOT NULL AND NEW.acknowledged IS NOT OLD.acknowledged)
BEGIN SELECT RAISE(ABORT, 'a delegation''s outcome is written once'); END""",
    ):
        schema.execute(statement)


def _evidence_targets(schema: Schema) -> None:
    """Version 30: two more evidence targets, ``quote`` and ``delegation``, and a
    ``locator`` for what either names beyond its target (docs/specs/ledger.md, LG-8;
    one migration that lane S's quotes and lane D's delegations share). A quote names
    the reference it quotes at a revision, with the PDF's SHA-256 when it quotes a
    PDF, and its locator says where and what: the page, its lines and the text, or
    the ePrint passage and the date stamp the server gave. A review target alone names
    no revision; an artifact names its SHA-256."""
    rebuild(
        schema,
        "ledger_evidence",
        """CREATE TABLE ledger_evidence (
    entry_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 128),
    evidence_id TEXT NOT NULL
        CHECK (length(evidence_id) = 33 AND evidence_id GLOB 'evidence-*'
        AND substr(evidence_id, 10) NOT GLOB '*[^0-9a-f]*'),
    role TEXT NOT NULL
        CHECK (role IN ('supports', 'contradicts', 'derivation', 'independent_check')),
    type TEXT NOT NULL CHECK (type IN ('proof', 'exact_computation', 'finite_test',
        'numerical_experiment', 'source_claim', 'synthetic', 'unclassified')),
    target TEXT NOT NULL CHECK (target IN ('entry', 'artifact', 'block', 'review', 'quote',
        'delegation')),
    target_id TEXT NOT NULL CHECK (length(CAST(target_id AS BLOB)) BETWEEN 1 AND 128),
    target_revision INTEGER
        CHECK (target_revision IS NULL OR target_revision BETWEEN 1 AND 999999999),
    block TEXT CHECK (block IS NULL OR length(CAST(block AS BLOB)) BETWEEN 1 AND 128),
    span TEXT CHECK (span IS NULL OR length(CAST(span AS BLOB)) BETWEEN 1 AND 128),
    sha256 TEXT CHECK (sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
    checked INTEGER CHECK (checked IS NULL OR checked BETWEEN 1 AND 999999999),
    scope TEXT CHECK (scope IS NULL OR length(CAST(scope AS BLOB)) BETWEEN 1 AND 16384),
    added INTEGER NOT NULL CHECK (added BETWEEN 1 AND revision),
    locator TEXT CHECK (locator IS NULL OR (json_valid(locator) AND json_type(locator) = 'object'
        AND length(CAST(locator AS BLOB)) <= 16384)),
    CHECK ((role = 'independent_check') = (checked IS NOT NULL)),
    CHECK ((target = 'block') = (block IS NOT NULL)),
    CHECK (span IS NULL OR block IS NOT NULL),
    CHECK (target != 'review' OR target_revision IS NULL),
    CHECK (target NOT IN ('entry', 'artifact', 'block', 'quote') OR target_revision IS NOT NULL),
    CHECK (target != 'artifact' OR sha256 IS NOT NULL),
    CHECK (sha256 IS NULL OR target IN ('artifact', 'quote', 'delegation')),
    CHECK (target != 'quote' OR locator IS NOT NULL),
    CHECK (locator IS NULL OR target IN ('quote', 'delegation')),
    PRIMARY KEY (entry_id, revision, ordinal),
    UNIQUE (entry_id, revision, evidence_id),
    FOREIGN KEY (entry_id, revision) REFERENCES ledger_revisions(entry_id, revision)
) STRICT""",
    )


def _citation_checks(schema: Schema) -> None:
    """Version 31: the consistency reviewer's checks of quotes (docs/specs/
    consistency-review.md, CR-4 as the coordinator amended it): for one quote an entry
    revision holds, the review that checked it, the source it read with the tool
    and the arguments that returned it (whole when they fit 8 KiB of JSON, else clipped,
    with the digest of the whole), the digest
    of the revision's statement, fields, assumptions and evidence it covered, and the
    verdict with its explanation. Written once."""
    for statement in (
        """CREATE TABLE citation_checks (
    check_id TEXT PRIMARY KEY CHECK (length(check_id) = 33 AND check_id GLOB 'citation-*'
        AND substr(check_id, 10) NOT GLOB '*[^0-9a-f]*'),
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    review_id TEXT NOT NULL REFERENCES review_runs(review_id),
    entry_id TEXT NOT NULL,
    entry_revision INTEGER NOT NULL CHECK (entry_revision BETWEEN 1 AND 999999999),
    evidence_id TEXT NOT NULL CHECK (length(CAST(evidence_id AS BLOB)) BETWEEN 1 AND 64),
    material TEXT NOT NULL CHECK (length(material) = 64 AND material NOT GLOB '*[^0-9a-f]*'),
    source TEXT NOT NULL CHECK (length(CAST(source AS BLOB)) BETWEEN 1 AND 64),
    source_tool TEXT NOT NULL CHECK (length(CAST(source_tool AS BLOB)) BETWEEN 1 AND 128),
    source_arguments TEXT NOT NULL CHECK (json_valid(source_arguments)
        AND length(CAST(source_arguments AS BLOB)) <= 8192),
    source_arguments_sha256 TEXT NOT NULL CHECK (length(source_arguments_sha256) = 64
        AND source_arguments_sha256 NOT GLOB '*[^0-9a-f]*'),
    verdict TEXT NOT NULL CHECK (verdict IN ('supported', 'unsupported', 'inconclusive')),
    explanation TEXT NOT NULL CHECK (length(CAST(explanation AS BLOB)) BETWEEN 1 AND 16384),
    created REAL NOT NULL,
    FOREIGN KEY (entry_id, entry_revision) REFERENCES ledger_revisions(entry_id, revision)
) STRICT""",
        "CREATE INDEX citation_checks_quotes ON citation_checks(entry_id,evidence_id,created)",
        """CREATE TRIGGER citation_checks_no_update BEFORE UPDATE ON citation_checks
BEGIN SELECT RAISE(ABORT, 'citation checks are written once'); END""",
        """CREATE TRIGGER citation_checks_no_delete BEFORE DELETE ON citation_checks
BEGIN SELECT RAISE(ABORT, 'citation checks are written once'); END""",
    ):
        schema.execute(statement)


def _delegation_reads(schema: Schema) -> None:
    """Version 32: a check's outcome gains whether the child read each target of the
    entry's proofs at the revision its link pins (``read_proofs``; LG-17), written once
    with the rest of the outcome. The column has no CHECK, which ``ALTER TABLE`` would
    have every row pass: triggers check it as a row is written."""
    reads = "(NEW.read_proofs IS NULL OR (NEW.ended IS NOT NULL AND NEW.read_proofs IN (0, 1)))"
    for statement in (
        "ALTER TABLE delegations ADD COLUMN read_proofs INTEGER",
        f"""CREATE TRIGGER delegations_reads_insert BEFORE INSERT ON delegations
WHEN NOT {reads} BEGIN SELECT RAISE(ABORT, 'CHECK constraint failed: delegations'); END""",
        f"""CREATE TRIGGER delegations_reads_update BEFORE UPDATE ON delegations
WHEN NOT {reads} BEGIN SELECT RAISE(ABORT, 'CHECK constraint failed: delegations'); END""",
        "DROP TRIGGER delegations_kept",
        """CREATE TRIGGER delegations_kept BEFORE UPDATE ON delegations
WHEN NEW.delegation_id IS NOT OLD.delegation_id OR NEW.project_id IS NOT OLD.project_id
OR NEW.session_id IS NOT OLD.session_id OR NEW.parent IS NOT OLD.parent
OR NEW.run IS NOT OLD.run OR NEW.kind IS NOT OLD.kind OR NEW.brief IS NOT OLD.brief
OR NEW.started_at IS NOT OLD.started_at OR (OLD.ended IS NOT NULL AND (
NEW.ended IS NOT OLD.ended OR NEW.stopped IS NOT OLD.stopped
OR NEW.report IS NOT OLD.report OR NEW.ended_at IS NOT OLD.ended_at
OR NEW.refused IS NOT OLD.refused OR NEW.result IS NOT OLD.result
OR NEW.errors IS NOT OLD.errors OR NEW.alias IS NOT OLD.alias
OR NEW.models IS NOT OLD.models OR NEW.read_entry IS NOT OLD.read_entry
OR NEW.read_proofs IS NOT OLD.read_proofs))
OR (OLD.acknowledged IS NOT NULL AND NEW.acknowledged IS NOT OLD.acknowledged)
BEGIN SELECT RAISE(ABORT, 'a delegation''s outcome is written once'); END""",
    ):
        schema.execute(statement)


def _delegation_limits(schema: Schema) -> None:
    """Version 33: a project's delegation setting gains its limits, as a JSON object: how
    many delegated runs a session's tree runs at once, and a check's tokens and turns
    (plans-and-delegation spec, PD-38). The column has no CHECK, which ``ALTER TABLE``
    would have every row pass: triggers check it as a row is written."""
    limits = (
        "(NEW.limits IS NULL OR (json_valid(NEW.limits) AND json_type(NEW.limits) = 'object' "
        "AND length(CAST(NEW.limits AS BLOB)) <= 4096))"
    )
    for statement in (
        "ALTER TABLE project_delegation ADD COLUMN limits TEXT",
        f"""CREATE TRIGGER project_delegation_insert BEFORE INSERT ON project_delegation
WHEN NOT {limits}
BEGIN SELECT RAISE(ABORT, 'CHECK constraint failed: project_delegation'); END""",
        f"""CREATE TRIGGER project_delegation_update BEFORE UPDATE ON project_delegation
WHEN NOT {limits}
BEGIN SELECT RAISE(ABORT, 'CHECK constraint failed: project_delegation'); END""",
    ):
        schema.execute(statement)


def _ledger_work(schema: Schema) -> None:
    """Version 34: work records (docs/specs/ledger.md, LG-6), append-only, on an issue, a
    claim or a result: what was tried, by whom, how it ended (``limit`` with what ran out,
    ``obstruction_claimed`` with its evidence, ``produced`` with the entries, or
    ``assessment``, which may name the record it assesses and its verdict), its pinned
    references, and the record it is part of. ``position`` orders the project's records
    as they were made. Neither an update, a delete nor an insert over a record (``INSERT
    OR REPLACE``) changes one."""
    for statement in (
        """CREATE TABLE ledger_work (
    record_id TEXT PRIMARY KEY CHECK (length(record_id) = 29 AND record_id GLOB 'work-*'
        AND substr(record_id, 6) NOT GLOB '*[^0-9a-f]*'),
    position INTEGER NOT NULL UNIQUE CHECK (position >= 1),
    project TEXT NOT NULL CHECK (length(CAST(project AS BLOB)) BETWEEN 1 AND 128),
    entry_id TEXT NOT NULL REFERENCES ledger_entries(entry_id),
    entry_revision INTEGER NOT NULL CHECK (entry_revision BETWEEN 1 AND 999999999),
    tried TEXT NOT NULL CHECK (length(CAST(tried AS BLOB)) BETWEEN 1 AND 32768),
    ending TEXT NOT NULL
        CHECK (ending IN ('limit', 'obstruction_claimed', 'produced', 'assessment')),
    detail TEXT NOT NULL CHECK (json_valid(detail) AND json_type(detail) = 'object'
        AND length(CAST(detail AS BLOB)) <= 16384),
    refs TEXT NOT NULL CHECK (json_valid(refs) AND json_type(refs) = 'array'
        AND length(CAST(refs AS BLOB)) <= 65536),
    part_of TEXT REFERENCES ledger_work(record_id),
    assesses TEXT REFERENCES ledger_work(record_id),
    verdict TEXT CHECK (verdict IS NULL OR verdict IN ('accepted', 'rejected', 'inconclusive')),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 8192),
    reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) BETWEEN 1 AND 16384),
    operation_id TEXT NOT NULL CHECK (length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 128),
    provenance TEXT CHECK (provenance IS NULL OR (json_valid(provenance)
        AND json_type(provenance) = 'object' AND length(CAST(provenance AS BLOB)) <= 4096)),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    CHECK ((ending = 'limit') = (coalesce(json_type(detail, '$.limit'), '') = 'text')),
    CHECK ((ending = 'produced')
        = (coalesce(json_array_length(detail, '$.produced'), 0) >= 1)),
    CHECK ((assesses IS NULL) = (verdict IS NULL)),
    CHECK (ending = 'assessment' OR assesses IS NULL),
    CHECK (ending != 'obstruction_claimed' OR json_array_length(refs) >= 1)
) STRICT""",
        "CREATE INDEX ledger_work_entry ON ledger_work(entry_id, position)",
        "CREATE INDEX ledger_work_assesses ON ledger_work(assesses, position) "
        "WHERE assesses IS NOT NULL",
        """CREATE TRIGGER ledger_work_no_replace BEFORE INSERT ON ledger_work
WHEN EXISTS (SELECT 1 FROM ledger_work WHERE record_id = NEW.record_id
    OR position = NEW.position)
BEGIN SELECT RAISE(ABORT, 'work records are append-only'); END""",
        """CREATE TRIGGER ledger_work_no_update BEFORE UPDATE ON ledger_work
BEGIN SELECT RAISE(ABORT, 'work records are append-only'); END""",
        """CREATE TRIGGER ledger_work_no_delete BEFORE DELETE ON ledger_work
BEGIN SELECT RAISE(ABORT, 'work records are append-only'); END""",
    ):
        schema.execute(statement)


def _skills(schema: Schema) -> None:
    """Version 35: imported skill packs (docs/specs/skills.md, SK-1 to SK-6): their files,
    content-addressed and written once (``skill_blobs``); each snapshot's files and the
    skills it holds; each import, with its source, origin, ref, resolved commit or
    archive digest and report; and each project's pin of a source's import, whose
    revision counts its moves."""
    hex64 = "length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*'"
    name = "length({0}) BETWEEN 1 AND 64 AND {0} NOT GLOB '*[^a-z0-9-]*'"
    for statement in (
        f"""CREATE TABLE skill_blobs (
    sha256 TEXT PRIMARY KEY CHECK ({hex64.format("sha256")}),
    content BLOB NOT NULL CHECK (length(content) <= 1048576)
) STRICT""",
        f"""CREATE TABLE skill_snapshots (
    snapshot TEXT PRIMARY KEY CHECK ({hex64.format("snapshot")}),
    files INTEGER NOT NULL CHECK (files BETWEEN 1 AND 4096),
    bytes INTEGER NOT NULL CHECK (bytes BETWEEN 0 AND 33554432)
) STRICT""",
        """CREATE TABLE skill_files (
    snapshot TEXT NOT NULL REFERENCES skill_snapshots(snapshot),
    path TEXT NOT NULL CHECK (length(CAST(path AS BLOB)) BETWEEN 1 AND 1024),
    blob TEXT NOT NULL REFERENCES skill_blobs(sha256),
    executable INTEGER NOT NULL CHECK (executable IN (0, 1)),
    PRIMARY KEY (snapshot, path)
) STRICT""",
        f"""CREATE TABLE skill_skills (
    snapshot TEXT NOT NULL REFERENCES skill_snapshots(snapshot),
    name TEXT NOT NULL CHECK ({name.format("name")}),
    description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 1024),
    body_sha256 TEXT NOT NULL CHECK ({hex64.format("body_sha256")}),
    metadata TEXT NOT NULL CHECK (json_valid(metadata)
        AND length(CAST(metadata AS BLOB)) <= 16384),
    PRIMARY KEY (snapshot, name)
) STRICT""",
        f"""CREATE TABLE skill_imports (
    import_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK ({name.format("source")}),
    snapshot TEXT NOT NULL REFERENCES skill_snapshots(snapshot),
    kind TEXT NOT NULL CHECK (kind IN ('git', 'archive')),
    origin TEXT NOT NULL CHECK (length(CAST(origin AS BLOB)) BETWEEN 1 AND 4096),
    ref TEXT CHECK (ref IS NULL OR length(ref) BETWEEN 1 AND 200),
    commit_id TEXT CHECK (commit_id IS NULL OR (length(commit_id) IN (40, 64)
        AND commit_id NOT GLOB '*[^0-9a-f]*')),
    archive_sha256 TEXT CHECK (archive_sha256 IS NULL OR ({hex64.format("archive_sha256")})),
    root TEXT NOT NULL CHECK (length(CAST(root AS BLOB)) <= 1024),
    report TEXT NOT NULL CHECK (json_valid(report) AND length(CAST(report AS BLOB)) <= 1048576),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 4096),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    CHECK ((kind = 'git') = (commit_id IS NOT NULL)),
    CHECK ((kind = 'archive') = (archive_sha256 IS NOT NULL))
) STRICT""",
        "CREATE INDEX skill_imports_source ON skill_imports(source,import_id)",
        f"""CREATE TABLE project_skill_pins (
    project TEXT NOT NULL REFERENCES projects(project_id),
    source TEXT NOT NULL CHECK ({name.format("source")}),
    import_id INTEGER NOT NULL REFERENCES skill_imports(import_id),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 4096),
    pinned TEXT NOT NULL CHECK (length(pinned) BETWEEN 1 AND 64),
    PRIMARY KEY (project, source)
) STRICT""",
        """CREATE TRIGGER project_skill_pins_forward BEFORE UPDATE ON project_skill_pins
WHEN NEW.project != OLD.project OR NEW.source != OLD.source OR NEW.revision != OLD.revision + 1
BEGIN SELECT RAISE(ABORT, 'a skill pin moves one revision at a time'); END""",
    ):
        schema.execute(statement)
    for table in ("skill_blobs", "skill_snapshots", "skill_files", "skill_skills", "skill_imports"):
        for action in ("UPDATE", "DELETE"):
            schema.execute(
                f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'imported skill packs are written once'); END"
            )


def _cost_limits(schema: Schema) -> None:
    """Version 36: a project's optional daily cost limit, in the gateway's currency
    (run-service spec RS-6a; the owner, 2026-10-15), in place of its MAIN token budget,
    whose table goes with what it held: spending is mostly enforced upstream, by the
    gateway's own budget, and a project without a limit has none."""
    for statement in (
        """CREATE TABLE project_cost_limits (
    project_id TEXT PRIMARY KEY REFERENCES projects (project_id),
    daily_cost REAL NOT NULL CHECK (daily_cost BETWEEN 0 AND 1000000000),
    updated_at TEXT NOT NULL
) STRICT""",
        "DROP TABLE project_budgets",
    ):
        schema.execute(statement)


def _user_questions(schema: Schema) -> None:
    """Version 37: questions a main run asks the user (plans-and-delegation spec, PD-23 to
    PD-26): the question, its options and whether several may be chosen, written once by
    the run that asked; then, once, how it closed: the user's answer, with the chat
    message that gave it when the user answered by writing to the session, or why no run
    can receive one. Questions are kept, and are not permission requests (PD-25)."""
    for statement in (
        f"""CREATE TABLE user_questions (
    question_id TEXT PRIMARY KEY
        CHECK (length(question_id) = 28 AND question_id GLOB 'qst-*'
        AND substr(question_id, 5) NOT GLOB '*[^0-9a-f]*'),
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run TEXT NOT NULL CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    tool_call TEXT NOT NULL CHECK (length(CAST(tool_call AS BLOB)) BETWEEN 1 AND 256),
    operation TEXT NOT NULL CHECK (length(CAST(operation AS BLOB)) BETWEEN 1 AND 256),
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 4000),
    options TEXT NOT NULL CHECK (json_valid(options) AND json_type(options) = 'array'
        AND json_array_length(options) IN (0, 2, 3, 4) AND length(CAST(options AS BLOB)) <= 16384),
    multiple INTEGER NOT NULL CHECK (multiple IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN ('open', 'answered', 'cancelled', 'interrupted')),
    asked_at TEXT NOT NULL CHECK (length(asked_at) BETWEEN 1 AND 64),
    answer TEXT CHECK (answer IS NULL OR (json_valid(answer) AND json_type(answer) = 'object'
        AND length(CAST(answer AS BLOB)) <= 131072)),
    closed_at TEXT CHECK (closed_at IS NULL OR length(closed_at) BETWEEN 1 AND 64),
    reason TEXT CHECK (reason IS NULL OR length(CAST(reason AS BLOB)) BETWEEN 1 AND 1024),
    message_id TEXT UNIQUE CHECK (message_id IS NULL OR ({_ID.format("message_id")})),
    CHECK ((state = 'open') = (closed_at IS NULL)),
    CHECK (message_id IS NULL OR state = 'answered'),
    CHECK ((state = 'answered') <= (answer IS NOT NULL)),
    CHECK (state NOT IN ('open', 'interrupted') OR answer IS NULL),
    CHECK (multiple = 0 OR json_array_length(options) > 0),
    UNIQUE (project_id, session_id, operation),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id)
) STRICT""",
        "CREATE INDEX user_questions_open ON user_questions (project_id, session_id, state)",
        """CREATE TRIGGER user_questions_kept BEFORE UPDATE ON user_questions
WHEN OLD.state != 'open' OR NEW.question_id IS NOT OLD.question_id
OR NEW.project_id IS NOT OLD.project_id OR NEW.session_id IS NOT OLD.session_id
OR NEW.run IS NOT OLD.run OR NEW.tool_call IS NOT OLD.tool_call
OR NEW.operation IS NOT OLD.operation OR NEW.text IS NOT OLD.text
OR NEW.options IS NOT OLD.options OR NEW.multiple IS NOT OLD.multiple
OR NEW.asked_at IS NOT OLD.asked_at
BEGIN SELECT RAISE(ABORT, 'a question is written once, and closed once'); END""",
        """CREATE TRIGGER user_questions_no_delete BEFORE DELETE ON user_questions
BEGIN SELECT RAISE(ABORT, 'questions are kept'); END""",
    ):
        schema.execute(statement)


def _delegation_files(schema: Schema) -> None:
    """Version 38: a delegation's outcome gains ``files``, the platform's own list of the
    artifact revisions and tombstones the child's calls recorded, at most 200 with their
    total and the calls' gaps (research artifacts, RA-36), written once with the rest of
    the outcome. The column has no CHECK, which ``ALTER TABLE`` would have every row
    pass: triggers check it as a row is written."""
    files = (
        "(NEW.files IS NULL OR (NEW.ended IS NOT NULL AND json_valid(NEW.files) "
        "AND json_type(NEW.files) = 'object' AND length(CAST(NEW.files AS BLOB)) <= 131072))"
    )
    for statement in (
        "ALTER TABLE delegations ADD COLUMN files TEXT",
        f"""CREATE TRIGGER delegations_files_insert BEFORE INSERT ON delegations
WHEN NOT {files} BEGIN SELECT RAISE(ABORT, 'CHECK constraint failed: delegations'); END""",
        f"""CREATE TRIGGER delegations_files_update BEFORE UPDATE ON delegations
WHEN NOT {files} BEGIN SELECT RAISE(ABORT, 'CHECK constraint failed: delegations'); END""",
        "DROP TRIGGER delegations_kept",
        """CREATE TRIGGER delegations_kept BEFORE UPDATE ON delegations
WHEN NEW.delegation_id IS NOT OLD.delegation_id OR NEW.project_id IS NOT OLD.project_id
OR NEW.session_id IS NOT OLD.session_id OR NEW.parent IS NOT OLD.parent
OR NEW.run IS NOT OLD.run OR NEW.kind IS NOT OLD.kind OR NEW.brief IS NOT OLD.brief
OR NEW.started_at IS NOT OLD.started_at OR (OLD.ended IS NOT NULL AND (
NEW.ended IS NOT OLD.ended OR NEW.stopped IS NOT OLD.stopped
OR NEW.report IS NOT OLD.report OR NEW.ended_at IS NOT OLD.ended_at
OR NEW.refused IS NOT OLD.refused OR NEW.result IS NOT OLD.result
OR NEW.errors IS NOT OLD.errors OR NEW.alias IS NOT OLD.alias
OR NEW.models IS NOT OLD.models OR NEW.read_entry IS NOT OLD.read_entry
OR NEW.read_proofs IS NOT OLD.read_proofs OR NEW.files IS NOT OLD.files))
OR (OLD.acknowledged IS NOT NULL AND NEW.acknowledged IS NOT OLD.acknowledged)
BEGIN SELECT RAISE(ABORT, 'a delegation''s outcome is written once'); END""",
    ):
        schema.execute(statement)


def _delegation_tasks(schema: Schema) -> None:
    """Version 39: a delegation's kind is a ``check`` or a ``task``, a brief the parent
    writes (plans-and-delegation spec, PD-11). The table is made again with the wider
    CHECK, its columns, rows, indexes and triggers as they were."""
    (create,) = schema.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='delegations'"
    ).fetchone()
    old = "CHECK (kind IN ('check'))"
    if old not in create:
        raise ControlError(INTERNAL, "the delegations table's kind is not as version 38 left it")
    rebuild(schema, "delegations", create.replace(old, "CHECK (kind IN ('check', 'task'))", 1))


def _role_costs(schema: Schema) -> None:
    """Version 40: what each reviewer and job watcher call cost, as the gateway reported
    it, in its currency (run-service spec RS-6a; lab 40, B5): the project's cost and its
    daily limit count every role's calls, as they count MAIN's and delegated runs'. A call
    the gateway gave no cost for, or made before this version, has none."""
    for statement in (
        "ALTER TABLE review_calls ADD COLUMN cost REAL CHECK (cost IS NULL OR cost >= 0)",
        "ALTER TABLE job_watcher_calls ADD COLUMN cost REAL CHECK (cost IS NULL OR cost >= 0)",
    ):
        schema.execute(statement)


def _skill_settings(schema: Schema) -> None:
    """Version 41: each project's skill settings (docs/specs/skills.md, SK-33, SK-38): the
    skills loaded in full at every run's start and the skills left out of its set, by
    qualified ID, changed only by the user, with a revision that counts the changes. A
    project without a row has neither."""
    schema.execute(
        """CREATE TABLE project_skill_settings (
    project TEXT PRIMARY KEY REFERENCES projects(project_id),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 999999999),
    always TEXT NOT NULL CHECK (json_valid(always) AND json_type(always) = 'array'
        AND length(CAST(always AS BLOB)) <= 16384),
    omitted TEXT NOT NULL CHECK (json_valid(omitted) AND json_type(omitted) = 'array'
        AND length(CAST(omitted AS BLOB)) <= 65536),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 4096),
    changed TEXT NOT NULL CHECK (length(changed) BETWEEN 1 AND 64)
) STRICT"""
    )


def _builtin_skills(schema: Schema) -> None:
    """Version 42: an import may be of the built-in skills (docs/specs/skills.md, SK-40),
    which the harness ships: no commit or archive, and its ref names the harness version
    and the snapshot. The source ``builtin`` is theirs alone. A pack the user imported
    under that name before is renamed ``builtin-user`` (or ``builtin-user-2`` and on, if
    taken), with its pins and the settings that name its skills; each of its imports'
    reports says so in a warning."""
    hex64 = "length({0}) = 64 AND {0} NOT GLOB '*[^0-9a-f]*'"
    name = "length({0}) BETWEEN 1 AND 64 AND {0} NOT GLOB '*[^a-z0-9-]*'"
    taken = {row[0] for row in schema.execute("SELECT DISTINCT source FROM skill_imports")}
    if "builtin" in taken:
        candidates = ("builtin-user", *(f"builtin-user-{n}" for n in range(2, 10000)))
        renamed = next(candidate for candidate in candidates if candidate not in taken)
        note = (
            f"renamed from builtin to {renamed} by version 42: builtin names the platform's "
            "own skills"
        )
        guards = schema.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('skill_imports_no_update','project_skill_pins_forward')"
        ).fetchall()
        for guard, _ in guards:
            schema.execute(f'DROP TRIGGER "{guard}"')
        schema.execute(
            "UPDATE skill_imports SET source=?,report=json_set(report,'$.warnings',json("
            "json_insert(coalesce(json_extract(report,'$.warnings'),'[]'),'$[#]',?))) "
            "WHERE source='builtin'",
            (renamed, note),
        )
        schema.execute("UPDATE project_skill_pins SET source=? WHERE source='builtin'", (renamed,))
        for column in ("always", "omitted"):
            schema.execute(
                f"UPDATE project_skill_settings SET {column}="  # noqa: S608 - its own names
                f"replace({column},'\"builtin/','\"' || ? || '/')",
                (renamed,),
            )
        for _, sql in guards:
            schema.execute(sql)
    rebuild(
        schema,
        "skill_imports",
        f"""CREATE TABLE skill_imports (
    import_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK ({name.format("source")}),
    snapshot TEXT NOT NULL REFERENCES skill_snapshots(snapshot),
    kind TEXT NOT NULL CHECK (kind IN ('git', 'archive', 'builtin')),
    origin TEXT NOT NULL CHECK (length(CAST(origin AS BLOB)) BETWEEN 1 AND 4096),
    ref TEXT CHECK (ref IS NULL OR length(ref) BETWEEN 1 AND 200),
    commit_id TEXT CHECK (commit_id IS NULL OR (length(commit_id) IN (40, 64)
        AND commit_id NOT GLOB '*[^0-9a-f]*')),
    archive_sha256 TEXT CHECK (archive_sha256 IS NULL OR ({hex64.format("archive_sha256")})),
    root TEXT NOT NULL CHECK (length(CAST(root AS BLOB)) <= 1024),
    report TEXT NOT NULL CHECK (json_valid(report) AND length(CAST(report AS BLOB)) <= 1048576),
    author TEXT NOT NULL CHECK (json_valid(author) AND length(CAST(author AS BLOB)) <= 4096),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    CHECK ((kind = 'git') = (commit_id IS NOT NULL)),
    CHECK ((kind = 'archive') = (archive_sha256 IS NOT NULL)),
    CHECK (kind != 'builtin' OR ref IS NOT NULL),
    CHECK ((source = 'builtin') = (kind = 'builtin'))
) STRICT""",
    )


def _skill_vectors(schema: Schema) -> None:
    """Version 43: each skill's embedding for retrieval (docs/specs/skills.md, SK-29), by
    snapshot, the model the gateway reported and its dimensions, as little-endian float32,
    written once: an import of the same snapshot by the same model reuses them, and
    another model's are kept apart. The space each alias last answered in, by snapshot,
    so that an import whose vectors are there asks the gateway nothing. And the gateway's
    embedding calls, with what it reported they cost, by project (RS-6a)."""
    name = "length(name) BETWEEN 1 AND 64 AND name NOT GLOB '*[^a-z0-9-]*'"
    model = "length(CAST(model AS BLOB)) BETWEEN 1 AND 200"
    for statement in (
        f"""CREATE TABLE skill_vectors (
    snapshot TEXT NOT NULL,
    model TEXT NOT NULL CHECK ({model}),
    dimensions INTEGER NOT NULL CHECK (dimensions BETWEEN 1 AND 16384),
    name TEXT NOT NULL CHECK ({name}),
    vector BLOB NOT NULL CHECK (length(vector) = 4 * dimensions),
    PRIMARY KEY (snapshot, model, dimensions, name),
    FOREIGN KEY (snapshot, name) REFERENCES skill_skills(snapshot, name)
) STRICT""",
        """CREATE TRIGGER skill_vectors_no_update BEFORE UPDATE ON skill_vectors
BEGIN SELECT RAISE(ABORT, 'skill vectors are written once'); END""",
        """CREATE TRIGGER skill_vectors_no_delete BEFORE DELETE ON skill_vectors
BEGIN SELECT RAISE(ABORT, 'skill vectors are written once'); END""",
        f"""CREATE TABLE skill_vector_spaces (
    snapshot TEXT NOT NULL REFERENCES skill_snapshots(snapshot),
    alias TEXT NOT NULL CHECK (length(CAST(alias AS BLOB)) BETWEEN 1 AND 200),
    model TEXT NOT NULL CHECK ({model}),
    dimensions INTEGER NOT NULL CHECK (dimensions BETWEEN 1 AND 16384),
    seen REAL NOT NULL,
    PRIMARY KEY (snapshot, alias, model, dimensions)
) STRICT""",
        f"""CREATE TABLE skill_embedding_calls (
    project TEXT NOT NULL REFERENCES projects(project_id),
    created REAL NOT NULL,
    model TEXT NOT NULL CHECK ({model}),
    texts INTEGER NOT NULL CHECK (texts BETWEEN 1 AND 100000),
    cost REAL CHECK (cost IS NULL OR cost >= 0)
) STRICT""",
        "CREATE INDEX skill_embedding_calls_project ON skill_embedding_calls(project,created)",
    ):
        schema.execute(statement)


def _skill_metadata(schema: Schema) -> None:
    """Version 44: what the import's model pass derived from a snapshot's catalog
    (docs/specs/skills.md, SK-30 to SK-32), written once by snapshot, alias and the pass's
    version, with the items it dropped and the model that answered; and the pass's calls,
    with what the gateway reported they cost, by project when the import names one
    (RS-6a)."""
    alias = "length(CAST({0} AS BLOB)) BETWEEN 1 AND 200"
    for statement in (
        f"""CREATE TABLE skill_metadata (
    snapshot TEXT NOT NULL REFERENCES skill_snapshots(snapshot),
    alias TEXT NOT NULL CHECK ({alias.format("alias")}),
    version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
    model TEXT NOT NULL CHECK ({alias.format("model")}),
    derived TEXT NOT NULL CHECK (json_valid(derived) AND json_type(derived) = 'object'
        AND length(CAST(derived AS BLOB)) <= 262144),
    dropped TEXT NOT NULL CHECK (json_valid(dropped) AND json_type(dropped) = 'array'
        AND length(CAST(dropped AS BLOB)) <= 262144),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    PRIMARY KEY (snapshot, alias, version)
) STRICT""",
        """CREATE TRIGGER skill_metadata_no_update BEFORE UPDATE ON skill_metadata
BEGIN SELECT RAISE(ABORT, 'derived skill metadata is written once'); END""",
        """CREATE TRIGGER skill_metadata_no_delete BEFORE DELETE ON skill_metadata
BEGIN SELECT RAISE(ABORT, 'derived skill metadata is written once'); END""",
        f"""CREATE TABLE skill_metadata_calls (
    project TEXT REFERENCES projects(project_id),
    created REAL NOT NULL,
    snapshot TEXT NOT NULL,
    model TEXT NOT NULL CHECK ({alias.format("model")}),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    cost REAL CHECK (cost IS NULL OR cost >= 0)
) STRICT""",
        "CREATE INDEX skill_metadata_calls_project ON skill_metadata_calls(project,created)",
    ):
        schema.execute(statement)


def _skill_overrides(schema: Schema) -> None:
    """Version 45: each project's overrides of what the import's model pass derives
    (docs/specs/skills.md, SK-33): a skill's short description, its topic and whether it
    is an entry skill, and the derived bindings a pack's skills may not have. They sit in
    the project's skill settings, under the same revision, and survive new pins."""
    schema.execute(
        "ALTER TABLE project_skill_settings ADD COLUMN overrides TEXT NOT NULL DEFAULT '{}' "
        "CHECK (json_valid(overrides) AND json_type(overrides) = 'object' "
        "AND length(CAST(overrides AS BLOB)) <= 131072)"
    )


def _archive(schema: Schema) -> None:
    """Version 46: the sessions' archive (docs/specs/context-management.md, CM-3 to CM-7):
    every message a run adds to its conversation, written whole as the run goes and never
    changed, with its ID, unique in the project, its position in the session, its run
    and parent run, its kind and source, its content as the run added it, a tool
    result's call and links, and an answer's tool calls, native payload, model and
    gateway version; and the texts the search covers, numbered as the documents of two
    FTS5 indexes, by words (``unicode61``, ranked by BM25) and by substrings
    (``trigram``): each message's, and each saved file's behind a sample, kept up to 1
    MiB, with the artifact revision its call recorded. The indexes hold no copy of the
    text (``content=''``); history/archive.py says how to rebuild them."""
    bounded = "{0} IS NULL OR length(CAST({0} AS BLOB)) BETWEEN 1 AND {1}"
    for statement in (
        f"""CREATE TABLE archive_messages (
    project TEXT NOT NULL,
    message_id TEXT NOT NULL CHECK (length(message_id) = 20 AND message_id GLOB 'msg-*'
        AND substr(message_id, 5) NOT GLOB '*[^0-9a-f]*'),
    session TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 999999999999),
    run TEXT NOT NULL CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    parent TEXT CHECK ({bounded.format("parent", 256)}),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 32 AND kind NOT GLOB '*[^a-z_]*'),
    content TEXT CHECK (length(CAST(content AS BLOB)) <= 67108864),
    source TEXT CHECK ({bounded.format("source", 512)}),
    refs TEXT NOT NULL CHECK (json_valid(refs) AND json_type(refs) = 'array'
        AND length(refs) <= 65536),
    call_id TEXT CHECK ({bounded.format("call_id", 512)}),
    tool_calls TEXT CHECK (tool_calls IS NULL OR (json_valid(tool_calls)
        AND json_type(tool_calls) = 'array')),
    files TEXT CHECK (files IS NULL OR (json_valid(files) AND json_type(files) = 'object')),
    model TEXT CHECK ({bounded.format("model", 512)}),
    gateway_version TEXT CHECK ({bounded.format("gateway_version", 128)}),
    native_format TEXT CHECK ({bounded.format("native_format", 64)}),
    native BLOB CHECK (native IS NULL OR length(native) <= 67108864),
    CHECK (content IS NOT NULL OR role = 'assistant'),
    CHECK ((native IS NULL) = (native_format IS NULL)),
    PRIMARY KEY (project, message_id),
    UNIQUE (project, session, position),
    FOREIGN KEY (project, session) REFERENCES sessions (project_id, session_id)
) STRICT""",
        "CREATE INDEX archive_messages_run ON archive_messages (project, session, run, position)",
        """CREATE TRIGGER archive_messages_no_update BEFORE UPDATE ON archive_messages
BEGIN SELECT RAISE(ABORT, 'archived messages are written once'); END""",
        """CREATE TRIGGER archive_messages_no_delete BEFORE DELETE ON archive_messages
BEGIN SELECT RAISE(ABORT, 'archived messages are kept'); END""",
        f"""CREATE TABLE archive_docs (
    doc INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    message_id TEXT NOT NULL,
    path TEXT CHECK ({bounded.format("path", 4096)}),
    size INTEGER CHECK (size IS NULL OR size >= 0),
    text TEXT,
    left_out TEXT CHECK ({bounded.format("left_out", 512)}),
    artifact_id TEXT CHECK ({bounded.format("artifact_id", 128)}),
    revision INTEGER CHECK (revision IS NULL OR revision >= 1),
    CHECK (path IS NOT NULL OR (size IS NULL AND text IS NULL AND left_out IS NULL)),
    CHECK ((artifact_id IS NULL) = (revision IS NULL)),
    CHECK (path IS NULL OR size IS NOT NULL),
    CHECK (text IS NULL OR left_out IS NULL),
    FOREIGN KEY (project, message_id) REFERENCES archive_messages (project, message_id)
) STRICT""",
        "CREATE INDEX archive_docs_message ON archive_docs (project, message_id)",
        """CREATE TRIGGER archive_docs_no_update BEFORE UPDATE ON archive_docs
BEGIN SELECT RAISE(ABORT, 'archived texts are written once'); END""",
        """CREATE TRIGGER archive_docs_no_delete BEFORE DELETE ON archive_docs
BEGIN SELECT RAISE(ABORT, 'archived texts are kept'); END""",
        "CREATE VIRTUAL TABLE archive_words USING fts5(body, content='', "
        "tokenize='unicode61 remove_diacritics 2')",
        "CREATE VIRTUAL TABLE archive_trigrams USING fts5(body, content='', tokenize='trigram')",
    ):
        schema.execute(statement)


def _research_state(schema: Schema) -> None:
    """Version 47: the research state's link kind and flag cause (docs/specs/context-management.md,
    CM-10): a decision's ``reopen_on`` links name what its reopen condition watches, and a
    change to what they name flags it (``reopen_condition``); an agent's reopening of an
    obstructed issue is a work record (``reopened``). The tables are made again
    with the wider CHECK, their rows, indexes and triggers as they were. Issue kinds,
    caveats, reopen conditions and session scopes are entry fields and need no column: an
    issue that names no kind reads as a question."""
    for table, old, new in (
        ("ledger_links", "'affects','concerns'))", "'affects','concerns','reopen_on'))"),
        ("ledger_flags", "'spread'))", "'spread', 'reopen_condition'))"),
        ("ledger_work", "'produced', 'assessment'))", "'produced', 'assessment', 'reopened'))"),
    ):
        (create,) = schema.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if create.count(old) != 1:
            raise ControlError(INTERNAL, f"the {table} table is not as the version before left it")
        rebuild(schema, table, create.replace(old, new, 1))


def _archived_answers(schema: Schema) -> None:
    """Version 48: what an archived answer came from, beside its native payload, model
    and gateway version: the alias it was asked on, which with the model decides whether
    its native items may be sent back (docs/specs/context-management.md, CM-42), and the
    SHA-256 of what the model was given, in hexadecimal. Answers archived before have
    neither."""
    for statement in (
        "ALTER TABLE archive_messages ADD COLUMN alias TEXT "
        "CHECK (alias IS NULL OR length(CAST(alias AS BLOB)) BETWEEN 1 AND 256)",
        "ALTER TABLE archive_messages ADD COLUMN prefix TEXT CHECK (prefix IS NULL OR "
        "(length(prefix) = 64 AND prefix NOT GLOB '*[^0-9a-f]*'))",
    ):
        schema.execute(statement)


def _adapter_state(schema: Schema) -> None:
    """Version 49: what the adapter that made an archived answer keeps of its call, as a
    JSON object of at most 64 KiB, so that a view rebuilt from the archive has it again
    (docs/specs/context-management.md, CM-38). Answers archived before have none."""
    schema.execute(
        "ALTER TABLE archive_messages ADD COLUMN adapter_state TEXT CHECK (adapter_state IS "
        "NULL OR (json_valid(adapter_state) AND json_type(adapter_state) = 'object' AND "
        "length(CAST(adapter_state AS BLOB)) <= 65536))"
    )


def _read_register(schema: Schema) -> None:
    """Version 50: the read register (docs/specs/context-management.md, CM-12): each
    source a tool result read, beside the result's archived message, with its kind, its
    key in that kind, how a reader knows it, the read's extent and what it covered. A row
    is written once, in the change that archives its message, and never changed."""
    bounded = "length(CAST({0} AS BLOB)) BETWEEN 1 AND {1}"
    for statement in (
        f"""CREATE TABLE source_reads (
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    message_id TEXT NOT NULL,
    tool TEXT NOT NULL CHECK ({bounded.format("tool", 256)}),
    kind TEXT NOT NULL CHECK (kind IN ('pdf', 'file', 'eprint', 'web', 'nist', 'paper',
        'connector')),
    source TEXT NOT NULL CHECK ({bounded.format("source", 1024)}),
    label TEXT CHECK (label IS NULL OR {bounded.format("label", 4096)}),
    extent TEXT NOT NULL CHECK (extent IN ('whole', 'partial', 'abstract_only',
        'preview_only', 'unknown')),
    detail TEXT NOT NULL CHECK (json_valid(detail) AND json_type(detail) = 'object'
        AND length(detail) <= 16384),
    FOREIGN KEY (project, message_id) REFERENCES archive_messages (project, message_id)
) STRICT""",
        "CREATE INDEX source_reads_source ON source_reads (project, kind, source)",
        "CREATE INDEX source_reads_session ON source_reads (project, session)",
        """CREATE TRIGGER source_reads_no_update BEFORE UPDATE ON source_reads
BEGIN SELECT RAISE(ABORT, 'reads are written once'); END""",
        """CREATE TRIGGER source_reads_no_delete BEFORE DELETE ON source_reads
BEGIN SELECT RAISE(ABORT, 'reads are kept'); END""",
    ):
        schema.execute(statement)


def _provenance(schema: Schema) -> None:
    """Version 51: the basis and anchors of each ledger revision and work record
    (docs/specs/context-management.md, CM-9): where it comes from, ``user``,
    ``user_quoted``, ``observed`` or ``interpreted``, and the places it rests on, as a
    JSON array. Written with the row; the rows written before hold neither."""
    basis = "basis IS NULL OR basis IN ('user', 'user_quoted', 'observed', 'interpreted')"
    anchors = (
        "anchors IS NULL OR (json_valid(anchors) AND json_type(anchors) = 'array' "
        "AND length(CAST(anchors AS BLOB)) <= 65536)"
    )
    for table in ("ledger_revisions", "ledger_work"):
        schema.execute(f"ALTER TABLE {table} ADD COLUMN basis TEXT CHECK ({basis})")
        schema.execute(f"ALTER TABLE {table} ADD COLUMN anchors TEXT CHECK ({anchors})")


def _fold_events(schema: Schema) -> None:
    """Version 52: a run's folds are run events of kind ``fold``
    (docs/specs/context-management.md, CM-46). The table is made again with the wider
    CHECK, its rows, indexes and triggers as they were."""
    (create,) = schema.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='run_events'"
    ).fetchone()
    old = "'waiting', 'stopped'))"
    if create.count(old) != 1:
        raise ControlError(INTERNAL, "the run_events table is not as version 51 left it")
    rebuild(schema, "run_events", create.replace(old, "'waiting', 'stopped', 'fold'))", 1))


def _cost_sources(schema: Schema) -> None:
    """Version 53: where each reviewer, job watcher and import pass call's cost came from
    (run-service spec RS-6a; lab 43, B2), as run records' ``usage`` says it: ``gateway``,
    reported by it, or ``model_info``, priced from the gateway's model information. A
    call without a cost, or made before this version, has none."""
    source = "cost_source IS NULL OR cost_source IN ('gateway', 'model_info')"
    for table in ("review_calls", "job_watcher_calls", "skill_metadata_calls"):
        schema.execute(f"ALTER TABLE {table} ADD COLUMN cost_source TEXT CHECK ({source})")


def _fold_proposals(schema: Schema) -> None:
    """Version 54: the research-state updates folds propose, kept apart from the ledger
    until the main agent or the user accepts or dismisses each
    (docs/specs/context-management.md, CM-15): the typed operation as the fold's call gave
    it, its anchors as resolved, the fold and run that proposed it, and how it was decided.
    A proposal is never deleted; it waits, is accepted with the result the ledger wrote,
    in the same change, or is dismissed with a reason."""
    json_text = "{0} IS NULL OR (json_valid({0}) AND length(CAST({0} AS BLOB)) <= {1})"
    for statement in (
        f"""CREATE TABLE fold_proposals (
    proposal_id TEXT PRIMARY KEY CHECK (length(proposal_id) = 29 AND proposal_id GLOB 'prop-*'
        AND substr(proposal_id, 6) NOT GLOB '*[^0-9a-f]*'),
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    fold TEXT NOT NULL CHECK ({_ID.format("fold")}),
    run TEXT NOT NULL CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    created TEXT NOT NULL CHECK (length(created) BETWEEN 1 AND 64),
    body TEXT NOT NULL CHECK (json_valid(body) AND json_type(body) = 'object'
        AND length(CAST(body AS BLOB)) <= 65536),
    anchors TEXT NOT NULL CHECK (json_valid(anchors) AND json_type(anchors) = 'array'
        AND length(CAST(anchors AS BLOB)) <= 65536),
    state TEXT NOT NULL CHECK (state IN ('waiting', 'accepted', 'dismissed')),
    result TEXT CHECK ({json_text.format("result", 65536)}),
    reason TEXT CHECK (reason IS NULL OR length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
    decided_by TEXT CHECK ({json_text.format("decided_by", 4096)}),
    decided_at TEXT CHECK (decided_at IS NULL OR length(decided_at) BETWEEN 1 AND 64),
    CHECK ((state = 'waiting') = (decided_at IS NULL)),
    CHECK ((state = 'accepted') = (result IS NOT NULL)),
    CHECK ((state = 'dismissed') = (reason IS NOT NULL)),
    FOREIGN KEY (project, session) REFERENCES sessions (project_id, session_id)
) STRICT""",
        "CREATE INDEX fold_proposals_waiting ON fold_proposals (project, session, state, created)",
        """CREATE TRIGGER fold_proposals_kept BEFORE DELETE ON fold_proposals
BEGIN SELECT RAISE(ABORT, 'proposals are kept'); END""",
        """CREATE TRIGGER fold_proposals_decided BEFORE UPDATE ON fold_proposals
WHEN OLD.state != 'waiting' OR NEW.body IS NOT OLD.body OR NEW.anchors IS NOT OLD.anchors
OR NEW.fold IS NOT OLD.fold OR NEW.project IS NOT OLD.project OR NEW.session IS NOT OLD.session
BEGIN SELECT RAISE(ABORT, 'a decided proposal is not changed'); END""",
    ):
        schema.execute(statement)


def _document_pins(schema: Schema) -> None:
    """Version 55: each document revision's references to recorded files with their pins
    (docs/specs/figures.md, FG-7 to FG-10), a JSON array kept with the revision, which
    its no-update trigger already keeps as written. Revisions saved before hold none."""
    schema.execute(
        "ALTER TABLE revisions ADD COLUMN pins TEXT CHECK (pins IS NULL OR (json_valid(pins) "
        "AND json_type(pins) = 'array' AND length(CAST(pins AS BLOB)) <= 1048576))"
    )


def _statement_and_chat_pins(schema: Schema) -> None:
    """Version 56: the pins of the references to recorded files in ledger statements and
    in chat (docs/specs/figures.md, FG-7, FG-8): a JSON array on each ledger revision,
    and, for a user's message or an agent's answer, a row of ``chat_pins`` written with
    it and never changed. Rows written before hold none."""
    pins = (
        "json_valid(pins) AND json_type(pins) = 'array' AND length(CAST(pins AS BLOB)) <= 1048576"
    )
    for statement in (
        f"ALTER TABLE ledger_revisions ADD COLUMN pins TEXT CHECK (pins IS NULL OR ({pins}))",
        f"""CREATE TABLE chat_pins (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('message', 'event')),
    key TEXT NOT NULL CHECK (length(CAST(key AS BLOB)) BETWEEN 1 AND 512),
    pins TEXT NOT NULL CHECK ({pins}),
    PRIMARY KEY (project_id, session_id, source, key),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id)
) STRICT""",
        """CREATE TRIGGER chat_pins_no_update BEFORE UPDATE ON chat_pins
BEGIN SELECT RAISE(ABORT, 'chat pins are written once'); END""",
        """CREATE TRIGGER chat_pins_no_delete BEFORE DELETE ON chat_pins
BEGIN SELECT RAISE(ABORT, 'chat pins are kept'); END""",
    ):
        schema.execute(statement)


def _figure_checks(schema: Schema) -> None:
    """Version 57: the figure checks (docs/specs/figures.md, FG-19 to FG-21). A save of
    a document or a ledger entry records it in ``figure_sources`` until its figures are
    checked, and when a check its checker could not finish is tried again (``tries``,
    ``after``); ``figure_checks`` holds the result of each figure of a text's current
    revision, by the block (or ``statement``) and the figure's place there (``chart:<n>``
    or ``file:<n>``), replaced when a later revision is checked."""
    subject = "subject_type TEXT NOT NULL CHECK (subject_type IN ('document', 'statement'))"
    for statement in (
        f"""CREATE TABLE figure_sources (
    {subject},
    subject_id TEXT NOT NULL,
    project TEXT NOT NULL,
    seq INTEGER NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0,
    after REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (subject_type, subject_id)
) STRICT""",
        f"""CREATE TABLE figure_checks (
    {subject},
    subject_id TEXT NOT NULL,
    project TEXT NOT NULL,
    revision INTEGER NOT NULL,
    place TEXT NOT NULL,
    figure TEXT NOT NULL CHECK (figure GLOB 'chart:[0-9]*' OR figure GLOB 'file:[0-9]*'),
    path TEXT,
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected', 'unavailable')),
    result TEXT NOT NULL CHECK (json_valid(result) AND length(CAST(result AS BLOB)) <= 65536),
    checked TEXT NOT NULL,
    PRIMARY KEY (subject_type, subject_id, place, figure)
) STRICT""",
    ):
        schema.execute(statement)


def _tool_journal(schema: Schema) -> None:
    """Version 58: tool intents before dispatch and full tool records saved atomically
    with their archive messages (research-continuity.md, RC-10, RC-14). Older messages
    have no tool record here; their finished run records remain the original source."""
    for statement in (
        """CREATE TABLE tool_intents (
    seq INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    run TEXT NOT NULL CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    operation_id TEXT NOT NULL CHECK (length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 128),
    body_bytes INTEGER NOT NULL CHECK (body_bytes =
        length(CAST(call_id AS BLOB)) + length(CAST(tool AS BLOB)) +
        length(CAST(arguments AS BLOB)) + length(CAST(context AS BLOB)) +
        length(CAST(created AS BLOB))),
    call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    context TEXT NOT NULL CHECK (json_valid(context) AND json_type(context) = 'object'),
    created TEXT NOT NULL,
    UNIQUE (project, session, run, operation_id),
    FOREIGN KEY (project, session) REFERENCES sessions (project_id, session_id)
) STRICT""",
        "CREATE INDEX tool_intents_run ON tool_intents (project, session, run, seq)",
        """CREATE TABLE archive_tool_records (
    project TEXT NOT NULL,
    message_id TEXT NOT NULL,
    session TEXT NOT NULL,
    run TEXT NOT NULL,
    operation_id TEXT,
    record_bytes INTEGER NOT NULL CHECK (record_bytes = length(CAST(record AS BLOB))),
    record TEXT NOT NULL CHECK (json_valid(record) AND json_type(record) = 'object'),
    PRIMARY KEY (project, message_id),
    UNIQUE (project, session, run, operation_id),
    FOREIGN KEY (project, message_id) REFERENCES archive_messages (project, message_id),
    FOREIGN KEY (project, session, run, operation_id)
        REFERENCES tool_intents (project, session, run, operation_id)
) STRICT""",
    ):
        schema.execute(statement)
    for table in ("tool_intents", "archive_tool_records"):
        for action in ("UPDATE", "DELETE"):
            schema.execute(
                f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'tool evidence is written once'); END"
            )


def _run_inputs(schema: Schema) -> None:
    """Version 59: mandatory immutable starting inputs before a stored run's calls."""
    schema.execute("""CREATE TABLE run_inputs (
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    run TEXT NOT NULL CHECK (length(CAST(run AS BLOB)) BETWEEN 1 AND 256),
    snapshot TEXT NOT NULL CHECK (json_valid(snapshot) AND json_type(snapshot) = 'object'
        AND length(CAST(snapshot AS BLOB)) <= 1048576),
    PRIMARY KEY (project, session, run),
    FOREIGN KEY (project, session) REFERENCES sessions (project_id, session_id)
) STRICT""")
    for action in ("UPDATE", "DELETE"):
        schema.execute(
            f"CREATE TRIGGER run_inputs_no_{action.lower()} BEFORE {action} ON run_inputs "
            "BEGIN SELECT RAISE(ABORT, 'starting inputs are written once'); END"
        )


def _model_usage(schema: Schema) -> None:
    """Version 60: observed main/delegated usage committed with each assistant row."""
    schema.execute("""CREATE TABLE archive_model_usage (
    project TEXT NOT NULL,
    message_id TEXT NOT NULL,
    session TEXT NOT NULL,
    run TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 999999999),
    role TEXT NOT NULL CHECK (role IN ('main', 'delegated')),
    observed REAL NOT NULL CHECK (observed >= 0),
    usage TEXT NOT NULL CHECK (json_valid(usage) AND json_type(usage) = 'object'
        AND length(CAST(usage AS BLOB)) <= 16384),
    PRIMARY KEY (project, message_id),
    UNIQUE (project, session, run, ordinal),
    FOREIGN KEY (project, message_id) REFERENCES archive_messages (project, message_id)
) STRICT""")
    schema.execute(
        "CREATE INDEX archive_model_usage_window ON archive_model_usage (project,observed)"
    )
    for action in ("UPDATE", "DELETE"):
        schema.execute(
            f"CREATE TRIGGER archive_model_usage_no_{action.lower()} "
            f"BEFORE {action} ON archive_model_usage "
            "BEGIN SELECT RAISE(ABORT, 'model usage is written once'); END"
        )


def _run_collections(schema: Schema) -> None:
    """Version 61: paged run history and immutable successful-response receipts."""
    schema.execute("""CREATE TABLE run_collections (
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    run TEXT NOT NULL,
    collection TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    item TEXT NOT NULL CHECK (json_valid(item)),
    PRIMARY KEY (project,session,run,collection,position)
) STRICT""")
    schema.execute("""CREATE TABLE run_members (
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    run TEXT NOT NULL,
    collection TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (project,session,run,collection,value)
) STRICT""")
    schema.execute("""CREATE TABLE response_receipts (
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    run TEXT NOT NULL,
    source TEXT NOT NULL,
    notice TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (project,session,run,source,notice),
    FOREIGN KEY (project,message_id) REFERENCES archive_messages (project,message_id)
) STRICT""")
    for action in ("UPDATE", "DELETE"):
        schema.execute(
            f"CREATE TRIGGER response_receipts_no_{action.lower()} BEFORE {action} "
            "ON response_receipts BEGIN SELECT RAISE(ABORT, 'response receipts are immutable'); END"
        )


def _research_work(schema: Schema) -> None:
    """Version 62: durable responsibilities and explicit fenced Resume requests."""
    from sandbox_science.orchestrator.continuity.schema import install

    install(schema)


def _immutable_run_collections(schema: Schema) -> None:
    """Version 63: final history references resolve immutable collection rows."""
    for action in ("UPDATE", "DELETE"):
        schema.execute(
            f"CREATE TRIGGER run_collections_no_{action.lower()} BEFORE {action} "
            "ON run_collections BEGIN SELECT RAISE(ABORT, 'run collections are immutable'); END"
        )


def _recovery_dispatch(schema: Schema) -> None:
    """Version 64: durable child inputs and paid-dispatch uncertainty."""
    from sandbox_science.orchestrator.continuity.recovery_schema import install

    install(schema)


def _child_settlements(schema: Schema) -> None:
    """Version65: replayable typed delegated outcomes."""
    from sandbox_science.orchestrator.continuity.child_settlement import install

    install(schema)


def _user_uploads(schema: Schema) -> None:
    """Version 66: immutable original user uploads."""
    from sandbox_science.orchestrator.uploads import install

    install(schema)


def _larger_user_uploads(schema: Schema) -> None:
    """Version 67: 100 MiB immutable original uploads."""
    from sandbox_science.orchestrator.uploads import expand_limit

    expand_limit(schema)


def _artifact_blobs(schema: Schema) -> None:
    """Version 68: external immutable content and durable bounded transfer admission."""
    schema.execute("""CREATE TABLE artifact_revision_blobs (
        artifact TEXT NOT NULL, number INTEGER NOT NULL, project TEXT NOT NULL,
        sha256 TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0),
        PRIMARY KEY(artifact,number),
        FOREIGN KEY(artifact,number) REFERENCES artifact_revisions(artifact,number)) STRICT""")
    for action in ("UPDATE", "DELETE"):
        schema.execute(
            f"CREATE TRIGGER artifact_revision_blobs_no_{action.lower()} BEFORE {action} "
            "ON artifact_revision_blobs BEGIN "
            "SELECT RAISE(ABORT,'artifact blobs are immutable'); END"
        )
    schema.execute("""CREATE TABLE artifact_transfers (
        identity TEXT PRIMARY KEY, project TEXT NOT NULL,
        bytes INTEGER NOT NULL CHECK(bytes>=0),sha256 TEXT NOT NULL) STRICT""")
    schema.execute("DROP TRIGGER artifact_usage_revision")
    schema.execute(
        "CREATE TRIGGER artifact_usage_revision AFTER INSERT ON artifact_revisions BEGIN "  # noqa: S608 - fixed SQL
        f"{_USAGE_ROWS} UPDATE artifact_usage SET revisions=revisions+1,"
        "bytes=bytes+CASE WHEN json_extract(NEW.record,'$.content_retained') "
        "THEN NEW.size ELSE 0 END,metadata=metadata+length(CAST(NEW.record AS BLOB)) "
        f"{_USAGE_OF}; END"
    )


def _agent_messages(schema: Schema) -> None:
    """Durable messages between directly related runs of one research tree."""
    for statement in (
        """CREATE TABLE agent_messages (
    message_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    sender_run TEXT NOT NULL,
    sender_delegation TEXT,
    recipient_delegation TEXT,
    body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) BETWEEN 1 AND 4096),
    created_at TEXT NOT NULL,
    delivered_run TEXT,
    UNIQUE (project_id, session_id, operation_id),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id),
    FOREIGN KEY (sender_delegation) REFERENCES delegations (delegation_id),
    FOREIGN KEY (recipient_delegation) REFERENCES delegations (delegation_id)
) STRICT""",
        "CREATE INDEX agent_messages_pending ON agent_messages "
        "(project_id, session_id, recipient_delegation, delivered_run)",
        """CREATE TRIGGER agent_messages_kept BEFORE UPDATE ON agent_messages
WHEN NEW.message_id IS NOT OLD.message_id OR NEW.project_id IS NOT OLD.project_id
OR NEW.session_id IS NOT OLD.session_id OR NEW.operation_id IS NOT OLD.operation_id
OR NEW.sender_run IS NOT OLD.sender_run
OR NEW.sender_delegation IS NOT OLD.sender_delegation
OR NEW.recipient_delegation IS NOT OLD.recipient_delegation
OR NEW.body IS NOT OLD.body OR NEW.created_at IS NOT OLD.created_at
OR OLD.delivered_run IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'an agent message is kept'); END""",
        """CREATE TRIGGER agent_messages_no_delete BEFORE DELETE ON agent_messages
BEGIN SELECT RAISE(ABORT, 'agent messages are kept'); END""",
    ):
        schema.execute(statement)


def _kernel_cell_lookup(schema: Schema) -> None:
    """Version 70: find cell provenance by operation across long research sessions."""
    schema.execute(
        "CREATE INDEX research_kernel_cell_identity ON research_observations "
        "(project,session,kind,identity) WHERE kind='kernel_cell'"
    )


def _context_auxiliary_window(schema: Schema) -> None:
    """Version 71: bound the inspector's latest auxiliary-intent snapshot."""
    schema.execute(
        "CREATE INDEX research_model_context_window ON research_model_calls "
        "(project,session,run,created DESC,identity DESC) "
        "WHERE kind NOT IN ('main','delegated')"
    )


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
    _ledger,
    _ledger_links,
    _statement_math,
    _artifact_usage,
    _file_settings,
    _ledger_evidence,
    _ledger_review_index,
    _memory,
    _ledger_flags,
    _frontier_categories,
    _frontier_changes,
    _delegations,
    _delegation_results,
    _evidence_targets,
    _citation_checks,
    _delegation_reads,
    _delegation_limits,
    _ledger_work,
    _skills,
    _cost_limits,
    _user_questions,
    _delegation_files,
    _delegation_tasks,
    _role_costs,
    _skill_settings,
    _builtin_skills,
    _skill_vectors,
    _skill_metadata,
    _skill_overrides,
    _archive,
    _research_state,
    _archived_answers,
    _adapter_state,
    _read_register,
    _provenance,
    _fold_events,
    _cost_sources,
    _fold_proposals,
    _document_pins,
    _statement_and_chat_pins,
    _figure_checks,
    _tool_journal,
    _run_inputs,
    _model_usage,
    _run_collections,
    _research_work,
    _immutable_run_collections,
    _recovery_dispatch,
    _child_settlements,
    _user_uploads,
    _larger_user_uploads,
    _artifact_blobs,
    _agent_messages,
    _kernel_cell_lookup,
    _context_auxiliary_window,
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
