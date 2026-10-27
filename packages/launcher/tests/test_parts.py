# SPDX-License-Identifier: MIT
"""The state directory, the tokens and each part's settings and command line."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import tomllib
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts.openshell_version import OPENSHELL_VERSION
from sandbox_science.launcher import openshell_version, orchestrator, parts, skills
from sandbox_science.launcher.config import LauncherError, load_config
from sandbox_science.launcher.state import (
    Layout,
    check_private,
    ensure_token,
    private_dir,
    write_private,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("ceiling", [None, 80_000, 600_000])
def test_context_ceiling_reaches_confined_control_and_harness(
    make_config: Any, ceiling: int | None
) -> None:
    config = load_config(make_config({"run_service": {"context_ceiling": ceiling}}))
    layout = parts.prepare(config)
    directory, _ = orchestrator.prepare(config, layout)
    configured = json.loads((directory / "control.json").read_text())["settings"]
    argv = parts.harness_argv(config, layout)
    if ceiling is None:
        assert "ceiling" not in configured
        assert "--context-ceiling" not in argv
    else:
        assert configured["ceiling"] == ceiling
        assert argv[argv.index("--context-ceiling") + 1] == str(ceiling)
    assert configured["cost_limits"] is True


@pytest.mark.parametrize("ceiling", [0, -1])
def test_nonpositive_context_ceiling_is_refused(make_config: Any, ceiling: int) -> None:
    with pytest.raises(LauncherError, match="context_ceiling"):
        load_config(make_config({"run_service": {"context_ceiling": ceiling}}))


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
    assert {client["name"] for client in settings["clients"] if client["artifact_retention"]} == {
        "harness"
    }
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


def test_a_preview_name_is_passed_to_the_workspace(make_config: Any, commands: Path) -> None:
    config = load_config(
        make_config(
            {
                "bin_dir": str(commands),
                "workspace": {"listen": "127.0.0.1:18200", "static": "web/dist",
                              "preview_name": "127.0.0.1"},
            }
        )
    )  # fmt: skip
    workspace = parts.workspace_argv(config, Layout(config.state_dir))
    assert workspace[workspace.index("--preview-name") + 1] == "127.0.0.1"


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
    assert "--preview-name" not in workspace  # the workspace's default: localhost
    assert workspace[workspace.index("--worker-token-file") + 1] == str(
        layout.token("workspace-worker")
    )
    assert "sandbox-reviewer" in workspace
    assert [
        workspace[index + 1] for index, value in enumerate(workspace) if value == "--agent-model"
    ] == [
        "main=sandbox-orchestrator",
        "delegated=sandbox-orchestrator",
        "reviewer=sandbox-reviewer",
    ]
    assert "reviewer=medium" in workspace
    granted = parts.policy_argv(config, layout)
    assert granted[-3:] == ["--project", "crypto", str(config.worker.policy)]
    projects = parts.project_argvs(config, layout, "sha256:" + "a" * 64)
    assert [argv[1:3] for argv in projects] == [
        ["project", "create"],
        ["project", "revise"],
        ["session", "open"],
    ]
    assert projects[2][-2:] == ["--session", "main"]
    # A project the control store has is not made again, only moved to the policy.
    again = parts.project_argvs(config, layout, "sha256:" + "b" * 64, {"crypto": "minimal"})
    assert [argv[1:3] for argv in again] == [["project", "revise"], ["session", "open"]]
    with pytest.raises(LauncherError, match="keeps the profile it was made with"):
        parts.project_argvs(config, layout, "sha256:" + "b" * 64, {"crypto": "crypto-base"})


def test_messages_reviewer_omits_the_default_effort(make_config: Any, commands: Path) -> None:
    config = load_config(
        make_config(
            {
                "bin_dir": str(commands),
                "model": {"reviewer": "claude", "api": {"claude": "messages"}},
            }
        )
    )
    layout = Layout(config.state_dir)
    harness = parts.harness_argv(config, layout)
    workspace = parts.workspace_argv(config, layout)
    assert "reviewer=medium" not in harness
    assert "reviewer=medium" not in workspace
    assert "--reviewer-effort" not in workspace


def test_the_skills_embedding_model_reaches_imports_and_runs_by_the_key_path(
    make_config: Any, commands: Path
) -> None:
    pack = {"git": "https://git.example/pack.git", "ref": "v1", "path": "skills"}
    config = load_config(
        make_config(
            {
                "bin_dir": str(commands),
                "skills": {"embedding_model": "embed", "packs": {"ss": pack}},
            }
        )
    )
    argv = skills.import_argv(config, "ss", config.skills.packs["ss"])
    assert argv == [
        "import", "--name", "ss", "--git", "https://git.example/pack.git", "--ref", "v1",
        "--path", "skills", "--embedding-model", "embed",
        "--model-url", "https://gateway.example/v1",
        "--api-key-file", str(config.model.api_key_file),
    ]  # fmt: skip
    assert "not read by the launcher" not in " ".join(argv)
    harness = parts.harness_argv(config, Layout(config.state_dir))
    assert harness[harness.index("--skills-embedding-model") + 1] == "embed"
    plain = load_config(make_config({"bin_dir": str(commands)}))
    assert "--skills-embedding-model" not in parts.harness_argv(plain, Layout(plain.state_dir))
    archived = load_config(make_config({"skills": {"packs": {"ss": {"archive": "p.zip"}}}}))
    assert skills.import_argv(archived, "ss", archived.skills.packs["ss"]) == [
        "import", "--name", "ss", "--archive", str(archived.skills.packs["ss"].archive),
    ]  # fmt: skip


def test_the_metadata_model_reaches_imports_with_the_project_that_pays(
    make_config: Any, commands: Path
) -> None:
    """The import's pass by alias, the key by its path, and the first project that names
    the pack, whose cost its calls count for."""
    config = load_config(
        make_config(
            {
                "bin_dir": str(commands),
                "projects": [
                    {"id": "first", "profile": "minimal", "skills": ["ss"]},
                    {"id": "second", "profile": "minimal", "skills": ["ss"]},
                ],
                "skills": {"metadata_model": "pass", "packs": {"ss": {"archive": "p.zip"}}},
            }
        )
    )
    argv = skills.import_argv(config, "ss", config.skills.packs["ss"])
    assert argv[-8:] == [
        "--metadata-model", "pass", "--model-url", "https://gateway.example/v1",
        "--api-key-file", str(config.model.api_key_file), "--project", "first",
    ]  # fmt: skip
    assert "not read by the launcher" not in " ".join(argv)
    # Runs are given what that alias's pass derived, and no other's.
    harness = parts.harness_argv(config, Layout(config.state_dir))
    assert harness[harness.index("--skills-metadata-model") + 1] == "pass"
    # The workspace's skills page shows the same pass's output (lab 43, B5).
    page = parts.workspace_argv(config, Layout(config.state_dir))
    assert page[page.index("--skills-metadata-model") + 1] == "pass"
    plain = load_config(make_config({"bin_dir": str(commands)}))
    assert "--skills-metadata-model" not in parts.workspace_argv(plain, Layout(plain.state_dir))


def test_a_daily_cost_limit_is_set_for_each_project(make_config: Any, commands: Path) -> None:
    model = {"daily_cost_limit": 25}
    config = load_config(make_config({"bin_dir": str(commands), "model": model}))
    layout = Layout(config.state_dir)
    budget = [
        argv
        for argv in parts.project_argvs(config, layout, "sha256:" + "a" * 64)
        if argv[1:3] == ["project", "budget"]
    ]
    assert [argv[-4:] for argv in budget] == [["--project", "crypto", "--daily-cost-limit", "25"]]
    unset = load_config(make_config({"bin_dir": str(commands)}))
    assert not any(
        argv[1:3] == ["project", "budget"]
        for argv in parts.project_argvs(unset, layout, "sha256:" + "a" * 64)
    )
    with pytest.raises(LauncherError, match="daily_cost_limit"):
        load_config(make_config({"bin_dir": str(commands), "model": {"daily_cost_limit": -1}}))


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


def _openshell(config: Any, script: str) -> None:
    cli = Path(config.openshell.cli)
    cli.write_text(f"#!/bin/sh\n{script}\n")
    cli.chmod(0o755)


@pytest.mark.parametrize(
    "script",
    [
        "echo openshell 0.1.3",
        "echo openshell 0.1.50",
        f"echo openshell {OPENSHELL_VERSION}-pre.1",
        "echo",
        f"echo openshell {OPENSHELL_VERSION}; exit 1",
        "sleep 0.1; exit 0",
    ],
)
def test_check_refuses_an_openshell_cli_of_another_release(
    make_config: Any, commands: Path, script: str
) -> None:
    config = load_config(make_config({"bin_dir": str(commands)}))
    _openshell(config, script)
    with pytest.raises(LauncherError) as error:
        parts.check(config)
    assert f"needs OpenShell {OPENSHELL_VERSION}" in str(error.value)
    assert config.openshell.cli in str(error.value)


def test_check_accepts_the_pinned_openshell_cli_and_names_a_missing_one(
    make_config: Any, commands: Path
) -> None:
    config = load_config(make_config({"bin_dir": str(commands)}))
    for said in (f"openshell {OPENSHELL_VERSION}", f"openshell v{OPENSHELL_VERSION}"):
        _openshell(config, f"echo {said}")
        parts.check(config)
    Path(config.openshell.cli).unlink()
    with pytest.raises(LauncherError, match="no OpenShell CLI"):
        parts.check(config)


def test_an_openshell_cli_that_does_not_answer_is_refused(
    make_config: Any, commands: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openshell_version, "TIMEOUT", 0.2)
    config = load_config(make_config({"bin_dir": str(commands)}))
    _openshell(config, "sleep 5")
    with pytest.raises(LauncherError, match="did not answer"):
        parts.check(config)


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


def test_a_state_directory_with_only_the_volume_setgid_bit_is_accepted(tmp_path: Path) -> None:
    # A Kubernetes volume with an fsGroup gives its directories mode 2700 at each mount.
    volume = tmp_path / "volume"
    volume.mkdir(mode=0o700)
    volume.chmod(0o2700)
    assert private_dir(volume) == volume
    check_private(volume)
    assert stat.S_IMODE(volume.stat().st_mode) == 0o2700
    volume.chmod(0o2770)
    with pytest.raises(LauncherError, match=r"is open to other users .mode 2770"):
        private_dir(volume)
    with pytest.raises(LauncherError, match=r"is open to other users .mode 2770"):
        check_private(volume)


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


def test_a_private_write_is_synced_before_its_rename_and_its_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "sandbox.json"
    synced: list[tuple[str, str | None]] = []
    real = os.fsync

    def fsync(descriptor: int) -> None:
        where = os.readlink(f"/proc/self/fd/{descriptor}")
        content = target.read_text() if target.exists() else None
        synced.append((where, content))
        real(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    write_private(target, '{"name": "ss-orch-0123456789", "id": null}\n')
    assert len(synced) == 2
    (file, before), (directory, after) = synced
    assert Path(file).parent == tmp_path
    assert Path(file).name.startswith(".sandbox.json.")  # the temporary file, before rename
    assert before is None
    assert directory == str(tmp_path)  # then the directory, once the rename happened
    assert after == '{"name": "ss-orch-0123456789", "id": null}\n'


def test_the_workspace_gets_the_direct_paths_services_and_its_check(
    make_config: Any, commands: Path, tmp_path: Path
) -> None:
    """With [mcp_direct] (D98), the workspace shows the configured servers and asks the
    control service, with a token of its own, for the user's availability check; no MCP
    proxy part is needed, made or started (it retired on 2026-10-26)."""

    from .test_direct_mcp import direct_files

    direct_files(tmp_path)
    direct = {
        "servers": {"crypto-research-mcp": {}},
        "about": {
            "crypto-research-mcp/e-print-mcp": "IACR ePrint papers",
            "crypto-research-mcp": "the portal",
        },
        "index": ["crypto-research-mcp/e-print-mcp"],
    }
    config = load_config(make_config({"bin_dir": str(commands), "mcp_direct": direct}))
    parts.check(config)  # no sandbox-science-mcp-proxy command is looked for
    layout = parts.prepare(config)
    assert not layout.mcp.exists()  # nothing of the retired proxy's is made
    workspace = parts.workspace_argv(config, layout)
    assert workspace[workspace.index("--mcp-project") :] == [
        "--mcp-project", "crypto",
        "--mcp-service", "crypto-research-mcp=the portal",
        "--mcp-check", "http://127.0.0.1:8202",
        "--mcp-check-token-file", str(layout.token("workspace-control")),
    ]  # fmt: skip
    assert "IACR ePrint papers" not in workspace  # behind-service purpose is not top-level
    assert not {"--mcp-proxy", "--mcp-token-file"} & set(parts.harness_argv(config, layout))
    directory, _ = orchestrator.prepare(config, layout)
    host = json.loads((directory / "control.json").read_text())
    digest = Path(host["check_token_sha256_file"])
    assert mode(digest) == mode(layout.token("workspace-control")) == 0o600
    token = layout.token("workspace-control").read_text().strip()
    assert digest.read_text().strip() == hashlib.sha256(token.encode()).hexdigest()
    assert "proxy" not in host
    for wrong in (
        {"about": {"Not_A_Server": "x"}},
        {"about": {"e-print-mcp": "IACR ePrint"}},  # not behind a configured server
        {"about": {"crypto-research-mcp/e-print-mcp": ""}},
        {"about": {"crypto-research-mcp/e-print-mcp": "   "}},
        {"about": {"crypto-research-mcp/e-print-mcp": "x" * 201}},
        {"index": []},
        {"index": ["other/e-print-mcp"]},
    ):
        with pytest.raises(LauncherError):
            load_config(make_config({"mcp_direct": {**direct, **wrong}}))


def test_without_mcp_the_workspace_shows_no_services(make_config: Any, commands: Path) -> None:
    config = load_config(make_config({"bin_dir": str(commands)}))
    layout = parts.prepare(config)
    workspace = parts.workspace_argv(config, layout)
    assert not {"--mcp-project", "--mcp-service", "--mcp-check"} & set(workspace)


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


def test_the_run_service_calls_each_alias_through_the_api_the_config_names(
    make_config: Any, commands: Path
) -> None:
    """CM-41: ``[model] api`` names each alias's gateway API, which the run service is
    told; an alias it leaves out, and every alias by default, takes chat completions. An
    API there is no adapter for, or an empty alias, is refused."""
    model = {"delegated": "child", "api": {"sandbox-orchestrator": "responses", "child": "chat"}}
    config = load_config(make_config({"bin_dir": str(commands), "model": model}))
    harness = parts.harness_argv(config, Layout(config.state_dir))
    named = [harness[i + 1] for i, flag in enumerate(harness) if flag == "--api"]
    assert named == ["sandbox-orchestrator=responses", "child=chat"]
    plain = load_config(make_config({"bin_dir": str(commands)}))
    assert "--api" not in parts.harness_argv(plain, Layout(plain.state_dir))
    for wrong in ({"main": "carrier-pigeon"}, {"": "chat"}):
        with pytest.raises(LauncherError, match="api"):
            load_config(make_config({"model": {"api": wrong}}))


def test_the_run_service_calls_claude_aliases_through_messages_with_the_replay_setting(
    make_config: Any, commands: Path
) -> None:
    """CM-38, CM-39: an alias ``api`` names ``messages`` goes to the run service so,
    with the thinking replay setting when there is one, off by default. A setting with
    no Claude alias to use it is refused."""
    replay = {"image": "sha256:" + "a" * 64, "version": "1.107.0", "verified": "2026-11-02",
              "run": "cm-f30-1"}  # fmt: skip
    model = {"api": {"sandbox-orchestrator": "messages"}, "thinking_replay": replay}
    config = load_config(make_config({"bin_dir": str(commands), "model": model}))
    harness = parts.harness_argv(config, Layout(config.state_dir))
    named = [harness[i + 1] for i, flag in enumerate(harness) if flag == "--api"]
    assert named == ["sandbox-orchestrator=messages"]
    assert json.loads(harness[harness.index("--thinking-replay") + 1]) == replay
    plain = load_config(make_config({"bin_dir": str(commands)}))
    assert "--thinking-replay" not in parts.harness_argv(plain, Layout(plain.state_dir))
    for unused in ({}, {"x": "responses"}):
        with pytest.raises(LauncherError, match="thinking_replay"):
            load_config(make_config({"model": {"api": unused, "thinking_replay": replay}}))


def test_the_run_service_is_told_which_aliases_accept_images(
    make_config: Any, commands: Path
) -> None:
    """docs/specs/figures.md, FG-33: ``[model] images`` declares the aliases whose runs are
    offered ``view_image`` whatever the gateway says; none by default."""
    model = {"images": ["sandbox-orchestrator", "child"]}
    config = load_config(make_config({"bin_dir": str(commands), "model": model}))
    harness = parts.harness_argv(config, Layout(config.state_dir))
    named = [harness[i + 1] for i, flag in enumerate(harness) if flag == "--images"]
    assert named == ["sandbox-orchestrator", "child"]
    plain = load_config(make_config({"bin_dir": str(commands)}))
    assert "--images" not in parts.harness_argv(plain, Layout(plain.state_dir))
    with pytest.raises(LauncherError, match="images"):
        load_config(make_config({"model": {"images": [""]}}))


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
    (build / "figures").mkdir(parents=True)
    for name in (*parts.MATH_FILES, *parts.FIGURE_FILES):
        (build / name).write_text("// built\n")
    node = fake_node(tmp_path / "node-bin", "v24.20.0")
    config = load_config(
        make_config(
            {
                "bin_dir": str(commands),
                "projects": [{"id": "crypto", "profile": "minimal",
                              "sessions": ["a", {"id": "c", "dedicated": True}]}],
                "model": {"watcher": "watcher", "watcher_interval": 600, "delegated": "child"},
                "run_service": {"math_checks": str(build), "node": str(node), "run_share": 25,
                                "max_turns": 5000},
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
    assert harness[harness.index("--run-share") + 1] == "25"
    assert harness[harness.index("--max-turns") + 1] == "5000"
    assert harness[harness.index("--delegated-model") + 1] == "child"
    assert "--watcher-calls-per-hour" not in harness  # the run service's default
    assert harness[harness.index("--math-node") + 1] == str(node)
    assert harness[harness.index("--math-launcher") + 1] == str(build / "check-math.mjs")
    assert harness[harness.index("--math-bundle") + 1] == str(build / "mathcheck.mjs")
    # The figure checker beside it, on the same Node (FG-20).
    figures = build / "figures"
    assert harness[harness.index("--figure-launcher") + 1] == str(figures / "check-figures.mjs")
    assert harness[harness.index("--figure-bundle") + 1] == str(figures / "figurecheck.mjs")
    plain = load_config(make_config({"bin_dir": str(commands)}))
    assert not any(
        flag.startswith(
            ("--watcher", "--math", "--figure", "--run-share", "--delegated", "--max-turns")
        )
        for flag in parts.harness_argv(plain, layout)
    )
    # 0 is the run service's default, which it keeps.
    zero = load_config(make_config({"bin_dir": str(commands), "run_service": {"max_turns": 0}}))
    assert "--max-turns" not in parts.harness_argv(zero, layout)
    with pytest.raises(LauncherError, match="max_turns"):
        load_config(make_config({"bin_dir": str(commands), "run_service": {"max_turns": 100_001}}))


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


def test_role_effort_reaches_both_confined_sides_and_native_cli(make_config: Any) -> None:
    efforts = {"main": "max", "delegated": "low", "fold": "high", "overview": "medium"}
    config = load_config(make_config({"model": {"reasoning_effort": efforts}}))
    layout = parts.prepare(config)
    directory, upload = orchestrator.prepare(config, layout)
    assert json.loads((directory / "control.json").read_text())["reasoning_effort"] == efforts
    assert json.loads((upload / "runtime.json").read_text())["reasoning_effort"] == efforts
    argv = parts.harness_argv(config, layout)
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--reasoning-effort"] == [
        f"{role}={effort}" for role, effort in efforts.items()
    ]


@pytest.mark.parametrize("efforts", [{"embedding": "max"}, {"main": "MAX!"}])
def test_invalid_role_effort_is_refused(make_config: Any, efforts: dict[str, str]) -> None:
    with pytest.raises(LauncherError, match="reasoning_effort"):
        load_config(make_config({"model": {"reasoning_effort": efforts}}))
