# SPDX-License-Identifier: MIT
"""Permission requests the worker lists and answers for the workspace (D97): the policy
middleware's escalations of research code's HTTP and git requests and of the agent's MCP
calls straight to a server (D98), and the provisioning operations' file-set requests, in
the middleware's event store.

The worker reads them for an operator client's projects only, never with a request's
body, a call's arguments or a package file's contents: what a card shows is the summary
the middleware or the worker recorded. Any client may ask whether one MCP request is a
given call's and stands as a refusal said (``PermissionConfirmation``), for its own
projects: the harness's check of a refusal before it waits on it.
docs/POLICY-MIDDLEWARE.md, "Permission requests", has the rules.
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


class McpAsked(Contract):
    """An MCP call the middleware holds, as its card shows it: never the arguments."""

    server: Text
    method: Text
    tool: Text
    """Empty for a method other than ``tools/call``."""
    effect: Text | None = None
    """The call's effect class (D96)."""
    summary: dict[Text, Text] = Field(default_factory=dict[str, str], max_length=20)
    """The call's significant arguments, by kind (url, host, recipient, title, size, query)."""


class HeldCall(Contract):
    """One of the calls before a hold, as its card lists it: never a query or arguments."""

    method: Text
    host: Text
    target: Text
    """A URL's path, with its query's size only, or an MCP method and tool."""


class HoldAsked(Contract):
    """A hold of a session's outward calls by a detection rule (D103): what the rule saw.
    Clearing it resumes the calls; stopping it stops the session's runs."""

    rule: Text
    count: Annotated[int, Field(ge=0)]
    threshold: Annotated[int, Field(ge=0)]
    window_seconds: Annotated[int, Field(ge=0)]
    host: Text | None = None
    requests: list[HeldCall] = Field(default_factory=list[HeldCall], max_length=5)
    """The subject's latest calls, newest last."""


class WorkerPermissionRequest(Contract):
    """One unanswered permission request of a project: a sandbox's network or package
    request, an MCP call of the project's agent, or a hold of a session's calls."""

    seq: Annotated[int, Field(ge=1)]
    """Its sequence number in the middleware's event store."""
    kind: Literal["network", "packages", "mcp", "hold"]
    project_id: Identifier
    environment_id: Identifier | None = None
    """The environment whose sandbox asked; an MCP call names none."""
    session_id: Identifier | None = None
    """The session that asked, when the request names one (a provisioning operation)."""
    run: Text | None = None
    """The run whose MCP call it is, as the run service named it in its headers and the
    middleware recorded it (DA-4): attribution only. A delegated run's card names it."""
    caller_kind: CallerKind
    policy: Text
    """The policy revision it was escalated under."""
    rule: Text | None = None
    reason: Text | None = None
    max_grant: Literal["none", "once", "session", "project"]
    """The widest grant the user may give (D97)."""
    http: HttpSummary | None = None
    packages: PackagesAsked | None = None
    mcp: McpAsked | None = None
    hold: HoldAsked | None = None
    recorded_at: AwareDatetime
    expires_at: AwareDatetime
    timed_out: bool
    on_timeout: Literal["deny", "alert"]


class WorkerPermissionListing(Contract):
    requests: list[WorkerPermissionRequest] = Field(max_length=MAX_LISTED)


class WorkerPermissionAnswer(Contract):
    """The user's answer, which the workspace relays: approve or deny, with the ledger's
    scope (``request`` or ``host`` for a network request, ``files`` for a file set,
    ``call`` for an MCP call, ``session`` for a hold, which an approval clears) and for an
    approval how long it lasts."""

    approve: bool
    scope: Literal["request", "host", "files", "call", "session"]
    hours: Annotated[float, Field(gt=0, le=MAX_ANSWER_HOURS)] | None = None


class WorkerPermissionAnswered(Contract):
    seq: Annotated[int, Field(ge=1)]
    answer: Annotated[int, Field(ge=1)]
    """The answer's own sequence number in the store."""
    kind: Literal["approval", "denial"]


HeldReason = Literal["approval_required", "approval_denied", "escalation_timed_out"]
"""The refusals that name a permission request."""


class PermissionConfirmation(Contract):
    """Whether MCP permission request ``seq`` (the route's) is this call's and stands as a
    refusal of it said: the call's project, server, method, tool (empty for a method other
    than ``tools/call``) and the hex SHA-256 of its parameters, as the middleware keys it."""

    reason: HeldReason
    project_id: Identifier
    server: Text
    method: Text
    tool: Text
    arguments: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class PermissionConfirmed(Contract):
    confirmed: bool
    """True only when the request exists, is an MCP call of that project, and the ledger
    gives the call that refusal (``Approvals.confirms``)."""
