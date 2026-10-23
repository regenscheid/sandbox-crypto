# SPDX-License-Identifier: MIT
"""The connector tool calls a project's user approved for the whole project (D76; owner
decision, 2026-10-21): one tool of a server, or any tool of it the policy escalates.

The MCP proxy decides each call: its policy says which tools escalate to the user, and
an approval it holds lets a held call through. A grant here is the user's standing
answer, which the workspace gives the proxy again whenever a call it covers is held
(``workspace.approvals``), for a short time each, so that removing the grant here ends it
soon after. No grant reaches a tool the policy denies: the proxy asks about none.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Final

from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import Contract, Identifier

from .control import Receipts
from .documents import DocumentStore
from .documents.errors import CONFLICT, INTERNAL, INVALID_REQUEST, POLICY_DENIED, DocumentError
from .documents.models import Author

MAX_GRANTS: Final = 256
_RECEIPTS = Receipts("approvals", "operation ID already names another approval settings change")


class Grant(Contract):
    server: Identifier
    tool: str | None = Field(default=None, min_length=1, max_length=200)
    """The tool, or None for any tool of the server that the policy escalates."""

    def covers(self, server: str, tool: str) -> bool:
        return self.server == server and (self.tool is None or self.tool == tool)


_GRANTS: Final = TypeAdapter(list[Grant])


class ApprovalSettings(Contract):
    project: str
    revision: int = Field(ge=0)
    grants: tuple[Grant, ...] = Field(max_length=MAX_GRANTS)


class ApprovalSettingsChange(Contract):
    expected: int = Field(ge=0, le=999999999, strict=True)
    grants: list[Grant] = Field(max_length=MAX_GRANTS)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProjectApprovals:
    """Revisioned standing approvals for projects in one control store."""

    def __init__(self, documents: DocumentStore) -> None:
        self.documents = documents

    def get(self, project: str) -> ApprovalSettings:
        _scope(project)
        if self.documents.control.missing:
            return ApprovalSettings(project=project, revision=0, grants=())
        with self.documents.control.read() as tx:
            revision, grants = _read(tx.sql, project)
        return ApprovalSettings(project=project, revision=revision, grants=grants)

    def covering(self, project: str, server: str, tool: str) -> Grant | None:
        """The grant that covers a call of ``tool`` on ``server``: the tool's own first."""
        found = [grant for grant in self.get(project).grants if grant.covers(server, tool)]
        return min(found, key=lambda grant: grant.tool is None, default=None)

    def change(self, project: str, value: object, author: Author) -> tuple[ApprovalSettings, bool]:
        """The user's whole list, at the revision read (``expected``)."""
        _scope(project)
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user approves a project's tool calls")
        try:
            change = ApprovalSettingsChange.model_validate(value)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid approval settings change") from None
        wanted = _canonical(change.grants)
        fingerprint = hashlib.sha256(
            json.dumps(change.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        with self.documents.control.write() as tx:
            db = tx.sql
            replay = _RECEIPTS.find(db, project, author.actor, change.operation_id, fingerprint)
            if replay is not None:
                return _view(project, *_read(db, project)), True
            revision, current = _read(db, project)
            if revision != change.expected:
                raise DocumentError(CONFLICT, "the project's approvals changed meanwhile")
            if current != wanted:
                db.execute(
                    "INSERT INTO project_approval_settings VALUES(?,?,?,?) ON CONFLICT(project) "
                    "DO UPDATE SET revision=excluded.revision,grants=excluded.grants,"
                    "author=excluded.author",
                    (
                        project,
                        revision + 1,
                        json.dumps([grant.model_dump(mode="json") for grant in wanted]),
                        author.model_dump_json(),
                    ),
                )
            if _RECEIPTS.count(db, project) >= 4096:
                raise DocumentError(CONFLICT, "the approval settings' operation quota is reached")
            result = _view(project, *_read(db, project))
            _RECEIPTS.record(
                db, project, author.actor, change.operation_id, fingerprint,
                {"revision": result.revision},
            )  # fmt: skip
            return result, False


def _canonical(grants: list[Grant]) -> tuple[Grant, ...]:
    unique = {(grant.server, grant.tool or ""): grant for grant in grants}
    return tuple(unique[key] for key in sorted(unique))


def _view(project: str, revision: int, grants: tuple[Grant, ...]) -> ApprovalSettings:
    return ApprovalSettings(project=project, revision=revision, grants=grants)


def _read(db: sqlite3.Connection, project: str) -> tuple[int, tuple[Grant, ...]]:
    row = db.execute(
        "SELECT revision,grants FROM project_approval_settings WHERE project=?", (project,)
    ).fetchone()
    if row is None:
        return 0, ()
    try:
        return int(row[0]), _canonical(_GRANTS.validate_python(json.loads(row[1])))
    except (ValidationError, ValueError, TypeError):
        raise DocumentError(INTERNAL, "invalid stored approval settings") from None


def _scope(project: object) -> None:
    if (
        not isinstance(project, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project) is None
    ):
        raise DocumentError(INVALID_REQUEST, "invalid project")
