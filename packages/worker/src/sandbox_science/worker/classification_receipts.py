# SPDX-License-Identifier: MIT
"""Receipts of the user's classification acceptances, by operation ID (CL-3, CL-4).

The workspace names each acceptance it relays with an operation ID. The worker keeps the
first answer to it in its store, with the digest of what was asked, so that a retry (a
workspace restart, a lost response) gets that answer unchanged and accepts nothing twice,
and the same ID with another request is a conflict. Receipts are kept per project, like
executions' operation IDs.

The acceptance itself happens before its receipt is kept: a worker that stops between the
two leaves an acceptance without a receipt, and a retry then accepts again (the same
revision, recorded twice), never nothing.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from typing import Final

from sandbox_science.contracts import SYSTEM_CLOCK, Clock

MAX_RESPONSE_BYTES: Final = 1024 * 1024


class ReceiptConflictError(Exception):
    """An operation ID already used for another request."""


def create_schema(db: sqlite3.Connection) -> None:
    """Append-only worker migration: the receipts table."""
    db.execute(
        f"""CREATE TABLE classification_receipts (
    operation_key TEXT PRIMARY KEY CHECK (length(operation_key) BETWEEN 3 AND 257),
    request_digest TEXT NOT NULL CHECK (length(request_digest)=64
        AND request_digest NOT GLOB '*[^0-9a-f]*'),
    response TEXT NOT NULL CHECK (length(CAST(response AS BLOB)) BETWEEN 1
        AND {MAX_RESPONSE_BYTES}),
    recorded_at TEXT NOT NULL
) STRICT"""
    )


class ClassificationReceipts:
    """The receipts table of the worker's store."""

    def __init__(self, db: sqlite3.Connection, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.db = db
        self.clock = clock
        self._lock = threading.Lock()

    def once(self, project: str, operation_id: str, digest: str, act: Callable[[], str]) -> str:
        """The answer to an operation: its receipt's, else ``act()``'s, kept as its receipt.
        One acceptance runs at a time, so two retries of one ID cannot both act. Raises
        ReceiptConflictError for an ID used with another request (``digest``). Blocking."""
        key = f"{project}/{operation_id}"
        with self._lock:
            row = self.db.execute(
                "SELECT request_digest, response FROM classification_receipts"
                " WHERE operation_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if str(row[0]) != digest:
                    raise ReceiptConflictError(
                        f"operation {operation_id!r} was used for another request"
                    )
                return str(row[1])
            response = act()
            self.db.execute(
                "INSERT INTO classification_receipts VALUES (?, ?, ?, ?)",
                (key, digest, response, self.clock.now().isoformat()),
            )
            return response
