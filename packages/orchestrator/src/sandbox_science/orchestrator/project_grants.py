# SPDX-License-Identifier: MIT
"""The user's standing answers to permission requests: grants (D97).

A grant names what it covers and where it holds: an effect class on one MCP server, a host
for research code's reads, or a package source (an ecosystem, and for conda a channel),
for one session or for the whole project. It covers a held request only within that
request's ceiling (``max_grant``), read from the request itself each time, so a policy
revision that lowers a ceiling takes effect at once; an ``unclassified`` call is never
covered. A class grant covers only the agent's calls, a host grant only research code's,
so neither rides on the other. A tool a server adds later is covered only if the policy
classifies it into a granted class.

Grants are persistent until revoked, unless the user gave one an expiry; the workspace
records each use. They are not bound to a sandbox or a policy revision: a grant answers
only what the current policy escalates, under the ceiling the request carries, so a new
revision cannot widen what it means (docs/MCP-CONNECTORS.md, "Grants").
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import Field

from sandbox_science.contracts import Contract
from sandbox_science.policy.approvals import READ_METHODS, allowed

from .documents import DocumentStore
from .documents.errors import INVALID_REQUEST, NOT_FOUND, POLICY_DENIED, DocumentError
from .documents.models import Author

GrantKind = Literal["mcp", "network", "packages"]
StoredScope = Literal["session", "project"]
MAX_GRANTS: Final = 512
"""Live grants one project may hold."""
MAX_GRANT_HOURS: Final = 24 * 365
_TEXT: Final = r"^[^\x00-\x1f\x7f]{1,253}$"


class ProjectGrant(Contract):
    """One grant, as the Grants panel lists it."""

    grant_id: str
    kind: GrantKind
    scope: StoredScope
    session: str | None = None
    server: str | None = None
    effect: str | None = None
    """An MCP grant's effect class on ``server``."""
    host: str | None = None
    methods: Literal["read"] | None = None
    """A network grant's host and method class: research code's reads only."""
    ecosystem: str | None = None
    channel: str | None = None
    """A package grant's ecosystem and, for conda, channel."""
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    uses: int = Field(default=0, ge=0)


class GrantListing(Contract):
    project: str
    grants: list[ProjectGrant]


@dataclass(frozen=True, slots=True)
class Asked:
    """What a held request asks for, as grants compare it. The workspace builds it from
    the proxy's or the worker's record of the request, never from the agent's words."""

    kind: GrantKind
    caller_kind: str
    session: str | None
    max_grant: str | None
    server: str | None = None
    effect: str | None = None
    host: str | None = None
    method: str | None = None
    sources: tuple[tuple[str, str | None], ...] = ()
    """A package request's sources: every one must be granted."""

    def subjects(self) -> list[dict[str, str | None]]:
        """What a grant of this request names: one per package source."""
        if self.kind == "mcp":
            return [{"server": self.server, "effect": self.effect}]
        if self.kind == "network":
            return [{"host": self.host, "methods": "read"}]
        return [{"ecosystem": eco, "channel": channel} for eco, channel in self.sources]

    def grantable(self, scope: str) -> str | None:
        """Why the user cannot grant this request for ``scope``, or None if they can."""
        if not allowed(scope, self.max_grant, self.effect, session=self.session is not None):
            return f"the policy allows no {scope} grant for this request"
        if scope == "once":
            return None
        if self.kind == "mcp" and (
            self.caller_kind != "agent" or not self.server or not self.effect
        ):
            return "only the agent's tool calls of a classified tool are granted by class"
        if self.kind == "network" and (
            self.caller_kind != "code" or not self.host or (self.method or "") not in READ_METHODS
        ):
            return "research code's network requests are granted by host for reads only"
        if self.kind == "packages" and not self.sources:
            return "the request names no package source"
        return None


def covers(grant: ProjectGrant, asked: Asked, now: datetime) -> bool:
    """Whether ``grant`` covers one subject of ``asked`` (for packages, any one source)."""
    if grant.kind != asked.kind or (grant.expires_at is not None and now >= grant.expires_at):
        return False
    if grant.scope == "session" and (asked.session is None or grant.session != asked.session):
        return False
    if asked.grantable(grant.scope) is not None:
        return False  # the request's ceiling, now, or its caller, rules the grant out
    if grant.kind == "mcp":
        return (grant.server, grant.effect) == (asked.server, asked.effect)
    if grant.kind == "network":
        return grant.host == asked.host
    return (grant.ecosystem, grant.channel) in asked.sources


def covering(grants: Iterable[ProjectGrant], asked: Asked, now: datetime) -> list[ProjectGrant]:
    """The grants that together cover ``asked``, or none: a package request needs every
    source granted; anything else one grant. A project grant is preferred to a session's."""
    live = sorted((g for g in grants if covers(g, asked, now)), key=lambda g: g.scope != "project")
    if asked.kind != "packages":
        return live[:1]
    chosen: list[ProjectGrant] = []
    for source in asked.sources:
        found = next((g for g in live if (g.ecosystem, g.channel) == source), None)
        if found is None:
            return []
        chosen.append(found)
    return list({g.grant_id: g for g in chosen}.values())


_COLUMNS: Final = (
    "grant_id,kind,scope,session,server,effect,host,methods,ecosystem,channel,"
    "created_at,expires_at,last_used_at,uses"
)
_LIVE: Final = (
    "SELECT grant_id,kind,scope,session,server,effect,host,methods,ecosystem,channel,"
    "created_at,expires_at,last_used_at,uses FROM project_grants "
    "WHERE project=? AND revoked_at IS NULL ORDER BY created_at DESC, grant_id"
)


class ProjectGrants:
    """A control store's grants: listed, given, used and revoked."""

    def __init__(self, documents: DocumentStore) -> None:
        self.documents = documents

    def listing(self, project: str) -> GrantListing:
        """The project's grants that are not revoked, newest first."""
        _scope(project)
        if self.documents.control.missing:
            return GrantListing(project=project, grants=[])
        with self.documents.control.read() as tx:
            return GrantListing(project=project, grants=_live(tx.sql, project))

    def projects_with_code_grants(self) -> list[str]:
        """The projects that hold network or package grants: the only ones whose worker
        requests a relay need look at."""
        if self.documents.control.missing:
            return []
        with self.documents.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT DISTINCT project FROM project_grants "
                "WHERE kind IN ('network','packages') AND revoked_at IS NULL ORDER BY project"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def projects_with_worker_grants(self) -> list[str]:
        """The projects that hold any grant: those whose requests at the worker (research
        code's, and the agent's MCP calls the middleware holds, D98) a relay need look at."""
        if self.documents.control.missing:
            return []
        with self.documents.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT DISTINCT project FROM project_grants "
                "WHERE revoked_at IS NULL ORDER BY project"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def covering(self, project: str, asked: Asked, now: datetime) -> list[ProjectGrant]:
        return covering(self.listing(project).grants, asked, now)

    def give(
        self,
        project: str,
        asked: Asked,
        scope: str,
        author: Author,
        now: datetime,
        hours: float | None = None,
    ) -> list[ProjectGrant]:
        """Grant what ``asked`` asks for, for ``scope`` (session or project), within its
        ceiling, which is checked here whatever a client sent. One grant per subject; one
        the project already holds is kept, not repeated."""
        _scope(project)
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user grants a project's requests")
        if scope not in ("session", "project"):
            raise DocumentError(INVALID_REQUEST, "a grant is for a session or the project")
        problem = asked.grantable(scope)
        if problem is not None:
            raise DocumentError(POLICY_DENIED, problem)
        if hours is not None and not 0 < hours <= MAX_GRANT_HOURS:
            raise DocumentError(INVALID_REQUEST, f"a grant lasts at most {MAX_GRANT_HOURS} hours")
        session = asked.session if scope == "session" else None
        expires = now + timedelta(hours=hours) if hours is not None else None
        given: list[str] = []
        with self.documents.control.write() as tx:
            db = tx.sql
            for subject in asked.subjects():
                for value in subject.values():
                    if value is not None and re.fullmatch(_TEXT, value) is None:
                        raise DocumentError(INVALID_REQUEST, "invalid grant")
                key = _subject(asked.kind, subject)
                row = db.execute(
                    "SELECT grant_id FROM project_grants WHERE project=? AND subject=? AND "
                    "scope=? AND coalesce(session,'')=? AND revoked_at IS NULL",
                    (project, key, scope, session or ""),
                ).fetchone()
                if row is not None:
                    given.append(str(row[0]))
                    continue
                if _count(db, project) >= MAX_GRANTS:
                    raise DocumentError(
                        POLICY_DENIED, f"a project holds at most {MAX_GRANTS} grants"
                    )
                grant_id = "grant-" + secrets.token_hex(10)
                db.execute(
                    "INSERT INTO project_grants (grant_id,project,kind,scope,session,server,"
                    "effect,host,methods,ecosystem,channel,subject,created_at,expires_at,"
                    "author) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant_id, project, asked.kind, scope, session,
                        subject.get("server"), subject.get("effect"), subject.get("host"),
                        subject.get("methods"), subject.get("ecosystem"), subject.get("channel"),
                        key, _time(now), _time(expires) if expires else None,
                        author.model_dump_json(),
                    ),
                )  # fmt: skip
                given.append(grant_id)
            found = {grant.grant_id: grant for grant in _live(db, project)}
        return [found[grant_id] for grant_id in given if grant_id in found]

    def used(self, project: str, grant_ids: Sequence[str], now: datetime) -> None:
        """Note that these grants answered a request."""
        if not grant_ids:
            return
        with self.documents.control.write() as tx:
            tx.sql.executemany(
                "UPDATE project_grants SET last_used_at=?, uses=uses+1 "
                "WHERE project=? AND grant_id=? AND revoked_at IS NULL",
                [(_time(now), project, grant_id) for grant_id in grant_ids],
            )

    def revoke(self, project: str, grant_id: str, author: Author, now: datetime) -> GrantListing:
        """End a grant at once: it covers nothing from now on. Revoking one already
        revoked is a no-op; an unknown one is not found."""
        _scope(project)
        if author.kind != "user":
            raise DocumentError(POLICY_DENIED, "only the user revokes a project's grants")
        with self.documents.control.write() as tx:
            row = tx.sql.execute(
                "SELECT revoked_at FROM project_grants WHERE project=? AND grant_id=?",
                (project, grant_id),
            ).fetchone()
            if row is None:
                raise DocumentError(NOT_FOUND, "no such grant")
            if row[0] is None:
                tx.sql.execute(
                    "UPDATE project_grants SET revoked_at=? WHERE project=? AND grant_id=?",
                    (_time(now), project, grant_id),
                )
            return GrantListing(project=project, grants=_live(tx.sql, project))


def _subject(kind: str, subject: dict[str, str | None]) -> str:
    return json.dumps([kind, *(subject.get(name) for name in sorted(subject))])


def _count(db: sqlite3.Connection, project: str) -> int:
    row = db.execute(
        "SELECT count(*) FROM project_grants WHERE project=? AND revoked_at IS NULL", (project,)
    ).fetchone()
    return int(row[0])


def _live(db: sqlite3.Connection, project: str) -> list[ProjectGrant]:
    rows = db.execute(_LIVE, (project,)).fetchall()
    names = _COLUMNS.split(",")
    return [ProjectGrant.model_validate(dict(zip(names, row, strict=True))) for row in rows]


def _time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _scope(project: object) -> None:
    if (
        not isinstance(project, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project) is None
    ):
        raise DocumentError(INVALID_REQUEST, "invalid project")
