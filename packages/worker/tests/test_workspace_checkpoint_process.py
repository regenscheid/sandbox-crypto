# SPDX-License-Identifier: MIT
"""An actual killed operator process leaves partial bytes and an incomplete intent."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.contracts import EnvironmentInfo, EnvironmentState
from sandbox_science.testing.waiting import eventually
from sandbox_science.worker.checkpoint_operator import verify
from sandbox_science.worker.environments import EnvironmentRecords
from sandbox_science.worker.state_lock import StateLock
from sandbox_science.worker.store import RevisionRecord, Store

pytestmark = pytest.mark.slow


async def test_killed_real_checkpoint_process_cannot_commit_or_replace_results(tmp_path: Path):
    root, state, output = tmp_path / "sandbox", tmp_path / "state", tmp_path / "checkpoint"
    root.mkdir()
    state.mkdir()
    prefix = root / ".sandbox-science-prefix"
    prefix.mkdir()
    identity = "prefix-" + "a" * 32
    (prefix / ".sandbox-science-prefix-id").write_text(identity)
    (root / ".openshell-initialized").write_bytes(b"initialized\n")
    with (root / "large-research").open("wb") as handle:
        handle.truncate(16 * 1024**3)
    records = EnvironmentRecords(state / "environments.sqlite3")
    records.save(
        EnvironmentInfo(
            project_id="p",
            environment_id="e",
            profile="crypto-base",
            image="example.invalid/test@sha256:" + "1" * 64,
            sandbox="ss-test",
            sandbox_id="sandbox-test",
            policy="sha256:" + "2" * 64,
            state=EnvironmentState.STOPPED,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        driver_config=json.dumps(
            {"kubernetes": {"pod": {"node_selector": {"kubernetes.io/arch": "arm64"}}}}
        ),
        require_workspace_prefix=True,
    )
    records.close()
    store = Store(state / "worker.sqlite3")
    store.remember_workspace_prefix("e", identity, "inc-" + "b" * 24)
    store.save_revision("e", RevisionRecord(1, "c" * 32, "original"))
    store.close()
    fake_cli = tmp_path / "test-openshell"
    fake_cli.write_text(
        f"#!{sys.executable}\nimport json\n"
        'print(json.dumps({"id":"sandbox-test","phase":"Stopped"}))\n'
    )
    fake_cli.chmod(0o700)
    settings = tmp_path / "worker.toml"
    settings.write_text(
        f'state_dir = "{state}"\ntoken_file = "never-read-test-token"\n'
        f'[openshell]\nisolation_tier = 3\ncli = "{fake_cli}"\n'
    )
    command = [
        sys.executable,
        "-c",
        "from sandbox_science.worker.cli import main; raise SystemExit(main())",
        "workspace-checkpoint",
        "create",
        "--settings",
        str(settings),
        "--environment",
        "e",
        "--architecture",
        "arm64",
        "--checkpoint",
        str(output),
        "--workspace",
        str(root),
        "--exclusive-offline-mount",
    ]
    environment = {**os.environ, "TMPDIR": str(tmp_path)}
    process = await asyncio.create_subprocess_exec(
        *command,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await eventually(
            lambda: (
                (output / "archive.bin").exists()
                and (output / "archive.bin").stat().st_size > 1024**2
            ),
            timeout=10,
            describe="the actual checkpoint process writing file bytes",
        )
        process.kill()
        await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == -9
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert (output / "intent.json").is_file()
    assert (output / "archive.bin").stat().st_size > 1024**2
    assert not (output / "receipt.json").exists()
    with pytest.raises(FileNotFoundError):
        verify(output)
    assert (root / "large-research").stat().st_size == 16 * 1024**3
    with StateLock(state):
        pass  # OS release of the killed process's state lease is observable.
    retry = await asyncio.create_subprocess_exec(
        *command,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, errors = await asyncio.wait_for(retry.communicate(), 10)
        assert retry.returncode == 1
        assert b"File exists" in errors
    finally:
        if retry.returncode is None:
            retry.kill()
            await retry.communicate()
    assert not (output / "receipt.json").exists()
