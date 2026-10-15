# SPDX-License-Identifier: MIT
"""The state directory, the tokens and each part's settings and command line."""

from __future__ import annotations

import hashlib
import stat
import threading
import tomllib
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher import parts
from sandbox_science.launcher.config import LauncherError, load_config
from sandbox_science.launcher.state import Layout, ensure_token, write_private


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_prepare_makes_private_tokens_once_and_the_workers_settings(make_config: Any) -> None:
    config = load_config(make_config())
    layout = parts.prepare(config)
    for directory in (layout.root, layout.tokens, layout.worker, layout.control, layout.logs):
        assert mode(directory) == 0o700
    names = ("worker", "harness-worker", "workspace-worker", "launcher-worker")
    tokens = {name: layout.token(name).read_text() for name in names}
    assert all(mode(layout.token(name)) == 0o600 for name in names)
    assert len(set(tokens.values())) == 4
    assert mode(layout.worker_settings) == 0o600
    settings = tomllib.loads(layout.worker_settings.read_text())
    assert settings["token_file"] == str(layout.token("worker"))
    assert settings["state_dir"] == str(layout.worker_state)
    assert settings["listen"] == "127.0.0.1:18100"
    digests = {client["name"]: client["token_sha256"] for client in settings["clients"]}
    assert digests == {
        name: hashlib.sha256(tokens[f"{name}-worker"].strip().encode()).hexdigest()
        for name in ("harness", "workspace", "launcher")
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


def test_a_state_directory_open_to_others_is_refused_not_changed(
    make_config: Any, tmp_path: Path
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    config = load_config(make_config({"state_dir": str(shared)}))
    with pytest.raises(LauncherError, match=r"is open to other users .mode 755"):
        parts.prepare(config)
    assert mode(shared) == 0o755
    assert list(shared.iterdir()) == []


def test_a_model_key_file_open_to_others_is_refused_without_reading_it(
    make_config: Any, commands: Path
) -> None:
    config = load_config(make_config({"bin_dir": str(commands)}))
    config.model.api_key_file.chmod(0o644)
    with pytest.raises(LauncherError, match=r"the model key file .* is open to other users"):
        parts.check(config)


def test_writers_of_one_file_never_share_a_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "shared.json"

    failures: list[BaseException] = []

    def write(number: int) -> None:
        try:
            for _ in range(50):
                write_private(target, f"{number}\n" * 1000)
        except BaseException as failure:  # a writer's temporary file taken by another
            failures.append(failure)

    threads = [threading.Thread(target=write, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    lines = set(target.read_text().splitlines())
    assert len(lines) == 1  # one writer's whole text, never a mix
    assert mode(target) == 0o600
    assert [path.name for path in tmp_path.iterdir()] == ["shared.json"]


def test_the_mcp_proxy_gets_its_files_and_the_run_service_its_flags(
    make_config: Any, commands: Path
) -> None:
    proxy = {
        "image": "sandbox-science/mcp-proxy:abc1234",
        "providers": ["p"],
        "servers": ["s", "t"],
    }
    config = load_config(make_config({"bin_dir": str(commands), "mcp_proxy": proxy}))
    parts.check(config)
    layout = parts.prepare(config)
    upload = layout.mcp / "upload"
    assert mode(layout.mcp) == mode(upload) == 0o700
    for name in ("pool-ceiling.yaml", "policy.yaml", "proxy.toml"):
        assert mode(upload / name) == 0o600
    assert (upload / "policy.yaml").read_text() == config.worker.policy.read_text()
    assert mode(layout.token("mcp-orchestrator")) == 0o600
    harness = parts.harness_argv(config, layout)
    assert harness[harness.index("--mcp-proxy") :] == [
        "--mcp-proxy", "http://127.0.0.1:18080",
        "--mcp-token-file", str(layout.token("mcp-orchestrator")),
        "--mcp-project", "crypto", "--mcp-server", "s", "--mcp-server", "t",
    ]  # fmt: skip
    (commands / "sandbox-science-mcp-proxy").unlink()
    with pytest.raises(LauncherError, match="no sandbox-science-mcp-proxy in"):
        parts.check(config)


def test_the_run_service_takes_the_review_limits_the_config_sets(
    make_config: Any, commands: Path
) -> None:
    """Lab 38: the per-review token budget and the reviewer's hourly cap pass through."""
    model = {"reviewer": "r", "review_token_budget": 800000, "review_calls_per_hour": 500}
    config = load_config(make_config({"bin_dir": str(commands), "model": model}))
    harness = parts.harness_argv(config, Layout(config.state_dir))
    assert harness[harness.index("--review-token-budget") + 1] == "800000"
    assert harness[harness.index("--review-calls-per-hour") + 1] == "500"
    unset = load_config(make_config({"bin_dir": str(commands), "model": {"reviewer": "r"}}))
    assert "--review-token-budget" not in parts.harness_argv(unset, Layout(unset.state_dir))


def fake_node(directory: Path, version: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    node = directory / "node"
    node.write_text(f"#!/bin/sh\necho {version}\n")
    node.chmod(0o755)
    return node


def test_dedicated_sessions_watcher_and_math_reach_their_commands(
    make_config: Any, commands: Path, tmp_path: Path
) -> None:
    build = tmp_path / "dist-math"
    build.mkdir()
    for name in parts.MATH_FILES:
        (build / name).write_text("// built\n")
    node = fake_node(tmp_path / "node-bin", "v24.20.0")
    config = load_config(
        make_config(
            {
                "bin_dir": str(commands),
                "projects": [{"id": "crypto", "profile": "minimal",
                              "sessions": ["a", {"id": "c", "dedicated": True}]}],
                "model": {"watcher": "watcher", "watcher_interval": 600},
                "run_service": {"math_checks": str(build), "node": str(node)},
            }
        )
    )  # fmt: skip
    parts.check(config)
    layout = Layout(config.state_dir)
    opened = [argv for argv in parts.project_argvs(config, layout, "sha256:" + "a" * 64)
              if argv[1:3] == ["session", "open"]]  # fmt: skip
    assert [argv[-2:] for argv in opened] == [["--session", "a"], ["c", "--dedicated"]]
    harness = parts.harness_argv(config, layout)
    assert harness[harness.index("--watcher-model") + 1] == "watcher"
    assert harness[harness.index("--watcher-interval") + 1] == "600"
    assert "--watcher-calls-per-hour" not in harness  # the run service's default
    assert harness[harness.index("--math-node") + 1] == str(node)
    assert harness[harness.index("--math-launcher") + 1] == str(build / "check-math.mjs")
    assert harness[harness.index("--math-bundle") + 1] == str(build / "mathcheck.mjs")
    plain = load_config(make_config({"bin_dir": str(commands)}))
    assert not any(
        flag.startswith(("--watcher", "--math")) for flag in parts.harness_argv(plain, layout)
    )


def test_math_checks_need_their_build_and_a_recent_node(
    make_config: Any, commands: Path, tmp_path: Path
) -> None:
    build = tmp_path / "dist-math"
    old = fake_node(tmp_path / "old", "v20.11.1")
    config = load_config(
        make_config(
            {"bin_dir": str(commands), "run_service": {"math_checks": str(build), "node": str(old)}}
        )
    )
    with pytest.raises(LauncherError) as error:
        parts.check(config)
    message = str(error.value)
    assert f"no math checker build at {build}: in web/workspace, run npm run build:math" in message
    assert "the math checks need Node 22.12 or newer" in message
    assert "v20.11.1" in message
