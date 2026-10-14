# SPDX-License-Identifier: MIT
"""Executor helpers that the sandbox's restrictions shape, the isolation probe, and the
executor's own protocol where the worker API cannot reach the case."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from sandbox_science.contracts import ProbeFacts
from sandbox_science.testing import FakeClock, eventually
from sandbox_science.worker import executor, sandbox_files

pytestmark = pytest.mark.slow


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0): check its state.
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            state = handle.read().rsplit(b")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != b"Z"


def _gone(pid: int) -> bool:
    for _ in range(100):
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _read_pid(path: Path) -> int:
    for _ in range(100):
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written")


def test_a_calls_leftovers_end_one_process_at_a_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # OpenShell's seccomp filter refuses kill(-pgid) but allows kill(pid) (lab 4), so the
    # cleanup must never need a group signal; and setsid must not get a process away.
    def refuse(pgid: int, signum: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refuse)
    owner, other_owner = f"test-executor-{uuid4().hex}", f"test-executor-{uuid4().hex}"
    script = (
        f"sleep 60 & echo $! > {tmp_path}/plain\n"
        f"setsid sleep 61 & echo $! > {tmp_path}/setsid\n"
        "wait"
    )
    call = subprocess.Popen(
        ["/bin/bash", "-c", script],
        env={**os.environ, executor.OWNER: owner},
        start_new_session=True,
    )
    other = subprocess.Popen(
        ["sleep", "60"],
        env={**os.environ, executor.OWNER: other_owner},
        start_new_session=True,
    )
    executor._spawned.add(call.pid)
    try:
        plain, escaped = _read_pid(tmp_path / "plain"), _read_pid(tmp_path / "setsid")
        info = executor.process_info(call.pid)
        assert info is not None
        assert executor.end_owned(owner, (call.pid, info.start)) >= 3
        call.wait(timeout=10)
        assert _gone(plain)
        assert _gone(escaped)
        assert _alive(other.pid)  # another owner's process is left alone
    finally:
        executor.end_owned(owner, None)
        call.wait(timeout=10)
        executor._spawned.discard(call.pid)
        other.kill()
        other.wait()


def test_parallel_fixture_owners_are_isolated_from_sibling_cleanup(tmp_path: Path) -> None:
    # end_owned also scans /proc outside its descendants. Independent pytest suites
    # must never reuse a mark: a sibling cleaner can see and kill that suite's owner.
    owners = [f"test-executor-{uuid4().hex}" for _ in range(2)]
    processes: list[subprocess.Popen[bytes]] = []
    cleaner = (
        "import sys\n"
        "from sandbox_science.worker import executor\n"
        "print(executor.end_owned(sys.argv[1], None))\n"
    )
    child = (
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    try:
        for index, owner in enumerate(owners):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", child, str(tmp_path / f"ready-{index}")],
                    env={**os.environ, executor.OWNER: owner},
                    start_new_session=True,
                )
            )
        for index, (process, owner) in enumerate(zip(processes, owners, strict=True)):
            # Popen's exec handshake can precede readable /proc environ. Wait for
            # actual child userspace readiness before asserting cleanup ownership.
            assert _read_pid(tmp_path / f"ready-{index}") == process.pid
            assert executor.process_owner(process.pid) == owner
        for index, owner in enumerate(owners):
            cleaned = subprocess.run(
                [sys.executable, "-c", cleaner, owner],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            assert int(cleaned.stdout) == 1
            assert processes[index].wait(timeout=10) == -9
            if index == 0:
                assert _alive(processes[1].pid)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_ending_an_owner_with_nothing_left_is_harmless() -> None:
    process = subprocess.Popen(["true"], start_new_session=True)
    process.wait()
    assert executor.end_owned(f"test-executor-{uuid4().hex}", (process.pid, 0)) == 0


def test_an_oversized_result_is_trimmed_to_fit_one_line() -> None:
    # H3: an end line over the worker's limit used to end the connection.
    text = "\x01" * (executor.MAX_LINE // 4)  # six bytes each once escaped
    end = {
        "type": "event",
        "execution": "exe-1",
        "event": "end",
        "state": "succeeded",
        "result": {
            "stdout": {"name": "stdout", "text": "head" + text + "tail", "total_bytes": 1},
            "outputs": [{"type": "display", "data": {"text/plain": "x" * 4096}}] * 500,
        },
    }
    line = executor.fit_line(end)
    assert line is not None
    assert len(line) <= executor.MAX_LINE
    fitted = json.loads(line)
    assert fitted["state"] == "succeeded"
    stdout = fitted["result"]["stdout"]
    assert stdout["truncated"] is True
    assert stdout["text"].startswith("head")
    assert stdout["text"].endswith("tail")
    assert "omitted to fit the result" in stdout["text"]


def test_a_reply_too_large_to_send_becomes_an_error() -> None:
    reply = {"type": "reply", "id": 7, "ok": True, "data": "x" * executor.MAX_LINE}
    line = executor.fit_line(reply)
    assert line is not None
    refused = json.loads(line)
    assert (refused["id"], refused["ok"], refused["error"]["kind"]) == (7, False, "internal")


def test_lines_are_utf8_whatever_the_text() -> None:
    line = executor.encode_line({"text": "é \udcff 😀"})  # a lone surrogate from a file name
    assert json.loads(line.decode("utf-8")) == {"text": "é ? 😀"}


def test_a_long_session_gets_a_kernel_id_the_worker_can_report(tmp_path: Path) -> None:
    long = executor.Kernel("s" * 128, "python", tmp_path, {}, last_used=0.0)
    short = executor.Kernel("s1", "python", tmp_path, {}, last_used=0.0)
    assert short.kernel_id == "krn-s1-python"
    assert len(long.kernel_id) <= 128
    assert long.kernel_id.startswith("krn-")
    assert long.kernel_id.endswith("-python")


class Recorder:
    """An in-process executor whose replies are kept."""

    def __init__(self, root: Path) -> None:
        self.messages: list[dict[str, Any]] = []
        self.executor = executor.Executor(root, self.send)

    async def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        await self.executor.handle(request)
        return self.messages[-1]


@pytest.mark.asyncio
async def test_reading_a_fifo_is_refused_at_once(tmp_path: Path) -> None:
    # M1: opening a FIFO waited for a writer, stalling every session.
    recorder = Recorder(tmp_path)
    (tmp_path / "sessions" / "s1").mkdir(parents=True)
    os.mkfifo(tmp_path / "sessions" / "s1" / "pipe")
    request = {"id": 1, "op": "read", "session": "s1", "path": "pipe"}
    reply = await asyncio.wait_for(recorder.ask(request), 5)
    assert reply["ok"] is False
    assert reply["error"]["kind"] == "invalid_request"
    assert "not a regular file" in reply["error"]["message"]


@pytest.mark.asyncio
async def test_a_read_hashes_the_whole_file_in_the_same_pass(tmp_path: Path) -> None:
    data = os.urandom(3 * 1024 * 1024 + 17)
    (tmp_path / "sessions" / "s1").mkdir(parents=True)
    (tmp_path / "sessions" / "s1" / "data.bin").write_bytes(data)
    recorder = Recorder(tmp_path)
    base = {"op": "read", "session": "s1", "path": "data.bin"}
    hashed = await recorder.ask({"id": 1, **base, "offset": 1024 * 1024 + 5, "length": 1500000})
    assert hashed["sha256"] == hashlib.sha256(data).hexdigest()
    assert hashed["size"] == len(data)
    content = base64.b64decode(hashed["content_b64"])
    assert content == data[1024 * 1024 + 5 : 1024 * 1024 + 5 + 1500000]
    assert hashed["eof"] is False
    # Publication's later ranges skip the hash.
    tail = await recorder.ask({"id": 2, **base, "offset": 3 * 1024 * 1024, "hash": False})
    assert "sha256" not in tail
    assert base64.b64decode(tail["content_b64"]) == data[3 * 1024 * 1024 :]
    assert tail["eof"] is True


@pytest.mark.asyncio
async def test_hashing_a_large_file_does_not_stall_the_executor(tmp_path: Path) -> None:
    # M1: a sparse file is instant to make and slow to hash.
    (tmp_path / "sessions" / "s1").mkdir(parents=True)
    with open(tmp_path / "sessions" / "s1" / "sparse", "wb") as handle:
        handle.truncate(2 * 1024**3)
    recorder = Recorder(tmp_path)
    loop = asyncio.get_running_loop()
    worst = 0.0

    async def tick() -> None:
        nonlocal worst
        while True:
            started = loop.time()
            await asyncio.sleep(0.01)
            worst = max(worst, loop.time() - started - 0.01)

    ticker = asyncio.ensure_future(tick())
    await asyncio.sleep(0.05)  # the ticker is running before the read starts
    try:
        reply = await recorder.ask(
            {"id": 1, "op": "read", "session": "s1", "path": "sparse", "length": 1}
        )
        await asyncio.sleep(0.05)  # the ticker sees a stall only once the loop runs again
    finally:
        ticker.cancel()
    assert reply["size"] == 2 * 1024**3
    assert worst < 0.5


@pytest.mark.asyncio
async def test_symlinked_areas_are_not_followed(tmp_path: Path) -> None:
    # A symlinked shared/ or sessions/<id> used to be followed by the file tools.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("not for the agent")
    root = tmp_path / "project"
    (root / "sessions").mkdir(parents=True)
    (root / "shared").symlink_to(outside)
    (root / "sessions" / "s3").symlink_to(outside)
    recorder = Recorder(root)
    for session, path in (("s1", "shared/secret"), ("s3", "secret")):
        reply = await recorder.ask({"id": 1, "op": "read", "session": session, "path": path})
        assert reply["ok"] is False, path
        assert "goes through a symlink" in reply["error"]["message"]
    listing = await recorder.ask({"id": 2, "op": "list", "session": "s1", "path": "shared"})
    assert listing["ok"] is False
    written = await recorder.ask(
        {"id": 3, "op": "write", "session": "s3", "path": "x", "content_b64": ""}
    )
    assert written["ok"] is False
    assert not (outside / "x").exists()
    files, _ = recorder.executor.snapshot("s3")
    assert files == {}


class Stdio:
    """An executor process, spoken to directly in its own protocol."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    @classmethod
    async def start(cls, root: Path) -> Stdio:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            executor.__file__,
            "--stdio",
            "--root",
            str(root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=executor.MAX_LINE,
        )
        stdio = cls(process)
        stdio.send({"id": -2, "op": "hello"})
        hello = await stdio.until(_reply(-2))
        stdio.send(
            {
                "id": -1,
                "op": "package_gate",
                "incarnation": hello["package_gate"],
                "generation": 0,
                "mode": "ready",
                "operation": "",
                "revision": None,
            }
        )
        assert (await stdio.until(_reply(-1)))["ok"]
        return stdio

    def send(self, *requests: dict[str, Any]) -> None:
        # One write: the executor reads them in one go, as it may from the worker.
        assert self.process.stdin is not None
        self.process.stdin.write(b"".join(json.dumps(item).encode() + b"\n" for item in requests))

    async def until(
        self, accept: Callable[[dict[str, Any]], bool], timeout: float = 120
    ) -> dict[str, Any]:
        assert self.process.stdout is not None
        while True:
            message = json.loads(await asyncio.wait_for(self.process.stdout.readline(), timeout))
            if accept(message):
                return message

    async def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 30)


def _end(execution: str) -> Callable[[dict[str, Any]], bool]:
    return lambda message: message.get("event") == "end" and message["execution"] == execution


def _reply(request_id: int) -> Callable[[dict[str, Any]], bool]:
    return lambda message: message.get("type") == "reply" and message.get("id") == request_id


@pytest.mark.asyncio
async def test_the_shared_scan_reports_stamps_digests_and_the_mark(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    (shared / "bin").mkdir(parents=True)
    (shared / "a.txt").write_bytes(b"a")
    (shared / "bin" / "run.sh").write_bytes(b"#!/bin/sh\n")
    (shared / "bin" / "run.sh").chmod(0o755)
    (shared / "big.bin").write_bytes(b"x" * 100)
    (shared / ".write-0123456789abcdef").write_bytes(b"a write under way")
    (shared / "bad\x01name").write_bytes(b"?")
    recorder = Recorder(tmp_path)
    wanted = ["shared/a.txt", "shared/bin/run.sh", "shared/big.bin"]
    request = {"id": 1, "op": "shared", "hash": wanted, "hash_limit": 50, "hash_budget": 1 << 20}
    reply = await recorder.ask({**request, "mark": "m1 p1"})
    assert reply["ok"] is True
    assert sorted(reply["files"]) == ["shared/a.txt", "shared/big.bin", "shared/bin/run.sh"]
    info = os.stat(shared / "a.txt")
    assert reply["files"]["shared/a.txt"] == [info.st_mtime_ns, 1, info.st_ino, 0]
    assert reply["files"]["shared/bin/run.sh"][3] == 1
    assert reply["sha256"] == {
        "shared/a.txt": hashlib.sha256(b"a").hexdigest(),
        "shared/bin/run.sh": hashlib.sha256(b"#!/bin/sh\n").hexdigest(),
    }
    assert reply["too_large"] == ["shared/big.bin"]
    assert reply["odd"] == ["shared/bad\x01name"]
    # A name that can never be synced proves nothing missing, so the scan is not partial.
    assert reply["partial"] is False
    assert reply["mark"] == "m1 p1"
    assert (tmp_path / ".sandbox-science-sync").read_text() == "m1 p1"
    again = await recorder.ask(
        {"id": 2, "op": "shared", "hash": [], "hash_limit": 0, "hash_budget": 0}
    )
    assert again["mark"] == "m1 p1"
    assert again["sha256"] == {}


def test_a_digest_belongs_to_the_stamp_it_was_scanned_with(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"one")
    info = os.stat(tmp_path / "f")
    stamp = [info.st_mtime_ns, info.st_size, info.st_ino]
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        digest = sandbox_files.stamped_sha256(directory, "f", stamp)
        assert digest == hashlib.sha256(b"one").hexdigest()
        # Replaced since the scan: no digest, so the worker compares it again later.
        (tmp_path / "g").write_bytes(b"two")
        os.replace(tmp_path / "g", tmp_path / "f")
        assert sandbox_files.stamped_sha256(directory, "f", stamp) is None
    finally:
        os.close(directory)


@pytest.mark.asyncio
async def test_a_shared_file_is_removed_only_as_it_was_synced(tmp_path: Path) -> None:
    deep = tmp_path / "shared" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "f.txt").write_bytes(b"synced")
    recorder = Recorder(tmp_path)
    base = {"op": "remove", "path": "shared/d/e/f.txt"}
    changed = await recorder.ask({"id": 1, **base, "expected_sha256": "0" * 64})
    assert changed["error"]["kind"] == "conflict"
    assert (deep / "f.txt").exists()
    expected = hashlib.sha256(b"synced").hexdigest()
    removed = await recorder.ask({"id": 2, **base, "expected_sha256": expected})
    assert removed["ok"] is True
    # The directories it leaves empty go too, up to the shared area.
    assert not (tmp_path / "shared" / "d").exists()
    assert (tmp_path / "shared").is_dir()
    (tmp_path / "sessions" / "s1").mkdir(parents=True)
    (tmp_path / "sessions" / "s1" / "w.txt").write_bytes(b"w")
    outside = await recorder.ask(
        {"id": 3, "op": "remove", "session": "s1", "path": "w.txt", "expected_sha256": "0" * 64}
    )
    assert outside["error"]["kind"] == "invalid_request"


@pytest.mark.asyncio
async def test_a_sync_write_sets_the_mode_and_reports_the_stamp(tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    recorder = Recorder(tmp_path)
    reply = await recorder.ask(
        {
            "id": 1,
            "op": "write",
            "path": "shared/bin/run.sh",
            "content_b64": base64.b64encode(b"#!/bin/sh\n").decode(),
            "create_only": True,
            "executable": True,
        }
    )
    written = tmp_path / "shared" / "bin" / "run.sh"
    info = os.stat(written)
    assert reply["stamp"] == [info.st_mtime_ns, info.st_size, info.st_ino]
    assert info.st_mode & 0o777 == 0o755


@pytest.mark.asyncio
async def test_a_moved_file_keeps_its_inode_and_its_writers(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "results.csv").write_bytes(b"n\n1\n")
    recorder = Recorder(tmp_path)
    with open(shared / "results.csv", "ab") as writer:  # a job appending to it
        moved = await recorder.ask(
            {
                "id": 1,
                "op": "move",
                "path": "shared/results.csv",
                "target": "shared/results.conflict-env2.csv",
            }
        )
        writer.write(b"2\n")
    target = shared / "results.conflict-env2.csv"
    assert moved["ok"] is True
    assert not (shared / "results.csv").exists()
    assert target.read_bytes() == b"n\n1\n2\n"
    info = os.stat(target)
    assert moved["stamp"][2] == info.st_ino
    (shared / "other.csv").write_bytes(b"x")
    taken = await recorder.ask(
        {
            "id": 2,
            "op": "move",
            "path": "shared/other.csv",
            "target": "shared/results.conflict-env2.csv",
        }
    )
    assert taken["error"]["kind"] == "conflict"
    assert target.read_bytes() == b"n\n1\n2\n"


def _holder(path: Path, mode: str) -> subprocess.Popen[bytes]:
    """A process that keeps ``path`` open in ``mode`` until it is killed."""
    script = (
        "import sys, time; f = open(sys.argv[1], sys.argv[2]); print(flush=True); time.sleep(30)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(path), mode], stdout=subprocess.PIPE
    )
    assert holder.stdout is not None
    holder.stdout.readline()  # the file is open
    return holder


def test_only_a_writer_keeps_a_file_from_being_replaced(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    target = shared / "data.h5"
    target.write_bytes(b"v1")
    reader = _holder(target, "rb")
    try:
        assert not sandbox_files.being_written(os.path.realpath(target))
        directory = os.open(shared, os.O_RDONLY | os.O_DIRECTORY)
        expected = hashlib.sha256(b"v1").hexdigest()
        reply = sandbox_files.write_file(
            directory, "data.h5", b"v2", expected, False, "x", None, True
        )
        assert "busy" not in reply
        assert target.read_bytes() == b"v2"
    finally:
        reader.kill()
        reader.wait()
    writer = _holder(target, "ab")
    try:
        assert sandbox_files.being_written(os.path.realpath(target))
        directory = os.open(shared, os.O_RDONLY | os.O_DIRECTORY)
        expected = hashlib.sha256(b"v2").hexdigest()
        reply = sandbox_files.write_file(
            directory, "data.h5", b"v3", expected, False, "x", None, True
        )
        assert reply == {"busy": True}
        assert target.read_bytes() == b"v2"
        assert [path.name for path in shared.iterdir()] == ["data.h5"]  # nothing left over
    finally:
        writer.kill()
        writer.wait()


def test_a_writer_that_opens_the_file_just_before_the_swap_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "log.csv"
    target.write_bytes(b"v1\n")
    exchange = sandbox_files._exchange
    holders: list[subprocess.Popen[bytes]] = []

    def a_job_opens_it_first(parent: int, first: str, second: str) -> bool:
        if not holders:  # after the check made before the swap
            holders.append(_holder(target, "ab"))
        return exchange(parent, first, second)

    monkeypatch.setattr(sandbox_files, "_exchange", a_job_opens_it_first)
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    expected = hashlib.sha256(b"v1\n").hexdigest()
    try:
        reply = sandbox_files.write_file(
            directory, "log.csv", b"v2\n", expected, False, "x", None, True
        )
        assert reply == {"busy": True}  # swapped, seen and swapped back
        assert target.read_bytes() == b"v1\n"
        assert [path.name for path in tmp_path.iterdir()] == ["log.csv"]
    finally:
        for holder in holders:
            holder.kill()
            holder.wait()


def test_a_write_that_lands_after_the_check_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "f").write_bytes(b"old")
    expected = hashlib.sha256(b"old").hexdigest()
    hashed = sandbox_files._hashed

    def and_then_research_code_writes(
        parent: int, name: str, path: str
    ) -> tuple[str, os.stat_result]:
        result = hashed(parent, name, path)
        (tmp_path / "f").write_bytes(b"theirs")
        return result

    monkeypatch.setattr(sandbox_files, "_hashed", and_then_research_code_writes)
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    with pytest.raises(executor.ToolError, match="changed while it was written"):
        sandbox_files.write_file(directory, "f", b"ours", expected, False, "f")
    assert (tmp_path / "f").read_bytes() == b"theirs"


def test_a_write_landing_just_before_the_swap_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "f").write_bytes(b"old")
    exchange = sandbox_files._exchange
    swaps: list[tuple[str, str]] = []

    def research_code_writes_first(parent: int, first: str, second: str) -> bool:
        if not swaps:
            with open(tmp_path / "f", "ab") as handle:  # after every check
                handle.write(b"+theirs")
        swaps.append((first, second))
        return exchange(parent, first, second)

    monkeypatch.setattr(sandbox_files, "_exchange", research_code_writes_first)
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    expected = hashlib.sha256(b"old").hexdigest()
    with pytest.raises(executor.ToolError, match="changed while it was written"):
        sandbox_files.write_file(directory, "f", b"ours", expected, False, "f")
    assert (tmp_path / "f").read_bytes() == b"old+theirs"
    assert len(swaps) == 2  # swapped, and swapped back
    assert sorted(path.name for path in tmp_path.iterdir()) == ["f"]


@pytest.mark.asyncio
async def test_a_pipe_in_place_of_the_mark_does_not_hang_the_scan(tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    os.mkfifo(tmp_path / ".sandbox-science-sync")
    recorder = Recorder(tmp_path)
    request = {"id": 1, "op": "shared", "hash": [], "hash_limit": 0, "hash_budget": 0}
    reply = await asyncio.wait_for(recorder.ask(request), 5)
    assert reply["mark"] is None


def test_escaped_names_count_in_full_against_the_reply_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    for n in range(10):
        (shared / ("\\" * 20 + str(n))).write_bytes(b"x")
    # Each path is about 30 bytes as text, about 70 once JSON escapes it.
    monkeypatch.setattr(sandbox_files, "SHARED_REPLY_BYTES", 400)
    directory = os.open(shared, os.O_RDONLY | os.O_DIRECTORY)
    files, truncated, _, _ = sandbox_files.scan_shared(directory)
    assert truncated
    assert len(files) < 10


@pytest.mark.asyncio
async def test_a_shell_call_cancelled_before_it_starts_ends_at_once(tmp_path: Path) -> None:
    # A cancel read together with its shell call cancelled a task that had not started:
    # no end ever came, and the call stayed in the executor's table.
    stdio = await Stdio.start(tmp_path)
    try:
        stdio.send({"id": 1, "op": "hello"})
        await stdio.until(_reply(1))
        stdio.send(
            {
                "id": 2,
                "op": "shell",
                "execution": "exe-1",
                "session": "s1",
                "script": "touch ran",
                "timeout": 30,
                "limit": 4096,
            },
            {"id": 3, "op": "cancel", "execution": "exe-1"},
        )
        end = await stdio.until(_end("exe-1"), timeout=10)
        assert end["state"] == "cancelled"
        assert not (tmp_path / "sessions" / "s1" / "ran").exists()
        stdio.send({"id": 4, "op": "cancel", "execution": "exe-1"})
        assert (await stdio.until(_reply(4)))["found"] is False
    finally:
        await stdio.close()


@pytest.mark.asyncio
async def test_kernel_cancels_and_restarts_say_what_happened(tmp_path: Path) -> None:
    stdio = await Stdio.start(tmp_path)
    cell = {"op": "cell", "session": "s1", "timeout": 60, "limit": 65536}
    try:
        # Cancelled while its kernel starts: it never runs.
        stdio.send({"id": 1, **cell, "execution": "exe-a", "code": "open('ran', 'w').close()"})
        await stdio.until(_reply(1))
        stdio.send({"id": 2, "op": "cancel", "execution": "exe-a"})
        end = await stdio.until(_end("exe-a"))
        assert end["state"] == "cancelled"
        assert not (tmp_path / "sessions" / "s1" / "ran").exists()
        # A restart does not wait for the running cell, which ends cancelled.
        stdio.send({"id": 3, **cell, "execution": "exe-b", "code": "import time; time.sleep(30)"})
        await stdio.until(lambda message: message.get("event") == "state")
        started = time.monotonic()
        stdio.send({"id": 4, "op": "restart", "session": "s1", "language": "python"})
        seen: dict[str, dict[str, Any]] = {}
        while len(seen) < 2:  # the reply and the cell's end, in either order
            message = await stdio.until(lambda item: _reply(4)(item) or _end("exe-b")(item))
            seen[message["type"]] = message
        restarted, end = seen["reply"], seen["event"]
        assert time.monotonic() - started < 25
        assert restarted["kernel"]["generation"] == 2
        assert end["state"] == "cancelled"
        assert end["result"]["error"]["message"] == "the kernel was restarted"
        assert end["result"]["kernel"]["generation"] == 1
    finally:
        await stdio.close()


@pytest.mark.asyncio
async def test_kernels_started_together_each_get_their_own_unix_socket_names(
    tmp_path: Path,
) -> None:
    # Over TCP, Jupyter picks free ports that the kernel binds later, and kernels started
    # together could take each other's: one died (EADDRINUSE) or never answered.
    stdio = await Stdio.start(tmp_path)
    code = (
        "from ipykernel.connect import get_connection_info\n"
        "info = get_connection_info(unpack=True)\n"
        "print(info['transport'], info['ip'])"
    )
    sessions = [f"s{number}" for number in range(6)]
    try:
        stdio.send(
            *(
                {
                    "id": number,
                    "op": "cell",
                    "session": session,
                    "execution": f"exe-{session}",
                    "timeout": 60,
                    "limit": 65536,
                    "code": code,
                }
                for number, session in enumerate(sessions)
            )
        )
        ends: dict[str, dict[str, Any]] = {}
        while len(ends) < len(sessions):
            message = await stdio.until(lambda item: item.get("event") == "end")
            ends[message["execution"]] = message
        addresses = set[str]()
        for end in ends.values():
            assert end["state"] == "succeeded", end
            transport, address = end["result"]["stdout"]["text"].split()
            assert transport == "ipc"
            assert address.startswith("@ss-kernel-")
            addresses.add(address)
        assert len(addresses) == len(sessions)
    finally:
        await stdio.close()


def test_capture_keeps_head_and_tail() -> None:
    capture = executor.Capture("stdout", 10)
    live = capture.feed(b"abcdefghij0123456789")
    assert live == "abcde"
    summary = capture.summary()
    assert summary["truncated"] is True
    assert summary["total_bytes"] == 20
    assert summary["text"].startswith("abcde\n[… 10 bytes omitted …]\n")
    assert summary["text"].endswith("56789")


def test_the_environments_tools_come_first_on_path(tmp_path: Path) -> None:
    # Lab 5: Sage's gp and GAP interfaces look their programs up on PATH.
    prefix = tmp_path / "envs" / "sage"
    (prefix / "conda-meta").mkdir(parents=True)
    tools = str(prefix / "bin")
    env = executor.tool_environment(
        {"PATH": f"/usr/bin:{tools}:/bin", "HOME": "/sandbox"}, f"{tools}/python", str(prefix)
    )
    assert env["PATH"] == f"{tools}:/usr/bin:/bin"
    assert env["CONDA_PREFIX"] == str(prefix)
    assert env["HOME"] == "/sandbox"


def test_a_plain_interpreter_gets_no_conda_prefix(tmp_path: Path) -> None:
    env = executor.tool_environment({}, str(tmp_path / "bin" / "python3"), str(tmp_path))
    assert env["PATH"] == str(tmp_path / "bin")
    assert "CONDA_PREFIX" not in env


def test_probe_facts_describe_this_process() -> None:
    facts = ProbeFacts.model_validate(executor.probe_facts())
    assert facts.uid == os.getuid()
    assert facts.hostname == socket.gethostname()
    assert facts.kernel_release == platform.release()
    assert facts.boot_id == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert facts.seccomp is not None
    assert facts.no_new_privs is not None
    assert facts.capabilities is not None


def test_sentinel_states(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.write_text("x")
    assert executor.sentinel_state(str(present)) == "present"
    assert executor.sentinel_state(str(tmp_path / "absent")) == "absent"
    assert executor.sentinel_state(str(present / "below-a-file")) == "absent"
    assert executor.sentinel_state("relative/path") == "unknown"


def test_a_sentinel_behind_a_closed_directory_is_unknown(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root searches any directory")
    closed = tmp_path / "closed"
    closed.mkdir()
    (closed / "file").write_text("x")
    closed.chmod(0)
    try:
        assert executor.sentinel_state(str(closed / "file")) == "unknown"
    finally:
        closed.chmod(0o700)


@pytest.mark.asyncio
async def test_the_probe_operation(tmp_path: Path) -> None:
    replies: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        replies.append(message)

    probing = executor.Executor(tmp_path, send)
    present = tmp_path / "sentinel"
    present.write_text("x")
    paths = [str(present), str(tmp_path / "absent")]
    await probing.handle({"id": 1, "op": "probe", "sentinels": paths})
    for bad in ("/one/path", [1], [str(present)] * (executor.MAX_SENTINELS + 1)):
        await probing.handle({"id": 2, "op": "probe", "sentinels": bad})
    reply, *refusals = replies
    assert reply["ok"] is True
    assert reply["sentinels"] == {paths[0]: "present", paths[1]: "absent"}
    assert ProbeFacts.model_validate(reply["facts"]).uid == os.getuid()
    assert [refusal["error"]["kind"] for refusal in refusals] == ["invalid_request"] * 3


@pytest.mark.parametrize(
    ("sockets", "protected", "calls"),
    [
        # Sockets still work once it is not dumpable: it stays protected.
        ([True, True], True, [(executor.PR_SET_DUMPABLE, 0)]),
        # OpenShell: sockets stop working, so it becomes dumpable again (lab 8).
        ([True, False], False, [(executor.PR_SET_DUMPABLE, 0), (executor.PR_SET_DUMPABLE, 1)]),
        # No sockets to lose: protected anyway.
        ([False, False], True, [(executor.PR_SET_DUMPABLE, 0)]),
    ],
)
def test_the_executor_stays_dumpable_where_sockets_need_it(
    monkeypatch: pytest.MonkeyPatch,
    sockets: list[bool],
    protected: bool,
    calls: list[tuple[int, int]],
) -> None:
    answers = iter(sockets)
    made: list[tuple[int, int]] = []

    def prctl(option: int, value: int) -> bool:
        made.append((option, value))
        return True

    monkeypatch.setattr(executor, "_sockets_work", lambda: next(answers))
    monkeypatch.setattr(executor, "_prctl", prctl)
    assert executor.protect_process() is protected
    assert made == calls


def test_a_refused_prctl_leaves_the_executor_unprotected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_sockets_work", lambda: True)
    monkeypatch.setattr(executor, "_prctl", lambda option, value: False)
    assert executor.protect_process() is False


def test_unix_sockets_work_here() -> None:
    assert executor._sockets_work()


@pytest.mark.parametrize("call", ["bind", "listen", "connect", "accept"])
def test_a_refused_socket_call_makes_the_probe_say_no(
    monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    # A sandbox may refuse any of the calls a kernel's client makes, not only bind.
    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(socket.socket, call, refuse)
    assert executor._sockets_work() is False


def test_pdf_requests_become_the_helpers_command_line() -> None:
    argv = executor.pdf_argv(
        {
            "operation": "render",
            "path": "paper.pdf",
            "page": 3,
            "dpi": 100,
            "region": [72, 650.5, 300, 720],
        }
    )
    assert argv[:4] == [sys.executable, "-I", str(executor.PDF_HELPER), "render"]
    assert argv[4:] == ["paper.pdf", "--page", "3", "--dpi", "100", "--region", "72,650.5,300,720"]
    pages = executor.pdf_argv(
        {"operation": "pages", "path": "p.pdf", "pages": "1-3", "include_artifacts": True}
    )
    assert pages[3:] == ["pages", "p.pdf", "--pages", "1-3", "--include-artifacts"]
    with pytest.raises(executor.ToolError, match="no PDF operation"):
        executor.pdf_argv({"operation": "delete", "path": "p.pdf"})


def test_pdf_is_offered_with_the_helper_and_pdf_oxide() -> None:
    pytest.importorskip("pdf_oxide")
    assert executor.available_features() == ["jobs", "pdf"]


def test_the_kernel_idle_option_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        executor.main(["--stdio", "--root", str(tmp_path), "--kernel-idle", "0"])


@pytest.mark.asyncio
async def test_an_idle_kernel_is_stopped_once_it_has_been_unused_long_enough(
    tmp_path: Path,
) -> None:
    # The reaper reads the fake clock, so a kernel's idle time passes only when the test
    # moves the clock; the next cell in the session reports the lost state.
    clock = FakeClock()
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    instance = executor.Executor(tmp_path, send, kernel_idle=10, clock=clock)

    async def seen(accept: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        return await eventually(lambda: next((m for m in messages if accept(m)), None), timeout=120)

    async def ask(request: dict[str, Any]) -> dict[str, Any]:
        await instance.handle(request)
        return await seen(_reply(request["id"]))

    async def cell(number: int, code: str) -> dict[str, Any]:
        execution = f"exe-{number}"
        request = {"op": "cell", "session": "s1", "timeout": 60, "limit": 65536}
        await ask({"id": number, **request, "execution": execution, "code": code})
        return await seen(_end(execution))

    hello = await ask({"id": -2, "op": "hello"})
    gate = {"incarnation": hello["package_gate"], "generation": 0, "mode": "ready"}
    ready = await ask({"id": -1, "op": "package_gate", **gate, "operation": "", "revision": None})
    assert ready["ok"]
    try:
        assert (await cell(1, "x = 1"))["state"] == "succeeded"
        kernel = instance.kernels[("s1", "python")]
        reaper = instance.spawn(instance.reap_idle_kernels(10))
        await eventually(lambda: clock.sleepers == 1)
        clock.advance(5)  # the reaper looks, and keeps a kernel used 5 s ago
        await eventually(lambda: clock.sleepers == 1)
        assert kernel.manager is not None
        clock.advance(5)
        await eventually(lambda: kernel.manager is None)
        after = await cell(2, "x")
        assert after["result"]["state_lost"] is True
        assert after["result"]["error"]["name"] == "NameError"
        reaper.cancel()
    finally:
        await instance.shutdown()
