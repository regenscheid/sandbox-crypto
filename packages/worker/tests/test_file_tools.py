# SPDX-License-Identifier: MIT
"""The file tools' sandbox side, in this process: ``edit_file`` (sandbox_files.py) and the
searches (sandbox_search.py), on real files, with their path rules and bounds (tools spec,
Files 7 to 10). The executor's and the worker API's part is in test_file_tools_service.py."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import EditFileRequest, ErrorKind, ExecutionContext, GlobRequest
from sandbox_science.worker import files as worker_files
from sandbox_science.worker import recovery_fence, sandbox_files, sandbox_search
from sandbox_science.worker.channel import ExecutorChannel
from sandbox_science.worker.errors import WorkerError

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")


def _requests(root: Path) -> sandbox_files.Requests:
    (root / "sessions" / "s1").mkdir(parents=True, exist_ok=True)
    return sandbox_files.Requests(root, max_read=1024, max_list=100)


def _edit(requests: sandbox_files.Requests, **fields: Any) -> dict[str, Any]:
    return asyncio.run(requests.edit({"session": "s1", **fields}))


def _refused(requests: sandbox_files.Requests, **fields: Any) -> sandbox_files.ToolError:
    with pytest.raises(sandbox_files.ToolError) as caught:
        _edit(requests, **fields)
    return caught.value


# --- edit_file ------------------------------------------------------------------------------


def test_an_edit_replaces_the_one_occurrence_and_reports_the_new_hash(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    target = tmp_path / "sessions" / "s1" / "notes" / "a.md"
    target.parent.mkdir()
    target.write_text("alpha\nbeta\ngamma\n")
    reply = _edit(requests, path="notes/a.md", old_string="beta", new_string="BETA")
    assert target.read_text() == "alpha\nBETA\ngamma\n"
    assert reply["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert (reply["size"], reply["replacements"]) == (len(target.read_bytes()), 1)
    # No temporary file is left beside it.
    assert sorted(os.listdir(target.parent)) == ["a.md"]


def test_an_ambiguous_or_missing_string_changes_nothing(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    target = tmp_path / "sessions" / "s1" / "a.txt"
    target.write_text("x = 1\nx = 1\n")
    ambiguous = _refused(requests, path="a.txt", old_string="x = 1", new_string="x = 2")
    assert ambiguous.kind == "invalid_request"
    assert ambiguous.message.startswith("ambiguous: old_string occurs 2 times")
    missing = _refused(requests, path="a.txt", old_string="y = 1", new_string="y = 2")
    assert missing.kind == "not_found"
    assert missing.message.startswith("not_found: old_string does not occur")
    assert target.read_text() == "x = 1\nx = 1\n"
    reply = _edit(requests, path="a.txt", old_string="x = 1", new_string="x = 2", replace_all=True)
    assert reply["replacements"] == 2
    assert target.read_text() == "x = 2\nx = 2\n"


def test_an_edit_needs_an_existing_text_file_within_the_limit(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    workspace = tmp_path / "sessions" / "s1"
    assert _refused(requests, path="none.txt", old_string="a", new_string="b").kind == "not_found"
    (workspace / "bin.dat").write_bytes(b"a\x00b")
    binary = _refused(requests, path="bin.dat", old_string="a", new_string="b")
    assert (binary.kind, "not UTF-8 text" in binary.message) == ("invalid_request", True)
    (workspace / "latin.txt").write_bytes(b"caf\xe9")
    assert (
        "not UTF-8" in _refused(requests, path="latin.txt", old_string="c", new_string="C").message
    )
    (workspace / "big.txt").write_text("a" * 1025)
    large = _refused(requests, path="big.txt", old_string="a", new_string="b", replace_all=True)
    assert "larger than the 1024 bytes" in large.message
    (workspace / "near.txt").write_text("a" * 1000)
    grown = _refused(requests, path="near.txt", old_string="a", new_string="bb", replace_all=True)
    assert "would be larger" in grown.message
    (workspace / "dir").mkdir()
    assert (
        "not a regular file"
        in _refused(requests, path="dir", old_string="a", new_string="b").message
    )
    os.mkfifo(workspace / "pipe")
    assert (
        "not a regular file"
        in _refused(requests, path="pipe", old_string="a", new_string="b").message
    )
    same = _refused(requests, path="near.txt", old_string="a", new_string="a")
    assert same.kind == "invalid_request"


def test_an_edit_follows_the_file_specs_path_rules(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    workspace = tmp_path / "sessions" / "s1"
    (workspace / "link.txt").symlink_to(outside)
    (workspace / "linked").symlink_to(tmp_path)
    for path in ("/etc/passwd", "../outside.txt", "a/../b", "", "a//b"):
        assert _refused(requests, path=path, old_string="s", new_string="t").kind == (
            "invalid_request"
        ), path
    assert "symlink" in _refused(requests, path="link.txt", old_string="s", new_string="t").message
    assert (
        "symlink"
        in _refused(requests, path="linked/outside.txt", old_string="s", new_string="t").message
    )
    assert outside.read_text() == "secret"


def test_a_shared_edit_needs_the_hash_it_read(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    shared = tmp_path / "shared" / "notes.md"
    shared.parent.mkdir()
    shared.write_text("draft\n")
    unguarded = _refused(requests, path="shared/notes.md", old_string="draft", new_string="final")
    assert "need expected_sha256" in unguarded.message
    stale = _refused(
        requests,
        path="shared/notes.md",
        old_string="draft",
        new_string="final",
        expected_sha256="0" * 64,
    )
    assert stale.kind == "conflict"
    current = hashlib.sha256(b"draft\n").hexdigest()
    reply = _edit(
        requests,
        path="shared/notes.md",
        old_string="draft",
        new_string="final",
        expected_sha256=current,
    )
    assert shared.read_text() == "final\n"
    assert reply["sha256"] == hashlib.sha256(b"final\n").hexdigest()
    # A session file takes an optional hash, checked as well.
    (tmp_path / "sessions" / "s1" / "a.txt").write_text("one")
    assert (
        _refused(
            requests, path="a.txt", old_string="one", new_string="two", expected_sha256="1" * 64
        ).kind
        == "conflict"
    )


def test_a_change_between_the_read_and_the_write_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _requests(tmp_path)
    target = tmp_path / "sessions" / "s1" / "a.txt"
    target.write_text("one two")
    original = sandbox_files.write_file

    def racing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        target.write_text("one three")  # another writer, after the edit read the file
        return original(*args, **kwargs)

    monkeypatch.setattr(sandbox_files, "write_file", racing)
    refused = _refused(requests, path="a.txt", old_string="one", new_string="ONE")
    assert refused.kind == "conflict"
    assert target.read_text() == "one three"  # the other writer's change is not lost


def test_an_edit_keeps_the_executable_bit(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    script = tmp_path / "sessions" / "s1" / "run.sh"
    script.write_text("#!/bin/sh\necho one\n")
    script.chmod(0o755)
    _edit(requests, path="run.sh", old_string="one", new_string="two")
    assert script.stat().st_mode & 0o111
    data = tmp_path / "sessions" / "s1" / "data.txt"
    data.write_text("one")
    data.chmod(0o644)
    _edit(requests, path="data.txt", old_string="one", new_string="two")
    assert not data.stat().st_mode & 0o111


# --- glob -----------------------------------------------------------------------------------


def _search(directory: Path, name: str | None = None, **request: Any) -> dict[str, Any]:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    return sandbox_search.run({"fd": descriptor, "name": name, "seconds": 10, **request})


def _tree(root: Path) -> Path:
    workspace = root / "ws"
    for path, text in {
        "a.py": "import os\nprint('hello')\n",
        "src/b.py": "def f():\n    return 'Hello'\n",
        "src/deep/c.py": "x = 1\n",
        "src/notes.md": "hello there\n",
        "data/blob.bin": "hello\x00world",
    }.items():
        (workspace / path).parent.mkdir(parents=True, exist_ok=True)
        (workspace / path).write_text(text)
    return workspace


def test_glob_matches_path_patterns_newest_first(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    for offset, name in enumerate(("src/deep/c.py", "a.py", "src/b.py")):
        os.utime(workspace / name, (1_000_000 + offset, 1_000_000 + offset))
    found = _search(workspace, op="glob", pattern="**/*.py")
    assert [entry["path"] for entry in found["entries"]] == ["src/b.py", "a.py", "src/deep/c.py"]
    assert found["truncated"] is False
    top = _search(workspace, op="glob", pattern="*.py")
    assert [entry["path"] for entry in top["entries"]] == ["a.py"]
    one = _search(workspace, op="glob", pattern="src/?.py")
    assert [entry["path"] for entry in one["entries"]] == ["src/b.py"]
    chosen = _search(workspace, op="glob", pattern="src/[!b].md")
    assert chosen["entries"] == []
    directories = _search(workspace, op="glob", pattern="src/*")
    kinds = {entry["path"]: entry["type"] for entry in directories["entries"]}
    assert kinds == {"src/b.py": "file", "src/deep": "directory", "src/notes.md": "file"}
    # Paths are the workspace's, with the searched directory before them.
    below = _search(workspace, "src", op="glob", pattern="**/*.py", prefix="src")
    assert {entry["path"] for entry in below["entries"]} == {"src/b.py", "src/deep/c.py"}


def test_glob_lists_symlinks_without_following_them(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "inner" / "secret.py").write_text("s = 1\n")
    (workspace / "escape").symlink_to(outside)
    (workspace / "file-link.py").symlink_to(outside / "inner" / "secret.py")
    found = _search(workspace, op="glob", pattern="**")
    paths = {entry["path"]: entry["type"] for entry in found["entries"]}
    assert paths["escape"] == "symlink"
    assert paths["file-link.py"] == "symlink"
    assert not any("secret" in path for path in paths)
    with pytest.raises(sandbox_search.SearchError, match="symlink"):
        _search(workspace, "escape", op="glob", pattern="**", prefix="escape")


def test_glob_is_bounded_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "many"
    workspace.mkdir()
    for n in range(30):
        (workspace / f"f{n:02d}.txt").write_text("x")
    limited = _search(workspace, op="glob", pattern="*.txt", limit=10)
    assert (len(limited["entries"]), limited["truncated"], limited["stopped"]) == (
        10,
        True,
        "results",
    )
    monkeypatch.setattr(sandbox_search, "MAX_SCAN_ENTRIES", 12)
    walked = _search(workspace, op="glob", pattern="*.txt")
    assert (len(walked["entries"]), walked["truncated"], walked["stopped"]) == (12, True, "entries")
    assert walked["scanned"] == 12


def test_a_search_skips_the_prefix_and_the_executors_state_unless_named(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    for name in sandbox_search.SKIPPED:
        (workspace / name / "lib").mkdir(parents=True)
        (workspace / name / "lib" / "hidden.py").write_text("hello = 1\n")
    found = _search(workspace, op="glob", pattern="**/hidden.py")
    assert found["entries"] == []
    assert sorted(found["skipped"]) == sorted(sandbox_search.SKIPPED)
    hits = _search(workspace, op="grep", pattern="hello")
    assert not any("hidden" in path for path in hits["files"])
    named = _search(
        workspace,
        ".executor",
        op="grep",
        pattern="hello",
        prefix=".executor",
    )
    assert named["files"] == [".executor/lib/hidden.py"]


@pytest.mark.parametrize(
    "pattern", ["", "/abs/*", "../*.py", "a/./b", "a//b", "x" * 1001, "a\x00b"]
)
def test_a_pattern_breaking_the_path_rules_is_refused(tmp_path: Path, pattern: str) -> None:
    workspace = _tree(tmp_path)
    with pytest.raises(sandbox_search.SearchError) as caught:
        _search(workspace, op="glob", pattern=pattern)
    assert caught.value.kind == "invalid_request"


# --- grep -----------------------------------------------------------------------------------


def test_grep_modes(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    files = _search(workspace, op="grep", pattern="hello")
    assert files["files"] == ["a.py", "src/notes.md"]
    assert files["skipped_binary"] == 1  # data/blob.bin
    either = _search(workspace, op="grep", pattern="hello", case_insensitive=True)
    assert either["files"] == ["a.py", "src/b.py", "src/notes.md"]
    counts = _search(workspace, op="grep", pattern="o", mode="count")
    assert {c["path"]: c["count"] for c in counts["counts"]} == {
        "a.py": 2,
        "src/b.py": 1,
        "src/notes.md": 1,
    }
    content = _search(workspace, op="grep", pattern="return", mode="content")
    assert content["lines"] == ["src/b.py:2:     return 'Hello'"]
    around = _search(workspace, op="grep", pattern="return", mode="content", context_lines=1)
    assert around["lines"] == ["src/b.py-1- def f():", "src/b.py:2:     return 'Hello'"]
    only_py = _search(workspace, op="grep", pattern="hello", glob="*.py", case_insensitive=True)
    assert only_py["files"] == ["a.py", "src/b.py"]
    by_path = _search(workspace, op="grep", pattern=".", glob="src/**/*.py")
    assert by_path["files"] == ["src/b.py", "src/deep/c.py"]
    single = _search(workspace / "src", "b.py", op="grep", pattern="Hello", prefix="src/b.py")
    assert single["files"] == ["src/b.py"]


def test_grep_cuts_long_lines_and_marks_groups(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lines = [f"line {n}" for n in range(1, 21)]
    lines[2] = "match " + "y" * 1000
    lines[15] = "match again"
    (workspace / "f.txt").write_text("\r\n".join(lines) + "\r\n")
    found = _search(workspace, op="grep", pattern="^match", mode="content", context_lines=1)
    shown = found["lines"]
    assert shown[0] == "f.txt-2- line 2"
    assert shown[1].startswith("f.txt:3: match yyy")
    assert shown[1].endswith("…[cut]")
    assert len(shown[1]) < sandbox_search.MAX_LINE_CHARS + 40
    assert shown[3] == "--"
    assert shown[4:] == ["f.txt-15- line 15", "f.txt:16: match again", "f.txt-17- line 17"]


def test_grep_is_bounded_in_results_bytes_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for n in range(10):
        (workspace / f"f{n}.txt").write_text("hit\n" * 5)
    limited = _search(workspace, op="grep", pattern="hit", max_results=3)
    assert (limited["files"], limited["truncated"], limited["stopped"]) == (
        ["f0.txt", "f1.txt", "f2.txt"],
        True,
        "results",
    )
    exact = _search(workspace, op="grep", pattern="hit", max_results=10)
    assert (len(exact["files"]), exact["truncated"]) == (10, False)
    lines = _search(workspace, op="grep", pattern="hit", mode="content", max_results=7)
    assert (len(lines["lines"]), lines["stopped"]) == (7, "results")
    monkeypatch.setattr(sandbox_search, "MAX_SCANNED_BYTES", 50)
    stopped = _search(workspace, op="grep", pattern="nothing")
    assert (stopped["stopped"], stopped["truncated"]) == ("bytes", True)
    assert stopped["scanned_bytes"] <= 50
    monkeypatch.setattr(sandbox_search, "MAX_SCANNED_BYTES", 1 << 20)
    monkeypatch.setattr(sandbox_search, "MAX_FILE_BYTES", 10)
    large = _search(workspace, op="grep", pattern="hit")
    assert (large["files"], large["skipped_large"]) == ([], 10)
    monkeypatch.setattr(sandbox_search, "MAX_FILE_BYTES", 1 << 20)
    monkeypatch.setattr(sandbox_search, "MAX_REPLY_BYTES", 40)
    reply = _search(workspace, op="grep", pattern="hit", mode="content")
    assert (reply["stopped"], reply["truncated"]) == ("reply", True)


def test_grep_stops_at_its_time(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "f.txt").write_text("hit\n" * 5000)
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    found = sandbox_search.run(
        {"fd": descriptor, "name": None, "seconds": 1e-9, "op": "grep", "pattern": "hit"}
    )
    assert (found["stopped"], found["truncated"], found["files"]) == ("time", True, [])


def test_grep_follows_no_symlink(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("hello secret\n")
    (workspace / "link.txt").symlink_to(outside / "secret.txt")
    (workspace / "dirlink").symlink_to(outside)
    found = _search(workspace, op="grep", pattern="secret")
    assert found["files"] == []
    with pytest.raises(sandbox_search.SearchError, match="symlink"):
        _search(workspace, "link.txt", op="grep", pattern="secret", prefix="link.txt")


@pytest.mark.parametrize(
    ("request_fields", "message"),
    [
        ({"pattern": "("}, "not a regular expression"),
        ({"pattern": "a", "mode": "lines"}, "mode must be"),
        ({"pattern": "a", "context_lines": 11}, "context_lines"),
        ({"pattern": "a", "max_results": 0}, "max_results"),
        ({"pattern": "a", "glob": "../x"}, "segment"),
    ],
)
def test_a_bad_grep_request_is_refused(
    tmp_path: Path, request_fields: dict[str, Any], message: str
) -> None:
    workspace = _tree(tmp_path)
    with pytest.raises(sandbox_search.SearchError, match=message) as caught:
        _search(workspace, op="grep", **request_fields)
    assert caught.value.kind == "invalid_request"


def test_a_missing_path_is_not_found(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    with pytest.raises(sandbox_search.SearchError) as caught:
        _search(workspace, "absent", op="grep", pattern="a", prefix="absent")
    assert caught.value.kind == "not_found"
    with pytest.raises(sandbox_search.SearchError, match="not a directory"):
        _search(workspace, "a.py", op="glob", pattern="*", prefix="a.py")


def test_paths_are_one_line_of_text(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "two\nlines.txt").write_text("hello\n")
    (workspace / os.fsdecode(b"bad\xff.txt")).write_text("hello\n")
    found = _search(workspace, op="grep", pattern="hello")
    assert found["files"] == ["bad�.txt", "two\\nlines.txt"]


# --- The worker's part ----------------------------------------------------------------------


class _Channel:
    def __init__(self, hello: dict[str, Any]) -> None:
        self.hello = hello


@pytest.mark.parametrize("hello", [{}, {"file_tools": True}, {"file_tools": 2}])
def test_an_older_executor_is_refused_before_anything_is_sent(hello: dict[str, Any]) -> None:
    with pytest.raises(WorkerError) as caught:
        worker_files._file_tools(cast(ExecutorChannel, _Channel(hello)))
    assert caught.value.kind is ErrorKind.UNSUPPORTED
    assert "recreate the environment" in caught.value.message
    worker_files._file_tools(cast(ExecutorChannel, _Channel({"file_tools": 1})))


def test_searches_are_passive_and_an_edit_is_an_effect() -> None:
    passive = recovery_fence._PASSIVE
    assert {"glob", "grep", "read", "list"} <= passive
    assert "edit" not in passive
    assert "write" not in passive


def test_the_contracts_hold_the_tools_rules() -> None:
    with pytest.raises(ValueError, match="the same"):
        EditFileRequest(context=CONTEXT, path="a", old_string="x", new_string="x")
    with pytest.raises(ValueError, match="at least 1 character"):
        EditFileRequest(context=CONTEXT, path="a", old_string="", new_string="x")
    with pytest.raises(ValueError, match="segment"):
        EditFileRequest(context=CONTEXT, path="../a", old_string="x", new_string="y")
    for pattern in ("/x", "a/../b", ""):
        with pytest.raises(ValueError, match="pattern"):
            GlobRequest(context=CONTEXT, pattern=pattern)
    with pytest.raises(ValueError, match="less than or equal to 200"):
        GlobRequest(context=CONTEXT, pattern="*", limit=201)
