# SPDX-License-Identifier: MIT
"""Discovery: the skills that match what the user asked, shown before the model answers
(docs/specs/skills.md, SK-35 to SK-37, SK-21).

Before a run's first model call the run's task is scored against its skills, and so is
each user message that arrives during the run, before the call that carries it; the call
waits for the score at most ``WAIT_SECONDS``. A message under ``MIN_WORDS`` distinct words
is not scored. A skill is shown when it shares at least two of the message's words, or
all of a two-word message, and scores at least half the best; at most ``MAX_SHOWN`` are
shown, by name and description, in a notice framed by ``notices.SKILLS``. A skill the run
was shown or has loaded is not shown again. Nothing is blocked and no load is forced
(SK-36): the model decides. The run's record keeps each notice: what it answered, the
skills shown with their revisions, and how the scoring went.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Final

from sandbox_science.orchestrator.hooks import Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.notices import SKILLS

from .store import Entry
from .tool import SkillTool, rank, words

_log = logging.getLogger(__name__)

WAIT_SECONDS: Final = 5.0
"""How long a model call waits for the score (question 12)."""
MAX_SHOWN: Final = 5
MIN_WORDS: Final = 3


def matches(tool: SkillTool, text: str, skip: set[str]) -> list[Entry]:
    """The run's skills that match ``text``, best first, without those in ``skip``."""
    query = words(text)
    if len(set(query)) < MIN_WORDS:
        return []
    ranked = [
        (entry, score)
        for entry, matched, score in rank(tool.skills.entries, query)
        if len(matched) >= min(2, len(set(query)))
    ]
    if not ranked:
        return []
    best = ranked[0][1]
    shown = [entry for entry, score in ranked if score >= best / 2 and entry.qualified not in skip]
    return shown[:MAX_SHOWN]


class SkillDiscovery(TurnHook):
    """One run's discovery notices: for its task at the start, and, through the user's
    messages hook, for each message that arrives (``about``)."""

    name = "skills"

    def __init__(self, tool: SkillTool, task: str | None) -> None:
        self.tool, self.task = tool, task
        self.shown: set[str] = set()
        self.records: list[dict[str, Any]] = []

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point != "start" or not self.task:
            return ()
        found = await self.about([self.task], answered="task")
        return () if found is None else (found,)

    async def about(self, texts: Sequence[str], answered: str = "message") -> Notice | None:
        """The notice for ``texts``, or ``None`` when no skill matches or scoring did not
        finish in time; either way the record says what happened."""
        skip = self.shown | set(self.tool.loaded)
        record: dict[str, Any] = {"answered": answered, "scoring": "lexical"}
        try:
            found = await asyncio.wait_for(
                asyncio.to_thread(matches, self.tool, "\n".join(texts), skip), WAIT_SECONDS
            )
        except TimeoutError:
            found, record["scoring"] = [], "timed_out"
        except Exception:  # a failed score sends the call without a notice
            _log.exception("scoring the skills for discovery failed")
            found, record["scoring"] = [], "failed"
        record["skills"] = [{"id": e.qualified, "revision": e.revision} for e in found]
        self.records.append(record)
        if not found:
            return None
        self.shown.update(entry.qualified for entry in found)
        payload = {"skills": [{"id": e.qualified, "description": e.description} for e in found]}
        return Notice(self.name, (f"shown-{len(self.records)}",), SKILLS.text, payload)

    def summary(self) -> object | None:
        """Each discovery: what it answered, the skills shown, and how scoring went."""
        return {"discoveries": self.records} if self.records else None
