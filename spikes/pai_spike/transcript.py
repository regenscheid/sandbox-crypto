# SPDX-License-Identifier: MIT
"""A long session for the context-management replay, built from recorded lab runs.

Each lab run record keeps exactly what its model calls were sent: the system message,
the session's earlier conversation (earlier tasks and final answers), the notices and
task of this run, and its tool calls and results. Chaining several runs' own parts
gives one long conversation as if no history had been trimmed between them, which is
what compaction would have to handle. The control stores are copies of lab evidence,
read-only and outside git.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Message = dict[str, Any]


@dataclass
class Run:
    lab: str
    name: str
    task: str | None
    own: list[Message]
    system: str


@dataclass
class Session:
    system: str
    runs: list[Run] = field(default_factory=list[Run])

    @property
    def messages(self) -> list[Message]:
        return [{"role": "system", "content": self.system}] + [m for r in self.runs for m in r.own]


def _own_part(messages: list[Message], task: str | None) -> list[Message] | None:
    """This run's messages: from its first notice or task after the session's history."""
    if task is not None:
        hits = [i for i, m in enumerate(messages) if m["role"] == "user" and m["content"] == task]
        if not hits:
            return None
        t = hits[-1]
    else:
        # A run started by a comment: its first input is the first user message after
        # the last answer of the history (an assistant message with no tool calls that
        # is followed only by user messages up to the first tool call or answer).
        t = next(
            (i for i, m in enumerate(messages) if m["role"] == "assistant" and m.get("tool_calls")),
            len(messages),
        )
        while t > 1 and messages[t - 1]["role"] == "user":
            t -= 1
        return messages[t:]
    s = t
    while s > 1 and messages[s - 1]["role"] == "user":
        s -= 1
    return messages[s:]


def load(stores: list[tuple[str, Path]], role: str = "main") -> Session:
    runs: list[Run] = []
    for lab, path in stores:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = c.execute(
            "select name, record from runs where role = ? order by name", (role,)
        ).fetchall()
        for name, record in rows:
            rec = json.loads(record)
            messages = rec["messages"]
            own = _own_part(messages, rec.get("task"))
            if not own:
                continue
            runs.append(Run(lab, name, rec.get("task"), own, messages[0]["content"]))
    system = max((r.system for r in runs), key=len)
    return Session(system, runs)


def tool_names(messages: list[Message]) -> list[str]:
    names: list[str] = []
    for m in messages:
        for call in m.get("tool_calls") or []:
            if call["function"]["name"] not in names:
                names.append(call["function"]["name"])
    return names


if __name__ == "__main__":
    import sys

    labs = Path(sys.argv[1])
    session = load([(n, labs / f"control-{n}.sqlite3") for n in ("39", "40", "41")])
    total = session.messages
    print("runs", len(session.runs), "messages", len(total), "chars", len(json.dumps(total)))
    for r in session.runs:
        print(r.lab, r.name[:24], len(r.own), len(json.dumps(r.own)), repr((r.task or "")[:60]))
