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

import pytest

from sandbox_science.contracts import ProbeFacts
from sandbox_science.worker import executor


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
    script = (
        f"sleep 60 & echo $! > {tmp_path}/plain\n"
        f"setsid sleep 61 & echo $! > {tmp_path}/setsid\n"
        "wait"
    )
    call = subprocess.Popen(  # noqa: S603 - a fixed command
        ["/bin/bash", "-c", script],
        env={**os.environ, executor.OWNER: "call-test"},
        start_new_session=True,
    )
    other = subprocess.Popen(
        ["sleep", "60"],  # noqa: S607
        env={**os.environ, executor.OWNER: "call-other"},
        start_new_session=True,
    )
    executor._spawned.add(call.pid)  # pyright: ignore[reportPrivateUsage]
    try:
        plain, escaped = _read_pid(tmp_path / "plain"), _read_pid(tmp_path / "setsid")
        info = executor.process_info(call.pid)
        assert info is not None
        assert executor.end_owned("call-test", (call.pid, info.start)) >= 3
        call.wait(timeout=10)
        assert _gone(plain)
        assert _gone(escaped)
        assert _alive(other.pid)  # another owner's process is left alone
    finally:
        executor._spawned.discard(call.pid)  # pyright: ignore[reportPrivateUsage]
        other.kill()
        other.wait()


def test_ending_an_owner_with_nothing_left_is_harmless() -> None:
    process = subprocess.Popen(["true"], start_new_session=True)  # noqa: S607
    process.wait()
    assert executor.end_owned("call-finished", (process.pid, 0)) == 0


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
    long = executor.Kernel("s" * 128, "python", tmp_path, {})
    short = executor.Kernel("s1", "python", tmp_path, {})
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
        return cls(process)

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


def test_loopback_sockets_work_here() -> None:
    assert executor._sockets_work()  # pyright: ignore[reportPrivateUsage]


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
    assert executor.available_features() == ["pdf"]


def test_the_kernel_idle_option_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        executor.main(["--stdio", "--root", str(tmp_path), "--kernel-idle", "0"])
