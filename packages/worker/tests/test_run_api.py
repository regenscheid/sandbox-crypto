# SPDX-License-Identifier: MIT
"""Actual HTTP/client composition with isolated SQL and fake guest channel only."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from sandbox_science.contracts import (
    CellRequest,
    EnvironmentInfo,
    ExecutionContext,
    ExecutionState,
    Result,
)
from sandbox_science.contracts.environments import EnvironmentState
from sandbox_science.contracts.run_cells import RunCellRequest
from sandbox_science.contracts.run_kernels import RunKernelBinding, RunKernelScope
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import Backend
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.environments import EnvironmentRecords, Environments
from sandbox_science.worker.manager import ExecutionManager, _Environment, _Live
from sandbox_science.worker.run_kernel_state import RunControlReceipt
from sandbox_science.worker.settings import ClientSettings, token_digest
from sandbox_science.worker.store import Store

# Only synthetic test owners inspect private lifecycle and SQL state.
# pyright: reportPrivateUsage=false
pytestmark = pytest.mark.asyncio
POOL = "test-secret-pool-" + "p" * 32
CONTROL = "test-secret-controller-" + "c" * 32
ORDINARY = "test-secret-ordinary-" + "o" * 32
OTHER = "test-secret-other-controller-" + "a" * 32
INC = "inc-" + "a" * 24
VM = ("synthetic-vm", "synthetic-vm-id")
SCOPE = RunKernelScope(
    project_id="p",
    session_id="s",
    environment_id="e",
    parent_run_id="parent",
    delegation_id="dlg-child",
    child_run_id="child",
)
CONTEXT = ExecutionContext(project_id="p", session_id="s", environment_id="e")


class Guest:
    incarnation = INC
    daemon_token = "test-secret-no-actual-guest"  # noqa: S105
    closed = False
    detaching = False

    def __init__(self) -> None:
        self.hello = {"run_kernel_protocol": 1, "kernel_revision_protocol": 1}
        self.calls: list[str] = []
        self.block: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def detach(self) -> None:
        self.detaching = True
        self.calls.append("detach")

    async def close(self) -> None:
        self.closed = True
        self.calls.append("close")

    async def request(self, operation: str, **_: object) -> Message:
        self.calls.append(operation)
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        return {"kernels": []}


@dataclass
class Fixture:
    manager: ExecutionManager
    guest: Guest
    http: TestClient[web.Request, web.Application]
    selected: WorkerClient


@pytest_asyncio.fixture
async def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fixture]:
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
    manager._environments["e"] = _Environment(
        channel=cast(ExecutorChannel, guest), languages=("python", "sage"), last_used=123
    )
    manager.daemon_identity = lambda _: VM
    store.remember_endpoint("e", INC, VM)
    environments_db = EnvironmentRecords(tmp_path / "environments.sqlite3")
    environments_db.save(
        EnvironmentInfo(
            environment_id="e",
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
            records=environments_db,
            deleted=[],
            required_deleted=[],
            required_deleting=[],
            confirmed_deleted=[],
            reusing=[],
        ),
    )
    clients = [
        ClientSettings(
            name="control", token_sha256=token_digest(CONTROL), projects=("p",), run_controller=True
        ),
        ClientSettings(name="ordinary", token_sha256=token_digest(ORDINARY), projects=("p",)),
        ClientSettings(
            name="other", token_sha256=token_digest(OTHER), projects=("p",), run_controller=True
        ),
    ]

    async def dispatch(live: _Live, request: RunCellRequest) -> None:
        assert live.run_admission is not None
        assert live.run_admission() is True
        await guest.request("run_cell", code=request.code)
        manager._finish(live, ExecutionState.SUCCEEDED, Result(exit_code=0))

    monkeypatch.setattr(manager, "_dispatch", dispatch)
    http = TestClient(TestServer(create_app(manager, POOL, environments, clients)))
    await http.start_server()
    selected = WorkerClient(str(http.make_url("")), CONTROL)
    try:
        yield Fixture(manager, guest, http, selected)
    finally:
        if guest.block is not None:
            guest.block.set()
        await selected.close()
        await http.close()
        tasks = tuple(manager._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        manager._blob_pool.shutdown(wait=True, cancel_futures=True)
        store.close()
        environments_db.close()


async def test_actual_http_enroll_submit_selected_get_events_and_legacy_denial(
    api: Fixture,
) -> None:
    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    request = RunCellRequest(
        binding=receipt.binding,
        context=CONTEXT,
        operation_id="cell",
        code="synthetic child request",
    )
    first = await api.selected.submit_run_cell(request)
    await asyncio.sleep(0)
    result = await api.selected.get_run(
        receipt.binding, first.execution.execution_id, first.execution.attempt_id
    )
    assert result.execution.state is ExecutionState.SUCCEEDED
    assert api.guest.calls == ["run_cell"]
    page = await api.selected.run_events(
        receipt.binding, first.execution.execution_id, first.execution.attempt_id, after=0, limit=1
    )
    assert len(page.events) == 1
    assert page.next_after == 1
    assert page.last_seq >= 3
    second = await api.selected.run_events(
        receipt.binding,
        first.execution.execution_id,
        first.execution.attempt_id,
        after=page.next_after,
        limit=32,
    )
    assert second.next_after == second.last_seq
    assert len(second.events) >= 2
    replay = await api.selected.submit_run_cell(request)
    assert replay.execution.execution_id == first.execution.execution_id
    assert api.guest.calls == ["run_cell"]
    for token in (POOL, CONTROL, ORDINARY, OTHER):
        headers = {"Authorization": "Bearer " + token}
        for suffix, method in (("", "get"), ("/events", "get"), ("/cancel", "post")):
            response = await getattr(api.http, method)(
                "/v1/executions/" + first.execution.execution_id + suffix, headers=headers
            )
            assert response.status == 404
    # A valid ordinary request cannot adopt the selected operation's durable row.
    response = await api.http.post(
        "/v1/executions",
        data=CellRequest(
            context=CONTEXT, operation_id="cell", code="synthetic child request"
        ).model_dump_json(),
        headers={"Authorization": "Bearer " + POOL},
    )
    assert response.status >= 400
    assert api.guest.calls == ["run_cell"]


@pytest.mark.parametrize("token", [POOL, ORDINARY])
async def test_permission_denied_before_body_even_for_pool(api: Fixture, token: str) -> None:
    for action in ("enroll", "submit", "get", "events", "cancel", "observe", "close", "restart"):
        response = await api.http.post(
            "/v1/run-kernels/" + action,
            data=b"invalid",
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status == 403
        assert response.headers["Cache-Control"] == "no-store"
    assert api.guest.calls == []


async def test_default_permissions_and_restart_separate() -> None:
    base = {"name": "c", "token_sha256": "0" * 64, "projects": ("p",)}
    assert ClientSettings.model_validate(base).run_controller is False
    with pytest.raises(ValidationError):
        ClientSettings.model_validate({**base, "run_restart": True})
    with pytest.raises(ValidationError):
        ClientSettings.model_validate({**base, "run_controller": 1})


async def test_wrong_controller_scope_and_attempt_no_fallback(api: Fixture) -> None:
    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    async with WorkerClient(str(api.http.make_url("")), OTHER) as other:
        with pytest.raises(WorkerAPIError) as error:
            await other.enroll_run(SCOPE, "enroll")
        assert error.value.status == 404
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.enroll_run(SCOPE.model_copy(update={"project_id": "foreign"}), "enroll")
    assert error.value.status == 404
    result = await api.selected.submit_run_cell(
        RunCellRequest(
            binding=receipt.binding, context=CONTEXT, operation_id="cell", code="synthetic"
        )
    )
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.get_run(
            receipt.binding, result.execution.execution_id, "att-" + "0" * 24
        )
    assert error.value.status == 404
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.restart_run(receipt.binding, "restart", "python")
    assert error.value.status == 403


async def test_close_is_sticky_exact_replay_and_historical_read(api: Fixture) -> None:
    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    request = RunCellRequest(
        binding=receipt.binding, context=CONTEXT, operation_id="cell", code="synthetic"
    )
    first = await api.selected.submit_run_cell(request)
    await asyncio.sleep(0)
    close = await api.selected.close_run(receipt.binding, "close")
    assert close.state == "done"
    assert api.guest.calls.count("run_close") == 1
    assert await api.selected.close_run(receipt.binding, "close") == close
    assert api.guest.calls.count("run_close") == 1
    assert (
        await api.selected.get_run(
            receipt.binding, first.execution.execution_id, first.execution.attempt_id
        )
    ).execution.state is ExecutionState.SUCCEEDED
    with pytest.raises(WorkerAPIError):
        await api.selected.submit_run_cell(request.model_copy(update={"operation_id": "later"}))
    assert api.guest.calls.count("run_cell") == 1


async def test_enrollment_original_lost_response_replay_never_rebinds(api: Fixture) -> None:
    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    api.manager.daemon_identity = lambda _: ("replacement", "replacement-id")
    # Exact historic enrollment is a receipt, not eligibility or a new VM assertion.
    assert await api.selected.enroll_run(SCOPE, "enroll") == receipt
    with pytest.raises(WorkerAPIError):
        await api.selected.observe_run(receipt.binding)
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.enroll_run(SCOPE, "different-op")
    assert error.value.status == 409
    assert api.guest.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        b'{"scope":{},"scope":{},"operation_id":"x"}',
        b'{"scope":{},"operation_id":true}',
        b"{}",
        b'{"x":NaN}',
    ],
)
async def test_strict_bounded_request_and_no_authority_injection(api: Fixture, raw: bytes) -> None:
    response = await api.http.post(
        "/v1/run-kernels/enroll", data=raw, headers={"Authorization": "Bearer " + CONTROL}
    )
    assert response.status == 400
    assert api.guest.calls == []


async def test_unsupported_missing_channel_corrupt_registry_and_oversize_record_hold(
    api: Fixture,
) -> None:
    api.guest.hello = {}
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.enroll_run(SCOPE, "enroll")
    assert error.value.status == 422
    api.guest.hello = {"run_kernel_protocol": 1}
    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    first = await api.selected.submit_run_cell(
        RunCellRequest(
            binding=receipt.binding, context=CONTEXT, operation_id="cell", code="synthetic"
        )
    )
    await asyncio.sleep(0)
    api.manager.store._db.execute(
        "UPDATE executions SET record=? WHERE execution_id=?",
        ("x" * (2 * 1024 * 1024), first.execution.execution_id),
    )
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.get_run(
            receipt.binding, first.execution.execution_id, first.execution.attempt_id
        )
    assert error.value.status == 503
    api.manager.store._db.execute("DELETE FROM run_api_owners")
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.enroll_run(SCOPE, "enroll")
    assert error.value.status == 503


async def test_passive_observe_does_not_touch_activity_and_blocks_old_binding(api: Fixture) -> None:
    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    before = api.manager._environments["e"].last_used
    result = await api.selected.observe_run(receipt.binding)
    assert result.kernels == ()
    assert api.manager._environments["e"].last_used == before
    api.manager.daemon_identity = lambda _: ("replaced", "replaced")
    with pytest.raises(WorkerAPIError):
        await api.selected.observe_run(receipt.binding)
    assert api.guest.calls == ["run_kernels"]


async def test_owned_route_cancellation_and_shutdown_keep_slots_until_rpc_drains(
    api: Fixture,
) -> None:
    from sandbox_science.worker.app import _CLIENT, Client
    from sandbox_science.worker.run_api import RUN_API

    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    lane = cast(TestServer, api.http.server).app[RUN_API]
    owner = api.manager.store._db.execute("SELECT owner FROM run_api_owners").fetchone()[0]
    release = asyncio.Event()
    api.guest.block = release
    api.guest.entered.clear()
    raw = ('{"binding":' + receipt.binding.model_dump_json() + "}").encode()

    class Body:
        async def iter_chunked(self, _: int):
            yield raw

    class Request(dict[str, object]):
        def __init__(self, client: Client) -> None:
            super().__init__({_CLIENT: client})
            self.content = Body()
            self.query: dict[str, str] = {}
            self.match_info = {"action": "observe"}
            self.transport = SimpleNamespace(is_closing=lambda: False)
            self.app = cast(TestServer, api.http.server).app

    request = Request(Client("control", frozenset({"p"}), run_identity=owner))
    caller = asyncio.create_task(lane.handle(cast(web.Request, request)))
    await api.guest.entered.wait()
    caller.cancel("original caller interruption")
    await asyncio.sleep(0)
    caller.cancel("later")
    closer = asyncio.create_task(lane.close())
    await asyncio.sleep(0)
    closer.cancel("close interruption")
    await asyncio.sleep(0)
    closer.cancel("later close")
    assert lane._active == 1
    assert len(lane._work) == 1
    assert len(lane._scopes) == 1
    assert not caller.done()
    assert not closer.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as first:
        await caller
    assert first.value.args == ("original caller interruption",)
    with pytest.raises(asyncio.CancelledError):
        await closer
    assert lane._active == 0
    assert not lane._work
    assert not lane._scopes


async def test_registry_loss_partial_initialization_and_same_config_restart_hold(
    api: Fixture,
) -> None:
    from sandbox_science.worker.run_api import RUN_API, RunAPI

    first = await api.selected.enroll_run(SCOPE, "enroll")
    lane = cast(TestServer, api.http.server).app[RUN_API]
    reopened = RunAPI(api.manager, lane.authorize, enabled=True)
    reopened._require()
    # Same live configured HTTP principal gets its historical receipt after owner reopen.
    assert await api.selected.enroll_run(SCOPE, "enroll") == first
    db = api.manager.store._db
    db.execute("DROP TABLE run_api_owners")
    RunAPI(api.manager, lane.authorize, enabled=True)
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.enroll_run(SCOPE, "enroll")
    assert error.value.status == 503
    assert db.execute("SELECT count(*) FROM run_kernel_bindings").fetchone() == (1,)


@pytest.mark.parametrize("damage", ["partial", "missing", "empty_replacement"])
async def test_retained_registry_never_initializes_missing_history(
    api: Fixture, damage: str
) -> None:
    from sandbox_science.worker.run_api import RUN_API, RunAPI

    await api.selected.enroll_run(SCOPE, "enroll")
    lane = cast(TestServer, api.http.server).app[RUN_API]
    db = api.manager.store._db
    if damage == "partial":
        db.execute("UPDATE run_api_meta SET ready=0")
    elif damage == "missing":
        db.execute("DROP TABLE run_api_meta")
        db.execute("DROP TABLE run_api_owners")
    else:
        db.execute("DELETE FROM run_api_owners")
        db.execute("UPDATE run_api_meta SET count=0")
    RunAPI(api.manager, lane.authorize, enabled=True)
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.enroll_run(SCOPE, "enroll")
    assert error.value.status == 503
    assert db.execute("SELECT count(*) FROM run_kernel_bindings").fetchone() == (1,)


async def test_atomic_owner_quota_refusal_rolls_back_both_enrollments(
    api: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.worker import run_api

    monkeypatch.setattr(run_api, "MAX_OWNERS", 0)
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.enroll_run(SCOPE, "enroll")
    assert error.value.status == 503
    for query in (
        "SELECT count(*) FROM run_api_owners",
        "SELECT count(*) FROM run_kernel_bindings",
    ):
        assert api.manager.store._db.execute(query).fetchone() == (0,)
    assert api.manager.store._db.execute("SELECT count FROM run_api_meta").fetchone() == (0,)


@pytest.mark.parametrize(
    "damage", ["gap", "duplicate_key", "oversize", "wrong_attempt", "boolean_seq"]
)
async def test_selected_event_page_corruption_holds(api: Fixture, damage: str) -> None:
    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    result = await api.selected.submit_run_cell(
        RunCellRequest(
            binding=receipt.binding, context=CONTEXT, operation_id="cell", code="synthetic"
        )
    )
    await asyncio.sleep(0)
    db = api.manager.store._db
    identity = result.execution.execution_id
    if damage == "gap":
        db.execute("DELETE FROM events WHERE execution_id=? AND seq=1", (identity,))
    else:
        raw = db.execute(
            "SELECT event FROM events WHERE execution_id=? AND seq=1", (identity,)
        ).fetchone()[0]
        if damage == "duplicate_key":
            raw = raw.replace('"seq":1', '"seq":2,"seq":1')
        elif damage == "oversize":
            raw = "x" * (2 * 1024 * 1024)
        elif damage == "wrong_attempt":
            import json

            value = json.loads(raw)
            value["execution_id"] = "exe-" + "0" * 24
            raw = json.dumps(value)
        else:
            raw = raw.replace('"seq":1', '"seq":true')
        db.execute("UPDATE events SET event=? WHERE execution_id=? AND seq=1", (raw, identity))
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.run_events(
            receipt.binding, identity, result.execution.attempt_id, after=0, limit=32
        )
    assert error.value.status == 503


async def test_postcommit_control_schema_failure_is_unknown_not_input_refusal(
    api: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    original = api.manager.run_dispatcher.close

    async def damaged(
        binding: RunKernelBinding, operation_id: str, *, admitted: Callable[[], bool]
    ) -> RunControlReceipt:
        result = await original(binding, operation_id, admitted=admitted)
        wrong = result.request.model_copy(
            update={"binding": binding.model_copy(update={"sandbox_id": "foreign"})}
        )
        return result.model_copy(update={"request": wrong})

    monkeypatch.setattr(api.manager.run_dispatcher, "close", damaged)
    with pytest.raises(WorkerAPIError) as error:
        await api.selected.close_run(receipt.binding, "close")
    assert error.value.status == 503
    assert api.guest.calls == ["run_close"]
    monkeypatch.setattr(api.manager.run_dispatcher, "close", original)
    assert (await api.selected.close_run(receipt.binding, "close")).state == "done"
    assert api.guest.calls == ["run_close"]


async def test_exact_queued_cancel_does_not_cancel_sibling(
    api: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    async def queued(_: _Live, __: RunCellRequest) -> None:
        await release.wait()

    monkeypatch.setattr(api.manager, "_dispatch", queued)
    first = await api.selected.enroll_run(SCOPE, "enroll")
    second = await api.selected.enroll_run(
        SCOPE.model_copy(update={"delegation_id": "dlg-sibling", "child_run_id": "sibling"}),
        "enroll-sibling",
    )
    a = await api.selected.submit_run_cell(
        RunCellRequest(binding=first.binding, context=CONTEXT, operation_id="a", code="synthetic")
    )
    b = await api.selected.submit_run_cell(
        RunCellRequest(binding=second.binding, context=CONTEXT, operation_id="b", code="synthetic")
    )
    cancelled = await api.selected.cancel_run(
        first.binding, a.execution.execution_id, a.execution.attempt_id
    )
    assert cancelled.execution.state is ExecutionState.CANCELLED
    assert (
        await api.selected.get_run(second.binding, b.execution.execution_id, b.execution.attempt_id)
    ).execution.state is ExecutionState.QUEUED
    assert api.guest.calls == []
    release.set()


async def test_explicit_restart_permission_exact_control_and_historical_replay(
    api: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandbox_science.contracts.run_cells import run_kernel_id
    from sandbox_science.contracts.run_kernels import run_kernel_binding_sha256
    from sandbox_science.worker.app import ENVIRONMENTS

    app = create_app(
        api.manager,
        POOL,
        cast(TestServer, api.http.server).app[ENVIRONMENTS],
        [
            ClientSettings(
                name="restart-controller",
                token_sha256=token_digest(CONTROL),
                projects=("p",),
                run_controller=True,
                run_restart=True,
            )
        ],
    )
    async with TestServer(app) as server, WorkerClient(str(server.make_url("")), CONTROL) as client:
        receipt = await client.enroll_run(SCOPE, "enroll")

        async def gate(_: str, __: ExecutorChannel) -> bool:
            return True

        async def restart(operation: str, **_: object) -> Message:
            assert operation == "run_restart"
            api.guest.calls.append(operation)
            return {
                "kernel": {
                    "kernel_id": run_kernel_id(receipt.binding, "python"),
                    "language": "python",
                    "generation": 1,
                    "birth_id": "birth-" + "a" * 24,
                    "run_binding_sha256": run_kernel_binding_sha256(receipt.binding),
                }
            }

        monkeypatch.setattr(api.manager, "_sync_package_gate", gate)
        monkeypatch.setattr(api.guest, "request", restart)
        result = await client.restart_run(receipt.binding, "restart", "python")
        assert result.state == "done"
        assert result.kernel is not None
        assert result.kernel.kernel_id == run_kernel_id(receipt.binding, "python")
        api.manager.daemon_identity = lambda _: ("replacement", "replacement")
        assert await client.restart_run(receipt.binding, "restart", "python") == result
        assert api.guest.calls == ["run_restart"]


@pytest.mark.parametrize("damage", ["oversize", "blob", "duplicate", "scope", "missing"])
async def test_managed_authorization_preparse_bounds_and_scope(api: Fixture, damage: str) -> None:
    import tracemalloc

    from sandbox_science.worker.app import ENVIRONMENTS

    environments = cast(TestServer, api.http.server).app[ENVIRONMENTS]
    db = environments.records._db
    if damage == "missing":
        db.execute("DELETE FROM environments WHERE environment_id='e'")
    else:
        raw = db.execute("SELECT document FROM environments WHERE environment_id='e'").fetchone()[0]
        if damage == "oversize":
            raw = "x" * (20 * 1024 * 1024)
        elif damage == "blob":
            raw = raw.encode()
        elif damage == "duplicate":
            raw = raw.replace('"project_id":"p"', '"project_id":"foreign","project_id":"p"')
        else:
            raw = raw.replace('"environment_id":"e"', '"environment_id":"other"')
        db.execute("UPDATE environments SET document=? WHERE environment_id='e'", (raw,))
    tracemalloc.start()
    try:
        with pytest.raises(WorkerAPIError) as error:
            await api.selected.enroll_run(SCOPE, "enroll")
        assert error.value.status == (404 if damage == "missing" else 503)
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 512 * 1024
    finally:
        tracemalloc.stop()
    assert api.guest.calls == []


async def test_shutdown_detaches_without_tombstone_and_same_original_reopens(
    api: Fixture, tmp_path: Path
) -> None:
    from sandbox_science.worker.app import ENVIRONMENTS

    receipt = await api.selected.enroll_run(SCOPE, "enroll")
    first = await api.selected.submit_run_cell(
        RunCellRequest(
            binding=receipt.binding, context=CONTEXT, operation_id="cell", code="synthetic"
        )
    )
    await asyncio.sleep(0)
    environments = cast(TestServer, api.http.server).app[ENVIRONMENTS]
    store = api.manager.store
    changes = store._db.total_changes
    snapshot = store._db.execute("SELECT binding,owner,record FROM run_api_owners").fetchall()
    api.manager._channels.add(cast(ExecutorChannel, api.guest))
    await api.http.close()
    await api.manager.close()
    assert store._db.total_changes == changes
    assert store._db.execute("SELECT count(*) FROM run_kernel_closures").fetchone() == (0,)
    assert api.guest.calls == ["run_cell", "detach"]
    new = ExecutionManager(
        store,
        cast(Backend, object()),
        Blobs(tmp_path / "reopened-blobs"),
        pool_id="synthetic",
        max_artifact_size=1024,
        state_dir=tmp_path / "reopened",
    )
    guest = Guest()
    new._environments["e"] = _Environment(
        channel=cast(ExecutorChannel, guest), languages=("python", "sage"), last_used=123
    )
    new.daemon_identity = lambda _: VM
    new._channels.add(cast(ExecutorChannel, guest))
    app = create_app(
        new,
        POOL,
        environments,
        [
            ClientSettings(
                name="control",
                token_sha256=token_digest(CONTROL),
                projects=("p",),
                run_controller=True,
            )
        ],
    )
    try:
        async with (
            TestServer(app) as server,
            WorkerClient(str(server.make_url("")), CONTROL) as client,
        ):
            assert await client.enroll_run(SCOPE, "enroll") == receipt
            historical = await client.get_run(
                receipt.binding, first.execution.execution_id, first.execution.attempt_id
            )
            assert historical.execution.state is ExecutionState.SUCCEEDED
            assert (await client.observe_run(receipt.binding)).kernels == ()
            assert store._db.total_changes == changes
            assert (
                store._db.execute("SELECT binding,owner,record FROM run_api_owners").fetchall()
                == snapshot
            )
            assert guest.calls == ["run_kernels"]
    finally:
        await new.close()


async def test_selected_method_and_unknown_route_errors_are_no_store(api: Fixture) -> None:
    wrong_method = await api.http.get(
        "/v1/run-kernels/get", headers={"Authorization": "Bearer " + CONTROL}
    )
    assert wrong_method.status == 405
    assert wrong_method.headers["Cache-Control"] == "no-store"
    absent = await api.http.post(
        "/v1/run-kernels/absent", headers={"Authorization": "Bearer " + CONTROL}
    )
    assert absent.status == 404
    assert absent.headers["Cache-Control"] == "no-store"
    unauthorized = await api.http.post("/v1/run-kernels/enroll")
    assert unauthorized.status == 401
    assert unauthorized.headers["Cache-Control"] == "no-store"
