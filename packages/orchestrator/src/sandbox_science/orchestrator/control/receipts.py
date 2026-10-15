# SPDX-License-Identifier: MIT
"""Operation receipts: a change's first result, kept under its operation ID so that a
retry with the same request gets it again and a different request is refused.

One table holds every kind of change's receipts (``operation_receipts``); a ``scope``
names the kind (documents, comments, reviews and findings, project macros), and each
keeps its own fingerprint of the request. A receipt is written in the change it
records, so it exists exactly when the change committed (refactoring plan, B-04).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Final

from .errors import CONFLICT, ControlError

Json = dict[str, Any]


def _canonical(value: object) -> str:
    """The JSON text a receipt keeps: sorted keys, no spaces, UTF-8 as is, and no NaN or
    infinity."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


class Receipts:
    """The receipts of one kind of change (``scope``). ``conflict`` is the message a
    request gets when its operation ID already names another change; ``{operation}``
    in it names that ID."""

    SCOPES: Final = ("document", "comment", "review", "macros", "ledger", "files", "memory")

    def __init__(self, scope: str, conflict: str) -> None:
        if scope not in self.SCOPES:
            raise ValueError(f"unknown receipt scope {scope!r}")
        self.scope, self.conflict = scope, conflict

    def find(
        self, db: sqlite3.Connection, project: str, actor: str, operation: str, fingerprint: str
    ) -> Json | None:
        """The result first recorded under ``operation``, or None when there is none.
        A receipt for another request refuses this one."""
        row = db.execute(
            "SELECT fingerprint,result FROM operation_receipts WHERE scope=? AND project=? "
            "AND actor=? AND operation_id=?",
            (self.scope, project, actor, operation),
        ).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise ControlError(CONFLICT, self.conflict.format(operation=operation))
        return json.loads(row[1])

    def record(
        self,
        db: sqlite3.Connection,
        project: str,
        actor: str,
        operation: str,
        fingerprint: str,
        result: Json,
    ) -> None:
        """Keep ``result`` as the receipt of ``operation``, in the change it records."""
        db.execute(
            "INSERT INTO operation_receipts(scope,project,actor,operation_id,fingerprint,result) "
            "VALUES(?,?,?,?,?,?)",
            (self.scope, project, actor, operation, fingerprint, _canonical(result)),
        )

    def count(self, db: sqlite3.Connection, project: str) -> int:
        """How many receipts of this kind a project keeps."""
        return db.execute(
            "SELECT count(*) FROM operation_receipts WHERE scope=? AND project=?",
            (self.scope, project),
        ).fetchone()[0]
