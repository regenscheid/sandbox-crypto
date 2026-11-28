# SPDX-License-Identifier: MIT
"""Turn hooks: how a subsystem takes part in a run of the tool loop.

A hook offers notices at the loop's safe points, sees each tool result, has its say on
a closing answer, and acknowledges what a saved run record says was delivered. The
loop alone applies the delivery rules:

- A notice's IDs count as delivered only after a model call that saw it returns.
- The run's record is saved, naming each source's delivered IDs in
  ``RunRecord.delivered`` and its summary in ``RunRecord.summaries``, before any hook
  acknowledges them. A hook that fails to acknowledge does not stop the others. What a
  hook contributes to the record is checked where it contributes it, never first at the
  save: a notice whose source or IDs are not identifiers cannot be made (``Notice``), and
  a summary that is not JSON under an identifier is refused (``contribute``).
- At a closing answer, a hook gets another turn only while turns remain, and a
  response round only while response rounds remain, and either only with notices.
  Hooks check this too, before doing work for a turn that cannot come.
- Before an answer with no tool call has its closing, a hook may offer to carry the run
  past it to its next turn (``goes_on``), as an open plan does, and commits to the offer
  only once the loop found room and no limit due for that turn (``went_on``): the answer
  is then an ordinary message, never held, and no closing is asked.
- At a limit, the run's last model call is its report round (``loop._report``): no hook
  offers notices before it but those it gives for that call (``report_notices``), after
  the report's own; it may call no tool (``tool_choice: none``), no tool it asks for all
  the same runs, and its closing has no turn to give.
- Notices at the ``tool`` point ride on the tool result, bounded with it, each under
  a key of its own. A hook that fails there, or a notice whose key is taken or that is
  too long, offers nothing: the result stands and the notice waits for a later safe
  point, or for the next run if its hook already counted it as handed out.

Hooks run in a fixed order, which is the order their notices reach the model. A veto
hook is asked about a closing answer first, and again after every hook accepted it.
Leases are taken in hook order and released in reverse; each lease releases what it
holds whatever happened to the others.

A hook that fails at any of its points for a reason outside the agent's control, the
worker unreachable or what it reads there gone (``unavailable``), never ends the run by
itself (``guarded``): the point goes on as if the hook had nothing to say, the run's
record names the gap (``summaries.hook_gaps``), and at a safe point the model is told
what it means (``TurnHook.unread``). A closing in which such a hook fails still closes.

A hook may also wrap each model call and each tool call (``wrap_model_call``,
``wrap_tool_call``), the first hook outermost, inside the loop's own wraps
(``wraps.py``). Wraps run where the loop runs: a confined attempt's hooks are proxies
over a channel that carries none, so the host refuses a hook that overrides one
(``wraps_calls``).
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

from pydantic import JsonValue, TypeAdapter, ValidationError

from sandbox_science.contracts import ErrorKind, Identifier

from .notices import HOOK_GAP, HOOK_GAP_ATTACHED
from .worker_client import WorkerAPIError, WorkerProtocolError

if TYPE_CHECKING:
    from .context.folding import ContextView
    from .model import Message, ModelTurn, ToolCall
    from .projects import ModelUsage, RunRecord
    from .tools import ToolOutcome

_log = logging.getLogger(__name__)

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

DECISION_WAIT_SECONDS: Final = 1800.0
"""How long a child's check-in waits at a safe point for its parent's decision before the
automatic decision is made for it (D102)."""
HALT_TIMEOUT_SECONDS: Final = DECISION_WAIT_SECONDS + 600.0
"""How long a confined runtime waits for a hook's ``halt``, where a check-in waits: the
decision's wait, measured from the start of ``halt``, plus a margin for the check-in's own
reads and the automatic decision, so that decision always answers first."""
RESPONSE_ROUNDS: Final = 2
"""Closing answers a run may take back to respond to findings, whatever their source."""
GAPS: Final = "hook_gaps"
"""The notice source, and the run record's summary, of the points a hook could not read."""
MAX_GAPS: Final = 50
"""The most gaps a run's record names; the count goes on past it."""
REFUSED: Final = "refused_contributions"
"""Where a run's record names the summaries it refused (``contribute``)."""
_IDENTIFIER: Final = TypeAdapter(Identifier)
_JSON: Final = TypeAdapter(JsonValue)


def identifier(value: str, what: str) -> str:
    """``value``, which a run's record keeps as an ``Identifier``; a ``ValueError`` naming
    ``what`` it is otherwise."""
    try:
        return _IDENTIFIER.validate_python(value)
    except ValidationError:
        raise ValueError(f"{what} {value!r} is not an identifier") from None


def checked_ids(source: str, ids: Sequence[str]) -> tuple[str, ...]:
    """A source's notice IDs, as a run's record keeps them under ``delivered``."""
    identifier(source, "a notice source")
    return tuple(identifier(item, f"a {source} notice ID") for item in ids)


RECURSION: Final = "recursion_loop"
"""The validation error of a value nested deeper than validation follows."""
_GONE: Final = object()
"""Marks what ``prune`` leaves out until it sweeps the value."""


def mark_invalid(value: object, loc: Sequence[str | int], *, outermost: bool = False) -> str | None:
    """Mark for removal the innermost list element on ``loc``, a validation error's place
    in ``value``, or, on a path with no list, its last dictionary entry: the path marked,
    or ``None`` where the error has no element to remove. A union's type tags in ``loc``
    (``dict``, ``list``) are passed over. With ``outermost``, as for a value nested past
    what validation follows, the outermost list element on the path is marked instead."""
    steps = list(loc[:-1] if loc and loc[-1] == "[key]" else loc)
    current: object = value
    path: list[str | int] = []
    entry: tuple[dict[str | int, object], str | int, list[str | int]] | None = None
    element: tuple[list[object], int, list[str | int]] | None = None
    for step in steps:
        if isinstance(current, dict) and step in current:
            container = cast(dict[str | int, object], current)
            entry = (container, step, [*path, step])
            current = container[step]
        elif isinstance(current, list) and isinstance(step, int):
            items = cast(list[object], current)
            if not 0 <= step < len(items):
                break
            if element is None or not outermost:
                element = (items, step, [*path, step])
            current = items[step]
        else:
            continue
        path.append(step)
    if element is not None:
        items, index, path = element
        items[index] = _GONE
    elif entry is not None:
        container, key, path = entry
        container[key] = _GONE
    else:
        return None
    return ".".join(str(step) for step in path)


def sweep_marked(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: sweep_marked(v)
            for k, v in cast(dict[object, object], value).items()
            if v is not _GONE
        }
    if isinstance(value, list):
        return [sweep_marked(v) for v in cast(list[object], value) if v is not _GONE]
    return value


def prune(value: object, error: ValidationError) -> tuple[object, list[str]]:
    """``value`` without exactly the elements ``error`` found invalid, each list element or
    dictionary entry by its own place, never its neighbours; and those places. ``value``
    is copied first."""
    found = copy.deepcopy(value)
    paths = [
        p
        for e in error.errors()
        if (p := mark_invalid(found, e["loc"], outermost=e["type"] == RECURSION)) is not None
    ]
    return sweep_marked(found), paths


def contribute(summaries: dict[str, Any], name: str, value: object) -> None:
    """Keep a hook's summary in a run's ``summaries`` under its name: what of it is not
    JSON is left out, element by element, logged, and named under ``REFUSED``, so that a
    summary that cannot be kept never costs the run its record; one under a name that is
    not an identifier is refused whole."""
    try:
        identifier(name, "a summary's name")
    except ValueError as error:
        _log.error("the %s summary is refused: %s", name[:128], error)
        summaries.setdefault(REFUSED, {})[name[:128]] = str(error)[:300]
        return
    for _ in range(4):
        try:
            summaries[name] = _JSON.validate_python(value)
            return
        except ValidationError as error:
            value, paths = prune(value, error)
            _log.error("the %s summary is kept without %s", name, ", ".join(paths)[:300])
            left = summaries.setdefault(REFUSED, {}).setdefault(name, [])
            left.extend(paths[:50])
            if not paths:
                break
        except RecursionError as error:
            summaries.setdefault(REFUSED, {})[name] = str(error)[:300]
            return
    summaries.setdefault(REFUSED, {})[name] = "it could not be kept as JSON"


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

    def __post_init__(self) -> None:
        # Its IDs go into the run's record as delivered: one it cannot keep is refused here.
        checked_ids(self.source, self.ids)


@dataclass(frozen=True)
class Closing:
    """A hook's view of a closing answer. Its notices are added in every case.

    - ``accept``: the answer may stand.
    - ``continue``: the notices are new input; the run takes another turn.
    - ``respond``: the notices are findings to answer; the run takes a response round,
      one for every hook that asks in this closing. With ``hold_answer``, the answer is
      not the run's answer to the user: the one after the round is (consistency review,
      CR-48).
    - ``retry``: the answer cannot stand; another turn, or the run stops as ``stopped``.
    - ``stop``: the run ends as ``stopped``, optionally retaining its checkpoint answer.
    """

    action: Literal["accept", "continue", "respond", "retry", "stop"] = "accept"
    notices: tuple[Notice, ...] = ()
    stopped: str = "user_message"
    retain_answer: bool = False
    hold_answer: bool = False


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

    @property
    def withheld(self) -> bool:
        """Whether the closing answer the conversation ends with is kept from the user
        while a hook decides on it (``TurnHook.holds_answers``; consistency review,
        CR-48): the run's events write it once the closing ends."""
        ...

    @property
    def answer(self) -> str | None:
        """The run's answer to the user, once a closing let one stand."""
        ...

    @property
    def holding(self) -> bool:
        """Whether the answer a closing held for its response round is still kept from the
        user (consistency review, CR-48): the run's events write it only if it becomes the
        run's answer, not when the round's own answer stands."""
        ...

    def delivered_ids(self, source: str) -> AbstractSet[str]: ...

    def note_gap(self, gap: dict[str, str]) -> None:
        """Keep, for the run's record, that a hook could not be read at a point."""
        ...


class TurnHook:
    """One subsystem's part in a run. Every method has a default that does nothing."""

    name: str = "hook"
    veto: bool = False
    """Asked about a closing answer before the others, and again once they accept it."""
    unread: str = "its notices could not be read now; they come at a later point"
    """What a run is told of this hook's failure at a point (``guarded``): what it means
    for the run's work."""

    @property
    def holds_answers(self) -> bool:
        """Whether a closing answer is kept from the user until this hook's closing ends,
        as a final review in ``hold`` keeps it (consistency review, CR-48)."""
        return False

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

    async def report_notices(self, run: RunView, limit: str) -> Sequence[Notice]:
        """Before the run's report round at ``limit``, once no hook halted it: this hook's
        notices for that last call, which follow the report's own, such as what a delegated
        run's report must hold; no other notice of the hook reaches that call."""
        return ()

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
        it, through the gates. Read-class calls of one reply may run at once, each in a
        wrap of its own (``loop._batches``). What a wrap returns is the outcome the runaway
        guard, the tool point's notices and the transcript see, in the reply's order. The
        report round's calls, and one the output limit cut off, which do not run, pass no
        wrap. The default calls it once. ``call`` is as the model wrote
        it, its ``purpose_note`` included: the toolbox checks the purpose and takes it
        out before any gate or handler, and a wrap decides nothing on it (Purpose 3)."""
        return await handler(call, operation)

    async def observe_tool(self, run: RunView, record: dict[str, Any]) -> None:
        """After each tool call that ran, with the outcome the transcript keeps, in the
        reply's order whatever order its calls ended in."""

    async def goes_on(self, run: RunView, answer: str) -> Sequence[Notice]:
        """Before the closing of an answer with no tool call, once the model call that gave
        it returned and before ``model_returned``: the notices that would carry the run on
        to its next turn instead, when work this hook governs remains, as an active plan's
        workable steps do (``plan_tools.Plans``). An offer only, which spends nothing: the
        loop settles the run's room and limits, then asks ``went_on``. None to let the
        closing decide."""
        return ()

    async def went_on(self, run: RunView, notices: Sequence[Notice]) -> bool:
        """The loop takes this hook's offer from ``goes_on``, the run having room and no
        limit due for its next turn: whether the hook commits to it, spending what bounds
        it. Only then is the answer the user's to see at once, as a message, with no
        closing asked, so no hook holds or reviews it; otherwise the answer has its
        closing as any other. A hook that overrides ``goes_on`` overrides this too."""
        return False

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


ENVIRONMENT_GONE: Final = re.compile(r"no environment '[^']+'")
"""The worker's answer for an environment it no longer has (W/app.py), as its sandbox
was deleted under the run."""


def unavailable(error: BaseException) -> bool:
    """Whether a hook's failure is outside the agent's control, and so one the run goes on
    past (``guarded``). Only these shapes are:

    - a ``TimeoutError``: a read the worker did not answer in time;
    - a ``WorkerAPIError`` of kind ``unavailable``: the worker unreachable, or answering
      that it cannot serve now (a proxy's 502/503/504 before it among them);
    - a ``WorkerAPIError`` of kind ``not_found`` that the worker answered over HTTP (it has
      a status) naming an environment it no longer has (``ENVIRONMENT_GONE``).

    Any other error is the hook's own or the platform's integrity failing, which still
    ends the run: a worker of another protocol version (releases are lockstep), an owner
    or identity check the harness makes itself, a control-store read."""
    if isinstance(error, TimeoutError):
        return True
    if not isinstance(error, WorkerAPIError) or isinstance(error, WorkerProtocolError):
        return False
    if error.kind is ErrorKind.UNAVAILABLE:
        return True
    return (
        error.kind is ErrorKind.NOT_FOUND
        and error.status is not None
        and ENVIRONMENT_GONE.search(error.message) is not None
    )


def gap(hook: TurnHook, point: str, run: RunView, error: BaseException) -> Notice:
    """Note in the run's record that ``hook`` could not be read at ``point``, and the
    notice that tells the model so: at the ``tool`` point, under its key beside the
    result."""
    said = str(error)[:300] or type(error).__name__
    _log.warning("the %s hook failed at %s, and the run goes on: %s", hook.name, point, said)
    run.note_gap({"source": hook.name, "point": point, "error": said})
    framing = HOOK_GAP_ATTACHED if point == "tool" else HOOK_GAP
    return Notice(
        GAPS,
        (),
        framing.text,
        {"source": hook.name, "point": point, "error": said, "unread": hook.unread},
    )


async def guarded[T](hook: TurnHook, point: str, run: RunView, call: Awaitable[T], default: T) -> T:
    """``call``, one of ``hook``'s methods at ``point``, or ``default`` when it fails for a
    reason outside the agent's control (``unavailable``), its gap noted (``gap``); any
    other failure is the hook's own fault, and is raised."""
    try:
        return await call
    except Exception as error:
        if not unavailable(error):
            raise
        gap(hook, point, run, error)
        return default


async def offered(hook: TurnHook, point: Point, run: RunView) -> tuple[Notice, ...]:
    """``hook``'s notices at ``point``; when it fails for a reason outside the agent's
    control, the notice that it could not be read (``gap``), and the point goes on."""
    try:
        return tuple(await hook.notices(point, run))
    except Exception as error:
        if not unavailable(error):
            raise
        return (gap(hook, point, run, error),)


def overrides(hook: TurnHook, name: str) -> bool:
    """Whether ``hook``'s class replaces the ``TurnHook`` method ``name``."""
    return getattr(type(hook), name) is not getattr(TurnHook, name)


def wraps_calls(hook: TurnHook) -> bool:
    """Whether ``hook`` overrides a wrap point, which only an unconfined loop can run."""
    return overrides(hook, "wrap_model_call") or overrides(hook, "wrap_tool_call")


@asynccontextmanager
async def _nothing() -> AsyncGenerator[None]:
    yield
