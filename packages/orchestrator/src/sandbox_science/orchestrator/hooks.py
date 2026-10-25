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
- At a limit, the run's last model call is its report round (``loop._report``): no hook
  offers notices before it, no tool it asks for runs, and its closing has no turn to
  give.
- Notices at the ``tool`` point ride on the tool result, bounded with it, each under
  a key of its own. A hook that fails there, or a notice whose key is taken or that is
  too long, offers nothing: the result stands and the notice waits for a later safe
  point, or for the next run if its hook already counted it as handed out.

Hooks run in a fixed order, which is the order their notices reach the model. A veto
hook is asked about a closing answer first, and again after every hook accepted it.
Leases are taken in hook order and released in reverse; each lease releases what it
holds whatever happened to the others.

A hook may also wrap each model call and each tool call (``wrap_model_call``,
``wrap_tool_call``), the first hook outermost, inside the loop's own wraps
(``wraps.py``). Wraps run where the loop runs: a confined attempt's hooks are proxies
over a channel that carries none, so the host refuses a hook that overrides one
(``wraps_calls``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

if TYPE_CHECKING:
    from .context.folding import ContextView
    from .model import Message, ModelTurn, ToolCall
    from .projects import ModelUsage, RunRecord
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

type ModelHandler = Callable[[], Awaitable[ModelTurn | None]]
"""The next step of a model call: the reply, or ``None`` when none came. Its innermost
step raises the model's errors, and answers ``None`` with no call made when the
conversation is past the run's budget."""

type ToolHandler = Callable[[ToolCall, str], Awaitable[ToolOutcome]]
"""The next step of a tool call, given the call and its operation ID: its outcome."""

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
    - ``stop``: the run ends as ``stopped``, optionally retaining its checkpoint answer.
    """

    action: Literal["accept", "continue", "respond", "retry", "stop"] = "accept"
    notices: tuple[Notice, ...] = ()
    stopped: str = "user_message"
    retain_answer: bool = False


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
    operation_id: str | None = None
    """The intent saved before entering the toolbox; absent for report-round refusals."""
    consumed: Mapping[str, AbstractSet[str]] | None = None
    usage: ModelUsage | None = None
    usage_index: int | None = None
    """One-based answered-response index, independent of refused model attempts."""


class MessageLog(Protocol):
    """Where a run keeps each message as it adds it to its conversation: the session's
    archive (docs/specs/context-management.md, CM-4)."""

    async def start_tool(self, call: ToolCall, operation_id: str) -> None:
        """Persist intent before dispatch. A failure prevents the call from running."""
        ...

    async def append(self, message: Message, appended: Appended) -> Message:
        """Keep ``message``, before the next model call; the message as the
        conversation holds it, with its ID header (CM-4a). A failure fails the run."""
        ...

    def retained_tool(self, record: dict[str, Any]) -> dict[str, Any]:
        """Historical representation of the just-committed tool result. The loop keeps
        the original current result through its observers before replacing it."""
        ...


class RunView(Protocol):
    """Execution state and the read-only conversation a hook may inspect."""

    @property
    def context(self) -> ContextView: ...

    @property
    def turns(self) -> int:
        """Model calls the run has started."""
        ...

    @property
    def turns_left(self) -> int: ...

    @property
    def tool_records(self) -> Sequence[dict[str, Any]]:
        """One record per tool call so far, as the run's record keeps them."""
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
    def halted(self) -> str | None:
        """Why a hook stopped the run before its next model call, if one did: the run's
        last closing then has no turn to take."""
        ...

    def delivered_ids(self, source: str) -> AbstractSet[str]: ...


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

    async def turn_limit(self, run: RunView) -> int | None:
        """An authoritative allowance's current attempt ceiling, or no override."""
        return None

    async def halt(self, run: RunView) -> str | None:
        """Before every model call, including the first, and before its notices: the reason the
        run stops there instead, which becomes its ``stopped``, or ``None`` to go on."""
        return None

    async def last_call(self, run: RunView) -> str | None:
        """Before each model call after the first, once no hook halted the run: the limit
        that leaves room for this call and no other, which makes it the run's report
        round and becomes its ``stopped`` (a delegated run's tokens, ``run_budget``), or
        ``None`` to go on (run-service spec, RS-6f)."""
        return None

    async def consumption(self) -> Sequence[str]:
        """Input IDs already shown by tools, under this hook's source; no display change."""
        return ()

    async def model_returned(self, run: RunView) -> None:
        """After each successful model call, once the loop has counted its deliveries and
        added the reply to the conversation."""

    async def gate(self, effect: Effect) -> Refusal | None:
        """Before a tool call of this effect class: a refusal, or ``None`` to let it run."""
        return None

    async def gate_call(self, tool: str, effect: Effect) -> Refusal | None:
        """Before a call of the tool named ``tool``, of this effect class: by default the
        class's ``gate``. A hook that tells tools of one class apart overrides this, as the
        delegated runs' fence does (D99)."""
        return await self.gate(effect)

    async def wrap_model_call(self, run: RunView, handler: ModelHandler) -> ModelTurn | None:
        """Around each attempt at a model call, the attempts after a fold among them, and
        the report round's: ``handler`` makes it. A wrap may act before and after it, try
        it again, or answer in its place; the default calls it once."""
        return await handler()

    async def wrap_tool_call(
        self, run: RunView, call: ToolCall, operation: str, handler: ToolHandler
    ) -> ToolOutcome:
        """Around each tool call the run makes, once its intent is saved: ``handler`` runs
        it, through the gates. What a wrap returns is the outcome the runaway guard, the
        tool point's notices and the transcript see. The report round's calls, which do
        not run, pass no wrap. The default calls it once. ``call`` is as the model wrote
        it, its ``purpose_note`` included: the toolbox checks the purpose and takes it
        out before any gate or handler, and a wrap decides nothing on it (Purpose 3)."""
        return await handler(call, operation)

    async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
        """After each tool call, with the outcome the transcript keeps."""

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        return Closing()

    async def accept(self, run: RunView) -> None:
        """Every hook accepted the closing answer and no response round follows."""

    async def finishing(self, run: RunView, stopped: str) -> str | None:
        """After the loop ends normally, before releasing its leases. May wait for
        owned work without model calls. Cancellation still follows lease cleanup.
        Return a replacement stop reason only when finalization finds a missing outcome.
        """
        return None

    def summary(self) -> object | None:
        """What the run's record keeps for this source at the end, if anything."""
        return None

    async def acknowledge(self, record: RunRecord) -> None:
        """After the record is saved: settle what it names as delivered."""


def overrides(hook: TurnHook, name: str) -> bool:
    """Whether ``hook``'s class replaces the ``TurnHook`` method ``name``."""
    return getattr(type(hook), name) is not getattr(TurnHook, name)


def wraps_calls(hook: TurnHook) -> bool:
    """Whether ``hook`` overrides a wrap point, which only an unconfined loop can run."""
    return overrides(hook, "wrap_model_call") or overrides(hook, "wrap_tool_call")


@asynccontextmanager
async def _nothing() -> AsyncGenerator[None]:
    yield
