# SPDX-License-Identifier: MIT
"""The provisioning tools (provision.py): what they ask the worker, and what the model reads.

A scripted worker client stands in for the worker; the worker's own tests
(packages/worker/tests/test_provisioner.py) run the operations themselves.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import (
    ExecutionContext,
    PackageChange,
    ProbeResult,
    ProvisioningQueue,
    ProvisionOperation,
    ProvisionRequest,
    ResolvedFile,
)
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.tools import Toolbox, operation_id
from sandbox_science.orchestrator.worker_client import WorkerClient

pytestmark = pytest.mark.asyncio

TOKEN = "k" * 40
CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def operation(request: ProvisionRequest, **fields: Any) -> ProvisionOperation:
    return ProvisionOperation.model_validate(
        {
            "provision_id": "prv-" + "a" * 24,
            "request": request,
            "state": "queued",
            "submitted_at": NOW,
            **fields,
        }
    )


class ScriptedClient(WorkerClient):
    """A worker client whose operations follow a script of states."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        super().__init__("http://127.0.0.1:9", TOKEN)
        self.script = script
        self.asked: list[ProvisionRequest] = []
        self.polled: list[tuple[str, int, float]] = []
        self.cancelled: list[str] = []
        self.request: ProvisionRequest | None = None

    def _next(self, seq: int) -> ProvisionOperation:
        assert self.request is not None
        fields = self.script.pop(0) if self.script else {}
        return operation(self.request, seq=seq, **fields)

    async def provision(self, request: ProvisionRequest) -> tuple[ProvisionOperation, bool]:
        self.asked.append(request)
        self.request = request
        return self._next(1), True

    async def provision_status(
        self,
        provision_id: str,
        *,
        after: int = -1,
        wait: float = 0.0,
        project_id: str | None = None,
    ) -> ProvisionOperation:
        assert project_id == "p1"
        self.polled.append((provision_id, after, wait))
        return self._next(after + 1)

    async def cancel_provision(
        self, provision_id: str, *, project_id: str | None = None
    ) -> ProvisionOperation:
        self.cancelled.append(provision_id)
        return self._next(9)

    async def provisioning_queue(
        self, environment_id: str, *, project_id: str | None = None
    ) -> ProvisioningQueue:
        assert project_id == "p1"
        assert self.request is not None
        waiting = operation(self.request, state="queued", queue_position=1)
        return ProvisioningQueue(
            environment_id=environment_id,
            status="needs_recovery",
            revision=4,
            operations=(waiting,),
        )


FILE = ResolvedFile(
    ecosystem="conda",
    name="fpylll",
    version="0.6.1",
    build="py313h0_0",
    channel="conda-forge",
    subdir="linux-64",
    file="fpylll-0.6.1-py313h0_0.conda",
    sha256="b" * 64,
    license="GPL-2.0-or-later",
    action="escalate",
    rule="packages.conda",
)


def call(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments))


async def test_the_tools_are_offered_where_the_worker_provisions() -> None:
    client = ScriptedClient([])
    try:
        assert "provision" not in {spec.name for spec in Toolbox(client, CONTEXT).specs}
        names = {spec.name for spec in Toolbox(client, CONTEXT, features=("provisioning",)).specs}
        assert {"provision", "provision_status"} <= names
    finally:
        await client.close()


async def test_provision_asks_once_and_reports_a_wait_for_permission() -> None:
    client = ScriptedClient(
        [
            {"state": "resolving"},
            {
                "state": "awaiting_permission",
                "outcome": "awaiting_permission",
                "permission_request": 12,
                "files": (FILE,),
                "planned_revision": 1,
                "next_step": "The user approves it with approve --request 12.",
            },
        ]
    )
    toolbox = Toolbox(client, CONTEXT, features=("provisioning",))
    try:
        arguments = {"purpose": "LLL on the lattice", "conda": ["fpylll"]}
        tool_call = call("provision", arguments)
        outcome = await toolbox.call(tool_call, operation_id("run-1-t0", tool_call))
    finally:
        await client.close()
    [request] = client.asked
    assert (request.conda, request.purpose) == (("fpylll",), "LLL on the lattice")
    assert request.context == CONTEXT
    assert request.operation_id == operation_id("run-1-t0", tool_call)
    shown = json.loads(outcome.text)
    assert (shown["outcome"], shown["permission_request"]) == ("awaiting_permission", 12)
    assert shown["files"] == ["conda-forge::fpylll 0.6.1 py313h0_0 (GPL-2.0-or-later) escalate"]
    assert "approve --request 12" in shown["next"]
    assert "provision_status" in shown["follow"]
    assert shown["revisions"] == {"planned_against": 1}
    assert outcome.record["provision"]["permission_request"] == 12
    assert [after for _, after, _ in client.polled] == [1]


async def test_provision_status_follows_an_operation_to_its_end() -> None:
    probe = ProbeResult(
        capability="gp",
        probe="gp-factor",
        kind="functional",
        status="passed",
        revision=2,
        recorded_at=NOW,
    )
    client = ScriptedClient(
        [
            {"state": "installing"},
            {
                "state": "finished",
                "outcome": "completed",
                "revision_before": 1,
                "revision_after": 2,
                "changes": (
                    PackageChange(ecosystem="conda", name="pari", after="2.15.4"),
                    PackageChange(ecosystem="conda", name="gmp", before="6.2.1", after="6.3.0"),
                ),
                "probes": (probe,),
            },
        ]
    )
    client.request = ProvisionRequest(
        operation_id="op", context=CONTEXT, purpose="factor", capability="gp"
    )
    toolbox = Toolbox(client, CONTEXT, features=("provisioning",))
    try:
        arguments = {"operation": "prv-" + "a" * 24, "wait_seconds": 600}
        outcome = await toolbox.call(call("provision_status", arguments), "op-2")
    finally:
        await client.close()
    shown = json.loads(outcome.text)
    assert (shown["outcome"], shown["state"]) == ("completed", "finished")
    assert shown["revisions"] == {"before": 1, "after": 2}
    assert shown["changes"] == ["+ pari 2.15.4", "gmp 6.2.1 -> 6.3.0"]
    assert shown["probes"] == ["gp-factor (functional) passed at revision 2"]
    assert "follow" not in shown


async def test_provision_status_cancels_and_lists_the_queue() -> None:
    client = ScriptedClient([{"state": "finished", "outcome": "failed", "stage": "queue"}])
    client.request = ProvisionRequest(
        operation_id="op", context=CONTEXT, purpose="x", system=("libgmp-dev",)
    )
    toolbox = Toolbox(client, CONTEXT, features=("provisioning",))
    try:
        cancelled = await toolbox.call(
            call("provision_status", {"operation": "prv-1", "cancel": True}), "op-3"
        )
        queue = await toolbox.call(call("provision_status", {}), "op-4")
    finally:
        await client.close()
    assert client.cancelled == ["prv-1"]
    assert json.loads(cancelled.text)["stopped_at"] == "queue"
    listed = json.loads(queue.text)
    assert (listed["packages"], listed["revision"]) == ("needs_recovery", 4)
    assert listed["operations"][0]["place"] == 1
    assert listed["operations"][0]["asked_for"] == "system:libgmp-dev"


@pytest.mark.parametrize(
    "arguments",
    [
        {"conda": ["fpylll"]},  # no purpose
        {"purpose": "x", "conda": ["--channel=bioconda"]},
        {"purpose": "x", "pip": ["requests"]},  # no pip_reason
        {"purpose": "x"},
        {"purpose": "x", "capability": "gp", "conda": ["pari"]},
    ],
)
async def test_requests_that_are_no_package_never_reach_the_worker(
    arguments: dict[str, Any],
) -> None:
    client = ScriptedClient([])
    toolbox = Toolbox(client, CONTEXT, features=("provisioning",))
    try:
        outcome = await toolbox.call(call("provision", arguments), "op-5")
    finally:
        await client.close()
    assert json.loads(outcome.text)["error"]["kind"] == "invalid_request"
    assert client.asked == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"environment": "../executions"},
        {"environment": 7},
        {"operation": "../../v1/executions/exe-1"},
        {"operation": ["prv-1"]},
    ],
)
async def test_only_identifiers_reach_the_workers_routes(arguments: dict[str, Any]) -> None:
    client = ScriptedClient([])
    toolbox = Toolbox(client, CONTEXT, features=("provisioning",))
    try:
        outcome = await toolbox.call(call("provision_status", arguments), "op-7")
    finally:
        await client.close()
    assert json.loads(outcome.text)["error"]["kind"] == "invalid_request"
    assert (client.polled, client.cancelled) == ([], [])


async def test_a_long_result_is_bounded() -> None:
    files = tuple(
        FILE.model_copy(update={"name": f"package{index}", "file": f"package{index}-1-0.conda"})
        for index in range(500)
    )
    client = ScriptedClient(
        [
            {
                "state": "awaiting_permission",
                "outcome": "awaiting_permission",
                "files": files,
                "notes": tuple("a long note " * 150 for _ in range(30)),
                "next_step": "x" * 2000,
            }
        ]
    )
    toolbox = Toolbox(client, CONTEXT, features=("provisioning",), output_chars=4000)
    try:
        outcome = await toolbox.call(
            call("provision", {"purpose": "x", "capability": "sage"}), "op-6"
        )
    finally:
        await client.close()
    assert len(outcome.text) <= 4000
    shown = json.loads(outcome.text)
    assert shown["files"][-1].startswith("[… ")
    assert len(outcome.record["provision"]["files"]) == 500
    assert len(outcome.record["provision"]["notes"]) == 30


@pytest_asyncio.fixture
async def worker() -> AsyncIterator[tuple[TestServer, list[tuple[str, str, dict[str, str]]]]]:
    seen: list[tuple[str, str, dict[str, str]]] = []
    request = ProvisionRequest(operation_id="op", context=CONTEXT, purpose="x", capability="gp")
    body = operation(request).model_dump_json()
    queue = ProvisioningQueue(environment_id="env1", status="ready").model_dump_json()

    async def answer(http: web.Request) -> web.Response:
        seen.append((http.method, http.path, dict(http.query)))
        if http.path.endswith("/provisioning") and http.method == "GET":
            return web.Response(text=queue, content_type="application/json")
        return web.Response(
            text=body,
            status=201 if http.method == "POST" and http.path == "/v1/provisioning" else 200,
            content_type="application/json",
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", answer)
    server = TestServer(app)
    await server.start_server()
    yield server, seen
    await server.close()


async def test_the_worker_client_names_the_routes_and_their_parameters(
    worker: tuple[TestServer, list[tuple[str, str, dict[str, str]]]],
) -> None:
    server, seen = worker
    request = ProvisionRequest(operation_id="op", context=CONTEXT, purpose="x", capability="gp")
    async with WorkerClient(str(server.make_url("")), TOKEN) as client:
        _, created = await client.provision(request)
        await client.provision_status("prv-1", after=3, wait=90, project_id="p1")
        await client.cancel_provision("prv-1", project_id="p1")
        await client.provisioning_queue("env1", project_id="p1")
    assert created is True
    assert seen == [
        ("POST", "/v1/provisioning", {}),
        ("GET", "/v1/provisioning/prv-1", {"after": "3", "wait": "60", "project_id": "p1"}),
        ("POST", "/v1/provisioning/prv-1/cancel", {"project_id": "p1"}),
        ("GET", "/v1/environments/env1/provisioning", {"project_id": "p1"}),
    ]
