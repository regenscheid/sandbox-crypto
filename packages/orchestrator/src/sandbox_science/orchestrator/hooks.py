# SPDX-License-Identifier: MIT
"""Turn hooks: how a subsystem takes part in a run of the tool loop.

A hook offers notices at the loop's safe points, may ask for another turn when the
model answers, and acknowledges what a saved run record says was delivered. The loop
alone applies the delivery rules:

- A notice's IDs count as delivered only after a model call that saw it returns.
- The run's record is saved, naming each source's delivered IDs in
  ``RunRecord.delivered``, before any hook acknowledges them. A hook that fails to
  acknowledge does not stop the others.
- A closing hook gets another turn only while turns remain, and only with notices.
- Notices at the ``tool`` point ride on the tool result, bounded with it, each under
  a key of its own. A hook that fails there, or a notice whose key is taken or that is
  too long, offers nothing: the result stands and the notice waits.

Hooks run in a fixed order, which is the order their notices reach the model.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from .projects import RunRecord

Point = Literal["start", "turn", "tool"]
"""``start``: once, before the first turn. ``turn``: before each model call.
``tool``: after each tool call, on its result."""


@dataclass(frozen=True)
class Notice:
    """Untrusted data for the model from one source, with the IDs it delivers.

    As a message, ``framing`` is the text before the JSON payload. At the ``tool``
    point, ``framing`` is the key the payload has next to the tool result.
    """

    source: str
    ids: tuple[str, ...]
    framing: str
    payload: object


@dataclass(frozen=True)
class Closing:
    """A hook's view of a closing answer: accept it, or continue with ``notices``."""

    action: Literal["accept", "continue"] = "accept"
    notices: tuple[Notice, ...] = ()


class RunView(Protocol):
    """What a hook may read of the run in progress."""

    @property
    def turns_left(self) -> int: ...

    def delivered_ids(self, source: str) -> frozenset[str]: ...


class TurnHook:
    """One subsystem's part in a run. Every method has a default that does nothing."""

    name: str = "hook"

    def lease(self) -> AbstractAsyncContextManager[None]:
        """Held for the whole run, around every turn."""
        return _nothing()

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        return ()

    async def model_returned(self, run: RunView) -> None:
        """After each successful model call, once the loop has counted its deliveries."""

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        return Closing()

    async def acknowledge(self, record: RunRecord) -> None:
        """After the record is saved: settle what it names as delivered."""


@asynccontextmanager
async def _nothing() -> AsyncGenerator[None]:
    yield
