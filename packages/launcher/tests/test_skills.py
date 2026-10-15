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
    lines, failed = asyncio.run(skills.keep(config, layout, supervisor, update=update))
    assert failed == []
    return lines


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
    assert _keep(config) == [f"skills: crypto keeps ss at {first}"]
    assert len(_imports(config)) == 1
    _, named = _config(make_config, pack, "named.toml", ref="main")
    (kept,) = _keep(named)
    assert kept.startswith(f"skills: crypto keeps ss at {first} (the config names another ref")
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
    said = capsys.readouterr().out
    assert "skills: ss is left as it was: skills import failed, with status 1" in said
    assert "nowhere' does not appear to be a git repository" in said


def test_a_pack_that_fails_leaves_the_others_going(
    make_config: Any, pack: Path, tmp_path: Path
) -> None:
    """``up`` goes on without a pack it cannot import, and the others are imported and
    pinned all the same."""
    path = make_config(
        {
            "bin_dir": str(HARNESS.parent),
            "projects": [{"id": "crypto", "profile": "minimal", "skills": ["gone", "ss"]}],
            "skills": {
                "packs": {"gone": {"git": str(tmp_path / "nowhere")}, "ss": {"git": str(pack)}}
            },
        }
    )
    config = load_config(path)
    layout = Layout(config.state_dir)
    layout.make()
    subprocess.run(
        [str(HARNESS), "project", "create", "--state-dir", str(layout.control),
         "--project", "crypto", "--profile", "minimal", "--policy", "sha256:" + "a" * 64],
        capture_output=True, check=True,
    )  # fmt: skip
    supervisor = Supervisor(layout, asyncio.Event(), stop_timeout=5)
    lines, failed = asyncio.run(skills.keep(config, layout, supervisor))
    assert failed == ["gone"]
    assert lines[0].startswith("skills: gone is left as it was: skills import failed")
    first = _git(pack, "rev-parse", "HEAD")[:12]
    assert lines[1:] == [
        f"skills: ss imported at {first}, 2 skills",
        f"skills: crypto pins ss at {first}",
    ]


def _project(layout: Layout, *ids: str) -> None:
    for project in ids:
        subprocess.run(
            [str(HARNESS), "project", "create", "--state-dir", str(layout.control),
             "--project", project, "--profile", "minimal", "--policy", "sha256:" + "a" * 64],
            capture_output=True, check=True,
        )  # fmt: skip


def _repo(root: Path, names: list[str]) -> Path:
    for name in names:
        path = root / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: The {name} skill.\n---\n{name}\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "C1")
    return root


def _lines(config: LauncherConfig, *, update: bool = False) -> tuple[list[str], list[str]]:
    layout = Layout(config.state_dir)
    supervisor = Supervisor(layout, asyncio.Event(), stop_timeout=5)
    return asyncio.run(skills.keep(config, layout, supervisor, update=update))


def test_a_failed_pin_is_one_projects_and_up_says_what_each_pins(
    make_config: Any, tmp_path: Path, home: Path
) -> None:
    """Review of #435, findings 1 and 2: a pack one project cannot pin, for a name another
    of its packs has, is still pinned in the others, the line names the project, and a
    later up says the revision each project pins, not the latest import's."""
    one = _repo(tmp_path / "one", ["alpha", "gamma"])
    two = _repo(tmp_path / "two", ["alpha", "beta"])
    projects = [
        {"id": "a", "profile": "minimal", "skills": ["one", "two"]},
        {"id": "b", "profile": "minimal", "skills": ["two"]},
        {"id": "c", "profile": "minimal", "skills": ["two"]},
    ]
    packs = {"one": {"git": str(one)}, "two": {"git": str(two)}}
    path = make_config(
        {"bin_dir": str(HARNESS.parent), "projects": projects, "skills": {"packs": packs}}
    )
    config = load_config(path)
    layout = Layout(config.state_dir)
    layout.make()
    _project(layout, "a", "b", "c")
    first = {
        name: _git(root, "rev-parse", "HEAD")[:12] for name, root in (("one", one), ("two", two))
    }
    lines, failed = _lines(config)
    assert failed == ["two in a"]
    assert lines[:2] == [
        f"skills: one imported at {first['one']}, 2 skills",
        f"skills: a pins one at {first['one']}",
    ]
    assert lines[2] == f"skills: two imported at {first['two']}, 2 skills"
    assert lines[3].startswith(f"skills: two at {first['two']} is not pinned in a: skills pin")
    assert "alpha already defined by one" in lines[3]
    assert lines[4:] == [
        f"skills: b pins two at {first['two']}",
        f"skills: c pins two at {first['two']}",
    ]
    # two's next commit drops alpha: b and c move, a now pins it too.
    (two / "skills/alpha/SKILL.md").unlink()
    _git(two, "commit", "-qam", "C2")
    second = _git(two, "rev-parse", "HEAD")[:12]
    lines, failed = _lines(config, update=True)
    assert failed == []
    assert f"skills: a pins two at {second}" in lines
    assert f"skills: b moves two from {first['two']} to {second}: removed alpha" in lines
    # A later commit adds gamma, which one has: a's pin cannot move, and up says so.
    path_gamma = two / "skills/gamma/SKILL.md"
    path_gamma.parent.mkdir(parents=True)
    path_gamma.write_text("---\nname: gamma\ndescription: G.\n---\nG\n")
    _git(two, "add", "-A")
    _git(two, "commit", "-qm", "C3")
    third = _git(two, "rev-parse", "HEAD")[:12]
    lines, failed = _lines(config, update=True)
    assert failed == ["two in a"]
    assert any(line.startswith(f"skills: two at {third} is not pinned in a:") for line in lines)
    lines, failed = _lines(config)
    assert f"skills: a keeps two at {second}" in lines
    assert f"skills: b keeps two at {third}" in lines


def test_a_changed_place_or_directory_is_noted(
    make_config: Any, tmp_path: Path, home: Path
) -> None:
    """Review of #435, finding 3: the note compares the recorded origin and directory."""
    first = _repo(tmp_path / "first", ["alpha"])
    other = _repo(tmp_path / "other", ["omega"])
    projects = [{"id": "crypto", "profile": "minimal", "skills": ["ss"]}]

    def configured(name: str, **pack: str) -> LauncherConfig:
        changes = {"bin_dir": str(HARNESS.parent), "projects": projects}
        return load_config(make_config({**changes, "skills": {"packs": {"ss": pack}}}, name))

    config = configured("sandbox-science.toml", git=str(first))
    Layout(config.state_dir).make()
    _project(Layout(config.state_dir), "crypto")
    assert _lines(config)[1] == []
    at = _git(first, "rev-parse", "HEAD")[:12]
    assert _lines(config)[0] == [f"skills: crypto keeps ss at {at}"]
    (moved,) = _lines(configured("other.toml", git=str(other)))[0]
    assert moved.endswith(
        "(the config names another source: sandbox-science skills update imports it)"
    )
    (pathed,) = _lines(configured("pathed.toml", git=str(first), path="elsewhere"))[0]
    assert "another path" in pathed


def test_a_pack_beside_a_built_in_skill_is_refused_at_the_first_up(
    make_config: Any, tmp_path: Path, home: Path
) -> None:
    """Review of #435, finding 4: the built-in skills are pinned before a pack is, so a
    pack with a built-in skill's name is refused at once, with the command to follow."""
    pack = _repo(tmp_path / "ss", ["pdf-reading", "alpha"])
    path = make_config(
        {
            "bin_dir": str(HARNESS.parent),
            "projects": [{"id": "crypto", "profile": "minimal", "skills": ["ss"]}],
            "skills": {"packs": {"ss": {"git": str(pack)}}},
        }
    )
    config = load_config(path)
    Layout(config.state_dir).make()
    _project(Layout(config.state_dir), "crypto")
    lines, failed = _lines(config)
    assert failed == ["ss in crypto"]
    assert "already defined by the built-in skills" in lines[1]
    assert "--exclude builtin/pdf-reading" in lines[1]


def test_an_update_that_would_embed_needs_the_key_file_first(
    make_config: Any, pack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review of #435, finding 2 (P7): nothing is imported when the key file is missing."""
    path = make_config(
        {
            "bin_dir": str(HARNESS.parent),
            "model": {"api_key_file": "gone.key"},
            "projects": [{"id": "crypto", "profile": "minimal", "skills": ["ss"]}],
            "skills": {"embedding_model": "embed", "packs": {"ss": {"git": str(pack)}}},
        }
    )
    config = load_config(path)
    Layout(config.state_dir).make()
    assert cli.main(["skills", "update", "--config", str(path)]) == 1
    assert "cannot embed the skills: no model key file at" in capsys.readouterr().err
    assert _imports(config) == []
