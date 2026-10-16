# SPDX-License-Identifier: MIT
"""Turn hooks: how a subsystem takes part in a run of the tool loop.

A hook offers notices at the loop's safe points, sees each tool result, has its say on
a closing answer, and acknowledges what a saved run record says was delivered. The
loop alone applies the delivery rules:

- A notice's IDs count as delivered only after a model call that saw it returns.
- The run's record is saved, naming each source's delivered IDs in
  ``RunRecord.delivered`` and its summary in ``RunRecord.summaries``, before any hook
  acknowledges them. A hook that fails to acknowledge does not stop the others.
- At a closing answer, a hook gets another turn only while turns remain, and a
  response round only while response rounds remain, and either only with notices.
  Hooks check this too, before doing work for a turn that cannot come.
- Notices at the ``tool`` point ride on the tool result, bounded with it, each under
  a key of its own. A hook that fails there, or a notice whose key is taken or that is
  too long, offers nothing: the result stands and the notice waits for a later safe
  point, or for the next run if its hook already counted it as handed out.

Hooks run in a fixed order, which is the order their notices reach the model. A veto
hook is asked about a closing answer first, and again after every hook accepted it.
Leases are taken in hook order and released in reverse; each lease releases what it
holds whatever happened to the others.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

if TYPE_CHECKING:
    from .model import Message, ModelTurn, ToolCall
    from .projects import ModelUsage, RunRecord
    from .prompts import PromptPins
    from .tools import ToolOutcome

Point = Literal["start", "turn", "tool"]
"""``start``: once, before the first turn. ``turn``: before each model call.
``tool``: after each tool call, on its result."""

Effect = Literal["read", "cancel", "effect", "plan", "proposal"]
"""What a tool call may change: ``read`` and ``cancel`` tools nothing a plan governs,
``effect`` tools anything, ``plan`` tools the plan, which they check themselves, and
``proposal`` tools nothing until the user accepts what they propose (a project memory
proposal): the plan fence lets them pass, but a run that no longer holds its session
may not make one."""

RESPONSE_ROUNDS: Final = 2
"""Closing answers a run may take back to respond to findings, whatever their source."""


@dataclass(frozen=True)
class Notice:
    """Untrusted data for the model from one source, with the IDs it delivers.

    As a message, ``framing`` is the text before the JSON payload. At the ``tool``
    point, ``framing`` is the key the payload has next to the tool result.

    A ``user`` notice is the user's own message, not data: its message is ``framing``
    alone, the text as the user wrote it, and a hook offers it only before a model call.
    """

    source: str
    ids: tuple[str, ...]
    framing: str
    payload: object
    user: bool = False


@dataclass(frozen=True)
class Closing:
    """A hook's view of a closing answer. Its notices are added in every case.

    - ``accept``: the answer may stand.
    - ``continue``: the notices are new input; the run takes another turn.
    - ``respond``: the notices are findings to answer; the run takes a response round,
      one for every hook that asks in this closing.
    - ``retry``: the answer cannot stand; another turn, or the run stops as ``stopped``.
    - ``stop``: the run ends without an answer, as ``stopped``.
    """

    action: Literal["accept", "continue", "respond", "retry", "stop"] = "accept"
    notices: tuple[Notice, ...] = ()
    stopped: str = "user_message"


@dataclass(frozen=True)
class Refusal:
    """A gate's refusal of a tool call, which the model reads as the call's error."""

    kind: str
    """An ``ErrorKind`` value."""
    message: str


@dataclass(frozen=True)
class Appended:
    """What the loop knows of a message it adds to the conversation: the run's task, a
    notice (the user's own messages among them), the model's answer, or a tool's
    result."""

    kind: Literal["task", "notice", "assistant", "tool_result"]
    notice: Notice | None = None
    reply: ModelTurn | None = None
    call: ToolCall | None = None
    outcome: ToolOutcome | None = None


class MessageLog(Protocol):
    """Where a run keeps each message as it adds it to its conversation: the session's
    archive (docs/specs/context-management.md, CM-4)."""

    async def append(self, message: Message, appended: Appended) -> Message:
        """Keep ``message``, before the next model call; the message as the
        conversation holds it, with its ID header (CM-4a). A failure fails the run."""
        ...


class RunView(Protocol):
    """What a hook may read of the run in progress."""

    @property
    def messages(self) -> Sequence[Message]: ...

    @property
    def turns(self) -> int:
        """Model calls the run has started."""
        ...

    @property
    def turns_left(self) -> int: ...

    @property
    def prompt_provenance(self) -> PromptPins | None: ...

    @property
    def tool_records(self) -> Sequence[dict[str, Any]]:
        """One record per tool call so far, as the run's record keeps them."""
        ...

    @property
    def usage(self) -> Sequence[ModelUsage]:
        """Each answered model call's usage, in order."""
        ...

    @property
    def since(self) -> int:
        """The calls made before the view was last rebuilt: ``usage[since:]`` are those
        that measured it as it is."""
        ...

    @property
    def offered(self) -> Mapping[str, AbstractSet[str]]:
        """Each source's notice IDs in the conversation, delivered or not."""
        ...

    @property
    def response_rounds(self) -> int:
        """Response rounds the run has taken."""
        ...

    @property
    def can_respond(self) -> bool:
        """Whether a turn and a response round remain."""
        ...

    @property
    def size(self) -> int:
        """What the next model call would send, in characters, tool schemas included:
        after a fold, the view it rebuilt and what was added since."""
        ...

    @property
    def halted(self) -> str | None:
        """Why a hook stopped the run before its next model call, if one did: the run's
        last closing then has no turn to take."""
        ...

    def delivered_ids(self, source: str) -> frozenset[str]: ...


class TurnHook:
    """One subsystem's part in a run. Every method has a default that does nothing."""

    name: str = "hook"
    veto: bool = False
    """Asked about a closing answer before the others, and again once they accept it."""

    def lease(self) -> AbstractAsyncContextManager[None]:
        """Held for the whole run, around every turn."""
        return _nothing()

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        return ()

    async def halt(self, run: RunView) -> str | None:
        """Before each model call after the first, and before its notices: the reason the
        run stops there instead, which becomes its ``stopped``, or ``None`` to go on."""
        return None

    async def model_returned(self, run: RunView) -> None:
        """After each successful model call, once the loop has counted its deliveries and
        added the reply to the conversation."""

    async def gate(self, effect: Effect) -> Refusal | None:
        """Before a tool call of this effect class: a refusal, or ``None`` to let it run."""
        return None

    async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
        """After each tool call, with the outcome the transcript keeps."""

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        return Closing()

    async def accept(self, run: RunView) -> None:
        """Every hook accepted the closing answer and no response round follows."""

    def summary(self) -> object | None:
        """What the run's record keeps for this source at the end, if anything."""
        return None

    async def acknowledge(self, record: RunRecord) -> None:
        """After the record is saved: settle what it names as delivered."""


@asynccontextmanager
async def _nothing() -> AsyncGenerator[None]:
    yield
