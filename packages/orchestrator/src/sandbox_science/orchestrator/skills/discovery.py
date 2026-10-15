"""Discovery: the skills that match what the user asked, shown before the model answers
(docs/specs/skills.md, SK-35 to SK-37, SK-21).

Before a run's first model call the run's task is scored against its skills, and so is
each user message that arrives during the run, before the call that carries it; the call
waits for the score at most ``wait`` seconds of the run's clock. A message under
``MIN_WORDS`` distinct words is not scored. A skill is shown when it shares at least two
of the message's words and scores at least half the best; at most ``MAX_SHOWN`` are
shown, by name and description, in a notice framed by ``notices.SKILLS``. A skill the run
was shown, in a notice a model call saw, or has loaded is not shown again; one in a
notice the run dropped, as at a closing with no room for it, may be. Nothing is blocked
and no load is forced (SK-36): the model decides. The run's record keeps each notice: its
ID, which ``RunRecord.delivered`` names once a model call saw it, what it answered, the
skills shown with their revisions, and how the scoring went. Scoring runs in a thread,
pure and quick: one that outlasts the wait ends on its own, and nothing waits for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from typing import Any, Final, TypeVar

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock
from sandbox_science.orchestrator.hooks import Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.notices import SKILLS

from .store import Entry
from .tool import SkillTool, rank, words

_log = logging.getLogger(__name__)
T = TypeVar("T")

WAIT_SECONDS: Final = 5.0
"""How long a model call waits for the score by default (question 12)."""
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
        if len(matched) >= 2
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

    def __init__(
        self,
        tool: SkillTool,
        task: str | None,
        *,
        wait: float = WAIT_SECONDS,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.tool, self.task, self.wait, self.clock = tool, task, wait, clock
        self.shown: set[str] = set()
        """The skills in notices a model call saw."""
        self.pending: dict[str, tuple[str, ...]] = {}
        """Each notice not yet seen by a model call, and its skills."""
        self.records: list[dict[str, Any]] = []

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point != "start" or not self.task:
            return ()
        found = await self.about([self.task], answered="task")
        return () if found is None else (found,)

    async def model_returned(self, run: RunView) -> None:
        """A notice the call saw shows its skills; one it did not see was dropped."""
        seen = run.delivered_ids(self.name)
        for notice, skills in self.pending.items():
            if notice in seen:
                self.shown.update(skills)
        self.pending.clear()

    async def about(
        self, texts: Sequence[str], messages: Sequence[str] = (), answered: str = "message"
    ) -> Notice | None:
        """The notice for ``texts``, the user's ``messages`` by their IDs, or ``None`` when
        no skill matches or scoring did not finish in time; either way the record says
        what happened."""
        waiting = {skill for skills in self.pending.values() for skill in skills}
        skip = self.shown | waiting | set(self.tool.loaded)
        record: dict[str, Any] = {"answered": answered, "scoring": "lexical"}
        if messages:
            record["messages"] = list(messages)
        try:
            found = await self._bounded(
                asyncio.to_thread(matches, self.tool, "\n".join(texts), skip)
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
        notice = f"shown-{len(self.records)}"
        record["notice"] = notice
        self.pending[notice] = tuple(entry.qualified for entry in found)
        payload = {"skills": [{"id": e.qualified, "description": e.description} for e in found]}
        return Notice(self.name, (notice,), SKILLS.text, payload)

    async def _bounded(self, work: Awaitable[T]) -> T:
        """``work``'s result, or ``TimeoutError`` once ``wait`` seconds of the clock pass."""
        task = asyncio.ensure_future(work)
        timer = asyncio.ensure_future(self.clock.sleep(self.wait))
        try:
            await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        if not task.done():
            task.cancel()
            raise TimeoutError
        return task.result()

    def summary(self) -> object | None:
        """Each discovery: its notice, what it answered, the skills shown, and how scoring
        went."""
        return {"discoveries": self.records} if self.records else None
