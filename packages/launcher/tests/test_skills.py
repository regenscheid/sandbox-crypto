# SPDX-License-Identifier: MIT
"""The config's skill packs through the real harness (docs/specs/skills.md, SK-28;
SK-F14): the first ``up`` imports and pins a pack, a later one fetches nothing and keeps
the pin, and an update moves it, saying what changed.

The pack is ``ss-test-skills``, written here under the MIT License, in a temporary git
repository: ``main`` at C1, then C2, which changes ``alpha``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher import cli, skills
from sandbox_science.launcher.config import LauncherConfig, LauncherError, load_config
from sandbox_science.launcher.state import Layout
from sandbox_science.launcher.supervise import Supervisor

HARNESS = Path(sys.executable).parent / "sandbox-science-harness"
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not HARNESS.exists(), reason="needs the workspace's harness command"),
]
ALPHA = "---\nname: alpha\ndescription: Says the marker.\n---\nSS-MARKER-ALPHA-1\n"
BETA = "---\nname: beta\ndescription: Counts the lines of the notes.\n---\nCount them.\n"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        cwd=root, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return done.stdout.strip()


@pytest.fixture
def pack(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "ss-test-skills"
    for path, text in {"skills/alpha/SKILL.md": ALPHA, "skills/beta/SKILL.md": BETA}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "C1")
    return root


def _config(
    make_config: Any, pack: Path, name: str = "sandbox-science.toml", **settings: str
) -> tuple[Path, LauncherConfig]:
    path = make_config(
        {
            "bin_dir": str(HARNESS.parent),
            "projects": [{"id": "crypto", "profile": "minimal", "skills": ["ss"]}],
            "skills": {"packs": {"ss": {"git": str(pack), **settings}}},
        },
        name,
    )
    return path, load_config(path)


def _keep(config: LauncherConfig, *, update: bool = False) -> list[str]:
    layout = Layout(config.state_dir)
    supervisor = Supervisor(layout, asyncio.Event(), stop_timeout=5)
    return asyncio.run(skills.keep(config, layout, supervisor, update=update))


def _imports(config: LauncherConfig) -> list[dict[str, Any]]:
    argv = [str(HARNESS), "skills", "list", "--state-dir", str(Layout(config.state_dir).control)]
    done = subprocess.run([*argv, "--source", "ss"], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def test_up_keeps_its_pin_until_an_update_moves_it(
    make_config: Any, pack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SK-F14."""
    path, config = _config(make_config, pack)
    layout = Layout(config.state_dir)
    layout.make()
    subprocess.run(
        [str(HARNESS), "project", "create", "--state-dir", str(layout.control),
         "--project", "crypto", "--profile", "minimal", "--policy", "sha256:" + "a" * 64],
        capture_output=True, check=True,
    )  # fmt: skip
    first = _git(pack, "rev-parse", "HEAD")[:12]
    assert _keep(config) == [
        f"skills: ss imported at {first}, 2 skills",
        f"skills: crypto pins ss at {first}",
    ]
    (pack / "skills/alpha/SKILL.md").write_text(ALPHA + "Changed.\n")
    _git(pack, "commit", "-qam", "C2")
    second = _git(pack, "rev-parse", "HEAD")[:12]
    # The second up fetches nothing and keeps C1.
    assert _keep(config) == [f"skills: ss kept at {first}"]
    assert len(_imports(config)) == 1
    _, named = _config(make_config, pack, "named.toml", ref="main")
    (kept,) = _keep(named)
    assert kept.startswith(f"skills: ss kept at {first} (the config names another ref")
    # The update pins C2 and says alpha changed.
    assert _keep(config, update=True) == [
        f"skills: ss fetched at {second}, 2 skills",
        f"skills: crypto moves ss from {first} to {second}: changed alpha",
    ]
    assert cli.main(["skills", "update", "--config", str(path), "--pack", "ss"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"skills: ss is unchanged at {second}",
        f"skills: crypto keeps ss at {second}, unchanged",
    ]
    assert len(_imports(config)) == 2
    with pytest.raises(LauncherError, match=r"no skills\.packs other"):
        asyncio.run(
            skills.keep(
                config,
                layout,
                Supervisor(layout, asyncio.Event(), stop_timeout=5),
                only=["other"],
            )
        )


def test_an_update_needs_the_projects_and_names_a_failed_fetch(
    make_config: Any, pack: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = _config(make_config, pack)
    assert cli.main(["skills", "update", "--config", str(path)]) == 1
    assert "sandbox-science up makes the projects first" in capsys.readouterr().err
    missing, config = _config(make_config, tmp_path / "nowhere")
    Layout(config.state_dir).make()
    assert cli.main(["skills", "update", "--config", str(missing)]) == 1
    said = capsys.readouterr().err
    assert "skills import failed, with status 1" in said
    assert "nowhere' does not appear to be a git repository" in said
