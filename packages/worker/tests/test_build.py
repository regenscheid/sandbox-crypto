# SPDX-License-Identifier: MIT
"""Building a worker from its settings, as ``serve`` does."""

from __future__ import annotations

from pathlib import Path

from sandbox_science.contracts import ExecutionContext, ExecutionState, ShellRequest
from sandbox_science.worker.build import build_worker
from sandbox_science.worker.lifecycle import UNMANAGED
from sandbox_science.worker.settings import Settings

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


async def test_a_worker_built_on_the_local_backend_runs_a_call(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {"token_file": tmp_path / "worker.token", "state_dir": tmp_path / "state", "local": {}}
    )
    parts = build_worker(settings, "test-secret-" + "l" * 40)
    try:
        assert parts.environments is None
        assert parts.manager.lifecycle is UNMANAGED
        assert not parts.manager.backend.daemon
        await parts.manager.start()
        execution, created = await parts.manager.submit(
            ShellRequest(operation_id="built", context=CONTEXT, script="printf built")
        )
        assert created
        ended = await parts.manager.wait(execution.execution_id)
        assert ended.state is ExecutionState.SUCCEEDED
        assert ended.result is not None
        assert ended.result.stdout is not None
        assert ended.result.stdout.text == "built"
    finally:
        await parts.close()
