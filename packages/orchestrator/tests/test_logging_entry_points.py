# SPDX-License-Identifier: MIT
"""The orchestrator's entry points set up the shared logging (D103), and a run's log
records carry its project, session, environment and run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sandbox_science.contracts.logs import current_context
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.confinement import runtime, server
from sandbox_science.orchestrator.harness import Harness


def spy(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[tuple[str, object]]:
    set_up: list[tuple[str, object]] = []
    monkeypatch.setattr(
        module, "configure_logging", lambda service, **kw: set_up.append((service, kw))
    )
    return set_up


def test_the_run_service_sets_up_logging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_up = spy(monkeypatch, cli)

    async def serve(args: argparse.Namespace) -> int:
        return 0

    monkeypatch.setattr(cli, "_serve", serve)
    argv = ["serve", "--worker", "http://127.0.0.1:9", "--token-file", str(tmp_path / "t"),
            "--state-dir", str(tmp_path), "--model-url", "https://gateway.invalid/v1",
            "--api-key-file", str(tmp_path / "k")]  # fmt: skip
    assert cli.main([*argv, "--log-level", "debug"]) == 0
    assert set_up == [("harness", {"level": "DEBUG"})]


@pytest.mark.parametrize(("module", "service"), [(server, "control"), (runtime, "agent-runtime")])
def test_the_confinement_services_set_up_logging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: Any, service: str
) -> None:
    set_up = spy(monkeypatch, module)
    monkeypatch.setattr(sys, "argv", ["x", "--settings", str(tmp_path / "missing.json")])
    with pytest.raises(FileNotFoundError):  # the settings are read after the logging is up
        module.main()
    assert set_up == [(service, {})]


async def test_a_run_logs_under_its_ids() -> None:
    seen: list[dict[str, str]] = []

    async def run(admitted: object, sink: object, **_: object) -> str:
        seen.append(dict(current_context()))
        return "finished"

    admitted: Any = SimpleNamespace(
        run_id="run-7",
        context=SimpleNamespace(project_id="p", session_id="s", environment_id="env-1"),
    )
    harness: Any = SimpleNamespace(_run=run)
    sink: Any = object()
    assert await Harness.start(harness, admitted, sink) == "finished"
    assert seen == [{"project": "p", "session": "s", "environment": "env-1", "run": "run-7"}]
    assert dict(current_context()) == {}
