# SPDX-License-Identifier: MIT
"""The ``run`` command's order: the session and the worker's isolation come first."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.model import ModelError
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError

STORE = "control.sqlite3"


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    closed: list[str] = []

    class Model:
        def __init__(self, base_url, model, key, **kwargs):
            self.alias = model

        async def close(self):
            closed.append(f"model:{self.alias}")

    class Client:
        base_url = "http://unused.invalid"

        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("client")

        async def environment(self, environment_id):
            raise WorkerAPIError(ErrorKind.UNSUPPORTED, "a local worker manages no environments")

        async def capabilities(self, *args):
            location = SimpleNamespace(
                isolation_evidence=None, pool_id="p", backend="local", sandbox_id="e", hostname="h"
            )
            return SimpleNamespace(location=location, languages=(), features=())

    monkeypatch.setattr(cli, "OpenAICompatibleModel", Model)
    monkeypatch.setattr(cli, "WorkerClient", Client)
    monkeypatch.setattr(cli, "read_secret", lambda _: "test-secret-unused")

    def arguments(**changes: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "project": "p", "session": "s", "environment": None, "dedicated": False,
            "research_template": None, "script": None, "model_url": "http://unused.invalid",
            "model": "main", "api_key_file": None, "run_id": "r1", "worker": "unused",
            "token_file": None, "allow_unisolated": False, "min_isolation_tier": 3,
            "state_dir": tmp_path, "watcher_model": "watch", "watcher_script": None,
            "reviewer_model": None, "reviewer_script": None, "math_node": None,
            "watcher_calls_per_hour": 1, "watcher_first_check": 1, "watcher_interval": 1,
            "task": "T", "max_turns": 1, "max_input_chars": 400_000, "transcript": None,
        }  # fmt: skip
        return argparse.Namespace(**{**values, **changes})

    return SimpleNamespace(arguments=arguments, closed=closed, root=tmp_path)


def project(root: Path) -> ProjectStore:
    projects = ProjectStore(root)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    return projects


async def test_a_refused_worker_is_refused_before_the_stores_open_or_scripts_are_read(
    run, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = project(run.root)

    def unopened(*args: object, **kwargs: object) -> None:
        raise AssertionError("a store opened before the run was admitted")

    monkeypatch.setattr(cli, "DocumentStore", unopened)
    monkeypatch.setattr(cli, "JobState", unopened)
    args = run.arguments(watcher_script=run.root / "missing.json")
    assert await cli._run(args) == 2
    assert capsys.readouterr().err == (
        "error: the worker offers no isolation evidence. At least tier 3 is required "
        "(--allow-unisolated for development)\n"
    )
    assert projects.runs("p", "s") == []
    assert run.closed == ["model:main", "client"]


async def test_a_missing_project_leaves_no_control_store(run) -> None:
    with pytest.raises(ProjectError, match="no project"):
        await cli._run(run.arguments(allow_unisolated=True))
    assert not (run.root / STORE).exists()


async def test_an_unreadable_role_script_is_reported_as_an_error(
    run, capsys: pytest.CaptureFixture[str]
) -> None:
    project(run.root)
    missing = run.root / "missing.json"
    with pytest.raises(ModelError, match="the --watcher-script file cannot be read"):
        await cli._run(run.arguments(allow_unisolated=True, watcher_script=missing))
    assert run.closed == ["model:main", "client"]


def test_the_command_reports_an_unreadable_script(run, capsys: pytest.CaptureFixture[str]) -> None:
    project(run.root)
    missing = run.root / "missing.json"
    argv = ["run", "--worker", "http://127.0.0.1:9", "--token-file", str(missing),
            "--project", "p", "--session", "s", "--state-dir", str(run.root),
            "--script", str(missing), "task"]  # fmt: skip
    assert cli.main(argv) == 1
    assert capsys.readouterr().err.startswith("error: the --script file cannot be read: ")
