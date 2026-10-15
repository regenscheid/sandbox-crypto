# SPDX-License-Identifier: MIT
"""A launcher config with every file it names, in a temporary directory."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher import toml

ConfigMaker = Callable[..., Path]


def _merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        elif value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


@pytest.fixture
def make_config(tmp_path: Path) -> ConfigMaker:
    """Write ``sandbox-science.toml`` with ``changes`` merged in (None removes a key)."""
    site = tmp_path / "site"
    for directory in ("policy-store", "web/dist", "bin"):
        (site / directory).mkdir(parents=True, exist_ok=True)
    (site / "model.key").write_text("not read by the launcher\n")
    (site / "model.key").chmod(0o600)
    for name in ("policy.yaml", "targets.yaml", "pool-ceiling.yaml", "web/dist/index.html"):
        (site / name).write_text("# test\n")
    openshell = site / "bin" / "openshell"
    openshell.write_text("#!/bin/sh\nexit 0\n")
    openshell.chmod(0o755)

    def make(changes: dict[str, Any] | None = None, name: str = "sandbox-science.toml") -> Path:
        base: dict[str, Any] = {
            "state_dir": "state",
            "projects": [{"id": "crypto", "profile": "minimal", "sessions": ["main"]}],
            "model": {
                "url": "https://gateway.example/v1",
                "name": "sandbox-orchestrator",
                "api_key_file": "model.key",
            },
            "worker": {"listen": "127.0.0.1:18100", "policy": "policy.yaml"},
            "workspace": {"listen": "127.0.0.1:18200", "static": "web/dist"},
            "openshell": {
                "cli": "bin/openshell",
                "python": "/opt/sandbox-science/bin/python",
                "isolation_tier": 3,
                "env": {"XDG_CONFIG_HOME": "~/openshell-config"},
                "environments": {
                    "policy_store": "policy-store",
                    "targets": "targets.yaml",
                    "ceiling": "pool-ceiling.yaml",
                    "profiles": {"minimal": "sandbox-science/minimal:abc1234"},
                },
            },
        }
        path = site / name
        path.write_text(toml.dumps(_merge(base, changes or {})))
        return path

    return make


@pytest.fixture
def commands(tmp_path: Path) -> Path:
    """A directory with stand-ins for the parts' commands, each doing nothing."""
    directory = tmp_path / "commands"
    directory.mkdir()
    for name in ("sandbox-science-worker", "sandbox-science-harness", "sandbox-science-workspace"):
        command = directory / name
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o755)
    return directory


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    assert os.path.expanduser("~") == str(path)
    return path
