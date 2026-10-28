# SPDX-License-Identifier: MIT
"""The worker's durable checkpoint records (C2): holds, verified checkpoints and the
deletion journal, in ``checkpoints.sqlite3`` beside the worker's other state.

A **hold** keeps a suspended environment's sandbox stopped while a checkpoint Job reads
its volume: ``Environments`` refuses to start it until the hold is released or expires,
so a forgotten checkpoint never blocks research for long. A **checkpoint** row exists
only once the worker verified the uploaded bytes itself; it counts (``kept``) only once
it verified them again in the kept bucket. The **deletion journal** records every volume
or checkpoint removal an operator asked for, intent before effect, with who asked; its
rows are never changed or removed.

The operator's commands write these from their own process (``kubectl exec`` into the
worker container), beside the serving worker. The worker records each sandbox **start**
here, in the same transaction that finds no hold, and a plan places its hold in a
transaction that finds no start under way, so the two cannot interleave; the last start
also tells cleanup whether a checkpoint is older than the work in its workspace. SQLite's
own locking orders the writers; every write is a single short transaction, synchronous.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from sandbox_science.contracts.workspace_checkpoint import (
    CheckpointPlan,
    CheckpointReceipt,
    CheckpointState,
    InventoryCheckpoint,
    InventoryHold,
)

DATABASE = "checkpoints.sqlite3"
DEFAULT_KEEP = 2
"""Verified checkpoints kept per workspace; older ones become cleanup candidates."""

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS holds (
    checkpoint_id TEXT PRIMARY KEY,
    environment_id TEXT NOT NULL,
    sandbox TEXT NOT NULL,
    plan TEXT NOT NULL CHECK (length(CAST(plan AS BLOB)) BETWEEN 2 AND 262144),
    placed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    placed_by TEXT NOT NULL CHECK (length(placed_by) BETWEEN 1 AND 256),
    released_at TEXT,
    released_reason TEXT
) STRICT""",
    "CREATE UNIQUE INDEX IF NOT EXISTS holds_active ON holds(environment_id)"
    " WHERE released_at IS NULL",
    """CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY REFERENCES holds(checkpoint_id),
    environment_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sandbox TEXT NOT NULL,
    sandbox_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE CHECK (length(manifest_sha256) = 64),
    receipt TEXT NOT NULL CHECK (length(CAST(receipt AS BLOB)) BETWEEN 2 AND 262144),
    state TEXT NOT NULL CHECK (state IN ('verified', 'kept', 'deleting', 'deleted')),
    verified_at TEXT NOT NULL,
    verified_by TEXT NOT NULL,
    kept_at TEXT,
    kept_by TEXT,
    deleting_at TEXT,
    deleted_at TEXT
) STRICT""",
    "CREATE INDEX IF NOT EXISTS checkpoints_sandbox ON checkpoints(sandbox, state)",
    """CREATE TABLE IF NOT EXISTS checkpoint_chunks (
    manifest_sha256 TEXT NOT NULL REFERENCES checkpoints(manifest_sha256),
    seq INTEGER NOT NULL,
    chunk TEXT NOT NULL CHECK (length(chunk) = 64),
    PRIMARY KEY (manifest_sha256, seq)
) STRICT""",
    """CREATE TABLE IF NOT EXISTS starts (
    environment_id TEXT PRIMARY KEY,
    began_at TEXT NOT NULL,
    ended_at TEXT
) STRICT""",
    """CREATE TABLE IF NOT EXISTS deletions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 256),
    kind TEXT NOT NULL CHECK (kind IN ('volume', 'checkpoint')),
    subject TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 256),
    phase TEXT NOT NULL CHECK (phase IN ('intent', 'done', 'failed')),
    detail TEXT NOT NULL CHECK (length(CAST(detail AS BLOB)) BETWEEN 2 AND 65536)
)""",
    "CREATE TRIGGER IF NOT EXISTS deletions_kept_update BEFORE UPDATE ON deletions"
    " BEGIN SELECT RAISE(ABORT, 'the deletion journal is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS deletions_kept_delete BEFORE DELETE ON deletions"
    " BEGIN SELECT RAISE(ABORT, 'the deletion journal is append-only'); END",
)


class RecordError(ValueError):
    """A checkpoint record request the records refuse; the message says why."""


@dataclass(frozen=True, slots=True)
class Hold:
    plan: CheckpointPlan
    placed_at: datetime
    placed_by: str
    released_at: datetime | None


@dataclass(frozen=True, slots=True)
class Deletion:
    seq: int
    recorded_at: datetime
    actor: str
    kind: Literal["volume", "checkpoint"]
    subject: str
    phase: Literal["intent", "done", "failed"]
    detail: dict[str, object]


class HeldError(Exception):
    """A checkpoint holds the environment's sandbox stopped until ``until``."""

    def __init__(self, until: datetime) -> None:
        super().__init__(
            f"held stopped for a workspace checkpoint until {until:%Y-%m-%d %H:%M} UTC"
        )
        self.until = until


STALE_START = timedelta(minutes=15)
"""A start recorded as under way for longer than this was cut short (a worker restart)."""


def held_until(state_dir: Path, environment_id: str, now: datetime) -> datetime | None:
    """When the environment's checkpoint hold expires, if one holds it now. No records
    file, or one whose tables do not exist yet, means no hold; any other error raises, so
    a caller deciding a start fails closed."""
    path = state_dir / DATABASE
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path, timeout=5)) as db:
        try:
            rows = db.execute(
                "SELECT expires_at FROM holds WHERE environment_id=? AND released_at IS NULL",
                (environment_id,),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return None
            raise
    return _active(rows, now)


def _active(rows: list[tuple[object]], now: datetime) -> datetime | None:
    for (expires,) in rows:
        until = datetime.fromisoformat(str(expires))
        if until > now:
            return until
    return None


def begin_start(state_dir: Path, environment_id: str, now: datetime) -> None:
    """Before the worker starts a stopped sandbox: in one transaction, refuse if a hold
    holds it (HeldError), else record the start as under way. Any database error raises:
    the start is refused rather than run unchecked."""
    records = CheckpointRecords(state_dir)
    try:
        records.begin_start(environment_id, now)
    finally:
        records.close()


def end_start(state_dir: Path, environment_id: str, now: datetime) -> None:
    """The start under way ended, however it ended."""
    records = CheckpointRecords(state_dir)
    try:
        records.end_start(environment_id, now)
    finally:
        records.close()


class CheckpointRecords:
    """``checkpoints.sqlite3``: opened by one operator command at a time."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / DATABASE
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        for statement in _SCHEMA:
            self.db.execute(statement)
        self.path.chmod(0o600)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # Starts

    def begin_start(self, environment_id: str, now: datetime) -> None:
        with self._write() as db:
            until = _active(
                db.execute(
                    "SELECT expires_at FROM holds WHERE environment_id=? AND released_at IS NULL",
                    (environment_id,),
                ).fetchall(),
                now,
            )
            if until is not None:
                raise HeldError(until)
            db.execute(
                "INSERT INTO starts VALUES (?,?,NULL) ON CONFLICT(environment_id)"
                " DO UPDATE SET began_at=excluded.began_at, ended_at=NULL",
                (environment_id, now.isoformat()),
            )

    def end_start(self, environment_id: str, now: datetime) -> None:
        with self._write() as db:
            db.execute(
                "UPDATE starts SET ended_at=? WHERE environment_id=?",
                (now.isoformat(), environment_id),
            )

    def last_starts(self) -> dict[str, datetime]:
        """When the worker last began starting each environment's sandbox."""
        return {
            str(environment): datetime.fromisoformat(str(began))
            for environment, began in self.db.execute("SELECT environment_id, began_at FROM starts")
        }

    # Holds

    def place(self, plan: CheckpointPlan, now: datetime, actor: str) -> None:
        """Hold the plan's environment; refuses while another unexpired hold holds it, or
        while the worker is starting its sandbox."""
        with self._write() as db:
            starting = db.execute(
                "SELECT began_at FROM starts WHERE environment_id=? AND ended_at IS NULL",
                (plan.source.environment_id,),
            ).fetchone()
            if (
                starting is not None
                and datetime.fromisoformat(str(starting[0])) > now - STALE_START
            ):
                raise RecordError("the worker is starting this sandbox; try again shortly")
            for checkpoint_id, expires in db.execute(
                "SELECT checkpoint_id, expires_at FROM holds"
                " WHERE environment_id=? AND released_at IS NULL",
                (plan.source.environment_id,),
            ).fetchall():
                if datetime.fromisoformat(str(expires)) > now:
                    raise RecordError(
                        f"checkpoint {checkpoint_id} holds this environment until {expires}"
                    )
                db.execute(
                    "UPDATE holds SET released_at=?, released_reason='expired'"
                    " WHERE checkpoint_id=?",
                    (now.isoformat(), checkpoint_id),
                )
            db.execute(
                "INSERT INTO holds VALUES (?,?,?,?,?,?,?,NULL,NULL)",
                (
                    plan.checkpoint_id,
                    plan.source.environment_id,
                    plan.source.sandbox,
                    plan.model_dump_json(),
                    now.isoformat(),
                    plan.hold_expires_at.isoformat(),
                    actor,
                ),
            )

    def hold(self, checkpoint_id: str) -> Hold:
        row = self.db.execute(
            "SELECT plan, placed_at, placed_by, released_at FROM holds WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            raise RecordError(f"no checkpoint plan {checkpoint_id}")
        return Hold(
            CheckpointPlan.model_validate_json(str(row[0])),
            datetime.fromisoformat(str(row[1])),
            str(row[2]),
            None if row[3] is None else datetime.fromisoformat(str(row[3])),
        )

    def release(self, checkpoint_id: str, now: datetime, reason: str) -> bool:
        """Lift a hold; whether it was still in place."""
        with self._write() as db:
            return (
                db.execute(
                    "UPDATE holds SET released_at=?, released_reason=?"
                    " WHERE checkpoint_id=? AND released_at IS NULL",
                    (now.isoformat(), reason[:200], checkpoint_id),
                ).rowcount
                == 1
            )

    def holds(self, now: datetime) -> list[InventoryHold]:
        return [
            InventoryHold(
                checkpoint_id=str(row[0]),
                environment_id=str(row[1]),
                sandbox=str(row[2]),
                expires_at=datetime.fromisoformat(str(row[3])),
            )
            for row in self.db.execute(
                "SELECT checkpoint_id, environment_id, sandbox, expires_at FROM holds"
                " WHERE released_at IS NULL ORDER BY placed_at"
            )
            if datetime.fromisoformat(str(row[3])) > now
        ]

    # Checkpoints

    def verified(
        self,
        plan: CheckpointPlan,
        manifest: str,
        receipt: CheckpointReceipt,
        now: datetime,
        actor: str,
        chunk_names: list[str] | None = None,
    ) -> None:
        """Record a checkpoint the worker verified in the upload bucket."""
        with self._write() as db:
            found = db.execute(
                "SELECT manifest_sha256, state FROM checkpoints WHERE checkpoint_id=?",
                (plan.checkpoint_id,),
            ).fetchone()
            if found is not None:
                if found[0] != manifest:
                    raise RecordError("this checkpoint was verified under another manifest")
                return
            db.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,'verified',?,?,NULL,NULL,NULL,NULL)",
                (
                    plan.checkpoint_id,
                    plan.source.environment_id,
                    plan.source.project_id,
                    plan.source.sandbox,
                    plan.source.sandbox_id,
                    manifest,
                    receipt.model_dump_json(),
                    now.isoformat(),
                    actor,
                ),
            )
            # The chunk list outlives the manifest, so a deletion can always finish.
            db.executemany(
                "INSERT INTO checkpoint_chunks VALUES (?,?,?)",
                [(manifest, seq, name) for seq, name in enumerate(chunk_names or ())],
            )

    def kept(self, checkpoint_id: str, manifest: str, now: datetime, actor: str) -> None:
        """Count a checkpoint, once verified again in the kept bucket."""
        with self._write() as db:
            row = db.execute(
                "SELECT manifest_sha256, state FROM checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
            if row is None or row[0] != manifest:
                raise RecordError("verify the checkpoint in the upload bucket first")
            if row[1] == "kept":
                return
            if row[1] != "verified":
                raise RecordError(f"checkpoint {checkpoint_id} is {row[1]}")
            db.execute(
                "UPDATE checkpoints SET state='kept', kept_at=?, kept_by=? WHERE checkpoint_id=?",
                (now.isoformat(), actor, checkpoint_id),
            )

    def receipt(self, manifest: str) -> CheckpointReceipt:
        row = self.db.execute(
            "SELECT receipt FROM checkpoints WHERE manifest_sha256=?", (manifest,)
        ).fetchone()
        if row is None:
            raise RecordError("no checkpoint with that manifest")
        return CheckpointReceipt.model_validate_json(str(row[0]))

    def chunk_names(self, manifest: str) -> list[str]:
        """The chunks the worker read when it verified the checkpoint, in order."""
        return [
            str(row[0])
            for row in self.db.execute(
                "SELECT chunk FROM checkpoint_chunks WHERE manifest_sha256=? ORDER BY seq",
                (manifest,),
            )
        ]

    def checkpoints(self) -> list[InventoryCheckpoint]:
        result: list[InventoryCheckpoint] = []
        for row in self.db.execute(
            "SELECT checkpoint_id, environment_id, project_id, sandbox, sandbox_id,"
            " manifest_sha256, receipt, state, verified_at, kept_at FROM checkpoints"
            " ORDER BY verified_at"
        ):
            receipt = CheckpointReceipt.model_validate_json(str(row[6]))
            result.append(
                InventoryCheckpoint(
                    checkpoint_id=str(row[0]),
                    environment_id=str(row[1]),
                    project_id=str(row[2]),
                    sandbox=str(row[3]),
                    sandbox_id=str(row[4]),
                    manifest_sha256=str(row[5]),
                    archive_size=receipt.archive_size,
                    content_bytes=receipt.content_bytes,
                    capture=receipt.capture or "offline_mount",
                    skipped=len(receipt.skipped),
                    state=_state(str(row[7])),
                    created_at=receipt.created_at,
                    verified_at=datetime.fromisoformat(str(row[8])),
                    kept_at=None if row[9] is None else datetime.fromisoformat(str(row[9])),
                )
            )
        return result

    def rotation(self, sandbox: str, keep: int) -> tuple[list[str], list[str]]:
        """The kept checkpoints of a workspace, newest first: the ``keep`` that stay, and
        the older ones an operator may remove. Nothing is removed here."""
        kept = sorted(
            (c for c in self.checkpoints() if c.sandbox == sandbox and c.state == "kept"),
            key=lambda c: c.created_at,
            reverse=True,
        )
        return [c.manifest_sha256 for c in kept[:keep]], [c.manifest_sha256 for c in kept[keep:]]

    def forget(
        self, manifest: str, now: datetime, actor: str, *, volume_gone: bool
    ) -> InventoryCheckpoint:
        """Mark a checkpoint ``deleting`` before its objects go, with who asked; it stops
        counting at once. Refuses the last kept checkpoint of a workspace whose volume may
        still exist."""
        with self._write() as db:
            row = db.execute(
                "SELECT checkpoint_id, sandbox, state FROM checkpoints WHERE manifest_sha256=?",
                (manifest,),
            ).fetchone()
            if row is None:
                raise RecordError("no checkpoint with that manifest")
            checkpoint_id, sandbox, state = str(row[0]), str(row[1]), str(row[2])
            if state == "deleted":
                raise RecordError("this checkpoint is already deleted")
            others = db.execute(
                "SELECT count(*) FROM checkpoints WHERE sandbox=? AND state='kept'"
                " AND manifest_sha256 != ?",
                (sandbox, manifest),
            ).fetchone()[0]
            if state == "kept" and not others and not volume_gone:
                raise RecordError(
                    "this is the last verified checkpoint of a workspace whose volume still exists"
                )
            if state != "deleting":
                db.execute(
                    "UPDATE checkpoints SET state='deleting', deleting_at=? WHERE checkpoint_id=?",
                    (now.isoformat(), checkpoint_id),
                )
            self._journal(
                db,
                now,
                actor,
                "checkpoint",
                manifest,
                "intent",
                {
                    "checkpoint_id": checkpoint_id,
                    "sandbox": sandbox,
                    "was": state,
                    "volume_gone": volume_gone,
                },
            )
        return next(c for c in self.checkpoints() if c.manifest_sha256 == manifest)

    def deleted(self, manifest: str, now: datetime, actor: str, detail: dict[str, object]) -> None:
        with self._write() as db:
            if (
                db.execute(
                    "UPDATE checkpoints SET state='deleted', deleted_at=?"
                    " WHERE manifest_sha256=? AND state='deleting'",
                    (now.isoformat(), manifest),
                ).rowcount
                != 1
            ):
                raise RecordError("record the deletion's intent first")
            self._journal(db, now, actor, "checkpoint", manifest, "done", detail)

    # The journal

    def journal(
        self,
        now: datetime,
        actor: str,
        kind: Literal["volume", "checkpoint"],
        subject: str,
        phase: Literal["intent", "done", "failed"],
        detail: dict[str, object],
    ) -> None:
        with self._write() as db:
            self._journal(db, now, actor, kind, subject, phase, detail)

    @staticmethod
    def _journal(
        db: sqlite3.Connection,
        now: datetime,
        actor: str,
        kind: str,
        subject: str,
        phase: str,
        detail: dict[str, object],
    ) -> None:
        if not 1 <= len(actor) <= 256 or not actor.isprintable():
            raise RecordError("name who runs the deletion (1 to 256 printable characters)")
        db.execute(
            "INSERT INTO deletions(recorded_at, actor, kind, subject, phase, detail)"
            " VALUES (?,?,?,?,?,?)",
            (now.isoformat(), actor, kind, subject, phase, json.dumps(detail, sort_keys=True)),
        )

    def deletions(self) -> list[Deletion]:
        return [
            Deletion(
                int(row[0]),
                datetime.fromisoformat(str(row[1])),
                str(row[2]),
                _kind(str(row[3])),
                str(row[4]),
                _phase(str(row[5])),
                json.loads(str(row[6])),
            )
            for row in self.db.execute(
                "SELECT seq, recorded_at, actor, kind, subject, phase, detail"
                " FROM deletions ORDER BY seq"
            )
        ]


def _state(value: str) -> CheckpointState:
    if value == "verified" or value == "kept" or value == "deleting" or value == "deleted":
        return value
    raise RecordError("unknown checkpoint state")


def _kind(value: str) -> Literal["volume", "checkpoint"]:
    if value == "volume" or value == "checkpoint":
        return value
    raise RecordError("unknown deletion kind")


def _phase(value: str) -> Literal["intent", "done", "failed"]:
    if value == "intent" or value == "done" or value == "failed":
        return value
    raise RecordError("unknown deletion phase")
