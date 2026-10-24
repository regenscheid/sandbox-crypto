# SPDX-License-Identifier: MIT
"""Network and package permission requests the worker lists and answers for the workspace
(D97): the policy middleware's escalations of research code's HTTP and git requests, and
the provisioning operations' file-set requests, in the middleware's event store.

The worker reads them for an operator client's projects only, never with a request's
body or a package file's contents: what a card shows is the summary the middleware or
the worker recorded. docs/POLICY-MIDDLEWARE.md, "Permission requests", has the rules.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from .model import Contract, Identifier

MAX_LISTED: Final = 256
"""Requests one listing carries, oldest first."""
MAX_ANSWER_HOURS: Final = 24 * 30
Text = Annotated[str, StringConstraints(max_length=600)]
CallerKind = Literal["agent", "code", "user"]
"""Who asked: the agent (through a tool), research code, or the user."""


class HttpSummary(Contract):
    """An HTTP or git request as its card shows it: never the body."""

    method: Text
    host: Text
    port: Annotated[int, Field(ge=0, le=65535)]
    path: Text
    size: Annotated[int, Field(ge=0)]
    """The body's size in bytes."""
    content_type: Text | None = None
    fields: dict[Text, Text] = Field(default_factory=dict[str, str], max_length=20)
    """A JSON body's top-level fields, each value truncated."""


class PackageSource(Contract):
    """Where escalated package files come from, as package grants compare them: the
    ecosystem and, for conda, the channel."""

    ecosystem: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    channel: Text | None = None


class PackagesAsked(Contract):
    """What a package request asks to install."""

    sources: list[PackageSource] = Field(max_length=64)
    files: Annotated[int, Field(ge=0)]
    """How many package files it lets download."""
    asked: Text | None = None
    """What was asked for, such as ``Conda: sage``."""
    purpose: Text | None = None
    """Why, in the agent's words."""
    package: Text | None = None
    """For one file the package cache serves, the package it is of."""


class WorkerPermissionRequest(Contract):
    """One unanswered network or package permission request of a project's sandbox."""

    seq: Annotated[int, Field(ge=1)]
    """Its sequence number in the middleware's event store."""
    kind: Literal["network", "packages"]
    project_id: Identifier
    environment_id: Identifier
    session_id: Identifier | None = None
    """The session that asked, when the request names one (a provisioning operation)."""
    caller_kind: CallerKind
    policy: Text
    """The policy revision it was escalated under."""
    rule: Text | None = None
    reason: Text | None = None
    max_grant: Literal["none", "once", "session", "project"]
    """The widest grant the user may give (D97)."""
    http: HttpSummary | None = None
    packages: PackagesAsked | None = None
    recorded_at: AwareDatetime
    expires_at: AwareDatetime
    timed_out: bool
    on_timeout: Literal["deny", "alert"]


class WorkerPermissionListing(Contract):
    requests: list[WorkerPermissionRequest] = Field(max_length=MAX_LISTED)


class WorkerPermissionAnswer(Contract):
    """The user's answer, which the workspace relays: approve or deny, with the ledger's
    scope (``request`` or ``host`` for a network request, ``files`` for a file set) and
    for an approval how long it lasts."""

    approve: bool
    scope: Literal["request", "host", "files"]
    hours: Annotated[float, Field(gt=0, le=MAX_ANSWER_HOURS)] | None = None


class WorkerPermissionAnswered(Contract):
    seq: Annotated[int, Field(ge=1)]
    answer: Annotated[int, Field(ge=1)]
    """The answer's own sequence number in the store."""
    kind: Literal["approval", "denial"]
