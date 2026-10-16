# SPDX-License-Identifier: MIT
"""Live-run scheduling over durable observations, with no reviewer execution authority."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncGenerator, Collection, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from sandbox_science.contracts import ExecutionContext, new_id
from sandbox_science.contracts.artifact_inventory import ArtifactRevisionRef
from sandbox_science.orchestrator.aio import lease, threaded
from sandbox_science.orchestrator.documents.errors import POLICY_DENIED, DocumentError
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.store import DocumentStore
from sandbox_science.orchestrator.history import body
from sandbox_science.orchestrator.hooks import (
    Closing,
    Effect,
    Notice,
    Point,
    RunView,
    TurnHook,
)
from sandbox_science.orchestrator.memory_prompt import MemoryGiven
from sandbox_science.orchestrator.model import ChatModel, Message, ToolSpec
from sandbox_science.orchestrator.notices import REVIEW_COUNTS, REVIEW_FINDINGS
from sandbox_science.orchestrator.projects import recorded

from .actions import action_of
from .artifacts import ArtifactConsumer
from .delegated import observe_delegated
from .findings import Findings
from .ledger import ingest_ledger
from .models import EvidenceAnchor, ReviewType
from .notices import FindingNotices
from .quotes import EPRINT_TOOL, PDF_TOOLS, eprint_record, mark_of, pdf_record
from .rubric import CLASSIFIER_VERSION
from .runner import ReviewerRunner, schema
from .store import MAX_ITEM_BYTES, Json, ReviewStore, clipped, dump

if TYPE_CHECKING:
    from sandbox_science.orchestrator.projects import RunRecord

AUTHOR_SPECS = (
    schema(
        "review_findings",
        "List the session's unresolved consistency findings.",
        {"after": {"type": "string"}},
        [],
    ),
    schema(
        "review_finding_read",
        "Read a finding's pinned evidence and bounded transition history.",
        {
            "finding": {"type": "string"},
            "before": {"type": "integer"},
            "start": {"type": "integer"},
            "count": {"type": "integer"},
        },
        ["finding"],
    ),
    schema(
        "review_evidence_read",
        "Read an exact evidence slice linked by a session finding.",
        {
            "review": {"type": "string"},
            "evidence": {"type": "string"},
            "start": {"type": "integer"},
            "count": {"type": "integer"},
            "block": {"type": "string"},
        },
        ["review", "evidence"],
    ),
    schema(
        "review_respond",
        (
            "Main agent only: claim a fix with explanation and changed "
            "revision references, or rebut with evidence. Authors cannot "
            "confirm their own fixes."
        ),
        {
            "finding": {"type": "string"},
            "expected": {"type": "integer"},
            "action": {"enum": ["fix", "rebut"]},
            "explanation": {"type": "string"},
            "evidence": {
                "type": "array",
                "maxItems": 8,
                "description": "Pinned review evidence, by its evidence ID (review_finding_read "
                "lists the finding's), optionally narrowed to a block, formula or lines",
                "items": EvidenceAnchor.model_json_schema(),
            },
            "revisions": {
                "type": "array",
                "maxItems": 16,
                "description": "Changed document revisions: [document ID, revision]",
                "items": {
                    "type": "array",
                    "items": {"type": ["string", "integer"]},
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
            "artifact_revisions": {
                "type": "array",
                "maxItems": 16,
                "items": ArtifactRevisionRef.model_json_schema(),
            },
            "entry_revisions": {
                "type": "array",
                "maxItems": 16,
                "description": "Changed ledger entry revisions: [entry handle or ID, revision]",
                "items": {"type": "array"},
            },
            "operation_id": {"type": "string"},
        },
        ["finding", "expected", "action", "explanation"],
    ),
    schema("request_review", "Request a review at the next safe point of this live run.", {}, []),
    schema(
        "review_response_read",
        "Read an exact ordinary MAIN explanation source and its body CAS.",
        {
            "finding": {"type": "string"},
            "response_revision": {"type": "integer"},
            "body_revision": {"type": "integer"},
            "start": {"type": "integer"},
            "count": {"type": "integer"},
        },
        ["finding", "response_revision"],
    ),
    schema(
        "review_response_edit",
        "MAIN only: correct its session's explanation source. Original decision/action/evidence "
        "remain immutable; mechanical acceptance does not resolve the scientific parent.",
        {
            "finding": {"type": "string"},
            "response_revision": {"type": "integer"},
            "expected": {"type": "integer"},
            "base": {"type": "integer"},
            "sha256": {"type": "string"},
            "body": {"type": "string"},
            "operation_id": {"type": "string"},
        },
        ["finding", "response_revision", "expected", "base", "sha256", "body"],
    ),
)
REVIEW_EFFECTS: Final[dict[str, Effect]] = {
    "review_findings": "read",
    "review_finding_read": "read",
    "review_evidence_read": "read",
    "review_response_read": "read",
}
"""The author tools that only read; responding and requesting a review are effects."""
_COMPLETE = re.compile(
    r"\b(?:done|completed|finished|implemented|task is complete|work is complete)\b", re.I
)


def classify(final_text: str | None, *, effects: bool, unresolved: bool) -> Json:
    """A recorded conservative rule classifier, independently authored and versioned."""
    completion = bool(_COMPLETE.search(final_text or ""))
    return {
        "version": CLASSIFIER_VERSION,
        "label": "terminal" if effects or unresolved or completion else "conversation",
        "effects": effects,
        "unresolved": unresolved,
        "completion_claim": completion,
    }


def execution_projection(record: Json) -> tuple[Json, bool]:
    """Copy bounded worker observations; mutable paths remain references, never reads."""
    result = cast(Json, record.get("result") or {})
    payload: Json = {
        k: record.get(k)
        for k in (
            "execution_id",
            "attempt_id",
            "context",
            "kind",
            "state",
            "submitted_at",
            "started_at",
            "ended_at",
            "location",
        )
    }
    request = cast(Json, record.get("request") or {})
    payload["request"] = {
        k: clipped(str(v), 4096)
        for k, v in request.items()
        if k in ("script", "code", "argv", "cwd", "language", "path")
    }
    complete = True
    for stream in ("stdout", "stderr"):
        output = cast(Json, result.get(stream) or {})
        original = str(output.get("text", ""))
        payload[stream] = clipped(original, 8192)
        payload[stream + "_truncated"] = bool(output.get("truncated")) or len(original) > 8192
        payload[stream + "_line_numbers_available"] = not payload[stream + "_truncated"]
        complete = complete and not payload[stream + "_truncated"]
    payload.update(
        exit_code=result.get("exit_code"),
        error=result.get("error"),
        elapsed_seconds=result.get("elapsed_seconds"),
        written_files=[clipped(str(p), 256) for p in result.get("written_files", [])[:100]],
        written_files_truncated=bool(result.get("written_files_truncated"))
        or len(result.get("written_files", [])) > 100,
        outputs_note="Rich output and shell artifact inventory are not copied by this slice.",
    )
    if payload["error"]:
        payload["error"] = {
            k: clipped(str(v), 1024)
            for k, v in payload["error"].items()
            if k in ("kind", "message")
        }
    payload["omitted_rich_outputs"] = len(result.get("outputs", []))
    payload["canonical_execution"] = record.get("execution_id")
    complete = complete and not (
        payload["omitted_rich_outputs"]
        or result.get("written_files")
        or payload["written_files_truncated"]
    )
    return payload, complete


_log = logging.getLogger(__name__)


def source_of(
    messages: Sequence[Message], call: Json | None, servers: Collection[str] = ()
) -> tuple[str, Json, Json] | None:
    """The source record of the tool call ``messages`` ends with, when it read a source a
    quote can cite (CR-4): ``pdf_pages`` or ``pdf_search``, from the helper's structured
    result, or the e-print server's full text, through one of the run's configured
    connectors (``servers``) without an error; with its mark and locator.

    ``call`` is the call as the run executed it, its record included (``tool_records``):
    its name and arguments, not a transcript lookup, decide what it read. The transcript
    must agree: the assistant message before the result names the call's ID once, with
    that name and those arguments, so that duplicate IDs attribute nothing (the
    verification's N1)."""
    if call is None or not messages or messages[-1].get("role") != "tool":
        return None
    call_id = str(messages[-1].get("tool_call_id", ""))
    name, raw = str(call.get("tool", "")), call.get("arguments")
    asked = next((m for m in reversed(messages[:-1]) if m.get("role") == "assistant"), None)
    made = [
        cast(Json, cast(Json, c).get("function") or {})
        for c in cast(list[Any], (asked or {}).get("tool_calls") or [])
        if cast(Json, c).get("id") == call_id
    ]
    if len(made) != 1 or made[0].get("name") != name or made[0].get("arguments") != raw:
        return None
    try:
        arguments: object = json.loads(str(raw or "{}"))
    except ValueError:
        return None
    if not isinstance(arguments, dict):
        return None
    given = cast(Json, arguments)
    record: Json | None = None
    if name in PDF_TOOLS and isinstance(call.get("pdf"), dict):
        record = pdf_record(name, given, cast(Json, call["pdf"]))
    elif name == EPRINT_TOOL and call.get("connector") in servers and "error" not in call:
        # The result's text after its archive header (CM-4a), as the server wrote it.
        record = eprint_record(name, given, body(str(messages[-1].get("content", ""))))
    if record is None:
        return None
    # Bounded as an item is: lines past the bound are left out, and say so.
    while len(dump(record).encode()) > MAX_ITEM_BYTES // 2 and record.get("lines"):
        record["lines"] = record["lines"][: len(record["lines"]) // 2]
        record["lines_truncated"] = True
    return mark_of(record), {"call": call_id[:128], "tool": name[:128]}, record


class ReviewController:
    """Detached checkpoints and bounded terminal/confirmation reviews in one live run.

    A durable lease handles concurrent processes. A deployment must keep an event loop
    alive for detached work; this controller never invents an idle-session scheduler.
    """

    def __init__(
        self,
        store: ReviewStore,
        context: ExecutionContext,
        *,
        run_id: str,
        model: ChatModel | None,
        alias: str | None,
        role: str = "main",
        messages: asyncio.Queue[str] | None = None,
        author_alias: str | None = None,
        persistent: bool = False,
        memory: MemoryGiven | None = None,
        connectors: Collection[str] = (),
        earlier: int = 0,
    ) -> None:
        """``memory`` is the project memory the reviewer is given: the one the reviewed
        run was given when it started (PM-8). ``connectors`` are the MCP servers the run's
        project configures, whose e-print results can be quotes' sources (CR-4).
        ``earlier`` is how many messages after the system message are the session's
        earlier conversation (RS-10). They are not observed: each was observed in its own
        run's window, and an earlier answer is not this run's narration, so the window's
        actions, which list only its own calls, would hold no record of what it says was
        done (lab 41)."""
        store.scope(context.project_id, context.session_id)
        project = recorded(store.documents.path.parent, context.project_id)
        if context.environment_id != project.sessions[context.session_id].environment_id:
            raise DocumentError(POLICY_DENIED, "review context does not match the recorded session")
        if model is not None and (not alias or len(alias) > 128):
            raise ValueError("a reviewer requires its own bounded model alias")
        if messages is not None and messages.maxsize:
            raise ValueError("review interruption requires an unbounded input queue")
        self.store, self.context, self.run_id = store, context, run_id
        self.model, self.alias, self.role, self.messages = model, alias, role, messages
        self.persistent = persistent
        self.author_alias = author_alias
        self.memory = memory
        self.connectors = frozenset(connectors)
        self.last_counts: Json | None = None
        self.documents_complete = True
        self.artifacts_complete = True
        self.ledger_complete = True
        self.artifact_consumer = (
            ArtifactConsumer(store, *self.scope) if store.artifacts is not None else None
        )
        self.owner, self.instance = new_id("review-owner"), new_id("turn")
        self.task: asyncio.Task[None] | None = None
        self.review: str | None = None
        if earlier < 0:
            raise ValueError("earlier counts messages")
        self.position = 1 + earlier if earlier else 0
        """The next message to observe; the system message and the earlier conversation
        before it are never observed."""
        self.effects = False
        self.seen: set[str] = set()
        self.delivered: set[str] = set()
        self.notice_store = FindingNotices(store)
        self.findings = Findings(store)
        self.review_ids: list[str] = []

    @property
    def scope(self) -> tuple[str, str]:
        return self.context.project_id, self.context.session_id

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return AUTHOR_SPECS

    async def _source(self, messages: Sequence[Message], call: Json | None) -> None:
        """Record what a tool call read that a quote can cite, as a ``source``
        observation that makes no review due (CR-4), under a key that begins with its
        mark, which the feed's key index finds (``quotes.source``)."""
        found = source_of(messages, call, self.connectors)
        if found is None:
            return
        mark, locator, record = found
        index = len(messages) - 1
        await asyncio.to_thread(
            self.store.observe,
            *self.scope,
            f"source:{mark}:{self.instance}:{index}",
            "source",
            {"run": self.run_id, "invocation": self.instance, "message": index, **locator},
            record,
            complete=not record.get("lines_truncated"),
        )

    async def _action(self, messages: Sequence[Message], call: Json | None) -> None:
        """Record a tool call the run made that changes something, as an ``action``
        observation that makes no review due: the window's ``actions`` item lists it."""
        found = action_of(messages, call)
        if found is None:
            return
        index = len(messages) - 1
        await asyncio.to_thread(
            self.store.observe,
            *self.scope,
            f"{self.instance}:action:{index}",
            "action",
            {"run": self.run_id, "invocation": self.instance, "message": index},
            found,
        )

    async def _execution(self, execution: Json) -> None:
        """Record an execution in the state a tool call saw it: one item per state."""
        payload, complete = execution_projection(execution)
        effects = execution.get("started_at") is not None
        self.effects = self.effects or effects
        await asyncio.to_thread(
            self.store.observe,
            *self.scope,
            f"execution:{execution['execution_id']}:{execution.get('state')}",
            "execution",
            {
                "execution": execution["execution_id"],
                "attempt": execution.get("attempt_id"),
                "run": self.run_id,
            },
            payload,
            complete=complete,
            effects=effects,
            cells=int(execution.get("kind") == "cell"),
        )

    async def observe(
        self, messages: Sequence[Message], *, tool: Json | None = None, call: Json | None = None
    ) -> None:
        for index in range(self.position, len(messages)):
            message = messages[index]
            if message.get("role") == "system":
                continue
            original = dump(message)
            payload: Json = {
                "role": message.get("role"),
                "content": clipped(original, 32000),
                "truncated": len(original) > 32000,
            }
            await asyncio.to_thread(
                self.store.observe,
                *self.scope,
                f"{self.instance}:message:{index}",
                "transcript",
                {"run": self.run_id, "invocation": self.instance, "message": index},
                payload,
                complete=len(original) <= 32000,
            )
        self.position = len(messages)
        if tool is not None:
            await self._source(messages, call)
            await self._action(messages, call)
            raw_execution = tool.get("execution")
            # A wait's ended jobs are executions in the state it saw them end (lab 41's B7).
            raw_ended = tool.get("executions")
            ended = cast(list[Any], raw_ended) if isinstance(raw_ended, list) else []
            for raw in [raw_execution, *ended]:
                if isinstance(raw, dict):
                    await self._execution(cast(Json, raw))
            raw_artifact = tool.get("artifact")
            if isinstance(raw_artifact, dict) and self.artifact_consumer is None:
                artifact = cast(Json, raw_artifact)
                encoded = dump(artifact)
                self.effects = True
                await asyncio.to_thread(
                    self.store.observe,
                    *self.scope,
                    f"{self.instance}:artifact:{self.position}",
                    "artifact",
                    {"artifact": clipped(encoded, 1000), "run": self.run_id},
                    {
                        "reference": clipped(encoded, 16000),
                        "contents": "Unavailable: this slice pins publication metadata only.",
                    },
                    complete=False,
                    artifacts=1,
                    effects=True,
                )
        # What the run's delegated runs did, once each delegation ends (lab 41's B5).
        await asyncio.to_thread(observe_delegated, self.store, *self.scope, self.run_id)
        ingestion = await asyncio.to_thread(self.store.ingest_documents, *self.scope)
        self.documents_complete = ingestion["complete"]
        self.ledger_complete = await self._ledger()
        if self.artifact_consumer is not None:
            artifact_ingestion = await self.artifact_consumer.ingest()
            self.artifacts_complete = artifact_ingestion["complete"]

    async def _ledger(self, *, drain: bool = False) -> bool:
        """Read the ledger's events into the review feed; whether it read them all. A page
        the feed refuses leaves the window's coverage incomplete and the run going, rather
        than failing every safe point after it (the review of CR-4)."""
        try:
            read = await asyncio.to_thread(ingest_ledger, self.store, *self.scope, drain=drain)
        except DocumentError as error:
            _log.warning("the ledger's events were not read for review: %s", error.message)
            return False
        return bool(read["complete"])

    async def _dispatch(self, kind: ReviewType, classifier: Json | None = None) -> str | None:
        if self.role != "main":
            return None
        await asyncio.to_thread(self.findings.supersede, *self.scope)
        review = await asyncio.to_thread(
            self.store.dispatch,
            *self.scope,
            kind,
            author_run=self.run_id,
            owner=self.owner,
            alias=self.alias,
            classifier=classifier,
            coverage_complete=self.documents_complete
            and self.artifacts_complete
            and self.ledger_complete,
        )
        if review is not None:
            self.review = review
            self.review_ids.append(review)
            self.task = asyncio.create_task(
                ReviewerRunner(
                    self.store, *self.scope, review, self.owner, self.model, memory=self.memory
                ).run()
            )
        return review

    async def safe_point(
        self, messages: Sequence[Message], *, tool: Json | None = None, call: Json | None = None
    ) -> None:
        """``tool`` is the record of the tool call ``messages`` ends with, and ``call`` that
        call as the run executed it (its ``tool_records`` entry)."""
        await self.observe(messages, tool=tool, call=call)
        if self.task is not None and self.task.done():
            await self.task
            self.task = None
        if self.task is None and await asyncio.to_thread(self.store.due, *self.scope):
            targets = await asyncio.to_thread(self.store.confirmation_targets, *self.scope)
            await self._dispatch("confirmation" if targets else "checkpoint")

    async def notices(self) -> list[Json]:
        if self.role != "main":
            return []
        return await asyncio.to_thread(
            self.notice_store.pending, *self.scope, self.run_id, self.seen
        )

    async def _wait(self, task: asyncio.Task[None]) -> str | None:
        """A user message wins even if completion races; no model polling occurs."""
        if self.messages is None:
            await asyncio.shield(task)
            return None
        incoming = asyncio.create_task(self.messages.get())
        consumed = False
        try:
            await asyncio.wait((task, incoming), return_when=asyncio.FIRST_COMPLETED)
            if incoming.done() and not incoming.cancelled():
                consumed = True
                return incoming.result()
            await asyncio.shield(task)
            return None
        finally:
            if not incoming.done():
                incoming.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await incoming
            if not incoming.cancelled() and not consumed:
                self.messages.put_nowait(incoming.result())

    async def terminal(self, messages: Sequence[Message], final_text: str | None) -> Json:
        await self.observe(messages)
        ingestion = await asyncio.to_thread(self.store.ingest_documents, *self.scope, drain=True)
        self.documents_complete = ingestion["complete"]
        self.ledger_complete = await self._ledger(drain=True)
        artifact_ingestion = (
            await self.artifact_consumer.ingest(drain=True)
            if self.artifact_consumer is not None
            else {"complete": True, "enabled": False}
        )
        self.artifacts_complete = artifact_ingestion["complete"]
        states = await asyncio.to_thread(self.store.finding_states, *self.scope)
        effects = await asyncio.to_thread(self.store.run_effects, *self.scope, self.run_id)
        counts = await asyncio.to_thread(self.notice_store.counts, *self.scope)
        label = classify(
            final_text,
            effects=effects or self.effects,
            unresolved=bool(states.get("open") or states.get("claimed fixed")),
        )
        label["document_ingestion"] = ingestion
        label["artifact_ingestion"] = artifact_ingestion
        config, _ = self.store.config(self.context.project_id)
        summary: Json = {"classifier": label, "reviews": self.review_ids, "counts": counts}
        if label["label"] == "conversation" or not config.enabled or self.role != "main":
            return summary
        if self.task is not None:
            message = await self._wait(self.task)
            if message is not None:
                return {**summary, "pending": True, "user_message": message}
            self.task = None
        # A confirmation consumes finding groups before new feed items, so a full
        # feed cannot starve claimed fixes. At most three windows run at this hold;
        # any remaining work is explicitly pending, never labelled as passed.
        for _ in range(3):
            targets = await asyncio.to_thread(self.store.confirmation_targets, *self.scope)
            kind: Literal["confirmation", "terminal"] = "confirmation" if targets else "terminal"
            review = await self._dispatch(kind, label)
            if review is None:
                # Another live process owns this session's review, or the project's
                # reviewer has no calls left this hour: the work waits as one pending
                # review. This one-shot driver cannot take over or wait on an unowned
                # in-memory task.
                summary["pending"] = True
                break
            if not config.terminal_hold:
                summary["pending"] = True
                break
            assert self.task is not None  # noqa: S101
            message = await self._wait(self.task)
            if message is not None:
                return {**summary, "pending": True, "user_message": message}
            self.task = None
            ended = await asyncio.to_thread(self.store.run, *self.scope, review)
            if ended["state"] in ("failed", "dropped"):
                continue
            if (
                ended["state"] == "capped"
                and (await asyncio.to_thread(self.store.progress, *self.scope))["retry_end"]
                is not None
            ):
                # A limit ended it: its window waits for a later review, not this hold,
                # which the same limit would end again.
                summary["outcome"] = ended["state"]
                summary["pending"] = True
                break
            if ended["kind"] == "confirmation":
                # The confirmation left new feed pending; a terminal window still
                # checks the final author answer after checking the proposed fix.
                continue
            summary["outcome"] = ended["state"]
            progress = await asyncio.to_thread(self.store.progress, *self.scope)
            if progress["remaining"]:
                continue
            break
        else:
            progress = await asyncio.to_thread(self.store.progress, *self.scope)
            if progress["retry_end"] is None and not progress["remaining"]:
                summary["outcome"] = "unreviewed"
            else:
                summary["pending"] = True
        summary["counts"] = await asyncio.to_thread(self.notice_store.counts, *self.scope)
        return summary

    async def author_call(
        self, name: str, args: Json, operation: str, *, model: str | None = None
    ) -> tuple[str, Json]:
        if name == "review_findings":
            if set(args) - {"after"}:
                raise DocumentError("invalid_request", "unknown finding-list field")
            result = await asyncio.to_thread(
                self.findings.listing, *self.scope, after=args.get("after"), unresolved=True
            )
        elif name == "review_finding_read":
            if set(args) - {"finding", "before", "start", "count"} or "finding" not in args:
                raise DocumentError("invalid_request", "finding is required")
            result = await asyncio.to_thread(
                self.findings.get, *self.scope, args["finding"], before=args.get("before")
            )
            start, count = args.get("start", 0), args.get("count", 6000)
            if (
                type(start) is not int
                or not 0 <= start <= 10000000
                or type(count) is not int
                or not 1 <= count <= 8000
            ):
                raise DocumentError("invalid_request", "invalid finding slice")
            source = dump(result)
            result = {
                "finding": args["finding"],
                "text": source[start : start + count],
                "next_start": start + count if start + count < len(source) else None,
                "history_before": result["history_before"],
            }

        elif name == "review_evidence_read":
            if set(args) - {"review", "evidence", "start", "count", "block"} or not {
                "review",
                "evidence",
            } <= set(args):
                raise DocumentError("invalid_request", "review and evidence are required")
            count = args.get("count", 6000)
            if type(count) is not int or not 1 <= count <= 8000:
                raise DocumentError("invalid_request", "invalid evidence count")
            result = await asyncio.to_thread(
                self.store.read_evidence,
                *self.scope,
                args["review"],
                args["evidence"],
                start=args.get("start", 0),
                count=count,
                block=args.get("block"),
            )
        elif name in ("review_response_read", "review_response_edit"):
            from .response_bodies import ResponseBodies

            bodies = ResponseBodies(self.store)
            if name == "review_response_read":
                if set(args) - {
                    "finding",
                    "response_revision",
                    "body_revision",
                    "start",
                    "count",
                } or not {"finding", "response_revision"} <= set(args):
                    raise DocumentError(
                        "invalid_request", "finding and original response revision are required"
                    )
                start, count = args.get("start", 0), args.get("count", 6000)
                if (
                    type(start) is not int
                    or not 0 <= start <= 32768
                    or type(count) is not int
                    or not 1 <= count <= 8000
                ):
                    raise DocumentError("invalid_request", "invalid response source slice")
                source = await threaded(
                    bodies.get,
                    *self.scope,
                    args["finding"],
                    args["response_revision"],
                    body_revision=args.get("body_revision"),
                )
                result = {
                    **source,
                    "body": source["body"][start : start + count],
                    "start": start,
                    "next_start": start + count if start + count < len(source["body"]) else None,
                }
            else:
                author = Author(
                    kind="agent",
                    session_id=self.context.session_id,
                    run_id=self.run_id,
                    role=self.role,
                    model=model,
                    alias=self.author_alias,
                )
                source = await threaded(
                    bodies.edit,
                    *self.scope,
                    {**args, "operation_id": args.get("operation_id", operation)},
                    author,
                )
                result = {k: v for k, v in source.items() if k != "body"}
        elif name == "review_respond":
            author = Author(
                kind="agent",
                session_id=self.context.session_id,
                run_id=self.run_id,
                role=self.role,
                model=model,
                alias=self.author_alias,
            )
            result = await asyncio.to_thread(
                self.findings.respond,
                *self.scope,
                {**args, "operation_id": args.get("operation_id", operation)},
                author,
            )
        elif name == "request_review" and not args:
            await asyncio.to_thread(self.store.request, *self.scope)
            result = {"queued": True}
        else:
            raise DocumentError("invalid_request", "invalid review tool arguments")
        encoded = dump(result)
        if len(encoded) > 20000:
            return dump(
                {
                    "error": "response_bound",
                    "message": (
                        "Read this finding in the workspace; the original evidence remains stored."
                    ),
                }
            ), {"review_tool": name}
        return encoded, {"review_tool": name, "result": result}

    @asynccontextmanager
    async def active(self) -> AsyncGenerator[None]:
        await asyncio.to_thread(self.store.recover, *self.scope)
        renew = partial(self.notice_store.renew, *self.scope, self.run_id)
        successful = False
        try:
            async with lease(renew, interval=30, name="review notice"):
                yield
            successful = True
        finally:
            if (
                (not successful or not self.persistent)
                and self.task is not None
                and not self.task.done()
            ):
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
            await asyncio.to_thread(
                self.notice_store.release,
                *self.scope,
                self.run_id,
                self.delivered if successful else None,
            )

    async def close(self) -> None:
        """The persistent host owns shutdown; unfinished reviews retain retry state."""
        try:
            if self.task is not None and not self.task.done():
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
        finally:
            if self.artifact_consumer is not None:
                await self.artifact_consumer.aclose()


class Reviews(TurnHook):
    """Consistency reviews in a run: findings and changed counts before each turn, each
    tool result for the reviewer, a terminal review of a closing answer with its
    response rounds, and at the end the bound on delivered findings never fixed."""

    name = "reviews"

    def __init__(self, controller: ReviewController) -> None:
        self.controller = controller
        self.result: Json | None = None

    def lease(self) -> AbstractAsyncContextManager[None]:
        return self.controller.active()

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point != "turn":
            return ()
        await self.controller.safe_point(run.messages)
        return await self._pending()

    async def model_returned(self, run: RunView) -> None:
        # The lease keeps delivered findings claimed until the saved run acknowledges them.
        self.controller.delivered.update(run.delivered_ids(self.name))

    async def observe_tool(self, run: RunView, record: Json) -> None:
        executed = run.tool_records
        call = dict(executed[-1]) if executed else None
        await self.controller.safe_point(run.messages, tool=record, call=call)

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        controller = self.controller
        self.result = await controller.terminal(run.messages, answer)
        if "user_message" in self.result:
            return Closing("stop", stopped="user_message")
        configuration, _ = await asyncio.to_thread(
            controller.store.config, controller.context.project_id
        )
        if not run.can_respond or run.response_rounds >= configuration.response_rounds:
            return Closing()
        await controller.safe_point(run.messages)
        found = await self._pending()
        return Closing("respond" if any(n.ids for n in found) else "accept", found)

    async def accept(self, run: RunView) -> None:
        controller, counts = self.controller, (self.result or {}).get("counts", {})
        if controller.delivered and any(str(key).endswith(":open") for key in counts):
            await asyncio.to_thread(
                controller.findings.bound,
                *controller.scope,
                "The bounded author response rounds ended; no fix was confirmed.",
                notices=controller.delivered,
            )
            if self.result is not None:
                self.result["counts"] = await asyncio.to_thread(
                    controller.notice_store.counts, *controller.scope
                )

    def summary(self) -> object | None:
        return self.result

    async def acknowledge(self, record: RunRecord) -> None:
        if ids := record.delivered.get(self.name):
            await asyncio.to_thread(self._acknowledge, record, ids)

    async def _pending(self) -> tuple[Notice, ...]:
        controller = self.controller
        found = await controller.notices()
        counts = await asyncio.to_thread(controller.notice_store.counts, *controller.scope)
        changed = bool(counts) and counts != controller.last_counts
        controller.last_counts = counts
        notices: list[Notice] = []
        if found:
            ids = tuple(str(notice["notice_id"]) for notice in found)
            controller.seen.update(ids)
            notices.append(Notice(self.name, ids, REVIEW_FINDINGS.text, found))
        if changed:
            notices.append(Notice(self.name, (), REVIEW_COUNTS.text, counts))
        return tuple(notices)

    def _acknowledge(self, record: RunRecord, ids: list[str]) -> None:
        documents = DocumentStore(
            self.controller.store.documents.path, producer=self.controller.store.documents.producer
        )
        try:
            FindingNotices(ReviewStore(documents)).acknowledge(
                record.context.project_id, record.context.session_id, record.run_id, ids
            )
        finally:
            documents.close()
