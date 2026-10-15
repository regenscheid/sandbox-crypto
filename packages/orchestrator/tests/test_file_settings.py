# SPDX-License-Identifier: MIT
"""A project's file settings: recording all files or results only, and ignore patterns
(research artifacts, RA-29, RA-39)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.documents import USER, DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.file_settings import DEFAULT_IGNORE, IgnoreRules, ProjectFiles
from sandbox_science.orchestrator.projects import ProjectStore

DEFAULTS = IgnoreRules(DEFAULT_IGNORE)


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        (".cache/pip/wheels/x.whl", True),
        ("notes/.cache/y", True),  # a directory pattern matches at any depth
        ("cache/x", False),
        ("pkg/__pycache__/mod.cpython-312.pyc", True),
        ("mod.pyc", True),
        ("build/out.o", True),
        ("src/build/out.o", True),
        ("build.py", False),
        ("tmp.txt", False),  # tmp/ names a directory
        ("tmp/scratch.txt", True),
        ("notes.tmp", True),
        (".jobs/job-1/stdout", True),
        ("draft.md~", True),
        ("shared/.cache/x", True),  # matched inside shared/
        ("shared/bibliography.bib", False),
        ("notes/summary.md", False),
        ("data/results.csv", False),
        ("x.egg-info/PKG-INFO", True),
    ],
)
def test_the_default_patterns(path: str, ignored: bool) -> None:
    assert DEFAULTS.ignores(path) is ignored


@pytest.mark.parametrize(
    ("pattern", "matches", "misses"),
    [
        ("data/raw/", ["data/raw/a.h5", "data/raw/b/c"], ["x/data/raw/a", "data/raw"]),
        ("/top.txt", ["top.txt"], ["a/top.txt"]),
        ("**/out/*.csv", ["out/a.csv", "a/b/out/c.csv"], ["out/a/b.csv", "out/a.txt"]),
        ("docs/**/*.md", ["docs/a.md", "docs/x/y/b.md"], ["a/docs/b.md"]),
        ("[ab].txt", ["a.txt", "q/b.txt"], ["c.txt", "ab.txt"]),
        ("[!ab].txt", ["c.txt"], ["a.txt"]),
        ("logs", ["logs", "logs/x", "a/logs/y"], ["logs2"]),
        ("a?c", ["abc"], ["ac", "a/c"]),
    ],
)
def test_patterns_follow_gitignore(pattern: str, matches: list[str], misses: list[str]) -> None:
    rules = IgnoreRules([pattern])
    assert [rules.ignores(p) for p in matches] == [True] * len(matches)
    assert [rules.ignores(p) for p in misses] == [False] * len(misses)


@pytest.fixture
def files(tmp_path: Path) -> Iterator[ProjectFiles]:
    ProjectStore(tmp_path).create("p1", "minimal", "sha256:" + "a" * 64)
    documents = DocumentStore(store_path(tmp_path))
    yield ProjectFiles(documents)
    documents.close()


def test_only_the_user_changes_them_with_the_revision_read(files: ProjectFiles) -> None:
    assert files.get("p1").model_dump() == {
        "project": "p1", "revision": 0, "recording": "all", "ignore": DEFAULT_IGNORE,
    }  # fmt: skip
    change = {"expected": 0, "recording": "results", "ignore": ["*.log"], "operation_id": "o1"}
    agent = Author(kind="agent", run_id="r1", session_id="s1", role="main")
    with pytest.raises(DocumentError, match="only the user"):
        files.change("p1", change, agent)
    changed, replayed = files.change("p1", change, USER)
    assert (changed.revision, changed.recording, changed.ignore, replayed) == (
        1, "results", ("*.log",), False,
    )  # fmt: skip
    assert files.change("p1", change, USER) == (changed, True)  # a retry
    with pytest.raises(DocumentError, match="changed meanwhile"):
        files.change("p1", {**change, "operation_id": "o2"}, USER)
    with pytest.raises(DocumentError, match="another"):
        files.change("p1", {**change, "recording": "all"}, USER)  # o1 named another change
    assert files.get("p2").revision == 0  # each project its own


@pytest.mark.parametrize("pattern", ["!keep.txt", "a//b", "[abc", "x/../y", "", "a\nb"])
def test_a_pattern_that_cannot_mean_anything_is_refused(files: ProjectFiles, pattern: str) -> None:
    change = {"expected": 0, "recording": "all", "ignore": [pattern], "operation_id": "o1"}
    with pytest.raises(DocumentError):
        files.change("p1", change, USER)
    assert files.get("p1").revision == 0


def test_the_command_line_shows_and_changes_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ProjectStore(tmp_path).create("p1", "minimal", "sha256:" + "a" * 64)
    base = ["project", "files", "--state-dir", str(tmp_path), "--project", "p1"]
    assert cli.main(base) == 0
    assert '"revision": 0' in capsys.readouterr().out
    assert cli.main([*base, "--recording", "results"]) == 1
    assert "--expected" in capsys.readouterr().err
    change = [*base, "--expected", "0", "--operation-id", "o1", "--ignore-defaults"]
    assert cli.main([*change, "--ignore", "*.h5", "--recording", "results"]) == 0
    shown = capsys.readouterr().out
    assert '"recording": "results"' in shown
    assert '"*.h5"' in shown
    assert '".cache/"' in shown
    again = [*base, "--expected", "1", "--operation-id", "o2", "--no-ignore"]
    assert cli.main(again) == 0
    assert '"ignore": []' in capsys.readouterr().out


def test_a_name_built_to_backtrack_is_matched_quickly() -> None:
    rules = IgnoreRules(["*-*-*.log", "**/a*a*a*a*a*b/"])
    name = "-" * 250 + "x"
    deep = "/".join(["a" * 250] * 16) + "/c"
    started = time.monotonic()
    for _ in range(100):
        assert not rules.ignores(name)
        assert not rules.ignores(deep)
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("path", ["x" * 256, "a/" + "é" * 128, "a//b", "../a", "", "shared/"])
def test_a_path_with_an_unusable_name_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match="not a path"):
        DEFAULTS.ignores(path)


def test_settings_too_large_or_with_a_backwards_class_are_invalid(files: ProjectFiles) -> None:
    base = {"expected": 0, "recording": "all", "operation_id": "o1"}
    with pytest.raises(DocumentError) as backwards:
        files.change("p1", {**base, "ignore": ["[z-a].txt"]}, USER)
    with pytest.raises(DocumentError) as large:
        files.change("p1", {**base, "ignore": ["é" * 256] * 200}, USER)
    assert backwards.value.kind == large.value.kind == "invalid_request"
    assert files.get("p1").revision == 0
