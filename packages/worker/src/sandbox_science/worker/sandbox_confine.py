# SPDX-License-Identifier: MIT
"""Hold a delegated run's processes to its write area (delegated agents spec, DA-20).

The executor starts a delegated run's shell commands, jobs and kernels through this file,
as ``python -I -S sandbox_confine.py --rw DIR … -- COMMAND …``. It adds a Landlock
ruleset to itself, nested inside the sandbox's own (OpenShell's), then replaces itself
with the command, so the command and everything it starts can write only below the
directories named, and never lift the rule. Reads are left to the sandbox's ruleset.
The same ruleset scopes abstract Unix sockets and signals (Landlock ABI 6): the process
cannot connect to a socket made outside it, such as another run's kernel, nor signal a
process outside it.

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
)
"""Every right that changes the file system, up to ABI 3. Execute, read-file and read-dir
are not handled, so this ruleset leaves reading to the sandbox's."""
FILE_RIGHTS: Final = WRITE_FILE | TRUNCATE
"""The handled rights a rule on a file (not a directory) may name."""
SCOPE_ABSTRACT_UNIX_SOCKET: Final = 1 << 0
SCOPE_SIGNAL: Final = 1 << 1
SCOPED: Final = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL

DEVICES: Final = (
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/tty",
    "/dev/ptmx",
    "/dev/pts",
    "/dev/shm",  # noqa: S108 - the shared-memory device, not a temporary file
)
"""Writable wherever they exist: the sinks, pseudo-terminals (Sage's pexpect interfaces)
and shared memory (Python's multiprocessing), which the sandbox's policy grants too."""


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


def _ruleset(libc: ctypes.CDLL) -> int:
    attr = _RulesetAttr(HANDLED, 0, SCOPED)
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


def _allow(libc: ctypes.CDLL, ruleset: int, descriptor: int) -> None:
    directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
    rule = _PathBeneathAttr(HANDLED if directory else FILE_RIGHTS, descriptor)
    _syscall(
        libc, _ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH),
        ctypes.byref(rule), ctypes.c_uint32(0),
    )  # fmt: skip


def restrict(writable: Sequence[str], devices: Sequence[str] = DEVICES) -> None:
    """Restrict this process and all it starts: write only below ``writable`` (absolute
    directories, opened without following links; each must exist) and to ``devices``
    (followed; those missing are skipped), with abstract Unix sockets and signals scoped.
    Raises ``ConfinementError`` before restricting anything if the ruleset is incomplete."""
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
                _allow(libc, ruleset, descriptor)
            finally:
                os.close(descriptor)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise ConfinementError(f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(ctypes.get_errno())}")
        _syscall(libc, _RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0))
    finally:
        os.close(ruleset)


def command(python: str, script: str, writable: Sequence[str]) -> list[str]:
    """The argv prefix that runs a command under ``restrict(writable)``."""
    prefix = [python, "-I", "-S", script]
    for path in writable:
        prefix += ["--rw", path]
    return [*prefix, "--"]


def main(argv: Sequence[str]) -> int:
    writable: list[str] = []
    rest = list(argv)
    while rest and rest[0] == "--rw" and len(rest) > 1:
        writable.append(rest[1])
        rest = rest[2:]
    if not rest or rest[0] != "--" or len(rest) < 2:
        print("usage: sandbox_confine.py [--rw DIR]... -- COMMAND [ARG]...", file=sys.stderr)
        return FAILED
    try:
        restrict(writable)
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
