# SPDX-License-Identifier: MIT
"""The harness the loop characterization drives: ``cli.main`` against an in-process
worker and a scripted OpenAI-compatible gateway, with notice sources made through
public stores, and a view of each message as its role, framing and notice IDs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
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
from sandbox_science.orchestrator.mathcheck import Formula, MathCheck
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
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
    "review_counts": "Consistency review counts (read review_findings for details; ",
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
    """Each message as (role, kind, detail): its framing and the notice IDs it carries."""
    result: list[Shape] = []
    for message in messages:
        role, content = str(message["role"]), message.get("content")
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


def call(name: str, /, **arguments: Any) -> tuple[str, Json]:
    return name, arguments


@dataclass
class Gateway:
    """Answers the main alias from ``script``, delegated runs (on the main alias) from
    ``delegated``, and the reviewer alias from ``reviewer``."""

    url: str = ""
    script: list[Reply] = field(default_factory=list[Reply])
    delegated: list[Reply] = field(default_factory=list[Reply])
    reviewer: Callable[[list[Message]], Reply] | None = None
    requests: dict[str, list[list[Message]]] = field(default_factory=dict[str, list[list[Message]]])
    numbered: int = 0

    @property
    def main(self) -> list[list[Message]]:
        """What each main model call saw, in order."""
        return self.requests.get("main", [])

    @property
    def children(self) -> list[list[Message]]:
        """What each delegated run's model call saw, in order."""
        return self.requests.get("delegated", [])

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        alias, messages = str(body["model"]), cast(list[Message], body["messages"])
        child = alias == "main" and str(messages[0].get("content", "")).startswith(DELEGATED)
        self.requests.setdefault("delegated" if child else alias, []).append(messages)
        if child:
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
        return web.json_response(
            {
                "model": f"scripted-{alias}",
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(messages), "completion_tokens": self.numbered},
            }
        )


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[Gateway]:
    found = Gateway()
    app = web.Application()
    app.router.add_post("/chat/completions", found.handle)
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
    ) -> RunRecord:
        """One ``run`` through the CLI; the record as the project keeps it. Its delegated
        runs answer from ``delegated``, in the order they call the model."""
        self.gateway.script = list(script)
        self.gateway.delegated = list(delegated)
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
        assert await asyncio.to_thread(cli.main, [*argv, TASK]) == status
        assert not self.gateway.script, "the run ended before the script did"
        assert not self.gateway.delegated, "the delegated runs ended before their script did"
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

    def work_request(self, operation: str = "request-1") -> str:
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
                    "body": "Explain the result.",
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
    # Reviews are off unless a test turns them on: with no reviewer model, every
    # terminal answer would otherwise record three failed review windows.
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
