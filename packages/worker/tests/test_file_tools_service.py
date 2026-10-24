# SPDX-License-Identifier: MIT
"""The file tools through the executor and the worker API (tools spec, Files 7 to 10):
``edit``, ``glob`` and ``grep`` in a real executor, its search helper process and its
time bound, the shared files' sync around an edit, and an older executor."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.worker import executor

from .harness import Harness

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]


class Recorder:
    """An in-process executor whose replies are kept."""

    def __init__(self, root: Path) -> None:
        self.messages: list[dict[str, Any]] = []
        self.executor = executor.Executor(root, self.send)

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def ask(self, **request: Any) -> dict[str, Any]:
        await self.executor.handle({"id": len(self.messages) + 1, "session": "s1", **request})
        return self.messages[-1]


@pytest.fixture
def recorder(tmp_path: Path) -> Recorder:
    workspace = tmp_path / "sessions" / "s1"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("def f():\n    return 1\n")
    (workspace / "notes.md").write_text("hello\n")
    return Recorder(tmp_path)


async def test_hello_says_the_executor_has_the_file_tools(recorder: Recorder) -> None:
    hello = await recorder.ask(op="hello")
    assert hello["file_tools"] == executor.FILE_TOOLS == 1


async def test_the_executor_edits_globs_and_greps(recorder: Recorder, tmp_path: Path) -> None:
    edited = await recorder.ask(
        op="edit", path="src/a.py", old_string="return 1", new_string="return 2"
    )
    assert edited["ok"] is True, edited
    assert edited["replacements"] == 1
    text = (tmp_path / "sessions" / "s1" / "src" / "a.py").read_bytes()
    assert edited["sha256"] == hashlib.sha256(text).hexdigest()
    found = await recorder.ask(op="glob", pattern="**/*.py")
    assert found["ok"] is True, found
    assert [entry["path"] for entry in found["entries"]] == ["src/a.py"]
    below = await recorder.ask(op="glob", path="src", pattern="*")
    assert [entry["path"] for entry in below["entries"]] == ["src/a.py"]
    lines = await recorder.ask(op="grep", pattern="return", mode="content")
    assert lines["lines"] == ["src/a.py:2:     return 2"]
    one = await recorder.ask(op="grep", path="notes.md", pattern="hel+o")
    assert one["files"] == ["notes.md"]
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "s.txt").write_text("hello shared\n")
    shared = await recorder.ask(op="grep", path="shared", pattern="hello")
    assert shared["files"] == ["shared/s.txt"]


async def test_the_executor_refuses_paths_outside_the_areas(
    recorder: Recorder, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("hello secret\n")
    (tmp_path / "sessions" / "s1" / "escape").symlink_to(outside)
    for path in ("/etc", "..", "../s2", "src/../..", "escape", "escape/secret.txt"):
        for op, fields in (
            ("glob", {"pattern": "**"}),
            ("grep", {"pattern": "secret"}),
            ("edit", {"old_string": "hello", "new_string": "bye"}),
        ):
            reply = await recorder.ask(op=op, path=path, **fields)
            assert reply["ok"] is False, (op, path, reply)
            assert reply["error"]["kind"] == "invalid_request", (op, path, reply)
    assert (outside / "secret.txt").read_text() == "hello secret\n"
    missing = await recorder.ask(op="grep", path="absent/deeper", pattern="x")
    assert missing["error"]["kind"] == "not_found"


async def test_a_search_past_its_time_is_ended(
    recorder: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pattern that backtracks for ever on one line: only ending the helper stops it.
    (tmp_path / "sessions" / "s1" / "slow.txt").write_text("a" * 40 + "!\n")
    monkeypatch.setattr(executor, "SEARCH_SECONDS", 0.5)
    monkeypatch.setattr(executor, "SEARCH_GRACE", 0.5)
    started = asyncio.get_running_loop().time()
    reply = await recorder.ask(op="grep", pattern="(a+)+$")
    assert reply["ok"] is False
    assert reply["error"]["kind"] == "timed_out"
    assert asyncio.get_running_loop().time() - started < 10
    # The helper is gone, and the executor still answers.
    later = await recorder.ask(op="grep", pattern="return")
    assert later["files"] == ["src/a.py"]


# --- Through the worker API -----------------------------------------------------------------


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, shared_bytes=1 << 20) as started:
        yield started


def _in(environment: str, session: str) -> dict[str, str]:
    return {"project_id": "p1", "session_id": session, "environment_id": environment}


async def _write(worker: Harness, where: dict[str, str], path: str, text: str, **more: Any) -> Any:
    content = base64.b64encode(text.encode()).decode()
    status, body = await worker.post(
        "/v1/files/write", {"context": where, "path": path, "content_b64": content, **more}
    )
    assert status == 200, body
    return body


async def test_the_api_edits_globs_and_greps(worker: Harness) -> None:
    here = _in("env1", "s1")
    written = await _write(worker, here, "src/a.py", "x = 1\ny = 1\n")
    status, edited = await worker.post(
        "/v1/files/edit",
        {"context": here, "path": "src/a.py", "old_string": "= 1", "new_string": "= 2"},
    )
    assert (status, edited["error"]["kind"]) == (400, "invalid_request"), edited
    assert "ambiguous" in edited["error"]["message"]
    status, edited = await worker.post(
        "/v1/files/edit",
        {
            "context": here,
            "path": "src/a.py",
            "old_string": "= 1",
            "new_string": "= 2",
            "replace_all": True,
            "expected_sha256": written["sha256"],
        },
    )
    assert status == 200, edited
    assert edited["replacements"] == 2
    status, read = await worker.post("/v1/files/read", {"context": here, "path": "src/a.py"})
    assert base64.b64decode(read["content_b64"]) == b"x = 2\ny = 2\n"
    assert read["sha256"] == edited["sha256"]
    status, listing = await worker.post("/v1/files/glob", {"context": here, "pattern": "**/*.py"})
    assert status == 200, listing
    assert [entry["path"] for entry in listing["entries"]] == ["src/a.py"]
    assert listing["entries"][0]["modified_at"]
    status, found = await worker.post(
        "/v1/files/grep", {"context": here, "pattern": "y = 2", "mode": "count"}
    )
    assert status == 200, found
    assert found["counts"] == [{"path": "src/a.py", "count": 1}]
    status, refused = await worker.post(
        "/v1/files/grep", {"context": here, "pattern": "[", "mode": "content"}
    )
    assert (status, refused["error"]["kind"]) == (400, "invalid_request")
    status, missing = await worker.post(
        "/v1/files/edit",
        {"context": here, "path": "none.txt", "old_string": "a", "new_string": "b"},
    )
    assert (status, missing["error"]["kind"]) == (404, "not_found")


async def test_a_shared_edit_expects_the_projects_version(worker: Harness) -> None:
    a, c = _in("env1", "s1"), _in("env2", "s3")
    first = await _write(worker, a, "shared/notes.md", "draft\n", create_only=True)
    body = {"path": "shared/notes.md", "old_string": "draft", "new_string": "final"}
    status, unguarded = await worker.post("/v1/files/edit", {"context": c, **body})
    assert (status, unguarded["error"]["kind"]) == (400, "invalid_request")
    status, edited = await worker.post(
        "/v1/files/edit", {"context": c, **body, "expected_sha256": first["sha256"]}
    )
    assert status == 200, edited
    # The first environment sees the other's edit, and its stale hash is a conflict.
    status, seen = await worker.post("/v1/files/read", {"context": a, "path": "shared/notes.md"})
    assert base64.b64decode(seen["content_b64"]) == b"final\n"
    again = {"path": "shared/notes.md", "old_string": "final", "new_string": "again"}
    status, stale = await worker.post(
        "/v1/files/edit", {"context": a, **again, "expected_sha256": first["sha256"]}
    )
    assert (status, stale["error"]["kind"]) == (409, "conflict"), stale
    status, found = await worker.post(
        "/v1/files/grep", {"context": a, "path": "shared", "pattern": "final"}
    )
    assert found["files"] == ["shared/notes.md"]


async def test_an_older_executor_answers_unsupported(worker: Harness) -> None:
    here = _in("env1", "s1")
    await _write(worker, here, "a.txt", "one\n")
    channel = worker.view.channel("env1")
    del channel.hello["file_tools"]
    for route, fields in (
        ("edit", {"path": "a.txt", "old_string": "one", "new_string": "two"}),
        ("glob", {"pattern": "*"}),
        ("grep", {"pattern": "one"}),
    ):
        status, body = await worker.post(f"/v1/files/{route}", {"context": here, **fields})
        assert body["error"]["kind"] == "unsupported", (route, status, body)
        assert "recreate the environment" in body["error"]["message"]
    assert (worker.workspace / "a.txt").read_text() == "one\n"


async def test_the_executor_tree_has_the_search_helper(tmp_path: Path) -> None:
    from sandbox_science.worker.backends import SANDBOX_FILES

    assert "sandbox_search.py" in {path.name for path in SANDBOX_FILES}
    assert os.path.exists(executor.sandbox_search.__file__)
