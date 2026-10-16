# SPDX-License-Identifier: MIT
"""The harness the loop characterization drives: ``cli.main`` against an in-process
worker and a scripted OpenAI-compatible gateway, with notice sources made through
public stores, and a view of each message as its role, framing and notice IDs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import (
    ExecutionContext,
    KernelInfo,
    KernelObservation,
    WriteFileRequest,
)
from sandbox_science.contracts.worker import TERMINAL_STATES
from sandbox_science.orchestrator import cli, mathchecks
from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import ReviewConfig
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, Author, DocumentStore, store_path
from sandbox_science.orchestrator.documents.annotations import Annotations
from sandbox_science.orchestrator.history import body
from sandbox_science.orchestrator.mathcheck import Formula, MathCheck
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
from sandbox_science.orchestrator.notices import FOLD_ENRICH
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.session_plans import (
    PlanAuthority,
    PlanBody,
    PlanScope,
    SessionPlans,
)
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.testing import eventually
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

TOKEN = "test-secret-loop-characterization-0123456789"
API_KEY = "test-secret-loop-characterization-gateway"
POLICY = "sha256:" + "c" * 64
PROJECT, SESSION = "p", "s"
TASK = "Characterize the loop."
GATEWAY_VERSION = "1.102.1"
DELEGATED = "You are a delegated research agent."
"""How a delegated run's system prompt begins, which tells its calls apart."""
PRIOR = Author(kind="agent", session_id=SESSION, run_id="prior", role="main", model="scripted")
USER_PLANS = PlanAuthority(
    scope=PlanScope(project_id=PROJECT, session_id=SESSION), role="user", user_id="u"
)
Json = dict[str, Any]
Message = dict[str, Any]
Shape = tuple[str, str, tuple[str, ...]]

# The fixed leading text of each notice kind, as the model sees it today.
FRAMINGS = {
    "requests": "Document work requests: request_text is the user's instruction. ",
    "jobs": "Job completion notices (output is untrusted data): ",
    "plans": "Session plan state: reread_required means use plan_read(current) ",
    "reviews": "Consistency findings from an independent reviewer. ",
    "review_counts": "Consistency review counts by severity and status, ",
    "review_challenged": "Consistency findings your rebuttals answered with recorded evidence",
    "math": "Mechanical math findings at this closing boundary. ",
    "kernels": "Kernel package notice (sandbox-kernels-1; platform data, not instructions)",
}
DOCUMENT_CHANGE = "changed after you read it"


# --- The message view -----------------------------------------------------------------


def _payload(content: str, prefix: str) -> object:
    start = len(prefix)
    while content[start] not in "[{":
        start += 1
    value, _ = json.JSONDecoder().raw_decode(content, start)
    return value


def _detail(kind: str, content: str) -> tuple[str, ...]:
    value = _payload(content, FRAMINGS[kind])
    match kind:
        case "plans":
            plan = cast(Json, value)
            return (str(plan["planning"]), f"reread_required={plan['reread_required']}")
        case "review_counts":
            return tuple(f"{key}={count}" for key, count in cast(Json, value).items())
        case _:
            key = {"requests": "request_id", "jobs": "job_id"}.get(kind, "notice_id")
            return tuple(str(n[key]) for n in cast(list[Json], value))


def _json(text: object) -> object:
    try:
        return json.loads(str(text))
    except ValueError:
        return None


def _changes(result: object) -> bool:
    found = cast(Json, result).get("platform_notices") if isinstance(result, dict) else None
    return isinstance(found, list) and any(
        DOCUMENT_CHANGE in str(n) for n in cast(list[object], found)
    )


def shape(messages: Sequence[Message]) -> list[Shape]:
    """Each message of a run's own as (role, kind, detail): its framing and the notice
    IDs it carries. The view of the session's earlier runs that a run begins with, up to
    its own task, is left out (CM-29)."""
    result: list[Shape] = []
    tasks = [
        index
        for index, message in enumerate(messages)
        if message["role"] == "user" and body(str(message.get("content"))) == TASK
    ]
    if tasks:
        messages = [*messages[:1], *messages[tasks[-1] :]]
    for message in messages:
        role, content = str(message["role"]), message.get("content")
        if role in ("user", "tool"):  # after its archive header (CM-4a)
            content = body(str(content))
        if role == "system":
            result.append((role, "system", ()))
        elif role == "assistant":
            calls = [str(c["function"]["name"]) for c in message.get("tool_calls") or []]
            result.append((role, "calls:" + ",".join(calls) if calls else "answer", ()))
        elif role == "tool":
            parsed, kind, ids = _json(content), "result", ()
            if isinstance(parsed, dict) and "job_notices" in parsed:
                outer = cast(Json, parsed)
                ids = tuple(str(n["job_id"]) for n in cast(list[Json], outer["job_notices"]))
                parsed, kind = _json(outer["tool_result"]), "result+jobs"
            if _changes(parsed):
                kind += "+documents"
            result.append((role, kind, ids))
        elif content == TASK:
            result.append((role, "task", ()))
        else:
            text = str(content)
            kind = next((k for k, prefix in FRAMINGS.items() if text.startswith(prefix)), None)
            assert kind is not None, text[:200]
            result.append((role, kind, _detail(kind, text)))
    return result


USER_HEADER = re.compile(r"\[msg-[0-9a-f]{16} · user message\]\n")


def user_text(message: Message) -> str:
    """A delivered user message's text, after the header naming its archive ID (RS-7,
    CM-4a)."""
    assert message["role"] == "user", message
    content = str(message["content"])
    found = USER_HEADER.match(content)
    assert found is not None, content[:200]
    return content[found.end() :]


def own(record: RunRecord) -> list[Message]:
    """A run's own messages: its record's, without the view of its session's earlier
    runs that it began with (CM-29)."""
    view = record.summaries.get("view")
    parts = int(cast(Json, view)["parts"]) if isinstance(view, dict) else 0
    return [*record.messages[:1], *record.messages[1 + parts :]]


def plans(planning: str, reread: bool) -> Shape:
    return ("user", "plans", (planning, f"reread_required={reread}"))


def counts(**values: int) -> Shape:
    return ("user", "review_counts", tuple(f"{k.replace('_', ':')}={v}" for k, v in values.items()))


def notice(kind: str, *ids: str) -> Shape:
    return ("user", kind, ids)


SYSTEM: Shape = ("system", "system", ())
TASK_MESSAGE: Shape = ("user", "task", ())
ANSWER: Shape = ("assistant", "answer", ())
RESULT: Shape = ("tool", "result", ())


def calls(*names: str) -> Shape:
    return ("assistant", "calls:" + ",".join(names), ())


# --- The in-process worker, with a synthetic kernel observation ---------------------


@dataclass
class Lineage:
    """The kernels the worker observes; ``None`` leaves it to the real worker.

    The local backend records no kernel's start revision, so it never reports one that
    predates a package change. The test plays that part of the worker's HTTP API: one
    Python kernel started at revision 0, observed at revision ``current``.
    """

    current: int | None = None
    fail: bool = False
    """Whether an observation fails, as with an executor that is not connected yet."""

    def observation(self, context: ExecutionContext) -> KernelObservation:
        assert self.current is not None
        kernel = KernelInfo(
            kernel_id="kernel-synthetic",
            language="python",
            generation=1,
            start_revision=0,
            current_revision=self.current,
            revision_status="predates",
        )
        return KernelObservation(
            environment_id=context.environment_id,
            session_id=context.session_id,
            current_revision=self.current,
            observed_at=datetime.now(UTC),
            kernels=(kernel,),
        )


@dataclass
class Worker:
    url: str
    lineage: Lineage


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Worker]:
    root = tmp_path / "worker"
    root.mkdir()
    store = Store(root / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(python=sys.executable), root),
        Blobs(root / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=root,
    )
    lineage = Lineage()

    @web.middleware
    async def synthetic_kernels(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if request.path == "/v1/kernels/observe" and lineage.fail:
            return web.json_response(
                {"error": {"kind": "unavailable", "message": "synthetic failure"}}, status=503
            )
        if request.path == "/v1/kernels/observe" and lineage.current is not None:
            context = ExecutionContext.model_validate((await request.json())["context"])
            return web.Response(
                text=lineage.observation(context).model_dump_json(),
                content_type="application/json",
            )
        return await handler(request)

    app = create_app(manager, TOKEN)
    app.middlewares.append(synthetic_kernels)  # inside the worker's own token guard
    await manager.start()
    try:
        async with TestServer(app) as server:
            yield Worker(str(server.make_url("/")).rstrip("/"), lineage)
    finally:
        await manager.close()
        store.close()


# --- A scripted OpenAI-compatible gateway ----------------------------------------------


@dataclass
class Reply:
    """One scripted model answer; ``before`` runs while the model is thinking."""

    text: str | None = None
    calls: Sequence[tuple[str, Json]] = ()
    before: Callable[[], Awaitable[object]] | None = None
    calls_from: Callable[[list[Message]], Sequence[tuple[str, Json]]] | None = None
    """Makes the calls from what the model saw, for arguments only the run knows."""
    status: int = 200
    error: str = "synthetic gateway failure"
    """The body of an answer that is not a 200."""
    retry_after: str | None = None
    cost: str | None = None
    """The call's cost, as LiteLLM's proxy reports it in ``x-litellm-response-cost``."""
    tokens: int | None = None
    """The prompt tokens the gateway reports for the call; by default, its messages' count."""


def call(name: str, /, **arguments: Any) -> tuple[str, Json]:
    return name, arguments


@dataclass
class Gateway:
    """Answers the main alias from ``script``, delegated runs (on whichever alias, told
    apart by their system prompt) from ``delegated``, and the reviewer alias from
    ``reviewer``."""

    url: str = ""
    script: list[Reply] = field(default_factory=list[Reply])
    delegated: list[Reply] = field(default_factory=list[Reply])
    reviewer: Callable[[list[Message]], Reply] | None = None
    requests: dict[str, list[list[Message]]] = field(default_factory=dict[str, list[list[Message]]])
    capable: dict[str, bool] | None = None
    """Whether each alias can call tools, as ``GET /model/info`` says; without it that
    answers 404."""
    enrich: list[Reply] = field(default_factory=list[Reply])
    """The folds' enrichment calls' answers, in order; past them, a short narrative."""
    choices: list[object] = field(default_factory=list[object])
    """Each enrichment request's ``tool_choice``."""
    per_token: float | None = None
    """With it, each chat completion reports its request's characters, tools included,
    divided by it as its prompt tokens, unless its reply scripts them."""
    limits: dict[str, Json] | None = None
    """Each alias's model information for its limits, as ``GET /model/info`` says, such
    as ``max_input_tokens``."""
    routed: dict[str, list[Reply]] = field(default_factory=dict[str, list[Reply]])
    """Delegated runs' answers by a text their brief holds, for runs that call the model
    at once, such as a child and its own child; others answer from ``delegated``."""
    listed: list[str] | None = None
    """The aliases ``GET /models`` lists; without them it answers 404, as a gateway that
    cannot list them."""
    tools: dict[str, list[list[Json]]] = field(default_factory=dict[str, list[list[Json]]])
    """The tools each call carried, by alias."""
    numbered: int = 0
    embed: Callable[[str], list[float]] | None = None
    """``POST /embeddings``'s vector for each text; without it that answers 404."""
    embed_model: str = "embed-model-v1"
    """The model ``/embeddings`` says answered."""
    embed_status: int = 200
    embed_cost: str | None = "0.001"
    """``/embeddings``' cost header, per call."""
    embed_hold: asyncio.Event | None = None
    """While set to an unset event, ``/embeddings`` answers nothing: a peer that never
    answers, until the test sets it."""
    embedded: list[list[str]] = field(default_factory=list[list[str]])
    """The texts of each ``/embeddings`` request, in order."""
    responses: list[Json] = field(default_factory=list[Json])
    """The body of each ``/responses`` request, in order."""
    responded: list[str] = field(default_factory=list[str])
    """Each ``/responses`` answer, as it was sent."""
    raw: list[bytes] = field(default_factory=list[bytes])
    """Each ``/responses`` request's body, as it arrived."""
    enriching: list[int] = field(default_factory=list[int])
    """Which of the ``/responses`` requests (by index) were fold enrichments."""

    async def respond(self, request: web.Request) -> web.Response:
        """The main alias's script through OpenAI's Responses API, as LiteLLM serves it:
        each answer a reasoning item with synthetic encrypted content, then its text and
        calls."""
        assert request.headers.get("Authorization") == f"Bearer {API_KEY}"
        self.raw.append(await request.read())
        body = cast(Json, json.loads(self.raw[-1]))
        assert body["model"] == "main", body["model"]
        self.responses.append(body)
        last = cast(list[Json], body["input"])[-1:]
        if last and str(last[0].get("content", "")).startswith(FOLD_ENRICH.text):
            self.enriching.append(len(self.responses) - 1)
            narrative = json.dumps({"narrative": "Earlier work, as its outline shows.", "keys": []})
            reply = self.enrich.pop(0) if self.enrich else Reply(narrative)
        else:
            reply = self.script.pop(0) if self.script else Reply("(the script has ended)")
        if reply.before is not None:
            await reply.before()
        turn = len(self.responses)
        output: list[Json] = [
            {"id": f"rs_litellm_{turn}", "type": "reasoning", "summary": [],
             "encrypted_content": f"gAAAA-test-reasoning-{turn}"},
        ]  # fmt: skip
        if reply.text is not None:
            output.append(
                {"id": f"msg_litellm_{turn}", "type": "message", "status": "completed",
                 "role": "assistant", "phase": "final_answer",
                 "content": [{"type": "output_text", "text": reply.text, "annotations": []}]}
            )  # fmt: skip
        for name, arguments in reply.calls:
            self.numbered += 1
            output.append(
                {"id": f"fc_litellm_{self.numbered}", "type": "function_call",
                 "status": "completed", "call_id": f"call-{self.numbered}", "name": name,
                 "arguments": json.dumps(arguments)}
            )  # fmt: skip
        text = json.dumps(
            {"id": f"resp_litellm_{turn}", "object": "response", "status": "completed",
             "model": "scripted-main", "output": output,
             "usage": {"input_tokens": len(body["input"]) if self.per_token is None
                       else int(len(json.dumps(body)) / self.per_token), "output_tokens": 1}}
        )  # fmt: skip
        self.responded.append(text)
        headers = {"x-litellm-version": self.version, "Content-Type": "application/json"}
        return web.Response(text=text, headers=headers)

    claude: list[Json] = field(default_factory=list[Json])
    """The body of each ``/messages`` request, in order."""
    claude_answers: list[str] = field(default_factory=list[str])
    """Each ``/messages`` answer, as it was sent."""
    claude_raw: list[bytes] = field(default_factory=list[bytes])
    """Each ``/messages`` request's body, as it arrived."""
    claude_betas: list[str] = field(default_factory=list[str])
    """Each ``/messages`` request's ``anthropic-beta`` header."""
    served: dict[str, str] | None = None
    """The provider model behind each alias, as ``GET /model/info`` says."""

    async def message(self, request: web.Request) -> web.Response:
        """The main alias's script through Anthropic's Messages API, as LiteLLM serves
        it: each answer a signed reasoning block, then, before its calls, its text as a
        progress note, or else its text."""
        assert request.headers.get("Authorization") == f"Bearer {API_KEY}"
        self.claude_raw.append(await request.read())
        self.claude_betas.append(request.headers.get("anthropic-beta", ""))
        body = cast(Json, json.loads(self.claude_raw[-1]))
        assert body["model"] == "main", body["model"]
        self.claude.append(body)
        reply = self.script.pop(0) if self.script else Reply("(the script has ended)")
        turn = len(self.claude)
        updates = body["thinking"]["display"] == "updates"
        content: list[Json] = [{"type": "thinking", "thinking": "", "signature": f"sig-{turn}a"}]
        if reply.text is not None and reply.calls:
            note = reply.text if updates else ""
            content.append({"type": "thinking", "thinking": note, "signature": f"sig-{turn}b"})
        elif reply.text is not None:
            content.append({"type": "text", "text": reply.text})
        for name, arguments in reply.calls:
            self.numbered += 1
            content.append(
                {"type": "tool_use", "id": f"toolu_{self.numbered}", "name": name,
                 "input": arguments}
            )  # fmt: skip
        text = json.dumps(
            {"id": f"msg_{turn}", "type": "message", "role": "assistant",
             "model": "scripted-main", "content": content,
             "stop_reason": "tool_use" if reply.calls else "end_turn",
             "usage": {"input_tokens": 10, "cache_read_input_tokens": len(body["messages"]),
                       "cache_creation_input_tokens": 0, "output_tokens": 1}}
        )  # fmt: skip
        self.claude_answers.append(text)
        headers = {"x-litellm-version": self.version, "Content-Type": "application/json"}
        return web.Response(text=text, headers=headers)

    version: str = GATEWAY_VERSION
    """What ``x-litellm-version`` names on each chat completion."""

    @property
    def main(self) -> list[list[Message]]:
        """What each main model call saw, in order."""
        return self.requests.get("main", [])

    @property
    def children(self) -> list[list[Message]]:
        """What each delegated run's model call saw, in order."""
        return self.requests.get("delegated", [])

    async def models(self, request: web.Request) -> web.Response:
        if self.listed is None:
            return web.Response(status=404, text="not found")
        assert request.headers.get("Authorization") == f"Bearer {API_KEY}"
        data = [{"id": alias, "object": "model"} for alias in self.listed]
        return web.json_response({"object": "list", "data": data})

    async def model_info(self, request: web.Request) -> web.Response:
        if self.capable is None and self.served is None and self.limits is None:
            return web.Response(status=404, text="not found")
        assert request.headers.get("Authorization") == f"Bearer {API_KEY}"
        data = [
            {"model_name": alias, "model_info": {"supports_function_calling": calls}}
            for alias, calls in (self.capable or {}).items()
        ]
        data += [
            {"model_name": alias, "litellm_params": {"model": model}, "model_info": {}}
            for alias, model in (self.served or {}).items()
        ]
        data += [
            {"model_name": alias, "model_info": info} for alias, info in (self.limits or {}).items()
        ]
        return web.json_response({"data": data})

    async def embeddings(self, request: web.Request) -> web.Response:
        if self.embed is None:
            return web.Response(status=404, text="not found")
        assert request.headers.get("Authorization") == f"Bearer {API_KEY}"
        texts = cast(list[str], (await request.json())["input"])
        self.embedded.append(texts)
        if self.embed_hold is not None:
            await self.embed_hold.wait()
        if self.embed_status != 200:
            return web.Response(status=self.embed_status, text="the embedding model is down")
        data = [
            {"object": "embedding", "index": index, "embedding": self.embed(text)}
            for index, text in reversed(list(enumerate(texts)))
        ]
        headers = {} if self.embed_cost is None else {"x-litellm-response-cost": self.embed_cost}
        answer = {"object": "list", "model": self.embed_model, "data": data}
        return web.json_response(answer, headers=headers)

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        alias, messages = str(body["model"]), cast(list[Message], body["messages"])
        child = alias == "delegated" or (
            alias != "reviewer" and str(messages[0].get("content", "")).startswith(DELEGATED)
        )
        key = "delegated" if child else alias
        last = str(messages[-1].get("content", "")) if messages else ""
        if last.startswith(FOLD_ENRICH.text):
            key = "enrich"
            self.choices.append(body.get("tool_choice"))
        self.requests.setdefault(key, []).append(messages)
        self.tools.setdefault(key, []).append(cast(list[Json], body.get("tools") or []))
        brief = str(messages[1].get("content", "")) if len(messages) > 1 else ""
        routed = next((r for k, r in self.routed.items() if k in brief), None)
        if key == "enrich":
            narrative = json.dumps({"narrative": "Earlier work, as its outline shows.", "keys": []})
            reply = self.enrich.pop(0) if self.enrich else Reply(narrative)
        elif child and routed is not None:
            reply = routed.pop(0) if routed else Reply("(no routed script)")
        elif child:
            reply = self.delegated.pop(0) if self.delegated else Reply("(no delegated script)")
        elif alias == "main":
            reply = self.script.pop(0) if self.script else Reply("(the script has ended)")
        else:
            assert self.reviewer is not None, alias
            reply = self.reviewer(messages)
        if reply.before is not None:
            await reply.before()
        if reply.status != 200:
            headers = {} if reply.retry_after is None else {"Retry-After": reply.retry_after}
            return web.Response(status=reply.status, text=reply.error, headers=headers)
        tool_calls: list[Json] = []
        made = reply.calls_from(messages) if reply.calls_from is not None else reply.calls
        for name, arguments in made:
            self.numbered += 1
            tool_calls.append(
                {
                    "id": f"call-{self.numbered}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            )
        message: Json = {"role": "assistant", "content": reply.text}
        if tool_calls:
            message["tool_calls"] = tool_calls
        priced = {} if reply.cost is None else {"x-litellm-response-cost": reply.cost}
        priced["x-litellm-version"] = self.version
        return web.json_response(
            {
                "model": f"scripted-{alias}",
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": (
                        reply.tokens
                        if reply.tokens is not None
                        else len(messages)
                        if self.per_token is None
                        else int(len(json.dumps(body)) / self.per_token)
                    ),
                    "completion_tokens": self.numbered,
                },
            },
            headers=priced,
        )


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[Gateway]:
    found = Gateway()
    app = web.Application()
    app.router.add_post("/chat/completions", found.handle)
    app.router.add_post("/responses", found.respond)
    app.router.add_post("/messages", found.message)
    app.router.add_get("/models", found.models)
    app.router.add_get("/model/info", found.model_info)
    app.router.add_post("/embeddings", found.embeddings)
    async with TestServer(app) as server:
        found.url = str(server.make_url("/")).rstrip("/")
        yield found


@dataclass
class Reviewer:
    """A reviewer that flags the latest transcript item in each window while ``flags``
    lasts, and otherwise finishes inconclusive."""

    flags: int = 99
    windows: int = 0

    def __call__(self, messages: list[Message]) -> Reply:
        last = messages[-1]
        if last["role"] == "user":
            self.windows += 1
            return Reply(calls=[call("review_manifest")])
        asked = messages[-2]["tool_calls"][0]["function"]["name"]
        if asked == "review_manifest" and self.flags > 0:
            self.flags -= 1
            items = cast(list[Json], json.loads(last["content"])["evidence"])
            evidence = [i["evidence"] for i in items if i["kind"] == "transcript"][-1]
            return Reply(
                calls=[
                    call(
                        "review_finding",
                        severity="fail",
                        category="completion",
                        statement=f"Window {self.windows}: the answer claims more than it shows.",
                        anchors=[{"evidence": evidence}],
                        operation_id=f"flag-{self.windows}",
                    )
                ]
            )
        outcome = "findings" if asked == "review_finding" else "inconclusive"
        return Reply(json.dumps({"outcome": outcome, "summary": "Checked.", "cap_reached": False}))


# --- The harness under test: cli.main against the worker and the gateway ----------------


class RejectingChecker:
    """A math checker that rejects every formula, without Node or the built bundle."""

    def identity(self) -> dict[str, object]:
        return {"bundle_sha256": "a" * 64, "launcher_sha256": "b" * 64, "engine": "0.18.9"}

    async def check_many(self, formulas: list[Formula]) -> list[MathCheck]:
        return [
            MathCheck(
                id=f.id,
                status="rejected",
                error="Unknown command",
                remedy="x",
                input_sha256=hashlib.sha256(f.model_dump_json().encode()).hexdigest(),
                bundle_sha256="a" * 64,
                launcher_sha256="b" * 64,
                engine="0.18.9",
                runtime="22.0.0",
            )
            for f in formulas
        ]


PLAN: Json = {
    "goal": "Characterize the loop",
    "feasibility": "Inspect the recorded messages",
    "outputs": ({"title": "Result", "description": "Notes", "evidence_standard": "Reproduce"},),
    "tracks": (),
    "phases": (
        {
            "phase_id": "analysis",
            "title": "Analysis",
            "steps": (
                {
                    "step_id": "step-0",
                    "title": "Step",
                    "description": "Inspect",
                    "evidence_standard": "Retain evidence",
                },
            ),
        },
    ),
}


def start_job(name: str = "go") -> tuple[str, Json]:
    """A background job that ends once the test writes ``name`` (see Harness.end_job)."""
    return call(
        "start_job",
        script=f"while [ ! -e {name} ]; do sleep 0.02; done; echo job-output",
        purpose=f"wait for {name}",
        expected_progress="ends when the file appears",
    )


@dataclass
class Harness:
    tmp: Path
    worker: Worker
    gateway: Gateway

    @property
    def state(self) -> Path:
        return self.tmp / "state"

    def context(self) -> ExecutionContext:
        session = ProjectStore(self.state).get(PROJECT).sessions[SESSION]
        return ExecutionContext(
            project_id=PROJECT, session_id=SESSION, environment_id=session.environment_id
        )

    async def run(
        self,
        run_id: str,
        script: Sequence[Reply],
        *,
        status: int = 0,
        max_turns: int | None = None,
        max_input_chars: int | None = None,
        reviewer: bool = False,
        options: Sequence[str] = (),
        delegated: Sequence[Reply] = (),
        task: str = TASK,
        routed: dict[str, Sequence[Reply]] | None = None,
    ) -> RunRecord:
        """One ``run`` through the CLI; the record as the project keeps it. Its delegated
        runs answer from ``delegated``, in the order they call the model."""
        self.gateway.script = list(script)
        self.gateway.delegated = list(delegated)
        self.gateway.routed = {key: list(replies) for key, replies in (routed or {}).items()}
        self.gateway.requests.clear()
        transcript = self.tmp / f"{run_id}.json"
        token, key = self.tmp / "worker.token", self.tmp / "gateway.key"
        token.write_text(TOKEN)
        key.write_text(API_KEY)
        argv = [
            "run", "--worker", self.worker.url, "--token-file", str(token),
            "--project", PROJECT, "--session", SESSION, "--state-dir", str(self.state),
            "--allow-unisolated", "--model-url", self.gateway.url, "--api-key-file", str(key),
            "--model", "main", "--run-id", run_id, "--transcript", str(transcript),
        ]  # fmt: skip
        if max_turns is not None:
            argv += ["--max-turns", str(max_turns)]
        if max_input_chars is not None:
            argv += ["--max-input-chars", str(max_input_chars)]
        if reviewer:
            argv += ["--reviewer-model", "reviewer"]
        argv += list(options)
        assert await asyncio.to_thread(cli.main, [*argv, task]) == status
        assert not self.gateway.script, "the run ended before the script did"
        assert not self.gateway.delegated, "the delegated runs ended before their script did"
        assert not any(self.gateway.routed.values()), "a routed script was not used up"
        record = RunRecord.model_validate_json(transcript.read_text())
        store = ProjectStore(self.state)
        kept = [store.run(PROJECT, SESSION, entry.name) for entry in store.runs(PROJECT, SESSION)]
        assert kept.count(record) == 1
        return record

    # The notice sources, made through public stores as the user or an earlier run would.

    def documents(self) -> DocumentStore:
        store = DocumentStore(store_path(self.state))
        mathchecks.subscribe(store)  # as the harness and the workspace do
        return store

    def notes(self) -> None:
        """A project document the user wrote."""
        documents = self.documents()
        try:
            if not any(info.name == "notes" for info in documents.documents(PROJECT)):
                documents.create(
                    PROJECT, name="notes", title="Notes", text="A result.\n", author=USER
                )
        finally:
            documents.close()

    def work_request(self, operation: str = "request-1", body: str = "Explain the result.") -> str:
        """A USER work request addressed to the session, as its request ID."""
        self.notes()
        documents = self.documents()
        try:
            created = Annotations(documents).change(
                PROJECT,
                "notes",
                {
                    "action": "create",
                    "anchor": {"revision": 1, "block": "b1"},
                    "body": body,
                    "addressed_to": SESSION,
                    "operation_id": operation,
                },
                USER,
            )
            return str(created["request_id"])
        finally:
            documents.close()

    def request_states(self) -> dict[str, str]:
        documents = self.documents()
        try:
            annotations = Annotations(documents)
            return {
                str(request["request_id"]): str(request["state"])
                for thread in annotations.list(PROJECT, "notes")["threads"]
                for request in annotations.read_thread(PROJECT, "notes", thread["thread_id"])[
                    "requests"
                ]
            }
        finally:
            documents.close()

    def user_edit(self, text: str) -> None:
        documents = self.documents()
        try:
            documents.edit_source(
                PROJECT,
                "notes",
                base=documents.document(PROJECT, "notes").head,
                text=text,
                author=USER,
                reason="USER edit",
                operation_id=f"user-edit-{len(text)}",
            )
        finally:
            documents.close()

    def configure_reviews(self, config: ReviewConfig) -> None:
        documents = self.documents()
        try:
            ReviewStore(documents).configure(PROJECT, SESSION, config)
        finally:
            documents.close()

    def review_finding(self) -> str:
        """A fail finding an earlier review left open, as its notice ID."""
        documents = self.documents()
        try:
            reviews = ReviewStore(documents)
            reviews.configure(PROJECT, SESSION, ReviewConfig())
            reviews.observe(
                PROJECT, SESSION, "prior-claim", "transcript", {"run": "prior"},
                {"content": "There are 16."},
            )  # fmt: skip
            review = reviews.dispatch(
                PROJECT, SESSION, "terminal", author_run="prior", owner="fixture", alias="scripted"
            )
            assert review is not None
            evidence = reviews.manifest(PROJECT, SESSION, review)["evidence"][0]["evidence"]
            found = Findings(reviews).create(
                PROJECT,
                SESSION,
                review,
                "fixture",
                {
                    "severity": "fail",
                    "category": "contradicting_value",
                    "statement": "The count contradicts the pinned evidence.",
                    "anchors": [{"evidence": evidence}],
                    "operation_id": "prior-finding",
                },
            )
            reviews.finish(PROJECT, SESSION, review, "fixture", "findings", "A prior finding.")
            reviews.configure(PROJECT, SESSION, ReviewConfig(enabled=False))
            return f"{found['finding_id']}-r{found['revision']}"
        finally:
            documents.close()

    def findings(self) -> dict[str, str]:
        """Each finding's origin and status, as ``{origin: status}``."""
        documents = self.documents()
        try:
            listing = Findings(ReviewStore(documents)).listing(PROJECT, SESSION)["findings"]
            return {str(f["origin"]): str(f["status"]) for f in listing}
        finally:
            documents.close()

    async def math_finding(self) -> str:
        """A rejected formula in a workbook an earlier run wrote, as its notice ID.

        The math checker needs Node and a built bundle, which CI lacks; a checker that
        rejects every formula records the same finding through the public math queue.
        """
        documents = self.documents()
        try:
            documents.create(
                PROJECT,
                name="maths",
                title="Maths",
                text="$\\unknown{x}$\n\nOther paragraph.\n",
                author=PRIOR,
                session_id=SESSION,
            )
            jobs = MathJobs(ReviewStore(documents))
            while (check := await asyncio.to_thread(jobs.claim, PROJECT, "fixture")) is not None:
                while not await jobs.step(check, "fixture", RejectingChecker()):
                    pass
            listing = Findings(ReviewStore(documents)).listing(PROJECT, SESSION)["findings"]
        finally:
            documents.close()
        (found,) = [f for f in listing if f["origin"] == "math"]
        return f"{found['finding_id']}-r{found['revision']}"

    def user_plan(self) -> None:
        documents = self.documents()
        try:
            SessionPlans(documents).publish(
                USER_PLANS, "user-plan", PlanBody.model_validate(PLAN), "USER steering"
            )
        finally:
            documents.close()

    async def end_job(self, name: str = "go") -> str:
        """Let the session's running job end, and wait until the worker says it has."""
        context = self.context()
        async with WorkerClient(self.worker.url, TOKEN) as client:
            listing = await client.jobs(context)
            (job,) = [
                j.execution_id
                for j in listing.jobs
                if j.state not in TERMINAL_STATES
                and j.job is not None
                and j.job.purpose == f"wait for {name}"
            ]
            await client.write_file(
                WriteFileRequest(
                    context=context, path=name, content_b64=base64.b64encode(b"go").decode()
                )
            )

            async def ended() -> bool:
                status = await client.job_status(job, project_id=PROJECT)
                return status.execution.state in TERMINAL_STATES

            await eventually(ended)
        return job


@pytest_asyncio.fixture
async def harness(tmp_path: Path, worker: Worker, gateway: Gateway) -> Harness:
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", POLICY)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    found = Harness(tmp_path, worker, gateway)
    # Reviews are off unless a test turns them on: with no reviewer model they are
    # off anyway (lab 43's B6), and a test that names one turns them on.
    found.configure_reviews(ReviewConfig(enabled=False))
    return found


@dataclass
class Sources:
    job: str
    request: str
    finding: str
    math: str


async def six_sources(harness: Harness) -> Sources:
    """One notice of each kind waiting for the session's next run."""
    started = await harness.run("run-job", [Reply(calls=[start_job()]), Reply("A job is running.")])
    assert shape(started.messages) == [
        SYSTEM, TASK_MESSAGE, plans("no_current_plan", False), calls("start_job"), RESULT, ANSWER
    ]  # fmt: skip
    job = await harness.end_job()
    request = harness.work_request()
    finding = harness.review_finding()
    math = await harness.math_finding()
    harness.user_plan()
    harness.worker.lineage.current = 2
    return Sources(job, request, finding, math)
