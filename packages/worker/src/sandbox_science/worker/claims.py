# SPDX-License-Identifier: MIT
"""Durable, bounded idle-retention claims, always pinned to a sandbox generation."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, EnvironmentInfo, ErrorKind
from sandbox_science.contracts.environment_claims import MAX_ENVIRONMENT_CLAIMS, EnvironmentClaim

from .errors import WorkerError


class EnvironmentClaims:
    """One worker's claim store. The environment owner serializes lifecycle changes."""

    def __init__(self, path: Path, *, clock: Clock = SYSTEM_CLOCK) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS claims (environment TEXT NOT NULL, project TEXT NOT NULL, "
            "session TEXT NOT NULL, holder TEXT NOT NULL, sandbox TEXT NOT NULL, "
            "expires REAL NOT NULL, PRIMARY KEY(environment, holder))"
        )
        self._lock = threading.Lock()

    def grant(self, request: EnvironmentClaim, info: EnvironmentInfo) -> datetime:
        """Persist a grant under the live-claim cap; renewing uses the same slot."""
        if info.sandbox_id is None or request.seconds <= 0:
            raise ValueError("a claim needs an existing sandbox and positive duration")
        context = request.context
        if context.project_id != info.project_id or context.environment_id != info.environment_id:
            raise WorkerError(ErrorKind.CONFLICT, "claim scope does not match the environment")
        now = self.clock.now().timestamp()
        expires = now + request.seconds
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM claims WHERE expires <= ?", (now,))
                prior = self._db.execute(
                    "SELECT project, session, sandbox FROM claims WHERE environment=? AND holder=?",
                    (info.environment_id, request.holder),
                ).fetchone()
                scope = context.project_id, context.session_id, info.sandbox_id
                if prior is not None and tuple(prior) != scope:
                    raise WorkerError(ErrorKind.CONFLICT, "claim holder belongs to another scope")
                count = self._db.execute(
                    "SELECT count(*) FROM claims WHERE environment=?", (info.environment_id,)
                ).fetchone()[0]
                if prior is None and count >= MAX_ENVIRONMENT_CLAIMS:
                    raise WorkerError(ErrorKind.UNAVAILABLE, "environment claim limit reached")
                self._db.execute(
                    "INSERT INTO claims VALUES (?,?,?,?,?,?) ON CONFLICT(environment,holder) "
                    "DO UPDATE SET expires=excluded.expires",
                    (info.environment_id, *scope[:2], request.holder, info.sandbox_id, expires),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return datetime.fromtimestamp(expires, UTC)

    def release(self, request: EnvironmentClaim) -> None:
        """Release only this holder's original project/session/sandbox claim."""
        context = request.context
        with self._lock:
            self._db.execute(
                "DELETE FROM claims WHERE environment=? AND project=? AND session=? "
                "AND holder=? AND sandbox=?",
                (
                    context.environment_id,
                    context.project_id,
                    context.session_id,
                    request.holder,
                    request.sandbox_id,
                ),
            )

    def held(self, info: EnvironmentInfo) -> bool:
        """Whether an unexpired claim protects this exact sandbox."""
        with self._lock:
            return (
                self._db.execute(
                    "SELECT 1 FROM claims WHERE environment=? AND project=? AND sandbox=? "
                    "AND expires>? LIMIT 1",
                    (
                        info.environment_id,
                        info.project_id,
                        info.sandbox_id,
                        self.clock.now().timestamp(),
                    ),
                ).fetchone()
                is not None
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
