# SPDX-License-Identifier: MIT
"""A child environment's inputs from control's retained content (child environments
spec, CE-7): what the artifact inventory gives for a session path or a pinned revision,
and how an input is sent to the worker in parts, resuming where the worker's received
bytes end. The placement and the run are in ``test_delegation_environments``."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from pydantic import JsonValue

from sandbox_science.contracts.child_environments import RetainedInput
from sandbox_science.orchestrator.delegation.child_inputs import (
    InventoryContent,
    StagingError,
    pending,
    reference_files,
    resolve,
    send_input,
    session_path,
)

from .test_artifact_review import build, publish


def test_the_inventory_gives_kept_files_by_path_and_revisions_by_pin(tmp_path: Path) -> None:
    state = build(tmp_path)
    try:
        _reviews, inventory, source = state
        data = b"count = 16\n"
        revision = publish(state, path="results/count.txt", data=data)
        content = InventoryContent(inventory, "p", "s")
        found = content.find("results/count.txt")
        assert found == RetainedInput(
            path="results/count.txt",
            artifact_id=revision.artifact_id,
            revision=1,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )
        # The same session's file by the path the project's listing names, too.
        assert content.find("sessions/s/results/count.txt") is not None
        assert content.find("results/none.txt") is None
        # Another session's file is no input of this one's.
        assert (
            InventoryContent(inventory, "p", "other").find("sessions/s/results/count.txt") is None
        )
        assert content.find("../escape") is None
        pinned = content.pinned(revision.artifact_id, 1)
        assert pinned == found
        assert content.pinned(revision.artifact_id, 2) is None
        assert pinned is not None
        with content.open(pinned) as stream:
            assert stream.read() == data
        # A file deleted since is found no more; its pinned revision still is.
        inventory.record_deletion(source, "results/count.txt")
        assert content.find("results/count.txt") is None
        assert content.pinned(revision.artifact_id, 1) == found
    finally:
        state[1].documents.close()


def test_paths_and_references() -> None:
    assert session_path("sessions/s/a/b.txt", "s") == "a/b.txt"
    assert session_path("sessions/t/a.txt", "s") == "sessions/t/a.txt"
    assert session_path("shared/a.txt", "s") == "shared/a.txt"
    refs: list[JsonValue] = [
        {"file": "notes/a.md"},
        {"message": "msg-0123456789abcdef"},
        {"file": "b.txt"},
    ]
    assert reference_files(refs) == ["notes/a.md", "b.txt"]
    assert reference_files(None) == []
    item = RetainedInput(path="a.txt", artifact_id="x", revision=1, sha256="0" * 64, size=1)
    other = item.model_copy(update={"path": "b.txt"})
    assert pending((item, other), ["inputs/a.txt"]) == [1]


class _Content:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.opened = 0

    def item(self, path: str, revision: int = 1) -> RetainedInput:
        data = self.files[path]
        return RetainedInput(
            path=path,
            artifact_id=f"a-{path.replace('/', '-')}",
            revision=revision,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )

    def find(self, path: str) -> RetainedInput | None:
        return self.item(path) if path in self.files else None

    def pinned(self, artifact_id: str, revision: int) -> RetainedInput | None:
        found = [p for p in self.files if self.item(p).artifact_id == artifact_id]
        return self.item(found[0], revision) if found else None

    def open(self, item: RetainedInput) -> io.BytesIO:
        self.opened += 1
        return io.BytesIO(self.files[item.path])


def test_resolve_takes_each_path_once_first_first() -> None:
    content = _Content({"a.txt": b"a", "b.txt": b"b"})
    found = resolve(content, ["a.txt", "missing", "a.txt"], [("a-b.txt", 2), ("a-a.txt", 3)])
    assert [(i.path, i.revision) for i in found] == [("a.txt", 1), ("b.txt", 2)]


async def test_an_input_goes_in_parts_from_where_the_worker_holds_it() -> None:
    data = b"0123456789"
    content = _Content({"a.bin": data})
    item = content.item("a.bin")
    held = bytearray(b"0123456")  # what an earlier sender left: the worker holds 7 bytes
    calls: list[tuple[int, bytes]] = []

    async def send(index: int, offset: int, part: bytes) -> tuple[int, bool, bool]:
        calls.append((offset, part))
        if offset != len(held):
            return len(held), False, False
        held.extend(part)
        return len(held), True, len(held) == len(data)

    await send_input(content, 0, item, send, part_bytes=4)
    assert bytes(held) == data
    assert calls == [(0, b""), (7, b"789")]
    # A worker that went back (its part was discarded) is sent the bytes again.
    held.clear()
    calls.clear()
    content.opened = 0
    seen = iter([4])

    async def rewinding(index: int, offset: int, part: bytes) -> tuple[int, bool, bool]:
        calls.append((offset, part))
        if offset == 0 and not part:
            return next(seen, 0), False, False  # holds 4 bytes, then none
        if offset == 4 and len(held) == 0:
            return 0, False, False
        if offset != len(held):
            return len(held), False, False
        held.extend(part)
        return len(held), True, len(held) == len(data)

    await send_input(content, 0, item, rewinding, part_bytes=4)
    assert bytes(held) == data
    assert content.opened == 2  # opened again to go back


async def test_content_shorter_than_its_size_fails_by_its_path() -> None:
    content = _Content({"a.bin": b"abc"})
    item = content.item("a.bin").model_copy(update={"size": 10})

    async def send(index: int, offset: int, part: bytes) -> tuple[int, bool, bool]:
        return offset + len(part), True, False

    with pytest.raises(StagingError, match=r"staging_failed: input a\.bin: control's content"):
        await send_input(content, 0, item, send)

    async def refusing(index: int, offset: int, part: bytes) -> tuple[int, bool, bool]:
        return 0, False, False

    with pytest.raises(StagingError, match="another sender"):
        await send_input(content, 0, content.item("a.bin"), refusing)
