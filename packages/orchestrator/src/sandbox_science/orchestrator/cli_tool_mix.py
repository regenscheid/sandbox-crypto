# SPDX-License-Identifier: MIT
"""``sandbox-science-harness tool-mix``: how the recorded runs of a state directory
computed, per session and in total (docs/specs/tools.md, Kernels 13).

It reads the control store's recorded runs through a read-only connection, with no
service running, and counts their tool calls with the classifier the live summary uses
(``tool_mix``), so runs recorded before the summary existed are measured too. A run that
kept no record (cut short, such as by the loss of its agent VM) is counted from its
events, and the output says how many runs were. It writes nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from sandbox_science.contracts import ErrorKind

from .projects import ProjectError, ProjectStore
from .tool_mix import CLASSIFIER, ToolMix, scan, scan_events, unkept_runs


def add_parser(commands: Any) -> None:
    command = commands.add_parser(
        "tool-mix",
        help="count the recorded runs' kernel cells, shell calls (and those that ran "
        "Python), jobs and kernel restarts, per session and in total; read-only",
    )
    command.add_argument("--state-dir", type=Path, required=True)
    command.add_argument("--project", help="only this project's sessions")
    command.add_argument("--session", help="only this session")
    command.add_argument("--json", action="store_true", help="print the counts as JSON")


def tool_mix(args: argparse.Namespace) -> int:
    try:
        with contextlib.closing(ProjectStore(args.state_dir, readonly=True)) as store:
            found = measure(store, args.project, args.session)
    except ProjectError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(found, indent=2))
        return 0
    for row in found["sessions"]:
        print(f"{row['project']}/{row['session']}: {_counted(row)} · {_line(row)}")
    print(f"total: {_counted(found['total'])} · {_line(found['total'])}")
    return 0


def measure(store: ProjectStore, project: str | None, session: str | None) -> dict[str, Any]:
    """The counts of every run, by session, oldest session name first, with their total:
    the ``--json`` form. A run with a kept record is counted from its tool records; one
    that wrote events and kept none, from its events (``runs_from_events`` of the runs)."""
    projects = [project] if project is not None else store.project_ids()
    sessions: list[dict[str, Any]] = []
    total, total_runs, total_events = ToolMix(), 0, 0
    for project_id in projects:
        names = sorted(store.get(project_id).sessions)
        if session is not None:
            if project is not None and session not in names:
                raise _absent(project_id, session)
            names = [name for name in names if name == session]
        for session_id in names:
            mix, runs = ToolMix(), 0
            for entry in store.iter_runs(project_id, session_id):
                mix.add(scan(store.control, project_id, session_id, entry.name))
                runs += 1
            unkept = unkept_runs(store.control, project_id, session_id)
            for name in unkept:
                mix.add(scan_events(store.control, project_id, session_id, name))
            total.add(mix)
            total_runs += runs + len(unkept)
            total_events += len(unkept)
            sessions.append(
                {
                    "project": project_id,
                    "session": session_id,
                    "runs": runs + len(unkept),
                    "runs_from_events": len(unkept),
                    **mix.summary(),
                }
            )
    return {
        "classifier": CLASSIFIER,
        "sessions": sessions,
        "total": {"runs": total_runs, "runs_from_events": total_events, **total.summary()},
    }


def _absent(project: str, session: str) -> ProjectError:
    return ProjectError(ErrorKind.NOT_FOUND, f"project {project!r} has no session {session!r}")


def _runs(count: int) -> str:
    return f"{count} {'run' if count == 1 else 'runs'}"


def _counted(counts: dict[str, Any]) -> str:
    """The runs, and how many of them were counted from events, when any were."""
    text = _runs(counts["runs"])
    if counts["runs_from_events"]:
        text += f" · {_runs(counts['runs_from_events'])} from events"
    return text


def _line(counts: dict[str, Any]) -> str:
    mix = ToolMix(**{key: counts[key] for key in ToolMix().summary() if key != "classifier"})
    return mix.line()
