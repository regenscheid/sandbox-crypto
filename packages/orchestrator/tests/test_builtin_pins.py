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

import json
import logging
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.control import ControlError, migrations
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
BEFORE_BUILTIN = migrations.BASELINE + migrations.MIGRATIONS.index(migrations._builtin_skills) - 1
"""The version before the built-in kind (42)."""


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
    assert _settings(harness.state, "--expected", "0", "--exclude", "other/thing") == 0
    capsys.readouterr()
    assert cli.main(["skills", "pin", *where, "--project", PROJECT, "--source", "ss"]) == 1
    error = capsys.readouterr().err
    assert "already defined by the built-in skills" in error
    # The hint, followed as given, keeps the other exclusion (review of #426).
    hint = error.split("exclude them first: skills settings ")[1].split(" (the project")[0]
    assert hint == f"--project {PROJECT} --expected 1 --exclude builtin/pdf-reading"
    assert "the project then excludes builtin/pdf-reading, other/thing" in error
    assert _settings(harness.state, *shlex.split(hint)[2:]) == 0
    assert json.loads(capsys.readouterr().out)["excluded"] == [
        "builtin/pdf-reading",
        "other/thing",
    ]
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
        # The import's report says so (SK-4).
        assert store.imports("builtin")[0].report["left_out"] == {PROJECT: ["alpha"]}
        # With the pack's alpha excluded, the run gets the built-in one, and the listing
        # agrees.
        store.set_settings(PROJECT, USER, 0, always=(), excluded=("ss/alpha",))
        found = store.effective(PROJECT)
        assert [e.qualified for e in found.entries] == ["builtin/alpha", "builtin/pdf-reading"]
        (pin,) = [pin for pin in store.pins(PROJECT) if pin.source == "builtin"]
        assert pin.left_out == ()
    finally:
        projects.close()


def test_a_run_gets_the_built_in_skills_of_its_role(shipped: Path, tmp_path: Path) -> None:
    """A delegated run gets only the built-in skills whose tools it has (builtin.ROLES)."""
    (shipped / "delegating-work").mkdir()
    (shipped / "delegating-work" / "SKILL.md").write_text(
        "---\nname: delegating-work\ndescription: Delegate.\n---\nD\n"
    )
    projects = ProjectStore(tmp_path / "state")
    projects.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    try:
        store = SkillStore(projects.control)
        store.ensure(PROJECT, builtin.builtin_pack())
        names = {
            role: [e.name for e in store.effective(PROJECT, role).entries]
            for role in ("main", "delegated")
        }
    finally:
        projects.close()
    assert names == {"main": ["delegating-work", "pdf-reading"], "delegated": ["delegating-work"]}


def test_a_harness_without_its_pack_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(builtin, "ROOT", tmp_path / "missing")
    with caplog.at_level(logging.ERROR):
        assert builtin.builtin_pack() is None
    assert "the harness ships no built-in skills at" in caplog.text


@pytest.mark.slow
def test_the_installed_wheel_reads_the_seven_built_in_skills(tmp_path: Path) -> None:
    """B1 of the review of #426: the wheel the orchestrator image installs carries the
    pack as package data, and the installed harness reads it there, without this
    directory's checkout and without the tests' fixture."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv is None:
        pytest.skip("needs uv to build the wheel")
    top = Path(__file__).resolve().parents[3]
    built = subprocess.run(
        [uv, "build", "--package", "sandbox-science-orchestrator", "--wheel",
         "--out-dir", str(tmp_path / "dist"), "--offline", "--no-create-gitignore"],
        cwd=top, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert built.returncode == 0, built.stderr
    (wheel,) = (tmp_path / "dist").glob("*.whl")
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as found:
        found.extractall(site)
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(site)!r})\n"
        "from sandbox_science.orchestrator.skills import builtin\n"
        "pack = builtin.builtin_pack()\n"
        "names = sorted({f.path.split('/')[0] for f in pack.files}) if pack else None\n"
        "print(json.dumps({'root': str(builtin.ROOT), 'names': names}))\n"
    )
    ran = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True
    )
    shown = json.loads(ran.stdout)
    assert shown["root"] == str(site / "sandbox_science/orchestrator/skills/builtin_pack")
    assert shown["names"] == sorted(builtin.ROLES)


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
        BEFORE_BUILTIN,
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


def test_a_pack_the_user_named_builtin_is_renamed_by_the_migration(
    tmp_path: Path, shipped: Path
) -> None:
    """Minor 3 of the review of #426: before version 42 a user's pack could take the name
    ``builtin``; the migration renames it, its pins and its skills in the settings, and
    the reports say so. The name then pins built-in imports only."""
    current = ProjectStore(tmp_path / "current")
    current.create(PROJECT, "minimal", "sha256:" + "a" * 64)
    store = SkillStore(current.control)
    store.add("legacy", snapshot(from_git(str(_pack(tmp_path / "pack", "alpha")))), USER)
    store.pin(PROJECT, "legacy", USER)
    store.set_settings(PROJECT, USER, 0, always=("legacy/alpha",), excluded=())
    current.close()
    (tmp_path / "old").mkdir()
    old = tmp_path / "old/control.sqlite3"
    older_store(tmp_path / "current/control.sqlite3", old, BEFORE_BUILTIN)
    with sqlite3.connect(old) as db:  # as a K1-era import under the name would have it
        guards = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE name IN "
            "('skill_imports_no_update','project_skill_pins_forward')"
        ).fetchall()
        for name, _ in guards:
            db.execute(f'DROP TRIGGER "{name}"')
        db.execute("UPDATE skill_imports SET source='builtin'")
        db.execute("UPDATE project_skill_pins SET source='builtin'")
        db.execute("UPDATE project_skill_settings SET always='[\"builtin/alpha\"]'")
        for _, sql in guards:
            db.execute(sql)
    migrated = ProjectStore(tmp_path / "old")
    try:
        store = SkillStore(migrated.control)
        (renamed,) = store.imports("builtin-user")
        assert renamed.kind == "git"
        assert renamed.report["warnings"] == [
            "renamed from builtin to builtin-user by version 42: builtin names the "
            "platform's own skills"
        ]
        assert [pin.source for pin in store.pins(PROJECT)] == ["builtin-user"]
        assert store.settings(PROJECT).always == ("builtin-user/alpha",)
        with pytest.raises(ControlError, match="no import of builtin"):
            store.pin(PROJECT, "builtin", USER)
        store.ensure(PROJECT, builtin.builtin_pack())
        assert [pin.pinned.kind for pin in store.pins(PROJECT)] == ["builtin", "git"]
    finally:
        migrated.close()
    with sqlite3.connect(old) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO skill_imports(source,snapshot,kind,origin,ref,commit_id,root,report,"
            "author,created) SELECT 'builtin',snapshot,'git',origin,NULL,commit_id,root,report,"
            "author,created FROM skill_imports WHERE kind='git'"
        )
