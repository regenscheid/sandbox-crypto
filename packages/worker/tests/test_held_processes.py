# SPDX-License-Identifier: MIT
"""A delegated run's processes are held to its write area (delegated agents spec, DA-20,
fixtures DA-F9 and DA-F10), through the worker API on the local backend with real
commands and kernels: a shell command, a job and a Python cell whose context names
``work/<delegation_id>`` write in that area and the run's scratch directory and nowhere
else, cannot reach another run's kernel or signal its processes, and are refused, never
run unconfined, on an executor that cannot hold them."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from sandbox_science.contracts import ExecutionContext, KernelRestartRequest
from sandbox_science.worker import executor, sandbox_confine
from sandbox_science.worker.errors import WorkerError

from .harness import Harness

_, UNAVAILABLE = sandbox_confine.abi()
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(UNAVAILABLE is not None, reason=f"no confinement here: {UNAVAILABLE}"),
]

AREA = "work/dlg-a"
SIBLING = "work/dlg-b"

ATTEMPTS = {
    "parent": "sources/a.md",
    "new_parent_file": "sources/new.md",
    "input": "inputs/data.csv",
    "sibling": f"{SIBLING}/notes.md",
    "shared": "../../shared/x.txt",
    "executor": "../../.executor/ipython/injected.py",
}
"""Paths a child's code tries to write, relative to the session directory."""


def _in(area: str | None = None) -> dict[str, str]:
    context = {"project_id": "p1", "session_id": "s1", "environment_id": "env1"}
    return context if area is None else {**context, "write_area": area}


@pytest_asyncio.fixture
async def worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path) as started:
        yield started


@pytest_asyncio.fixture
async def daemon_worker(tmp_path: Path) -> AsyncIterator[Harness]:
    async with await Harness.start(tmp_path, daemon=True) as started:
        yield started


async def _seed(worker: Harness) -> None:
    """The parent's files, its input copy and a sibling's area, written by the platform."""
    for path in ("sources/a.md", "inputs/data.csv", f"{SIBLING}/notes.md"):
        status, body = await worker.post(
            "/v1/files/write",
            {
                "context": _in(),
                "path": path,
                "content_b64": base64.b64encode(b"original\n").decode(),
            },
        )
        assert status == 200, body


def _unchanged(worker: Harness) -> None:
    session = worker.workspace
    for path in ("sources/a.md", "inputs/data.csv", f"{SIBLING}/notes.md"):
        assert (session / path).read_text() == "original\n", path
    assert not (session / "sources/new.md").exists()
    assert not (session.parent.parent / "shared/x.txt").exists()
    assert not (session.parent.parent / ".executor/ipython/injected.py").exists()


SHELL = """
set -u
pwd
echo "work=$RUN_WORK tmp=$TMPDIR"
echo mine > notes.md && echo wrote-area
echo scratch > "$TMPDIR/t" && echo wrote-tmp
for path in {paths}; do
  if (echo child > "$SESSION_WORKSPACE/$path") 2>/dev/null; then echo "WROTE $path"; fi
done
rm -f "$SESSION_WORKSPACE/sources/a.md" 2>/dev/null && echo REMOVED
mv notes.md "$SESSION_WORKSPACE/sources/moved.md" 2>/dev/null && echo MOVED
ln -s "$SESSION_WORKSPACE/sources" out && (echo child > out/a.md) 2>/dev/null && echo LINKED
echo done
"""


async def test_a_childs_shell_writes_its_area_and_nothing_else(worker: Harness) -> None:
    """DA-F9: the shell starts in the area, writes it and $TMPDIR; the parent's file, a new
    file beside it, the input copy, a sibling's area, shared/ and the executor's files are
    refused, as are removing, moving out and writing through a link."""
    await _seed(worker)
    script = SHELL.format(paths=" ".join(ATTEMPTS.values()))
    execution = await worker.run("child-shell", _in(AREA), kind="shell", script=script)
    assert execution["state"] == "succeeded", execution
    lines = execution["result"]["stdout"]["text"].splitlines()
    area = worker.workspace / AREA
    assert lines[0] == str(area)
    assert lines[1].startswith(f"work={area} tmp=")
    assert lines[2:] == ["wrote-area", "wrote-tmp", "done"], lines
    assert (area / "notes.md").read_text() == "mine\n"
    _unchanged(worker)


async def test_the_sandbox_daemon_holds_a_child_as_the_executor_does(
    daemon_worker: Harness,
) -> None:
    """The daemon passes the request and the executor's ``confinement`` on unchanged."""
    worker = daemon_worker
    await _seed(worker)
    script = 'echo mine > notes.md; (echo child > "$SESSION_WORKSPACE/inputs/data.csv") || true'
    execution = await worker.run("daemon-shell", _in(AREA), kind="shell", script=script)
    assert execution["state"] == "succeeded", execution
    assert (worker.workspace / AREA / "notes.md").read_text() == "mine\n"
    _unchanged(worker)


async def test_the_parent_and_the_platform_are_not_held(worker: Harness) -> None:
    """A request without a write area (a main run's) writes anywhere, as before."""
    await _seed(worker)
    execution = await worker.run(
        "parent-shell", _in(), kind="shell", script="echo parent > sources/a.md && pwd"
    )
    assert execution["state"] == "succeeded", execution
    assert execution["result"]["stdout"]["text"] == f"{worker.workspace}\n"
    assert (worker.workspace / "sources/a.md").read_text() == "parent\n"


async def test_a_childs_job_is_held_too(worker: Harness) -> None:
    await _seed(worker)
    job = await worker.run(
        "child-job",
        _in(AREA),
        kind="job",
        purpose="a held job",
        expected_progress="none",
        script='echo kept > out.txt; echo child > "$SESSION_WORKSPACE/inputs/data.csv"',
    )
    assert job["state"] == "failed", job
    assert (worker.workspace / AREA / "out.txt").read_text() == "kept\n"
    _unchanged(worker)


SIGNALS = """
set -u
sleep 30 & own=$!
kill -TERM $own; wait $own; echo "own $?"
for target in $KERNEL $EXECUTOR; do
  kill -0 $target 2>/dev/null && echo "ESCAPE $target"
  kill -TERM $target 2>/dev/null && echo "ESCAPE TERM $target"
  kill -CONT $target 2>/dev/null && echo "ESCAPE CONT $target"
done
kill -0 -- -$KERNEL_GROUP 2>/dev/null && echo "ESCAPE group"
timeout 1 sleep 10; echo "timeout $?"
python3 - <<'PY'
import concurrent.futures, multiprocessing, subprocess
def square(x):
    return x * x
if __name__ == "__main__":
    with multiprocessing.Pool(2) as pool:
        print("pool", sum(pool.map(square, range(10))))
    pool = multiprocessing.Pool(2)
    pool.terminate()
    pool.join()
    print("pool terminated")
    with concurrent.futures.ProcessPoolExecutor(2) as executor:
        print("futures", sum(executor.map(square, range(10))))
    child = subprocess.Popen(["sleep", "30"])
    child.kill()
    print("kill", child.wait())
PY
echo done
"""


async def test_a_childs_shell_signals_its_own_processes_and_nothing_else(
    worker: Harness,
) -> None:
    """DA-20's signal rule with the signal shim: the shell's ``kill``, ``timeout``, a
    multiprocessing pool and its terminate, a process pool and ``Popen.kill`` reach the
    child's own processes; ``kill`` of the parent's kernel, the executor or the kernel's
    process group is refused, and the parent's kernel keeps running."""
    parent = await worker.run(
        "parent", _in(), kind="cell", code="import os; (os.getpid(), os.getppid())"
    )
    kernel, executor_pid = json.loads(
        parent["result"]["outputs"][0]["data"]["text/plain"].replace("(", "[").replace(")", "]")
    )
    script = (
        f"KERNEL={kernel} EXECUTOR={executor_pid} KERNEL_GROUP={os.getpgid(kernel)}\n" + SIGNALS
    )
    child = await worker.run("child-signals", _in(AREA), kind="shell", script=script)
    assert child["state"] == "succeeded", child
    assert child["result"]["stdout"]["text"].splitlines() == [
        "own 143",
        "timeout 124",
        "pool 285",
        "pool terminated",
        "futures 285",
        "kill -9",
        "done",
    ], child["result"]
    again = await worker.run("parent-again", _in(), kind="cell", code="os.getpid()")
    assert again["result"]["outputs"][0]["data"]["text/plain"] == str(kernel)


CELL = """
import json, os, socket
from pathlib import Path
session = Path(os.environ["SESSION_WORKSPACE"])
import ipykernel.connect
own = ipykernel.connect.get_connection_info(unpack=True)["ip"]
found = {"cwd": os.getcwd(), "wrote": [], "tried": 0, "connected": [], "signalled": False}
Path("cell.txt").write_text("mine")
for path in PATHS:
    try:
        (session / path).write_text("child")
        found["wrote"].append(path)
    except OSError:
        pass
for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
    name = line.split()[-1] if len(line.split()) > 7 else ""
    if name.startswith("@ss-kernel-") and not name.startswith(own):
        found["tried"] += 1
        with socket.socket(socket.AF_UNIX) as client:
            try:
                client.connect("\\0" + name[1:])
                found["connected"].append(name)
            except OSError:
                pass
import ctypes, signal, subprocess
value = ctypes.c_void_p(0)
libc = ctypes.CDLL(None, use_errno=True)
libc.sigqueue.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
for attempt in (
    lambda: os.kill(PARENT, 0),
    lambda: os.kill(PARENT, signal.SIGTERM),
    lambda: os.killpg(os.getpgid(PARENT), 0),
):
    try:
        attempt()
        found["signalled"] = True
    except OSError:
        pass
if libc.sigqueue(PARENT, signal.SIGTERM, value) == 0:
    found["signalled"] = True
own = subprocess.Popen(["sleep", "30"])
own.terminate()
found["own"] = own.wait()
print(json.dumps(found))
"""


async def test_a_childs_kernel_is_held_and_cannot_reach_the_parents(worker: Harness) -> None:
    """DA-F9 for a cell, and DA-F10: the child's own kernel starts in its area, writes
    nothing outside, cannot connect to the parent's kernel's sockets and cannot signal it;
    a restart keeps it held."""
    await _seed(worker)
    parent = await worker.run("parent-cell", _in(), kind="cell", code="import os; os.getpid()")
    assert parent["state"] == "succeeded", parent
    pid = int(parent["result"]["outputs"][0]["data"]["text/plain"])
    code = f"PATHS = {json.dumps(list(ATTEMPTS.values()))}\nPARENT = {pid}\n" + CELL
    for attempt in ("first", "restarted"):
        child = await worker.run(
            f"child-cell-{attempt}", _in(AREA), kind="cell", code=code, run="r-child"
        )
        assert child["state"] == "succeeded", child["result"]["error"]
        found = json.loads(child["result"]["stdout"]["text"])
        assert found.pop("tried") >= 5  # the parent's kernel's channels
        assert found == {
            "cwd": str(worker.workspace / AREA),
            "wrote": [],
            "connected": [],
            "signalled": False,
            "own": -15,  # its own process, through the signal shim
        }
        assert (worker.workspace / AREA / "cell.txt").read_text() == "mine"
        if attempt == "first":
            await worker.manager.restart_kernel(
                KernelRestartRequest(
                    context=ExecutionContext.model_validate(_in(AREA)), run="r-child"
                )
            )
    _unchanged(worker)
    again = await worker.run("parent-again", _in(), kind="cell", code="os.getpid()")
    assert again["result"]["outputs"][0]["data"]["text/plain"] == str(pid)  # untouched


async def test_a_childs_cell_needs_its_own_kernel_with_its_own_area(worker: Harness) -> None:
    """A cell naming a write area but no run would use the session's kernel, which is not
    held: refused. A run's kernel keeps the area it started with."""
    no_run = await worker.run("no-run", _in(AREA), kind="cell", code="1")
    assert no_run["state"] == "failed"
    assert no_run["result"]["error"]["kind"] == "invalid_request"
    first = await worker.run("first", _in(AREA), kind="cell", code="1", run="r1")
    assert first["state"] == "succeeded", first
    other = await worker.run("other", _in(SIBLING), kind="cell", code="1", run="r1")
    assert other["result"]["error"]["kind"] == "conflict"
    assert "kernel_confinement" in other["result"]["error"]["message"]
    bare = await worker.run("bare", _in(), kind="cell", code="1", run="r1")
    assert bare["result"]["error"]["kind"] == "conflict"


async def test_an_executor_that_cannot_hold_a_run_runs_none_of_its_code(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DA-F10's refusal: an executor that says why it cannot, or says nothing (one from
    before), gets no delegated run's shell, job, cell or restart; the parent's still run."""
    await worker.run("connect", _in(), kind="shell", script="true")
    channel = worker.view.channel()
    report: dict[str, Any] = channel.hello["confinement"]
    assert report["protocol"] == 2
    assert report["landlock_abi"] >= sandbox_confine.REQUIRED_ABI
    # The probe found TCP held on this host's kernel, and the local backend built the
    # signal shim: both only inform.
    assert (report["tcp_held"], report["own_signals"]) == (True, True)
    for hello in (
        {**report, "unavailable": "Landlock ABI 5"},
        # Protocol 1 left the signal calls a supervisor carries out open, and its probe
        # never checked that a signal stays in.
        {"protocol": 1, "landlock_abi": report["landlock_abi"]},
        None,
    ):
        if hello is None:
            monkeypatch.delitem(channel.hello, "confinement")
        else:
            monkeypatch.setitem(channel.hello, "confinement", hello)
        marker = worker.workspace / AREA / "ran"
        for kind, extra in (
            ("shell", {"script": f"touch {marker}"}),
            ("job", {"script": f"touch {marker}", "purpose": "p", "expected_progress": "e"}),
            ("cell", {"code": f"open({str(marker)!r}, 'w')", "run": "r1"}),
        ):
            refused = await worker.run(
                f"refused-{kind}-{secrets.token_hex(4)}", _in(AREA), kind=kind, **extra
            )
            assert refused["state"] == "failed", refused
            assert refused["result"]["error"]["kind"] == "unsupported", refused
        assert not marker.exists()
        with pytest.raises(WorkerError) as restart:
            await worker.manager.restart_kernel(
                KernelRestartRequest(context=ExecutionContext.model_validate(_in(AREA)), run="r1")
            )
        assert restart.value.kind == "unsupported"
    parent = await worker.run("parent", _in(), kind="shell", script="echo ok")
    assert parent["result"]["stdout"]["text"] == "ok\n"


def test_the_wrapper_fails_closed(tmp_path: Path) -> None:
    """A ruleset that cannot be made in full runs nothing: a missing directory, or a
    symbolic link in place of one."""
    marker = tmp_path / "ran"
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    script = sandbox_confine.__file__
    for writable in (tmp_path / "missing", tmp_path / "link"):
        argv = [
            sys.executable,
            "-I",
            "-S",
            script,
            "--rw",
            str(writable),
            "--",
            "touch",
            str(marker),
        ]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        assert done.returncode == sandbox_confine.FAILED
        assert "cannot hold this run to its write area" in done.stderr
    assert not marker.exists()
    usage = subprocess.run([sys.executable, script, "--"], capture_output=True, check=False)
    assert usage.returncode == sandbox_confine.FAILED


REACH = """
import ctypes, fcntl, json, os, resource, socket
found = {}
def attempt(name, call):
    try:
        call()
        found[name] = "done"
    except OSError as error:
        found[name] = error.errno
libc = ctypes.CDLL(None, use_errno=True)
def checked(result):
    if result != 0:
        raise OSError(ctypes.get_errno(), "")
for pid in (KERNEL, EXECUTOR):
    attempt(f"prlimit {pid}", lambda: resource.prlimit(pid, resource.RLIMIT_NOFILE, (3, 3)))
    attempt(f"nice {pid}", lambda: os.setpriority(os.PRIO_PROCESS, pid, 19))
    attempt(f"ioprio {pid}", lambda: checked(libc.syscall(IOPRIO_SET, 1, pid, (3 << 13))))
    attempt(f"ptrace {pid}", lambda: checked(libc.ptrace(16, pid, 0, 0)))
attempt("nice user", lambda: os.setpriority(os.PRIO_USER, 0, 19))
os.sched_setaffinity(KERNEL, {0})  # succeeds doing nothing
lock = os.open(LOCK, os.O_RDONLY)
fcntl.flock(lock, fcntl.LOCK_EX)  # succeeds doing nothing
fcntl.lockf(lock, fcntl.LOCK_SH)
attempt("lease", lambda: fcntl.fcntl(lock, fcntl.F_SETLEASE, fcntl.F_RDLCK))
attempt("bind", lambda: socket.socket().bind(("127.0.0.1", 0)))
attempt("connect", lambda: socket.create_connection(("127.0.0.1", PORT), timeout=5))
urandom = os.open("/dev/urandom", os.O_RDONLY)
attempt("ioctl", lambda: fcntl.ioctl(urandom, 0x80045200, bytearray(4)))  # RNDGETENTCNT
open("found.json", "w").write(json.dumps(found) + "\\n")
"""


async def test_a_child_cannot_limit_lock_or_reach_the_parents_processes(
    daemon_worker: Harness,
) -> None:
    """DA-F10, after the review: a child's code cannot set another process's limits,
    priority, I/O priority or scheduling, ptrace it, hold a lock or lease against the
    platform, bind or connect over TCP other than to the proxy, or ioctl a device other
    than a terminal; the parent's kernel and the executor keep working."""
    import resource
    import socket as sockets

    worker = daemon_worker
    parent = await worker.run(
        "parent", _in(), kind="cell", code="import os; (os.getpid(), os.getppid())"
    )
    kernel, executor = json.loads(
        parent["result"]["outputs"][0]["data"]["text/plain"].replace("(", "[").replace(")", "]")
    )
    lock = worker.workspace.parent.parent / ".executor/provisioning/lock"
    assert lock.exists()
    with sockets.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        number = {"x86_64": 251, "aarch64": 30}[os.uname().machine]
        code = (
            f"KERNEL, EXECUTOR, LOCK, PORT, IOPRIO_SET = {kernel}, {executor}, "
            f"{str(lock)!r}, {port}, {number}\n" + REACH
        )
        job = worker.submit(
            "child", _in(AREA), kind="job", purpose="p", expected_progress="e",
            script=f"python3 - <<'PY'\n{code}\nimport time; time.sleep(30)\nPY",
        )  # fmt: skip
        started = await job
        await worker.wait_running(started["execution_id"])
        report = worker.workspace / AREA / "found.json"
        for _ in range(300):
            if report.exists() and report.read_text().endswith("\n"):
                break
            await asyncio.sleep(0.05)
        found: dict[str, Any] = json.loads(report.read_text())
    refused = {key: value for key, value in found.items() if value != "done"}
    assert refused == found, found  # every attempt refused
    assert set(found) >= {f"prlimit {kernel}", f"prlimit {executor}", "lease", "connect"}
    # The lock the child took is free: the parent's shell runs at once, a package command
    # could take its turn, and the parent's kernel and the executor keep their limits.
    after = await worker.run(
        "parent-shell", _in(), kind="shell", script="echo ran", timeout_seconds=5
    )
    assert after["result"]["stdout"]["text"] == "ran\n"
    burn = "import time\nt = time.process_time()\nwhile time.process_time() - t < 1.5: pass\n'ok'"
    survived = await worker.run("parent-burn", _in(), kind="cell", code=burn)
    assert survived["result"]["outputs"][0]["data"]["text/plain"] == "'ok'"
    assert resource.prlimit(executor, resource.RLIMIT_NOFILE)[0] > 3
    assert os.sched_getaffinity(kernel) != {0} or os.cpu_count() == 1
    await worker.cancel(started["execution_id"])


def test_the_wrapper_connects_to_the_proxy_port_only(tmp_path: Path) -> None:
    """``--connect`` opens one TCP port, the egress proxy's, and only for connecting."""
    import socket as sockets

    with sockets.socket() as allowed, sockets.socket() as other:
        for server in (allowed, other):
            server.bind(("127.0.0.1", 0))
            server.listen()
        ports = [server.getsockname()[1] for server in (allowed, other)]
        code = (
            "import socket\n"
            f"for port in {ports}:\n"
            "    try:\n"
            "        socket.create_connection(('127.0.0.1', port), timeout=5).close()\n"
            "        print('connected', port)\n"
            "    except OSError as error:\n"
            "        print('refused', error.errno)\n"
        )
        argv = [
            *sandbox_confine.command(
                sys.executable, sandbox_confine.__file__, [str(tmp_path)], [ports[0]]
            ),
            sys.executable,
            "-c",
            code,
        ]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert done.stdout.split("\n")[:2] == [f"connected {ports[0]}", "refused 13"], done


def test_the_proxy_ports_come_from_the_sandboxs_variables() -> None:
    env = {"HTTPS_PROXY": "http://10.200.0.1:3128", "http_proxy": "proxy.local", "NO_PROXY": "x"}
    assert executor.proxy_ports(env) == (80, 3128)
    assert executor.proxy_ports({"ALL_PROXY": "socks5://[::1]:not-a-port"}) == ()


def _run_filter(program: list[tuple[int, int, int, int]], data: bytes) -> int:
    """Evaluate the classic BPF subset the filter uses on one ``seccomp_data``."""
    accumulator, pc = 0, 0
    while True:
        code, jt, jf, k = program[pc]
        pc += 1
        if code == 0x20:
            accumulator = int.from_bytes(data[k : k + 4], "little")
        elif code == 0x54:
            accumulator &= k
        elif code in (0x15, 0x35):
            taken = accumulator == k if code == 0x15 else accumulator >= k
            pc += jt if taken else jf
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unknown instruction {code:#x}")


ARCHES = {"x86_64": (0xC000003E, 302, 72, 41, 53), "aarch64": (0xC00000B7, 261, 25, 198, 199)}
SIGNAL_CALLS = {
    # kill, rt_sigqueueinfo, ioctl, tkill, tgkill, rt_tgsigqueueinfo
    "x86_64": (62, 129, 16, 200, 234, 297),
    "aarch64": (129, 138, 29, 130, 131, 240),
}


@pytest.mark.parametrize("machine", sorted(ARCHES))
def test_the_seccomp_filter_answers_each_call_as_the_spec_says(machine: str) -> None:
    arch, prlimit, fcntl, socket_nr, socketpair = ARCHES[machine]
    program = sandbox_confine.seccomp_program(machine, 4321)

    def answer(nr: int, *args: int, audit: int = arch) -> int:
        words = [*args, *([0] * (6 - len(args)))]
        data = nr.to_bytes(4, "little") + audit.to_bytes(4, "little") + bytes(8)
        return _run_filter(program, data + b"".join(w.to_bytes(8, "little") for w in words))

    allow, kill, errno = 0x7FFF0000, 0x80000000, lambda e: 0x50000 | e
    assert answer(prlimit, 0) == answer(prlimit, 4321) == allow
    assert answer(prlimit, 999) == answer(prlimit, (1 << 32) | 999) == errno(1)
    assert answer(fcntl, 3, 6) == answer(fcntl, 3, 38) == errno(0)  # locks: no-ops
    assert answer(fcntl, 3, 1024) == errno(1)  # F_SETLEASE
    assert answer(fcntl, 3, 3) == allow  # F_GETFL
    for nr in (socket_nr, socketpair):
        assert answer(nr, 1, 1, 0) == answer(nr, 16, 3, 0) == allow  # Unix, netlink
        assert answer(nr, 2, 1, 0) == answer(nr, 10, 1 | 0o2000000, 6) == allow  # TCP
        assert answer(nr, 2, 2, 17) == answer(nr, 10, 2, 58) == allow  # UDP, ping
        assert answer(nr, 2, 1, 262) == answer(nr, 10, 1, 132) == errno(93)  # MPTCP, SCTP
        assert answer(nr, 2, 5, 0) == errno(93)  # SOCK_SEQPACKET would be SCTP
        assert answer(nr, 43, 1, 0) == answer(nr, 17, 3, 0) == errno(97)  # AF_SMC, packet
    for nr in (425, 426, 427):
        assert answer(nr) == errno(38)  # io_uring
    assert answer(0, audit=0x40000003) == kill  # i386 on x86_64, or anything foreign
    if machine == "x86_64":
        assert answer(0x40000000 | 41, 2, 1, 262) == kill  # x32
    assert answer(1) == allow
    with pytest.raises(sandbox_confine.ConfinementError):
        sandbox_confine.seccomp_program("riscv64", 1)


@pytest.mark.parametrize("machine", sorted(ARCHES))
def test_the_seccomp_filter_refuses_the_signal_calls_a_supervisor_carries_out(
    machine: str,
) -> None:
    """``kill`` and ``rt_sigqueueinfo`` are ``EPERM`` whatever they name (a process,
    the caller's own, its group, a group, every process), as are an owner or a signal
    for a descriptor; the thread-directed calls, which Landlock checks, pass."""
    arch = ARCHES[machine][0]
    fcntl = ARCHES[machine][2]
    kill, sigqueueinfo, ioctl, tkill, tgkill, tgsigqueueinfo = SIGNAL_CALLS[machine]
    program = sandbox_confine.seccomp_program(machine, 4321)

    def answer(nr: int, *args: int) -> int:
        words = [arg % (1 << 64) for arg in (*args, *([0] * (6 - len(args))))]
        data = nr.to_bytes(4, "little") + arch.to_bytes(4, "little") + bytes(8)
        return _run_filter(program, data + b"".join(w.to_bytes(8, "little") for w in words))

    allow, refused = 0x7FFF0000, 0x50001
    for pid in (999, 4321, 0, -999, -1):
        for sig in (0, 15, 18):
            assert answer(kill, pid, sig) == refused, (pid, sig)
            assert answer(sigqueueinfo, pid, sig, 0x1000) == refused, (pid, sig)
    for command in (8, 10, 15):  # F_SETOWN, F_SETSIG, F_SETOWN_EX
        assert answer(fcntl, 3, command, 999) == refused
        assert answer(fcntl, 3, command, 0) == refused
    assert answer(fcntl, 3, 9) == answer(fcntl, 3, 16) == allow  # F_GETOWN, F_GETOWN_EX
    assert answer(fcntl, 3, 4, 0o4000) == allow  # F_SETFL
    for request in (0x8901, 0x8902):  # FIOSETOWN, SIOCSPGRP
        assert answer(ioctl, 3, request, 0x1000) == refused
    assert answer(ioctl, 3, 0x5401) == answer(ioctl, 3, 0x8903) == allow  # TCGETS, FIOGETOWN
    assert answer(tkill, 999, 15) == answer(tgkill, 999, 999, 15) == allow
    assert answer(tgsigqueueinfo, 999, 999, 18, 0x1000) == allow


def test_the_probe_fails_when_a_signal_leaves_the_ruleset(tmp_path: Path) -> None:
    """``--probe`` holds on this host, reporting TCP held; the same checks in a process
    that is not held find each way to signal its parent, and refuse."""
    probe = [sys.executable, "-I", "-S", sandbox_confine.__file__, "--probe"]
    done = subprocess.run(probe, capture_output=True, text=True, check=False, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"tcp": True}
    unheld = (
        "import sys\n"
        f"sys.path.insert(0, {str(Path(sandbox_confine.__file__).parent)!r})\n"
        "import sandbox_confine\n"
        "sandbox_confine.restrict = lambda *args, **kwargs: None\n"
        "sys.exit(sandbox_confine.probe())\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", unheld],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert done.returncode == sandbox_confine.FAILED
    assert done.stderr.startswith("a signal reached a process outside the ruleset: kill(")
    assert not done.stdout


def test_a_shared_files_stamp_cannot_be_set_back(tmp_path: Path) -> None:
    """A held run can reset the modification time of a shared file it can read, but not
    its change time: the sync's stamp still moves, and the file is hashed again."""
    from sandbox_science.worker import sandbox_files

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.txt").write_text("one\n")
    before = sandbox_files.scan_shared(os.open(shared, os.O_RDONLY | os.O_DIRECTORY))[0]
    stat = (shared / "a.txt").stat()
    while (shared / "a.txt").stat().st_ctime_ns == stat.st_ctime_ns:  # a clock tick on
        (shared / "a.txt").write_text("two\n")  # same size, in place
        os.utime(shared / "a.txt", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = sandbox_files.scan_shared(os.open(shared, os.O_RDONLY | os.O_DIRECTORY))[0]
    assert after["shared/a.txt"][1:] == before["shared/a.txt"][1:]
    assert after["shared/a.txt"][0] != before["shared/a.txt"][0]


async def test_joblib_keeps_its_pickles_off_shared_memory(worker: Harness) -> None:
    """A held run can write /dev/shm; joblib's memory-mapped pickles, which its workers
    load, are not there for the parent, and are in the child's own scratch for it."""
    script = 'echo "$JOBLIB_TEMP_FOLDER"'
    parent = await worker.run("parent", _in(), kind="shell", script=script)
    child = await worker.run("child", _in(AREA), kind="shell", script=script)
    root = worker.workspace.parent.parent
    assert parent["result"]["stdout"]["text"] == f"{root}/.executor/joblib\n"
    assert child["result"]["stdout"]["text"].startswith(f"{root}/.executor/runs/")
    assert child["result"]["stdout"]["text"].endswith("/tmp\n")


OTHER_SOCKETS = """
import ctypes, json, os, socket
libc = ctypes.CDLL(None, use_errno=True)
found = {}
def attempt(name, family, kind, protocol, host):
    try:
        with socket.socket(family, kind, protocol) as client:
            client.settimeout(3)
            client.connect((host, PORT))
            client.sendall(name.encode())
        found[name] = "connected"
    except OSError as error:
        found[name] = error.errno
attempt("tcp", socket.AF_INET, socket.SOCK_STREAM, 0, "127.0.0.1")
attempt("mptcp", socket.AF_INET, socket.SOCK_STREAM, 262, "127.0.0.1")
attempt("mptcp6", socket.AF_INET6, socket.SOCK_STREAM, 262, "::ffff:127.0.0.1")
attempt("sctp", socket.AF_INET, socket.SOCK_STREAM, 132, "127.0.0.1")
smc = libc.socket(43, socket.SOCK_STREAM, 0)
found["smc"] = ctypes.get_errno() if smc < 0 else "created"
params = ctypes.create_string_buffer(120)
ring = libc.syscall(ctypes.c_long(425), ctypes.c_uint(4), params)
found["io_uring"] = ctypes.get_errno() if ring < 0 else "created"
print(json.dumps(found))
"""


async def test_no_other_protocol_or_io_uring_reaches_a_tcp_server(worker: Harness) -> None:
    """After the second review: MPTCP (IPv4 and IPv6), SCTP and AF_SMC sockets and
    io_uring, which Landlock's TCP rules do not see, are refused, so a child reaches no
    TCP server of another run, and the server receives nothing."""
    import socket as sockets
    import threading

    received: list[bytes] = []
    with sockets.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(10)
        port = server.getsockname()[1]

        def accept() -> None:
            with contextlib.suppress(OSError):
                while True:
                    connection, _ = server.accept()
                    with connection:
                        received.append(connection.recv(64))

        listener = threading.Thread(target=accept, daemon=True)
        listener.start()
        code = f"PORT = {port}\n" + OTHER_SOCKETS
        child = await worker.run(
            "sockets", _in(AREA), kind="shell", script=f"python3 - <<'PY'\n{code}\nPY"
        )
    assert child["state"] == "succeeded", child["result"]
    found = json.loads(child["result"]["stdout"]["text"])
    assert found == {"tcp": 13, "mptcp": 93, "mptcp6": 93, "sctp": 93, "smc": 97, "io_uring": 38}
    assert received == []


async def test_each_held_kernel_keeps_its_own_ipython_directory(worker: Harness) -> None:
    """A held run's locks do nothing, so its kernels do not share IPython's history."""
    cell = await worker.run(
        "ipython", _in(AREA), kind="cell", code="import os; os.environ['IPYTHONDIR']", run="r1"
    )
    assert cell["result"]["outputs"][0]["data"]["text/plain"].endswith("/ipython/python'")
