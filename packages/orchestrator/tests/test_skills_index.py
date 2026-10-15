# SPDX-License-Identifier: MIT
"""A run's skills (docs/specs/skills.md, SK-7, SK-10 to SK-13, SK-21, SK-22, SK-33, SK-38,
SK-39, SK-42; SK-F1 to SK-F4, SK-F8, SK-F19): the index in the system message with its
pin, the ``skills`` tool's search, loads and reads with their records, and the project's
skills loaded at every run's start or excluded.

The pack is ``ss-test-skills``, written here under the MIT License, in a temporary git
repository: ``main`` at C1, then C2, which changes ``alpha``'s body.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.prompts import PromptPins
from sandbox_science.orchestrator.skills import Entry, SkillStore, from_git, snapshot
from sandbox_science.orchestrator.skills import tool as skills_tool
from sandbox_science.orchestrator.skills.index import FRAMING, index

from .loop_harness import PROJECT, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker

ALPHA = "---\nname: alpha\ndescription: Says the marker.\n---\nSS-MARKER-ALPHA-1\n"
BETA = (
    "---\nname: beta\ndescription: Counts the lines of the notes.\n---\n"
    "Read reference/notes.md and ../alpha/SKILL.md; SS-MARKER-BETA-1.\n"
)


@pytest.fixture(autouse=True)
def plain_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    root = tmp_path / "ss-test-skills"
    files = {
        "skills/alpha/SKILL.md": ALPHA,
        "skills/beta/SKILL.md": BETA,
        "skills/beta/reference/notes.md": "one\ntwo\n",
    }
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    (root / "skills/beta/reference/link").symlink_to("/etc/hostname")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "C1")
    return root


def _pin(state: Path, pack: Path, ref: str | None = None) -> None:
    projects = ProjectStore(state)
    try:
        store = SkillStore(projects.control)
        store.add("ss", snapshot(from_git(str(pack), ref)), USER)
        store.pin(PROJECT, "ss", USER)
    finally:
        projects.close()


def _system(record: RunRecord) -> str:
    return cast(str, record.messages[0]["content"])


def _skill_records(record: RunRecord) -> list[dict[str, Any]]:
    return [cast(dict[str, Any], entry["skills"]) for entry in record.tools if "skills" in entry]


def _results(record: RunRecord) -> list[str]:
    return [cast(str, m["content"]) for m in record.messages if m.get("role") == "tool"]


async def test_the_index_lists_skills_without_bodies(harness: Harness, pack: Path) -> None:
    """SK-F1."""
    _pin(harness.state, pack)
    record = await harness.run("run-1", [Reply("Done.")])
    system = _system(record)
    assert FRAMING in system
    assert "- ss/alpha: Says the marker." in system
    assert "- ss/beta: Counts the lines of the notes." in system
    assert "SS-MARKER" not in json.dumps(record.messages)
    pins = record.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert pins.skills is not None
    commit = _git(pack, "rev-parse", "HEAD")
    assert (pins.skills.sources, pins.skills.skills, pins.skills.shortening) == (
        (f"ss@{commit}",),
        2,
        "none",
    )
    assert "skills" in pins.tools.names
    assert _skill_records(record) == []


async def test_a_skill_loads_once_and_narration_is_not_evidence(
    harness: Harness, pack: Path
) -> None:
    """SK-F2 and SK-F3."""
    _pin(harness.state, pack)
    script = [
        Reply(calls=[call("skills", action="load", names=["alpha"])]),
        Reply(calls=[call("skills", action="load", names=["ss/alpha"])]),
        Reply("I followed ss/beta and ss/alpha."),
    ]
    record = await harness.run("run-1", script)
    first, second = _skill_records(record)
    (loaded,) = first["loads"]
    assert (loaded["id"], loaded["status"], first["role"], first["alias"]) == (
        "ss/alpha",
        "loaded",
        "main",
        "main",
    )
    alpha = hashlib.sha256(b"SS-MARKER-ALPHA-1\n").hexdigest()
    assert (loaded["body_sha256"], loaded["served_sha256"]) == (alpha, alpha)
    assert loaded["revision"] == _git(pack, "rev-parse", "HEAD")
    assert second["loads"][0]["status"] == "already_loaded"
    assert sum(text.count("SS-MARKER-ALPHA-1") for text in _results(record)) == 1
    assert "ss/beta" not in json.dumps(_skill_records(record))


async def test_search_says_why_and_resources_stay_in_the_pack(harness: Harness, pack: Path) -> None:
    """SK-F4 and the search's reasons."""
    _pin(harness.state, pack)
    script = [
        Reply(calls=[call("skills", action="search", query="count lines in notes")]),
        Reply(calls=[call("skills", action="search", query="elliptic curves")]),
        Reply(calls=[call("skills", action="read", skill="beta", path="reference/notes.md")]),
        Reply(calls=[call("skills", action="read", skill="beta", path="../alpha/SKILL.md")]),
        Reply(calls=[call("skills", action="read", skill="beta", path="../../../../etc/passwd")]),
        Reply(calls=[call("skills", action="read", skill="beta", path="reference/link")]),
        Reply("Done."),
    ]
    record = await harness.run("run-1", script)
    found, missed, notes, sibling, escape, link = _results(record)
    assert json.loads(found)["found"][0]["id"] == "ss/beta"
    assert json.loads(found)["found"][0]["matched"] == ["count", "line", "note"]
    assert json.loads(missed) == {
        "searched": 2,
        "found": [],
        "note": "no skill matched; this covers only this search",
    }
    assert "one\ntwo" in notes
    assert "SS-MARKER-ALPHA-1" in sibling
    assert json.loads(escape)["error"]["kind"] == "resource_outside_pack"
    assert json.loads(link)["error"]["kind"] == "not_found"
    reads = _skill_records(record)[2:]
    assert [r["status"] for r in reads] == ["ok", "ok", "resource_outside_pack", "not_found"]
    assert reads[0]["path"] == "beta/reference/notes.md"
    notes = hashlib.sha256(b"one\ntwo\n").hexdigest()
    assert (reads[0]["file_sha256"], reads[0]["served_sha256"]) == (notes, notes)


def _settings(state: Path, *args: str) -> None:
    argv = ["skills", "settings", "--state-dir", str(state), "--project", PROJECT, *args]
    assert cli.main(argv) == 0


async def test_skills_loaded_at_start_count_toward_the_cap(
    harness: Harness, pack: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SK-F19: the project's always-loaded skill sits between the index's markers, and a
    load past the run's budget is refused."""
    _pin(harness.state, pack)
    _settings(harness.state, "--expected", "0", "--always", "ss/alpha")
    # beta's body alone fits, and alpha's with it does not.
    monkeypatch.setattr(skills_tool, "MAX_LOADED", 70)
    record = await harness.run(
        "run-1", [Reply(calls=[call("skills", action="load", names=["beta"])]), Reply("Done.")]
    )
    system = _system(record)
    assert 'id="ss/alpha"' in system
    assert "SS-MARKER-ALPHA-1" in system
    pins = record.prompt_provenance
    assert isinstance(pins, PromptPins)
    assert pins.skills is not None
    assert pins.skills.always[0].startswith("ss/alpha@")
    (load,) = _skill_records(record)
    assert load["loads"][0]["status"] == "over_budget"
    # The start's load is evidence too, with the model that first answered (SK-21, SK-38).
    (started,) = cast(dict[str, Any], record.summaries["skills"])["loads"]
    alpha = hashlib.sha256(b"SS-MARKER-ALPHA-1\n").hexdigest()
    assert started == {
        "trigger": "start",
        "id": "ss/alpha",
        "revision": _git(pack, "rev-parse", "HEAD"),
        "role": "main",
        "alias": "main",
        "body_sha256": alpha,
        "served_sha256": alpha,
        "status": "loaded",
        "model": "scripted-main",
    }


async def test_a_skill_past_the_cap_at_the_start_is_named_and_recorded(
    harness: Harness, pack: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SK-39: a skill the project loads at the start that would pass the cap is left out,
    with a line in the section and an ``over_budget`` record."""
    _pin(harness.state, pack)
    _settings(harness.state, "--expected", "0", "--always", "ss/beta", "--always", "ss/alpha")
    monkeypatch.setattr(skills_tool, "MAX_LOADED", 70)
    record = await harness.run("run-1", [Reply("Done.")])
    system = _system(record)
    assert "SS-MARKER-BETA-1" in system
    assert "SS-MARKER-ALPHA-1" not in system
    assert "Not loaded at the start, as they would pass the run's 70 characters" in system
    assert "of skill text: ss/alpha." in system
    started = cast(dict[str, Any], record.summaries["skills"])["loads"]
    assert [(item["id"], item["status"]) for item in started] == [
        ("ss/beta", "loaded"),
        ("ss/alpha", "over_budget"),
    ]
    assert "body_sha256" not in started[1]


async def test_an_excluded_skill_is_left_out(harness: Harness, pack: Path) -> None:
    _pin(harness.state, pack)
    _settings(harness.state, "--expected", "0", "--exclude", "ss/beta")
    record = await harness.run(
        "run-1", [Reply(calls=[call("skills", action="load", names=["beta"])]), Reply("Done.")]
    )
    assert "ss/beta" not in _system(record)
    (load,) = _skill_records(record)
    assert load["status"] == "not_found"


async def test_a_run_keeps_the_skills_it_started_with(harness: Harness, pack: Path) -> None:
    """SK-F8: the pin moves during run 1, which still loads the C1 body; run 2 gets C2."""
    _pin(harness.state, pack)
    first = _git(pack, "rev-parse", "HEAD")
    (pack / "skills/alpha/SKILL.md").write_text(ALPHA.replace("ALPHA-1", "ALPHA-2"))
    _git(pack, "commit", "-qam", "C2")

    async def move() -> None:
        _pin(harness.state, pack)

    load = call("skills", action="load", names=["alpha"])
    one = await harness.run("run-1", [Reply(calls=[load], before=move), Reply("Done.")])
    two = await harness.run("run-2", [Reply(calls=[load]), Reply("Done.")])
    assert "SS-MARKER-ALPHA-1" in _results(one)[0]
    assert "SS-MARKER-ALPHA-2" in _results(two)[0]
    (old,), (new,) = (_skill_records(one)[0]["loads"], _skill_records(two)[0]["loads"])
    assert old["revision"] == first
    assert new["revision"] != first
    assert old["body_sha256"] != new["body_sha256"]


def test_the_index_is_shortened_deterministically() -> None:
    entries = [
        Entry("pack", f"skill-{i:03d}", "word " * 60, "0" * 64, "s", "r") for i in range(100)
    ]
    full, how = index(entries, 1_000_000)
    assert how == "none"
    assert full[0] == "- pack/skill-000: " + "word " * 59 + "word "
    short, how = index(entries, 12_000)
    assert how == "descriptions"
    assert short == index(entries, 12_000)[0]
    assert all(line.endswith("…") for line in short)
    names, how = index(entries, 2_000)
    assert how == "names"
    assert names[0].startswith("- pack: skill-000, skill-001")
    counts, how = index(entries, 100)
    assert (counts, how) == (["- pack: 100 skills"], "counts")


def test_settings_need_the_revision_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    ProjectStore(state).create(PROJECT, "minimal", "sha256:" + "a" * 64)
    base = ["skills", "settings", "--state-dir", str(state), "--project", PROJECT]
    assert cli.main(base) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 0
    assert cli.main([*base, "--expected", "0", "--always", "ss/alpha"]) == 0
    assert json.loads(capsys.readouterr().out)["always"] == ["ss/alpha"]
    assert cli.main([*base, "--expected", "0", "--exclude", "ss/beta"]) == 1
    assert "conflict" in capsys.readouterr().err
    assert cli.main([*base, "--expected", "1", "--exclude", "ss/beta", "--no-always"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["revision"], shown["always"], shown["excluded"]) == (2, [], ["ss/beta"])
    assert cli.main([*base, "--expected", "2", "--always", "not an id"]) == 1
    capsys.readouterr()
    # Without the revision a change is refused, naming it and the lists it would make.
    assert cli.main([*base, "--exclude", "ss/gamma"]) == 1
    said = capsys.readouterr().err
    assert "give --expected 2" in said
    assert "excluded then ['ss/beta', 'ss/gamma']" in said
    # --exclude adds to the list, and --include takes from it.
    assert cli.main([*base, "--expected", "2", "--exclude", "ss/gamma"]) == 0
    assert json.loads(capsys.readouterr().out)["excluded"] == ["ss/beta", "ss/gamma"]
    assert cli.main([*base, "--expected", "3", "--include", "ss/beta", "--always", "s/a"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["always"], shown["excluded"]) == (["s/a"], ["ss/gamma"])
    assert cli.main([*base, "--expected", "4", "--not-always", "s/a"]) == 0
    assert json.loads(capsys.readouterr().out)["always"] == []
