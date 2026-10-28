# SPDX-License-Identifier: MIT
"""USER project settings for connectors: which configured MCP servers are turned off.

The deployment configures the servers (the launcher's ``[mcp_proxy]`` and the policy);
a project's user may only turn some of them off, and back on. Turning one off narrows
what the project's agents can reach: its tools are not offered to a run that starts, and
a running run's calls to it are refused. Agents never change this setting.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Collection
from typing import Final

from pydantic import Field, TypeAdapter, ValidationError

from sandbox_science.contracts import Contract, Identifier

from .control import Receipts
from .documents import DocumentStore
from .documents.errors import CONFLICT, INTERNAL, INVALID_REQUEST, POLICY_DENIED, DocumentError
from .documents.models import Author

MAX_SERVERS: Final = 64
_RECEIPTS = Receipts("connectors", "operation ID already names another connector settings change")
_NAMES: Final = TypeAdapter(list[Identifier])


class ConnectorSettings(Contract):
    project: str
    revision: int = Field(ge=0)
    """Zero while the user has changed nothing: every configured server is on."""
    configured: tuple[str, ...] = Field(max_length=MAX_SERVERS)
    disabled: tuple[str, ...] = Field(max_length=MAX_SERVERS)


class ConnectorSettingsChange(Contract):
    expected: int = Field(ge=0, le=999999999, strict=True)
    disabled: list[Identifier] = Field(max_length=MAX_SERVERS)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProjectConnectors:
    """Revisioned connector settings for projects in one control store."""

    def __init__(self, documents: DocumentStore) -> None:
        self.documents = documents

    def disabled(self, project: str) -> frozenset[str]:
        """The servers turned off in the project; none before any change."""
        _scope(project)
        if self.documents.control.missing:
            return frozenset()
        with self.documents.control.read() as tx:
            return frozenset(_read(tx.sql, project)[1])

    def get(self, project: str, configured: Collection[str]) -> ConnectorSettings:
        _scope(project)
        revision, stored = (0, ()) if self.documents.control.missing else self._read(project)
        return _view(project, revision, stored, configured)

    def change(
        self,
        project: str,
        value: object,
        author: Author,
        *,
        configured: Collection[str],
        widen: bool = True,
    ) -> tuple[ConnectorSettings, bool]:
        """The user's change, at the revision read (``expected``): a list of configured
        servers to turn off. A server the deployment does not configure is refused, so
        the setting can only narrow. Without ``widen``, a change that turns a server back
        on is refused (the workspace's admin-only action, D104)."""
        _scope(project)
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user turns a project's connectors off")
        try:
            change = ConnectorSettingsChange.model_validate(value)
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "invalid connector settings change") from None
        unknown = sorted(set(change.disabled) - set(configured))
        if unknown:
            raise DocumentError(
                INVALID_REQUEST, f"not a configured connector: {', '.join(unknown)}"
            )
        wanted = tuple(sorted(set(change.disabled)))
        fingerprint = hashlib.sha256(
            json.dumps(change.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        with self.documents.control.write() as tx:
            db = tx.sql
            replay = _RECEIPTS.find(db, project, author.actor, change.operation_id, fingerprint)
            if replay is not None:
                return _view(project, *_read(db, project), configured), True
            revision, current = _read(db, project)
            if revision != change.expected:
                raise DocumentError(CONFLICT, "the project's connector settings changed meanwhile")
            if not widen and not set(current) <= set(wanted):
                raise DocumentError(
                    POLICY_DENIED, "turning an MCP service back on is for the workspace's admins"
                )
            if current != wanted:
                db.execute(
                    "INSERT INTO project_connector_settings VALUES(?,?,?,?) ON CONFLICT(project) "
                    "DO UPDATE SET revision=excluded.revision,disabled=excluded.disabled,"
                    "author=excluded.author",
                    (project, revision + 1, json.dumps(list(wanted)), author.model_dump_json()),
                )
            if _RECEIPTS.count(db, project) >= 4096:
                raise DocumentError(CONFLICT, "the connector settings' operation quota is reached")
            result = _view(project, *_read(db, project), configured)
            _RECEIPTS.record(
                db, project, author.actor, change.operation_id, fingerprint,
                {"revision": result.revision},
            )  # fmt: skip
            return result, False

    def _read(self, project: str) -> tuple[int, tuple[str, ...]]:
        with self.documents.control.read() as tx:
            return _read(tx.sql, project)


def _view(
    project: str, revision: int, stored: tuple[str, ...], configured: Collection[str]
) -> ConnectorSettings:
    # A server the deployment no longer configures stays off in the record, unshown.
    names = tuple(sorted(configured))
    return ConnectorSettings(
        project=project,
        revision=revision,
        configured=names,
        disabled=tuple(name for name in stored if name in names),
    )


def _read(db: sqlite3.Connection, project: str) -> tuple[int, tuple[str, ...]]:
    row = db.execute(
        "SELECT revision,disabled FROM project_connector_settings WHERE project=?", (project,)
    ).fetchone()
    if row is None:
        return 0, ()
    try:
        return int(row[0]), tuple(sorted(_NAMES.validate_python(json.loads(row[1]))))
    except (ValidationError, ValueError, TypeError):
        raise DocumentError(INTERNAL, "invalid stored connector settings") from None


def _scope(project: object) -> None:
    if (
        not isinstance(project, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project) is None
    ):
        raise DocumentError(INVALID_REQUEST, "invalid project")
