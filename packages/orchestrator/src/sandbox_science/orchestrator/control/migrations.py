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

import re
import sqlite3
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Final

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


MIGRATIONS: Final[tuple[Migration, ...]] = (
    _baseline,
    _projects,
    _comment_checks,
    _response_checks,
    _math_checks,
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
