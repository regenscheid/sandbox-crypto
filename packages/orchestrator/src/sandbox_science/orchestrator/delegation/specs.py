# SPDX-License-Identifier: MIT
"""The delegate tools as the model sees them (plans-and-delegation spec, PD-33).

The first brief is narrow: a check of one ledger entry at one revision, which a run
with a fresh context does (LG-17). The platform writes a check's task and lists the
entry's evidence as its inputs; the parent adds at most a short note, which the child
reads as the requester's note, not evidence (coordinator, 2026-10-15).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.agent_text import agent_text
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.hooks import Effect
from sandbox_science.orchestrator.model import ToolSpec

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


class CheckBrief(Contract):
    """What ``delegate`` takes: the entry and revision to check, and the parent's note.
    A brief carries no task or inputs of the parent's, which could carry the verdict."""

    check: CheckTarget
    note: Annotated[str, BeforeValidator(_note), Field(max_length=MAX_NOTE)] = ""
    model: str | None = Field(default=None, min_length=1, max_length=128)
    """A model alias the project lets a brief choose (PD-28)."""


class WaitRequest(Contract):
    delegation_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=MAX_WAITED)
    mode: Literal["all", "first"] = "all"


class Named(Contract):
    """``delegation_status`` and ``cancel_delegation`` name one delegation."""

    delegation_id: Identifier


SPECS: Final = (
    ToolSpec(
        "delegate",
        "Start a delegated run that independently checks one ledger entry at one "
        "revision, such as a proved result before you establish it. The platform writes "
        "its task: check whether the entry holds at that revision, from its statement, "
        "fields and evidence; it lists the entry's evidence as the inputs, and starts the "
        "checker with a fresh context: it sees none of your conversation, and has kernels "
        "of its own. Your note, if any, reaches it as the requester's note, not evidence. "
        "It runs while you go on; this returns its delegation_id at once. Its report "
        "reaches you as a delegation notice, or as the result of wait_delegations. You "
        "cannot give your final answer while it runs. Its result is its claim: a verdict, "
        "a status and a summary; the outcome says how the platform saw it end. A check "
        "that returned confirmed counts as the entry's independent check once you add it "
        "as evidence: role independent_check, delegation its ID, checked the revision it "
        "checked; one of the same material that refuted it, or completed inconclusive, "
        "keeps any from counting.",
        {
            "type": "object",
            "properties": {
                "check": {
                    "type": "object",
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
                    "description": "Optional: what the checker should know, such as where "
                    "a file is; never the verdict you expect",
                },
            },
            "required": ["check"],
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
        "executions are asked to stop. Returns its outcome.",
        {
            "type": "object",
            "properties": {"delegation_id": {"type": "string"}},
            "required": ["delegation_id"],
            "additionalProperties": False,
        },
    ),
)
NAMES: Final = frozenset(spec.name for spec in SPECS)


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
        "description": "Optional: the model alias the checker runs on, of those the "
        "project allows; without it, the delegated role's",
    }
    parameters = {**schema, "properties": {**schema["properties"], "model": model}}
    return (ToolSpec(delegate.name, delegate.description, parameters), *rest)


EFFECTS: Final[dict[str, Effect]] = {
    "wait_delegations": "read",
    "delegation_status": "read",
    "cancel_delegation": "cancel",
}
"""``delegate`` is an effect, which the plan fence and the session's lease gate."""
