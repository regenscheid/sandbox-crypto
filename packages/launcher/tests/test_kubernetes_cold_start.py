# SPDX-License-Identifier: MIT
"""A real workspace CLI starts from the renderer's newly initialized empty-volume layout."""

from __future__ import annotations

import asyncio
import contextlib
import re
import signal
import sys
from pathlib import Path

import aiohttp
import pytest

from sandbox_science.launcher.kubernetes_models import ModelCatalog
from sandbox_science.orchestrator.projects import ProjectStore

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[3]


async def test_workspace_cli_cold_volume_starts_without_connected_runtime(tmp_path):
    volume = tmp_path / "volume"
    volume.mkdir()
    assert not list(volume.iterdir())
    helper = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(ROOT / "images/services/initialize_state.py"),
        "--root",
        str(volume),
        "--component",
        "app",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(helper.communicate(), 10)
    assert helper.returncode == 0, (stdout, stderr)
    assert {path.name for path in volume.iterdir()} == {"control"}
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        "<!doctype html><title>Cold Workspace</title>"
        '<script type="module" src="/assets/test.js"></script>'
    )
    (static / "assets/test.js").write_text("console.log('synthetic cold-volume test')")
    token = tmp_path / "worker-token"
    token.write_text("test-secret-unreachable-worker-token")
    token.chmod(0o600)
    models = ModelCatalog.model_validate(
        {
            "main": {"alias": "test/research-main:v1.0"},
            "delegated": {"alias": "test-research-delegated"},
            "reviewer": {"alias": "test-research-reviewer"},
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "sandbox_science.workspace.cli",
        "serve",
        "--state-dir",
        str(volume / "control"),
        "--static",
        str(static),
        "--listen",
        "127.0.0.1:0",
        "--preview-listen",
        "127.0.0.1:0",
        "--worker",
        "https://127.0.0.1:1",
        "--worker-token-file",
        str(token),
        *models.workspace_arguments(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=tmp_path,
    )
    try:
        assert process.stdout is not None
        printed = ""
        while "token=" not in printed:
            line = await asyncio.wait_for(process.stdout.readline(), 20)
            assert line, "cold workspace exited before publishing its local login URL"
            printed += line.decode()
        login = re.search(r"(http://127\.0\.0\.1:\d+/login\?token=[A-Za-z0-9_-]{43})", printed)
        assert login is not None
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            async with client.get(login[1]) as response:
                assert response.status == 200
                assert "Cold Workspace" in await response.text()
                base = str(response.url).rstrip("/")
            async with client.get(f"{base}/api/v0/projects") as response:
                assert response.status == 200
                assert (await response.json())["projects"] == []
            with contextlib.closing(ProjectStore(volume / "control")) as projects:
                projects.create("cold-project", "minimal", "sha256:" + "a" * 64)
            async with client.get(
                f"{base}/api/v0/projects/cold-project/models/settings"
            ) as response:
                assert response.status == 200
                settings = await response.json()
                assert settings["roles"]["main"]["alias"] == "test/research-main:v1.0"
                assert settings["roles"]["main"]["reasoning_effort"] == "max"
                assert settings["roles"]["delegated"]["reasoning_effort"] == "max"
                assert settings["roles"]["reviewer"]["reasoning_effort"] == "medium"
        assert any((volume / "control").iterdir())
    finally:
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 20)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    assert process.returncode == 0
