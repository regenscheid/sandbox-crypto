# SPDX-License-Identifier: MIT
"""One project's monitor figures for the workspace's Monitor view (D103), counted by code.

The policy middleware's event store holds its records of the agent's MCP calls straight
to a server (``mcp-call``), research code's escalated, approved and alerted HTTP and git
requests, the permission requests (``escalation``) and their answers (``approval``,
``denial``), and the MCP proxy's records of the same kinds where they reach that store.
The worker reads them newest first, at most ``MAX_SCANNED`` records and no further back
than the window, and counts:

- **calls** by effect class and destination (an MCP server, or a host for research
  code's requests): allowed, escalated (each request once, however often it was
  retried), denied and held;
- **approvals**: the requests of each kind recorded in the window, by how they stand
  now, and the median wait for an answer;
- **holds** nobody has answered, by rule, however old (the ledger's own fold);
- **drift**: servers whose tool list drifted and is not accepted again for the project
  (the classification review's own reading);
- **inspection**: by server, MCP answers in which traffic inspection found a pattern,
  passed with an alert or blocked (``mcp-answer-alert``).

A record whose project is not the one asked for, or that cannot be read as its kind, is
skipped. Research code's requests name a sandbox, not a project: they count for the
project whose environment owns the sandbox, as the permission listing attributes them.
Nothing here writes, grants or answers anything.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, EnvironmentInfo
from sandbox_science.contracts.monitor import (
    MAX_CALL_ROWS,
    MAX_HOLD_SESSIONS,
    MAX_SCANNED,
    MAX_SERVERS,
    WINDOWS,
    ApprovalCounts,
    ApprovalKind,
    CallRow,
    DriftedServer,
    EnvironmentTier,
    InspectionRow,
    MonitorRecords,
    MonitorWindow,
    OpenHolds,
    RecordSource,
    WorkerMonitor,
)
from sandbox_science.event_store import EventStoreError, Record, StoreReader
from sandbox_science.policy.approvals import APPROVAL, DENIAL, ESCALATION, RequestKey
from sandbox_science.policy.reasons import APPROVAL_REQUIRED, SESSION_HELD

from .classifications import ClassificationDesk
from .permissions import STORE_TIMEOUT, Permissions, PermissionStoreError

MCP_CALL: Final = "mcp-call"
APPROVED: Final = "approved"
ALERT: Final = "alert"
ANSWER_ALERT: Final = "mcp-answer-alert"
KINDS: Final = (MCP_CALL, ESCALATION, APPROVAL, DENIAL, APPROVED, ALERT, ANSWER_ALERT)
"""The record kinds the figures count."""
SOURCES: Final[tuple[RecordSource, ...]] = ("policy-middleware", "mcp-proxy")
DETECTION: Final = "detect."
_KIND_ORDER: Final[tuple[ApprovalKind, ...]] = ("mcp", "network", "packages", "hold")
_SUFFIX: Final = re.compile(r"_r[0-9]{1,18}$")
_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HOST: Final = re.compile(r"^[A-Za-z0-9.:\[\]-]{1,253}$")


def _text(value: object, pattern: re.Pattern[str] = _NAME) -> str | None:
    """A name from a record, as it is when it is one; else nothing."""
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _time(text: str) -> datetime | None:
    try:
        found = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo is not None else None


@dataclass(slots=True)
class _Row:
    allowed: int = 0
    escalated: int = 0
    denied: int = 0
    held: int = 0


@dataclass(slots=True)
class _Request:
    kind: ApprovalKind
    at: datetime
    timeout: timedelta


@dataclass(slots=True)
class _Tally:
    """The counts of one read, filled newest record first."""

    project: str
    owners: Mapping[str, str]
    """Each owned sandbox's project, by OpenShell sandbox ID."""
    rows: dict[tuple[str, str | None, str | None], _Row] = field(
        default_factory=dict[tuple[str, str | None, str | None], _Row]
    )
    requests: dict[int, _Request] = field(default_factory=dict[int, _Request])
    answers: dict[int, tuple[str, datetime, bool]] = field(
        default_factory=dict[int, tuple[str, datetime, bool]]
    )
    """Each request's earliest answer seen: its kind, time and whether it is a withdrawal."""
    sources: set[RecordSource] = field(default_factory=set[RecordSource])
    inspected: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    """Each server's answers with an alert, and those blocked or cut."""

    def row(self, effect: str, server: str | None = None, host: str | None = None) -> _Row:
        return self.rows.setdefault((effect, server, host), _Row())

    def owner(self, sandbox: object) -> str | None:
        return self.owners.get(sandbox) if isinstance(sandbox, str) else None

    def add(self, record: Record, at: datetime) -> None:
        try:
            data = record.data()
        except (EventStoreError, ValueError):
            return
        if record.kind in (APPROVAL, DENIAL):
            # An answer is newer than its request: met first, kept until an older one.
            seq = data.get("request_seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                self.answers[seq] = (record.kind, at, data.get("withdrawn") is True)
            return
        source = record.source if record.source in SOURCES else None
        counted = False
        if record.kind == MCP_CALL:
            counted = self._call(data)
        elif record.kind == ESCALATION:
            counted = self._escalation(record.seq, data, at)
        elif record.kind in (APPROVED, ALERT):
            counted = self._passed(data)
        elif record.kind == ANSWER_ALERT:
            counted = self._answer_alert(data)
        if counted and source is not None:
            self.sources.add(source)

    def _call(self, data: Mapping[str, object]) -> bool:
        """An MCP message, as the middleware or the proxy recorded it."""
        attribution = _object(data.get("attribution"))
        project = data.get("project", attribution.get("project"))
        if project != self.project:
            return False
        decision = _object(data.get("decision"))
        effect = _text(decision.get("effect")) or "none"
        row = self.row(effect, server=_text(data.get("server")))
        reason = data.get("reason_code")
        code = _SUFFIX.sub("", reason) if isinstance(reason, str) else ""
        if data.get("outcome") != "refused":
            row.allowed += 1
        elif code == SESSION_HELD:
            row.held += 1
        elif code != APPROVAL_REQUIRED:
            # A request waiting for its answer is counted once, by its escalation.
            row.denied += 1
        return True

    def _answer_alert(self, data: Mapping[str, object]) -> bool:
        """An MCP answer in which inspection found a pattern: alerted, or blocked."""
        server = _text(data.get("server"))
        if data.get("project") != self.project or server is None:
            return False
        counts = self.inspected.setdefault(server, [0, 0])
        counts[1 if data.get("action") == "block" else 0] += 1
        return True

    def inspection(self) -> list[InspectionRow]:
        rows = [
            InspectionRow(server=server, alerts=alerts, blocks=blocks)
            for server, (alerts, blocks) in self.inspected.items()
        ]
        rows.sort(key=lambda row: (-(row.alerts + row.blocks), row.server))
        return rows[:MAX_SERVERS]

    def _escalation(self, seq: int, data: Mapping[str, object], at: datetime) -> bool:
        key = RequestKey.from_payload(_object(data.get("target")))
        if key is None:
            return False
        fields = key.fields()
        if key.kind == "mcp" or (key.kind == "hold" and fields["project_id"]):
            project = fields["project_id"]
        else:
            project = self.owner(fields["sandbox_id"])
        if project != self.project:
            return False
        terms = _object(data.get("escalation"))
        timeout = terms.get("timeout_seconds")
        seconds = timeout if isinstance(timeout, int) and not isinstance(timeout, bool) else 0
        kind: ApprovalKind
        if key.kind in ("mcp", "hold"):
            kind = cast(ApprovalKind, key.kind)
        else:
            kind = "packages" if key.kind == "packages" or "package" in data else "network"
        self.requests[seq] = _Request(kind, at, timedelta(seconds=max(0, min(seconds, 10**10))))
        rule = data.get("rule")
        detected = isinstance(rule, str) and rule.startswith(DETECTION)
        if key.kind == "mcp":
            row = self.row(_text(data.get("effect")) or "none", server=_text(fields["server"]))
        elif key.kind == "http":
            row = self.row(kind, host=_text(fields["host"], _HOST))
        else:
            return True  # a file set or a hold: an approval, not a call
        if detected:
            row.held += 1
        else:
            row.escalated += 1
        return True

    def _passed(self, data: Mapping[str, object]) -> bool:
        """Research code's request that went through on an approval or with an alert. An
        MCP call's such records repeat its ``mcp-call`` record, which counts it."""
        request = _object(data.get("request"))
        if "server" in data or not request:
            return False
        if self.owner(data.get("sandbox_id")) != self.project:
            return False
        kind = "packages" if "package" in data or data.get("grant") == "packages" else "network"
        self.row(kind, host=_text(request.get("host"), _HOST)).allowed += 1
        return True

    def calls(self) -> list[CallRow]:
        rows = [
            CallRow(
                effect=effect,
                server=server,
                host=host,
                allowed=row.allowed,
                escalated=row.escalated,
                denied=row.denied,
                held=row.held,
            )
            for (effect, server, host), row in self.rows.items()
        ]
        rows.sort(
            key=lambda item: (
                -(item.allowed + item.escalated + item.denied + item.held),
                item.server or "",
                item.host or "",
                item.effect,
            )
        )
        return rows[:MAX_CALL_ROWS]

    def approvals(self, now: datetime) -> list[ApprovalCounts]:
        counts = {kind: [0, 0, 0, 0] for kind in _KIND_ORDER}
        waits: dict[ApprovalKind, list[float]] = {kind: [] for kind in _KIND_ORDER}
        for seq, request in self.requests.items():
            found = self.answers.get(seq)
            index = 0
            if found is not None and not found[2]:
                index = 1 if found[0] == APPROVAL else 2
                waits[request.kind].append(max(0.0, (found[1] - request.at).total_seconds()))
            elif found is not None or now - request.at >= request.timeout:
                index = 3
            counts[request.kind][index] += 1
        return [
            ApprovalCounts(
                kind=kind,
                waiting=counts[kind][0],
                approved=counts[kind][1],
                denied=counts[kind][2],
                expired=counts[kind][3],
                median_wait_seconds=statistics.median(waits[kind]) if waits[kind] else None,
            )
            for kind in _KIND_ORDER
        ]


def _scan(
    records: Iterable[Record], project: str, owners: Mapping[str, str], since: datetime
) -> tuple[_Tally, int, bool]:
    """Count ``records``, newest first, back to ``since``: the tally, how many records
    were examined and whether the scan reached a record older than ``since``."""
    counted = _Tally(project, owners)
    scanned = 0
    for record in records:
        at = _time(record.time)
        if at is None:
            continue
        if at < since:
            return counted, scanned, True
        scanned += 1
        counted.add(record, at)
    return counted, scanned, False


class MonitorDesk:
    """The monitor figures' side of the worker: the event store and the environments."""

    def __init__(
        self,
        permissions: Permissions | None,
        environments: Callable[[Iterable[str] | None], list[EnvironmentInfo]],
        *,
        declared_tier: int | None,
        measured_tier: Callable[[str], int | None],
        classifications: ClassificationDesk | None = None,
        clock: Clock = SYSTEM_CLOCK,
        max_scanned: int = MAX_SCANNED,
    ) -> None:
        self.permissions = permissions
        self.environments = environments
        self.declared_tier = declared_tier
        self.measured_tier = measured_tier
        self.classifications = classifications
        self.clock = clock
        self.max_scanned = max_scanned

    def figures(self, project: str, window: MonitorWindow) -> WorkerMonitor:
        """The project's figures for the window ending now. Blocking."""
        now = self.clock.now()
        since = now - WINDOWS[window]
        records: MonitorRecords | None = None
        unavailable: str | None = None
        if self.permissions is None:
            unavailable = "the worker has no policy middleware event store"
        else:
            try:
                records = self._records(self.permissions, project, since, now)
            except PermissionStoreError as exc:
                unavailable = str(exc)[:300]
        return WorkerMonitor(
            project_id=project,
            window=window,
            since=since,
            at=now,
            records=records,
            unavailable=unavailable,
            environments=self._tiers(project),
        )

    def _records(
        self, permissions: Permissions, project: str, since: datetime, now: datetime
    ) -> MonitorRecords:
        owners = {
            info.sandbox_id: info.project_id for info in self.environments(None) if info.sandbox_id
        }
        if permissions.path.is_file():
            try:
                with StoreReader(permissions.path, timeout=STORE_TIMEOUT) as reader:
                    head = reader.head().seq
                    counted, scanned, reached = _scan(
                        reader.newest(self.max_scanned, KINDS), project, owners, since
                    )
                    truncated = False
                    if head > self.max_scanned and not reached:
                        # The oldest record the cap let in: is the window older still?
                        first = next(reader.records(after=head - self.max_scanned), None)
                        at = _time(first.time) if first is not None else None
                        truncated = at is None or at >= since
            except EventStoreError as exc:
                raise PermissionStoreError(f"the event store cannot be read: {exc}") from exc
        else:
            counted, scanned, truncated = _Tally(project, owners), 0, False
        return MonitorRecords(
            sources=[source for source in SOURCES if source in counted.sources],
            scanned=scanned,
            truncated=truncated,
            calls=counted.calls(),
            approvals=counted.approvals(now),
            holds=self._holds(permissions, project, owners),
            drift=self._drift(project),
            inspection=counted.inspection(),
        )

    @staticmethod
    def _holds(
        permissions: Permissions, project: str, owners: Mapping[str, str]
    ) -> list[OpenHolds]:
        """Every hold of the project nobody answered, by rule: the ledger's own fold."""
        permissions.refresh()
        by_rule: dict[str, OpenHolds] = {}
        for request in permissions.unanswered():
            if request.key.kind != "hold":
                continue
            fields = request.key.fields()
            owner = fields["project_id"] or owners.get(str(fields["sandbox_id"]))
            if owner != project:
                continue
            rule = f"{DETECTION}{fields['rule']}"[:128]
            found = by_rule.get(rule) or OpenHolds(rule=rule, count=0)
            session = _text(fields["session"])
            sessions = list(found.sessions)
            if session and session not in sessions and len(sessions) < MAX_HOLD_SESSIONS:
                sessions.append(session)
            oldest = request.recorded
            if found.oldest_at is not None and found.oldest_at < oldest:
                oldest = found.oldest_at
            by_rule[rule] = OpenHolds(
                rule=rule, count=found.count + 1, sessions=sessions, oldest_at=oldest
            )
        return [by_rule[rule] for rule in sorted(by_rule)][:64]

    def _drift(self, project: str) -> list[DriftedServer]:
        """Servers whose drift still holds for the project, as the review view reads them."""
        if self.classifications is None:
            return []
        return [
            DriftedServer(server=item.server, at=item.drift.at)
            for item in self.classifications.in_force(project)
            if item.drift is not None and item.drift.held
        ]

    def _tiers(self, project: str) -> list[EnvironmentTier]:
        return [
            EnvironmentTier(
                environment_id=info.environment_id,
                kind=info.kind,
                profile=info.profile,
                state=info.state,
                declared_tier=self.declared_tier,
                tier=self.measured_tier(info.environment_id),
                job_id=info.job_id,
            )
            for info in self.environments([project])
        ]
