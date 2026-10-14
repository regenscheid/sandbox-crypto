# SPDX-License-Identifier: MIT
"""The worker store's schema: one ordered list of migrations and one version.

The version is SQLite's ``user_version``, and a worker store carries
``APPLICATION_ID`` as its ``application_id``. Version 1 is the baseline,
``baseline.sql``, and each step in ``MIGRATIONS`` takes the store one version
further. ``open_store`` refuses a file this worker cannot use before anything could
change it, through a read-only connection: a store in a newer version, an unversioned
one from before the baseline (decision 6), one holding the executions of a removed
feature, and a file that is not a worker store. It then opens the writer and runs the
steps the store lacks, in one ``BEGIN IMMEDIATE`` transaction that reads the version
again, so a step runs once however many workers open the store together, and a step
that fails leaves the store as it was.

To discard a refused state directory, release or recycle its sandboxes first: the
worker keeps their daemons' endpoints there (docs/WORKER.md).

To change the schema, append a step to ``MIGRATIONS``. Never edit a step that has
shipped, or ``baseline.sql``: stores made with them keep them. A step gets the
connection inside the transaction, and must not commit or roll back. A step that adds
constraints to a small table rebuilds it (``rebuild``); a row the new definition
refuses leaves the store at its version with a message that names the table. A large
table, such as one of events or blobs, is not copied: its writes are checked by a
trigger instead.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Final

from sandbox_science.contracts import ErrorKind

from .errors import WorkerError

BASELINE: Final = 1
"""The version ``baseline.sql`` creates."""

APPLICATION_ID: Final = 0x53537773
"""The ``application_id`` of a worker store ("SSws")."""

type Migration = Callable[[sqlite3.Connection], None]


class RefusedRowError(Exception):
    """A row of the store that a migration step's new definition refuses."""


def rebuild(db: sqlite3.Connection, table: str, create: str) -> None:
    """Make ``table`` again as ``create`` defines it, with its rows and rowids, its indexes
    and triggers, the other tables' triggers and the views that name it: SQLite cannot add
    a constraint to a table. Only for small tables: the rows are copied, one at a time, so
    that a row the new definition refuses is named in the ``RefusedRowError``."""
    if not create.startswith(f"CREATE TABLE {table} ("):
        raise WorkerError(ErrorKind.INTERNAL, f"a rebuild of {table} needs its CREATE TABLE")
    attached = [
        str(sql)
        for (sql,) in db.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger')"
            " AND sql IS NOT NULL ORDER BY rowid",
            (table,),
        )
    ]
    named = re.compile(rf"\b{re.escape(table)}\b")
    dependents = [
        (str(kind), str(name), str(sql))
        for kind, name, sql in db.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger','view')"
            " AND tbl_name<>? AND sql IS NOT NULL ORDER BY rowid",
            (table,),
        )
        if named.search(str(sql))
    ]
    for kind, name, _ in dependents:
        db.execute(f'DROP {kind.upper()} "{name}"')
    info = list(db.execute(f"PRAGMA table_info({table})"))
    columns = ", ".join(str(row[1]) for row in info)
    key = [str(row[1]) for row in sorted(info, key=lambda row: int(row[5])) if int(row[5])]
    db.execute(create.replace(f"CREATE TABLE {table} (", f"CREATE TABLE {table}__new (", 1))
    for (rowid,) in db.execute(f"SELECT rowid FROM {table} ORDER BY rowid").fetchall():  # noqa: S608
        try:
            db.execute(
                f"INSERT INTO {table}__new (rowid, {columns}) "  # noqa: S608 - the store's own names
                f"SELECT rowid, {columns} FROM {table} WHERE rowid=?",
                (rowid,),
            )
        except sqlite3.IntegrityError as error:
            values = db.execute(
                f"SELECT {', '.join(key) or 'rowid'} FROM {table} WHERE rowid=?",  # noqa: S608
                (rowid,),
            ).fetchone()
            named_key = ", ".join(
                f"{column}={value!r}"
                for column, value in zip(key or ["rowid"], values, strict=True)
            )
            reason = str(error).replace(f"{table}__new", table)
            raise RefusedRowError(
                f"the row of {table} with {named_key} fails its check ({reason})"
            ) from None
    db.execute(f"DROP TABLE {table}")
    db.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
    for sql in attached:
        db.execute(sql)
    for _, _, sql in dependents:
        db.execute(sql)


def _identifier(column: str, *, nul: bool = False) -> str:
    """SQL for an identifier like ``[A-Za-z0-9][A-Za-z0-9._-]{0,127}``; with ``nul``, also
    one without a NUL, which ``length`` and ``GLOB`` stop at."""
    check = (
        f"length({column}) BETWEEN 1 AND 128 AND {column} NOT GLOB '*[^A-Za-z0-9._-]*'"
        f" AND {column} NOT GLOB '[._-]*'"
    )
    return f"({check} AND instr({column}, char(0)) = 0)" if nul else f"({check})"


def _prefixed(column: str, prefix: str, *, nul: bool = False) -> str:
    """SQL for an ID like ``pub-`` and 24 lower-case hex digits; with ``nul``, also one
    without a NUL."""
    check = (
        f"length({column}) = 28 AND {column} GLOB '{prefix}-*'"
        f" AND substr({column}, 5) NOT GLOB '*[^0-9a-f]*'"
    )
    return f"({check} AND instr({column}, char(0)) = 0)" if nul else f"({check})"


_COUNT: Final = "BETWEEN 0 AND 4611686018427387903"
"""A generation or revision: from 0 below 2**62."""


def _admission_tables(*, nul: bool) -> tuple[tuple[str, str], ...]:
    """The capture leases', package gates', recovery marks' and provisioning steps'
    definitions with their checks; ``nul`` also refuses a NUL in an identifier."""
    return (
        (
            "artifact_capture_leases",
            f"""CREATE TABLE artifact_capture_leases (
    publication_id TEXT PRIMARY KEY CHECK {_prefixed("publication_id", "pub", nul=nul)},
    environment_id TEXT NOT NULL CHECK {_identifier("environment_id", nul=nul)},
    incarnation TEXT NOT NULL
        CHECK (incarnation = '' OR {_prefixed("incarnation", "inc", nul=nul)}),
    capture_id TEXT NOT NULL
        CHECK (capture_id = '' OR {_prefixed("capture_id", "cap", nul=nul)}),
    phase TEXT NOT NULL CHECK (phase IN ('active', 'held')),
    guest_clean INTEGER NOT NULL CHECK (guest_clean IN (0, 1)),
    sandbox TEXT NOT NULL CHECK (sandbox = '' OR {_identifier("sandbox", nul=nul)}),
    sandbox_id TEXT NOT NULL CHECK (sandbox_id = '' OR {_identifier("sandbox_id", nul=nul)}),
    CHECK ((incarnation = '') = (capture_id = '')),
    CHECK ((sandbox = '') = (sandbox_id = ''))
) STRICT""",
        ),
        (
            "package_execution_gates",
            f"""CREATE TABLE package_execution_gates (
    environment_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL CHECK (generation {_COUNT}),
    mode TEXT NOT NULL CHECK (mode IN ('ready', 'hold', 'needs_recovery')),
    operation TEXT NOT NULL CHECK (operation = '' OR {_identifier("operation", nul=nul)}),
    revision INTEGER CHECK (revision IS NULL OR revision {_COUNT}),
    binding TEXT NOT NULL DEFAULT '' CHECK (length(CAST(binding AS BLOB)) <= 4096),
    CHECK (mode != 'ready' OR operation = ''),
    CHECK (mode != 'needs_recovery' OR operation != '')
) STRICT""",
        ),
        (
            "package_status",
            f"""CREATE TABLE package_status (
    environment_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status = 'needs_recovery'),
    provision_id TEXT NOT NULL CHECK {_identifier("provision_id", nul=nul)},
    revision INTEGER CHECK (revision IS NULL OR revision {_COUNT})
) STRICT""",
        ),
        (
            "provision_steps",
            """CREATE TABLE provision_steps (
    provision_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    request_digest TEXT NOT NULL
        CHECK (length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (provision_id, operation_id)
) STRICT""",
        ),
    )


def _admission_checks(db: sqlite3.Connection) -> None:
    """Version 2: capture leases, package gates, recovery marks and provisioning steps
    check their values when they are written, so reads take them as they are."""
    for table, create in _admission_tables(nul=False):
        rebuild(db, table, create)


def _nul_checks_and_clears(db: sqlite3.Connection) -> None:
    """Version 3: those identifiers refuse a NUL too, and the capture leases an operator
    clears are recorded (``captures --clear``)."""
    for table, create in _admission_tables(nul=True):
        rebuild(db, table, create)
    db.execute(
        """CREATE TABLE capture_clears (
    cleared_at TEXT NOT NULL,
    publication_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    incarnation TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    sandbox TEXT NOT NULL,
    sandbox_id TEXT NOT NULL
) STRICT"""
    )


def _run_kernels(db: sqlite3.Connection) -> None:
    """Version 4: the kernel generations of delegated runs, which have kernels of their
    own beside their session's (worker protocol 4)."""
    db.execute(
        f"""CREATE TABLE run_kernels (
    environment_id TEXT NOT NULL CHECK {_identifier("environment_id", nul=True)},
    session_id TEXT NOT NULL CHECK {_identifier("session_id", nul=True)},
    run TEXT NOT NULL CHECK {_identifier("run", nul=True)},
    language TEXT NOT NULL CHECK {_identifier("language", nul=True)},
    incarnation TEXT NOT NULL CHECK {_prefixed("incarnation", "inc", nul=True)},
    base INTEGER NOT NULL CHECK (base {_COUNT}),
    last INTEGER NOT NULL CHECK (last {_COUNT} AND base < last),
    PRIMARY KEY (environment_id, session_id, run, language)
) STRICT"""
    )


MIGRATIONS: list[Migration] = [_admission_checks, _nul_checks_and_clears, _run_kernels]
"""Steps after the baseline: the one at index ``i`` takes a store to ``BASELINE + i + 1``."""

_STATE: Final = (
    "SELECT (SELECT user_version FROM pragma_user_version),"
    " (SELECT application_id FROM pragma_application_id),"
    " (SELECT count(*) FROM sqlite_master)"
)
"""The version, the application ID and whether the file holds anything, in one statement
and so one snapshot: read apart, another worker's first commit could land in between."""


def latest() -> int:
    """The version this worker writes."""
    return BASELINE + len(MIGRATIONS)


def _statements(script: str) -> list[str]:
    found: list[str] = []
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            found.append(statement)
            statement = ""
    if statement.strip():
        raise WorkerError(ErrorKind.INTERNAL, "the worker store's baseline schema is incomplete")
    return found


def _baseline(db: sqlite3.Connection) -> None:
    """Make the baseline's tables and indexes, and the output observations' identity
    (output_observations.py)."""
    script = resources.files(__package__).joinpath("baseline.sql").read_text(encoding="utf-8")
    for statement in _statements(script):
        db.execute(statement)
    db.execute(
        "INSERT INTO output_observation_meta VALUES(1,?,?)",
        (secrets.token_hex(32).encode(), datetime.now(UTC).isoformat().encode()),
    )


def _refuse(path: Path, reason: str) -> WorkerError:
    return WorkerError(
        ErrorKind.UNAVAILABLE,
        f"{path} {reason}; to discard the state directory, release or recycle its "
        "sandboxes first, or use the worker that made it",
    )


def _check(path: Path, state: tuple[int, int, int]) -> int:
    """The version of a store this worker can bring to ``latest()``; a refusal otherwise."""
    version, application, objects = state
    if version == 0 and application == 0:
        if objects:  # an unversioned worker store from before version 1, or another file
            raise _refuse(path, "is not a worker store of version 1 or later")
        return 0
    if application != APPLICATION_ID or version < BASELINE:
        raise _refuse(path, "is not a worker store")
    if version > latest():
        raise _refuse(path, f"is in version {version}, newer than this worker's {latest()}")
    return version


def inspect(path: Path) -> None:
    """Refuse a store this worker cannot use, through a read-only connection that changes
    no file. A missing file is a new store."""
    if not path.exists():
        return
    # A read-only connection to a store in write-ahead mode makes the log and its index
    # (-wal, -shm) where there are none; a refused store has them removed again, so it
    # stays as it was.
    made = [file for file in (Path(f"{path}-wal"), Path(f"{path}-shm")) if not file.exists()]
    try:
        _inspect(path)
    except BaseException:
        for file in made:
            file.unlink(missing_ok=True)
        raise


def _inspect(path: Path) -> None:
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    except sqlite3.DatabaseError:
        raise _refuse(path, "is not a worker store") from None
    try:
        try:
            state = db.execute(_STATE).fetchone()
        except sqlite3.DatabaseError:
            raise _refuse(path, "is not a worker store") from None
        if _check(path, state) == 0:
            return
        # Delegated runs were removed; their old tables stay untouched.
        if db.execute(
            "SELECT 1 FROM executions WHERE kernel_scope IS NOT 'legacy' LIMIT 1"
        ).fetchone():
            raise _refuse(path, "holds delegated-run executions from a removed feature")
    finally:
        db.close()


def prepare(path: Path, db: sqlite3.Connection) -> None:
    """Bring the writer's store to ``latest()``: make it if it is new, and run the steps
    it lacks. Decides again inside the write transaction, from one snapshot."""
    db.execute("BEGIN IMMEDIATE")
    try:
        version = _check(path, db.execute(_STATE).fetchone())
        if version < latest():
            if version == 0:
                _baseline(db)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                version = BASELINE
            for number, step in enumerate(MIGRATIONS[version - BASELINE :], version + 1):
                try:
                    step(db)
                except RefusedRowError as refused:
                    raise WorkerError(
                        ErrorKind.UNAVAILABLE,
                        f"{path} stays at version {version}: version {number} refuses it, as "
                        f"{refused}. With the worker stopped, repair or delete that row, or use "
                        "the worker that made the store",
                    ) from None
                version = number
            db.execute(f"PRAGMA user_version={latest()}")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
