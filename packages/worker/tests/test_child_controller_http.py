# SPDX-License-Identifier: MIT
"""Actual child/store/client/HTTP composition; the guest channel is synthetic."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentState,
    ExecutionState,
    Result,
    StreamOutput,
)
from sandbox_science.orchestrator.child_attempts import (
    AppliedBudget,
    AttemptAuthority,
    ChildAttempts,
    RequestedProfile,
)
from sandbox_science.orchestrator.child_controller import (
    AppliedChildModel,
    ChildConfiguration,
    ChildController,
)
from sandbox_science.orchestrator.child_tools import ChildToolSettings
from sandbox_science.orchestrator.delegation_records import (
    DelegationBrief,
    DelegationRecords,
    DelegationScope,
    ParentAuthority,
    ProfileIntent,
    RequestedLimits,
)
from sandbox_science.orchestrator.documents.errors import DocumentError
from sandbox_science.orchestrator.documents.store import DocumentStore
from sandbox_science.orchestrator.model import ModelTurn, ToolCall
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerClient
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import Backend
from sandbox_science.worker.channel import ExecutorChannel
from sandbox_science.worker.environments import EnvironmentRecords, Environments
from sandbox_science.worker.manager import ExecutionManager, _Environment
from sandbox_science.worker.settings import ClientSettings, token_digest
from sandbox_science.worker.store import Store

# Only the isolated composition fixture inspects its owned SQL/channel state.
# pyright: reportPrivateUsage=false
POOL = "test-secret-child-composition-pool-" + "p" * 32
CONTROL = "test-secret-child-composition-controller-" + "c" * 32
ORDINARY = "test-secret-child-composition-ordinary-" + "o" * 32
INC = "inc-" + "a" * 24
VM = ("synthetic-vm", "synthetic-vm-id")
PROFILE = hashlib.sha256(b"explicit scripted composition profile").hexdigest()


class Guest:
    incarnation = INC
    daemon_token = "test-secret-synthetic-guest"
    closed = False
    detaching = False

    def __init__(self):
        self.hello = {"run_kernel_protocol": 1, "kernel_revision_protocol": 1}
        self.calls = []

    async def request(self, operation, **values):
        self.calls.append((operation, values))
        return {"kernels": []}

    async def detach(self):
        self.detaching = True

    async def close(self):
        self.closed = True


class ScriptedModel:
    def __init__(self, documents, first_name):
        self.documents = documents
        self.first_name = first_name
        self.inputs = []

    async def complete(self, messages, tools):
        raw = (
            self.documents._connection()
            .execute("SELECT record FROM child_attempt_events ORDER BY sequence DESC LIMIT 1")
            .fetchone()[0]
        )
        assert json.loads(raw)["body"]["kind"] == "reserve"
        self.inputs.append((json.loads(json.dumps(messages)), tuple(spec.name for spec in tools)))
        if len(self.inputs) == 1:
            return ModelTurn(
                None,
                (ToolCall("selected-one", self.first_name, '{"code":"synthetic 2"}'),),
                model="scripted-composition",
                output_tokens=12,
            )
        return ModelTurn(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "partial",
                    "summary": "Synthetic transport observation",
                    "results": [],
                    "deviations": [],
                    "open_questions": ["Real guest and provider are outside this fixture"],
                }
            ),
            model="scripted-composition",
            output_tokens=32,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["positive", "unoffered", "ordinary_token", "accepted_lost_ack"])
async def test_child_controller_uses_real_authenticated_selected_http(tmp_path, monkeypatch, mode):
    projects = ProjectStore(tmp_path / "control")
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    opened = projects.open_session("p", "s", dedicated=False)
    original = projects.session_snapshot("p", "s")
    documents = DocumentStore(tmp_path / "control/control.sqlite3")
    records = DelegationRecords(documents, initialize=True)
    parent = ParentAuthority(
        scope=DelegationScope(project_id="p", session_id="s", environment_id=opened.environment_id),
        parent_run_id="parent",
    )
    prepared = records.prepare(
        parent,
        "prepare",
        DelegationBrief(
            task="Exercise only the explicit synthetic selected route",
            context_summary="No real provider or guest execution",
            track=None,
            inputs=(),
            profile=ProfileIntent(
                profile_id="scripted", profile_sha256=PROFILE, model_alias="explicit-scripted"
            ),
            limits=RequestedLimits(turns=4, wall_seconds=30, output_tokens=256),
        ),
    )
    authority = AttemptAuthority(
        scope=parent.scope,
        parent_run_id="parent",
        delegation_id=prepared.delegation_id,
        child_run_id=prepared.child_run_id,
        controller_id="controller",
    )
    journal = ChildAttempts(documents, records, projects, initialize=True)
    settings = ChildToolSettings(("python",), 2.0, 1024, 2.0, 0.001, 32, 1)
    config = ChildConfiguration(
        RequestedProfile(profile_id="scripted", profile_sha256=PROFILE, alias="explicit-scripted"),
        AppliedBudget(
            max_calls=4,
            per_call_output_tokens=128,
            total_output_tokens=512,
            cumulative_input_bytes=1048576,
            wall_seconds=30,
        ),
        settings,
    )
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        cast(Backend, object()),
        Blobs(tmp_path / "blobs"),
        pool_id="synthetic",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    guest = Guest()
    manager._environments[opened.environment_id] = _Environment(
        channel=cast(ExecutorChannel, guest), languages=("python",), last_used=123
    )
    manager.daemon_identity = lambda _: VM
    store.remember_endpoint(opened.environment_id, INC, VM)
    environment_records = EnvironmentRecords(tmp_path / "environments.sqlite3")
    environment_records.save(
        EnvironmentInfo(
            environment_id=opened.environment_id,
            project_id="p",
            profile="minimal",
            image="synthetic",
            sandbox=VM[0],
            sandbox_id=VM[1],
            policy="sha256:" + "a" * 64,
            state=EnvironmentState.READY,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    environments = cast(
        Environments,
        SimpleNamespace(
            records=environment_records,
            deleted=[],
            required_deleted=[],
            required_deleting=[],
            confirmed_deleted=[],
            reusing=[],
        ),
    )
    clients = [
        ClientSettings(
            name="controller",
            token_sha256=token_digest(CONTROL),
            projects=("p",),
            run_controller=True,
        ),
        ClientSettings(name="ordinary", token_sha256=token_digest(ORDINARY), projects=("p",)),
    ]
    dispatched = []
    dispatched_done = asyncio.Event()

    async def dispatch(live, request):
        assert live.run_admission is not None
        assert live.run_admission()
        # Observe the committed real intent before even the synthetic guest edge.
        db = documents._connection()
        assert db is not None
        raw = db.execute(
            "SELECT record FROM child_attempt_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
        assert request.operation_id in raw
        assert request.code in raw
        dispatched.append(request)
        await guest.request("run_cell", code=request.code)
        manager._finish(
            live,
            ExecutionState.SUCCEEDED,
            Result(
                status="ok",
                stdout=StreamOutput(
                    name="stdout", text="synthetic result 2", truncated=False, total_bytes=18
                ),
            ),
        )
        dispatched_done.set()

    monkeypatch.setattr(manager, "_dispatch", dispatch)
    app = create_app(manager, POOL, environments, clients)
    paths = []

    @web.middleware
    async def observe(request, handler):
        paths.append(request.path)
        response = await handler(request)
        if (
            mode == "accepted_lost_ack"
            and request.path == "/v1/run-kernels/submit"
            and response.status in (200, 201)
        ):
            await asyncio.wait_for(dispatched_done.wait(), 2)
            return web.json_response(
                {"error": {"kind": "unavailable", "message": "synthetic lost acknowledgement"}},
                status=503,
            )
        return response

    app.middlewares.append(observe)
    model = ScriptedModel(documents, "shell" if mode == "unoffered" else "python")
    controller = None
    try:
        async with (
            TestServer(app) as server,
            WorkerClient(
                str(server.make_url("")), ORDINARY if mode == "ordinary_token" else CONTROL
            ) as worker,
        ):
            controller = ChildController(
                journal, records, worker, AppliedChildModel(model, 128, None), config, plans=None
            )
            result = await controller.run(
                authority, prepared, original, "run", admitted=lambda: True
            )
            state = journal.get(authority, result.attempt_id)
            assert state.sealed is not None
            assert state.pending is None
            assert not any(path.startswith("/v1/executions") for path in paths)
            assert all(names == ("python",) for _, names in model.inputs)
            if mode == "positive":
                assert result.held is None
                assert result.cleanup == "done"
                assert result.handback is not None
                assert result.handback.result is not None
                assert result.handback.result.status == "partial"
                assert len(dispatched) == 1
                assert len(model.inputs) == 2
                assert "synthetic result 2" in json.dumps(model.inputs[1][0])
                assert dispatched[0].binding.scope.child_run_id == prepared.child_run_id
                assert dispatched[0].context.session_id == "s"
                assert state.sealed.recorded_at <= result.handback.recorded_at
            elif mode == "unoffered":
                assert not dispatched
                assert len(model.inputs) == 2
                assert "/v1/run-kernels/submit" not in paths
            elif mode == "ordinary_token":
                assert result.held is not None
                assert not model.inputs
                assert not dispatched
                assert paths == ["/v1/run-kernels/enroll"]
            else:
                assert result.held == "tool_unknown"
                assert len(model.inputs) == 1
                assert len(dispatched) == 1
                assert paths.count("/v1/run-kernels/submit") == 1
                assert "synthetic 2" in json.dumps(
                    [m.model_dump(mode="json") for m in state.messages]
                )
                # A new owner cannot dispatch from this original historical attempt.
                replacement_model = ScriptedModel(documents, "python")
                replacement = ChildController(
                    journal,
                    records,
                    worker,
                    AppliedChildModel(replacement_model, 128, None),
                    config,
                    plans=None,
                )
                with pytest.raises(DocumentError) as refused:
                    await replacement.run(
                        authority, prepared, original, "run-again", admitted=lambda: True
                    )
                with pytest.raises(DocumentError) as drained:
                    await replacement.close()
                assert drained.value is refused.value
                assert not replacement_model.inputs
                assert paths.count("/v1/run-kernels/submit") == 1
    finally:
        if controller is not None:
            await controller.close()
        await manager.close()
        tasks = tuple(manager._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        manager._blob_pool.shutdown(wait=True, cancel_futures=True)
        store.close()
        environment_records.close()
        original.close()
        documents.close()
