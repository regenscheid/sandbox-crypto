# SPDX-License-Identifier: MIT
"""The context subsystem when the provider condenses older work (docs/specs/
context-management.md, CM-2a, CM-38b): Anthropic's server-side compaction in place of the
fold engine.

The run's view is built as the fold engine builds it, from the archive at the run's
start: its state block, any fold region earlier runs left, and the raw tail (``View``).
After that the view never folds: no fold, no rebuild, no enrichment call, no wall. The
Messages adapter sends each of the run's calls with the compaction edit at the run's
trigger (``model.COMPACTION``), and the API summarizes everything before the trigger into
a ``compaction`` block at the start of its answer, which the archive keeps with the
answer and the adapter sends back in its place.

What this engine keeps is the record: the trigger and the ceiling it came from, and each
compaction the run's answers carried, with its call's usage as the response gave it,
under the run's ``folds`` summary. A context error is not recovered here: there is
nothing to fold, so the call's error stands (CM-43's retry is the fold engine's).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

from sandbox_science.orchestrator.model import AssistantMessage

from .folding import FoldingView, FoldPoint

if TYPE_CHECKING:
    from sandbox_science.orchestrator.model import ToolSpec

    from .history.view import View

Json = dict[str, Any]
_log = logging.getLogger(__name__)

MODE: Final = "provider_compaction"
KEPT: Final = 100
"""The most compactions a run's record lists one by one; the total counts them all."""


class ProviderCompaction:
    """CM-2a's subsystem for the ``provider_compaction`` context mode: a ``FoldEngine``
    that never folds, and records the compactions the provider made."""

    def __init__(self, view: View, *, trigger: int, ceiling: int) -> None:
        self.view = view
        self.trigger, self.ceiling = trigger, ceiling
        self.stopped: str | None = None
        """Never set: the provider's compaction does not stop a run (the harness reads
        it as it reads the fold engine's)."""
        self.events: Callable[[Json], Awaitable[None]] | None = None
        self.refit: Callable[[View], None] | None = None
        self.specs: Callable[[], Sequence[ToolSpec]] = tuple
        self.compactions: list[Json] = []
        self.total = 0
        self._context: FoldingView | None = None
        self._seen: int | None = None

    async def before_call(self, point: FoldPoint) -> bool:
        """Note the compactions of the answers since the last call; never fold."""
        self._context = point.context
        self._scan()
        return True

    async def overflow(self, point: FoldPoint) -> bool:
        """A context error: nothing to fold, so no room is made."""
        return False

    async def finish_background(self) -> None:
        return None

    async def response_returned(self) -> None:
        return None

    def summary(self) -> object | None:
        self._scan()  # the run's last answers
        return {
            "mode": MODE,
            "geometry": {"ceiling": self.ceiling, "trigger": self.trigger},
            "compactions": list(self.compactions),
            "compactions_total": self.total,
        }

    def _scan(self) -> None:
        context = self._context
        if context is None:
            return
        messages = context.messages
        if self._seen is None:  # the run's start: the view's own compactions are older
            self._seen = len(messages)
            return
        for index in range(self._seen, len(messages)):
            found = compaction(messages[index])
            if found is not None:
                self.total += 1
                if len(self.compactions) < KEPT:
                    self.compactions.append({"message": index, **found})
                _log.info("the provider compacted the run's view: %s", found)
        self._seen = len(messages)


def compaction(message: object) -> Json | None:
    """An answer's compaction (CM-38b), if its response holds one: its summary's length
    in characters, and its call's usage, the compaction's iteration and the answer's, as
    the response gave them."""
    if not isinstance(message, AssistantMessage) or message.native_format != "messages":
        return None
    try:
        data: object = json.loads(message.native or b"")
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    response = cast(Json, data)
    content = response.get("content")
    blocks = [
        cast(Json, block)
        for block in (cast(list[object], content) if isinstance(content, list) else [])
        if isinstance(block, dict)
    ]
    summaries = [block for block in blocks if block.get("type") == "compaction"]
    if not summaries:
        return None
    text = summaries[0].get("content")
    usage = response.get("usage")
    found = cast(Json, usage).get("iterations") if isinstance(usage, dict) else None
    steps = [
        _step(cast(Json, step))
        for step in (cast(list[object], found) if isinstance(found, list) else [])[:8]
        if isinstance(step, dict)
    ]
    return {
        "summary_chars": len(text) if isinstance(text, str) else 0,
        # The API's compaction without a summary: the model called a tool instead.
        "summarized": isinstance(text, str) and bool(text.strip()),
        "iterations": steps,
    }


_COUNTS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _step(step: Json) -> Json:
    """An iteration's kind and counts, with its writes to the 1-hour cache."""
    kind = step.get("type")
    kept: Json = {"type": kind[:32] if isinstance(kind, str) else None}
    for key in _COUNTS:
        value = step.get(key)
        kept[key] = value if type(value) is int else None
    written = step.get("cache_creation")
    hour = (
        cast(Json, written).get("ephemeral_1h_input_tokens") if isinstance(written, dict) else None
    )
    kept["cache_1h_input_tokens"] = hour if type(hour) is int else None
    return kept
