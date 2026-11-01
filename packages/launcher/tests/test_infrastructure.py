# SPDX-License-Identifier: MIT
"""Automatic VM recovery persists decisions and honors explicit Stop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sandbox_science.launcher import orchestrator
from sandbox_science.launcher.config import LauncherError, load_config
from sandbox_science.launcher.infrastructure import (
    InfrastructureRecovery,
    RecoveryState,
    launcher_recovery,
)
from sandbox_science.launcher.state import Layout, write_private
from sandbox_science.launcher.supervise import Supervisor
from sandbox_science.testing import FakeClock

from .conftest import ConfigMaker


async def test_replacement_budget_survives_restart_and_stop_prevents_dispatch(
    make_config: ConfigMaker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(make_config({"orchestrator": {"recover_infrastructure": True}}))
    layout = Layout(tmp_path / "recovery")
    layout.make()
    (layout.root / "orchestrator").mkdir()
    stop = asyncio.Event()
    supervisor = Supervisor(layout, stop, stop_timeout=1)
    calls = 0

    async def fail(*args: object) -> None:
        nonlocal calls
        calls += 1
        raise LauncherError("known owned VM readiness failure")

    monkeypatch.setattr(orchestrator, "replace", fail)
    clock = FakeClock()
    for number in range(1, 11):
        recovery = launcher_recovery(config, layout, supervisor, clock=clock)
        await recovery.replace()
        state = RecoveryState.model_validate_json(recovery.path.read_bytes())
        assert state.attempts == number
        assert not state.pending
        assert state.retry_after > clock.now().timestamp()
    await launcher_recovery(config, layout, supervisor, clock=clock).replace()
    assert calls == 10
    recovery = launcher_recovery(config, layout, supervisor, clock=clock)
    state = RecoveryState.model_validate_json(recovery.path.read_bytes())
    assert state.blocked
    write_private(recovery.path, RecoveryState().model_dump_json())
    stop.set()
    await launcher_recovery(config, layout, supervisor, clock=clock).replace()
    assert calls == 10


async def test_unknown_replacement_is_retained_without_replay(
    make_config: ConfigMaker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(make_config({"orchestrator": {"recover_infrastructure": True}}))
    layout = Layout(tmp_path / "recovery")
    layout.make()
    directory = layout.root / "orchestrator"
    directory.mkdir()
    write_private(
        directory / "infrastructure-recovery.json",
        RecoveryState(attempts=1, pending=True).model_dump_json(),
    )
    supervisor = Supervisor(layout, asyncio.Event(), stop_timeout=1)

    async def forbidden(*args: object) -> None:
        pytest.fail("unknown replacement was dispatched again")

    monkeypatch.setattr(orchestrator, "replace", forbidden)
    recovery = launcher_recovery(config, layout, supervisor)
    await recovery.replace()
    assert recovery.state.blocked
    assert recovery.state.pending
    assert recovery.state.error == "prior replacement outcome is unknown"


@pytest.mark.parametrize("irreversible", [False, True])
async def test_an_interrupted_retryable_attempt_is_retried_unless_marked_irreversible(
    tmp_path: Path, irreversible: bool
) -> None:
    path = tmp_path / "infrastructure-recovery.json"
    stopping = asyncio.Event()

    async def interrupted() -> None:
        if irreversible:
            recovery.irreversible()
        assert RecoveryState.model_validate_json(path.read_bytes()).pending
        stopping.set()
        raise asyncio.CancelledError  # as SIGTERM cancels the owner mid-attempt

    def journal(replace: Callable[[], Awaitable[None]]) -> InfrastructureRecovery:
        return InfrastructureRecovery(
            path,
            enabled=True,
            alive=lambda: None,
            replace=replace,
            stopping=stopping,
            clock=FakeClock(),
            retryable=True,
        )

    recovery = journal(interrupted)
    with pytest.raises(asyncio.CancelledError):
        await recovery.replace()
    stopping.clear()
    calls = 0

    async def succeed() -> None:
        nonlocal calls
        calls += 1

    restarted = journal(succeed)
    assert restarted.state.blocked is irreversible
    assert restarted.state.attempts == 1  # the interrupted attempt still counts
    await restarted.replace()
    assert calls == (0 if irreversible else 1)
    assert restarted.state.attempts == (1 if irreversible else 2)


@pytest.mark.parametrize("draining", [True, "unknown"])
async def test_replacement_refuses_disconnected_but_draining_or_invalid_health(
    make_config: ConfigMaker, tmp_path: Path, draining: bool | str
) -> None:
    async def health(request: web.Request) -> web.Response:
        return web.json_response(
            {"ready": True, "runtime_connected": False, "runtime_draining": draining}
        )

    app = web.Application()
    app.router.add_get("/health", health)
    async with TestServer(app) as server:
        config = load_config(
            make_config(
                {
                    "orchestrator": {"control_listen": f"127.0.0.1:{server.port}"},
                    "stop_timeout": 0.05,
                }
            )
        )
        layout = Layout(tmp_path / "recovery")
        layout.make()
        supervisor = Supervisor(layout, asyncio.Event(), stop_timeout=1)
        with pytest.raises(LauncherError, match=r"did not settle|incompatible"):
            await orchestrator.replace(config, layout, supervisor)
        # Refusal happens before generation/token rotation or new VM creation.
        assert not (layout.root / "orchestrator").exists()
