# SPDX-License-Identifier: MIT
"""Automatic VM recovery persists decisions and honors explicit Stop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sandbox_science.launcher import orchestrator
from sandbox_science.launcher.config import LauncherError, load_config
from sandbox_science.launcher.infrastructure import InfrastructureRecovery, RecoveryState
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
        recovery = InfrastructureRecovery(config, layout, supervisor, clock=clock)
        await recovery.replace()
        state = RecoveryState.model_validate_json(recovery.path.read_bytes())
        assert state.attempts == number
        assert not state.pending
        assert state.retry_after > clock.now().timestamp()
    await InfrastructureRecovery(config, layout, supervisor, clock=clock).replace()
    assert calls == 10
    assert state.blocked
    write_private(recovery.path, RecoveryState().model_dump_json())
    stop.set()
    await InfrastructureRecovery(config, layout, supervisor, clock=clock).replace()
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
    recovery = InfrastructureRecovery(config, layout, supervisor)
    await recovery.replace()
    assert recovery.state.blocked
    assert recovery.state.pending
    assert recovery.state.error == "prior replacement outcome is unknown"
