# SPDX-License-Identifier: MIT
"""The agent's ``edit_file``, ``glob`` and ``grep`` tools (tools spec, Files 7 to 10):
their schemas and effect classes in each role, how their results reach the model within
its budget, how their failures map, and an older worker's answer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import (
    EditFileRequest,
    EditFileResult,
    ErrorKind,
    ExecutionContext,
    GlobEntry,
    GlobListing,
    GlobRequest,
    GrepCount,
    GrepRequest,
    GrepResult,
)
from sandbox_science.contracts.worker import PROTOCOL_HEADER, PROTOCOL_VERSION
from sandbox_science.orchestrator.delegation.effects import SESSION_TOOLS, reach
from sandbox_science.orchestrator.delegation.fence import ChildFence
from sandbox_science.orchestrator.effects import UnknownEffectError
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.session_plans import PlanBody, SessionPlans
from sandbox_science.orchestrator.tools import HARNESS_TOOL_NAMES, Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

from .loop_harness import PLAN, USER_PLANS
from .scripted import ToolCall
from .test_capability_assembly import offered
from .test_child_requests import stores as stores

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
SHA = "ab" * 32
WHEN = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


class FakeWorker:
    """The worker client's file tool calls, answered as a test sets them."""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self.answer: object = None

    def _reply(self, request: object) -> Any:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def edit_file(self, request: EditFileRequest) -> EditFileResult:
        return self._reply(request)

    async def glob_files(self, request: GlobRequest) -> GlobListing:
        return self._reply(request)

    async def grep_files(self, request: GrepRequest) -> GrepResult:
        return self._reply(request)


def _toolbox(fake: FakeWorker, *, output_chars: int = 20_000, delegated: bool = False) -> Toolbox:
    return Toolbox(
        cast(WorkerClient, fake), CONTEXT, output_chars=output_chars, delegated=delegated
    )


def _call(toolbox: Toolbox, name: str, **arguments: Any) -> ToolOutcome:
    call = ToolCall(id="c1", name=name, arguments=json.dumps(arguments))
    return asyncio.run(toolbox.call(call, "op1"))


# --- Offers and classes ---------------------------------------------------------------------


@pytest.mark.parametrize("role", ["main", "task", "check"])
def test_every_role_has_the_tools_with_their_effect_classes(role: str) -> None:
    entries = offered(role).entries
    assert entries["edit_file"].effect == entries["write_file"].effect == "effect"
    assert entries["glob"].effect == entries["grep"].effect == entries["read_file"].effect
    assert entries["grep"].effect == "read"
    edit = entries["edit_file"].spec.parameters
    assert edit["required"] == ["path", "old_string", "new_string"]
    assert edit["additionalProperties"] is False
    grep = entries["grep"].spec.parameters
    assert grep["properties"]["mode"]["enum"] == ["files", "count", "content"]
    assert grep["properties"]["max_results"]["maximum"] == 500
    assert grep["properties"]["context_lines"]["maximum"] == 10
    assert entries["glob"].spec.parameters["properties"]["limit"]["maximum"] == 200


def test_the_descriptions_steer_to_the_new_tools() -> None:
    entries = offered("main").entries
    assert "edit_file" in entries["write_file"].spec.description
    shell = entries["shell"].spec.description
    assert "grep and glob" in shell
    assert "edit_file" in shell


def test_an_edit_stays_in_the_session_and_no_connector_takes_the_names() -> None:
    assert "edit_file" in SESSION_TOOLS
    assert reach("edit_file") == "session"
    assert {"edit_file", "glob", "grep"} <= HARNESS_TOOL_NAMES


def test_a_draft_plan_lets_a_childs_edits_and_searches_through(
    stores: tuple[ProjectStore, SessionPlans, ExecutionContext],
) -> None:
    projects, plans, context = stores
    plans.publish(USER_PLANS, "p1", PlanBody.model_validate(PLAN), "Plan.")  # a draft
    fence = ChildFence(plans, projects, context, run_id="child")
    assert asyncio.run(fence.gate_call("edit_file", "effect")) is None
    assert asyncio.run(fence.gate_call("write_file", "effect")) is None
    for name in ("glob", "grep"):
        assert asyncio.run(fence.gate_call(name, "read")) is None


def test_a_childs_edit_and_search_go_where_its_write_and_read_go() -> None:
    """DA-19's write area is not built: a child's ``edit_file`` reaches the worker in the
    session's context, as its ``write_file`` does, and the worker's path rules bind both."""
    fake = FakeWorker()
    fake.answer = EditFileResult(sha256=SHA, size=3, replacements=1)
    toolbox = _toolbox(fake, delegated=True)
    _call(toolbox, "edit_file", path="work/d1/a.md", old_string="a", new_string="b")
    fake.answer = GrepResult(path=".", pattern="x", mode="files", scanned_files=0, scanned_bytes=0)
    _call(toolbox, "grep", pattern="x")
    edit, grep = fake.requests
    assert isinstance(edit, EditFileRequest)
    assert isinstance(grep, GrepRequest)
    assert edit.context == grep.context == CONTEXT


# --- Results --------------------------------------------------------------------------------


def test_an_edit_reports_its_hash_and_is_recorded_as_a_write() -> None:
    fake = FakeWorker()
    fake.answer = EditFileResult(sha256=SHA, size=12, replacements=2)
    outcome = _call(
        _toolbox(fake),
        "edit_file",
        path="notes.md",
        old_string="a",
        new_string="b",
        replace_all=True,
        expected_sha256="cd" * 32,
    )
    assert json.loads(outcome.text) == {
        "edited": "notes.md",
        "sha256": SHA,
        "size": 12,
        "replacements": 2,
    }
    assert outcome.record["edit"]["replacements"] == 2
    assert outcome.record["effect"] == "effect"
    assert outcome.written is not None
    assert outcome.written.paths == ("notes.md",)
    (request,) = fake.requests
    assert isinstance(request, EditFileRequest)
    assert (request.replace_all, request.expected_sha256) == (True, "cd" * 32)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"path": "a", "old_string": "x", "new_string": "x"}, "the same"),
        ({"path": "a", "old_string": "", "new_string": "x"}, "old_string"),
        ({"path": "/etc/passwd", "old_string": "x", "new_string": "y"}, "path"),
        ({"path": "a", "old_string": "\ud800", "new_string": "y"}, "unpaired surrogate"),
        ({"path": "a", "old_string": 3, "new_string": "y"}, "must be a string"),
    ],
)
def test_a_bad_edit_is_refused_before_the_worker(arguments: dict[str, Any], message: str) -> None:
    fake = FakeWorker()
    outcome = _call(_toolbox(fake), "edit_file", **arguments)
    error = json.loads(outcome.text)["error"]
    assert error["kind"] == "invalid_request"
    assert message in error["message"]
    assert fake.requests == []


def test_the_workers_refusals_reach_the_model() -> None:
    fake = FakeWorker()
    for kind, message in (
        (ErrorKind.NOT_FOUND, "not_found: old_string does not occur in 'a.md'"),
        (ErrorKind.INVALID_REQUEST, "ambiguous: old_string occurs 3 times in 'a.md'"),
        (ErrorKind.CONFLICT, "'shared/a.md' changed: its SHA-256 is " + SHA),
        (ErrorKind.UNSUPPORTED, "this worker predates edit_file, glob and grep"),
    ):
        fake.answer = WorkerAPIError(kind, message, 400)
        outcome = _call(_toolbox(fake), "edit_file", path="a.md", old_string="a", new_string="b")
        assert json.loads(outcome.text)["error"] == {"kind": kind.value, "message": message}


def test_an_edit_of_unknown_outcome_stops_the_attempt() -> None:
    fake = FakeWorker()
    fake.answer = WorkerAPIError(ErrorKind.INTERNAL, "proxy", 502, uncertain=True)
    with pytest.raises(UnknownEffectError):
        _call(_toolbox(fake), "edit_file", path="a.md", old_string="a", new_string="b")


def _listing(count: int, *, truncated: bool = False, stopped: Any = None) -> GlobListing:
    return GlobListing(
        path=".",
        pattern="**/*.py",
        entries=tuple(
            GlobEntry(path=f"src/module_{n:03d}.py", type="file", size=n, modified_at=WHEN)
            for n in range(count)
        ),
        truncated=truncated,
        stopped=stopped,
        scanned=count,
        skipped=(".executor",),
    )


def test_a_glob_fits_the_result_and_says_what_it_left_out() -> None:
    fake = FakeWorker()
    fake.answer = _listing(200, truncated=True, stopped="results")
    outcome = _call(_toolbox(fake, output_chars=4000), "glob", pattern="**/*.py")
    assert len(outcome.text) <= 4000
    shown = json.loads(outcome.text)
    assert shown["truncated"] is True
    assert 0 < len(shown["entries"]) < 200
    assert shown["entries"][0] == {
        "path": "src/module_000.py",
        "type": "file",
        "size": 0,
        "modified": "2026-10-24T12:00:00+00:00",
    }
    assert "more matched than the limit" in shown["note"]
    assert "shown in part" in shown["note"]
    assert ".executor" in shown["note"]
    assert outcome.record["glob"]["shown"] == len(shown["entries"])
    assert outcome.record["effect"] == "read"
    whole = _call(_toolbox(fake), "glob", pattern="**/*.py")
    assert len(json.loads(whole.text)["entries"]) == 200


def test_grep_results_fit_the_result_in_each_mode() -> None:
    fake = FakeWorker()
    lines = tuple(f"src/a.py:{n}: {'x' * 300}" for n in range(1, 501))
    fake.answer = GrepResult(
        path=".",
        pattern="x",
        mode="content",
        lines=lines,
        truncated=True,
        stopped="results",
        scanned_files=1,
        scanned_bytes=150_000,
        skipped_binary=2,
    )
    outcome = _call(_toolbox(fake), "grep", pattern="x", mode="content", max_results=500)
    assert len(outcome.text) <= 20_000
    shown = json.loads(outcome.text)
    assert shown["lines"] == list(lines[: len(shown["lines"])])
    assert shown["truncated"] is True
    assert shown["skipped_files"] == {"binary": 2}
    assert "shown in part" in shown["note"]
    (request,) = fake.requests
    assert isinstance(request, GrepRequest)
    assert (request.mode, request.max_results) == ("content", 500)
    fake.answer = GrepResult(
        path="src",
        pattern="x",
        mode="count",
        counts=(GrepCount(path="src/a.py", count=4),),
        scanned_files=1,
        scanned_bytes=10,
    )
    counted = json.loads(_call(_toolbox(fake), "grep", pattern="x", mode="count").text)
    assert counted["counts"] == [{"path": "src/a.py", "count": 4}]
    assert counted["truncated"] is False
    assert "note" not in counted
    fake.answer = GrepResult(
        path=".", pattern="nothing", mode="files", scanned_files=3, scanned_bytes=10
    )
    empty = json.loads(_call(_toolbox(fake), "grep", pattern="nothing").text)
    assert empty["files"] == []
    assert empty["note"] == "no match in the files searched"


@pytest.mark.parametrize(
    ("arguments", "where"),
    [
        ({"pattern": "x", "mode": "lines"}, "mode"),
        ({"pattern": "x", "context_lines": 11}, "context_lines"),
        ({"pattern": "x", "max_results": 501}, "max_results"),
        ({"pattern": "x", "glob": "../*.py"}, "glob"),
        ({"pattern": "x", "path": "../other"}, "path"),
        ({"pattern": ""}, "pattern"),
    ],
)
def test_a_bad_grep_is_refused_before_the_worker(arguments: dict[str, Any], where: str) -> None:
    fake = FakeWorker()
    error = json.loads(_call(_toolbox(fake), "grep", **arguments).text)["error"]
    assert error["kind"] == "invalid_request"
    assert where in error["message"]
    assert fake.requests == []


# --- An older worker ------------------------------------------------------------------------


@pytest_asyncio.fixture
async def older_worker() -> AsyncIterator[str]:
    """A worker of this protocol without the file tools' routes, and one route that
    answers with the worker's own ``not_found``."""

    async def stamp(request: web.Request, response: web.StreamResponse) -> None:
        response.headers[PROTOCOL_HEADER] = str(PROTOCOL_VERSION)

    async def missing(request: web.Request) -> web.Response:
        body = {"error": {"kind": "not_found", "message": "no environment 'env1'"}}
        return web.json_response(body, status=404)

    app = web.Application()
    app.on_response_prepare.append(stamp)
    app.router.add_post("/v1/files/grep", missing)
    async with TestServer(app) as server:
        yield str(server.make_url("/")).rstrip("/")


async def test_an_older_worker_is_unsupported_not_an_unknown_effect(older_worker: str) -> None:
    async with WorkerClient(older_worker, "test-secret-token") as client:
        with pytest.raises(WorkerAPIError) as caught:
            await client.edit_file(
                EditFileRequest(context=CONTEXT, path="a", old_string="x", new_string="y")
            )
        assert caught.value.kind is ErrorKind.UNSUPPORTED
        assert caught.value.uncertain is False
        assert "predates edit_file" in caught.value.message
        with pytest.raises(WorkerAPIError) as listed:
            await client.glob_files(GlobRequest(context=CONTEXT, pattern="*"))
        assert listed.value.kind is ErrorKind.UNSUPPORTED
        # The worker's own not_found stays what it is.
        with pytest.raises(WorkerAPIError) as found:
            await client.grep_files(GrepRequest(context=CONTEXT, pattern="x"))
        assert found.value.kind is ErrorKind.NOT_FOUND
        toolbox = Toolbox(client, CONTEXT)
        call = ToolCall(id="c1", name="glob", arguments=json.dumps({"pattern": "*"}))
        outcome = await toolbox.call(call, "op1")
        assert json.loads(outcome.text)["error"]["kind"] == "unsupported"
