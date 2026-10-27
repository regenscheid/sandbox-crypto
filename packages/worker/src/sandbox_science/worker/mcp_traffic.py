# SPDX-License-Identifier: MIT
"""A project's inspected MCP traffic, read from the policy middleware's event store.

The middleware records every MCP call it decides on the direct path (``mcp-call``, with
the call's arguments when it inspects traffic) and the answer to every admitted call
(``mcp-answer``), redacted and bounded before they are written (docs/POLICY-MIDDLEWARE.md,
"MCP traffic inspection"). This reads them oldest first after a record, for one project
and optionally one session, run or tool, and joins each call to its answer by
OpenShell's request id and the sandbox; an answer whose call is on an earlier page comes
on its own (``late_answers``). A record that cannot be read as its kind is
skipped. Nothing here writes; what it returns is the agent's and servers' text, which
the workspace shows as data.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from sandbox_science.contracts.mcp_traffic import (
    MAX_TRAFFIC_CALLS,
    MAX_TRAFFIC_SCAN,
    TrafficAnswer,
    TrafficCall,
    WorkerMcpTraffic,
)
from sandbox_science.event_store import EventStoreError, Record, StoreReader

from .permissions import STORE_TIMEOUT, PermissionStoreError

CALL: Final = "mcp-call"
ANSWER: Final = "mcp-answer"
KINDS: Final = (CALL, ANSWER)


@dataclass(frozen=True, slots=True)
class TrafficQuery:
    """Which calls a page holds."""

    project: str
    session: str | None = None
    run: str | None = None
    tool: str | None = None
    after: int = 0

    def matches(self, data: Mapping[str, object]) -> bool:
        return (
            data.get("project") == self.project
            and (self.session is None or data.get("session") == self.session)
            and (self.run is None or data.get("run") == self.run)
            and (self.tool is None or data.get("tool") == self.tool)
        )


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _call(record: Record, data: Mapping[str, object]) -> TrafficCall | None:
    decision = _object(data.get("decision"))
    try:
        return TrafficCall.model_validate(
            {
                "seq": record.seq,
                "at": record.time.replace("Z", "+00:00"),
                "request_id": data.get("request_id"),
                "session": data.get("session"),
                "run": data.get("run"),
                "server": data.get("server"),
                "method": data.get("method"),
                "tool": data.get("tool"),
                "decision": {
                    "outcome": data.get("outcome"),
                    "action": decision.get("action"),
                    "layer": decision.get("layer"),
                    "rule": decision.get("rule"),
                    "effect": decision.get("effect"),
                    "reason_code": data.get("reason_code"),
                    "permission_request": data.get("permission_request"),
                },
                "arguments": data.get("arguments"),
            }
        )
    except ValidationError:
        return None


def _answer(record: Record, data: Mapping[str, object]) -> TrafficAnswer | None:
    excerpt = _object(data.get("excerpt")) if data.get("excerpt") is not None else None
    try:
        return TrafficAnswer.model_validate(
            {
                "seq": record.seq,
                "at": record.time.replace("Z", "+00:00"),
                "request_id": data.get("request_id"),
                "status": data.get("status"),
                "inspected": data.get("inspected", True),
                "reason": data.get("reason"),
                "content_type": data.get("content_type"),
                "mode": data.get("mode"),
                "bytes": data.get("bytes"),
                "complete": data.get("complete"),
                "sha256": data.get("sha256"),
                "excerpt": excerpt.get("text") if excerpt is not None else None,
                "excerpt_truncated": excerpt.get("truncated") is True if excerpt else False,
                "note": data.get("note"),
                "events": data.get("events"),
                "findings": data.get("findings", []),
                "more_findings": data.get("more_findings", 0),
                "cut_after_result": data.get("cut_after_result") is True,
                "decision": data.get("decision"),
            }
        )
    except ValidationError:
        return None


def page(records: Iterable[Record], query: TrafficQuery) -> WorkerMcpTraffic:
    """One page from ``records``, oldest first after ``query.after``: the calls in the
    records it covers, each with its answer when that is among them too, and the answers
    to calls of earlier pages (``late_answers``). The page ends at the last record it
    covers (``next_after``), so every call and every answer is in exactly one page."""
    calls: list[TrafficCall] = []
    late: list[TrafficAnswer] = []
    waiting: dict[tuple[str, str], int] = {}
    """The index of each listed call still waiting for its answer, by request and sandbox."""
    scanned = last = 0
    more = False
    for record in records:
        if scanned >= MAX_TRAFFIC_SCAN or len(calls) + len(late) >= MAX_TRAFFIC_CALLS:
            more = True
            break
        scanned += 1
        last = record.seq
        try:
            data = record.data()
        except (EventStoreError, ValueError):
            continue
        if not query.matches(data):
            continue
        key = (str(data.get("request_id")), str(data.get("sandbox_id")))
        if record.kind == CALL:
            found = _call(record, data)
            if found is None:
                continue
            calls.append(found)
            if found.decision.outcome == "admitted":
                waiting[key] = len(calls) - 1
        elif record.kind == ANSWER:
            answer = _answer(record, data)
            if answer is None:
                continue
            index = waiting.pop(key, None)
            if index is None:
                late.append(answer)  # its call is on an earlier page
            else:
                calls[index] = calls[index].model_copy(update={"answer": answer})
    return WorkerMcpTraffic(
        project_id=query.project,
        calls=calls,
        late_answers=late,
        next_after=last if more else None,
        scanned=scanned,
    )


def read_traffic(path: Path, query: TrafficQuery) -> WorkerMcpTraffic:
    """A page of the project's inspected MCP traffic from the store at ``path``. Blocking;
    raises PermissionStoreError when the store cannot be read."""
    if not path.is_file():
        return WorkerMcpTraffic(project_id=query.project, calls=[], scanned=0)
    try:
        with StoreReader(path, timeout=STORE_TIMEOUT) as reader:
            return page(reader.records(after=query.after, kinds=KINDS), query)
    except EventStoreError as exc:
        raise PermissionStoreError(f"the event store cannot be read: {exc}") from exc
