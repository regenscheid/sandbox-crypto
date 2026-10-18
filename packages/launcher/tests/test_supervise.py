# SPDX-License-Identifier: MIT
"""A part that is started again gives up after a bound, rather than forever."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sandbox_science.launcher.state import Layout
from sandbox_science.launcher.supervise import Part, Supervisor

pytestmark = pytest.mark.slow


async def test_a_part_that_keeps_stopping_is_given_up_after_its_bound(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "state")
    layout.make()
    supervisor = Supervisor(
        layout, asyncio.Event(), stop_timeout=1, restart_delays=(0.01,), restarts=2
    )
    flaky = Part("flaky", ["/bin/sh", "-c", "sleep 0.2; exit 3"], tmp_path, restart=True)
    await supervisor.start(flaky, 5)
    try:
        reason = await asyncio.wait_for(supervisor.watch(), 20)
    finally:
        await supervisor.stop()
    assert reason is not None
    assert "the flaky kept stopping: it was started again 2 times in a row" in reason
