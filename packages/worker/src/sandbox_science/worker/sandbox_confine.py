# SPDX-License-Identifier: MIT
"""Hold a delegated run's processes to its write area (delegated agents spec, DA-20).

The executor starts a delegated run's shell commands, jobs and kernels through this file,
as ``python -I -S sandbox_confine.py --rw DIR … -- COMMAND …``. It adds a Landlock
ruleset to itself, nested inside the sandbox's own (OpenShell's), then replaces itself
with the command, so the command and everything it starts can write only below the
directories named, and never lift the rule. Reads are left to the sandbox's ruleset.
The same ruleset scopes abstract Unix sockets and signals (Landlock ABI 6): the process
cannot connect to a socket made outside it, such as another run's kernel, nor signal a
process outside it. It refuses TCP except connections to the ports named (an egress
proxy's), and ioctls on devices but the terminals. A seccomp filter beside it refuses
what Landlock does not cover: changing another process's limits, priority, scheduling
or I/O priority, and leases; it turns file locks into no-ops, which an unwritable file's
read-only descriptor could otherwise hold against the platform; and it refuses the
signal calls a sandbox's supervisor may carry out for the process outside its Landlock
domain (``kill``, ``rt_sigqueueinfo``, and making another process a descriptor's owner),
so every signal it sends goes through a call Landlock checks. The signal shim
(``shim/signal_self.c``) gives its programs ``kill`` back through those calls.

``--probe`` applies all of it in a process of its own and then tries each way to signal
the process that started it, failing if one goes through, and reports whether TCP is
held there, for the executor's ``hello``.

It uses only the standard library, since it runs inside a sandbox beside the executor,
and fails closed: if the ruleset cannot be made in full it exits with ``FAILED`` and never
runs the command. Linux only; elsewhere ``abi`` reports why it is unavailable.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import stat
import sys
from collections.abc import Sequence
from typing import Final

PROTOCOL: Final = 2
"""What the executor's ``hello`` reports as ``confinement.protocol``: 2 refuses the
signal calls a supervisor may carry out, and its probe checks that no signal leaves."""
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
        "ioprio_set": 251, "flock": 73, "fcntl": 72, "socket": 41, "socketpair": 53,
        "kill": 62, "rt_sigqueueinfo": 129, "ioctl": 16, "tkill": 200, "tgkill": 234,
        "rt_tgsigqueueinfo": 297}),
    "aarch64": (0xC00000B7, {"prlimit64": 261, "setpriority": 140, "sched_setaffinity": 122,
        "sched_setscheduler": 119, "sched_setparam": 118, "sched_setattr": 274,
        "ioprio_set": 30, "flock": 32, "fcntl": 25, "socket": 198, "socketpair": 199,
        "kill": 129, "rt_sigqueueinfo": 138, "ioctl": 29, "tkill": 130, "tgkill": 131,
        "rt_tgsigqueueinfo": 240}),
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
_IO_URING: Final = (425, 426, 427)  # setup, enter, register: the same on both
_FAMILIES: Final = (1, 16)  # AF_UNIX, AF_NETLINK: any type and protocol
_INET: Final = (2, 10)  # AF_INET, AF_INET6: stream or datagram, and these protocols
_PROTOCOLS: Final = (0, 6, 17, 1, 58)  # default, TCP, UDP, ICMP, ICMPv6
_AND: Final = 0x54  # BPF_ALU | BPF_AND | BPF_K
_ENOSYS: Final = 38
_EAFNOSUPPORT: Final = 97
_EPROTONOSUPPORT: Final = 93
_LOCKS: Final = (6, 7, 37, 38)  # F_SETLK, F_SETLKW, F_OFD_SETLK, F_OFD_SETLKW
_SETLEASE: Final = 1024
_SETOWN: Final = (8, 10, 15)  # F_SETOWN, F_SETSIG, F_SETOWN_EX
_IOCTL_SETOWN: Final = (0x8901, 0x8902)  # FIOSETOWN, SIOCSPGRP
_SUPERVISED_SIGNALS: Final = ("kill", "rt_sigqueueinfo")
"""Signal calls OpenShell 0.1.5's supervisor carries out itself, for a target it allows,
outside the caller's Landlock domain (a seccomp user notification answered by its own
``pidfd_send_signal``). ``EPERM`` from this filter takes precedence over the
notification, so the supervisor never sees them; the thread-directed calls it lets the
kernel continue (``tkill``, ``tgkill``, ``rt_tgsigqueueinfo``) stay, checked by Landlock."""
_PIDFD_SEND_SIGNAL: Final = 424  # the same on both
_PIDFD_OPEN: Final = 434
_SI_QUEUE: Final = -1


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


def seccomp_program(machine: str) -> list[tuple[int, int, int, int]]:
    """The filter: a call in ``_TARGET`` passes only for the calling process, named 0,
    else ``EPERM`` or, for scheduling, success doing nothing. A pid, even the caller's
    own, is never trusted: the filter cannot tell whose it is now, and the run's first
    process's pid, once it has exited, may be an outside process's; ``flock``
    and fcntl's locks succeed doing nothing; a lease and a descriptor's owner or signal
    (``F_SETOWN``, ``F_SETSIG``, ``F_SETOWN_EX``, ``FIOSETOWN``, ``SIOCSPGRP``) are
    ``EPERM``; so are ``kill`` and ``rt_sigqueueinfo``, whatever they name; io_uring is
    ``ENOSYS``; a socket passes ``_socket_rule``; another architecture's call ends the
    process. Everything else passes, to OpenShell's filter below."""
    if machine not in _ARCHES:
        raise ConfinementError(f"no seccomp filter for {machine}")
    arch, numbers = _ARCHES[machine]
    done = [(_RET, 0, 0, _ALLOW)]
    refuse = [(_RET, 0, 0, _ERRNO | _EPERM)]
    rules: list[list[tuple[int, int, int, int]]] = []
    for name, (index, which, answer) in _TARGET.items():
        body = [(_LD, 0, 0, 16 + 8 * index), (_JEQ, 1, 0, 0)]
        body += [(_RET, 0, 0, _ERRNO | answer), *done]
        if which is not None:
            body = [(_LD, 0, 0, 16 + 8 * which[0]), (_JEQ, 1, 0, which[1]), *refuse, *body]
        rules.append([(_JEQ, 0, len(body), numbers[name]), *body])
    rules.append([(_JEQ, 0, 1, numbers["flock"]), (_RET, 0, 0, _ERRNO)])
    fcntl = _fcntl_rule()
    rules.append([(_JEQ, 0, len(fcntl), numbers["fcntl"]), *fcntl])
    for name in _SUPERVISED_SIGNALS:
        rules.append([(_JEQ, 0, 1, numbers[name]), *refuse])
    ioctl = [(_LD, 0, 0, 24)]
    for request in _IOCTL_SETOWN:
        ioctl.append((_JEQ, len(_IOCTL_SETOWN) - len(ioctl) + 1, 0, request))
    ioctl += [*done, *refuse]
    rules.append([(_JEQ, 0, len(ioctl), numbers["ioctl"]), *ioctl])
    for number in _IO_URING:
        # Its socket and connect operations would pass the socket rule below unseen.
        rules.append([(_JEQ, 0, 1, number), (_RET, 0, 0, _ERRNO | _ENOSYS)])
    sockets = _socket_rule()
    for name in ("socket", "socketpair"):
        rules.append([(_JEQ, 0, len(sockets), numbers[name]), *sockets])
    program = [(_LD, 0, 0, 4), (_JEQ, 1, 0, arch), (_RET, 0, 0, _KILL), (_LD, 0, 0, 0)]
    if machine == "x86_64":
        program += [(_JGE, 0, 1, 0x40000000), (_RET, 0, 0, _KILL)]
    for rule in rules:
        # Each rule ends in a return; past its body, the number is reloaded for the next.
        program += [*rule, (_LD, 0, 0, 0)]
    return [*program, *done]


def _fcntl_rule() -> list[tuple[int, int, int, int]]:
    """fcntl's command: a lock succeeds doing nothing; a lease, an owner or a signal for
    the descriptor is ``EPERM`` (the kernel would check an owner's signal against the
    domain of whoever set it, and a supervisor that sets it on the caller's behalf is
    outside); anything else passes."""
    refused = (_SETLEASE, *_SETOWN)
    rule = [(_LD, 0, 0, 24)]
    allow_at = 1 + len(_LOCKS) + len(refused)
    for command in _LOCKS:  # to the no-op, two past the refusal
        rule.append((_JEQ, allow_at + 2 - len(rule) - 1, 0, command))
    for command in refused:  # to the refusal, one past the allow
        rule.append((_JEQ, allow_at + 1 - len(rule) - 1, 0, command))
    return [*rule, (_RET, 0, 0, _ALLOW), (_RET, 0, 0, _ERRNO | _EPERM), (_RET, 0, 0, _ERRNO)]


def _socket_rule() -> list[tuple[int, int, int, int]]:
    """A socket's family, type and protocol: Unix and netlink sockets pass; an Internet
    socket passes only as a stream or datagram of TCP, UDP or ICMP, which Landlock's TCP
    rules and the sandbox's network policy see. MPTCP, SCTP and the other families
    (AF_SMC carries TCP too) would pass Landlock's TCP rules unseen."""
    rule = [(_LD, 0, 0, 16)]
    # Indexes: 0 load; families; inet families; refuse; type load, and, 2 types,
    # refuse; protocol load; protocols; refuse; allow.
    allow_at = 9 + len(_FAMILIES) + len(_INET) + len(_PROTOCOLS)
    types_at = 1 + len(_FAMILIES) + len(_INET) + 1
    for family in _FAMILIES:
        rule.append((_JEQ, allow_at - len(rule) - 1, 0, family))
    for family in _INET:
        rule.append((_JEQ, types_at - len(rule) - 1, 0, family))
    rule += [(_RET, 0, 0, _ERRNO | _EAFNOSUPPORT), (_LD, 0, 0, 24), (_AND, 0, 0, 0xF)]
    protocols_at = len(rule) + 3
    for kind in (1, 2):  # SOCK_STREAM, SOCK_DGRAM
        rule.append((_JEQ, protocols_at - len(rule) - 1, 0, kind))
    rule += [(_RET, 0, 0, _ERRNO | _EPROTONOSUPPORT), (_LD, 0, 0, 32)]
    for protocol in _PROTOCOLS:
        rule.append((_JEQ, allow_at - len(rule) - 1, 0, protocol))
    return [*rule, (_RET, 0, 0, _ERRNO | _EPROTONOSUPPORT), (_RET, 0, 0, _ALLOW)]


def _seccomp(libc: ctypes.CDLL) -> None:
    program = seccomp_program(os.uname().machine)
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


class _SigInfo(ctypes.Structure):
    # siginfo_t on these 64-bit machines: signo, errno, code, padding, then the sender's
    # pid and uid; 128 bytes in all.
    _fields_ = [
        ("signo", ctypes.c_int),
        ("errno", ctypes.c_int),
        ("code", ctypes.c_int),
        ("pad", ctypes.c_int),
        ("pid", ctypes.c_int),
        ("uid", ctypes.c_uint),
        ("rest", ctypes.c_byte * 104),
    ]


def escapes(target: int) -> list[str]:
    """The ways this process signalled ``target``, a process outside its ruleset, or
    made it a descriptor's owner: each call that succeeded, by name. Held, there are none.

    The signals are 0, which delivers nothing; ``SIGCONT``, which a running process
    ignores and which OpenShell's supervisor handles apart from the others; and
    ``SIGURG``, which a process ignores unless it asks for it, standing for any other
    signal. Process groups and every process get 0 alone."""
    libc = _libc()
    numbers = _ARCHES.get(os.uname().machine, (0, {}))[1]
    if not numbers:
        return [f"no signal calls known for {os.uname().machine}"]
    found: list[str] = []

    def attempt(name: str, number: int, *args: object) -> None:
        words = [ctypes.c_long(arg) if isinstance(arg, int) else arg for arg in args]
        if int(libc.syscall(ctypes.c_long(number), *words)) >= 0:
            found.append(name)

    def info(sig: int) -> _SigInfo:
        return _SigInfo(sig, 0, _SI_QUEUE, 0, os.getpid(), os.getuid())

    group = os.getpgid(target)
    for sig in (0, int(signal.SIGCONT), int(signal.SIGURG)):
        attempt(f"kill({target}, {sig})", numbers["kill"], target, sig)
        attempt(f"tkill({target}, {sig})", numbers["tkill"], target, sig)
        attempt(f"tgkill({target}, {sig})", numbers["tgkill"], target, target, sig)
        queued = info(sig)
        attempt(
            f"rt_sigqueueinfo({target}, {sig})",
            numbers["rt_sigqueueinfo"], target, sig, ctypes.byref(queued),
        )  # fmt: skip
        attempt(
            f"rt_tgsigqueueinfo({target}, {sig})",
            numbers["rt_tgsigqueueinfo"], target, target, sig, ctypes.byref(queued),
        )  # fmt: skip
        pidfd = int(libc.syscall(ctypes.c_long(_PIDFD_OPEN), ctypes.c_long(target), 0))
        if pidfd >= 0:
            try:
                attempt(
                    f"pidfd_send_signal({target}, {sig})", _PIDFD_SEND_SIGNAL, pidfd, sig, None, 0
                )
            finally:
                os.close(pidfd)
    for pid in (0, -group, -1):
        attempt(f"kill({pid}, 0)", numbers["kill"], pid, 0)
    # A socket, since FIOSETOWN and SIOCSPGRP are sockets' ioctls; fcntl's take any file.
    left, right = socket.socketpair()
    with left, right:
        owner = ctypes.c_int(target)
        descriptor = left.fileno()
        attempt("F_SETOWN", numbers["fcntl"], descriptor, _SETOWN[0], target)
        attempt(
            "F_SETOWN_EX",
            numbers["fcntl"], descriptor, _SETOWN[2], ctypes.byref((ctypes.c_int * 2)(1, target)),
        )  # fmt: skip
        attempt("FIOSETOWN", numbers["ioctl"], descriptor, _IOCTL_SETOWN[0], ctypes.byref(owner))
    return found


def tcp_held() -> bool:
    """Whether this process's TCP rules hold: binding a loopback port is refused. A
    sandbox that makes sockets for its processes (OpenShell 0.1.5 does) binds it outside
    the ruleset."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
    except OSError:
        return True
    return False


def probe() -> int:
    """Apply the ruleset and the filter with nothing writable, then check that they hold:
    no signal reaches the process that started this one (the executor), which is outside
    them. Prints ``{"tcp": …}`` and exits 0 if all of that holds, else ``FAILED`` with why."""
    parent = os.getppid()
    try:
        restrict([])
    except (OSError, ConfinementError) as exc:
        print(exc, file=sys.stderr)
        return FAILED
    if parent <= 1:
        print("no process outside the probe to check its signals against", file=sys.stderr)
        return FAILED
    try:
        found = escapes(parent)
    except OSError as exc:
        print(f"cannot check the signal scope: {exc}", file=sys.stderr)
        return FAILED
    if found:
        print(
            "a signal reached a process outside the ruleset: " + ", ".join(found)[:240],
            file=sys.stderr,
        )
        return FAILED
    print(json.dumps({"tcp": tcp_held()}))
    return 0


def main(argv: Sequence[str]) -> int:
    """``--probe`` checks that the ruleset and the filter apply and hold (``probe``)."""
    if list(argv) == ["--probe"]:
        return probe()
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
