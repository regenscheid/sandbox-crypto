# SPDX-License-Identifier: MIT
"""Where the recorded runs' time went: model calls, tools by tool, waiting on the user
or on approvals, and the rest, for ``sandbox-science-harness tool-mix``.

Read from what the runs recorded, never estimated. A tool record's ``timing`` gives
when its call entered the toolbox and how long it took (``Toolbox.call``), and a
connector call held for the user's answer says how long it waited
(``approval_wait_ms``); a model call's observed usage gives its ``duration_ms``
(``continuity.models``). ``ask_user`` is time waiting on the user, and an approval wait
is taken out of its call's tool time. The rest is a run's span, from its start to its
end, less those: the harness's own work, such as folds' preparation and saving. Calls
that ran at once (read-class calls of one reply) are each counted whole, so tool time
can exceed the time that passed, and the rest is then 0. A record from before timing
existed counts its call as untimed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, cast

from .control import ControlStore
from .question_tool import ASK_USER

TIMING_KEYS: Final = ("timing", "approval_wait_ms")
"""A tool record's keys that time its call, which the run's tool event repeats."""

_TOOL_FIELDS: Final = (
    "json_extract({0},'$.tool'),json_extract({0},'$.timing.duration_ms'),"
    "json_extract({0},'$.approval_wait_ms'),json_extract({0},'$._record_storage.message')"
)


@dataclass
class RunTiming:
    """Milliseconds by where they went, over one run or several."""

    span_ms: int = 0
    """The runs' time from start to end, each run's own."""
    model_ms: int = 0
    model_calls: int = 0
    untimed_model_calls: int = 0
    tool_ms: dict[str, int] = field(default_factory=dict[str, int])
    """Each tool's calls' time, waiting on approvals left out."""
    tool_calls: int = 0
    untimed_tool_calls: int = 0
    user_ms: int = 0
    """``ask_user``'s calls: the run waiting for the user's answer."""
    approval_ms: int = 0
    """Connector calls held for the user's approval."""

    @property
    def rest_ms(self) -> int:
        spent = self.model_ms + sum(self.tool_ms.values()) + self.user_ms + self.approval_ms
        return max(0, self.span_ms - spent)

    def tool(self, name: str, duration: object, waited: object) -> None:
        """Count one tool call from its record's fields."""
        self.tool_calls += 1
        if not isinstance(duration, int):
            self.untimed_tool_calls += 1
            return
        if name == ASK_USER:
            self.user_ms += duration
            return
        held = min(waited, duration) if isinstance(waited, int) and waited > 0 else 0
        self.approval_ms += held
        self.tool_ms[name] = self.tool_ms.get(name, 0) + duration - held

    def model(self, duration: object) -> None:
        self.model_calls += 1
        if isinstance(duration, int):
            self.model_ms += duration
        else:
            self.untimed_model_calls += 1

    def add(self, other: RunTiming) -> None:
        self.span_ms += other.span_ms
        self.model_ms += other.model_ms
        self.model_calls += other.model_calls
        self.untimed_model_calls += other.untimed_model_calls
        for name, value in other.tool_ms.items():
            self.tool_ms[name] = self.tool_ms.get(name, 0) + value
        self.tool_calls += other.tool_calls
        self.untimed_tool_calls += other.untimed_tool_calls
        self.user_ms += other.user_ms
        self.approval_ms += other.approval_ms

    def summary(self) -> dict[str, Any]:
        """The ``--json`` form, in milliseconds."""
        return {
            "span_ms": self.span_ms,
            "model_ms": self.model_ms,
            "model_calls": self.model_calls,
            "untimed_model_calls": self.untimed_model_calls,
            "tools_ms": sum(self.tool_ms.values()),
            "tool_ms": dict(sorted(self.tool_ms.items(), key=lambda item: (-item[1], item[0]))),
            "tool_calls": self.tool_calls,
            "untimed_tool_calls": self.untimed_tool_calls,
            "user_ms": self.user_ms,
            "approval_ms": self.approval_ms,
            "rest_ms": self.rest_ms,
        }

    def line(self) -> str:
        """One line for people: the four parts, then the tools that took longest."""
        tools = sum(self.tool_ms.values())
        parts = [
            f"time {_duration(self.span_ms)}",
            f"model {_duration(self.model_ms)}",
            f"tools {_duration(tools)}",
            f"waiting {_duration(self.user_ms + self.approval_ms)}",
            f"rest {_duration(self.rest_ms)}",
        ]
        top = sorted(self.tool_ms.items(), key=lambda item: (-item[1], item[0]))[:3]
        if top:
            parts.append(", ".join(f"{name} {_duration(value)}" for name, value in top))
        untimed = self.untimed_model_calls + self.untimed_tool_calls
        if untimed:
            parts.append(f"{untimed} calls untimed")
        return " · ".join(parts)


def _duration(ms: int) -> str:
    seconds = ms // 1000
    if seconds < 60:
        return f"{ms / 1000:.1f}s"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds % 60:02d}s"


def _span(started: object, ended: object) -> int:
    if not isinstance(started, str) or not isinstance(ended, str):
        return 0
    try:
        begun, done = datetime.fromisoformat(started), datetime.fromisoformat(ended)
    except ValueError:
        return 0
    return max(0, round((done - begun).total_seconds() * 1000))


def _models(control: ControlStore, project: str, session: str, run: str, timing: RunTiming) -> bool:
    """The run's model calls from their intents' observed usage; whether it had any."""
    with control.read() as tx:
        rows = tx.sql.execute(
            "SELECT json_extract(usage,'$.duration_ms') FROM research_model_calls "
            "WHERE project=? AND session=? AND run=? AND usage IS NOT NULL",
            (project, session, run),
        ).fetchall()
    for (duration,) in rows:
        timing.model(duration)
    return bool(rows)


def scan(control: ControlStore, project: str, session: str, run: str) -> RunTiming:
    """One recorded run's time, from its record, its tool records and its model calls'
    intents (or, for a run without intents, its record's usage)."""
    timing = RunTiming()
    with control.read() as tx:
        db = tx.sql
        found = db.execute(
            "SELECT json_extract(record,'$.started_at'),json_extract(record,'$.ended_at'),"
            "json_extract(record,'$.usage') FROM runs WHERE project_id=? AND session_id=? "
            "AND name=?",
            (project, session, run),
        ).fetchone()
        rows = db.execute(
            f"SELECT {_TOOL_FIELDS.format('item')} FROM run_collections "  # noqa: S608 - fixed SQL
            "WHERE project=? AND session=? AND run=? AND collection='tools' ORDER BY position",
            (project, session, run),
        ).fetchall()
        if not rows:
            rows = db.execute(
                f"SELECT {_TOOL_FIELDS.format('u.value')} FROM runs r,"  # noqa: S608 - fixed SQL
                "json_each(r.record,'$.tools') u WHERE r.project_id=? AND r.session_id=? "
                "AND r.name=? ORDER BY u.key",
                (project, session, run),
            ).fetchall()
        for name, duration, waited, message in rows:
            if message is not None and duration is None:
                full = db.execute(
                    f"SELECT {_TOOL_FIELDS.format('record')} FROM archive_tool_records "  # noqa: S608
                    "WHERE project=? AND message_id=?",
                    (project, message),
                ).fetchone()
                if full is not None:
                    name, duration, waited, _ = full
            timing.tool(str(name), duration, waited)
    if found is not None:
        timing.span_ms = _span(found[0], found[1])
    if not _models(control, project, session, run, timing) and found is not None and found[2]:
        for usage in cast(list[object], json.loads(found[2])):
            entry: Mapping[str, object] = (
                cast(Mapping[str, object], usage) if isinstance(usage, dict) else {}
            )
            timing.model(entry.get("duration_ms"))
    return timing


def scan_events(control: ControlStore, project: str, session: str, run: str) -> RunTiming:
    """One run's time from its events, for a run with no kept record: its span from its
    first event to its last, its tools from its ``tool`` events, its model calls from
    their intents."""
    timing = RunTiming()
    with control.read() as tx:
        first, last = tx.sql.execute(
            "SELECT min(at),max(at) FROM run_events WHERE project_id=? AND session_id=? AND run=?",
            (project, session, run),
        ).fetchone()
        rows = tx.sql.execute(
            "SELECT json_extract(data,'$.name'),json_extract(data,'$.timing.duration_ms'),"
            "json_extract(data,'$.approval_wait_ms') FROM run_events WHERE project_id=? "
            "AND session_id=? AND run=? AND kind='tool' ORDER BY seq",
            (project, session, run),
        ).fetchall()
    timing.span_ms = _span(first, last)
    for name, duration, waited in rows:
        # An older event may not name its tool; only a timed call needs the name.
        timing.tool(name if isinstance(name, str) else "unnamed", duration, waited)
    _models(control, project, session, run, timing)
    return timing
