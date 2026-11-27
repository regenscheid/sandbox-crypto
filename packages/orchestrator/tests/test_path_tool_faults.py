# SPDX-License-Identifier: MIT
"""Faults on the path from a tool call to the worker and back, walked end to end.

Each row of ``CASES`` is one worker-backed tool meeting one fault. The run service runs
a real harness against the in-process worker (local backend, real stores); the fault is
injected in the worker's HTTP channel, as a middleware inside its token guard, or, for
a call the worker reports lost, through the worker's own ``Dispatcher.lose``. The model
is scripted: it makes the row's call (after any calls the row needs first), then
answers.

A row passes in one of two ways and no other (D100; docs/specs/tree-resume.md; the
audit's invariants I4 and I8):

- **told**: the run goes on, and the result the model reads for the call is an error
  that says whose fault it is (``cause``), whether a retry is right (``retry``) and what
  to do next (``next``). The specification names no field for these; the names are the
  audit's recommended failure table (its S1) and stand for whatever the fix adopts;
- **recovered**: the run stops as a transient failure, automatic recovery resumes it,
  the call's original operation is reconciled by its identity (a read is never an
  action of unknown effect, a write is looked up by its operation ID), and the resumed
  run reads the reconciled result.

A fault the client is built to absorb (an event stream dropped and back within the
reconnect window) may also give the call's ordinary result. A row whose fault never
reached the call fails, so that no row passes by accident.

Where the code does something else today the row is ``xfail(strict=True)`` with the
audit's identifier, and with ``raises`` naming the property that fails, so a row that
fails for another reason is a failure, not an expected one.

Not every fault applies to every tool: ``NOT_APPLICABLE`` says why for each pair left
out. ``admit_hosts`` and ``provision`` are not offered by the in-process worker (it has
no managed environments or provisioner); their rows are walked through the toolbox
against a scripted worker endpoint instead (``test_a_tool_the_local_worker_lacks``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    AdmissionDecision,
    AdmissionResult,
    Clock,
    EnvironmentInfo,
    EnvironmentState,
    ErrorKind,
    Execution,
    ExecutionContext,
    ExecutionState,
    JobAttempt,
    ProvisionRequest,
    WriteFileRequest,
)
from sandbox_science.contracts.environment_claims import CLAIM_SECONDS
from sandbox_science.contracts.restarts import LossCause
from sandbox_science.contracts.worker import MAX_JOB_RESTARTS, TERMINAL_STATES
from sandbox_science.orchestrator.confinement.transport import TransportError
from sandbox_science.orchestrator.context.history import body
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.continuity.recovery_tools import tool_reconcilable
from sandbox_science.orchestrator.continuity.transient import classify
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.effects import UnknownEffectError
from sandbox_science.orchestrator.environment_claim import environment_claim
from sandbox_science.orchestrator.harness import Harness as Core
from sandbox_science.orchestrator.harness import Role, Settings, Stores
from sandbox_science.orchestrator.jobs import JobTools, lost_notice
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.model import Message, ModelTurn, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.service import RunService
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.testing import FakeClock, eventually, fake_worker_app
from sandbox_science.testing.pdfs import make_pdf
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

from .loop_harness import POLICY, PROJECT, SESSION, TOKEN, Harness, Lineage, Reply, Worker, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .scripted import ToolCall
from .test_artifact_publication import CONTEXT as ARTIFACT_CONTEXT
from .test_artifact_publication import TOKEN as ARTIFACT_TOKEN
from .test_artifact_publication import worker_url as worker_url
from .test_environment_claim import CONTEXT as CLAIM_CONTEXT
from .test_environment_claim import Client as ClaimClient
from .test_file_recording import Recording
from .test_file_recording import recording as recording
from .test_provision_tool import operation as provision_operation

pytestmark = pytest.mark.slow

Json = dict[str, Any]

# --- The table ------------------------------------------------------------------------

Effect = Literal["read", "write", "execution", "job", "observe", "kernel"]


@dataclass(frozen=True)
class Tool:
    """A worker-backed tool as a row calls it: its arguments (``@JOB@`` is the job the
    calls before it started), the calls that set up what it needs, and the worker routes
    its own call makes, the first being the one a fault at its call meets."""

    name: str
    effect: Effect
    arguments: Json
    routes: tuple[str, ...]
    before: tuple[tuple[str, Json], ...] = ()


NOTES: Final = ("write_file", {"path": "notes.txt", "content": "hello notes\n"})
PAPER: Final = (
    "shell",
    {
        "script": "echo "
        + base64.b64encode(make_pdf([[(72, 700, "hello x")]])).decode()
        + " | base64 -d > paper.pdf"
    },
)


def job(script: str) -> tuple[str, Json]:
    return (
        "start_job",
        {"script": script, "purpose": "a job the row needs", "expected_progress": "ends soon"},
    )


EXECUTIONS: Final = ("POST /v1/executions", "GET /v1/executions/")
TOOLS: Final = (
    Tool("read_file", "read", {"path": "notes.txt"}, ("POST /v1/files/read",), (NOTES,)),
    Tool("list_files", "read", {"path": "."}, ("POST /v1/files/list",), (NOTES,)),
    Tool("glob", "read", {"pattern": "*.txt"}, ("POST /v1/files/glob",), (NOTES,)),
    Tool("grep", "read", {"pattern": "hello"}, ("POST /v1/files/grep",), (NOTES,)),
    Tool("pdf_info", "read", {"path": "paper.pdf"}, EXECUTIONS, (PAPER,)),
    Tool("pdf_pages", "read", {"path": "paper.pdf", "pages": "1"}, EXECUTIONS, (PAPER,)),
    Tool("pdf_search", "read", {"path": "paper.pdf", "query": "x"}, EXECUTIONS, (PAPER,)),
    Tool("pdf_render", "read", {"path": "paper.pdf", "page": 1}, EXECUTIONS, (PAPER,)),
    Tool("write_file", "write", {"path": "out.txt", "content": "x\n"}, ("POST /v1/files/write",)),
    Tool(
        "edit_file",
        "write",
        {"path": "notes.txt", "old_string": "hello", "new_string": "goodbye"},
        ("POST /v1/files/edit",),
        (NOTES,),
    ),
    Tool("publish", "write", {"path": "notes.txt"}, ("POST /v1/artifacts",), (NOTES,)),
    Tool("shell", "execution", {"script": "sleep 0.3; echo once >> effects.txt"}, EXECUTIONS),
    Tool("python", "execution", {"code": "import time; time.sleep(0.3); print(1)"}, EXECUTIONS),
    Tool("sage", "execution", {"code": "print(1)"}, EXECUTIONS),
    Tool(
        "start_job",
        "job",
        {"script": "sleep 0.2", "purpose": "the row's job", "expected_progress": "ends soon"},
        ("POST /v1/executions",),
    ),
    Tool("job_status", "observe", {"job_id": "@JOB@"}, ("GET /v1/jobs/",), (job("sleep 0.2"),)),
    Tool("wait_jobs", "observe", {"job_ids": ["@JOB@"]}, ("GET /v1/jobs/",), (job("sleep 0.2"),)),
    Tool("list_jobs", "observe", {}, ("GET /v1/jobs",)),
    Tool(
        "cancel_job",
        "job",
        {"job_id": "@JOB@"},
        ("POST /v1/executions/",),
        (job("sleep 1"),),
    ),
    Tool("restart_kernel", "kernel", {"language": "python"}, ("POST /v1/kernels/restart",)),
)
BY_NAME: Final = {tool.name: tool for tool in TOOLS}
STREAMED: Final = frozenset(t.name for t in TOOLS if t.routes == EXECUTIONS)
"""Tools whose call follows an execution's event stream to its end (``WorkerClient.run``)."""

Fault = Literal[
    "refused",
    "server_error",
    "reset",
    "reply_lost",
    "stream_drop",
    "restart",
    "lost",
    "restart_lost",
    "environment_gone",
    "disk_full",
    "quota",
]
FAULTS: Final[tuple[Fault, ...]] = (
    "refused",
    "server_error",
    "reset",
    "reply_lost",
    "stream_drop",
    "restart",
    "lost",
    "restart_lost",
    "environment_gone",
    "disk_full",
    "quota",
)
"""``refused``: a definite 4xx at the call. ``server_error``: a 5xx before the worker
acts. ``reset``: the connection is reset before the reply, the worker not having acted.
``reply_lost``: the worker acts, and its reply is lost. ``stream_drop``: the event
stream drops twice and comes back, inside the reconnect window. ``restart``: the worker
is unreachable, from just after the submission, for longer than the reconnect window,
while the execution goes on. ``lost``: the worker reports the call lost (its executor
gone). ``restart_lost``: both: the worker comes back and reports the call lost.
``environment_gone``: the worker answers that the sandbox is gone (its ``_abandon``
text). ``disk_full``: no space left on the device. ``quota``: a limit of the worker's
policy is reached."""

ABSORBED: Final = frozenset({"stream_drop"})
"""Faults the client is built to absorb, so that the call's ordinary result is right."""

ONE_REQUEST: Final = "the call is one request with no reconnect window: as reset"
NOT_APPLICABLE: Final[dict[tuple[str, Fault], str]] = {
    **{
        (t.name, f): ONE_REQUEST if f == "restart" else "the call follows no event stream"
        for t in TOOLS
        if t.name not in STREAMED
        for f in ("stream_drop", "restart")
    },
    **{
        (t.name, f): "the call has no execution for the worker to report lost"
        for t in TOOLS
        if t.name not in STREAMED
        for f in ("lost", "restart_lost")
    },
    **{
        (name, "environment_gone"): "the job records are the worker's; no sandbox is reached"
        for name in ("job_status", "wait_jobs", "list_jobs", "cancel_job")
    },
    **{
        (t.name, "disk_full"): "the call writes nothing"
        for t in TOOLS
        if t.effect in ("read", "observe")
    },
    ("sage", "disk_full"): "the local backend has no sage kernel to write with",
    ("start_job", "disk_full"): "a job's full disk is one of its endings (job endings table)",
    ("cancel_job", "disk_full"): "the call writes nothing",
    ("restart_kernel", "disk_full"): "the call writes nothing",
    ("publish", "disk_full"): "the worker's blob store is not the session's disk",
    **{
        (t.name, "quota"): "no limit of the worker's policy applies to the call"
        for t in TOOLS
        if t.name not in ("start_job", "publish")
    },
}


class Violation(AssertionError):  # noqa: N818 - each names the property that fails
    """A property the specification requires of a fault's outcome does not hold."""


class NotTold(Violation):
    """The run went on, but the error the model reads does not say whose fault it is,
    whether a retry is right and what to do next."""


class RecoveryDeclined(Violation):
    """The run stopped, and automatic recovery did not resume it."""


class RecoveryBlocked(Violation):
    """Automatic recovery resumed the tree, then stopped it on the call's operation."""


class NotToldOnResume(Violation):
    """The resumed run does not read what became of the call."""


@dataclass(frozen=True)
class Known:
    """Why a row fails today: the audit's identifier, and the property that fails."""

    audit: str
    raises: type[Violation]
    reason: str


READS: Final = tuple(t.name for t in TOOLS if t.effect == "read" and t.name not in STREAMED)
WRITES: Final = ("write_file", "edit_file", "publish")
EXECUTIONS_TOOLS: Final = tuple(sorted(STREAMED))
NO_RETRY_FIELD: Final = "no failure says whether to retry or what next"

KNOWN: Final[dict[tuple[str, Fault], Known]] = {
    # Definite refusals reach the model as {"error": {kind, message}} only.
    **{(t.name, "refused"): Known("V5", NotTold, NO_RETRY_FIELD) for t in TOOLS},
    # A POST whose reply is a 5xx or lost is an unknown effect: the run fails. Pure reads
    # are POSTs too and no adapter reconciles them, nor writes, cancel or kernel restarts.
    **{
        (name, fault): Known(
            "V2",
            RecoveryDeclined,
            "a POST tool's lost reply is an unknown effect no "
            "adapter reconciles, so recovery declines",
        )
        for name in (*READS, "write_file", "edit_file", "cancel_job", "restart_kernel")
        for fault in ("server_error", "reset", "reply_lost", "environment_gone")
        if (name, fault) not in NOT_APPLICABLE
    },
    # The publisher holds the files it could not publish and says so, in prose; the
    # one retry statement of the tool layer invites a retry whatever the outcome was.
    **{
        ("publish", fault): Known("V5", NotTold, "a held file's error names no cause or next step")
        for fault in ("server_error", "reset", "reply_lost", "environment_gone")
    },
    # A GET tool's failure is shown, with no word on retrying or that the jobs go on.
    **{
        (name, fault): Known("V6", NotTold, "a worker blip reads as `cannot reach the worker`")
        for name in ("job_status", "wait_jobs", "list_jobs")
        for fault in ("server_error", "reset", "reply_lost")
    },
    # A submission the worker never saw: the lookup finds nothing and recovery stops,
    # though nothing ran (new, see the report).
    **{
        (name, fault): Known(
            "new:N1",
            RecoveryBlocked,
            "an operation the worker never received blocks "
            "Resume as unknown_operation, though it had no effect",
        )
        for name in (*EXECUTIONS_TOOLS, "start_job")
        for fault in ("server_error", "reset", "environment_gone")
    },
    # A lost execution: shown, with no word on whether it may still run or a rerun.
    **{
        (name, "lost"): Known("V6", NotTold, "`… outcome is unknown`, with no next step")
        for name in EXECUTIONS_TOOLS
    },
    **{
        (name, "restart_lost"): Known(
            "V1",
            RecoveryBlocked,
            "a worker restart past the window ends the run, and the "
            "lost call stops recovery with lost_operation",
        )
        for name in EXECUTIONS_TOOLS
    },
    # Disk full: a write tool's ENOSPC is a 503, so an unknown effect; a command's is its
    # own stderr.
    **{
        (name, "disk_full"): Known(
            "V2", RecoveryDeclined, "ENOSPC is `unavailable` (503): an unknown effect"
        )
        for name in ("write_file", "edit_file")
    },
    **{
        (name, "disk_full"): Known("V5", NotTold, "a full disk is the command's own stderr")
        for name in ("shell", "python")
    },
    **{(name, "quota"): Known("V5", NotTold, NO_RETRY_FIELD) for name in ("start_job", "publish")},
}


@dataclass(frozen=True)
class Case:
    tool: Tool
    fault: Fault
    known: Known | None


CASES: Final = [
    Case(tool, fault, KNOWN.get((tool.name, fault)))
    for tool in TOOLS
    for fault in FAULTS
    if (tool.name, fault) not in NOT_APPLICABLE
]


def _param(case: Case) -> Any:
    marks = (
        [
            pytest.mark.xfail(
                strict=True,
                raises=case.known.raises,
                reason=f"{case.known.audit}: {case.known.reason}",
            )
        ]
        if case.known is not None
        else []
    )
    return pytest.param(case, id=f"{case.tool.name}-{case.fault}", marks=marks)


# --- The worker, with a fault in its channel --------------------------------------------


@dataclass
class Rule:
    """A fault armed on a set of routes, for its next ``times`` matching requests."""

    fault: Fault
    routes: tuple[str, ...]
    times: int = 1
    met: list[str] = field(default_factory=list[str])

    def matches(self, request: web.Request) -> bool:
        line = f"{request.method} {request.path}"
        return self.times > 0 and any(
            line.startswith(route) if route.endswith("/") else line == route
            for route in self.routes
        )


@dataclass
class Faults:
    """The faults armed on the worker's channel; ``lost`` loses the next submission."""

    rules: list[Rule] = field(default_factory=list[Rule])
    lose: bool = False
    lost: list[str] = field(default_factory=list[str])

    def arm(self, rule: Rule) -> None:
        self.rules.append(rule)

    def lift(self) -> None:
        for rule in self.rules:
            rule.times = 0
        self.lose = False

    def met(self) -> list[str]:
        return [line for rule in self.rules for line in rule.met]


@dataclass
class FaultyWorker(Worker):
    manager: ExecutionManager
    faults: Faults


GONE: Final = "sandbox ss-env-00000000 is gone; create the environment again"
"""The worker's answer when it gives up an environment (``W/environments.py`` _abandon)."""
FULL: Final = "cannot change 'out.txt': No space left on device"
"""The worker's answer to a file tool on a full disk (``W/sandbox_files.py`` file_error)."""


def _refusal(kind: ErrorKind, message: str, status: int) -> web.Response:
    return web.json_response({"error": {"kind": kind.value, "message": message}}, status=status)


def _reset(request: web.Request) -> web.Response:
    """Close the connection: the client sees it end before any reply."""
    assert request.transport is not None
    request.transport.close()
    return web.Response(status=500)


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[FaultyWorker]:
    """``loop_harness.worker`` with ``Faults`` in its channel, inside its token guard."""
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
    faults = Faults()

    def lose(response: web.StreamResponse) -> None:
        if not faults.lose or not isinstance(response, web.Response) or response.status != 201:
            return
        faults.lose = False
        execution = str(json.loads(response.text or "{}")["execution_id"])
        # The worker's own ending of an attempt whose executor is gone (W/recovery.py
        # passes this ``what``), reached as ManagerView.lose does in the worker's tests.
        live = manager._journal.live.get(execution)
        if live is not None:
            manager._dispatcher.lose(live, "the sandbox executor incarnation", LossCause.NEW_DAEMON)
            faults.lost.append(execution)

    @web.middleware
    async def channel(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        rule = next((r for r in faults.rules if r.matches(request)), None)
        if rule is None:
            response = await handler(request)
            if request.method == "POST" and request.path == "/v1/executions":
                lose(response)
            return response
        rule.times -= 1
        rule.met.append(f"{request.method} {request.path}")
        match rule.fault:
            case "refused":
                return _refusal(ErrorKind.CONFLICT, "synthetic: the worker refused the call", 409)
            case "server_error":
                return _refusal(ErrorKind.UNAVAILABLE, "synthetic: the worker failed", 503)
            case "environment_gone":
                return _refusal(ErrorKind.UNAVAILABLE, GONE, 503)
            case "disk_full":
                return _refusal(ErrorKind.UNAVAILABLE, FULL, 503)
            case "reply_lost":
                response = await handler(request)
                lose(response)
                return _reset(request)
            case _:  # reset, stream_drop, restart, restart_lost
                return _reset(request)

    app = create_app(manager, TOKEN)
    app.middlewares.append(channel)
    await manager.start()
    try:
        async with TestServer(app) as server:
            yield FaultyWorker(str(server.make_url("/")).rstrip("/"), Lineage(), manager, faults)
    finally:
        await manager.close()
        store.close()


OUTAGE: Final = 1000
"""Requests an outage fails until the walk lifts it, once the run has stopped."""
RETRIED: Final = 2
"""aiohttp sends an idempotent request again, once, when a reused connection closes
before its reply: a fault at a GET meets both, or the client never sees it."""


def arm(faults: Faults, case: Case) -> None:
    """Arm ``case``'s fault for the row's own call."""
    tool, fault = case.tool, case.fault
    first = tool.routes[:1]
    match fault:
        case "stream_drop":
            faults.arm(Rule(fault, ("GET /v1/executions/",), times=RETRIED))
        case "restart" | "restart_lost":
            # Every reconnection and the final read fail, past the reconnect window,
            # until the run has stopped and the walk lifts the outage.
            faults.arm(Rule(fault, ("GET /v1/executions/",), times=OUTAGE))
            faults.lose = fault == "restart_lost"
        case "lost":
            faults.lose = True
        case "disk_full" if tool.effect == "execution":
            pass  # the command writes to a full device itself (``disk_full_call``)
        case "quota":
            pass  # the worker's own limit (``row_calls``)
        case "reset" | "reply_lost" if first[0].startswith("GET "):
            faults.arm(Rule(fault, first, times=RETRIED))
        case _:
            faults.arm(Rule(fault, first))


# --- The model and the walk -------------------------------------------------------------

FAULTED: Final = "call-fault"


@dataclass
class Model:
    """Makes ``before``'s calls one a turn, then the row's call, arming its fault, then
    answers; ``seen`` keeps what each model call saw."""

    before: list[tuple[str, Json]]
    call: tuple[str, Json] | None
    """``None``: no call; the fault is armed as the model gives its answer."""
    armed: Callable[[], None]
    seen: list[list[Message]] = field(default_factory=list[list[Message]])
    made: bool = False

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        self.seen.append(list(messages))
        index = len(self.seen) - 1
        if index < len(self.before):
            name, arguments = self.before[index]
            return ModelTurn(None, (ToolCall(f"call-before-{index}", name, json.dumps(arguments)),))
        if not self.made and self.call is None:
            self.made = True
            self.armed()
            return ModelTurn("Done.")
        if not self.made and self.call is not None:
            self.made = True
            name, arguments = self.call
            text = json.dumps(arguments).replace("@JOB@", _last_job(messages))
            self.armed()
            return ModelTurn(None, (ToolCall(FAULTED, name, text),))
        return ModelTurn("Done.")


def _results(messages: Sequence[Message]) -> list[str]:
    return [body(str(m.get("content"))) for m in messages if m.get("role") == "tool"]


def _last_job(messages: Sequence[Message]) -> str:
    for text in reversed(_results(messages)):
        found = re.search(r'"job_id":\s*"(exe-[0-9a-f]+)"', text)
        if found:
            return found[1]
    return "exe-none"


def parsed(text: str) -> object:
    """The JSON a tool result leads with; a result with job notices, its own part."""
    try:
        value, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except ValueError:
        return None
    if isinstance(value, dict) and "tool_result" in value:
        return parsed(str(cast(Json, value)["tool_result"]))
    return cast(object, value)


def errors_in(value: object) -> list[Json]:
    """Every error the result holds: ``{"error": {...}}`` at any depth, and an
    execution result that did not succeed, which is its own error."""
    found: list[Json] = []
    if isinstance(value, dict):
        item = cast(Json, value)
        if isinstance(item.get("error"), dict):
            found.append(cast(Json, item["error"]))
        elif item.get("state") in ("failed", "lost", "timed_out", "cancelled"):
            found.append(item)
        for key, inner in item.items():
            if key != "error":
                found += errors_in(inner)
    elif isinstance(value, list):
        for inner in cast(list[object], value):
            found += errors_in(inner)
    return found


def _injected(case: Case) -> bool:
    """Whether the walk injects the row's fault, rather than the call meeting it itself."""
    return case.fault != "quota" and not (
        case.fault == "disk_full" and case.tool.effect == "execution"
    )


def _channel(error: Json) -> bool:
    """Whether an error is the channel's failure, rather than the call's own."""
    return error.get("kind") == "unavailable" or error.get("state") == "lost"


def told(error: Json) -> bool:
    """Whether an error says whose fault it is, whether to retry and what to do next."""
    return all(isinstance(error.get(key), str) and error[key] for key in ("cause", "retry", "next"))


@dataclass
class Walk:
    """What became of a row: the run's own result for the call, or its recovery."""

    outcome: Literal["went_on", "declined", "recovered", "blocked"]
    result: str | None = None
    """The faulted call's result as the model read it, in the run or after the Resume."""
    reason: str | None = None
    failure: object = None


@contextlib.asynccontextmanager
async def service_for(harness: Harness, model: Model) -> AsyncIterator[RunService]:
    """The run service on the harness's stores, with a client that reconnects at once
    (the reconnect window's attempts, without its seconds)."""
    projects = ProjectStore(harness.state)
    documents = DocumentStore(store_path(harness.state), producer="harness")
    try:
        async with WorkerClient(harness.worker.url, TOKEN, reconnect_delay=0.01) as client:
            stores = Stores(
                projects,
                documents,
                JobState(harness.state, control=documents.control),
                RunState(projects.control),
            )
            core = Core(
                client,
                Role(model, "main"),
                stores=stores,
                settings=Settings(min_isolation_tier=None),
            )
            service = RunService(core, poll=0.02, retry=0.0)
            try:
                yield service
            finally:
                await service.close()
    finally:
        documents.close()
        projects.close()


def _root(service: RunService) -> str:
    with service.stores.projects.control.read() as tx:
        (root,) = tx.sql.execute(
            "SELECT work FROM research_work WHERE project=? AND session=? AND parent IS NULL",
            (PROJECT, SESSION),
        ).fetchone()
    return str(root)


def _faulted(messages: Sequence[Message]) -> str | None:
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == FAULTED:
            return body(str(message.get("content")))
    return None


ROUNDS: Final = 6
"""Looks the service takes at a stopped run, each an automatic-recovery check and a
tick, before the walk takes it as one recovery declined."""


async def walk(harness: Harness, model: Model, faults: Faults) -> Walk:
    """Run the row's run; if it stops, let the service's automatic recovery resume it."""
    RunState(ProjectStore(harness.state).control).post(
        PROJECT, SESSION, "m-1", "Investigate.", author="user"
    )
    async with service_for(harness, model) as service:
        works = WorkRecords(service.stores.projects.control)

        async def settle() -> None:
            async def idle() -> bool:
                await service.tick()  # starts a Resume, then offers its ready branches
                return not service.active and not await service.waiting_resumes()

            await eventually(idle, timeout=60)

        await settle()
        root = works.get(PROJECT, SESSION, _root(service))
        if root.state == "completed":
            return Walk("went_on", _faulted(model.seen[-1]))
        faults.lift()  # the worker is back
        projects = service.stores.projects
        failure = [
            projects.run(PROJECT, SESSION, entry.name).summaries.get("failure")
            for entry in projects.runs(PROJECT, SESSION)
        ]
        for _ in range(ROUNDS):
            await service.auto_recovery_tick()
            await settle()
            root = works.get(PROJECT, SESSION, root.work)
            if root.state in ("completed", "blocked", "stopped"):
                break
        reason = f"{root.state}: {root.reason}"
        if root.state == "completed":
            found = [text for text in _results(model.seen[-1]) if '"reconciled"' in text]
            return Walk("recovered", found[-1] if found else None, reason, failure)
        if root.state == "blocked":
            return Walk("blocked", reason=reason, failure=failure)
        return Walk("declined", reason=reason, failure=failure)


def judge(case: Case, walked: Walk, faults: Faults) -> None:
    """Assert one of the two acceptable outcomes (the module's docstring)."""
    detail = f"{walked}; faults met: {faults.met()}; lost: {faults.lost}"
    if walked.result is not None and f"there is no tool named '{case.tool.name}'" in walked.result:
        pytest.skip(f"{case.tool.name} is not offered here (its program is not installed)")
    match walked.outcome:
        case "declined":
            raise RecoveryDeclined(detail)
        case "blocked":
            raise RecoveryBlocked(detail)
        case "recovered":
            reconciled = parsed(walked.result or "")
            if (
                not isinstance(reconciled, dict)
                or cast(Json, reconciled).get("reconciled") is not True
            ):
                raise NotToldOnResume(detail)
            execution = cast(Json, reconciled).get("execution")
            if case.tool.effect in ("execution", "job") or case.tool.name in STREAMED:
                assert isinstance(execution, dict), detail
                assert cast(Json, execution)["state"] != "lost", detail
        case "went_on":
            assert walked.result is not None, f"the model never read the call's result: {detail}"
            reached = faults.met() or faults.lost or not _injected(case)
            assert reached, f"the fault never reached the call: {detail}"
            errors = errors_in(parsed(walked.result))
            if case.fault in ABSORBED and not any(_channel(error) for error in errors):
                return  # the call's own result, the fault absorbed
            assert errors, f"the call succeeded as if the fault had not been: {detail}"
            if not all(told(error) for error in errors):
                raise NotTold(detail)


def disk_full_call(tool: Tool) -> tuple[str, Json]:
    """A command that writes to a device that is always full (ENOSPC)."""
    if tool.name == "shell":
        return "shell", {"script": "echo data > /dev/full"}
    return "python", {"code": "f = open('/dev/full', 'w')\nf.write('x')\nf.close()"}


def row_calls(case: Case) -> tuple[list[tuple[str, Json]], tuple[str, Json]]:
    tool = case.tool
    before = list(tool.before)
    call: tuple[str, Json] = (tool.name, tool.arguments)
    if case.fault == "disk_full" and tool.effect == "execution":
        call = disk_full_call(tool)
    elif case.fault == "quota" and tool.name == "start_job":
        # The session's four active jobs (the policy's default jobs_per_session).
        before += [job("sleep 60")] * 4  # cancelled when the row ends (``cancel_jobs``)
    elif case.fault == "quota" and tool.name == "publish":
        before = [
            ("shell", {"script": "head -c 2000000 /dev/zero > big.bin"}),
        ]
        call = ("publish", {"path": "big.bin"})
    return before, call


@pytest.mark.parametrize("case", [_param(case) for case in CASES])
async def test_a_fault_at_a_tool_call_is_told_or_recovered(harness: Harness, case: Case) -> None:
    site = cast(FaultyWorker, harness.worker)
    before, call = row_calls(case)
    model = Model(before, call, lambda: arm(site.faults, case))
    try:
        walked = await walk(harness, model, site.faults)
    finally:
        await cancel_jobs(harness)
    judge(case, walked, site.faults)
    if case.tool.name == "shell" and walked.outcome == "recovered":
        assert effects(harness) == ["once"]  # reconciled, never submitted again


async def cancel_jobs(harness: Harness) -> None:
    """End the jobs a row left running, so that none outlives its test."""
    context = harness.context()
    async with WorkerClient(harness.worker.url, TOKEN) as client:
        for job in (await client.jobs(context)).jobs:
            if job.state not in TERMINAL_STATES:
                await client.cancel(job.execution_id, project_id=PROJECT)


def effects(harness: Harness) -> list[str]:
    found = list(harness.tmp.glob("worker/**/effects.txt"))
    assert len(found) == 1, found
    return found[0].read_text().splitlines()


def test_the_table_covers_every_tool_and_fault() -> None:
    """Every (tool, fault) pair is a row or says why it is none; known failures are
    rows."""
    pairs = {(t.name, f) for t in TOOLS for f in FAULTS}
    rows = {(c.tool.name, c.fault) for c in CASES}
    assert rows | set(NOT_APPLICABLE) == pairs
    assert not rows & set(NOT_APPLICABLE)
    assert set(KNOWN) <= rows, set(KNOWN) - rows


# --- The unguarded points: hooks at the run's start and end, the claim, confinement ----


class HookFailed(Violation):
    """A worker blip in a hook outside a tool call ended the run for good."""


class AnswerAskedAgain(Violation):
    """The run stopped after the model's answer, and its Resume asked for it again."""


@dataclass(frozen=True)
class Point:
    """A point outside any tool call where the harness reads the worker."""

    name: str
    route: str
    known: Known | None


POINTS: Final = (
    # The job-notice hook at the run's start and at its final answer (O/loop.py:500-502,
    # 616). A blip there fails the run (phase ``harness``, transient); recovery (D100)
    # then resumes the tree. At the start nothing was done yet; at the closing point the
    # Resume asks the model again for the answer it had given.
    Point("start", "GET /v1/jobs", None),
    Point(
        "closing",
        "GET /v1/jobs",
        Known(
            "V16",
            AnswerAskedAgain,
            "the closing notice hook is unguarded: the run "
            "fails after its answer and the Resume asks the model again",
        ),
    ),
)


@pytest.mark.parametrize(
    "point",
    [
        pytest.param(
            p,
            id=p.name,
            marks=[
                pytest.mark.xfail(
                    strict=True, raises=p.known.raises, reason=f"{p.known.audit}: {p.known.reason}"
                )
            ]
            if p.known is not None
            else [],
        )
        for p in POINTS
    ],
)
async def test_a_blip_at_a_hook_outside_a_call_does_not_end_the_run(
    harness: Harness, point: Point
) -> None:
    """V16's points, walked: the run goes on, or recovery resumes it to its answer."""
    site = cast(FaultyWorker, harness.worker)
    rule = Rule("server_error", (point.route,))
    if point.name == "start":
        site.faults.arm(rule)
    model = Model([], None, lambda: site.faults.arm(rule) if point.name == "closing" else None)
    walked = await walk(harness, model, site.faults)
    assert rule.met, "the fault never reached the hook"
    if walked.outcome in ("declined", "blocked"):
        raise HookFailed(str(walked))
    # The answer the model gave stands: it is not asked for, and paid for, again.
    if len(model.seen) != 1:
        raise AnswerAskedAgain(f"{walked}; model calls: {len(model.seen)}")


class ClaimLost(Violation):
    """One failed renewal inside the worker's claim ended the root."""


@pytest.mark.xfail(
    strict=True,
    raises=ClaimLost,
    reason="V22: any failed renewal cancels the root, though the worker's claim lasts 120 s",
)
async def test_one_failed_claim_renewal_inside_the_claim_keeps_the_root() -> None:
    """The in-process worker's environments are unmanaged, so their claims never renew:
    the claim's own scripted client stands in for the worker here."""
    clock = FakeClock()
    client = ClaimClient(clock)
    entered, done = asyncio.Event(), asyncio.Event()

    async def root() -> None:
        async with environment_claim(client, CLAIM_CONTEXT, "root", clock=clock):
            entered.set()
            await done.wait()

    task = asyncio.create_task(root())
    await entered.wait()
    await eventually(lambda: clock.sleepers == 1)
    client.broken = True  # one renewal, 30 s into a 120 s claim, meets a blip
    clock.advance(CLAIM_SECONDS / 4)
    await eventually(lambda: len(client.calls) >= 2)
    client.broken = False
    await eventually(lambda: task.done() or clock.sleepers == 1)
    if task.done():
        with contextlib.suppress(BaseException):
            await task
        raise ClaimLost(str(task.exception()) if not task.cancelled() else "cancelled")
    clock.advance(CLAIM_SECONDS / 4)
    await eventually(lambda: len(client.calls) == 3)
    done.set()
    await task


class Diverges(Violation):
    """The same fault has another outcome confined than unconfined."""


DIVERGES: Final = Known("V10", Diverges, "permanent unconfined, bridge_loss (transient) confined")


@pytest.mark.parametrize(
    ("kind", "cause"),
    [
        ("worker_unknown_effect", "blip"),
        ("job_post_submit", "blip"),
        pytest.param(
            "job_post_submit",
            "store",
            marks=pytest.mark.xfail(strict=True, raises=Diverges, reason=f"V10: {DIVERGES.reason}"),
        ),
        ("provision_post_submit", "blip"),
        pytest.param(
            "provision_post_submit",
            "store",
            marks=pytest.mark.xfail(strict=True, raises=Diverges, reason=f"V10: {DIVERGES.reason}"),
        ),
    ],
)
def test_an_unknown_effect_is_classified_alike_confined_and_unconfined(
    kind: str, cause: str
) -> None:
    """An adapter raises ``UnknownEffectError`` from what failed after the submission:
    a worker blip, or the harness's own origin write (O/jobs.py:656-662). The confined
    host answers it with an ``unavailable`` reply holding its text
    (O/confinement/transport.py:324-326), which the runtime raises as a
    ``TransportError`` of that kind (:296); only its text crosses."""
    failed = (
        WorkerAPIError(ErrorKind.UNAVAILABLE, "cannot reach the worker")
        if cause == "blip"
        else ValueError("the origin record could not be written")
    )
    try:
        raise UnknownEffectError(kind, "the adapter's fixed text") from failed
    except UnknownEffectError as error:
        unconfined = error
    confined = TransportError("unavailable", str(unconfined)[:512])
    transient = classify(unconfined) is not None, classify(confined) is not None
    if transient[0] != transient[1]:
        raise Diverges(f"unconfined {classify(unconfined)}, confined {classify(confined)}")


async def test_a_job_whose_status_read_fails_after_submission_is_recovered(
    harness: Harness,
) -> None:
    """The fault at start_job's second request (O/jobs.py:656-662), walked unconfined:
    ``classify`` follows the error's cause, a worker blip, so the run is transient."""
    site = cast(FaultyWorker, harness.worker)
    rule = Rule("server_error", ("GET /v1/jobs/",))
    model = Model([], ("start_job", BY_NAME["start_job"].arguments), lambda: site.faults.arm(rule))
    walked = await walk(harness, model, site.faults)
    assert rule.met
    judge(Case(BY_NAME["start_job"], "server_error", None), walked, site.faults)


# --- Job endings as the agent hears them ---------------------------------------------
# The worker's side of each ending (final state, attempts, written files, the watchdog)
# is walked in packages/worker/tests/test_path_job_endings.py; these rows walk what the
# orchestrator tells the agent of it, against a real worker and real job stores.

JOB_CONTEXT: Final = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


@dataclass
class Jobs:
    client: WorkerClient
    state: JobState
    clock: Clock

    def tools(self, run: str, *, wake: Callable[[], Any] | None = None) -> JobTools:
        """A run's job tools; without ``wake``, a delegated run's, which no user wakes
        (O/harness.py:2131)."""
        return JobTools(
            self.client,
            JOB_CONTEXT,
            run_id=run,
            state=self.state,
            poll_seconds=0.05,
            clock=self.clock,
            wake=wake,
        )

    async def start(self, tools: JobTools, script: str, operation: str) -> str:
        text, _ = await tools.start(
            {"script": script, "purpose": "a job the row needs", "expected_progress": "ends"},
            operation,
        )
        return str(json.loads(text)["job_id"])

    async def ended(self, job: str) -> Execution:
        async def terminal() -> Execution | None:
            execution = await self.client.get(job, project_id=JOB_CONTEXT.project_id)
            return execution if execution.state in TERMINAL_STATES else None

        return await eventually(terminal, timeout=30)


@pytest_asyncio.fixture
async def jobs(worker_url: str, tmp_path: Path) -> AsyncIterator[Jobs]:
    """Job tools on a real worker, with the project's control store holding the session
    (its run tree, leases and job records)."""
    projects = ProjectStore(tmp_path / "state")
    projects.create(JOB_CONTEXT.project_id, "minimal", POLICY)
    projects.open_session(JOB_CONTEXT.project_id, JOB_CONTEXT.session_id, dedicated=False)
    state = JobState(tmp_path / "state", control=projects.control)
    try:
        async with WorkerClient(worker_url, ARTIFACT_TOKEN) as client:
            yield Jobs(client, state, SYSTEM_CLOCK)
    finally:
        state.close()
        projects.close()


class NotToldWhoCancelled(Violation):
    """A job the user cancelled reaches the agent as a bare ``cancelled``."""


@pytest.mark.xfail(
    strict=True,
    raises=NotToldWhoCancelled,
    reason="V11: a cancel from the workspace's Jobs panel reads like the agent's own",
)
async def test_a_users_cancel_from_the_workspace_is_told_as_the_users(jobs: Jobs) -> None:
    tools = jobs.tools("run-1")
    job = await jobs.start(tools, "sleep 30", "op-cancelled")
    # The workspace's Jobs panel cancels through the run's job tools, under its own
    # operation (WS/jobs.py:71-75).
    await tools.cancel({"job_id": job}, "workspace")
    await jobs.ended(job)
    (notice,) = await jobs.tools("run-2").notices()
    assert notice["state"] == "cancelled"
    if "user" not in str(notice.get("cancelled_by", "")):
        raise NotToldWhoCancelled(str(notice))


class NotToldSample(Violation):
    """Output shown in part does not say it is a sample."""


@pytest.mark.xfail(
    strict=True,
    raises=NotToldSample,
    reason="V18: job_status clips output to head and tail with no sample note",
)
async def test_a_jobs_output_shown_in_part_says_it_is_a_sample(jobs: Jobs) -> None:
    tools = jobs.tools("run-1")
    job = await jobs.start(tools, "python3 -c \"print('x' * 20000)\"", "op-long-output")
    await jobs.ended(job)
    text, _ = await tools.status({"job_id": job}, "op-status")
    shown = json.loads(text)
    assert "output omitted" in shown["stdout"]  # the job view shows a part
    if "sample" not in shown:  # what a shell result says of a part (O/tools.py:2112)
        raise NotToldSample(sorted(shown))


WAIT_BOUND: Final = 3600.0
"""The longest a wait may last without a turn of its own: the audit's suggested default
for the open decision on wait_jobs' bound (its S10)."""


class Unbounded(Violation):
    """A delegated run's wait outlasted any bound."""


@pytest.mark.xfail(
    strict=True,
    raises=Unbounded,
    reason="V14 (decision: the bound's length; asserted as the auditor's 1 h): a child's "
    "wait_jobs has no bound of its own and no user wake",
)
async def test_a_delegated_runs_wait_for_a_job_has_a_bound(worker_url: str, tmp_path: Path) -> None:
    clock = FakeClock()
    state = JobState(tmp_path / "state")
    try:
        async with WorkerClient(worker_url, ARTIFACT_TOKEN) as client:
            found = Jobs(client, state, SYSTEM_CLOCK)
            job = await found.start(found.tools("run-0"), "sleep 30", "op-forever")
            child = Jobs(client, state, clock).tools("child-run")  # no wake: a child's
            waiting = asyncio.create_task(child.wait({"job_ids": [job]}, "op-wait"))
            try:
                for _ in range(int(2 * WAIT_BOUND / 60)):
                    await eventually(lambda: clock.sleepers >= 1 or waiting.done())
                    if waiting.done():
                        break
                    clock.advance(60)
                if not waiting.done():
                    raise Unbounded(f"still waiting after {2 * WAIT_BOUND:g} s")
                text, _ = waiting.result()
                assert json.loads(text)["reason"] not in ("ended", "job_lost"), text
            finally:
                waiting.cancel()
                with contextlib.suppress(BaseException):
                    await waiting
                await client.cancel(job, project_id=JOB_CONTEXT.project_id)
    finally:
        state.close()


class WrongCount(Violation):
    """The restart notice's count contradicts the worker's own bound."""


async def test_a_restart_notice_counts_attempts_as_the_worker_does(jobs: Jobs) -> None:
    """A job whose second and third attempts were lost before their commands started:
    the worker has used two of its three restarts (only attempts whose command was sent
    count, ``JobAttempt.sent``), and starts a fifth attempt, whose notice says the command
    was sent to run twice of at most four times."""
    tools = jobs.tools("run-1")
    job = await jobs.start(tools, "true", "op-counted")
    execution = await jobs.ended(job)
    assert execution.job is not None
    now = datetime.now(UTC)

    def lost(number: int, started: bool) -> JobAttempt:
        return JobAttempt(
            attempt=number,
            attempt_id=f"att-{number:024x}",
            started_at=now if started else None,
            lost_at=now,
            sent=started,
            reason="the sandbox executor incarnation stopped during this attempt",
            stdout_path=f".jobs/{job}/stdout.log",
            stderr_path=f".jobs/{job}/stderr.log",
        )

    attempts = (lost(1, True), lost(2, False), lost(3, False), lost(4, True))
    restarting = execution.model_copy(
        update={
            "state": ExecutionState.QUEUED,
            "job": execution.job.model_copy(
                update={"restart": True, "attempt": 5, "attempts": attempts}
            ),
        }
    )
    assert sum(a.started_at is not None for a in attempts) - 1 < MAX_JOB_RESTARTS
    said = str(lost_notice(restarting, attempts[-1])["restart"])
    counted = re.search(r"sent to run (\d+) of at most (\d+) times", said)
    assert counted is not None, said
    runs = sum(a.started_at is not None for a in attempts)
    if int(counted[1]) != runs or int(counted[2]) != MAX_JOB_RESTARTS + 1:
        raise WrongCount(said)


class RecordedLater(Violation):
    """A job's file is recorded with bytes that changed after the job ended."""


@pytest.mark.xfail(
    strict=True,
    raises=RecordedLater,
    reason="V3: a job's files are read when its notice is made, not when it ended",
)
async def test_a_jobs_files_are_recorded_with_the_bytes_it_wrote(recording: Recording) -> None:
    client = recording.toolbox.client
    tools = JobTools(client, ARTIFACT_CONTEXT, run_id="run1")
    tools.recorder = recording.recorder
    await tools.start(
        {"script": "echo job-bytes > result.txt", "purpose": "write", "expected_progress": "-"},
        "op-job",
    )
    (job,) = (await client.jobs(ARTIFACT_CONTEXT, limit=10)).jobs

    async def ended() -> bool:
        execution = await client.get(job.execution_id, project_id="p1")
        return execution.state in TERMINAL_STATES

    await eventually(ended, timeout=30)
    # Someone else changes the file after the job ended, before the run hears of it.
    await client.write_file(
        WriteFileRequest(
            context=ARTIFACT_CONTEXT,
            path="result.txt",
            content_b64=base64.b64encode(b"edited later\n").decode(),
        )
    )
    (notice,) = await tools.notices()
    assert notice["recorded_files"]
    recorded = recording.heads()["sessions/s1/result.txt"].reference.sha256
    if recorded != hashlib.sha256(b"job-bytes\n").hexdigest():
        raise RecordedLater(f"recorded {recorded}, the job wrote job-bytes")


# --- Notices in a run tree --------------------------------------------------------------


class Misrouted(Violation):
    """A job's end is not told to the run responsible for it, or is told twice."""


@pytest.mark.xfail(
    strict=True,
    raises=Misrouted,
    reason="V15: an ended child's job notice goes to whichever run of the session polls "
    "first, a sibling included, and is acknowledged for the whole session",
)
async def test_an_ended_childs_job_is_told_to_its_parent_once(jobs: Jobs, tmp_path: Path) -> None:
    """A parent and two children share the session's environment; one child starts a
    job and ends; the job ends while the parent and the other child are active."""
    parent, child, sibling = (
        f"20261127T120000000000Z-{name}" for name in ("parent", "child", "sibling")
    )
    works = WorkRecords(jobs.state.control)
    works.admit(JOB_CONTEXT.project_id, JOB_CONTEXT.session_id, parent)
    for run in (child, sibling):
        works.admit(JOB_CONTEXT.project_id, JOB_CONTEXT.session_id, run, parent=parent)
    for run in (parent, child, sibling):
        jobs.state.heartbeat(JOB_CONTEXT, run)
    job = await jobs.start(jobs.tools(child), "true", "op-child-job")
    jobs.state.end_run(JOB_CONTEXT, child)  # the child ends; its job outlives it
    await jobs.ended(job)
    told: dict[str, list[str]] = {}
    # Each run polls at its next safe point and acknowledges what it delivered, as the
    # loop does once the run's record keeps it (O/jobs.py:1066-1069).
    for run in (sibling, parent, sibling, parent):
        delivered = [str(n["notice_id"]) for n in await jobs.tools(run).notices()]
        jobs.state.acknowledge(JOB_CONTEXT, run, delivered)
        told.setdefault(run, []).extend(delivered)
    if told.get(parent) != [job] or told.get(sibling):
        raise Misrouted(str(told))


# --- An environment replaced between two calls -----------------------------------------


class NotToldReplaced(Violation):
    """The next result after a replacement does not say the workspace and packages went."""


def replace_environment(harness: Harness) -> None:
    """Replace the session's sandbox as a recreation does: its executor, kernels and
    disk go; the next call meets a new one. The executor is this test's own child."""
    site = cast(FaultyWorker, harness.worker)
    environment = harness.context().environment_id
    connection = site.manager._connections.records[environment]
    assert connection.channel is not None
    process = connection.channel._process
    assert process is not None
    process.kill()
    for disk in harness.tmp.glob(f"worker/**/{environment}"):
        if disk.is_dir():
            shutil.rmtree(disk)


@pytest.mark.xfail(
    strict=True,
    raises=NotToldReplaced,
    reason="V8: a replaced environment is announced only as `the kernel restarted`",
)
async def test_a_replaced_environment_is_told_as_more_than_a_kernel_restart(
    harness: Harness,
) -> None:
    async def replaced() -> None:
        replace_environment(harness)

    record = await harness.run(
        "run-replaced",
        [
            Reply(calls=[call("python", code="x = 1\nopen('kept.txt', 'w').write('k')")]),
            Reply(calls=[call("python", code="print(1)")], before=replaced),
            Reply("Done."),
        ],
    )
    results = [body(str(m["content"])) for m in record.messages if m["role"] == "tool"]
    after = results[-1].lower()
    assert "state_lost" in after, after  # the kernel's restart is told today
    if not ("packages" in after and ("files" in after or "workspace" in after)):
        raise NotToldReplaced(after[:600])


@dataclass(frozen=True)
class Spelling:
    """One way a run may name a file, and whether it reaches the file today."""

    tool: str
    arguments: Json
    known: Known | None = None


SPELLED: Final = ("notes.txt", "shared/data.txt", "$PROJECT_SHARED/data.txt")
"""The spellings that reach the two files, one of which a not_found should name."""


def _standalone(name: str) -> str:
    """``name`` as a path of its own, not the end of a longer one."""
    return r"(?<![\w/$])" + re.escape(name)


NOT_NAMED: Final = Known("V20", Violation, "not_found names none of the file's other spellings")
SPELLINGS: Final = (
    Spelling("read_file", {"path": "notes.txt"}),
    Spelling(
        "read_file",
        {"path": "./notes.txt"},
        Known("V20", Violation, "`./x` is refused for its '.' segment, naming no other spelling"),
    ),
    Spelling("read_file", {"path": f"sessions/{SESSION}/notes.txt"}, NOT_NAMED),
    Spelling("read_file", {"path": "shared/data.txt"}),
    Spelling("shell", {"script": 'cat "$PROJECT_SHARED/data.txt"'}),
    Spelling(
        "shell",
        {"script": "cat shared/data.txt"},
        Known("V20", Violation, "a relative shared/ in code is the session's own folder"),
    ),
)


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param(
            s,
            id=f"{s.tool}:{next(iter(s.arguments.values()))}",
            marks=[
                pytest.mark.xfail(
                    strict=True,
                    raises=s.known.raises,
                    reason=f"{s.known.audit}: {s.known.reason}",
                )
            ]
            if s.known is not None
            else [],
        )
        for s in SPELLINGS
    ],
)
async def test_each_spelling_of_a_path_resolves_or_names_the_others(
    harness: Harness, spelling: Spelling
) -> None:
    record = await harness.run(
        "run-paths",
        [
            Reply(calls=[call("write_file", path="notes.txt", content="hello notes\n")]),
            Reply(
                calls=[
                    call(
                        "write_file",
                        path="shared/data.txt",
                        content="hello shared\n",
                        create_only=True,
                    )
                ]
            ),
            Reply(calls=[call(spelling.tool, **spelling.arguments)]),
            Reply("Done."),
        ],
    )
    result = [body(str(m["content"])) for m in record.messages if m["role"] == "tool"][-1]
    if "hello" in result:
        return  # resolved
    given = json.dumps(spelling.arguments)
    others = [name for name in SPELLED if not re.search(_standalone(name), given)]
    said = json.dumps(errors_in(parsed(result)))
    if not any(re.search(_standalone(name), said) for name in others):
        raise Violation(f"neither resolved nor named the others: {result[:400]}")


# --- The tools the in-process worker lacks: admit_hosts and provision -------------------
# The in-process worker has no managed environments (admissions) or provisioner, so
# these rows walk the toolbox and the real worker client against a scripted endpoint of
# the worker's routes, with the same channel faults; the outcome's recovery is the
# automatic recovery's own admission rule (``tool_reconcilable``).

LACKED: Final = ("admit_hosts", "provision")
LACKED_FAULTS: Final[tuple[Fault, ...]] = (
    "refused",
    "server_error",
    "reset",
    "reply_lost",
    "environment_gone",
)
LACKED_KNOWN: Final[dict[tuple[str, Fault], Known]] = {
    **{(name, "refused"): Known("V5", NotTold, NO_RETRY_FIELD) for name in LACKED},
    **{
        (name, fault): Known(
            "V2",
            RecoveryDeclined,
            "a POST tool's lost reply is an unknown effect no "
            "adapter reconciles, so recovery declines",
        )
        for name in LACKED
        for fault in ("server_error", "reset", "reply_lost", "environment_gone")
    },
}
LACKED_ROUTES: Final = {
    "admit_hosts": f"POST /v1/environments/{ARTIFACT_CONTEXT.environment_id}/admissions",
    "provision": "POST /v1/provisioning",
}
LACKED_CALLS: Final[dict[str, Json]] = {
    "admit_hosts": {"hosts": ["eprint.iacr.org"]},
    "provision": {"purpose": "a package the row needs", "conda": ["fpylll"]},
}


def _admitted(environment_id: str) -> str:
    now = datetime.now(UTC)
    return AdmissionResult(
        decisions=(
            AdmissionDecision(host="eprint.iacr.org", port=443, admitted=True, reason="admitted"),
        ),
        environment=EnvironmentInfo(
            environment_id=environment_id,
            project_id=ARTIFACT_CONTEXT.project_id,
            profile="minimal",
            image="sandbox-science/minimal:test",
            sandbox="ss-env1-00000000",
            policy=POLICY,
            state=EnvironmentState.READY,
            created_at=now,
            updated_at=now,
        ),
    ).model_dump_json()


@pytest_asyncio.fixture
async def endpoint() -> AsyncIterator[tuple[str, Faults]]:
    """The worker's admission and provisioning routes, scripted, behind ``Faults``."""
    faults = Faults()

    @web.middleware
    async def channel(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        rule = next((r for r in faults.rules if r.matches(request)), None)
        if rule is None:
            return await handler(request)
        rule.times -= 1
        rule.met.append(f"{request.method} {request.path}")
        match rule.fault:
            case "refused":
                return _refusal(ErrorKind.CONFLICT, "synthetic: the worker refused the call", 409)
            case "server_error":
                return _refusal(ErrorKind.UNAVAILABLE, "synthetic: the worker failed", 503)
            case "environment_gone":
                return _refusal(ErrorKind.UNAVAILABLE, GONE, 503)
            case "reply_lost":
                await handler(request)
                return _reset(request)
            case _:
                return _reset(request)

    async def admit(request: web.Request) -> web.Response:
        return web.Response(
            text=_admitted(request.match_info["environment"]), content_type="application/json"
        )

    async def provision(request: web.Request) -> web.Response:
        asked = ProvisionRequest.model_validate_json(await request.read())
        done = provision_operation(asked, state="finished", outcome="already_satisfied", seq=1)
        return web.Response(
            text=done.model_dump_json(), content_type="application/json", status=201
        )

    app = fake_worker_app(middlewares=[channel])
    app.router.add_post("/v1/environments/{environment}/admissions", admit)
    app.router.add_post("/v1/provisioning", provision)
    async with TestServer(app) as server:
        yield str(server.make_url("/")).rstrip("/"), faults


@pytest.mark.parametrize(
    ("name", "fault"),
    [
        pytest.param(
            name,
            fault,
            id=f"{name}-{fault}",
            marks=[
                pytest.mark.xfail(
                    strict=True,
                    raises=known.raises,
                    reason=f"{known.audit}: {known.reason}",
                )
            ]
            if (known := LACKED_KNOWN.get((name, fault))) is not None
            else [],
        )
        for name in LACKED
        for fault in LACKED_FAULTS
    ],
)
async def test_a_tool_the_local_worker_lacks(
    endpoint: tuple[str, Faults], name: str, fault: Fault
) -> None:
    url, faults = endpoint
    faults.arm(Rule(fault, (LACKED_ROUTES[name],)))
    async with WorkerClient(url, ARTIFACT_TOKEN) as client:
        toolbox = Toolbox(client, ARTIFACT_CONTEXT, features=("admission", "provisioning"))
        call_ = ToolCall(FAULTED, name, json.dumps(LACKED_CALLS[name]))
        unknown: UnknownEffectError | None = None
        outcome: ToolOutcome | None = None
        try:
            outcome = await toolbox.call(call_, f"op-{name}")
        except UnknownEffectError as error:
            unknown = error
    assert faults.met(), "the fault never reached the call"
    if unknown is not None:
        # The run stops on it (O/loop.py:1449-1460) as a transient failure...
        assert classify(unknown) == "worker_loss"
        # ...and automatic recovery admits it only if an adapter reconciles it.
        with sqlite3.connect(":memory:") as db:
            admitted = tool_reconcilable(db, "p1", "s1", "run", "op", name, call_.arguments)
        if not admitted:
            raise RecoveryDeclined(f"{name}'s unknown effect is not reconcilable") from unknown
        return
    assert outcome is not None
    errors = errors_in(parsed(outcome.text))
    assert errors, f"the call succeeded as if the fault had not been: {outcome.text}"
    if not all(told(error) for error in errors):
        raise NotTold(outcome.text)
