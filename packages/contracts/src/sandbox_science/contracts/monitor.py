# SPDX-License-Identifier: MIT
"""A project's monitor figures, as the worker computes them for an operator client (D103).

The worker reads the policy middleware's event store (and the MCP proxy's records, where
they reach that store) newest first, for one window or at most ``MAX_SCANNED`` records,
and counts by code what happened: calls by effect class and destination, permission
requests by kind and how they ended, open holds by rule and servers whose tool list
drifted. Beside them, the project's environments with their isolation tiers, from the
worker's own records. Counts only: no request's body, call's arguments or agent text.
docs/WORKER.md, "Monitor figures", has the rules.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from .environments import EnvironmentState
from .model import Contract, Counter, Identifier
from .worker import IsolationTier

MonitorWindow = Literal["24h", "7d"]
WINDOWS: Final[dict[str, timedelta]] = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
"""How far back each window reaches from the moment of the read."""
MAX_SCANNED: Final = 100_000
"""The most records one read examines, newest first, whatever the window."""
MAX_CALL_ROWS: Final = 500
"""Rows of call counts one answer carries, the busiest first."""
MAX_HOLD_SESSIONS: Final = 20
MAX_ENVIRONMENTS: Final = 1000
MAX_SERVERS: Final = 256

Name = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Host = Annotated[str, StringConstraints(min_length=1, max_length=253)]
ApprovalKind = Literal["mcp", "network", "packages", "hold"]
RecordSource = Literal["policy-middleware", "mcp-proxy"]


class CallRow(Contract):
    """One destination's calls of one effect class within the window.

    An MCP server's row counts its ``mcp-call`` records (every message, ``effect``
    ``none`` for one that is not a classified tool call) and its escalations; research
    code's HTTP and git requests have rows by host, ``effect`` ``network`` (or
    ``packages`` for a download from the package cache), with only what the middleware
    records of them: escalations, detections' holds and requests that went through on an
    approval or with an alert."""

    effect: Name
    server: Name | None = None
    host: Host | None = None
    allowed: Counter = 0
    """Went through: allowed, with an alert, or on an approval."""
    escalated: Counter = 0
    """Permission requests recorded (each once, whatever its retries)."""
    denied: Counter = 0
    """Refused: by the policy, a denial, a request nobody answered in time, or a check."""
    held: Counter = 0
    """Refused while a hold paused the session, or held for approval by a detection rule
    (an action-like URL, a drifted tool list)."""


class ApprovalCounts(Contract):
    """The permission requests of one kind recorded within the window, by how they stand
    now: waiting for an answer, approved, denied, or expired (unanswered past their
    deadline, or withdrawn by the worker as their operation ended)."""

    kind: ApprovalKind
    waiting: Counter = 0
    approved: Counter = 0
    denied: Counter = 0
    expired: Counter = 0
    median_wait_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    """From the request to its first answer, over the answered ones; null when none was."""


class OpenHolds(Contract):
    """Holds of a rule that nobody has cleared or stopped, however old (D103)."""

    rule: Name
    """``detect.<rule>``, as the hold's record names it."""
    count: Counter
    sessions: list[Identifier] = Field(
        default_factory=list[Identifier], max_length=MAX_HOLD_SESSIONS
    )
    """The sessions held, when a hold names one (an agent's); research code's holds a
    sandbox, which names none."""
    oldest_at: AwareDatetime | None = None


class DriftedServer(Contract):
    """An MCP server whose tool list drifted from its pin and is not accepted again for
    the project, so its non-read calls ask (D103)."""

    server: Name
    at: AwareDatetime | None = None


class InspectionRow(Contract):
    """One MCP server's answers in which inspection found a pattern within the window:
    those passed on with an alert, and those blocked or cut (docs/POLICY-MIDDLEWARE.md,
    "MCP traffic inspection")."""

    server: Name
    alerts: Counter = 0
    blocks: Counter = 0
    uninspected: Counter = 0
    """Passed unread, every response thread being busy: no pattern applied to them."""


class MonitorRecords(Contract):
    """What the event store's records say for the window."""

    sources: list[RecordSource] = Field(max_length=2)
    """Whose records the read found: the middleware's, and the MCP proxy's when its
    records reach the same store."""
    scanned: Counter
    """Records examined, newest first."""
    truncated: bool
    """The scan stopped at ``MAX_SCANNED`` before the window's start: older calls in the
    window are not counted."""
    calls: list[CallRow] = Field(max_length=MAX_CALL_ROWS)
    approvals: list[ApprovalCounts] = Field(max_length=4)
    """Every kind, in the order mcp, network, packages, hold."""
    holds: list[OpenHolds] = Field(max_length=64)
    drift: list[DriftedServer] = Field(max_length=MAX_SERVERS)
    inspection: list[InspectionRow] = Field(
        default_factory=list[InspectionRow], max_length=MAX_SERVERS
    )
    """Alerts and blocks of MCP answers by server, the busiest first."""
    inspection_dropped: Counter = 0
    """The project's answers whose records the middleware dropped within the window, its
    record queue being full (``mcp-inspection-dropped``): plain answers only, never one
    with a finding, a block or no inspection."""
    inspection_enabled: bool | None = None
    """Whether MCP traffic inspection is on, as the middleware recorded when it last
    started (``mcp-inspection-state``); null when it never recorded."""


class EnvironmentTier(Contract):
    """One of the project's environments with its isolation tier (D43)."""

    environment_id: Identifier
    kind: Literal["session", "job", "child"]
    profile: Identifier
    state: EnvironmentState
    declared_tier: IsolationTier | None = None
    """The tier the worker's sandbox driver declares."""
    tier: IsolationTier | None = None
    """The tier in effect as the probe measured it when the environment's executor last
    opened; null when the worker has not opened one since it started."""
    job_id: Identifier | None = None


class WorkerMonitor(Contract):
    """``GET /v1/monitor?project=P&window=24h|7d``: one project's monitor figures."""

    project_id: Identifier
    window: MonitorWindow
    since: AwareDatetime
    at: AwareDatetime
    records: MonitorRecords | None
    """Null when the event store is not configured or cannot be read (``unavailable``)."""
    unavailable: Annotated[str, StringConstraints(max_length=300)] | None = None
    environments: list[EnvironmentTier] = Field(max_length=MAX_ENVIRONMENTS)
