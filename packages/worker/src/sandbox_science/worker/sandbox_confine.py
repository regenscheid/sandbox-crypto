# SPDX-License-Identifier: MIT
"""Hold a delegated run's processes to its write area (delegated agents spec, DA-20).

The executor starts a delegated run's shell commands, jobs and kernels through this file,
as ``python -I -S sandbox_confine.py --rw DIR … -- COMMAND …``. It adds a Landlock
ruleset to itself, nested inside the sandbox's own (OpenShell's), then replaces itself
with the command, so the command and everything it starts can write only below the
directories named, and never lift the rule. Reads are left to the sandbox's ruleset.
The same ruleset scopes abstract Unix sockets and signals (Landlock ABI 6): the process
cannot connect to a socket made outside it, such as another run's kernel, nor signal a
process outside it. It refuses TCP except connections to the ports named (the sandbox's
egress proxy), and ioctls on devices but the terminals. A seccomp filter beside it
refuses what Landlock does not cover: changing another process's limits, priority,
scheduling or I/O priority, and leases; and it turns file locks into no-ops, which an
unwritable file's read-only descriptor could otherwise hold against the platform.

It uses only the standard library, since it runs inside a sandbox beside the executor,
and fails closed: if the ruleset cannot be made in full it exits with ``FAILED`` and never
runs the command. Linux only; elsewhere ``abi`` reports why it is unavailable.
"""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from collections.abc import Sequence
from typing import Final

PROTOCOL: Final = 1
"""What the executor's ``hello`` reports as ``confinement.protocol``."""
REQUIRED_ABI: Final = 6
"""Landlock's ABI 6 (Linux 6.12) scopes abstract Unix sockets and signals. Without the
scope a confined process could drive another run's kernel, which is not confined."""
FAILED: Final = 126
"""The exit status when the ruleset cannot be applied: the command was not run."""

_CREATE_RULESET: Final = 444  # the same number on x86_64 and aarch64
_ADD_RULE: Final = 445
_RESTRICT_SELF: Final = 446
_CREATE_RULESET_VERSION: Final = 1
_RULE_PATH_BENEATH: Final = 1
_PR_SET_NO_NEW_PRIVS: Final = 38

WRITE_FILE: Final = 1 << 1
REMOVE_DIR: Final = 1 << 4
REMOVE_FILE: Final = 1 << 5
MAKE_CHAR: Final = 1 << 6
MAKE_DIR: Final = 1 << 7
MAKE_REG: Final = 1 << 8
MAKE_SOCK: Final = 1 << 9
MAKE_FIFO: Final = 1 << 10
MAKE_BLOCK: Final = 1 << 11
MAKE_SYM: Final = 1 << 12
REFER: Final = 1 << 13
TRUNCATE: Final = 1 << 14
IOCTL_DEV: Final = 1 << 15
HANDLED: Final = (
    WRITE_FILE
    | REMOVE_DIR
    | REMOVE_FILE
    | MAKE_CHAR
    | MAKE_DIR
    | MAKE_REG
    | MAKE_SOCK
    | MAKE_FIFO
    | MAKE_BLOCK
    | MAKE_SYM
    | REFER
    | TRUNCATE
    | IOCTL_DEV
)
"""Every right that changes the file system, and ioctls on devices (ABI 5). Execute,
read-file and read-dir are not handled, so this ruleset leaves reading to the sandbox's."""
FILE_RIGHTS: Final = WRITE_FILE | TRUNCATE | IOCTL_DEV
"""The handled rights a rule on a file (not a directory) may name."""
SCOPE_ABSTRACT_UNIX_SOCKET: Final = 1 << 0
SCOPE_SIGNAL: Final = 1 << 1
SCOPED: Final = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL
BIND_TCP: Final = 1 << 0
CONNECT_TCP: Final = 1 << 1
_RULE_NET_PORT: Final = 2

TERMINALS: Final = ("/dev/tty", "/dev/ptmx", "/dev/pts")
DEVICES: Final = (
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    *TERMINALS,
    "/dev/shm",  # noqa: S108 - the shared-memory device, not a temporary file
)
"""Writable wherever they exist: the sinks, pseudo-terminals (Sage's pexpect interfaces,
the only devices that take ioctls) and shared memory (Python's multiprocessing), which
the sandbox's policy grants too."""

# The seccomp filter (struct seccomp_data: nr at 0, arch at 4, argument i's low word at
# 16 + 8 i on these little-endian machines; the kernel reads these arguments as ints).
_PR_SET_SECCOMP: Final = 22
_SECCOMP_MODE_FILTER: Final = 2
_ALLOW: Final = 0x7FFF0000
_KILL: Final = 0x80000000
_ERRNO: Final = 0x00050000
_EPERM: Final = 1
_LD: Final = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ: Final = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE: Final = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_RET: Final = 0x06  # BPF_RET | BPF_K
_ARCHES: Final = {
    # machine: (AUDIT_ARCH, {name: number}); x86_64's x32 numbers are refused below.
    "x86_64": (0xC000003E, {"prlimit64": 302, "setpriority": 141, "sched_setaffinity": 203,
        "sched_setscheduler": 144, "sched_setparam": 142, "sched_setattr": 314,
        "ioprio_set": 251, "flock": 73, "fcntl": 72}),
    "aarch64": (0xC00000B7, {"prlimit64": 261, "setpriority": 140, "sched_setaffinity": 122,
        "sched_setscheduler": 119, "sched_setparam": 118, "sched_setattr": 274,
        "ioprio_set": 30, "flock": 32, "fcntl": 25}),
}  # fmt: skip
_TARGET: Final = {
    # A call that acts on a process: the argument naming it, the argument that must say
    # it names a process (PRIO_PROCESS, IOPRIO_WHO_PROCESS), and its answer for another
    # target. Scheduling calls on another thread succeed doing nothing, since libzmq
    # aborts when a thread cannot set its own (which it names by thread ID, not 0).
    "prlimit64": (0, None, _EPERM),
    "sched_setaffinity": (0, None, 0),
    "sched_setscheduler": (0, None, 0),
    "sched_setparam": (0, None, 0),
    "sched_setattr": (0, None, 0),
    "setpriority": (1, (0, 0), _EPERM),
    "ioprio_set": (1, (0, 1), _EPERM),
}
_LOCKS: Final = (6, 7, 37, 38)  # F_SETLK, F_SETLKW, F_OFD_SETLK, F_OFD_SETLKW
_SETLEASE: Final = 1024


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    # The kernel's struct is packed (12 bytes); these fields have the same offsets, and
    # the kernel reads only those 12 bytes.
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class ConfinementError(Exception):
    """The ruleset could not be made or applied."""


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    result = int(libc.syscall(ctypes.c_long(number), *args))
    if result < 0:
        error = ctypes.get_errno()
        raise ConfinementError(f"syscall {number}: {os.strerror(error)}")
    return result


class _NetPortAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def seccomp_program(machine: str, self_pid: int) -> list[tuple[int, int, int, int]]:
    """The filter: a call in ``_TARGET`` passes only for the calling process (0) or
    ``self_pid``, else ``EPERM`` or, for scheduling, success doing nothing; ``flock``
    and fcntl's locks succeed doing nothing; a lease is ``EPERM``; another
    architecture's call ends the process. Everything else passes, to OpenShell's filter
    below."""
    if machine not in _ARCHES:
        raise ConfinementError(f"no seccomp filter for {machine}")
    arch, numbers = _ARCHES[machine]
    done = [(_RET, 0, 0, _ALLOW)]
    refuse = [(_RET, 0, 0, _ERRNO | _EPERM)]
    rules: list[list[tuple[int, int, int, int]]] = []
    for name, (index, which, answer) in _TARGET.items():
        body = [(_LD, 0, 0, 16 + 8 * index), (_JEQ, 2, 0, 0), (_JEQ, 1, 0, self_pid)]
        body += [(_RET, 0, 0, _ERRNO | answer), *done]
        if which is not None:
            body = [(_LD, 0, 0, 16 + 8 * which[0]), (_JEQ, 1, 0, which[1]), *refuse, *body]
        rules.append([(_JEQ, 0, len(body), numbers[name]), *body])
    rules.append([(_JEQ, 0, 1, numbers["flock"]), (_RET, 0, 0, _ERRNO)])
    fcntl = [(_LD, 0, 0, 24)]
    for command in _LOCKS:
        fcntl.append((_JEQ, len(_LOCKS) - len(fcntl) + 3, 0, command))
    fcntl += [(_JEQ, 1, 0, _SETLEASE), *done, *refuse, (_RET, 0, 0, _ERRNO)]
    rules.append([(_JEQ, 0, len(fcntl), numbers["fcntl"]), *fcntl])
    program = [(_LD, 0, 0, 4), (_JEQ, 1, 0, arch), (_RET, 0, 0, _KILL), (_LD, 0, 0, 0)]
    if machine == "x86_64":
        program += [(_JGE, 0, 1, 0x40000000), (_RET, 0, 0, _KILL)]
    for rule in rules:
        # Each rule ends in a return; past its body, the number is reloaded for the next.
        program += [*rule, (_LD, 0, 0, 0)]
    return [*program, *done]


def _seccomp(libc: ctypes.CDLL) -> None:
    program = seccomp_program(os.uname().machine, os.getpid())
    filters = (_SockFilter * len(program))(*(_SockFilter(*op) for op in program))
    fprog = _SockFprog(len(program), ctypes.cast(filters, ctypes.POINTER(_SockFilter)))
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise ConfinementError(f"seccomp filter: {os.strerror(ctypes.get_errno())}")


def _ruleset(libc: ctypes.CDLL) -> int:
    attr = _RulesetAttr(HANDLED, BIND_TCP | CONNECT_TCP, SCOPED)
    return _syscall(
        libc, _CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)),
        ctypes.c_uint32(0),
    )  # fmt: skip


def abi() -> tuple[int, str | None]:
    """The Landlock ABI this process can use, and why confinement is unavailable, or
    ``None`` when a ruleset like the one ``restrict`` applies can be made here. Making
    one (and closing it) restricts nothing."""
    if not sys.platform.startswith("linux"):
        return 0, f"Landlock is Linux's; this is {sys.platform}"
    try:
        libc = _libc()
        version = _syscall(
            libc, _CREATE_RULESET, None, ctypes.c_size_t(0),
            ctypes.c_uint32(_CREATE_RULESET_VERSION),
        )  # fmt: skip
    except (OSError, ConfinementError) as exc:
        return 0, f"Landlock is not available: {exc}"
    if version < REQUIRED_ABI:
        return (
            version,
            f"Landlock ABI {version}; holding a run to its write area needs {REQUIRED_ABI}",
        )
    try:
        os.close(_ruleset(libc))
    except (OSError, ConfinementError) as exc:
        return version, f"cannot make a Landlock ruleset: {exc}"
    return version, None


def _open_directory(path: str) -> int:
    """An O_PATH descriptor of the directory ``path``, absolute, following no symbolic
    link on the way: the executor made it, and a link put there since leads nowhere."""
    if not os.path.isabs(path):
        raise ConfinementError(f"not an absolute path: {path!r}")
    descriptor = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in [part for part in path.split("/") if part]:
            if name in (".", ".."):
                raise ConfinementError(f"not a plain path: {path!r}")
            following = os.open(
                name,
                os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = following
    except OSError as exc:
        os.close(descriptor)
        raise ConfinementError(f"cannot open {path!r}: {exc.strerror}") from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _allow(libc: ctypes.CDLL, ruleset: int, descriptor: int, ioctl: bool = False) -> None:
    directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
    rights = (HANDLED if directory else FILE_RIGHTS) & (~0 if ioctl else ~IOCTL_DEV)
    rule = _PathBeneathAttr(rights, descriptor)
    _syscall(
        libc, _ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH),
        ctypes.byref(rule), ctypes.c_uint32(0),
    )  # fmt: skip


def restrict(
    writable: Sequence[str], connect: Sequence[int] = (), devices: Sequence[str] = DEVICES
) -> None:
    """Restrict this process and all it starts: write only below ``writable`` (absolute
    directories, opened without following links; each must exist) and to ``devices``
    (followed; those missing are skipped), connect over TCP only to the ports
    ``connect``, with abstract Unix sockets and signals scoped and the seccomp filter on.
    Raises ``ConfinementError``, and runs nothing, if any part cannot be applied."""
    version, reason = abi()
    if reason is not None:
        raise ConfinementError(reason)
    del version
    libc = _libc()
    ruleset = _ruleset(libc)
    try:
        for path in writable:
            descriptor = _open_directory(path)
            try:
                _allow(libc, ruleset, descriptor)
            finally:
                os.close(descriptor)
        for path in devices:
            try:
                descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                _allow(libc, ruleset, descriptor, ioctl=path in TERMINALS)
            finally:
                os.close(descriptor)
        for port in connect:
            rule = _NetPortAttr(CONNECT_TCP, port)
            _syscall(
                libc, _ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_RULE_NET_PORT),
                ctypes.byref(rule), ctypes.c_uint32(0),
            )  # fmt: skip
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise ConfinementError(f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(ctypes.get_errno())}")
        _syscall(libc, _RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0))
        _seccomp(libc)
    finally:
        os.close(ruleset)


def command(
    python: str, script: str, writable: Sequence[str], connect: Sequence[int] = ()
) -> list[str]:
    """The argv prefix that runs a command under ``restrict(writable, connect)``."""
    prefix = [python, "-I", "-S", script]
    for path in writable:
        prefix += ["--rw", path]
    for port in connect:
        prefix += ["--connect", str(port)]
    return [*prefix, "--"]


def main(argv: Sequence[str]) -> int:
    """``--probe`` applies a ruleset and the filter with nothing writable, and exits 0
    if all of it took, for the executor's ``hello``."""
    if list(argv) == ["--probe"]:
        try:
            restrict([])
        except (OSError, ConfinementError) as exc:
            print(exc, file=sys.stderr)
            return FAILED
        return 0
    writable: list[str] = []
    connect: list[int] = []
    rest = list(argv)
    while rest and rest[0] in ("--rw", "--connect") and len(rest) > 1:
        if rest[0] == "--rw":
            writable.append(rest[1])
        elif rest[1].isdigit() and 0 < int(rest[1]) < 65536:
            connect.append(int(rest[1]))
        else:
            rest = []
            break
        rest = rest[2:]
    if not rest or rest[0] != "--" or len(rest) < 2:
        print(
            "usage: sandbox_confine.py [--rw DIR]... [--connect PORT]... -- COMMAND [ARG]...",
            file=sys.stderr,
        )
        return FAILED
    try:
        restrict(writable, connect)
    except (OSError, ConfinementError) as exc:
        print(f"sandbox-science: cannot hold this run to its write area: {exc}", file=sys.stderr)
        return FAILED
    try:
        os.execvp(rest[1], rest[1:])  # noqa: S606 - replaces this process, by design
    except OSError as exc:
        print(f"sandbox-science: cannot run {rest[1]!r}: {exc.strerror}", file=sys.stderr)
        return 127 if isinstance(exc, FileNotFoundError) else FAILED
    return FAILED  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
