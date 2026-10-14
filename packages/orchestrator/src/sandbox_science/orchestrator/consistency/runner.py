# SPDX-License-Identifier: MIT
"""A bounded reviewer loop whose tools cannot reach an execution boundary."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from typing import Literal

from pydantic import Field, ValidationError

from sandbox_science.contracts.model import Contract
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.mathjobs import threaded
from sandbox_science.orchestrator.model import ChatModel, Message, ModelError, ToolCall, ToolSpec

from .findings import Findings
from .models import ReviewConfig, ReviewerFinish
from .prompt_bundle import assemble_reviewer
from .rubric import RUBRIC, RUBRIC_VERSION
from .store import Json, ReviewStore, clipped, dump, text


class Read(Contract):
    evidence: str = Field(pattern=r"^ev-[0-9a-f]{24}$")
    start: int = Field(default=0, ge=0, le=100000000, strict=True)
    count: int = Field(default=16000, ge=1, le=16000, strict=True)
    block: str | None = Field(default=None, pattern=r"^b[0-9]{1,9}$")


class Manifest(Contract):
    after: int = Field(default=0, ge=0, le=1000000, strict=True)
    limit: int = Field(default=10, ge=1, le=20, strict=True)


class Checked(Contract):
    statement: str = Field(min_length=1, max_length=4096)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=16)


def schema(name: str, description: str, properties: Json, required: list[str]) -> ToolSpec:
    return ToolSpec(
        name,
        description,
        {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


SPECS = (
    schema(
        "review_manifest",
        "List the immutable snapshot evidence, in pages.",
        {"after": {"type": "integer"}, "limit": {"type": "integer"}},
        [],
    ),
    schema(
        "review_read",
        "Read a bounded slice of one pinned item; documents may name a block.",
        {
            "evidence": {"type": "string"},
            "start": {"type": "integer"},
            "count": {"type": "integer"},
            "block": {"type": "string"},
        },
        ["evidence"],
    ),
    schema(
        "review_checked",
        "Record a claim you checked and found consistent, with evidence IDs.",
        {
            "statement": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        ["statement", "evidence"],
    ),
    schema(
        "review_finding",
        "Record a bounded, pinned finding. Missing evidence is inconclusive.",
        {
            "severity": {"enum": ["fail", "warn", "note"]},
            "category": {
                "enum": [
                    "missing_action",
                    "contradicting_value",
                    "evidence_strength",
                    "reference",
                    "artifact_disagreement",
                    "method",
                    "completion",
                ]
            },
            "statement": {"type": "string"},
            "anchors": {"type": "array", "items": {"type": "object"}},
            "remedy": {"type": "string"},
            "assessment": {"enum": ["supported", "missing_evidence"]},
            "supersedes": {"type": "string"},
            "operation_id": {"type": "string"},
        },
        ["severity", "category", "statement", "anchors", "operation_id"],
    ),
    schema(
        "review_decide",
        "Confirm/reject a pinned claimed fix, or evaluate a pending rebuttal.",
        {
            "finding": {"type": "string"},
            "expected": {"type": "integer"},
            "decision": {
                "enum": ["confirm", "reject", "inconclusive", "accept_rebuttal", "reject_rebuttal"]
            },
            "explanation": {"type": "string"},
            "anchors": {"type": "array", "items": {"type": "object"}},
            "operation_id": {"type": "string"},
        },
        ["finding", "expected", "decision", "explanation", "anchors", "operation_id"],
    ),
)


class ReviewerRunner:
    """No Toolbox, worker client or execution tools are reachable from this loop."""

    def __init__(
        self,
        store: ReviewStore,
        project: str,
        session: str,
        review: str,
        owner: str,
        model: ChatModel | None,
    ) -> None:
        self.store, self.project, self.session = store, project, session
        self.review, self.owner, self.model = review, owner, model
        self.findings = Findings(store)
        self.record = store.run(project, session, review)
        self.config = ReviewConfig.model_validate(self.record["config"])

    async def tool(self, call: ToolCall) -> str:
        await asyncio.to_thread(
            self.store.grant, self.project, self.session, self.review, self.owner
        )
        if call.name not in {spec.name for spec in SPECS}:
            result: Json = {"error": "tool_not_granted", "tool": clipped(call.name, 128)}
        elif len(call.arguments) > 64000:
            result = {"error": "invalid_request", "message": "review arguments exceed their bound"}
        else:
            try:
                raw: object = json.loads(call.arguments)
                if call.name == "review_manifest":
                    value = Manifest.model_validate(raw)
                    result = await asyncio.to_thread(
                        self.store.manifest,
                        self.project,
                        self.session,
                        self.review,
                        after=value.after,
                        limit=value.limit,
                    )
                elif call.name == "review_read":
                    read = Read.model_validate(raw)
                    result = await asyncio.to_thread(
                        self.store.read_evidence,
                        self.project,
                        self.session,
                        self.review,
                        **read.model_dump(),
                        owner=self.owner,
                    )
                elif call.name == "review_checked":
                    checked = Checked.model_validate(raw)
                    text(checked.statement, 4096)
                    for evidence in checked.evidence:
                        await asyncio.to_thread(
                            self.store.evidence, self.project, self.session, self.review, evidence
                        )
                    result = {"checked": checked.model_dump(mode="json")}
                    await asyncio.to_thread(
                        self.store.record,
                        self.project,
                        self.session,
                        self.review,
                        "checked",
                        result,
                        owner=self.owner,
                    )
                elif call.name == "review_finding":
                    result = await asyncio.to_thread(
                        self.findings.create,
                        self.project,
                        self.session,
                        self.review,
                        self.owner,
                        raw,
                    )
                else:
                    result = await asyncio.to_thread(
                        self.findings.confirm,
                        self.project,
                        self.session,
                        self.review,
                        self.owner,
                        raw,
                    )
            except (ValueError, ValidationError, RecursionError, DocumentError) as error:
                result = {
                    "error": error.kind if isinstance(error, DocumentError) else "invalid_request",
                    "message": clipped(str(error), 1000),
                }
        # Refusals are durable observations too; they never call the author's tools.
        await asyncio.to_thread(
            self.store.record,
            self.project,
            self.session,
            self.review,
            "tool",
            {
                "id": clipped(call.id, 128),
                "tool": clipped(call.name, 128),
                "arguments": clipped(call.arguments, 64000),
                "result": result,
            },
            owner=self.owner,
        )
        encoded = dump(result)
        if len(encoded) > self.config.max_output_chars:
            return dump(
                {
                    "error": "response_bound",
                    "message": "Read a smaller evidence slice or manifest page.",
                }
            )
        return encoded

    async def run(self) -> None:
        if self.model is None:
            await self._finish(
                "failed", "The reviewer model alias is unavailable; this window was not reviewed."
            )
            return

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    await threaded(
                        self.store.renew, self.project, self.session, self.review, self.owner
                    )
                except DocumentError:
                    return

        lease = asyncio.create_task(heartbeat())
        try:
            async with asyncio.timeout(self.config.time_seconds):
                await self._loop()
        except TimeoutError:
            await self._finish(
                "capped", "The review time budget ended; recorded findings are retained.", cap=True
            )
        except asyncio.CancelledError:
            await self._finish("dropped", "The live reviewer task ended before a durable outcome.")
            raise
        except (ModelError, DocumentError, OSError, ValueError) as error:
            await self._finish(
                "failed", "The reviewer could not finish: " + clipped(str(error), 2000)
            )
        except Exception as error:
            await self._finish(
                "failed", f"The reviewer failed unexpectedly ({type(error).__name__})."
            )
        finally:
            lease.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease

    async def _loop(self) -> None:
        assert self.model is not None  # noqa: S101
        if self.record["rubric"] != RUBRIC_VERSION:
            raise ValueError("the reserved review rubric differs from the installed rubric")
        bundle = assemble_reviewer(RUBRIC, RUBRIC_VERSION, SPECS)
        messages: list[Message] = [
            {"role": "system", "content": bundle.provenance.rubric_text},
            {
                "role": "user",
                "content": dump(
                    {
                        "review": self.review,
                        "instruction": (
                            "Read review_manifest first. Check only this pinned snapshot."
                        ),
                    }
                ),
            },
        ]
        invalid = 0
        for _ in range(self.config.max_turns):
            encoded = dump(messages)
            if len(encoded) > self.config.max_input_chars:
                await self._finish(
                    "capped",
                    "The review context budget ended; recorded findings are retained.",
                    cap=True,
                )
                return
            # This is a soft estimate; provider usage is recorded separately and
            # used when higher. Only the gateway's spend cap is a monetary cap.
            estimate = math.ceil(len(encoded) / 4) + math.ceil(self.config.max_output_chars / 4)
            reservation = await threaded(
                self.store.reserve_call,
                self.project,
                self.session,
                self.review,
                estimate,
                owner=self.owner,
                prompt_provenance=bundle.provenance,
            )
            if reservation is None:
                await self._finish(
                    "capped",
                    "The reviewer call/token budget ended; recorded findings are retained.",
                    cap=True,
                )
                return
            await threaded(self.store.renew, self.project, self.session, self.review, self.owner)
            passed = bundle.tool_specs()
            bundle.verify(messages[0], passed)
            reply = await self.model.complete(messages, passed)
            await threaded(
                self.store.call_usage,
                self.project,
                self.session,
                self.review,
                reservation,
                model=reply.model,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                owner=self.owner,
            )
            content = reply.text or ""
            if (
                len(content) > self.config.max_output_chars
                or len(reply.tool_calls) > 16
                or sum(len(c.arguments) for c in reply.tool_calls) > 128000
            ):
                await self._finish("failed", "The reviewer response exceeded its bound.")
                return
            assistant: Message = {"role": "assistant", "content": content}
            if reply.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments},
                    }
                    for c in reply.tool_calls
                ]
            messages.append(assistant)
            await asyncio.to_thread(
                self.store.record,
                self.project,
                self.session,
                self.review,
                "model",
                {
                    "content": content,
                    "tools": [c.name for c in reply.tool_calls],
                    "resolved_model": reply.model,
                },
                owner=self.owner,
            )
            if reply.tool_calls:
                for call in reply.tool_calls:
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": await self.tool(call)}
                    )
                continue
            try:
                finish = ReviewerFinish.model_validate_json(content)
                text(finish.summary)
            except (ValidationError, DocumentError):
                invalid += 1
                if invalid > 1:
                    await self._finish(
                        "failed", "The reviewer returned invalid final output after one retry."
                    )
                    return
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Return the required JSON outcome, summary and cap_reached fields."
                        ),
                    }
                )
                continue
            with self.store.documents.control.read() as tx:
                db = tx.sql
                count = db.execute(
                    "SELECT count(*) FROM review_records WHERE review_id=? AND kind='finding'",
                    (self.review,),
                ).fetchone()[0]
                checked = db.execute(
                    "SELECT count(*) FROM review_records WHERE review_id=? AND kind='checked'",
                    (self.review,),
                ).fetchone()[0]
                undecided = db.execute(
                    "SELECT 1 FROM review_evidence e JOIN review_findings f ON "
                    "f.finding_id=json_extract(e.payload,'$.finding') "
                    "AND f.revision=json_extract(e.payload,'$.revision') "
                    "WHERE e.review_id=? AND e.kind='finding' "
                    "AND f.status='claimed fixed' LIMIT 1",
                    (self.review,),
                ).fetchone()
            # A reviewer cannot label its recorded problems as a pass, or turn an
            # unchecked window into a pass just by emitting the expected JSON.
            outcome = finish.outcome
            if count:
                outcome = "findings"
            elif outcome == "passed" and (
                not checked or undecided or not self.record["coverage_complete"]
            ):
                outcome = "inconclusive"
            await self._finish(outcome, finish.summary, cap=finish.cap_reached)
            return
        await self._finish(
            "capped", "The reviewer turn budget ended; recorded findings are retained.", cap=True
        )

    async def _finish(
        self,
        outcome: Literal["passed", "findings", "inconclusive", "capped", "failed", "dropped"],
        summary: str,
        *,
        cap: bool = False,
    ) -> None:
        try:
            await threaded(
                self.store.finish,
                self.project,
                self.session,
                self.review,
                self.owner,
                outcome,
                summary,
                cap_reached=cap,
            )
        except DocumentError as error:
            # An expired/recovered owner cannot overwrite the replacement's outcome.
            if error.kind not in ("conflict", "policy_denied"):
                raise
