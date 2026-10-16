# SPDX-License-Identifier: MIT
"""Discovery: the skills that match what the user asked, shown before the model answers
(docs/specs/skills.md, SK-35 to SK-37, SK-21).

Before a run's first model call the run's task is scored against its skills, and so is
each user message that arrives during the run, before the call that carries it; the call
waits for the score at most ``wait`` seconds of the run's clock. A message under
``MIN_WORDS`` distinct words is not scored, by words or by meaning, and waits for
nothing. A skill is shown when it shares at least two of the message's words and scores
at least half the best, or, once the run has its skills' vectors, when its meaning comes
close (``tool.SIMILAR``); the message's embedding counts toward the wait. At most
``MAX_SHOWN`` are shown, by name and description, in a notice framed by
``notices.SKILLS``, with the entry skills of their packs that the import's pass found. A
skill the run was shown, in a notice a model call saw, or has loaded is not shown again;
one in a notice the run dropped, as at a closing with no room for it, may be. Nothing is
blocked and no load is forced (SK-36): the model decides. The run's record keeps each
notice: its ID, which ``RunRecord.delivered`` names once a model call saw it, what it
answered, the skills shown with their revisions, and how the scoring went, with why it
was by words alone when an embedding model is set. Scoring by words runs in a thread,
pure and quick: one that outlasts the wait ends on its own, and nothing waits for it;
the embedding is cancelled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Iterable, Sequence
from typing import Any, Final, TypeVar, cast

from sandbox_science.contracts.clock import SYSTEM_CLOCK, Clock
from sandbox_science.orchestrator.hooks import Notice, Point, RunView, TurnHook
from sandbox_science.orchestrator.notices import SKILLS, SKILLS_WORDS

from .embeddings import EmbeddingError
from .index import shown, written_by
from .store import Entry
from .tool import SkillTool, hybrid, rank, words

_log = logging.getLogger(__name__)
T = TypeVar("T")

WAIT_SECONDS: Final = 5.0
"""How long a model call waits for the score by default (question 12)."""
MAX_SHOWN: Final = 5
MIN_WORDS: Final = 3


def matches(
    tool: SkillTool, text: str, skip: set[str], similar: dict[str, float] | None = None
) -> list[Entry]:
    """The run's skills that match ``text``, best first, without those in ``skip``: by its
    words, or, with ``similar``, by its meaning (SK-29)."""
    query = words(text)
    if len(set(query)) < MIN_WORDS:
        return []
    lexical = [
        (entry, score)
        for entry, matched, score in rank(tool.skills.entries, query)
        if len(matched) >= 2
    ]
    best = lexical[0][1] if lexical else 0.0
    by_words = {entry.qualified for entry, score in lexical if score >= best / 2}
    shown = [
        entry
        for entry, _, meaning in hybrid(tool.skills.entries, text, similar or {})
        if (entry.qualified in by_words or meaning is not None) and entry.qualified not in skip
    ]
    return shown[:MAX_SHOWN]


def shown_in(notices: Iterable[str]) -> set[str]:
    """The skills these discovery notices, as their messages hold them, showed."""
    found: set[str] = set()
    for text in notices:
        framing = next((f.text for f in (SKILLS, SKILLS_WORDS) if text.startswith(f.text)), None)
        try:
            payload: object = json.loads(text[len(framing) :]) if framing is not None else {}
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("skills", "entry"):
            listed = cast(dict[str, object], payload).get(key)
            for item in cast(list[object], listed) if isinstance(listed, list) else []:
                skill = cast(dict[str, object], item).get("id") if isinstance(item, dict) else None
                if isinstance(skill, str):
                    found.add(skill)
    return found


def _listed(entry: Entry) -> dict[str, str]:
    """A skill as a notice lists it: its ID and the description the index gives it, with
    who wrote that, when its pack did not, in a field of its own."""
    listed = {"id": entry.qualified, "description": shown(entry)}
    if by := written_by(entry):
        listed["description_by"] = by
    return listed


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
        text = "\n".join(texts)
        record: dict[str, Any] = {"answered": answered, "scoring": "lexical"}
        if messages:  # the user's messages, or the comments' work requests, by their IDs
            record["requests" if answered == "comment" else "messages"] = list(messages)

        async def score() -> list[Entry]:
            similar: dict[str, float] = {}
            if self.tool.vectors is not None:
                try:
                    similar, why = await self.tool.similarity(text)
                except EmbeddingError as error:
                    why = f"failed: {error}"
                if similar:  # only some packs compared by meaning: partial
                    record["scoring"] = "partial" if why else "hybrid"
                if why or not similar:
                    record["embedding"] = (why or "none")[:300]
            return await asyncio.to_thread(matches, self.tool, text, skip, similar)

        if len(set(words(text))) < MIN_WORDS:
            found: list[Entry] = []  # too short to score: no embedding, no wait
        else:
            try:
                found = await self._bounded(score())
            except TimeoutError:
                found, record["scoring"] = [], "timed_out"
            except Exception:  # a failed score sends the call without a notice
                _log.exception("scoring the skills for discovery failed")
                found, record["scoring"] = [], "failed"
        record["skills"] = [{"id": e.qualified, "revision": e.revision} for e in found]
        # The entry skills of the packs shown, which route to their others (SK-35).
        packs = {entry.source for entry in found}
        routes = [
            e
            for e in self.tool.skills.entries
            if e.entry and e.source in packs and e.qualified not in skip and e not in found
        ]
        if routes:
            record["entry"] = [{"id": e.qualified, "revision": e.revision} for e in routes]
        self.records.append(record)
        if not found:
            return None
        notice = f"shown-{len(self.records)}"
        record["notice"] = notice
        self.pending[notice] = tuple(entry.qualified for entry in (*found, *routes))
        payload: dict[str, object] = {"skills": [_listed(entry) for entry in found]}
        if routes:
            payload["entry"] = [_listed(entry) for entry in routes]
        # The framing says what was compared: words alone, or meaning too (lab 42).
        framing = SKILLS if record["scoring"] in ("hybrid", "partial") else SKILLS_WORDS
        return Notice(self.name, (notice,), framing.text, payload)

    async def _bounded(self, work: Awaitable[T]) -> T:
        """``work``'s result, or ``TimeoutError`` once ``wait`` seconds of the clock pass.
        However the wait ends, by the clock or by the run's cancellation, ``work`` is
        cancelled if it has not finished."""
        task = asyncio.ensure_future(work)
        timer = asyncio.ensure_future(self.clock.sleep(self.wait))
        try:
            await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                raise TimeoutError
            return task.result()
        finally:
            timer.cancel()
            if not task.done():
                task.cancel()

    def summary(self) -> object | None:
        """Each discovery: its notice, what it answered, the skills shown, and how scoring
        went."""
        return {"discoveries": self.records} if self.records else None
