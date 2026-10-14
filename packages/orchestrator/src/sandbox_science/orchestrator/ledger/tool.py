# SPDX-License-Identifier: MIT
"""The ledger tools, as the harness's agents use them (docs/specs/ledger.md).

An agent lists and reads entries and their histories, changes them with a mutation
(LG-30), and imports a results file that code in its sandbox wrote (LG-41): code has no
route to the ledger, and only the agent decides what enters it. Every change is the
run's: its session, role, model and tool call; the arguments cannot name another
author. Changes are effects, so the plan fence governs them; reads are reads.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import ExecutionContext, FileContent, ReadFileRequest
from sandbox_science.orchestrator.documents.errors import (
    INTERNAL,
    INVALID_REQUEST,
    DocumentError,
)
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.hooks import Effect
from sandbox_science.orchestrator.model import ToolSpec

from .models import PREFIXES, STATUSES, Mutation
from .store import Ledger

Json = dict[str, Any]

MAX_FILE: Final = 1024 * 1024
MAX_LINES: Final = 256
"""A results file's bounds (LG-41)."""

LEDGER_EFFECTS: Final[dict[str, Effect]] = {
    "ledger_list": "read",
    "ledger_read": "read",
    "ledger_history": "read",
}
"""The ledger tools that only read; the others are effects, which the plan fence gates."""

_ENTRY = {"type": "string", "description": "An entry's handle (such as C-12) or ID"}
_LINK = {
    "type": "object",
    "description": (
        "A link: its kind, and one target: entry (a handle, an ID, or new:<key> for an "
        "entry this change creates), finding (a review finding's ID), or document with "
        "revision and block. revision also pins an entry target; note states a "
        "compares_to link's difference"
    ),
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "answers",
                "assumes",
                "builds_on",
                "restates",
                "compares_to",
                "supersedes",
                "split_into",
                "affects",
                "concerns",
            ],
        },
        "entry": {"type": "string"},
        "finding": {"type": "string"},
        "document": {"type": "string"},
        "revision": {"type": "integer"},
        "block": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["kind"],
    "additionalProperties": False,
}
_CHANGE = {
    "type": "object",
    "description": (
        "One entry to create (give type, title and statement; origin project for a "
        "claim or result; key to link to it from this change's other entries) or to "
        "revise (give entry and the revision you read as expected, then only what "
        "changes). fields are the type's own: a claim's kind; a result's kind, "
        "category, conditions and measures; a reference's identifiers (doi, eprint, "
        "arxiv, nist, isbn, url), authors, published, version or retrieved, and pdf "
        "(artifact_id, revision). links replaces the entry's outgoing links"
    ),
    "properties": {
        "entry": _ENTRY,
        "expected": {"type": "integer", "minimum": 1},
        "key": {"type": "string"},
        "type": {"type": "string", "enum": list(PREFIXES)},
        "origin": {"type": "string", "enum": ["project", "published"]},
        "title": {"type": "string"},
        "statement": {
            "type": "string",
            "description": "Markdown with $…$ math; a reference's citation text",
        },
        "status": {
            "type": "string",
            "enum": sorted({status for statuses in STATUSES.values() for status in statuses}),
        },
        "fields": {"type": "object"},
        "links": {"type": "array", "items": _LINK, "maxItems": 64},
    },
    "additionalProperties": False,
}

SPECS: Final = (
    ToolSpec(
        "ledger_list",
        "List the project's ledger entries: issues, claims, results, assumptions, "
        "decisions, tasks and references, each with its handle, status and title, oldest "
        "first. Filter by type or current status; give after (the last entry you got) "
        "for the next page.",
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(PREFIXES)},
                "status": {"type": "string"},
                "after": _ENTRY,
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "ledger_read",
        "Read a ledger entry: its statement, status, fields, its links, the links other "
        "entries hold to it, and its revision, which a change to it must name. Give "
        "revision to read an earlier one.",
        {
            "type": "object",
            "properties": {"entry": _ENTRY, "revision": {"type": "integer", "minimum": 1}},
            "required": ["entry"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "ledger_history",
        "List a ledger entry's revisions, newest first: each one's status, title, "
        "author, reason, and whether its statement changed.",
        {
            "type": "object",
            "properties": {"entry": _ENTRY, "before": {"type": "integer", "minimum": 1}},
            "required": ["entry"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "ledger_change",
        "Create or revise ledger entries in one change: all of it is recorded, or none. "
        "Each revision names the revision you read; if the entry changed since, read it "
        "again. A new entry starts in its first status (open, proposed, active, todo). "
        "Retrying with the same operation_id returns the first result. A delegated agent "
        "creates entries but changes none.",
        {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why, in a sentence"},
                "changes": {"type": "array", "items": _CHANGE, "minItems": 1, "maxItems": 32},
                "operation_id": {"type": "string"},
            },
            "required": ["reason", "changes"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "ledger_import",
        "Import a results file that code in your sandbox wrote: JSON Lines, each line "
        "one change as ledger_change takes it, with its operation_id. Every line is "
        "checked before anything is recorded, and all are recorded with your authority, "
        "or none. Read the file first and import only what you stand behind. A line "
        "imported before is not recorded again.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The file's path"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
)

Reader = Callable[[ReadFileRequest], Awaitable[FileContent]]


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _failure(kind: str, message: str, **details: object) -> tuple[str, Json]:
    return _dump({"error": {"kind": kind, "message": message, **details}}), {"error": kind}


class LedgerTool:
    """One run's ledger tools."""

    specs: Final = SPECS

    def __init__(
        self,
        ledger: Ledger,
        context: ExecutionContext,
        *,
        run_id: str,
        role: str,
        alias: str | None = None,
        read: Reader | None = None,
    ) -> None:
        self.ledger, self.context, self.run_id = ledger, context, run_id
        self.role, self.alias, self.read = role, alias, read

    def author(self, call_id: str | None, model: str | None) -> Author:
        return Author(
            kind="agent",
            session_id=self.context.session_id,
            run_id=self.run_id,
            role=self.role,
            model=model,
            alias=self.alias,
            tool_call=call_id,
        )

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        operation: str,
        call_id: str | None = None,
        model: str | None = None,
    ) -> tuple[str, Json]:
        """One ledger tool call; a refusal comes back as an error for the model."""
        project = self.context.project_id
        author = self.author(call_id, model)
        handlers: dict[str, Callable[[], Any]] = {
            "ledger_list": lambda: self.ledger.entries(
                project,
                kind=args.get("type"),
                status=args.get("status"),
                after=args.get("after"),
                limit=args.get("limit", 50),
            ),
            "ledger_read": lambda: self.ledger.entry(project, args["entry"], args.get("revision")),
            "ledger_history": lambda: self.ledger.history(
                project, args["entry"], before=args.get("before")
            ),
            "ledger_change": lambda: self.ledger.change(
                project, {"operation_id": operation, **args}, author
            ),
        }
        spec = next((spec for spec in SPECS if spec.name == name), None)
        if spec is None:
            return _failure(INVALID_REQUEST, f"there is no tool named {name!r}")
        # Unknown arguments are refused, so that none can stand for an author (RA-16).
        unknown = sorted(set(args) - set(cast(Json, spec.parameters["properties"])))
        missing = [key for key in spec.parameters.get("required", ()) if key not in args]
        if unknown or missing:
            return _failure(
                INVALID_REQUEST, f"unknown arguments {unknown}, or missing arguments {missing}"
            )
        try:
            if name == "ledger_import":
                found = await self._import(args["path"], author)
            else:
                found = await asyncio.to_thread(handlers[name])
        except DocumentError as error:
            return _failure(error.kind, error.message, **error.details)
        except (TypeError, ValueError, KeyError) as error:
            return _failure(INVALID_REQUEST, f"invalid arguments: {error}")
        text = found if isinstance(found, str) else found.model_dump_json()
        record: Json = {"ledger": name}
        if name in ("ledger_change", "ledger_import"):
            record["result"] = json.loads(text)
        return text, record

    async def _import(self, path: object, author: Author) -> str:
        """Check every line of a results file, then apply them all, or none (LG-41)."""
        if self.read is None:
            raise DocumentError(INVALID_REQUEST, "this run has no workspace to read files from")
        if not isinstance(path, str):
            raise DocumentError(INVALID_REQUEST, "path names a file")
        try:
            content = await self.read(
                ReadFileRequest.model_validate(
                    {"context": self.context, "path": path, "length": MAX_FILE + 1}
                )
            )
        except ValidationError:
            raise DocumentError(INVALID_REQUEST, "path names no file in the workspace") from None
        if content.size > MAX_FILE or not content.eof:
            raise DocumentError(INVALID_REQUEST, f"a results file holds at most {MAX_FILE} bytes")
        try:
            data = base64.b64decode(content.content_b64, validate=True)
        except binascii.Error:
            raise DocumentError(
                INTERNAL, "the worker sent file content that is not base64"
            ) from None
        mutations = parse_results(data)
        source = {"file": content.path, "sha256": content.sha256}
        results = await asyncio.to_thread(
            self.ledger.apply,
            self.context.project_id,
            [(m, {**source, "line": line}) for line, m in mutations],
            author,
        )
        return _dump(
            {
                **source,
                "imported": [
                    {"line": line, **result.model_dump(mode="json")}
                    for (line, _), result in zip(mutations, results, strict=True)
                ],
            }
        )


def parse_results(data: bytes) -> list[tuple[int, Mutation]]:
    """A results file's mutations with their line numbers; every line is checked, and
    the first bad one refuses the file, naming its line (LG-36, LG-41)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DocumentError(INVALID_REQUEST, "a results file is UTF-8 text") from None
    found: list[tuple[int, Mutation]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(found) == MAX_LINES:
            raise DocumentError(INVALID_REQUEST, f"a results file holds at most {MAX_LINES} lines")
        try:
            found.append((number, Mutation.model_validate_json(line)))
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "line"
            raise DocumentError(
                INVALID_REQUEST, f"line {number}: {where}: {first['msg']}", {"line": number}
            ) from None
    if not found:
        raise DocumentError(INVALID_REQUEST, "the results file holds no changes")
    return found
