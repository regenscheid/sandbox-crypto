# SPDX-License-Identifier: MIT
"""The delegate tools as the model sees them (plans-and-delegation spec, PD-33).

A brief is a check or a task. A check is of one ledger entry at one revision, which a
run with a fresh context does (LG-17): the platform writes its task and lists the
entry's evidence as its inputs, and the parent adds at most a short note, which the
child reads as the requester's note, not evidence (coordinator, 2026-10-15). A task is
the parent's own (PD-11): its text, a context summary and its inputs, each checked as
any agent text is, since the child, its record and the workspace show them.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import Annotated, Any, Final, Literal

from pydantic import BeforeValidator, Field

from sandbox_science.contracts import ErrorKind, Identifier
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.agent_text import agent_text
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.hooks import Effect
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.worker_client import WorkerAPIError

from .narrowing import MAX_ENTRIES as MAX_CONNECTORS
from .narrowing import MAX_ENTRY as MAX_CONNECTOR
from .references import MAX_SPAN, SCHEMA, References

MAX_NOTE: Final = 500
MAX_WAITED: Final = 10


class CheckTarget(Contract):
    entry: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=999_999_999)


def _note(value: object) -> object:
    """A note as the brief keeps it: one line, its white space collapsed, so that it
    cannot imitate the brief's own parts, and holding no character the user may not see,
    since the checker, the evidence and the workspace show it."""
    if not isinstance(value, str) or not (kept := " ".join(value.split())):
        return "" if isinstance(value, str) else value
    try:
        return agent_text(kept, "the note")
    except DocumentError as error:
        raise ValueError(error.message) from None


MAX_TEXT: Final = 8000
MAX_INPUTS: Final = 20
MAX_INPUT: Final = 512


def _text(value: object, *, what: str) -> object:
    """An agent's text as a brief keeps it: trimmed, its line ends made line feeds, and
    holding no character the user may not see (``check_agent_text``)."""
    if not isinstance(value, str):
        return value
    try:
        return agent_text(value.strip(), what)
    except DocumentError as error:
        raise ValueError(error.message) from None


def _texts(what: str) -> BeforeValidator:
    return BeforeValidator(partial(_text, what=what))


MAX_DELIVERABLES: Final = 12
MAX_TITLE: Final = 120
MAX_DESCRIPTION: Final = 400
DeliverableKind = Literal["artifact", "file", "entry", "answer"]
"""What a deliverable is (PD-45): a file revision the child's calls record or publish, a
path they write, a ledger entry it creates or revises, or a short answer in its result."""


def _line(value: object, *, what: str) -> object:
    """An agent's text that must be one line, such as a deliverable's title."""
    found = _text(value, what=what)
    if isinstance(found, str) and "\n" in found:
        raise ValueError(f"{what} is one line")
    return found


class Deliverable(Contract):
    """One thing a task is to hand back (PD-45), which its result accounts for by the
    number the platform gives it, ``d1`` to ``d12`` in the brief's order."""

    title: Annotated[
        str,
        BeforeValidator(partial(_line, what="a deliverable's title")),
        Field(min_length=1, max_length=MAX_TITLE),
    ]
    kind: DeliverableKind
    description: Annotated[
        str, _texts("a deliverable's description"), Field(max_length=MAX_DESCRIPTION)
    ]
    """What counts as delivering it."""


class TaskBrief(Contract):
    """What ``delegate`` takes for a task (PD-11): what to do, what the child needs to
    know, and the inputs it should read; the parent writes all of it."""

    task: Annotated[str, _texts("the task"), Field(min_length=1, max_length=MAX_TEXT)]
    context: Annotated[str, _texts("the context"), Field(max_length=MAX_TEXT)] = ""
    inputs: tuple[
        Annotated[str, _texts("an input"), Field(min_length=1, max_length=MAX_INPUT)], ...
    ] = Field(default=(), max_length=MAX_INPUTS)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    """A model alias the project lets a brief choose (PD-28)."""
    track: Identifier | None = None
    """A track of the session's active plan, whose steps the child records its progress
    on, and only those (PD-9); a track has one running delegation at most (PD-12)."""
    issue: str | None = Field(default=None, min_length=1, max_length=128)
    """An issue the task works on, which its outcome is recorded on as work (LG-F8)."""
    delegate: bool = False
    """Whether the child may delegate tasks of its own, once more: only a main run's
    task, in a project whose depth is 2 (PD-15)."""
    references: References = ()
    """Records of the parent's own the child starts with, materialized into its state
    block and readable with its ``history_read`` (PD-11, CM-37b)."""
    deliverables: tuple[Deliverable, ...] = Field(default=(), max_length=MAX_DELIVERABLES)
    """What the child is to hand back, which its result accounts for (PD-45); optional,
    and written once with the brief."""
    environment: Literal["shared", "own"] | None = None
    """Where it works, where the project gives tasks environments of their own (CE-2):
    ``shared`` keeps a small task in the session's environment. ``own`` under a project
    that gives none is answered with a notice, and the task shares."""
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    """A child profile of the worker's, by name, for a task in its own environment (CE-3);
    the project's otherwise. Never a size or a node."""
    connectors: tuple[Annotated[str, Field(min_length=1, max_length=MAX_CONNECTOR)], ...] | None = (
        Field(default=None, max_length=MAX_CONNECTORS)
    )
    """The connectors the task may use, some of its parent's (DA-6,
    ``narrowing``); ``None`` for all of them."""


class CheckBrief(Contract):
    """What ``delegate`` takes: the entry and revision to check, and the parent's note.
    A brief carries no task or inputs of the parent's, which could carry the verdict."""

    check: CheckTarget
    note: Annotated[str, BeforeValidator(_note), Field(max_length=MAX_NOTE)] = ""
    model: str | None = Field(default=None, min_length=1, max_length=128)
    """A model alias the project lets a brief choose (PD-28)."""


def brief_of(args: dict[str, Any]) -> CheckBrief | TaskBrief:
    """The brief ``delegate``'s arguments give: a task when they name one, otherwise a
    check; a brief that names both is refused."""
    if "task" in args and "check" in args:
        raise WorkerAPIError(ErrorKind.INVALID_REQUEST, "a brief is a check or a task, not both")
    if "task" not in args and "connectors" in args:
        raise WorkerAPIError(
            ErrorKind.POLICY_DENIED,
            "not_permitted_for_role: a check's connectors are the platform's, the tools "
            "that only read; only a task's brief narrows them (DA-1, DA-6)",
        )
    if "task" not in args and "references" in args:
        raise WorkerAPIError(
            ErrorKind.POLICY_DENIED,
            "reference_refused: a check takes no references: it sees only its brief, "
            "never its parent's records (LG-17, CM-37a)",
        )
    return TaskBrief.model_validate(args) if "task" in args else CheckBrief.model_validate(args)


class WaitRequest(Contract):
    delegation_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=MAX_WAITED)
    mode: Literal["all", "first"] = "all"


class Named(Contract):
    """``delegation_status`` and ``cancel_delegation`` name one delegation."""

    delegation_id: Identifier


SPECS: Final = (
    ToolSpec(
        "delegate",
        "Start a delegated run, which works with a fresh context: it sees none of your "
        "conversation, only its brief, and has kernels of its own. Its brief is a check or "
        "a task. A check (check) independently checks one ledger entry at one revision, "
        "such as a proved result before you establish it: the platform writes its task, "
        "check whether the entry holds at that revision, and lists the entry's evidence as "
        "its inputs; your note, if any, reaches it as the requester's note, not evidence. "
        "A task (task) is yours to write: what to do, the context it needs (context), and "
        "the inputs it should read (inputs), and your records (references); it works "
        "under a delegated agent's authority. "
        "A task may list deliverables, which are optional: what it is to hand back, at most "
        f"{MAX_DELIVERABLES}, each a title (one line of at most {MAX_TITLE} characters), a "
        "kind (artifact, file, entry or answer) and a description of what counts (at most "
        f"{MAX_DESCRIPTION} characters); the platform numbers them d1, d2, … and its "
        "result accounts for each: it is complete only when every one was produced. Skip "
        "them for a small task. "
        "It runs while you go on; this returns its delegation_id at once. Its report "
        "reaches you as a delegation notice, or as the result of wait_delegations. You "
        "cannot give your final answer while it runs. Its result is its claim: a status "
        "and a summary, and for a check a verdict; the outcome says how the platform saw "
        "it end. A check that returned confirmed counts as the entry's independent check "
        "once you add it as evidence: role independent_check, delegation its ID, checked "
        "the revision it checked; one of the same material that refuted it, or completed "
        "inconclusive, keeps any from counting. "
        "Where the project gives tasks environments of their own, a task in one starts "
        "once it is ready, about one to five minutes; give a small task environment: "
        "shared.",
        {
            "type": "object",
            "properties": {
                "check": {
                    "type": "object",
                    "description": "For a check: the entry and the revision to check",
                    "properties": {
                        "entry": {"type": "string", "description": "A handle, such as R-1"},
                        "revision": {"type": "integer", "minimum": 1},
                    },
                    "required": ["entry", "revision"],
                    "additionalProperties": False,
                },
                "note": {
                    "type": "string",
                    "maxLength": MAX_NOTE,
                    "description": "For a check, optional: what the checker should know, "
                    "such as where a file is; never the verdict you expect",
                },
                "task": {
                    "type": "string",
                    "maxLength": MAX_TEXT,
                    "description": "For a task: what to do, and what to return",
                },
                "context": {
                    "type": "string",
                    "maxLength": MAX_TEXT,
                    "description": "For a task, optional: what it needs to know",
                },
                "inputs": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_INPUT},
                    "maxItems": MAX_INPUTS,
                    "description": "For a task, optional: paths in the session workspace or "
                    "shared/, documents and ledger entries it should read",
                },
                "track": {
                    "type": "string",
                    "description": "For a task, optional: a track of the session's active "
                    "plan, whose steps it records its progress on; one task at a time",
                },
                "delegate": {
                    "type": "boolean",
                    "description": "For a task, optional: let it delegate tasks of its own, "
                    "once more, where the project allows two levels",
                },
                "issue": {
                    "type": "string",
                    "description": "For a task, optional: the issue it works on, such as I-2; "
                    "its outcome, what it dismissed and the obstructions it claims are "
                    "recorded there as work",
                },
                "deliverables": {
                    "type": "array",
                    "maxItems": MAX_DELIVERABLES,
                    "description": "For a task, optional: what it is to hand back, numbered "
                    "d1, d2, … in this order; its result accounts for each",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE},
                            "kind": {
                                "type": "string",
                                "enum": ["artifact", "file", "entry", "answer"],
                                "description": "artifact: a file revision its calls record "
                                "or publish; file: a path its calls write; entry: a ledger "
                                "entry it creates or revises; answer: a short answer in its "
                                "result",
                            },
                            "description": {
                                "type": "string",
                                "maxLength": MAX_DESCRIPTION,
                                "description": "What counts as delivering it",
                            },
                        },
                        "required": ["title", "kind", "description"],
                        "additionalProperties": False,
                    },
                },
                "environment": {
                    "type": "string",
                    "enum": ["shared", "own"],
                    "description": "For a task, optional, where the project gives tasks "
                    "environments of their own: shared runs it in the session's environment",
                },
                "profile": {
                    "type": "string",
                    "description": "For a task in its own environment, optional: a child "
                    "profile of the project's by name",
                },
                "connectors": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_CONNECTOR},
                    "maxItems": MAX_CONNECTORS,
                    "description": "For a task, optional: the connectors it may use, some of "
                    "yours: each a server, SERVER/BEHIND as mcp_tools' index names it, "
                    "SERVER/TOOL, or SERVER/PATTERN such as "
                    "crypto-research-mcp/e-print-mcp_search_*. Without it, it gets all of yours",
                },
                "references": {
                    **SCHEMA,
                    "description": "For a task, optional: your records for its state "
                    "block, each a message (msg-…), from and to (at most "
                    f"{MAX_SPAN} positions of your run), entry or file",
                },
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "wait_delegations",
        "Keep this turn open without calling your model until all the delegated runs "
        "named (or the first with mode=first) end, or a user message arrives. Returns the "
        "outcomes of those that ended, with their whole reports, as many as fit; call it "
        "again for the rest. Any ended delegation of this session can be named, such as "
        "one a notice summarized.",
        {
            "type": "object",
            "properties": {
                "delegation_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_WAITED,
                },
                "mode": {"type": "string", "enum": ["all", "first"]},
            },
            "required": ["delegation_ids"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "delegation_status",
        "Read a delegation of this session: a running one's limits, or an ended one's "
        "whole outcome.",
        {
            "type": "object",
            "properties": {"delegation_id": {"type": "string"}},
            "required": ["delegation_id"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "cancel_delegation",
        "Cancel a delegated run you started. It ends cancelled with its record kept; its "
        "executions are asked to stop. A returned task that awaits your review is closed "
        "as cancelled instead of accepted. Returns its outcome.",
        {
            "type": "object",
            "properties": {"delegation_id": {"type": "string"}},
            "required": ["delegation_id"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "accept_delegation",
        "Accept a returned task you delegated, once you have reviewed its outcome: it "
        "closes, its own environment hands its files back and is released, and its kernels "
        "stop. To have it continue instead, send it feedback with message_child. A task "
        "you leave unreviewed is accepted when your run ends. Returns its outcome.",
        {
            "type": "object",
            "properties": {
                "delegation_id": {"type": "string"},
                "note": {
                    "type": "string",
                    "maxLength": MAX_NOTE,
                    "description": "Optional: why you accept it, one line, kept with it",
                },
            },
            "required": ["delegation_id"],
            "additionalProperties": False,
        },
    ),
)
NAMES: Final = frozenset(spec.name for spec in SPECS)


class Accepting(Contract):
    """``accept_delegation``'s arguments: a returned task, and the parent's note on it."""

    delegation_id: str = Field(min_length=1, max_length=128)
    note: Annotated[str, BeforeValidator(_note), Field(max_length=MAX_NOTE)] = ""


def offered(models: Sequence[str]) -> tuple[ToolSpec, ...]:
    """The tools a run is offered: ``delegate`` takes a ``model`` when the project lets
    a brief choose one of ``models`` (PD-28)."""
    if not models:
        return SPECS
    delegate, *rest = SPECS
    schema = delegate.parameters
    model = {
        "type": "string",
        "enum": list(models),
        "description": "Optional: the model alias the delegated run runs on, of those the "
        "project allows; without it, the delegated role's",
    }
    parameters = {**schema, "properties": {**schema["properties"], "model": model}}
    return (ToolSpec(delegate.name, delegate.description, parameters), *rest)


EFFECTS: Final[dict[str, Effect]] = {
    "wait_delegations": "read",
    "delegation_status": "read",
    "cancel_delegation": "cancel",
    "accept_delegation": "cancel",
}
"""``delegate`` is an effect, which the plan fence and the session's lease gate."""
