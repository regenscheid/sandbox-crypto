# SPDX-License-Identifier: MIT
"""Durable job notices, active job_waits and watcher accounting, beside project records.

Worker records remain authoritative for jobs. This store acknowledges a completion
only after the run containing it has been saved. A crash can repeat a notice, never
silently consume one. Each method owns a SQLite connection and may run in a thread.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sandbox_science.contracts import ExecutionContext

from .documents import DocumentStore, store_path
from .watcher_prompt import (
    MAX_PROVENANCE_BYTES,
    WatcherProvenance,
    WatcherProvenanceRead,
    encode_provenance,
)

LEASE_SECONDS = 30


class JobState:
    """Control-plane state shared by harness processes and the workspace server."""

    def __init__(self, state_dir: Path) -> None:
        self.path = store_path(state_dir)
        documents = DocumentStore(self.path, producer="job-state")
        try:
            documents.initialize()
        finally:
            documents.close()
        state_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS job_runs (
                    project TEXT, session TEXT, run TEXT, heartbeat REAL,
                    PRIMARY KEY(project, session, run));
                CREATE TABLE IF NOT EXISTS job_origins (
                    project TEXT, session TEXT, job TEXT, run TEXT,
                    PRIMARY KEY(project, session, job));
                CREATE TABLE IF NOT EXISTS job_acknowledged (
                    project TEXT, session TEXT, job TEXT, run TEXT,
                    PRIMARY KEY(project, session, job));
                CREATE TABLE IF NOT EXISTS job_waits (
                    project TEXT, session TEXT, run TEXT, payload TEXT,
                    PRIMARY KEY(project, session, run));
                CREATE TABLE IF NOT EXISTS job_watcher_calls (
                    id INTEGER PRIMARY KEY, project TEXT, session TEXT, run TEXT,
                    job TEXT, alias TEXT, requested_at REAL, input TEXT,
                    outcome TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER);
            """)
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT 1 FROM pragma_table_info('job_watcher_calls') "
                    "WHERE name='prompt_provenance'"
                ).fetchone()
                is None
            ):
                db.execute("ALTER TABLE job_watcher_calls ADD COLUMN prompt_provenance TEXT")

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def heartbeat(self, context: ExecutionContext, run: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO job_runs VALUES (?, ?, ?, ?)",
                (context.project_id, context.session_id, run, time.time()),
            )

    def end_run(self, context: ExecutionContext, run: str) -> None:
        with self._db() as db:
            key = context.project_id, context.session_id, run
            db.execute("DELETE FROM job_runs WHERE project=? AND session=? AND run=?", key)
            db.execute("DELETE FROM job_waits WHERE project=? AND session=? AND run=?", key)

    def origin(self, context: ExecutionContext, job: str, run: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO job_origins VALUES (?, ?, ?, ?)",
                (context.project_id, context.session_id, job, run),
            )

    def pending(self, context: ExecutionContext, run: str, jobs: Sequence[str]) -> list[str]:
        """Unacknowledged completions owned by this run, or whose owner has ended."""
        with self._db() as db:
            key = context.project_id, context.session_id
            ack = {
                row[0]
                for row in db.execute(
                    "SELECT job FROM job_acknowledged WHERE project=? AND session=?", key
                )
            }
            owned = {
                row[0]: row[1]
                for row in db.execute(
                    "SELECT job, run FROM job_origins WHERE project=? AND session=?", key
                )
            }
            active = {
                row[0]
                for row in db.execute(
                    "SELECT run FROM job_runs WHERE project=? AND session=? AND heartbeat>?",
                    (*key, time.time() - LEASE_SECONDS),
                )
            }
        return [
            job
            for job in jobs
            if job not in ack and (owned.get(job) == run or owned.get(job) not in active)
        ]

    def acknowledge(self, context: ExecutionContext, run: str, jobs: Sequence[str]) -> None:
        """Call only after a durable run record represents these notices."""
        with self._db() as db:
            db.executemany(
                "INSERT OR IGNORE INTO job_acknowledged VALUES (?, ?, ?, ?)",
                [(context.project_id, context.session_id, job, run) for job in jobs],
            )

    def waiting(
        self, context: ExecutionContext, run: str, payload: dict[str, object] | None
    ) -> None:
        with self._db() as db:
            key = context.project_id, context.session_id, run
            if payload is None:
                db.execute("DELETE FROM job_waits WHERE project=? AND session=? AND run=?", key)
            else:
                db.execute(
                    "INSERT OR REPLACE INTO job_waits VALUES (?, ?, ?, ?)",
                    (*key, json.dumps(payload)),
                )

    def waits(self, context: ExecutionContext) -> list[dict[str, object]]:
        with self._db() as db:
            rows = db.execute(
                """SELECT w.payload, r.heartbeat FROM job_waits w LEFT JOIN job_runs r
                ON (w.project=r.project AND w.session=r.session AND w.run=r.run)
                WHERE w.project=? AND w.session=?""",
                (context.project_id, context.session_id),
            )
            return [
                {
                    **cast(dict[str, object], json.loads(row[0])),
                    "connected": row[1] is not None and row[1] > time.time() - LEASE_SECONDS,
                }
                for row in rows
            ]

    def reserve(
        self,
        context: ExecutionContext,
        run: str,
        job: str,
        alias: str,
        payload: dict[str, object],
        limit: int,
        due: float,
        *,
        provenance: WatcherProvenance | None = None,
    ) -> int | None:
        """Reserve one call from the project's durable hourly watcher budget."""
        pin = encode_provenance(provenance) if provenance is not None else None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            latest = db.execute(
                "SELECT max(requested_at) FROM job_watcher_calls WHERE project=? AND job=?",
                (context.project_id, job),
            ).fetchone()[0]
            if latest is not None and latest >= due:
                return None
            used = db.execute(
                "SELECT count(*) FROM job_watcher_calls WHERE project=? AND requested_at>?",
                (context.project_id, now - 3600),
            ).fetchone()[0]
            if used >= limit:
                return None
            cursor = db.execute(
                """INSERT INTO job_watcher_calls
                (project,session,run,job,alias,requested_at,input,outcome,prompt_provenance)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    context.project_id,
                    context.session_id,
                    run,
                    job,
                    alias,
                    now,
                    json.dumps(payload),
                    "pending",
                    pin,
                ),
            )
            return cursor.lastrowid

    def watcher_provenance(self, context: ExecutionContext, call: int) -> WatcherProvenanceRead:
        """Internal exact-scope read; never fetch oversized or non-text metadata."""
        if type(call) is not int or not 1 <= call <= 2**63 - 1:
            raise ValueError("invalid watcher reservation ID")
        scope = context.project_id, context.session_id
        if any(
            type(value) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None
            for value in scope
        ):
            raise ValueError("invalid watcher reservation scope")
        with self._db() as db:
            row = db.execute(
                """SELECT CASE WHEN prompt_provenance IS NULL THEN 0
                    WHEN typeof(prompt_provenance)='text'
                      AND length(CAST(prompt_provenance AS BLOB))<=?
                    THEN 1 ELSE 2 END,
                  CASE WHEN typeof(prompt_provenance)='text'
                      AND length(CAST(prompt_provenance AS BLOB))<=?
                    THEN CAST(prompt_provenance AS BLOB) ELSE NULL END
                  FROM job_watcher_calls WHERE project=? AND session=? AND id=? LIMIT 1""",
                (
                    MAX_PROVENANCE_BYTES,
                    MAX_PROVENANCE_BYTES,
                    *scope,
                    call,
                ),
            ).fetchone()
        if row is None:
            return WatcherProvenanceRead("absent")
        if row[0] == 0:
            return WatcherProvenanceRead("legacy")
        if row[0] == 1:
            try:
                return WatcherProvenanceRead(
                    "complete", WatcherProvenance.model_validate_json(row[1])
                )
            except ValueError:
                pass
        return WatcherProvenanceRead("corrupt")

    def finish_call(
        self,
        call: int,
        outcome: str,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        with self._db() as db:
            db.execute(
                """UPDATE job_watcher_calls SET outcome=?,model=?,input_tokens=?,output_tokens=?
                WHERE id=?""",
                (outcome, model, input_tokens, output_tokens, call),
            )

    def last_check(self, context: ExecutionContext, job: str) -> float | None:
        with self._db() as db:
            value = db.execute(
                "SELECT max(requested_at) FROM job_watcher_calls WHERE project=? AND job=?",
                (context.project_id, job),
            ).fetchone()[0]
            return float(value) if value is not None else None

    def checkins(self, context: ExecutionContext, *, limit: int = 100) -> list[dict[str, object]]:
        """Recent validated watcher notes, also after a wait has ended."""
        with self._db() as db:
            rows = db.execute(
                """SELECT job,alias,model,requested_at,outcome FROM job_watcher_calls
                WHERE project=? AND session=? ORDER BY id DESC LIMIT ?""",
                (context.project_id, context.session_id, limit),
            ).fetchall()
        values: list[dict[str, object]] = []
        for job, alias, model, requested_at, raw in rows:
            try:
                answer: object = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(answer, dict):
                continue
            answer = cast(dict[str, object], answer)
            if answer.get("state") not in {"fine", "needs_attention"}:
                continue
            values.append(
                {
                    "job_id": job,
                    "alias": alias,
                    "model": model,
                    "at": datetime.fromtimestamp(requested_at, UTC).isoformat(),
                    **answer,
                }
            )
        return values
