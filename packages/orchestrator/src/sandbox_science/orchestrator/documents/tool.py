# SPDX-License-Identifier: MIT
"""The document tool, as the harness's agents use it (LD-15, LD-16).

An agent lists, reads, creates, patches and deletes living documents. A read shows each
block's ID and kind beside its text, and each formula's ID and TeX; the IDs are not in
the text. A patch names the revision it was written from, an operation ID (a retry with
the same one returns the first result), a reason, and its operations. Every change is
attributed to the run: its session, role, model and tool call (LD-10); the arguments
cannot name another author.

The harness keeps track of the documents the run has read or written during its turn.
After each tool call, the run's next safe point, it tells the run which of them someone
else changed since, in which revisions and which blocks (LD-15).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import JsonValue

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.aio import lease, threaded
from sandbox_science.orchestrator.control.notices import MAX_PER_RUN
from sandbox_science.orchestrator.hooks import (
    Closing,
    Effect,
    Notice,
    Point,
    RunView,
    TurnHook,
)
from sandbox_science.orchestrator.model import ToolSpec
from sandbox_science.orchestrator.notices import REQUESTS

from . import annotation_tools
from .annotations import Annotations
from .errors import INVALID_REQUEST, DocumentError
from .identity import Mapped
from .models import Author, ChangeResult, DocumentInfo, RevisionInfo
from .operations import unlay
from .store import DocumentStore

if TYPE_CHECKING:
    from sandbox_science.orchestrator.projects import RunRecord

Json = dict[str, Any]

DOCUMENT_EFFECTS: Final[dict[str, Effect]] = {
    "document_list": "read",
    "document_read": "read",
    "document_comments": "read",
    "document_comment_read": "read",
}
"""The document tools that only read; the others are effects."""

_DOCUMENT = {
    "type": "string",
    "description": "The document's ID, or its name (this session's workbook first)",
}
_OPERATION = {
    "type": "object",
    "description": (
        "One of: {op: replace, block, markdown}; {op: insert, markdown, and one of before, "
        "after (block IDs) or at: start|end}; {op: move, block, and before or after}; "
        "{op: delete, block}; {op: replace_math, math, tex}; {op: replace_text, block, "
        "quote, with}, where quote occurs exactly once in the block's text"
    ),
    "properties": {
        "op": {
            "type": "string",
            "enum": ["replace", "insert", "move", "delete", "replace_math", "replace_text"],
        },
        "block": {"type": "string"},
        "markdown": {"type": "string"},
        "before": {"type": "string"},
        "after": {"type": "string"},
        "at": {"type": "string", "enum": ["start", "end"]},
        "math": {"type": "string"},
        "tex": {"type": "string"},
        "quote": {"type": "string"},
        "with": {"type": "string"},
    },
    "required": ["op"],
    "additionalProperties": False,
}

SPECS: Final = (
    ToolSpec(
        "document_list",
        "List the project's living documents, and the sessions' workbook documents: ID, "
        "name, title, scope and current revision.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        "document_read",
        "Read a living document, the current revision unless you name one, as blocks in "
        "order: each with its ID, its kind, and its text without the markers of the lists "
        "and quotes around it, and the ID and TeX of each formula in it. The IDs are not "
        "part of the text; name them in patches.",
        {
            "type": "object",
            "properties": {
                "document": _DOCUMENT,
                "revision": {"type": "integer", "minimum": 1},
                "from_block": {
                    "type": "string",
                    "description": "Start at this block, to read the rest of a long document",
                },
            },
            "required": ["document"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "document_create",
        "Create a living document: Markdown with LaTeX math, $...$ inline and $$...$$ "
        "displayed, optional YAML front matter with title and macros, and theorem-like "
        "blocks as '::: lemma' to ':::'. scope 'project' makes a project document, "
        "'session' one of this session's workbook.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "A short file name, such as ff"},
                "title": {"type": "string"},
                "text": {"type": "string"},
                "scope": {"type": "string", "enum": ["project", "session"]},
                "reason": {"type": "string"},
            },
            "required": ["name", "title", "text", "scope"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "document_patch",
        "Change a living document. Name the revision you read as base_revision, give a "
        "short reason, and operations that each name the blocks or math they touch. "
        "Markdown you write is laid inside the lists and quotes around its place. A retry "
        "with the same operation_id returns the first result. If someone changed what you "
        "touch after your base revision, nothing is written: you get a conflict with both "
        "versions, so read the document again and patch the new revision. A patch that "
        "would change blocks it does not name, as an unclosed code fence would, is refused.",
        {
            "type": "object",
            "properties": {
                "document": _DOCUMENT,
                "base_revision": {"type": "integer", "minimum": 1},
                "operation_id": {
                    "type": "string",
                    "description": "Unique for this change, to retry it safely",
                },
                "reason": {"type": "string"},
                "operations": {"type": "array", "items": _OPERATION, "minItems": 1},
            },
            "required": ["document", "base_revision", "reason", "operations"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "document_delete",
        "Delete a living document this session created. Its history remains.",
        {
            "type": "object",
            "properties": {"document": _DOCUMENT, "reason": {"type": "string"}},
            "required": ["document", "reason"],
            "additionalProperties": False,
        },
    ),
)


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _failure(kind: str, message: str, **details: object) -> tuple[str, Json]:
    return _dump({"error": {"kind": kind, "message": message, **details}}), {"error": kind}


def _listing(info: DocumentInfo) -> Json:
    entry: Json = {
        "id": info.document_id,
        "name": info.name,
        "title": info.title,
        "scope": info.scope,
        "revision": info.head,
    }
    if info.session_id is not None:
        entry["session"] = info.session_id
    if info.takes_suggestions:
        entry["takes_suggestions"] = True
    return entry


def view_blocks(mapped: Mapped, start: int = 0) -> list[Json]:
    """The read view's blocks: leaves with their text, containers with their markers."""
    parsed = mapped.parsed
    found: list[Json] = []
    for block in parsed.blocks[start:]:
        entry: Json = {"id": mapped.block_ids[block.index], "kind": block.kind}
        parent = mapped.parent_id(block.index)
        if parent is not None:
            entry["in"] = parent
        attrs = {key: value for key, value in block.attrs.items() if value not in (None, "")}
        if block.kind == "container":
            entry["text"] = block.text.split("\n", 1)[0]
            entry.update({key: attrs[key] for key in ("container", "label") if key in attrs})
        elif block.kind == "list":
            entry["ordered"] = attrs.get("ordered", False)
        elif block.kind == "list_item":
            entry["marker"] = attrs.get("marker")
        elif block.kind != "quote":
            entry["text"] = block.text
            if block.kind == "equation" and attrs.get("label"):
                entry["label"] = attrs["label"]
            if attrs.get("figure"):
                entry["figure"] = True
        if block.spans:
            entry["math"] = [
                {
                    "id": mapped.span_ids[index],
                    "tex": unlay(parsed.spans[index].tex, block.continuation),
                }
                | ({"display": True} if parsed.spans[index].kind == "display" else {})
                for index in block.spans
            ]
        found.append(entry)
    return found


class DocumentTool:
    """The document tools of one agent run, in one session of a project."""

    def __init__(
        self,
        store: DocumentStore,
        context: ExecutionContext,
        *,
        run_id: str,
        role: str = "main",
        alias: str | None = None,
        output_chars: int = 20000,
    ) -> None:
        self.store = store
        self.context = context
        self.run_id = run_id
        self.role = role
        self.alias = alias
        self.output_chars = output_chars
        self.specs = SPECS + annotation_tools.SPECS
        self.request_seen: set[str] = set()
        self.request_delivered: set[str] = set()
        self._seen: dict[str, int] = {}
        """The revision of each document this run last saw: read, wrote or was told of."""
        self._names: dict[str, str] = {}

    async def request_notices(self) -> list[Json]:
        """Only user-addressed requests for this session's main role reach the model."""
        if self.role != "main":
            return []
        return await asyncio.to_thread(
            Annotations(self.store).pending,
            self.context.project_id,
            self.context.session_id,
            self.run_id,
            seen=self.request_seen,
            max_chars=20000,
        )

    def requests_waiting(self) -> bool:
        """Whether a work request waits that this run may still be offered: one no run
        holds, while the run is under its limit. It ends a job wait (run-service spec
        RS-7)."""
        if self.role != "main" or len(self.request_seen) >= MAX_PER_RUN:
            return False
        scope = (self.context.project_id, self.context.session_id)
        return bool(Annotations(self.store).waiting(scope))

    @asynccontextmanager
    async def active_requests(self) -> AsyncGenerator[None]:
        if self.role != "main":
            yield
            return

        annotations = Annotations(self.store)
        scope = (self.context.project_id, self.context.session_id, self.run_id)
        failed = True
        try:
            # A lease lost across a long outage may safely replay the request.
            async with lease(partial(annotations.renew, *scope), interval=30, name="request"):
                yield
            failed = False
        finally:
            # A failed run keeps no request: each may reach a later run.
            await threaded(
                annotations.release, *scope, keep=None if failed else self.request_delivered
            )

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
        """One document tool call; a refusal comes back as an error for the model."""
        handlers: dict[str, Callable[[], tuple[str, Json]]] = {
            "document_list": lambda: self._list(args),
            "document_read": lambda: self._read(args),
            "document_create": lambda: self._create(args, operation, call_id, model),
            "document_patch": lambda: self._patch(args, operation, call_id, model),
            "document_delete": lambda: self._delete(args, operation, call_id, model),
        }
        if name in annotation_tools.NAMES:
            handlers[name] = lambda: annotation_tools.call(
                self.store,
                self.context,
                name,
                args,
                operation,
                self.author(call_id, model),
                self.output_chars,
            )
        handler = handlers.get(name)
        if handler is None:
            return _failure(INVALID_REQUEST, f"there is no tool named {name!r}")
        # Unknown arguments are refused, so that none can stand for an author (RA-16).
        spec = next(spec for spec in self.specs if spec.name == name)
        unknown = sorted(set(args) - set(cast(dict[str, Any], spec.parameters["properties"])))
        if unknown:
            return _failure(INVALID_REQUEST, f"unknown arguments: {', '.join(unknown)}")
        try:
            return await asyncio.to_thread(handler)
        except DocumentError as error:
            return _failure(error.kind, error.message, **error.details)

    async def notices(self) -> list[str]:
        """What others changed in the documents this run has seen (LD-15)."""
        if not self._seen:
            return []
        return await asyncio.to_thread(self._notices)

    def _notices(self) -> list[str]:
        found: list[str] = []
        heads = self.store.heads(list(self._seen))
        for document_id, seen in list(self._seen.items()):
            if heads.get(document_id, seen) <= seen:
                continue
            # The revisions read here may go past the heads read above: they say how far.
            later = self.store.changes(self.context.project_id, document_id, seen)
            if not later:
                continue
            latest = max(info.number for info, _ in later)
            parts = [
                _describe(info, stored)
                for info, stored in later
                if not (info.author.kind == "agent" and info.author.run_id == self.run_id)
            ]
            self._seen[document_id] = max(latest, self._seen.get(document_id, 0))
            if parts:
                name = self._names.get(document_id, document_id)
                found.append(
                    f"document {name} ({document_id}) changed after you read it: "
                    + "; ".join(parts)
                    + f". Read revision {latest} before you patch it."
                )
        return found

    def _saw(self, info: DocumentInfo, revision: int) -> None:
        self._names[info.document_id] = info.name
        self._seen[info.document_id] = max(revision, self._seen.get(info.document_id, 0))

    def _list(self, args: dict[str, Any]) -> tuple[str, Json]:
        documents = self.store.documents(self.context.project_id)
        return _dump({"documents": [_listing(info) for info in documents]}), {
            "documents": len(documents)
        }

    def _read(self, args: dict[str, Any]) -> tuple[str, Json]:
        reference = _text(args, "document")
        number = args.get("revision")
        if number is not None and (not isinstance(number, int) or number < 1):
            raise DocumentError(INVALID_REQUEST, "revision is a revision number, from 1")
        info = self.store.document(self.context.project_id, reference, self.context.session_id)
        revision = self.store.revision(self.context.project_id, info.document_id, number)
        mapped = self.store.mapped(self.context.project_id, info.document_id, revision.info.number)
        start = 0
        from_block = args.get("from_block")
        if from_block is not None:
            index = mapped.index(str(from_block))
            if index is None:
                raise DocumentError(INVALID_REQUEST, f"this revision has no block {from_block}")
            start = index
        head: Json = {
            "document": _listing(info),
            "revision": revision.info.number,
            "current_revision": info.head,
        }
        if revision.info.tombstone:
            head["deleted"] = True
        from .project_macros import view

        context = view(
            self.store,
            self.context.project_id,
            document=info.document_id,
            revision=revision.info.number,
            historical=number is not None,
        )
        head["macros"] = context["macros"]
        head["macro_context"] = {k: v for k, v in context.items() if k != "macros"}
        blocks = view_blocks(mapped, start)
        shown = len(blocks)
        while True:
            body: Json = {**head, "blocks": blocks[:shown]}
            if shown < len(blocks):
                body["more"] = {
                    "from_block": blocks[shown]["id"],
                    "blocks_left": len(blocks) - shown,
                }
            text = _dump(body)
            if len(text) <= self.output_chars or shown <= 1:
                break
            shown = max(1, shown * 3 // 4)
        self._saw(info, revision.info.number)
        read = {
            "document": info.document_id,
            "revision": revision.info.number,
            "blocks": [block["id"] for block in blocks[:shown]],
        }
        return text, {"read": read}

    def _create(
        self, args: dict[str, Any], operation: str, call_id: str | None, model: str | None
    ) -> tuple[str, Json]:
        scope = args.get("scope")
        if scope not in ("project", "session"):
            raise DocumentError(INVALID_REQUEST, "scope is 'project' or 'session'")
        result = self.store.create(
            self.context.project_id,
            name=_text(args, "name"),
            title=_text(args, "title"),
            text=_text(args, "text"),
            author=self.author(call_id, model),
            session_id=self.context.session_id if scope == "session" else None,
            reason=str(args.get("reason") or "created"),
            operation_id=operation,
        )
        info = self.store.document(self.context.project_id, result.document_id)
        self._saw(info, result.revision or info.head)
        return self._result(result, info)

    def _patch(
        self, args: dict[str, Any], operation: str, call_id: str | None, model: str | None
    ) -> tuple[str, Json]:
        reference = _text(args, "document")
        base = args.get("base_revision")
        if not isinstance(base, int) or isinstance(base, bool):
            raise DocumentError(INVALID_REQUEST, "base_revision is the revision you read")
        given = args.get("operation_id")
        info = self.store.document(self.context.project_id, reference, self.context.session_id)
        result = self.store.patch(
            self.context.project_id,
            info.document_id,
            base=base,
            operations=args.get("operations"),
            reason=str(args.get("reason") or ""),
            author=self.author(call_id, model),
            operation_id=str(given) if given is not None else operation,
        )
        # The run has seen its base. Revisions after it are the notices' to tell, its own
        # skipped: a rebased patch's result does not say what the revisions it was
        # rebased over changed (LD-15).
        self._saw(info, base)
        return self._result(result, info)

    def _delete(
        self, args: dict[str, Any], operation: str, call_id: str | None, model: str | None
    ) -> tuple[str, Json]:
        info = self.store.document(
            self.context.project_id, _text(args, "document"), self.context.session_id
        )
        result = self.store.delete(
            self.context.project_id,
            info.document_id,
            author=self.author(call_id, model),
            reason=_text(args, "reason"),
            operation_id=operation,
        )
        self._saw(info, info.head)
        return self._result(result, info)

    def _result(self, result: ChangeResult, info: DocumentInfo) -> tuple[str, Json]:
        record: Json = {"document": result.model_dump(mode="json", exclude_defaults=True)}
        if result.outcome == "conflict":
            text, _ = _failure(
                "conflict",
                result.message or "the patch conflicts",
                current_revision=result.revision,
                rejected_patch=result.patch_id,
                conflicts=cast(JsonValue, result.conflicts),
            )
            return text, record
        if result.outcome == "refused":
            text, _ = _failure(
                "patch_changes_unnamed_blocks",
                result.message or "the patch was refused",
                blocks=cast(JsonValue, result.unnamed),
            )
            return text, record
        body: Json = {"document": info.document_id, "name": info.name, "outcome": result.outcome}
        body.update(
            result.model_dump(
                mode="json",
                exclude_defaults=True,
                exclude={"outcome", "document_id", "conflicts", "unnamed"},
            )
        )
        return _dump(body), record


def _text(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise DocumentError(INVALID_REQUEST, f"{name} is required, as text")
    return value


def _describe(info: RevisionInfo, block_map: Json) -> str:
    author = info.author
    if author.kind == "agent":
        who = f"agent run {author.run_id} of session {author.session_id}"
    elif author.kind == "platform":
        who = f"the platform's {author.service}"
    elif author.kind == "worker":
        who = (
            f"worker code in environment {author.environment_id}, incarnation {author.incarnation}"
        )
        if author.session_id is not None:
            who += f", session {author.session_id}"
    elif author.kind == "import":
        who = "an import by the user"
    else:
        who = "the user"
    if info.tombstone:
        return f"revision {info.number} by {who} deleted it"
    touched: list[str] = []
    for entry in cast(list[Json], block_map.get("blocks", [])):
        if entry.get("status") in ("changed", "added", "moved"):
            touched.append(f"{entry['id']} {entry['status']}")
    for entry in cast(list[Json], block_map.get("removed", [])):
        touched.append(f"{entry['id']} removed")
    what = ", ".join(touched) if touched else "no blocks"
    return f"revision {info.number} by {who}: {what}"


def with_notices(text: str, notices: Sequence[str]) -> str:
    """A tool result with platform notices: in its JSON object, or after its text."""
    if not notices:
        return text
    try:
        parsed: object = json.loads(text)
    except (ValueError, RecursionError):
        parsed = None
    if isinstance(parsed, dict):
        body = cast(dict[str, Any], parsed)
        existing = body.get("platform_notices")
        listed = cast(list[object], existing) if isinstance(existing, list) else []
        body["platform_notices"] = [*listed, *notices]
        return _dump(body)
    return text + "\n\nplatform notices:\n" + "\n".join(f"- {notice}" for notice in notices)


REQUESTS_TASK: Final = "(The user's comments addressed to this session, as work requests.)"
"""The task a run started for work requests alone is recorded with, and that the
session's later runs see in its place in the earlier conversation (RS-10)."""


class WorkRequests(TurnHook):
    """User work requests addressed to the session: before each turn, and at a closing
    answer, which they continue. Requests are leased to the run while it lasts.
    ``discover``, when the run has skills, gives the skills notice for the requests'
    texts, which goes just before them (docs/specs/skills.md, SK-35; lab 42)."""

    name = "requests"

    def __init__(
        self,
        tool: DocumentTool,
        discover: Callable[[Sequence[str], Sequence[str]], Awaitable[Notice | None]] | None = None,
    ) -> None:
        self.tool, self.discover = tool, discover

    def lease(self) -> AbstractAsyncContextManager[None]:
        return self.tool.active_requests()

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        return () if point == "tool" else await self._pending()

    async def model_returned(self, run: RunView) -> None:
        # The lease keeps delivered requests claimed until the saved run acknowledges them.
        self.tool.request_delivered.update(run.delivered_ids(self.name))

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        if run.turns_left <= 0:
            return Closing()
        found = await self._pending()
        return Closing("continue", found) if found else Closing()

    async def acknowledge(self, record: RunRecord) -> None:
        ids = record.delivered.get(self.name)
        if ids:
            await asyncio.to_thread(self._acknowledge, record, ids)

    def unanswered(self) -> set[str]:
        """The requests the run was offered and no model call saw."""
        return self.tool.request_seen - self.tool.request_delivered

    async def _pending(self) -> tuple[Notice, ...]:
        found = await self.tool.request_notices()
        if not found:
            return ()
        ids = tuple(str(notice["request_id"]) for notice in found)
        self.tool.request_seen.update(ids)
        requests = Notice(self.name, ids, REQUESTS.text, found)
        if self.discover is not None:
            texts = [str(notice.get("request_text") or "") for notice in found]
            if (skills := await self.discover(texts, ids)) is not None:
                return (skills, requests)
        return (requests,)

    def _acknowledge(self, record: RunRecord, ids: list[str]) -> None:
        store = DocumentStore(self.tool.store.path, producer=self.tool.store.producer)
        try:
            Annotations(store).acknowledge(
                record.context.project_id, record.context.session_id, record.run_id, ids
            )
        finally:
            store.close()
