# SPDX-License-Identifier: MIT
"""A project's inspected MCP traffic, as the worker reads it for an operator client.

The policy middleware records each MCP call it decides on the direct path (D98) with its
arguments (``mcp-call``) and each answer to an admitted call (``mcp-answer``), both
bounded and redacted before they are written (docs/POLICY-MIDDLEWARE.md, "MCP traffic
inspection"). ``GET /v1/mcp-traffic?project=P[&session=S][&run=R][&tool=T][&after=N]``
pages through a project's calls oldest first, each with its answer when one is recorded.
Everything in it came from the agent or a server: it is data to show as text, never
instructions.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from .model import Contract, Counter, Identifier, Sha256

MAX_TRAFFIC_CALLS: Final = 20
"""Calls one page carries, oldest first."""
MAX_TRAFFIC_SCAN: Final = 100_000
"""Records one read examines; a page that stops there says so with ``next_after``."""
ANSWER_LOOKAHEAD: Final = 5_000
"""Records read past a full page's last call for the answers of its calls."""
MAX_RECORDED_TEXT: Final = 4 * 1024 * 1024
MAX_FINDINGS: Final = 32

Name = Annotated[str, StringConstraints(max_length=300)]
Text = Annotated[str, StringConstraints(max_length=MAX_RECORDED_TEXT)]
Note = Annotated[str, StringConstraints(max_length=600)]


class TrafficArguments(Contract):
    """A call's parameters as recorded: redacted JSON text, cut at the deployment's bound."""

    text: Text
    truncated: bool
    bytes: Counter
    """The parameters' full size, as sent."""
    sha256: Sha256
    """Of the parameters as sent, before redaction: the digest a permission request's key
    holds."""


class TrafficDecision(Contract):
    """How the middleware decided the call."""

    outcome: Literal["admitted", "refused"]
    action: Name | None = None
    """The policy's action (``allow``, ``alert``, ``escalate``, ``deny``), when it decided."""
    layer: Name | None = None
    rule: Name | None = None
    effect: Name | None = None
    reason_code: Name | None = None
    permission_request: Counter | None = None


class TrafficFinding(Contract):
    """A pattern found in an answer: where, and the redacted text around it."""

    name: Name
    action: Literal["alert", "block"]
    offset: Counter
    """In characters of the answer's inspected text (an event stream's data text)."""
    excerpt: Annotated[str, StringConstraints(max_length=2000)]


class AnswerDecision(Contract):
    """What inspection did with the answer: passed it on as it came, refused it whole
    (``blocked``) or cut its stream (``cut``), naming the pattern."""

    action: Literal["passed", "blocked", "cut"]
    pattern: Name | None = None
    reason_code: Name | None = None


class TrafficAnswer(Contract):
    """An answer as the middleware recorded it."""

    seq: Counter
    at: AwareDatetime
    status: Annotated[int, Field(ge=0, le=999)]
    content_type: Name | None = None
    mode: Literal["whole", "stream", "headers"]
    """How it was read: whole, piece by piece, or its head only (nothing to read)."""
    bytes: Counter
    complete: bool
    """Whether its last piece was read: false when the client or server left early."""
    sha256: Sha256 | None = None
    excerpt: Text | None = None
    """The recorded text: null for a head only, or past the run's bound (``note``)."""
    excerpt_truncated: bool = False
    note: Note | None = None
    events: Counter | None = None
    """An event stream's events."""
    findings: list[TrafficFinding] = Field(default_factory=list[TrafficFinding], max_length=32)
    more_findings: Counter = 0
    decision: AnswerDecision


class TrafficCall(Contract):
    """One MCP call with its arguments, decision and answer."""

    seq: Counter
    at: AwareDatetime
    request_id: Name
    session: Identifier | None = None
    run: Identifier | None = None
    server: Name | None = None
    method: Name | None = None
    tool: Name | None = None
    decision: TrafficDecision
    arguments: TrafficArguments | None = None
    """Null when the call could not be read, or was recorded before inspection."""
    answer: TrafficAnswer | None = None
    """Null for a refused call, or one whose answer is not (yet) recorded."""


class WorkerMcpTraffic(Contract):
    """``GET /v1/mcp-traffic``: a page of a project's inspected MCP calls, oldest first."""

    project_id: Identifier
    calls: list[TrafficCall] = Field(max_length=MAX_TRAFFIC_CALLS)
    next_after: Counter | None = None
    """Pass as ``after`` for the next page; null when this page reached the newest call."""
    scanned: Counter
    unavailable: Note | None = None
    """Why the event store could not be read; ``calls`` is then empty."""
