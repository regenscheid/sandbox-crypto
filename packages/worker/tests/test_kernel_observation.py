# SPDX-License-Identifier: MIT
"""Existing-channel observations must not become execution or idle keepalives."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import ExecutionContext, KernelStatusRequest
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.worker import kernels as kernels_module
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.timeouts import ManagerTimeouts

from .harness import CONTEXT, TOKEN, Harness, view

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]
REQUEST = KernelStatusRequest(context=ExecutionContext.model_validate(CONTEXT))


@pytest_asyncio.fixture
async def observer(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path) as worker:
        yield worker


async def test_actual_client_observes_only_after_explicit_connection(
    observer: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = observer.manager

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("observation invoked environment lifecycle")

    async with WorkerClient(
        str(observer.client.make_url("")), TOKEN, session=observer.client.session
    ) as client:
        with monkeypatch.context() as closed:
            closed.setattr(manager, "_environment", forbidden)
            closed.setattr(manager.backend, "prepare", forbidden)
            with pytest.raises(WorkerAPIError) as error:
                await client.observe_kernels(REQUEST.context)
            assert error.value.kind == "unavailable"
            assert view(manager).connected() == set()
        explicit = await client.kernel_status(REQUEST.context)
        assert explicit.kernels == ()
        environment = view(manager).connection("env1")
        environment.last_used = before = time.monotonic() - 60
        with monkeypatch.context() as opened:
            opened.setattr(manager, "_environment", forbidden)
            opened.setattr(manager.backend, "prepare", forbidden)
            result = await client.observe_kernels(REQUEST.context)
        assert result.kernels == ()
        assert result.current_revision == explicit.current_revision
        assert result.observed_at.utcoffset() is not None
        assert environment.last_used == before
        assert environment.requests == 0
        assert view(manager).observations() == {}


async def test_live_kernel_pins_and_recovery_hold_remain_readonly(observer: Harness) -> None:
    await observer.run("observe-seed", kind="cell", code="observation_value = 1729")
    manager = observer.manager
    assert manager.revisions.observe("env1", "b" * 32) == 1
    manager.store.set_package_status("env1", "needs_recovery", "prv-synthetic", 0)
    before = list(manager.store._db.iterdump())
    environment = view(manager).connection("env1")
    environment.last_used = touched = time.monotonic() - 60
    result = await manager.observe_kernels(REQUEST)
    assert len(result.kernels) == 1
    assert (result.kernels[0].start_revision, result.current_revision) == (0, 1)
    assert result.kernels[0].revision_status == "predates"
    other = await manager.observe_kernels(
        KernelStatusRequest(context=REQUEST.context.model_copy(update={"session_id": "other"}))
    )
    assert other.kernels == ()
    assert list(manager.store._db.iterdump()) == before
    assert environment.last_used == touched
    assert environment.requests == 0
    assert manager.store.package_gate("env1").mode == "needs_recovery"


@pytest.mark.parametrize("reply", [[], [{"kernel_id": "k", "language": "python", "generation": 1}]])
@pytest.mark.parametrize("observe_method", ["observe_kernels"])
async def test_idle_release_wins_while_an_observer_rpc_is_blocked(
    observer: Harness,
    monkeypatch: pytest.MonkeyPatch,
    reply: list[dict[str, object]],
    observe_method: str,
) -> None:
    await observer.run("observe-open", kind="shell", script="true")
    manager = observer.manager
    started, release = asyncio.Event(), asyncio.Event()
    request = ExecutorChannel.request

    async def delayed(channel: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "kernels":
            started.set()
            await release.wait()
            return {"kernels": reply}
        return await request(channel, op, **fields)

    monkeypatch.setattr(ExecutorChannel, "request", delayed)
    pending = asyncio.create_task(getattr(manager, observe_method)(REQUEST))
    try:
        await started.wait()
        assert view(manager).connection("env1").requests == 0
        assert await asyncio.wait_for(manager.release_if_idle("env1", time.monotonic()), 1)
        release.set()
        with pytest.raises(WorkerError) as error:
            await pending
        assert error.value.kind == "unavailable"
        assert "env1" not in view(manager).connected()
        assert view(manager).observations() == {}
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("change", ["object", "channel", "incarnation", "detaching", "fence"])
@pytest.mark.parametrize("observe_method", ["observe_kernels"])
async def test_delayed_empty_reply_cannot_pass_a_changed_connection(
    observer: Harness, monkeypatch: pytest.MonkeyPatch, change: str, observe_method: str
) -> None:
    await observer.run("observe-open", kind="shell", script="true")
    manager = observer.manager
    environment = view(manager).connection("env1")
    channel = environment.channel
    assert channel is not None
    request = ExecutorChannel.request
    incarnation = channel.incarnation

    async def changed(peer: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "kernels":
            if change == "object":
                view(manager).put_connection(
                    "env1", type(environment)(channel=channel, last_used=environment.last_used)
                )
            elif change == "channel":
                environment.channel = None
            elif change == "incarnation":
                monkeypatch.setattr(channel, "incarnation", "inc-" + "f" * 24)
            elif change == "detaching":
                monkeypatch.setattr(channel, "detaching", True)
            else:
                view(manager).quarantine("env1")
            return {"kernels": []}
        return await request(peer, op, **fields)

    monkeypatch.setattr(ExecutorChannel, "request", changed)
    try:
        with pytest.raises(WorkerError) as error:
            await getattr(manager, observe_method)(REQUEST)
        assert error.value.kind == "unavailable"
        assert view(manager).observations() == {}
    finally:
        # Test corruption must not hide the actually owned process from teardown.
        view(manager).put_connection("env1", environment)
        environment.channel = channel
        channel.incarnation = incarnation
        channel.detaching = False
        view(manager).release_quarantine("env1")


@pytest.mark.parametrize("observe_method", ["observe_kernels"])
async def test_waiting_lifecycle_and_observer_limits_do_not_queue(
    observer: Harness, observe_method: str
) -> None:
    await observer.run("observe-open", kind="shell", script="true")
    manager = observer.manager
    environment = view(manager).connection("env1")
    channel = environment.channel
    assert channel is not None
    await environment.lock.acquire()
    lifecycle = asyncio.create_task(manager.release_if_idle("env1", time.monotonic()))
    try:
        await asyncio.sleep(0)
        for _ in range(10):
            with pytest.raises(WorkerError):
                await getattr(manager, observe_method)(REQUEST)
        assert view(manager).observations() == {}
        assert environment.requests == 0
    finally:
        environment.lock.release()
    assert await asyncio.wait_for(lifecycle, 1)
    assert "env1" not in view(manager).connected()


@pytest.mark.parametrize("observe_method", ["observe_kernels"])
async def test_global_and_per_environment_admission_owned_until_cancellation(
    observer: Harness, monkeypatch: pytest.MonkeyPatch, observe_method: str
) -> None:
    await observer.run("observe-open", kind="shell", script="true")
    manager = observer.manager
    environment = view(manager).connection("env1")
    started = asyncio.Event()
    count = 0
    request = ExecutorChannel.request

    async def blocked(channel: ExecutorChannel, op: str, **fields: Any) -> Message:
        nonlocal count
        if op == "kernels":
            count += 1
            if count == kernels_module.MAX_KERNEL_OBSERVATIONS:
                started.set()
            await asyncio.Event().wait()
        return await request(channel, op, **fields)

    monkeypatch.setattr(ExecutorChannel, "request", blocked)
    tasks: list[asyncio.Task[Any]] = []
    try:
        for index in range(kernels_module.MAX_KERNEL_OBSERVATIONS):
            name = f"env-{index}"
            record = type(environment)(channel=environment.channel, last_used=0.0)
            view(manager).put_connection(name, record)
            context = REQUEST.context.model_copy(update={"environment_id": name})
            tasks.append(
                asyncio.create_task(
                    getattr(manager, observe_method)(KernelStatusRequest(context=context))
                )
            )
        await asyncio.wait_for(started.wait(), 1)
        for name in ("env-0", "env1"):
            with pytest.raises(WorkerError, match="capacity"):
                await getattr(manager, observe_method)(
                    KernelStatusRequest(
                        context=REQUEST.context.model_copy(update={"environment_id": name})
                    )
                )
        assert len(view(manager).observations()) == kernels_module.MAX_KERNEL_OBSERVATIONS
    finally:
        for task in tasks:
            task.cancel()
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert view(manager).observations() == {}
    assert environment.requests == 0
    for index in range(kernels_module.MAX_KERNEL_OBSERVATIONS):
        view(manager).drop_connection(f"env-{index}")


@pytest.mark.parametrize("observe_method", ["observe_kernels"])
async def test_whole_deadline_includes_actual_channel_write_lock(
    tmp_path: Path, observe_method: str
) -> None:
    # The write lock stays held, so the observation's own deadline is its only way out.
    timeouts = ManagerTimeouts(kernel_observation=0.05)
    async with await Harness.start(tmp_path / "worker", timeouts=timeouts) as observer:
        await observer.run("observe-open", kind="shell", script="true")
        manager = observer.manager
        environment = view(manager).connection("env1")
        channel = view(manager).channel("env1")
        before = environment.last_used
        await channel._write_lock.acquire()
        try:
            with pytest.raises(WorkerError) as error:
                await asyncio.wait_for(getattr(manager, observe_method)(REQUEST), 10)
            assert error.value.kind == "unavailable"
            assert channel._pending == {}
            assert not channel.closed
        finally:
            channel._write_lock.release()
        assert environment.last_used == before
        assert view(manager).observations() == {}
        # Once the lock is free, an observation within the production budget succeeds.
        manager.timeouts = ManagerTimeouts()
        assert (await getattr(manager, observe_method)(REQUEST)).kernels == ()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        [{}],
        [{"kernel_id": "k", "language": "python", "generation": True}],
        [{"kernel_id": "k", "language": "python", "generation": 1}] * 2,
    ],
)
async def test_invalid_or_duplicate_kernel_identity_is_unavailable(
    observer: Harness, monkeypatch: pytest.MonkeyPatch, raw: object
) -> None:
    await observer.run("observe-open", kind="shell", script="true")
    request = ExecutorChannel.request

    async def malformed(channel: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "kernels":
            return {"kernels": raw}
        return await request(channel, op, **fields)

    monkeypatch.setattr(ExecutorChannel, "request", malformed)
    status, response = await observer.post("/v1/kernels/observe", {"context": CONTEXT})
    assert status == 503, response
    assert observer.view.observations() == {}


async def test_unauthenticated_observation_never_reaches_manager(observer: Harness) -> None:
    response = await observer.client.post("/v1/kernels/observe", json={"context": CONTEXT})
    assert response.status == 401
    assert observer.view.connected() == set()


@pytest.mark.parametrize("observe_method", ["observe_kernels"])
async def test_unhealthy_existing_connections_refuse_before_rpc(
    observer: Harness, monkeypatch: pytest.MonkeyPatch, observe_method: str
) -> None:
    await observer.run("observe-open", kind="shell", script="true")
    manager = observer.manager
    environment = view(manager).connection("env1")
    channel = environment.channel
    assert channel is not None
    untouched = environment.last_used
    before = list(manager.store._db.iterdump())

    async def forbidden(*args: Any, **kwargs: Any) -> Message:
        raise AssertionError("unhealthy observer dispatched a query")

    for condition in (
        "closed",
        "detaching",
        "missing",
        "shutdown",
        "recovering",
        "fence",
        "quarantined",
        "released",
        "session_closing",
        "recycle",
        "legacy",
    ):
        with monkeypatch.context() as patch:
            patch.setattr(ExecutorChannel, "request", forbidden)
            if condition in ("closed", "detaching"):
                patch.setattr(channel, condition, True)
            elif condition == "missing":
                patch.setattr(environment, "channel", None)
            elif condition in (
                "shutdown",
                "recovering",
                "fence",
                "quarantined",
                "released",
                "session_closing",
            ):
                view(manager).make_unusable(patch, condition, REQUEST.context)
            elif condition == "recycle":
                patch.setattr(manager.store, "recycle_needed", lambda _: True)
            else:
                patch.setitem(channel.hello, "kernel_revision_protocol", True)
            with pytest.raises(WorkerError) as error:
                await getattr(manager, observe_method)(REQUEST)
            assert error.value.kind == ("unsupported" if condition == "legacy" else "unavailable")
            assert view(manager).observations() == {}
        assert environment.requests == 0
        assert environment.last_used == untouched
    assert list(manager.store._db.iterdump()) == before


@pytest.mark.parametrize(
    "old_reply", [[], [{"kernel_id": "k", "language": "python", "generation": 1}]]
)
@pytest.mark.parametrize("observe_method", ["observe_kernels"])
async def test_completed_session_close_invalidates_only_its_active_observer(
    observer: Harness,
    monkeypatch: pytest.MonkeyPatch,
    old_reply: list[dict[str, object]],
    observe_method: str,
) -> None:
    from sandbox_science.contracts import SessionCloseRequest

    await observer.run("observe-close-seed", kind="cell", code="close_value = 1729")
    manager = observer.manager
    environment = view(manager).connection("env1")
    started, release = asyncio.Event(), asyncio.Event()
    request = ExecutorChannel.request

    async def delayed(channel: ExecutorChannel, op: str, **fields: Any) -> Message:
        if op == "kernels":
            started.set()
            await release.wait()
            return {"kernels": old_reply}
        return await request(channel, op, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(ExecutorChannel, "request", delayed)
        pending = asyncio.create_task(getattr(manager, observe_method)(REQUEST))
        try:
            await started.wait()
            # Closing another session must not invalidate this scoped token.
            other = REQUEST.context.model_copy(update={"session_id": "other"})
            await manager.close_session(SessionCloseRequest(context=other))
            assert view(manager).observations()["env1"].valid
            closed = await asyncio.wait_for(
                manager.close_session(SessionCloseRequest(context=REQUEST.context)), 1
            )
            assert len(closed.kernels) == 1
            assert view(manager).closing_sessions() == {}
            assert view(manager).connection("env1") is environment
            assert not view(manager).observations()["env1"].valid
            release.set()
            with pytest.raises(WorkerError) as error:
                await pending
            assert error.value.kind == "unavailable"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
    assert view(manager).observations() == {}
    assert (await getattr(manager, observe_method)(REQUEST)).kernels == ()
