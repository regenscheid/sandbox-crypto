# SPDX-License-Identifier: MIT
"""The executor's processes: finding, signalling and reaping what a call or kernel started.

Every command and kernel runs with its own OWNER mark, and the executor adopts orphaned
processes where the kernel lets it (a subreaper), so what a call started is found when
it ends, even outside its process group or session. Signals go to one process at a
time: OpenShell's seccomp filter refuses signals to process groups (lab 4).

The executor (executor.py) loads this file beside itself in a sandbox, so it depends
only on the standard library.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import socket
import time
from collections.abc import Generator, Iterable
from dataclasses import dataclass

KILL_ROUNDS = 5
OWNER = "SANDBOX_SCIENCE_OWNER"
"""Names the call or kernel that started a process, in its environment. Processes
keep it unless they replace their whole environment."""
PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36
spawned: set[int] = set()
"""Processes asyncio or jupyter_client started, and will reap: never reaped here."""
starting = 0
"""Children this process is starting whose pids are not in ``spawned`` yet, and helpers
it runs and reaps itself: while any are, no zombie child is collected, since one may be
theirs. Changed and read only on the event loop's thread."""


Leader = tuple[int, int]
"""A session leader's process ID and start time."""


@dataclass(frozen=True)
class Process:
    """A process as /proc showed it. Its pid and start time together name it."""

    pid: int
    ppid: int
    session: int
    start: int
    zombie: bool


def process_info(pid: int) -> Process | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    # The command name is in parentheses and may contain anything: parse after the
    # last ')'. Then come state, ppid, pgrp and session; start time is the 20th.
    fields = data[data.rfind(b")") + 2 :].split()
    try:
        return Process(pid, int(fields[1]), int(fields[3]), int(fields[19]), fields[0] == b"Z")
    except (IndexError, ValueError):
        return None


def enable_subreaper() -> bool:
    """Adopt orphan research descendants when supported by the sandbox kernel."""
    return prctl(PR_SET_CHILD_SUBREAPER, 1)


def process_table() -> dict[int, Process]:
    table: dict[int, Process] = {}
    with contextlib.suppress(OSError):
        for entry in os.listdir("/proc"):
            if entry.isdigit() and (info := process_info(int(entry))) is not None:
                table[info.pid] = info
    return table


def cpu_seconds_below(root: int) -> float:
    """CPU time of every process below ``root``, and of the children ``root`` reaped.

    A process's ``cutime`` and ``cstime`` hold what its reaped children used, so the
    sum stays put when a process ends, wherever below ``root`` it is reaped.
    """
    ticks: dict[int, tuple[int, int, int]] = {}  # pid -> (ppid, own, reaped children)
    with contextlib.suppress(OSError):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as handle:
                    data = handle.read()
                fields = data[data.rfind(b")") + 2 :].split()
                ppid, own = int(fields[1]), int(fields[11]) + int(fields[12])
                ticks[int(entry)] = (ppid, own, int(fields[13]) + int(fields[14]))
            except (OSError, IndexError, ValueError):
                continue
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in ticks.items():
        children.setdefault(ppid, []).append(pid)
    total = ticks[root][2] if root in ticks else 0
    stack = list(children.get(root, []))
    while stack:
        pid = stack.pop()
        _, own, reaped = ticks[pid]
        total += own + reaped
        stack.extend(children.get(pid, []))
    return total / os.sysconf("SC_CLK_TCK")


def process_owner(pid: int) -> str | None:
    """The OWNER a process started with: ``""`` without one, None when unreadable.

    Unreadable are zombies, other users' processes, and children of this executor
    between fork and exec, since the executor is not dumpable.
    """
    return started_with(pid, OWNER)


def started_with(pid: int, name: str) -> str | None:
    """A variable of the environment a process started with: ``""`` without it, None
    when unreadable."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            environ = handle.read()
    except OSError:
        return None
    if not environ:
        return None
    prefix = name.encode() + b"="
    for item in environ.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix) :].decode("utf-8", "replace")
    return ""


def owned_processes(owner: str, leader: Leader | None) -> list[Process]:
    """The live processes that belong to ``owner``, a call or a kernel.

    A process belongs to it when it is in the session ``leader`` began, carries
    the owner's mark, or descends from such a process without a mark of its
    own. Below this executor, a process with no mark at all has replaced its
    environment to get away, and belongs to whichever owner ends first. A
    process whose environment cannot be read is left alone.
    """
    table = process_table()
    me = os.getpid()
    session = leader[0] if leader is not None else None
    if (
        leader is not None
        and (head := table.get(leader[0])) is not None
        and head.start != leader[1]
    ):
        session = None  # the leader's pid was reused: the session number means nothing now
    children: dict[int, list[int]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info.pid)
    found: list[Process] = []
    seen = {me}
    stack: list[tuple[int, str]] = [(pid, "") for pid in children.get(me, ())]
    while stack:
        pid, inherited = stack.pop()
        info = table.get(pid)
        if info is None or pid in seen:
            continue
        seen.add(pid)
        mark = process_owner(pid)
        if mark is None and not info.zombie:
            continue
        whose = owner if session is not None and info.session == session else mark or inherited
        if whose in (owner, "") and not info.zombie:
            found.append(info)
        stack.extend((child, whose) for child in children.get(pid, ()))
    # Without a subreaper, init adopts orphans: find them by session and mark.
    for info in table.values():
        if info.pid in seen or info.zombie:
            continue
        if (session is not None and info.session == session) or process_owner(info.pid) == owner:
            found.append(info)
    return found


def signal_processes(processes: Iterable[Process], signum: int) -> list[Process]:
    """Signal each process that is still the one scanned; return those signalled."""
    sent: list[Process] = []
    for info in processes:
        current = process_info(info.pid)
        if current is None or current.start != info.start:
            continue  # gone, or its pid reused since the scan
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(info.pid, signum)
            sent.append(info)
    return sent


def end_owned(owner: str, leader: Leader | None) -> int:
    """SIGKILL everything ``owner`` left running; return how many processes that was.

    A few rounds catch processes forked meanwhile. Processes this executor
    adopted are reaped, so they do not stay as zombies.
    """
    killed: dict[int, Process] = {}
    for _ in range(KILL_ROUNDS):
        targets = owned_processes(owner, leader)
        if not targets:
            break
        for info in signal_processes(targets, signal.SIGKILL):
            killed[info.pid] = info
        time.sleep(0.01)
    _reap(killed)
    return len(killed)


def end_descendants() -> None:
    """SIGKILL every process below this executor, as it shuts down."""
    for _ in range(KILL_ROUNDS):
        table = process_table()
        children: dict[int, list[int]] = {}
        for info in table.values():
            children.setdefault(info.ppid, []).append(info.pid)
        targets: list[Process] = []
        stack = list(children.get(os.getpid(), ()))
        while stack:
            pid = stack.pop()
            if not table[pid].zombie:
                targets.append(table[pid])
            stack.extend(children.get(pid, ()))
        if not signal_processes(targets, signal.SIGKILL):
            break
        time.sleep(0.01)


def _reap(pids: Iterable[int]) -> None:
    """Collect the status of killed processes this executor adopted, so none stays a zombie."""
    waiting = [pid for pid in pids if pid not in spawned]
    for _ in range(20):
        if not waiting:
            return
        still: list[int] = []
        for pid in waiting:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:  # not adopted (yet): another parent reaps it
                continue
            if not done:
                still.append(pid)
        waiting = still
        time.sleep(0.01)


@contextlib.contextmanager
def starting_children() -> Generator[None]:
    """Hold off collecting zombie children while this process starts children of its own
    or runs a helper it reaps itself. Only on the event loop's thread."""
    global starting
    starting += 1
    try:
        yield
    finally:
        starting -= 1


def zombie_children() -> list[int]:
    """This process's zombie children that asyncio and jupyter_client do not wait for.
    Blocking: it reads /proc."""
    me = os.getpid()
    return [
        info.pid
        for info in process_table().values()
        if info.ppid == me and info.zombie and info.pid not in spawned
    ]


def collect_adopted(pids: Iterable[int]) -> int:
    """Collect those of ``pids`` that are still zombie children of this process, which
    adopted them (a subreaper) after their parent ended: a process that forks and exits
    once its parent has gone, as a double fork does, would otherwise stay a zombie until
    the executor exits. Return how many it collected.

    Only on the event loop's thread, with no await since ``starting`` is checked: none
    of these can then be a child this process is starting, whose status is not ours.
    """
    if starting:
        return 0
    me = os.getpid()
    collected = 0
    for pid in pids:
        info = process_info(pid)
        if info is None or not info.zombie or info.ppid != me or pid in spawned:
            continue
        with contextlib.suppress(ChildProcessError):
            done, _ = os.waitpid(pid, os.WNOHANG)
            collected += done == pid
    return collected


def _sockets_work() -> bool:
    """Whether this process may use an abstract Unix socket as a kernel's client does.

    ZeroMQ connects the executor to the kernel's channels, so the probe binds, listens,
    connects and accepts. Any refusal counts as no: the executor then stays dumpable
    rather than lose its kernels.
    """
    name = "\0ss-probe-" + secrets.token_hex(16)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(name)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(name)
                server.settimeout(5)
                accepted, _ = server.accept()
                accepted.close()
    except OSError:
        return False
    return True


def protect_process() -> bool:
    """Make this process not dumpable, unless that takes its sockets away; say whether it is.

    Research code runs as this same user. Not dumpable, this process keeps its /proc
    files (fd, environ, mem) and ptrace closed to it, so research code cannot write into
    the protocol or read other sessions' requests. OpenShell's supervisor refuses socket
    calls from a process it cannot inspect, which stops every kernel starting (lab 8);
    there the executor stays dumpable.
    """
    sockets = _sockets_work()
    if not prctl(PR_SET_DUMPABLE, 0):
        return False
    if sockets and not _sockets_work():
        prctl(PR_SET_DUMPABLE, 1)
        return False
    return True


def prctl(option: int, value: int) -> bool:
    """One prctl(2) call through libc; False where it is unavailable or refused."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        arguments = (ctypes.c_ulong(value), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
        return bool(libc.prctl(option, *arguments) == 0)
    except (ImportError, OSError, AttributeError):
        return False


def job_usage(owner: str, leader: Leader | None) -> tuple[float, int]:
    """Observed CPU and resident memory of processes belonging to one job."""
    ticks, pages = 0, 0
    for process in owned_processes(owner, leader):
        try:
            with open(f"/proc/{process.pid}/stat", "rb") as handle:
                data = handle.read(65536)
            fields = data[data.rfind(b")") + 2 :].split()
            if int(fields[19]) != process.start:
                continue
            ticks += sum(int(fields[i]) for i in (11, 12, 13, 14))
            pages += max(0, int(fields[21]))
        except (OSError, ValueError, IndexError):
            continue
    return ticks / os.sysconf("SC_CLK_TCK"), pages * os.sysconf("SC_PAGE_SIZE")
