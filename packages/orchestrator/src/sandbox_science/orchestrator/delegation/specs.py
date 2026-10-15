# SPDX-License-Identifier: MIT
"""The delegate tools as the model sees them (plans-and-delegation spec, PD-33).

The first brief is narrow: a check of one ledger entry at one revision, which a run
with a fresh context does (LG-17).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field

from sandbox_science.contracts import Identifier
from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.hooks import Effect
from sandbox_science.orchestrator.model import ToolSpec

MAX_TEXT: Final = 8000
MAX_INPUTS: Final = 20
MAX_INPUT: Final = 512
MAX_WAITED: Final = 10


class CheckTarget(Contract):
    entry: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=999_999_999)


class CheckBrief(Contract):
    """What ``delegate`` takes: the task, what the child needs to know, its inputs, and
    the entry and revision it checks."""

    task: str = Field(min_length=1, max_length=MAX_TEXT)
    context: str = Field(default="", max_length=MAX_TEXT)
    inputs: tuple[Annotated[str, Field(min_length=1, max_length=MAX_INPUT)], ...] = Field(
        default=(), max_length=MAX_INPUTS
    )
    check: CheckTarget


class WaitRequest(Contract):
    delegation_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=MAX_WAITED)
    mode: Literal["all", "first"] = "all"


class Named(Contract):
    """``delegation_status`` and ``cancel_delegation`` name one delegation."""

    delegation_id: Identifier


_TEXT = {"type": "string", "maxLength": MAX_TEXT}
SPECS: Final = (
    ToolSpec(
        "delegate",
        "Start a delegated run that independently checks one ledger entry at one "
        "revision, such as a proved result before you establish it. The delegated run "
        "starts with a fresh context: it sees only this brief, not your conversation, "
        "and has kernels of its own, so hand over state through files. It runs while you "
        "go on; this returns its delegation_id at once. Its report reaches you as a "
        "delegation notice, or as the result of wait_delegations. You cannot give your "
        "final answer while it runs. Its result is its claim: a verdict, a status and a "
        "summary; the outcome says how the platform saw it end.",
        {
            "type": "object",
            "properties": {
                "task": {**_TEXT, "description": "What to check and how"},
                "context": {**_TEXT, "description": "What the checker needs to know"},
                "inputs": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_INPUT},
                    "maxItems": MAX_INPUTS,
                    "description": "Paths in the session workspace or shared/, documents "
                    "and ledger entries it should read",
                },
                "check": {
                    "type": "object",
                    "properties": {
                        "entry": {"type": "string", "description": "A handle, such as R-1"},
                        "revision": {"type": "integer", "minimum": 1},
                    },
                    "required": ["entry", "revision"],
                    "additionalProperties": False,
                },
            },
            "required": ["task", "check"],
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
EFFECTS: Final[dict[str, Effect]] = {
    "wait_delegations": "read",
    "delegation_status": "read",
    "cancel_delegation": "cancel",
}
"""``delegate`` is an effect, which the plan fence and the session's lease gate."""
