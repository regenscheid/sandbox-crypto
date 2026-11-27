# SPDX-License-Identifier: MIT
"""Live-run scheduling over durable observations, with no reviewer execution authority."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncGenerator, Collection, MutableSequence, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, Any, Final, cast

from sandbox_science.contracts import ExecutionContext, new_id
from sandbox_science.contracts.artifact_inventory import ArtifactRevisionRef
from sandbox_science.orchestrator import jsontext
from sandbox_science.orchestrator.aio import lease, threaded
from sandbox_science.orchestrator.context.history import Archive, body
from sandbox_science.orchestrator.context.history.collections import Collection as RunCollection
from sandbox_science.orchestrator.context.history.descriptors import unpaged
from sandbox_science.orchestrator.context.history.snapshot import inline
from sandbox_science.orchestrator.documents.errors import POLICY_DENIED, DocumentError
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.documents.store import DocumentStore
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
from sandbox_science.orchestrator.notices import REVIEW_COUNTS, REVIEW_FINDINGS, unframed
from sandbox_science.orchestrator.projects import recorded
from sandbox_science.orchestrator.purpose import without
from sandbox_science.orchestrator.tool_entry import publications
from sandbox_science.orchestrator.view_image import ImageViewer

from .actions import action_of
from .archived import held_calls, places
from .artifacts import ArtifactConsumer
from .delegated import observe_delegated
from .executions import execution_projection
from .findings import Findings
from .ledger import ingest_ledger
from .models import (
    DISMISSAL_CHARACTERS,
    END_WAIT_SECONDS,
    FINAL_HOLD_SECONDS,
    FINAL_RECHECK_SECONDS,
    FINAL_REVIEW_SECONDS,
    FINDING_STATUSES,
    EvidenceAnchor,
    ReviewConfig,
    ReviewType,
)
from .notices import FindingNotices
from .quotes import (
    ALIAS,
    EPRINT_TOOLS,
    LIBRARY_TOOLS,
    PDF_TOOLS,
    eprint_record,
    library_record,
    mark_of,
    pdf_record,
    source_keys,
)
from .rubric import CLASSIFIER_VERSION
from .runner import ReviewerRunner, schema
from .store import MAX_ITEM_BYTES, Json, ReviewStore, clipped, dump

if TYPE_CHECKING:
    from sandbox_science.orchestrator.projects import RunRecord

AUTHOR_SPECS = (
    schema(
        "review_findings",
        "List the session's consistency findings by status: open by default, or fixed, "
        "dismissed, superseded or stale.",
        {"after": {"type": "string"}, "status": {"enum": list(FINDING_STATUSES)}},
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
            "Main agent only, on an open finding: fix names the revisions, or for a "
            "finding on messages the later message, that address it and queues its "
            "re-check, which alone makes it fixed; dismiss sets it aside with a reason of "
            f"at most {DISMISSAL_CHARACTERS:,} characters, which the user and the next "
            "review see."
        ),
        {
            "finding": {"type": "string"},
            "expected": {"type": "integer"},
            "action": {"enum": ["fix", "dismiss"]},
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
            "messages": {
                "type": "array",
                "maxItems": 8,
                "description": "For a finding on messages: the later archive messages that "
                "correct it, by their IDs (msg-...)",
                "items": {"type": "string"},
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
REVIEWS_OFF: Final = (
    "No reviewer model is configured, so consistency reviews are off: this session's work "
    "is not reviewed. Give the run service a reviewer alias (--reviewer-model) to turn "
    "them on."
)
"""Why a session's reviews are off, as its review list says once."""
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


_log = logging.getLogger(__name__)


def source_of(
    messages: Sequence[Message], call: Json | None, servers: Collection[str] = ()
) -> tuple[str, Json, Json] | None:
    """The source record of the tool call ``messages`` ends with, when it read a source a
    quote can cite (CR-4): ``pdf_pages`` or ``pdf_search``, from the helper's structured
    result, or the e-print server's or the library server's text of a paper, through one
    of the run's configured connectors (``servers``) without an error; with its mark and
    locator.

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
    written = made[0].get("arguments") if len(made) == 1 else None
    # The record keeps the call's arguments without its purpose (Purpose 3).
    if (
        len(made) != 1
        or made[0].get("name") != name
        or (without(written) if isinstance(written, str) else written) != raw
    ):
        return None
    try:
        arguments: object = jsontext.loads(str(raw or "{}"))
    except ValueError:
        return None
    if not isinstance(arguments, dict):
        return None
    given = cast(Json, arguments)
    record: Json | None = None
    if name in PDF_TOOLS and isinstance(call.get("pdf"), dict):
        record = pdf_record(name, given, cast(Json, call["pdf"]))
    elif (
        name in EPRINT_TOOLS + LIBRARY_TOOLS
        and call.get("connector") in servers
        and "error" not in call
    ):
        # The result's text after its archive header (CM-4a) and its framing line, as the
        # server wrote it, without the toolbox's line naming pages it left out (CM-12a).
        text = unframed(body(str(messages[-1].get("content", ""))))
        paged = call.get("paged")
        left_out: list[int] = []
        if isinstance(paged, dict):
            text = unpaged(text)
            left_out = [n for n in cast(list[Any], cast(Json, paged).get("pages") or [])
                        if isinstance(n, int)]  # fmt: skip
        if name in LIBRARY_TOOLS:
            record = library_record(name, given, text, left_out=left_out)
        else:
            record = eprint_record(name, given, text)
    if record is None:
        return None
    # Bounded as an item is: lines, pages or passages past the bound are left out, and say
    # so.
    while len(dump(record).encode()) > MAX_ITEM_BYTES // 2 and record.get("lines"):
        record["lines"] = record["lines"][: len(record["lines"]) // 2]
        record["lines_truncated"] = True
    texts = cast(Json, record.get("library") or record.get("eprint") or {})
    while len(dump(record).encode()) > MAX_ITEM_BYTES // 2 and texts.get("passages"):
        kept = len(texts["passages"]) // 2
        texts["passages"] = texts["passages"][:kept]
        if "pages" in texts:
            texts["pages"] = texts["pages"][:kept]
        record["passages_truncated"] = True
    return mark_of(record), {"call": call_id[:128], "tool": name[:128]}, record


def record_source(
    store: ReviewStore, project: str, session: str, suffix: str, locator: Json, record: Json
) -> None:
    """Record source record ``record`` in one transaction, under each of its keys
    (``quotes.source_keys``): the record under the first, and under each further one a
    row that names the first (``quotes.ALIAS``)."""
    first, *more = source_keys(record, suffix)
    complete = not record.get("lines_truncated") and not record.get("passages_truncated")
    store.scope(project, session)
    with store.documents.control.write() as tx:
        store.record_observation(
            tx.sql, project, session, first, "source", locator, record, complete=complete
        )
        for key in more:
            pointer: Json = {"tool": record.get("tool"), ALIAS: first}
            store.record_observation(tx.sql, project, session, key, "source", locator, pointer)


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
        archived_as: str | None = None,
        images: ImageViewer | None = None,
        hold_seconds: float = FINAL_HOLD_SECONDS,
        end_seconds: float = END_WAIT_SECONDS,
        recheck_seconds: float = FINAL_RECHECK_SECONDS,
    ) -> None:
        """``memory`` is the project memory the reviewer is given: the one the reviewed
        run was given when it started (PM-8). ``connectors`` are the MCP servers the run's
        project configures, whose e-print results can be quotes' sources (CR-4).
        ``earlier`` is how many messages after the system message are the run's view of
        its session (CM-29). They are not observed: each was observed in its own run's
        window, and an earlier answer is not this run's narration (lab 41); the calls
        whose results they hold are listed, as earlier runs' actions, with each window's
        (CR-2 as amended). ``archived_as`` is the run's name in the session's archive,
        when its messages are archived: each transcript item names the archived message
        it copies (CR-14 as amended). ``hold_seconds`` is how long ``final_review = "hold"``
        keeps a final answer for its review, from its start, and ``end_seconds`` how long
        the end of a run waits for its reviews in all (CR-48)."""
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
        self.off = model is None and alias is None
        """Whether no reviewer is configured: then reviews are off (CR-35 as amended). An
        alias whose model is unavailable is a reviewer that fails, as CR-F10 says."""
        self.persistent = persistent
        self.hold_seconds, self.end_seconds = hold_seconds, end_seconds
        self.recheck_seconds = recheck_seconds
        self.rechecked: str | None = None
        """The re-check after the final review's answering turn, once dispatched."""
        self.holding = False
        """Whether the run's final answer is kept from the user until its final review
        ends (``final_review = "hold"``), as read when the run's reviews begin."""
        self.final: str | None = None
        """The run's final review, once dispatched (CR-48)."""
        self.author_alias = author_alias
        self.memory = memory
        self.images = images
        """The viewer each review gets a fresh copy of, when the reviewer's model accepts
        images (docs/specs/figures.md, FG-39)."""
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
        self.archived_as = archived_as
        self.archive = Archive(store.documents.control) if archived_as is not None else None
        self.archived = 0
        """The archive position of the last message observed, of this run's."""
        self.viewed: tuple[tuple[str, str], ...] | None = None
        """The earlier runs' calls, each its run's ID and its own, whose results the
        run's view held."""
        self.effects = False
        self.seen: set[str] = set()
        self.delivered: set[str] = set()
        self.notice_store = FindingNotices(store)
        self.findings = Findings(store)
        self.review_ids: MutableSequence[str] = (
            RunCollection[str](store.documents.control, (*self.scope, archived_as), "reviews")
            if archived_as is not None
            else []
        )

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
        _, locator, record = found
        index = len(messages) - 1
        located = {"run": self.run_id, "invocation": self.instance, "message": index, **locator}
        await asyncio.to_thread(
            record_source, self.store, *self.scope, f"{self.instance}:{index}", located, record
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
        located = await self._places(messages)
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
            at = {"archive": located[index]} if index in located else {}
            await asyncio.to_thread(
                self.store.observe,
                *self.scope,
                f"{self.instance}:message:{index}",
                "transcript",
                {"run": self.run_id, "invocation": self.instance, "message": index, **at},
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
            # A publish of several files is one artifact item each (W2).
            for index, publication in enumerate(publications(tool)):
                raw_artifact = publication.get("artifact")
                if not isinstance(raw_artifact, dict) or self.artifact_consumer is not None:
                    continue
                artifact = cast(Json, raw_artifact)
                encoded = dump(artifact)
                self.effects = True
                await asyncio.to_thread(
                    self.store.observe,
                    *self.scope,
                    f"{self.instance}:artifact:{self.position}" + (f":{index}" if index else ""),
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

    async def _places(self, messages: Sequence[Message]) -> dict[int, Json]:
        """The archive place of each message not observed yet, by its index, when the
        run's messages are archived; and, at the first, the earlier runs' calls its view
        held (``archived.held_calls``)."""
        archive, name = self.archive, self.archived_as
        if archive is None or name is None:
            return {}
        project, session = self.scope
        if self.viewed is None:
            held = messages[1 : self.position]
            self.viewed = tuple(
                await asyncio.to_thread(held_calls, archive, project, session, name, held)
            )
        located = await asyncio.to_thread(
            places, archive, project, session, name, messages, self.position, self.archived
        )
        if located:
            self.archived = max(self.archived, *(found["position"] for found in located.values()))
        return located

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

    async def _dispatch(
        self,
        kind: ReviewType,
        classifier: Json | None = None,
        *,
        final: bool = False,
        answer: str | None = None,
        recheck_of: Collection[str] | None = None,
    ) -> str | None:
        if self.role != "main":
            return None
        if self.off:
            # No reviewer is configured: reviews are off, said once, not failed (lab 43's B6).
            await asyncio.to_thread(self.store.off, *self.scope, REVIEWS_OFF)
            return None
        await asyncio.to_thread(self.findings.queue_rechecks, *self.scope)
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
            viewed=self.viewed or (),
            final=final,
            answer=answer,
            recheck_of=recheck_of,
        )
        if review is not None:
            self.review = review
            self.review_ids.append(review)
            self.task = asyncio.create_task(
                ReviewerRunner(
                    self.store,
                    *self.scope,
                    review,
                    self.owner,
                    self.model,
                    memory=self.memory,
                    images=self.images.fresh() if self.images is not None else None,
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
        if self.final is not None:
            # The answering turn's work is named unreviewed, not checkpointed (CR-48).
            return
        if self.task is None and await asyncio.to_thread(self.store.due, *self.scope):
            targets = await asyncio.to_thread(self.store.confirmation_targets, *self.scope)
            await self._dispatch("confirmation" if targets else "checkpoint")

    async def notices(self) -> list[Json]:
        if self.role != "main":
            return []
        return await asyncio.to_thread(
            partial(self.notice_store.pending, raised_for=self.run_id),
            *self.scope,
            self.run_id,
            self.seen,
        )

    async def _wait(self, task: asyncio.Task[None], deadline: float | None = None) -> str | None:
        """A user message wins even if completion races; no model polling occurs. With
        ``deadline``, by the event loop's clock, the wait ends then, the task running on."""
        loop = asyncio.get_running_loop()
        timeout = None if deadline is None else max(0.0, deadline - loop.time())
        if self.messages is None:
            await asyncio.wait((task,), timeout=timeout)
            if task.done():
                await asyncio.shield(task)
            return None
        incoming = asyncio.create_task(self.messages.get())
        consumed = False
        try:
            await asyncio.wait(
                (task, incoming), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if incoming.done() and not incoming.cancelled():
                consumed = True
                return incoming.result()
            if task.done():
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
            unresolved=bool(states.get("open")),
        )
        label["document_ingestion"] = ingestion
        label["artifact_ingestion"] = artifact_ingestion
        config, _ = self.store.config(self.context.project_id)
        summary: Json = {
            "classifier": label,
            "counts": counts,
        }
        if not config.enabled or self.role != "main":
            return self._review_summary(summary)
        conversation = label["label"] == "conversation"
        if conversation and self.task is None:
            return self._review_summary(summary)
        if self.off:
            await self._dispatch("terminal", label)
            return self._review_summary({**summary, "off": REVIEWS_OFF})
        clock = asyncio.get_running_loop().time
        # The whole wait at the run's end is bounded (END_WAIT_SECONDS): a checkpoint
        # still running has the allowance beyond the final review's own time.
        ending = clock() + self.end_seconds
        if self.task is not None:
            if not config.terminal_hold:
                return self._review_summary({**summary, "pending": True})
            message = await self._wait(self.task, ending - FINAL_REVIEW_SECONDS)
            if message is not None:
                return self._review_summary({**summary, "pending": True, "user_message": message})
            if not self.task.done() and self.review is not None:
                # Ended at its deadline: its window is recorded unreviewed (CR-48).
                await self._expire(self.review, "The run's end left it no more time.")
            self.task = None
            if conversation:
                # An explicit checkpoint review can still be running when the
                # author's last words merely say it is waiting. Its findings
                # need the ordinary response round before this run ends.
                if self.review is not None:
                    ended = await asyncio.to_thread(self.store.run, *self.scope, self.review)
                    summary["outcome"] = ended["state"]
                    if ended["state"] in ("failed", "dropped", "capped"):
                        summary["pending"] = True
                summary["counts"] = await asyncio.to_thread(self.notice_store.counts, *self.scope)
                return self._review_summary(summary)
        # One final review, with a budget of its own, instead of a walk through the
        # backlog window by window (CR-48): what it is not given is named unreviewed.
        review = await self._dispatch("terminal", label, final=True, answer=final_text)
        if review is None:
            # Another live process owns this session's review: the work waits as one
            # pending review. This one-shot driver cannot take over or wait on an unowned
            # in-memory task.
            summary["final_review"] = await self._final_summary(None, config, finished=False)
            return self._review_summary({**summary, "pending": True})
        self.final = review
        if not config.terminal_hold:
            summary["final_review"] = await self._final_summary(review, config)
            return self._review_summary({**summary, "pending": True})
        assert self.task is not None  # noqa: S101
        deadline = ending
        if config.final_review == "hold":
            # The hold's limit runs from when its final review starts.
            deadline = min(deadline, clock() + self.hold_seconds)
        message = await self._wait(self.task, deadline)
        if message is not None:
            return self._review_summary({**summary, "pending": True, "user_message": message})
        finished = self.task.done()
        if not finished:
            await self._expire(review, "The final review's time limit ended it.")
        self.task = None
        final = await self._final_summary(review, config, finished=None if finished else False)
        summary["outcome"] = final["outcome"]
        summary["pending"] = not final["finished"]
        if not final["complete"]:
            summary["coverage_gap"] = True
        summary["final_review"] = final
        summary["counts"] = await asyncio.to_thread(self.notice_store.counts, *self.scope)
        return self._review_summary(summary)

    async def _expire(self, review: str, why: str) -> None:
        """End review ``review`` at its deadline (CR-48): what it recorded stays, its
        window is recorded unreviewed with the watermark past it, and no retry waits; then
        stop its task, whose own ending is refused as the review has ended."""
        try:
            await asyncio.to_thread(
                partial(
                    self.store.finish,
                    *self.scope,
                    review,
                    self.owner,
                    "capped",
                    why,
                    cap_reached=True,
                    deadline=True,
                )
            )
        except DocumentError as error:
            if error.kind not in ("conflict", "policy_denied"):
                raise
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.task = None

    async def _final_summary(
        self, review: str | None, config: ReviewConfig, *, finished: bool | None = None
    ) -> Json:
        """The final review's summary (``ReviewStore.final_summary``), with when the answer
        went out (``mode``) and, when its review did not run, that it did not finish."""
        found = await asyncio.to_thread(self.store.final_summary, *self.scope, review)
        if finished is not None:
            found["finished"] = finished
        return {"mode": config.final_review, **found}

    async def settled(
        self, messages: Sequence[Message], summary: Json, *, answered: bool = True
    ) -> Json:
        """The summary at a closing after the run's final review (one a run, CR-48).
        After the turn that answered its findings (``answered``), that turn's work is
        recorded unreviewed, for later reviews: one round, no chain. After a turn another
        hook took, such as for a user's message, its new work has an ordinary checkpoint,
        under the ordinary limits, waited for within the end's bound. The findings' states
        are read as the turn left them."""
        await self.observe(messages)
        config, _ = await asyncio.to_thread(self.store.config, self.context.project_id)
        if self.final is not None:
            await self._after_final(answered)
        final = await self._final_summary(self.final, config)
        previous = cast(Json, summary.get("final_review") or {})
        final["answered"] = answered or previous.get("answered") is True
        final["rechecked"] = self.rechecked is not None
        if self.rechecked is not None:
            ended = await asyncio.to_thread(self.store.run, *self.scope, self.rechecked)
            final["recheck"] = {"review": self.rechecked, "outcome": ended["state"]}
        if previous.get("finished") is False:
            final["finished"] = False
        counts = await asyncio.to_thread(self.notice_store.counts, *self.scope)
        return self._review_summary({**summary, "final_review": final, "counts": counts})

    async def _after_final(self, answered: bool) -> None:
        """The work after the final review's window: recorded unreviewed after its
        answering turn; otherwise given to an ordinary checkpoint, if the hourly calls
        allow one, and whatever no review then covers is left after the watermark."""
        assert self.final is not None  # noqa: S101
        ended = await asyncio.to_thread(self.store.run, *self.scope, self.final)
        coverage = await asyncio.to_thread(self.store.coverage, *self.scope)
        start, head = int(ended["end_seq"]) + 1, int(coverage["feed_end"])
        if head < start:
            return
        if answered:
            await asyncio.to_thread(
                self.store.record_unreviewed,
                *self.scope,
                start,
                head,
                "answering_turn",
                self.final,
            )
            await self._recheck()
            return
        if self.task is not None or await self._dispatch("checkpoint") is None:
            return
        assert self.task is not None  # noqa: S101
        assert self.review is not None  # noqa: S101
        loop = asyncio.get_running_loop()
        if await self._wait(self.task, loop.time() + self.end_seconds) is not None:
            return
        if not self.task.done():
            await self._expire(self.review, "The run's end left it no more time.")
        self.task = None

    async def _recheck(self) -> None:
        """The one re-check after the answering turn (CR-48): of the findings that turn
        claimed fixed, with its own calls (``FINAL_RECHECK_CALLS``, outside the hourly
        calls) and time (``recheck_seconds``), ended at its deadline if it outlasts it."""
        assert self.final is not None  # noqa: S101
        fixed = await asyncio.to_thread(self.store.answered, *self.scope, self.final)
        if not fixed or self.task is not None:
            return
        review = await self._dispatch("confirmation", recheck_of=fixed)
        if review is None:
            return
        self.rechecked = review
        assert self.task is not None  # noqa: S101
        loop = asyncio.get_running_loop()
        if await self._wait(self.task, loop.time() + self.recheck_seconds) is not None:
            return
        if not self.task.done():
            await self._expire(review, "The re-check's time limit ended it.")
        self.task = None

    def _review_summary(self, summary: Json) -> Json:
        # A terminal hold may dispatch another window after the initial metadata
        # snapshot. Refresh the bounded preview and exact count at every return.
        return {
            **summary,
            "reviews": inline(self.review_ids) or [],
            "reviews_total": len(self.review_ids),
        }

    async def author_call(
        self, name: str, args: Json, operation: str, *, model: str | None = None
    ) -> tuple[str, Json]:
        if name == "review_findings":
            if set(args) - {"after", "status"}:
                raise DocumentError("invalid_request", "unknown finding-list field")
            status = args.get("status", "open")
            if status not in FINDING_STATUSES:
                raise DocumentError("invalid_request", "unknown finding status")
            result = await asyncio.to_thread(
                partial(self.findings.listing, after=args.get("after"), statuses=(status,)),
                *self.scope,
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
        config, _ = await asyncio.to_thread(self.store.config, self.context.project_id)
        self.holding = (
            config.enabled
            and config.terminal_hold
            and config.final_review == "hold"
            and self.role == "main"
            and not self.off
        )
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
        self.answering = False
        """Whether the run took its one turn to answer the final review's findings."""

    def lease(self) -> AbstractAsyncContextManager[None]:
        return self.controller.active()

    @property
    def holds_answers(self) -> bool:
        # The answer before the final review, and in hold the reply of its answering
        # turn, until the re-check after it ends (CR-48).
        controller = self.controller
        return controller.holding and (controller.final is None or self.answering)

    async def notices(self, point: Point, run: RunView) -> Sequence[Notice]:
        if point != "turn":
            return ()
        await self.controller.safe_point(run.context.messages)
        return await self._pending()

    async def model_returned(self, run: RunView) -> None:
        # The lease keeps delivered findings claimed until the saved run acknowledges them.
        self.controller.delivered.update(run.delivered_ids(self.name))

    async def observe_tool(self, run: RunView, record: Json) -> None:
        executed = run.tool_records
        call = dict(executed[-1]) if executed else None
        await self.controller.safe_point(run.context.messages, tool=record, call=call)

    async def closing(self, run: RunView, answer: str | None) -> Closing:
        controller = self.controller
        if controller.final is not None:
            # One final review a run (CR-48): after its answering turn, or a turn another
            # hook took, the closing reviews no more than an ordinary checkpoint would.
            self.result = await controller.settled(
                run.context.messages, self.result or {}, answered=self.answering
            )
            return Closing()
        self.result = await controller.terminal(run.context.messages, answer)
        if "user_message" in self.result:
            return Closing("stop", stopped="user_message")
        configuration, _ = await asyncio.to_thread(
            controller.store.config, controller.context.project_id
        )
        if not run.can_respond or run.response_rounds >= configuration.response_rounds:
            return Closing()
        # terminal() already ingested this view and settled its bounded review.
        # A fresh dispatch here would be abandoned when this final answer exits.
        found = await self._pending()
        # Only open findings delivered to this run take a round (CR-29 as amended).
        respond = any(n.ids for n in found if n.framing == REVIEW_FINDINGS.text)
        if not respond:
            return Closing("accept", found)
        final = cast(Json, self.result.get("final_review") or {})
        self.answering = controller.final is not None
        if isinstance(self.result.get("final_review"), dict):
            final["answered"] = False
        # With "hold", the user's answer is the one after the round, unless the hold's
        # time passed before the review ended (CR-48).
        return Closing(
            "respond",
            found,
            hold_answer=final.get("mode") == "hold" and final.get("finished") is not False,
        )

    async def accept(self, run: RunView) -> None:
        controller, counts = self.controller, (self.result or {}).get("counts", {})
        if controller.delivered and any(str(key).endswith(":open") for key in counts):
            await asyncio.to_thread(
                controller.findings.bound,
                *controller.scope,
                "The bounded author response rounds ended; no re-check found it fixed.",
                notices=controller.delivered,
            )
            if self.result is not None:
                self.result["counts"] = await asyncio.to_thread(
                    controller.notice_store.counts, *controller.scope
                )
                if isinstance(final := self.result.get("final_review"), dict):
                    found = await asyncio.to_thread(
                        controller.store.final_summary, *controller.scope, controller.final
                    )
                    self.result["final_review"] = {
                        **cast(Json, final),
                        "open": found["open"],
                        "fixes_claimed": found["fixes_claimed"],
                    }

    def summary(self) -> object | None:
        final = (self.result or {}).get("final_review")
        if self.answering and isinstance(final, dict) and not cast(Json, final).get("answered"):
            # The answering turn did not end in a closing: it failed or was stopped, and
            # the final review's findings were not answered.
            cast(Json, final)["answering_failed"] = True
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
        # Open findings ask for a response; dismissed and stale ones are not injected.
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
