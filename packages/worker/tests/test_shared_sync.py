# SPDX-License-Identifier: MIT
"""The shared files' sync across a project's environments (D79), against executors that
do what they are asked and ones that research code has taken over."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.shared_sync import SharedRecords, SharedSync

pytestmark = pytest.mark.asyncio


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Environment:
    """An executor's side of the sync, over an in-memory ``shared/``.

    Its replies follow the executor's protocol; the attributes below make it lie, or
    make its scans miss files.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.stamps: dict[str, list[int]] = {}
        """Each file's modification time, size, inode and executable bit."""
        self.mark: str | None = None
        self.clock = 10**18
        self.inodes = 0
        self.racy = False
        """Scans come right after the latest change, as on a coarse clock."""
        self.partial = False
        self.hidden: set[str] = set()
        """Files a scan leaves out, as when a directory cannot be read."""
        self.claims: dict[str, str] = {}
        """Digests to report in place of the real ones."""
        self.refuse: dict[str, WorkerError] = {}
        """Errors for writes to these paths."""
        self.unreadable: dict[str, WorkerError] = {}
        """Errors for reads of these paths."""
        self.sizes: dict[str, int] = {}
        """Sizes to report in place of the real ones."""
        self.odd: list[str] = []
        """Names to report as not text."""
        self.opened: set[str] = set()
        """Files some process is writing."""
        self.latency = 0.0
        """Seconds each request takes, as over a real channel."""
        self.log: list[tuple[str, dict[str, Any]]] = []

    def put(
        self, path: str, data: bytes, *, executable: bool = False, same_stamp: bool = False
    ) -> None:
        """Research code writes a file; ``same_stamp`` rewrites it within a clock tick."""
        if same_stamp:
            mtime, size, inode, flag = self.stamps[path]
            assert size == len(data)
        else:
            self.clock += 10**9
            self.inodes += 1
            mtime, inode, flag = self.clock, self.inodes, int(executable)
        self.files[path] = data
        self.stamps[path] = [mtime, len(data), inode, flag]

    def remove(self, path: str) -> None:
        del self.files[path], self.stamps[path]

    def append(self, inode: int, data: bytes) -> bool:
        """A process that has the file ``inode`` open writes to it, whatever its name now."""
        for path, stamp in self.stamps.items():
            if stamp[2] == inode:
                self.clock += 10**9
                self.files[path] += data
                self.stamps[path] = [self.clock, len(self.files[path]), inode, stamp[3]]
                return True
        return False  # its name is gone: what it writes is lost

    async def request(self, op: str, **fields: Any) -> dict[str, Any]:
        self.log.append((op, fields))
        if self.latency:
            await asyncio.sleep(self.latency)
        if op == "shared":
            return self._scan(fields)
        if op == "read":
            if fields["path"] in self.unreadable:
                raise self.unreadable[fields["path"]]
            data = self.files.get(fields["path"])
            if data is None:
                raise WorkerError(ErrorKind.NOT_FOUND, "no file")
            offset, length = fields["offset"], fields["length"]
            chunk = data[offset : offset + length]
            reply: dict[str, Any] = {
                "offset": offset,
                "size": len(data),
                "content_b64": base64.b64encode(chunk).decode(),
                "eof": offset + len(chunk) >= len(data),
            }
            if fields.get("hash"):
                reply["sha256"] = self.claims.get(fields["path"], _sha(data))
            return reply
        if op == "write":
            return self._write(fields)
        if op == "remove":
            current = self.files.get(fields["path"])
            if current is None:
                raise WorkerError(ErrorKind.NOT_FOUND, "no file")
            if _sha(current) != fields["expected_sha256"]:
                raise WorkerError(ErrorKind.CONFLICT, "changed")
            if fields.get("if_unused") and fields["path"] in self.opened:
                return {"removed": False, "busy": True}
            self.remove(fields["path"])
            return {"removed": True}
        if op == "move":
            path, target = fields["path"], fields["target"]
            if target in self.files:
                raise WorkerError(ErrorKind.CONFLICT, "the new name is taken")
            if path not in self.files:
                raise WorkerError(ErrorKind.NOT_FOUND, "no file")
            self.files[target], self.stamps[target] = self.files.pop(path), self.stamps.pop(path)
            return {"stamp": self.stamps[target][:3]}
        raise AssertionError(op)

    def _scan(self, fields: dict[str, Any]) -> dict[str, Any]:
        if isinstance(fields.get("mark"), str):
            self.mark = fields["mark"]
        shown = {
            path: [stamp[0], self.sizes.get(path, stamp[1]), *stamp[2:]]
            for path, stamp in self.stamps.items()
            if path not in self.hidden
        }
        budget = fields["hash_budget"]
        digests: dict[str, str] = {}
        large: list[str] = []
        for path in fields["hash"]:
            if path not in shown:
                continue
            size = shown[path][1]
            if size > fields["hash_limit"]:
                large.append(path)
            elif size <= budget:
                digests[path] = self.claims.get(path, _sha(self.files[path]))
                budget -= size
        reply: dict[str, Any] = {
            "files": shown,
            "truncated": False,
            "partial": self.partial,
            "odd": list(self.odd),
            "sha256": digests,
            "too_large": large,
            "mark": self.mark,
            "now": self.clock if self.racy else self.clock + 10 * 10**9,
        }
        return reply

    def _write(self, fields: dict[str, Any]) -> dict[str, Any]:
        path = fields["path"]
        if path in self.refuse:
            raise self.refuse[path]
        if any(other.startswith(path + "/") for other in self.files):
            raise WorkerError(ErrorKind.INVALID_REQUEST, f"{path!r} is a directory")
        current = self.files.get(path)
        if fields.get("create_only") and current is not None:
            raise WorkerError(ErrorKind.CONFLICT, f"{path!r} already exists")
        expected = fields.get("expected_sha256")
        if expected is not None and (current is None or _sha(current) != expected):
            raise WorkerError(ErrorKind.CONFLICT, f"{path!r} changed")
        if current is not None and fields.get("if_unused") and path in self.opened:
            return {"busy": True}
        data = base64.b64decode(fields["content_b64"])
        self.put(path, data, executable=fields.get("executable") is True)
        return {"sha256": _sha(data), "size": len(data), "stamp": self.stamps[path][:3]}


@pytest.fixture
def sync(tmp_path: Path) -> Iterator[SharedSync]:
    records = SharedRecords(tmp_path / "shared.sqlite3")
    yield SharedSync(records, tmp_path / "shared", max_file_bytes=1024)
    records.close()


async def test_changes_reach_the_projects_other_environments(sync: SharedSync) -> None:
    e1, e2 = Environment(), Environment()
    e1.put("shared/r.csv", b"1,2\n")
    e1.put("shared/bin/run.sh", b"#!/bin/sh\n", executable=True)
    assert await sync.up("p1", "e1", "s1", e1) == []
    assert await sync.down("p1", "e2", "s2", e2) == []
    assert e2.files == e1.files
    assert e2.stamps["shared/bin/run.sh"][3] == 1
    e1.put("shared/r.csv", b"1,2\n3,4\n")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files["shared/r.csv"] == b"1,2\n3,4\n"
    # A file the sync wrote is hashed once more, in case it changed in the same clock tick.
    e2.log.clear()
    await sync.up("p1", "e2", "s2", e2)
    assert [fields["hash"] for _, fields in e2.log] == [[], ["shared/r.csv"]]
    # Then, with nothing changed, a call costs one scan before it and one after.
    e2.log.clear()
    await sync.down("p1", "e2", "s2", e2)
    await sync.up("p1", "e2", "s2", e2)
    assert [(op, fields["hash"]) for op, fields in e2.log] == [("shared", []), ("shared", [])]


async def test_a_deletion_goes_too_but_loses_to_a_change(sync: SharedSync) -> None:
    e1, e2 = Environment(), Environment()
    e1.put("shared/a.txt", b"a")
    e1.put("shared/b.txt", b"b")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.remove("shared/a.txt")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    assert "shared/a.txt" not in e2.files
    # e2 changes b.txt while e1 deletes it: the change is kept, everywhere.
    e2.put("shared/b.txt", b"b2")
    e1.remove("shared/b.txt")
    await sync.up("p1", "e1", "s1", e1)
    await sync.up("p1", "e2", "s2", e2)
    await sync.down("p1", "e1", "s1", e1)
    assert e1.files == {"shared/b.txt": b"b2"}


async def test_concurrent_changes_keep_a_conflict_copy(sync: SharedSync) -> None:
    e1, e2 = Environment(), Environment()
    e1.put("shared/notes.md", b"a")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.put("shared/notes.md", b"b")
    e2.put("shared/notes.md", b"c")
    assert await sync.up("p1", "e1", "s1", e1) == []
    [notice] = await sync.up("p1", "e2", "s2", e2)
    assert notice.endswith("this one is kept as shared/notes.conflict-e2.md")
    await sync.down("p1", "e2", "s2", e2)
    await sync.down("p1", "e1", "s1", e1)
    for environment in (e1, e2):
        assert environment.files == {
            "shared/notes.md": b"b",
            "shared/notes.conflict-e2.md": b"c",
        }


async def test_a_reported_digest_is_never_a_path(sync: SharedSync, tmp_path: Path) -> None:
    # C1: a "digest" naming a file beside the blob store copied the worker's token.
    (tmp_path / "worker.token").write_text("test-secret-pool-token\n")
    claim = "../" + "/" * 49 + "worker.token"
    assert len(claim) == 64
    hostile = Environment()
    hostile.put("shared/loot.txt", b"x" * 23)
    hostile.claims["shared/loot.txt"] = claim
    assert await sync.up("p1", "e1", "s1", hostile) == []
    reader = Environment()
    await sync.down("p1", "e2", "s2", reader)
    assert reader.files == {}
    with pytest.raises(ValueError, match="not a SHA-256 digest"):
        Blobs(tmp_path / "artifacts").path(claim)


async def test_content_is_read_and_checked_not_taken_from_another_project(
    sync: SharedSync,
) -> None:
    # H1: naming another project's SHA-256 used to be enough to get its content.
    secret = b"p1 results\n"
    owner = Environment()
    owner.put("shared/r.txt", secret)
    await sync.up("p1", "e1", "s1", owner)
    thief = Environment()
    thief.put("shared/stolen.txt", b"x" * len(secret))
    thief.claims["shared/stolen.txt"] = _sha(secret)
    await sync.up("p2", "e2", "s2", thief)
    reader = Environment()
    await sync.down("p2", "e3", "s3", reader)
    assert reader.files == {}
    assert sync.blobs("p1").path(_sha(secret)).exists()
    assert not sync.blobs("p2").path(_sha(secret)).exists()


async def test_a_new_sandbox_under_an_old_id_deletes_nothing(sync: SharedSync) -> None:
    # H2: an empty shared/ under a known ID read as every file deleted.
    e1, e2 = Environment(), Environment()
    e1.put("shared/a.txt", b"a")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    again = Environment()  # e2's sandbox, made again: empty, without the sync's mark
    assert await sync.up("p1", "e2", "s2", again) == []
    await sync.down("p1", "e1", "s1", e1)
    assert e1.files == {"shared/a.txt": b"a"}
    await sync.down("p1", "e2", "s2", again)
    assert again.files == {"shared/a.txt": b"a"}


async def test_a_scan_that_misses_files_deletes_nothing(sync: SharedSync) -> None:
    # H2: a directory the scan could not read looked deleted.
    e1 = Environment()
    e1.put("shared/d/x.csv", b"x")
    await sync.up("p1", "e1", "s1", e1)
    e1.hidden = {"shared/d/x.csv"}
    e1.partial = True
    await sync.up("p1", "e1", "s1", e1)
    e2 = Environment()
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files == {"shared/d/x.csv": b"x"}


async def test_a_deletion_reaches_a_file_the_scan_missed(sync: SharedSync) -> None:
    e1, e2 = Environment(), Environment()
    e1.put("shared/x", b"x")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.remove("shared/x")
    await sync.up("p1", "e1", "s1", e1)
    e2.partial = True
    e2.hidden = {"shared/x"}
    await sync.down("p1", "e2", "s2", e2)
    assert "shared/x" not in e2.files  # removed only because its content was the one synced


async def test_blobs_go_when_no_file_uses_them(sync: SharedSync) -> None:
    # H3: every version stayed in the blob store.
    e1 = Environment()
    store = sync.blobs("p1")
    e1.put("shared/a.bin", b"v1")
    await sync.up("p1", "e1", "s1", e1)
    e1.put("shared/a.bin", b"v2")
    await sync.up("p1", "e1", "s1", e1)
    assert not store.path(_sha(b"v1")).exists()
    assert store.path(_sha(b"v2")).exists()
    e1.put("shared/copy.bin", b"v2")  # the same content under another name
    e1.remove("shared/a.bin")
    await sync.up("p1", "e1", "s1", e1)
    assert store.path(_sha(b"v2")).exists()
    e1.remove("shared/copy.bin")
    await sync.up("p1", "e1", "s1", e1)
    assert not store.path(_sha(b"v2")).exists()
    # A blob a crash left behind goes at the next start.
    orphan = store.path(_sha(b"orphan"))
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    assert await sync.sweep() == 1
    assert not orphan.exists()


async def test_the_projects_copy_has_a_file_limit(tmp_path: Path) -> None:
    records = SharedRecords(tmp_path / "shared.sqlite3")
    sync = SharedSync(records, tmp_path / "shared", max_project_files=2)
    try:
        e1 = Environment()
        for name in ("a", "b", "c"):
            e1.put(f"shared/{name}", name.encode())
        [notice] = await sync.up("p1", "e1", "s1", e1)
        assert "at their limit (2 files" in notice
        assert await sync.up("p1", "e1", "s1", e1) == []  # told once
        e2 = Environment()
        await sync.down("p1", "e2", "s2", e2)
        assert e2.files == {"shared/a": b"a", "shared/b": b"b"}
    finally:
        records.close()


async def test_a_file_over_the_limit_is_not_hashed_again(sync: SharedSync) -> None:
    e1 = Environment()
    e1.put("shared/big.bin", b"x" * 2000)
    [notice] = await sync.up("p1", "e1", "s1", e1)
    assert "publish it as an artifact" in notice
    e1.log.clear()
    assert await sync.up("p1", "e1", "s1", e1) == []
    assert [fields["hash"] for _, fields in e1.log] == [[]]


async def test_a_rewrite_that_keeps_its_stamp_is_found_while_recent(sync: SharedSync) -> None:
    # M1: a same-size rewrite within one tick of a coarse clock kept its stamp.
    e1 = Environment()
    e1.racy = True
    e1.put("shared/s.txt", b"aaaa")
    await sync.up("p1", "e1", "s1", e1)
    e1.put("shared/s.txt", b"bbbb", same_stamp=True)
    await sync.up("p1", "e1", "s1", e1)
    e2 = Environment()
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files == {"shared/s.txt": b"bbbb"}


async def test_a_write_expects_the_projects_version(sync: SharedSync) -> None:
    # M2: two environments' writes expecting the same version both succeeded.
    e1, e2 = Environment(), Environment()
    e1.put("shared/x.txt", b"v1")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)

    def body(text: bytes) -> dict[str, Any]:
        return {
            "path": "shared/x.txt",
            "content_b64": base64.b64encode(text).decode(),
            "expected_sha256": _sha(b"v1"),
            "create_only": False,
        }

    await sync.write("p1", "e1", "s1", e1, body(b"from e1"), 10, [])
    with pytest.raises(WorkerError) as refused:
        await sync.write("p1", "e2", "s2", e2, body(b"from e2"), 10, [])
    assert refused.value.kind is ErrorKind.CONFLICT
    assert e2.files["shared/x.txt"] == b"from e1"


async def test_a_write_is_not_made_when_the_sync_before_it_fails(sync: SharedSync) -> None:
    e1, e2 = Environment(), Environment()
    e1.put("shared/x.txt", b"v1")
    await sync.up("p1", "e1", "s1", e1)
    e2.refuse["shared/x.txt"] = WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
    fields = {"path": "shared/y.txt", "content_b64": "eQ==", "create_only": True}
    with pytest.raises(WorkerError) as refused:
        await sync.write("p1", "e2", "s2", e2, fields, 10, [])
    assert refused.value.kind is ErrorKind.UNAVAILABLE
    assert "shared/y.txt" not in e2.files


async def test_a_file_that_cannot_be_brought_in_is_tried_again_later(tmp_path: Path) -> None:
    # M3: a file that could not be written was tried, and failed, at every call.
    records = SharedRecords(tmp_path / "shared.sqlite3")
    sync = SharedSync(records, tmp_path / "shared", failure_backoff=1.0)
    try:
        e1, e2 = Environment(), Environment()
        e1.put("shared/x.txt", b"x")
        await sync.up("p1", "e1", "s1", e1)
        e2.refuse["shared/x.txt"] = WorkerError(ErrorKind.POLICY_DENIED, "not here")
        assert await sync.down("p1", "e2", "s2", e2) == [
            "shared/x.txt could not be brought into this environment: not here"
        ]
        assert await sync.down("p1", "e2", "s2", e2) == []
        assert sum(op == "write" for op, _ in e2.log) == 1
        del e2.refuse["shared/x.txt"]
        await asyncio.sleep(1.1)
        await sync.down("p1", "e2", "s2", e2)
        assert e2.files == {"shared/x.txt": b"x"}
    finally:
        records.close()


async def test_an_executor_failure_stops_the_sync(sync: SharedSync) -> None:
    e1, e2 = Environment(), Environment()
    e1.put("shared/a.txt", b"a")
    await sync.up("p1", "e1", "s1", e1)
    e2.refuse["shared/a.txt"] = WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
    with pytest.raises(WorkerError):
        await sync.down("p1", "e2", "s2", e2)
    del e2.refuse["shared/a.txt"]
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files == {"shared/a.txt": b"a"}


async def test_a_directory_replaced_by_a_file_syncs_in_one_pass(sync: SharedSync) -> None:
    e1, e2 = Environment(), Environment()
    e1.put("shared/results/a.csv", b"1")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e2.remove("shared/results/a.csv")
    e2.put("shared/results", b"flat")
    await sync.up("p1", "e2", "s2", e2)
    assert await sync.down("p1", "e1", "s1", e1) == []
    assert e1.files == {"shared/results": b"flat"}


async def test_an_environment_is_synced_for_its_first_project_only(sync: SharedSync) -> None:
    # M6: a sandbox made by hand and used by two projects merged their shared files.
    handmade = Environment()
    handmade.put("shared/p1.txt", b"p1 only")
    await sync.up("p1", "h", "s1", handmade)
    [notice] = await sync.up("p2", "h", "t1", handmade)
    assert "synced with another project's environments" in notice
    assert await sync.up("p2", "h", "t1", handmade) == []  # told once
    other = Environment()
    await sync.down("p2", "b", "t2", other)
    assert other.files == {}


async def test_what_down_wrote_is_known(sync: SharedSync) -> None:
    e1, e2 = Environment(), Environment()
    e1.put("shared/r.txt", b"r")
    await sync.up("p1", "e1", "s1", e1)
    since = time.monotonic()
    await sync.down("p1", "e2", "s2", e2)
    await sync.up("p1", "e2", "s2", e2)  # a call that left the file as it was
    until = time.monotonic()
    assert sync.unchanged_writes("e2", ["shared/r.txt"], since, until) == {"shared/r.txt"}
    assert sync.unchanged_writes("e2", ["shared/r.txt"], until + 1, until + 2) == set()
    # Once a call writes the file after the sync, it is the call's own.
    e2.put("shared/r.txt", b"mine")
    await sync.up("p1", "e2", "s2", e2)
    assert sync.unchanged_writes("e2", ["shared/r.txt"], since, time.monotonic()) == set()


async def test_paths_that_cannot_be_synced_are_named_once(sync: SharedSync) -> None:
    e1 = Environment()
    e1.put("shared/ok.txt", b"ok")
    e1.put("shared/" + "d" * 4100, b"x")
    notices = await sync.up("p1", "e1", "s1", e1)
    assert len(notices) == 1
    assert "path is over 4096 bytes" in notices[0]
    assert await sync.up("p1", "e1", "s1", e1) == []
    e2 = Environment()
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files == {"shared/ok.txt": b"ok"}


# The second review's findings.


async def test_a_large_change_arrives_over_several_steps(tmp_path: Path) -> None:
    # H1: a step that ran out of time kept nothing, so the same work was redone forever.
    records = SharedRecords(tmp_path / "shared.sqlite3")
    sync = SharedSync(records, tmp_path / "shared", step_seconds=0.05)
    try:
        e1 = Environment()
        for n in range(40):
            e1.put(f"shared/f{n:02}", b"x%d" % n)
        e1.latency = 0.005  # each read takes a round trip
        await sync.up("p1", "e1", "s1", e1)
        first = len(records.state("p1", "e1").files)
        assert 0 < first < 40
        for _ in range(20):
            await sync.up("p1", "e1", "s1", e1)
        assert len(records.state("p1", "e1").files) == 40
        e2 = Environment()
        e2.latency = 0.005
        for _ in range(20):
            await sync.down("p1", "e2", "s2", e2)
        assert e2.files == e1.files
    finally:
        records.close()


async def test_a_file_that_cannot_be_written_does_not_stop_the_others(sync: SharedSync) -> None:
    # H2: one unwritable file stopped the environment's whole sync, at every call.
    e1, e2 = Environment(), Environment()
    for name in "abc":
        e1.put(f"shared/{name}", name.encode())
    await sync.up("p1", "e1", "s1", e1)
    e2.refuse["shared/b"] = WorkerError(ErrorKind.INVALID_REQUEST, "File name too long")
    [notice] = await sync.down("p1", "e2", "s2", e2)
    assert notice == "shared/b could not be brought into this environment: File name too long"
    assert e2.files == {"shared/a": b"a", "shared/c": b"c"}
    e2.refuse["shared/b"] = WorkerError(ErrorKind.INTERNAL, "something else")
    assert await sync.down("p1", "e2", "s2", e2) == []  # not tried again so soon
    # A failing executor still stops the step.
    e2.refuse["shared/b"] = WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
    e1.put("shared/d", b"d")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    assert "shared/d" in e2.files


async def test_conflict_copies_of_long_names_stay_within_the_name_limit(
    sync: SharedSync,
) -> None:
    name = "shared/" + "n" * 240 + ".csv"
    e1, e2 = Environment(), Environment()
    e1.put(name, b"1")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.put(name, b"2")
    e2.put(name, b"3")
    await sync.up("p1", "e1", "s1", e1)
    [notice] = await sync.up("p1", "e2", "s2", e2)
    [copy] = [path for path in e2.files if path != name]
    assert len(copy.rsplit("/", 1)[1].encode()) <= 255
    assert copy.endswith(".conflict-e2.csv")
    assert copy in notice
    assert e2.files == {name: b"2", copy: b"3"}


async def test_names_that_cannot_be_synced_stop_nothing_else(sync: SharedSync) -> None:
    # M5: one such name made every scan partial, so no deletion was passed on.
    e1, e2 = Environment(), Environment()
    e1.put("shared/a", b"a")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.odd = ["shared/screenshot" + chr(0x202F) + "at noon.png"]  # a narrow no-break space
    e1.put("shared/" + "x" * 300, b"too long a name")
    e1.remove("shared/a")
    notices = await sync.up("p1", "e1", "s1", e1)
    assert len(notices) == 2
    assert all("is not synced" in notice for notice in notices)
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files == {}  # the deletion arrived


async def test_a_file_is_not_read_when_the_project_has_no_room(tmp_path: Path) -> None:
    # M4: files were read before the project's limit was checked.
    records = SharedRecords(tmp_path / "shared.sqlite3")
    sync = SharedSync(records, tmp_path / "shared", max_project_bytes=100)
    try:
        e1 = Environment()
        e1.put("shared/big", b"x" * 200)
        [notice] = await sync.up("p1", "e1", "s1", e1)
        assert "at their limit" in notice
        assert [op for op, _ in e1.log if op == "read"] == []
        # An executor that understates a file's size: read once, refused, kept nowhere.
        e1.put("shared/liar", b"y" * 200)
        e1.sizes["shared/liar"] = 10
        await sync.up("p1", "e1", "s1", e1)
        assert not sync.blobs("p1").path(_sha(b"y" * 200)).exists()
        reads = len([op for op, _ in e1.log if op == "read"])
        await sync.up("p1", "e1", "s1", e1)
        assert len([op for op, _ in e1.log if op == "read"]) == reads  # not read again
    finally:
        records.close()


async def test_a_call_still_writing_a_conflicted_file_keeps_its_writes(sync: SharedSync) -> None:
    # M1: a sync in the middle of a call replaced the file the call was appending to.
    e1, e2 = Environment(), Environment()
    e1.put("shared/results.csv", b"n\n")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e2.put("shared/results.csv", b"n\n1\n")  # a job in e2 appends, and keeps the file open
    inode = e2.stamps["shared/results.csv"][2]
    e1.put("shared/results.csv", b"n\nfrom e1\n")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s3", e2)  # another session's call in e2 syncs meanwhile
    assert e2.append(inode, b"2\n")  # the job's later rows land in the conflict copy
    await sync.up("p1", "e2", "s2", e2)  # the job's call ends
    await sync.down("p1", "e1", "s1", e1)
    copy = "shared/results.conflict-e2.csv"
    for environment in (e1, e2):
        assert environment.files == {"shared/results.csv": b"n\nfrom e1\n", copy: b"n\n1\n2\n"}


async def test_a_file_a_process_has_open_is_replaced_once_it_is_closed(sync: SharedSync) -> None:
    e1, e2 = Environment(), Environment()
    e1.put("shared/log.txt", b"1")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.put("shared/log.txt", b"2")
    await sync.up("p1", "e1", "s1", e1)
    e2.opened.add("shared/log.txt")
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files["shared/log.txt"] == b"1"
    e2.opened.clear()
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files["shared/log.txt"] == b"2"


async def test_a_write_is_refused_if_its_file_could_not_be_brought_up_to_date(
    sync: SharedSync,
) -> None:
    # M2: a write expecting a stale local version was made, and quietly became a conflict.
    e1, e2 = Environment(), Environment()
    e1.put("shared/x.txt", b"v1")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.put("shared/x.txt", b"v2")
    await sync.up("p1", "e1", "s1", e1)
    e2.refuse["shared/x.txt"] = WorkerError(ErrorKind.POLICY_DENIED, "Permission denied")
    fields = {
        "path": "shared/x.txt",
        "content_b64": base64.b64encode(b"v3").decode(),
        "expected_sha256": _sha(b"v1"),
        "create_only": False,
    }
    notices: list[str] = []
    with pytest.raises(WorkerError, match="could not be brought up to date") as refused:
        await sync.write("p1", "e2", "s2", e2, fields, 10, notices)
    assert notices == ["shared/x.txt could not be brought into this environment: Permission denied"]
    assert refused.value.kind is ErrorKind.UNAVAILABLE
    assert e2.files["shared/x.txt"] == b"v1"


async def test_a_removed_mark_brings_no_deleted_file_back(sync: SharedSync) -> None:
    # M6: a lost mark made every file in the environment look new, deleted ones included.
    e1, e2 = Environment(), Environment()
    e1.put("shared/old.txt", b"old")
    e1.put("shared/keep.txt", b"keep")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.remove("shared/old.txt")
    await sync.up("p1", "e1", "s1", e1)
    e2.mark = None  # research code removed the mark
    await sync.down("p1", "e2", "s2", e2)
    await sync.down("p1", "e1", "s1", e1)
    assert e1.files == e2.files == {"shared/keep.txt": b"keep"}


async def test_blobs_a_cut_short_step_read_are_deleted_later(sync: SharedSync) -> None:
    # L1: a step cut short left the blobs it had read, or deleted ones it then recorded.
    e1 = Environment()
    e1.put("shared/a", b"a")
    e1.put("shared/b", b"b")
    e1.unreadable["shared/b"] = WorkerError(ErrorKind.UNAVAILABLE, "the executor went away")
    with pytest.raises(WorkerError):
        await sync.up("p1", "e1", "s1", e1)
    assert sync.blobs("p1").path(_sha(b"a")).exists()  # read, not recorded
    e1.remove("shared/a")
    del e1.unreadable["shared/b"]
    await sync.up("p1", "e1", "s1", e1)
    assert not sync.blobs("p1").path(_sha(b"a")).exists()
    assert sync.blobs("p1").path(_sha(b"b")).exists()


async def test_a_rewrite_within_a_tick_after_hashing_is_found_later(sync: SharedSync) -> None:
    # L2: a same-size rewrite in the clock tick after the hash was missed for good.
    e1 = Environment()
    e1.racy = True
    e1.put("shared/s.txt", b"aaaa")
    await sync.up("p1", "e1", "s1", e1)
    e1.put("shared/s.txt", b"bbbb", same_stamp=True)
    e1.racy = False  # the next scan comes long after
    await sync.up("p1", "e1", "s1", e1)
    e2 = Environment()
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files == {"shared/s.txt": b"bbbb"}


async def test_a_damaged_blob_is_not_copied_and_is_read_again(tmp_path: Path) -> None:
    # L3: a torn blob was written into other environments as the correct content.
    records = SharedRecords(tmp_path / "shared.sqlite3")
    sync = SharedSync(records, tmp_path / "shared", failure_backoff=0.0)
    try:
        e1, e2 = Environment(), Environment()
        e1.put("shared/x", b"right")
        await sync.up("p1", "e1", "s1", e1)
        sync.blobs("p1").path(_sha(b"right")).write_bytes(b"wrong")
        await sync.down("p1", "e2", "s2", e2)
        assert e2.files == {}
        await sync.up("p1", "e1", "s1", e1)  # e1 still has the content: read again from it
        await sync.down("p1", "e2", "s2", e2)
        assert e2.files == {"shared/x": b"right"}
    finally:
        records.close()


# The third review's findings.


async def test_a_blob_unreadable_for_now_is_not_lost(
    sync: SharedSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    # N1: one transient error deleted the project's only copy of a file.
    e1, e2 = Environment(), Environment()
    e1.put("shared/x", b"only here")
    await sync.up("p1", "e1", "s1", e1)
    blob = sync.blobs("p1").path(_sha(b"only here"))
    read_bytes = Path.read_bytes

    def exhausted(self: Path) -> bytes:
        if self == blob:
            raise OSError(24, "Too many open files")
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", exhausted)
    await sync.down("p1", "e2", "s2", e2)
    monkeypatch.undo()
    assert blob.read_bytes() == b"only here"
    assert e2.files == {}


async def test_a_step_that_waited_for_the_lock_still_gets_its_time(tmp_path: Path) -> None:
    # N2: a step's budget ran while it waited for another environment's step.
    records = SharedRecords(tmp_path / "shared.sqlite3")
    sync = SharedSync(records, tmp_path / "shared", step_seconds=0.2)
    try:
        busy, other = Environment(), Environment()
        for n in range(200):
            busy.put(f"shared/a{n:03}", b"a%d" % n)
        for n in range(20):
            other.put(f"shared/b{n:02}", b"b%d" % n)
        busy.latency = other.latency = 0.004
        await asyncio.gather(sync.up("p1", "e1", "s1", busy), sync.up("p1", "e2", "s2", other))
        names = records.state("p1", "e2").files
        assert any(name.startswith("shared/b") for name in names)
    finally:
        records.close()


async def test_a_file_a_process_reads_is_still_updated(sync: SharedSync) -> None:
    # N3: a file open only for reading was never updated; only writers keep it back.
    e1, e2 = Environment(), Environment()
    e1.put("shared/data.h5", b"v1")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.put("shared/data.h5", b"v2")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)  # a kernel reading it does not count as writing
    assert e2.files["shared/data.h5"] == b"v2"
    e1.put("shared/data.h5", b"v3")
    await sync.up("p1", "e1", "s1", e1)
    e2.opened.add("shared/data.h5")  # now a process writes it
    [notice] = await sync.down("p1", "e2", "s2", e2)
    assert "while a process here is writing it" in notice
    fields = {
        "path": "shared/data.h5",
        "content_b64": base64.b64encode(b"mine").decode(),
        "expected_sha256": _sha(b"v3"),
        "create_only": False,
    }
    with pytest.raises(WorkerError, match="a process in this environment is writing it"):
        await sync.write("p1", "e2", "s2", e2, fields, 10, [])
    e2.opened.clear()
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files["shared/data.h5"] == b"v3"


async def test_a_sandbox_made_again_with_older_content_keeps_the_projects(
    sync: SharedSync,
) -> None:
    # N5: an environment restored from an old copy rolled the project back.
    e1, e2 = Environment(), Environment()
    e1.put("shared/plan.md", b"old")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    restored = Environment()  # e2 made again from a backup holding a different version
    restored.put("shared/plan.md", b"restored")
    [notice] = await sync.up("p1", "e2", "s2", restored)
    assert "kept as shared/plan.conflict-e2.md" in notice
    await sync.down("p1", "e1", "s1", e1)
    assert e1.files == {"shared/plan.md": b"old", "shared/plan.conflict-e2.md": b"restored"}


async def test_updates_reach_files_past_a_scan_cut_short(sync: SharedSync) -> None:
    # N6: a synced file beyond a truncated scan never got new versions.
    e1, e2 = Environment(), Environment()
    e1.put("shared/x", b"1")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.put("shared/x", b"2")
    await sync.up("p1", "e1", "s1", e1)
    e2.partial = True
    e2.hidden = {"shared/x"}
    await sync.down("p1", "e2", "s2", e2)
    assert e2.files == {"shared/x": b"2"}


async def test_a_rewrite_in_the_tick_after_the_syncs_write_is_found(sync: SharedSync) -> None:
    # N7: the stamp the sync wrote was trusted, so a same-tick rewrite was missed.
    e1, e2 = Environment(), Environment()
    e1.put("shared/s.txt", b"aaaa")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e2.put("shared/s.txt", b"bbbb", same_stamp=True)  # within the write's clock tick
    await sync.up("p1", "e2", "s2", e2)
    await sync.down("p1", "e1", "s1", e1)
    assert e1.files == {"shared/s.txt": b"bbbb"}


async def test_a_moved_files_name_is_never_left_empty(sync: SharedSync) -> None:
    # N8: after a move, the name stayed empty until a later step wrote it.
    e1, e2 = Environment(), Environment()
    e1.put("shared/notes.md", b"a")
    await sync.up("p1", "e1", "s1", e1)
    await sync.down("p1", "e2", "s2", e2)
    e1.put("shared/notes.md", b"b")
    e2.put("shared/notes.md", b"c")
    await sync.up("p1", "e1", "s1", e1)
    await sync.up("p1", "e2", "s2", e2)  # an ``up`` alone, with no ``down`` after it
    assert e2.files == {"shared/notes.md": b"b", "shared/notes.conflict-e2.md": b"c"}


async def test_upload_limit_does_not_widen_other_shared_files(tmp_path: Path) -> None:
    records = SharedRecords(tmp_path / "uploads.sqlite3")
    sync = SharedSync(records, tmp_path / "blobs", max_file_bytes=4, max_upload_bytes=8)
    e1, e2 = Environment(), Environment()
    e1.put("shared/uploads/s1/u/source.pdf", b"12345678")
    e1.put("shared/ordinary.pdf", b"12345")
    e1.put("shared/uploads/s1/big/source.pdf", b"123456789")
    try:
        notices = await sync.up("p1", "e1", "s1", e1)
        assert len(notices) == 2
        assert await sync.down("p1", "e2", "s2", e2) == []
        assert e2.files == {"shared/uploads/s1/u/source.pdf": b"12345678"}
        hashed = [path for op, fields in e1.log if op == "shared" for path in fields["hash"]]
        assert "shared/ordinary.pdf" not in hashed
        assert "shared/uploads/s1/big/source.pdf" not in hashed
    finally:
        records.close()
