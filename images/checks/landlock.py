# SPDX-License-Identifier: MIT
"""Run a command under a Landlock ruleset like the one OpenShell v0.1.3 gives a research sandbox.

images/test.sh uses it to check, outside OpenShell, that the paths a base profile declares for
the OpenShell targets file (docs/IMAGES.md) are enough. OpenShell's supervisor handles every
filesystem access right of Landlock ABI 3 and grants ``read_only`` paths execute, read-file and
read-dir, and ``read_write`` paths all of them. File rules get only the rights that apply to
files. Paths that do not exist are skipped, as OpenShell's best-effort mode does.

Usage: landlock.py [--ro PATH]... [--rw PATH]... -- COMMAND [ARG]...
"""

from __future__ import annotations

import argparse
import ctypes
import os
import stat
import sys

SYS_LANDLOCK_CREATE_RULESET = 444  # the same on x86_64 and aarch64
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
TRUNCATE = 1 << 14
ALL_V3 = (1 << 15) - 1
"""Every filesystem right up to ABI 3: execute through truncate, including refer."""
READ = EXECUTE | READ_FILE | READ_DIR
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    # The kernel's struct is packed (12 bytes); the fields have the same offsets here, and the
    # kernel reads only those 12 bytes.
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _syscall(libc: ctypes.CDLL, number: int, *args: object) -> int:
    result = int(libc.syscall(ctypes.c_long(number), *args))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, f"syscall {number}: {os.strerror(error)}")
    return result


def restrict(read_only: list[str], read_write: list[str]) -> list[str]:
    """Restrict this process and its future children; return the paths that were skipped."""
    libc = ctypes.CDLL(None, use_errno=True)
    abi = _syscall(libc, SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                   ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))  # fmt: skip
    if abi < 3:
        raise OSError(f"Landlock ABI {abi}; OpenShell needs 3")
    attr = RulesetAttr(ALL_V3)
    ruleset = _syscall(libc, SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr),
                       ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))  # fmt: skip
    skipped: list[str] = []
    for paths, rights in ((read_only, READ), (read_write, ALL_V3)):
        for path in paths:
            try:
                descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                skipped.append(path)
                continue
            try:
                directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                rule = PathBeneathAttr(rights if directory else rights & FILE_RIGHTS, descriptor)
                _syscall(libc, SYS_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset),
                         ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule),
                         ctypes.c_uint32(0))  # fmt: skip
            finally:
                os.close(descriptor)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
    _syscall(libc, SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0))
    os.close(ruleset)
    return skipped


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run a command under a Landlock ruleset.")
    parser.add_argument("--ro", action="append", default=[], help="a read-only path")
    parser.add_argument("--rw", action="append", default=[], help="a read-write path")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command")
    skipped = restrict(args.ro, args.rw)
    if skipped:
        print(f"landlock: skipped missing paths: {' '.join(skipped)}", file=sys.stderr)
    os.execvp(command[0], command)  # noqa: S606 - replaces this process, by design
    return 1  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
