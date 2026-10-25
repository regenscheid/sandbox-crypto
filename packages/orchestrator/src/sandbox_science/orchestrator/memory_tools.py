# SPDX-License-Identifier: MIT
"""The project memory's tools, as main and delegated runs use them (PM-3).

``memory_list`` reads the accepted entries; ``memory_propose`` proposes adding, changing
or removing one, which waits for the user. No tool changes an entry. A proposal is the
run's: its session, role, model alias, the model that answered and the tool call; the
arguments cannot name another author. Listing is a read. A proposal changes nothing until
the user accepts it, so the plan fence lets it pass; a run that no longer holds its
session may not propose.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from typing import Any, Final

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.documents.errors import INVALID_REQUEST, DocumentError
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.hooks import Effect
from sandbox_science.orchestrator.model import ToolSpec

from .memory import MAX_ANCHORS, MAX_REASON, MAX_TEXT, ProjectMemory, anchor_names, prefix

Json = dict[str, Any]

MEMORY_EFFECTS: Final[dict[str, Effect]] = {
    "memory_list": "read",
    "memory_propose": "proposal",
}
"""Listing reads; a proposal changes nothing until the user accepts it, so the plan
fence lets it pass (the coordinator, 2026-10-15), unlike ``ledger_change``."""

_ANCHORS: Final = {
    "type": "array",
    "maxItems": MAX_ANCHORS,
    "description": (
        "Where the note comes from, besides this call, as the ledger's anchors are: needed "
        "when basis is user_quoted (a message the user wrote, with a quote of it, word for "
        "word) or observed (a tool result, an artifact revision, or a range of a tool's "
        "output that shows every number of the text)"
    ),
    "items": {
        "type": "object",
        "description": (
            "One of: message (an archive ID, msg-…, from a message's header; quote for the "
            "user's words), call (a tool call's ID, or the message ID of its result, for "
            "the result; start and end for a range of its output), artifact with revision, "
            "document with revision and block, entry (a ledger entry, with revision), or "
            "memory (a memory entry at a revision, such as mem-3@2)"
        ),
        "properties": {
            "message": {"type": "string"},
            "call": {"type": "string"},
            "quote": {"type": "string"},
            "start": {"type": "integer", "minimum": 0},
            "end": {"type": "integer", "minimum": 0},
            "artifact": {"type": "string"},
            "document": {"type": "string"},
            "entry": {"type": "string"},
            "memory": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
            "block": {"type": "string"},
            "sha256": {"type": "string"},
        },
        "additionalProperties": False,
    },
}

SPECS: Final = (
    ToolSpec(
        "memory_list",
        "List the project memory: the notes on how to work in this project that the user "
        "accepted, in order, each with its ID and revision. A proposal to change or remove "
        "an entry names both. Each note begins with a prefix: its age, its basis (user: "
        "the user's own; user, quoted; observed; interpreted; or not recorded, accepted "
        "before the basis was kept), its ID, revision and scope; its anchors follow. Prefer "
        "user and observed notes to interpreted and not recorded ones, and name a note's ID "
        "when you rely on it. A long memory comes in parts: give from (the next_from you "
        "got) to read on.",
        {
            "type": "object",
            "properties": {"from": {"type": "integer", "minimum": 0}},
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "memory_propose",
        "Propose one change to the project memory, for the user to accept or decline; "
        "nothing changes until then. Propose what later runs of this project should know "
        "about how to work: a convention or notation, a settled fact about the environment "
        "or the tools, the user's preference, or a pitfall to avoid. Results and evidence "
        "belong in documents, artifacts and the ledger, not here. add gives text; change "
        "gives the entry, the revision you read and the new text; remove gives the entry "
        f"and the revision you read. Text is at most {MAX_TEXT} characters, the reason at "
        f"most {MAX_REASON}. basis says what the note rests on: user_quoted for what the user "
        "said (anchor their message, with a quote), observed for what a tool result, an "
        "artifact or a source showed (anchor it: every number of the text must be in it), "
        "interpreted, the default, for your reading; code checks the basis against the "
        "anchors and refuses with provenance_required. The user's acceptance does not make "
        "a note the user's own. A run makes at most 10 proposals.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "change", "remove"]},
                "entry": {"type": "string", "description": "An entry's ID, such as mem-3"},
                "revision": {"type": "integer", "minimum": 1},
                "text": {"type": "string", "description": "Markdown with $…$ math"},
                "reason": {"type": "string", "description": "Why, in a sentence"},
                "basis": {
                    "type": "string",
                    "enum": ["user_quoted", "observed", "interpreted"],
                    "description": "What the note rests on; interpreted by default",
                },
                "anchors": _ANCHORS,
            },
            "required": ["action", "reason"],
            "additionalProperties": False,
        },
    ),
)


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _failure(kind: str, message: str) -> tuple[str, Json]:
    return _dump({"error": {"kind": kind, "message": message[:1000]}}), {"error": kind}


class MemoryTool:
    """One run's memory tools. A listing fits ``output_chars``: a long one comes in parts,
    each saying where the next begins."""

    specs: Final = SPECS

    def __init__(
        self,
        memory: ProjectMemory,
        context: ExecutionContext,
        *,
        run_id: str,
        role: str,
        alias: str | None = None,
        output_chars: int = 20000,
    ) -> None:
        self.memory, self.context, self.run_id = memory, context, run_id
        self.role, self.alias, self.budget = role, alias, output_chars

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        operation: str,
        call_id: str | None = None,
        model: str | None = None,
    ) -> tuple[str, Json]:
        """One memory tool call; a refusal comes back as an error for the model."""
        try:
            if name == "memory_list":
                return await asyncio.to_thread(self._list, args), {"memory": name}
            if name != "memory_propose":
                return _failure(INVALID_REQUEST, f"there is no tool named {name!r}")
            author = Author(
                kind="agent",
                session_id=self.context.session_id,
                run_id=self.run_id,
                role=self.role,
                model=model,
                alias=self.alias,
                tool_call=call_id,
            )
            proposal, replayed = await asyncio.to_thread(
                self.memory.propose, self.context.project_id, args, author, operation
            )
        except DocumentError as error:
            return _failure(error.kind, error.message)
        result = {
            "proposal_id": proposal.proposal_id,
            "state": proposal.state,
            "replayed": replayed,
            "note": "It waits for the user, who accepts or declines it; until then the "
            "memory is unchanged.",
        }
        return _dump(result), {"memory": name, "result": result}

    def _list(self, args: dict[str, Any]) -> str:
        start = args.get("from", 0)
        if set(args) - {"from"} or type(start) is not int or start < 0:
            raise DocumentError(INVALID_REQUEST, "memory_list takes only from, a count")
        memory = self.memory.memory(self.context.project_id)
        answer: Json = {"version": memory.version, "entries": [], "next_from": None}
        now = self.memory.clock()
        for entry in memory.entries[start:]:
            # The prefix first, then the text: what the agent reads before the text it
            # may rely on (PM-13). The anchors follow the text.
            value: Json = {
                "entry_id": entry.entry_id,
                "revision": entry.revision,
                "prefix": prefix(entry, now),
                "text": entry.text,
                "basis": entry.basis,
                "accepted": entry.accepted,
                "recorded_at": entry.recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "scope": entry.scope,
            }
            if entry.anchors:
                value["anchors"] = anchor_names(entry)
            # Measured with the longest next_from the answer can end with.
            if len(_dump({**answer, "entries": [*answer["entries"], value], "next_from": 999})) > (
                self.budget
            ):
                answer["next_from"] = start + len(answer["entries"])
                break
            answer["entries"].append(value)
        return _dump(answer)
