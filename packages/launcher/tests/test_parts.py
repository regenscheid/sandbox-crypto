# SPDX-License-Identifier: MIT
"""The state directory, the tokens and each part's settings and command line."""

from __future__ import annotations

import hashlib
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher import parts
from sandbox_science.launcher.config import LauncherError, load_config
from sandbox_science.launcher.state import Layout, ensure_token


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_prepare_makes_private_tokens_once_and_the_workers_settings(make_config: Any) -> None:
    config = load_config(make_config())
    layout = parts.prepare(config)
    for directory in (layout.root, layout.tokens, layout.worker, layout.control, layout.logs):
        assert mode(directory) == 0o700
    names = ("worker", "harness-worker", "workspace-worker")
    tokens = {name: layout.token(name).read_text() for name in names}
    assert all(mode(layout.token(name)) == 0o600 for name in names)
    assert len(set(tokens.values())) == 3
    assert mode(layout.worker_settings) == 0o600
    settings = tomllib.loads(layout.worker_settings.read_text())
    assert settings["token_file"] == str(layout.token("worker"))
    assert settings["state_dir"] == str(layout.worker_state)
    assert settings["listen"] == "127.0.0.1:18100"
    digests = {client["name"]: client["token_sha256"] for client in settings["clients"]}
    assert digests == {
        name: hashlib.sha256(tokens[f"{name}-worker"].strip().encode()).hexdigest()
        for name in ("harness", "workspace")
    }
    assert all(client["projects"] == ["crypto"] for client in settings["clients"])
    openshell = settings["openshell"]
    assert openshell["isolation_tier"] == 3
    assert Path(openshell["cli"]).is_absolute()
    assert Path(openshell["environments"]["targets"]).is_absolute()
    # No token is in any settings file, and the next up keeps the same tokens.
    assert not any(token.strip() in layout.worker_settings.read_text() for token in tokens.values())
    parts.prepare(config)
    assert {name: layout.token(name).read_text() for name in names} == tokens


def test_a_token_open_to_others_or_empty_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "shared.token"
    path.write_text("x" * 43)
    path.chmod(0o640)
    with pytest.raises(LauncherError, match="open to other users"):
        ensure_token(path)
    path.chmod(0o600)
    path.write_text("short")
    with pytest.raises(LauncherError, match="holds no token"):
        ensure_token(path)


def test_command_lines_name_files_never_secrets(make_config: Any, commands: Path) -> None:
    config = load_config(
        make_config({"bin_dir": str(commands), "model": {"reviewer": "sandbox-reviewer"}})
    )
    layout = Layout(config.state_dir)
    harness = parts.harness_argv(config, layout)
    assert harness[:2] == [str(commands / parts.HARNESS), "serve"]
    assert harness[harness.index("--api-key-file") + 1] == str(config.model.api_key_file)
    assert harness[harness.index("--token-file") + 1] == str(layout.token("harness-worker"))
    assert harness[harness.index("--worker") + 1] == "http://127.0.0.1:18100"
    assert harness[harness.index("--reviewer-model") + 1] == "sandbox-reviewer"
    workspace = parts.workspace_argv(config, layout)
    assert workspace[workspace.index("--preview-listen") + 1] == "127.0.0.1:18201"
    assert workspace[workspace.index("--worker-token-file") + 1] == str(
        layout.token("workspace-worker")
    )
    assert "sandbox-reviewer" in workspace
    granted = parts.policy_argv(config, layout)
    assert granted[-3:] == ["--project", "crypto", str(config.worker.policy)]
    projects = parts.project_argvs(config, layout, "sha256:" + "a" * 64)
    assert [argv[1:3] for argv in projects] == [
        ["project", "create"],
        ["project", "revise"],
        ["session", "open"],
    ]
    assert projects[2][-2:] == ["--session", "main"]


def test_check_names_everything_missing_and_never_reads_the_key(
    make_config: Any, commands: Path, tmp_path: Path
) -> None:
    config = load_config(make_config({"bin_dir": str(commands)}))
    config.model.api_key_file.chmod(0)  # the launcher only looks for it
    parts.check(config)
    (config.workspace.static / "index.html").unlink()
    (commands / parts.HARNESS).unlink()
    config.worker.policy.unlink()
    with pytest.raises(LauncherError) as error:
        parts.check(config)
    message = str(error.value)
    assert "npm ci --ignore-scripts && npm run build" in message
    assert f"no {parts.HARNESS} in {commands}" in message
    assert f"no project policy at {config.worker.policy}" in message
    config.model.api_key_file.chmod(0o600)
