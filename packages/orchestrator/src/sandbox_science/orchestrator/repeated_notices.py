# SPDX-License-Identifier: MIT
"""Telling a run that its session's last record lost its deliveries (run-service RS-13).

A run whose record could be kept only as a minimal one, without the notice IDs it was
given (``finalization.minimal``), acknowledges none of them, so the next run is given them
again. That run is told so first, once, before any of them.
"""

from __future__ import annotations

from collections.abc import Sequence

from .control import ControlStore
from .hooks import SALVAGED, Notice, Point, RunView, TurnHook
from .notices import NOTICES_MAY_REPEAT


def repeated_after(control: ControlStore, project: str, session: str) -> str | None:
    """The session's latest saved run, when its record was kept without its deliveries;
    ``None`` otherwise."""
    with control.read() as tx:
        row = tx.sql.execute(
            "SELECT name, json_extract(record, ?) FROM runs WHERE project_id=? AND "
            "session_id=? ORDER BY name DESC LIMIT 1",
            (f"$.summaries.{SALVAGED}.notices_may_repeat", project, session),
        ).fetchone()
    return str(row[0]) if row is not None and row[1] else None


class NoticesMayRepeat(TurnHook):
    """The first notice of the run after one whose record lost its deliveries."""

    name = "notices_may_repeat"

    def __init__(self, previous: str) -> None:
        self.previous = previous
        self.told = False

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point != "start" or self.told:
            return ()
        self.told = True
        return (Notice(self.name, (), NOTICES_MAY_REPEAT.text, {"run": self.previous}),)
