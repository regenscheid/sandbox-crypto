# SPDX-License-Identifier: MIT
"""The built-in skills' import and default pin (docs/specs/skills.md, SK-4, SK-40;
SK-F22, SK-F23): an ordinary Markdown pack under ``skills/builtin/``, imported and pinned
at a run's start like any pack, each skill excludable; a pack that collides with one is
refused until the user excludes it. ``test_builtin_skills.py`` checks the shipped pack
itself (SK-41).

The built-in skills here are test skills written under the MIT License in a temporary
directory, standing in for the repository's.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.control import migrations
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.prompts import PromptPins
from sandbox_science.orchestrator.skills import SkillStore, builtin, from_git, snapshot

from .control_versions import older_store
from .loop_harness import PROJECT, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

PDF = "---\nname: pdf-reading\ndescription: Read a long PDF with the PDF tools.\n---\n"


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "builtin"
    (root / "pdf-reading").mkdir(parents=True)
    (root / "pdf-reading" / "SKILL.md").write_text(PDF + "PDF-BODY-V1\n")
    monkeypatch.setattr(builtin, "ROOT", root)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return root


def _system(record: RunRecord) -> str:
    return cast(str, record.messages[0]["content"])


def _settings(state: Path, *args: str) -> int:
    return cli.main(["skills", "settings", "--state-dir", str(state), "--project", PROJECT, *args])


async def test_built_in_skills_are_pinned_excluded_and_upgraded(
    harness: Harness, shipped: Path
) -> None:
    """SK-F22."""
    one = await harness.run("run-1", [Reply("Done.")])
    assert "- builtin/pdf-reading: Read a long PDF with the PDF tools." in _system(one)
    pins = one.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert pins.skills is not None
    (source,) = pins.skills.sources
    assert source.startswith("builtin@0.1.0+")
    assert _settings(harness.state, "--expected", "0", "--exclude", "builtin/pdf-reading") == 0
    two = await harness.run("run-2", [Reply("Done.")])
    assert "pdf-reading" not in _system(two)
    (shipped / "pdf-reading" / "SKILL.md").write_text(PDF + "PDF-BODY-V2\n")
    assert _settings(harness.state, "--expected", "1", "--no-exclude") == 0
    three = await harness.run("run-3", [Reply("Done.")])
    later = three.prompt_provenance
    assert isinstance(later, PromptPins)
    assert later.skills is not None
    assert later.skills.sources != pins.skills.sources
    projects = ProjectStore(harness.state)
    try:
        imports = SkillStore(projects.control).imports("builtin")
    finally:
        projects.close()
    assert [(found.kind, found.commit) for found in imports] == [("builtin", None)] * 2
    assert imports[0].ref != imports[1].ref
    assert "- builtin/pdf-reading:" in _system(three)


def _pack(root: Path, name: str) -> Path:
    root.mkdir()
    files = {
        f"skills/{name}/SKILL.md": f"---\nname: {name}\ndescription: A pack's {name}.\n---\nPACK\n"
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    for args in (
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "one"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


async def test_a_pack_beside_a_built_in_needs_the_built_in_excluded(
    harness: Harness, shipped: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SK-F23."""
    await harness.run("run-1", [Reply("Done.")])
    pack = _pack(tmp_path / "pack", "pdf-reading")
    where = ["--state-dir", str(harness.state)]
    assert cli.main(["skills", "import", *where, "--name", "ss", "--git", str(pack)]) == 0
    capsys.readouterr()
    assert cli.main(["skills", "pin", *where, "--project", PROJECT, "--source", "ss"]) == 1
    error = capsys.readouterr().err
    assert "already defined by the built-in skills" in error
    assert "--exclude builtin/pdf-reading" in error
    assert _settings(harness.state, "--expected", "0", "--exclude", "builtin/pdf-reading") == 0
    assert cli.main(["skills", "pin", *where, "--project", PROJECT, "--source", "ss"]) == 0
    record = await harness.run("run-2", [Reply("Done.")])
    assert "- ss/pdf-reading: A pack's pdf-reading." in _system(record)
    assert "builtin/pdf-reading" not in _system(record)


def test_a_new_built_in_gives_way_to_a_pinned_pack(shipped: Path, tmp_path: Path) -> None:
    """A built-in version that adds a name a pinned pack has leaves its skill out."""
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    store = SkillStore(projects.control)
    try:
        store.add("ss", snapshot(from_git(str(_pack(tmp_path / "pack", "alpha")))), USER)
        store.pin(PROJECT, "ss", USER)
        (shipped / "alpha").mkdir()
        (shipped / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Built in.\n---\nB\n"
        )
        store.ensure(PROJECT, builtin.builtin_pack())
        found = store.effective(PROJECT)
        assert [e.qualified for e in found.entries] == ["builtin/pdf-reading", "ss/alpha"]
        (pin,) = [pin for pin in store.pins(PROJECT) if pin.source == "builtin"]
        assert pin.left_out == ("alpha",)
    finally:
        projects.close()


def test_the_built_in_source_name_is_reserved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    ProjectStore(state).create(PROJECT, "minimal", "sha256:" + "a" * 64)
    pack = _pack(tmp_path / "pack", "alpha")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        argv = [
            "skills",
            "import",
            "--state-dir",
            str(state),
            "--name",
            "builtin",
            "--git",
            str(pack),
        ]
        assert cli.main(argv) == 1
    assert "names the built-in skills only" in capsys.readouterr().err


def test_a_store_before_the_built_in_kind_keeps_its_imports(tmp_path: Path, shipped: Path) -> None:
    current = ProjectStore(tmp_path / "current")
    current.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    SkillStore(current.control).add(
        "ss", snapshot(from_git(str(_pack(tmp_path / "pack", "alpha")))), USER
    )
    current.close()
    (tmp_path / "old").mkdir()
    older_store(
        tmp_path / "current/control.sqlite3",
        tmp_path / "old/control.sqlite3",
        migrations.VERSION - 1,
    )
    migrated = ProjectStore(tmp_path / "old")
    try:
        store = SkillStore(migrated.control)
        assert [found.source for found in store.imports()] == ["ss"]
        store.ensure(PROJECT, builtin.builtin_pack())
        assert [found.kind for found in store.imports()] == ["builtin", "git"]
    finally:
        migrated.close()
    with sqlite3.connect(tmp_path / "old/control.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE skill_imports SET origin='elsewhere'")
