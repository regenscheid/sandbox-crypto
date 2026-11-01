# SPDX-License-Identifier: MIT
"""A delegated run's write area (delegated agents spec, DA-19) through the worker API:
a request whose context names ``work/<delegation_id>`` writes and edits only below it and
in ``shared/``, under the shared area's guard; anything else is refused with
``outside_write_area``, naming where it may write, before the environment is touched.
Path tricks (``..``, absolute paths, case, trailing slashes, symlinks) reach nothing
outside, and every run of the session still reads the write area."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError

from sandbox_science.contracts import ExecutionContext, outside_write_area, write_area
from sandbox_science.contracts.child_environments import hand_back_path, hand_back_reserved

from .harness import Harness

AREA = "work/dlg-a"
SIBLING = "work/dlg-b"


def _in(area: str | None = None) -> dict[str, str]:
    context = {"project_id": "p1", "session_id": "s1", "environment_id": "env1"}
    return context if area is None else {**context, "write_area": area}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest_asyncio.fixture
async def worker(tmp_path: Any) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, shared_bytes=1 << 20) as started:
        yield started


async def _write(worker: Harness, area: str | None, path: str, text: str, **more: Any) -> Any:
    return await worker.post(
        "/v1/files/write", {"context": _in(area), "path": path, "content_b64": _b64(text), **more}
    )


async def _edit(worker: Harness, area: str | None, path: str, old: str, new: str, **more: Any):
    return await worker.post(
        "/v1/files/edit",
        {"context": _in(area), "path": path, "old_string": old, "new_string": new, **more},
    )


def _refused(reply: tuple[int, Any], path: str) -> None:
    status, body = reply
    assert (status, body["error"]["kind"]) == (403, "policy_denied"), (path, body)
    message = body["error"]["message"]
    assert message.startswith(f"outside_write_area: {path!r} is outside your write area"), message
    # It names where the agent may write, so it can recover.
    assert f"only in {AREA}/ and in shared/" in message
    assert f"such as {AREA}/{path}" in message


@pytest.mark.asyncio
@pytest.mark.slow
async def test_a_child_writes_in_its_write_area_and_nowhere_else(worker: Harness) -> None:
    """DA-F4's writes: work/<id>/notes.md is written, sources/b.md refused; the parent
    (no write area) writes anywhere, and an edit is held as a write is."""
    status, body = await _write(worker, None, "sources/a.md", "parent's\n")
    assert status == 200, body
    status, body = await _write(worker, AREA, f"{AREA}/notes.md", "draft\n")
    assert status == 200, body
    status, body = await _edit(worker, AREA, f"{AREA}/notes.md", "draft", "final")
    assert status == 200, body
    _refused(await _write(worker, AREA, "sources/b.md", "child's\n"), "sources/b.md")
    _refused(await _edit(worker, AREA, "sources/a.md", "parent's", "child's"), "sources/a.md")
    root = worker.workspace
    assert not (root / "sources" / "b.md").exists()
    assert (root / "sources" / "a.md").read_text() == "parent's\n"
    assert (root / AREA / "notes.md").read_text() == "final\n"


@pytest.mark.asyncio
@pytest.mark.slow
async def test_path_tricks_reach_nothing_outside(worker: Harness) -> None:
    """Case, a longer or another delegation's name, the area itself and a trailing slash
    are refused; ``..``, absolute paths and empty segments are not workspace paths."""
    await _write(worker, None, "sources/a.md", "parent's\n")
    for path in (
        AREA,  # the area itself is not a file in it
        "work/dlg-ab/x.md",
        "work/DLG-A/x.md",
        "Work/dlg-a/x.md",
        "work/dlg-b/x.md",
        "dlg-a/x.md",
        "Shared/x.md",
    ):
        _refused(await _write(worker, AREA, path, "x"), path)
        _refused(await _edit(worker, AREA, path, "x", "y"), path)
    for path in (
        f"{AREA}/../../sources/a.md",
        f"{AREA}/./x.md",
        f"{AREA}/",
        f"{AREA}//x.md",
        f"./{AREA}/x.md",
        f"/{AREA}/x.md",
        "/etc/passwd",
    ):
        status, body = await _write(worker, AREA, path, "x")
        assert (status, body["error"]["kind"]) == (400, "invalid_request"), (path, body)
        status, body = await _edit(worker, AREA, path, "parent's", "child's")
        assert (status, body["error"]["kind"]) == (400, "invalid_request"), (path, body)
    assert (worker.workspace / "sources" / "a.md").read_text() == "parent's\n"
    assert not (worker.workspace / "work" / "dlg-ab").exists()
    assert not (worker.workspace / "x.md").exists()


@pytest.mark.asyncio
@pytest.mark.slow
async def test_a_symlink_in_the_write_area_leads_nowhere(worker: Harness) -> None:
    """A link the child's shell made in its write area, to a directory or a file outside,
    or the write area itself replaced by a link, is refused by the executor, which
    follows none; the file outside is unchanged."""
    await _write(worker, None, "sources/a.md", "parent's\n")
    await _write(worker, AREA, f"{AREA}/notes.md", "notes\n")
    area = worker.workspace / AREA
    os.symlink("../../sources", area / "dir")
    os.symlink("../../sources/a.md", area / "file.md")
    (worker.workspace / "work").mkdir(exist_ok=True)
    os.symlink("../sources", worker.workspace / SIBLING)
    for area_name, path in (
        (AREA, f"{AREA}/dir/b.md"),
        (AREA, f"{AREA}/dir/a.md"),
        (AREA, f"{AREA}/file.md"),
        (SIBLING, f"{SIBLING}/a.md"),
    ):
        status, body = await _write(worker, area_name, path, "child's\n")
        assert (status, body["error"]["kind"]) == (400, "invalid_request"), (path, body)
        assert "symlink" in body["error"]["message"], body
        status, body = await _edit(worker, area_name, path, "parent's", "child's")
        assert (status, body["error"]["kind"]) == (400, "invalid_request"), (path, body)
    assert (worker.workspace / "sources" / "a.md").read_text() == "parent's\n"
    assert not (worker.workspace / "sources" / "b.md").exists()


@pytest.mark.asyncio
@pytest.mark.slow
async def test_shared_keeps_its_guard(worker: Harness) -> None:
    """A child writes in shared/ as any run does: a new file with create_only, a change
    with the hash it read; without either it is refused."""
    status, body = await _write(worker, AREA, "shared/notes.md", "one\n")
    assert (status, body["error"]["kind"]) == (400, "invalid_request"), body
    status, made = await _write(worker, AREA, "shared/notes.md", "one\n", create_only=True)
    assert status == 200, made
    status, body = await _edit(worker, AREA, "shared/notes.md", "one", "two")
    assert (status, body["error"]["kind"]) == (400, "invalid_request"), body
    status, body = await _edit(
        worker, AREA, "shared/notes.md", "one", "two", expected_sha256=made["sha256"]
    )
    assert status == 200, body
    status, body = await _write(
        worker, AREA, "shared/notes.md", "three\n", expected_sha256=body["sha256"]
    )
    assert status == 200, body


@pytest.mark.asyncio
@pytest.mark.slow
async def test_the_parent_and_siblings_read_the_write_area(worker: Harness) -> None:
    """The write area is the session's to read: the parent's and a sibling's reads,
    listings and searches see it; the sibling cannot change it."""
    await _write(worker, AREA, f"{AREA}/notes.md", "found 17\n")
    for area in (None, SIBLING):
        status, read = await worker.post(
            "/v1/files/read", {"context": _in(area), "path": f"{AREA}/notes.md"}
        )
        assert status == 200, read
        assert base64.b64decode(read["content_b64"]) == b"found 17\n"
        status, found = await worker.post(
            "/v1/files/grep", {"context": _in(area), "pattern": "found", "path": "work"}
        )
        assert found["files"] == [f"{AREA}/notes.md"], found
    status, body = await _edit(worker, SIBLING, f"{AREA}/notes.md", "17", "18")
    assert body["error"]["kind"] == "policy_denied", body
    assert f"only in {SIBLING}/ and in shared/" in body["error"]["message"]
    status, body = await _edit(worker, None, f"{AREA}/notes.md", "17", "18")
    assert status == 200, body  # the parent may change it, as any file of its session


def test_a_write_area_has_one_shape() -> None:
    """Only ``work/<identifier>``: no deeper, wider or relative area can be named."""
    assert ExecutionContext.model_validate(_in(AREA)).write_area == AREA
    for bad in (".", "work", "work/", "work/a/b", "work/../x", "shared", "sources/x", "/work/a"):
        with pytest.raises(ValidationError):
            ExecutionContext.model_validate(_in(bad))
    # A context without one serializes as before.
    assert "write_area" not in ExecutionContext.model_validate(_in()).model_dump()


def test_the_check_and_the_hand_back_paths() -> None:
    context = ExecutionContext.model_validate(_in(write_area("dlg-a")))
    assert outside_write_area(context, "work/dlg-a/x/y.md") is None
    assert outside_write_area(context, "shared/x.md") is None
    assert outside_write_area(ExecutionContext.model_validate(_in()), "anything.md") is None
    refusal = outside_write_area(context, "x.md")
    assert refusal is not None
    assert refusal.startswith("outside_write_area: ")
    assert hand_back_path("work/dlg-a", "work/dlg-a/notes.md") == "work/dlg-a/notes.md"
    assert hand_back_path("work/dlg-a", "out/fig.png") == "work/dlg-a/out/fig.png"
    assert hand_back_path("work/dlg-a", "work/dlg-b/x") == "work/dlg-a/work/dlg-b/x"
    assert hand_back_reserved("work/dlg-a", ".handback/manifest.json")
    assert hand_back_reserved("work/dlg-a", "work/dlg-a/.handback/x")
    assert not hand_back_reserved("work/dlg-a", "work/dlg-a/handback/x")
