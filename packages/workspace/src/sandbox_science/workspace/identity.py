# SPDX-License-Identifier: MIT
"""Who a browser session is (D104): the operator's login link, or a user Authentik admitted.

The guard (app.py) sets each API request's ``Identity``. A session opened with the login
link that ``serve`` prints is the operator's: it is named ``user``, the workspace's one
user as the records have always named it, and it is an admin. A session opened from
Authentik's forward-auth token (forward_auth.py) is named by the token's username and is
an admin when the configured admin group is in the token's groups, or when no admin group
is configured.

The admin-only actions call ``require_admin``: accepting or editing an MCP service's
classification and re-accepting its tool list after drift (CL-3, CL-4), a project-scope
grant, clearing a hold, resolving a recovery, deciding a resource extension and changing
the agents' model settings. Each is logged with the user's name. Turning a configured MCP
service back on is admin-only too; the connector settings refuse it in the transaction
that writes them (``widen``), so that no change between a check and the write slips by.

Where a write records an ``Author``, it is the session's (``author``): the operator's is
``USER``, as before, and an Authentik user's is ``authentik:<username>``, so that no
Authentik user, whatever their name, is recorded as the operator. Chat messages and plan
edits still name the workspace's one user, as the agents read them.

``GET /api/v0/me`` answers the request's identity, for the page to show the name and to
disable what the user may not do, saying why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Literal

from aiohttp import web

from sandbox_science.contracts import Contract, ErrorKind
from sandbox_science.orchestrator.documents import USER, Author
from sandbox_science.orchestrator.projects import ProjectError

_log = logging.getLogger(__name__)

Login = Literal["token", "forward"]


@dataclass(frozen=True)
class Identity:
    user: str
    login: Login
    admin: bool
    expires: float | None = None
    """For a forward-auth session, when its token expires (seconds since the epoch)."""
    token: bytes = b""
    """For a forward-auth session, the SHA-256 of the token that opened it."""


OPERATOR: Final = Identity(user=USER.user or "user", login="token", admin=True)
FORWARD_AUTHOR: Final = "authentik:"
"""Prefixes an Authentik user's name as a record's author; the operator's is ``user``."""
"""The login link's session: the operator's own login."""
IDENTITY: Final = web.RequestKey("workspace_identity", Identity)
ADMIN_GROUP: Final = web.AppKey("workspace_admin_group", str)
"""The Authentik group the admin-only actions need; absent, every user is an admin."""


class Viewer(Contract):
    """``GET /api/v0/me``: who this session is, and what it may do."""

    user: str
    login: Login
    """``token`` for the login link, ``forward`` for a login through Authentik."""
    admin: bool
    admin_group: str | None
    """The Authentik group the admin-only actions need; null when none is configured."""


def identity(request: web.Request) -> Identity:
    """The request's identity, which the guard set; a request without one is refused."""
    found = request.get(IDENTITY)
    if found is None:
        raise ProjectError(ErrorKind.POLICY_DENIED, "not logged in")
    return found


def author(request: web.Request) -> Author:
    """The request's user as a record's author: the operator's login is ``USER``, an
    Authentik user ``authentik:<username>``, never the operator whatever the name."""
    found = identity(request)
    if found.login == "token":
        return USER
    return Author(kind="user", user=FORWARD_AUTHOR + found.user)


def operator(request: web.Request) -> str:
    """The request's user, as the records that name an operator by text name it."""
    return author(request).user or "user"


def require_admin(request: web.Request, action: str) -> None:
    """Refuse ``action`` (403) unless the request's user is an admin, and log who did it."""
    found = identity(request)
    if not found.admin:
        group = request.app.get(ADMIN_GROUP, "")
        _log.warning("refused %s to %s: not in the admin group", action, found.user)
        raise ProjectError(
            ErrorKind.POLICY_DENIED,
            f"{action} is for the workspace's admins: members of the Authentik group {group}",
        )
    _log.info("admin action: %s by %s (%s login)", action, found.user, found.login)


async def me(request: web.Request) -> web.Response:
    found = identity(request)
    group = request.app.get(ADMIN_GROUP)
    viewer = Viewer(user=found.user, login=found.login, admin=found.admin, admin_group=group)
    return web.json_response(viewer.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
