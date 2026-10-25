# SPDX-License-Identifier: MIT
"""The MCP canaries' part: a monitor, so a failed start or repeated stops never stop the
launch, and starting it again is bounded."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from sandbox_science.launcher import cli, parts
from sandbox_science.launcher.state import Layout
from sandbox_science.launcher.supervise import Part, Supervisor
from sandbox_science.testing import eventually

pytestmark = pytest.mark.slow


def _supervisor(tmp_path: Path, stop: asyncio.Event) -> Supervisor:
    layout = Layout(tmp_path / "state")
    layout.make()
    return Supervisor(
        layout,
        stop,
        stop_timeout=5,
        restart_delays=(0.05,),
        restarts=2,
        steady=60,
        keep_logs=False,
    )


def _part(tmp_path: Path, script: str) -> Part:
    return Part(
        "mcp-canaries",
        [sys.executable, "-c", script],
        tmp_path,
        ready_line=parts.CANARIES_READY,
        required=False,
        restart=True,
    )


def test_the_canary_part_is_optional_and_started_again(tmp_path: Path) -> None:
    part = cli._canaries(["sandbox-science-harness", "mcp-canaries"], Layout(tmp_path))
    assert (part.required, part.restart, part.ready_line) == (False, True, parts.CANARIES_READY)
    assert part.address is None  # it listens nowhere


async def test_a_canary_part_that_fails_to_start_leaves_the_launch_going(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    supervisor = _supervisor(tmp_path, asyncio.Event())
    started = await cli.start_canaries(
        supervisor, _part(tmp_path, "import sys; sys.exit(3)"), timeout=10
    )
    assert started is False
    assert supervisor.running == []
    assert "the MCP canaries did not start" in capsys.readouterr().out
    await supervisor.stop()


async def test_a_canary_part_that_keeps_stopping_is_given_up_without_stopping_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stop = asyncio.Event()
    supervisor = _supervisor(tmp_path, stop)
    script = f"print({parts.CANARIES_READY!r}, flush=True); import time; time.sleep(0.2)"
    assert await cli.start_canaries(supervisor, _part(tmp_path, script), timeout=10)
    watching = asyncio.ensure_future(supervisor.watch())
    try:
        await eventually(lambda: supervisor.running and supervisor.running[0].reported, timeout=60)
        assert not watching.done()  # an optional part gives no reason to stop the launch
        assert supervisor.running[0].restarts == 2
        assert "kept stopping: it was started again 2 times" in capsys.readouterr().out
    finally:
        stop.set()
        assert await asyncio.wait_for(watching, 10) is None
        await supervisor.stop()
