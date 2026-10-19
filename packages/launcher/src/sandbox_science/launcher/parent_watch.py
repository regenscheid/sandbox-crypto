# SPDX-License-Identifier: MIT
"""Darwin part wrapper: stop one launcher's session if its parent disappears.

macOS has no PR_SET_PDEATHSIG. The launcher owns the write end of a pipe; the
wrapper alone receives the read end. The part command does not inherit either
end. EOF is therefore a reliable parent-death notification even after PID reuse.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import time

from .processes import members


def _stop_session(session: int) -> None:
    own = os.getpid()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in members(session):
            if pid != own:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if not any(pid != own for pid in members(session)):
                    return
                time.sleep(0.2)


def main() -> int:
    fd, parent, *command = sys.argv[1:]
    if not command or os.getppid() != int(parent):
        return 70
    session = os.getsid(0)
    print("launcher parent watcher starting part", file=sys.stderr, flush=True)
    child = subprocess.Popen(command, close_fds=True)  # noqa: S603 - trusted launcher command
    print(f"launcher parent watcher started part PID {child.pid}", file=sys.stderr, flush=True)
    try:
        while child.poll() is None:
            ready, _, _ = select.select([int(fd)], [], [], 0.2)
            if ready and not os.read(int(fd), 1):
                _stop_session(session)
                return 70
        return child.returncode or 0
    finally:
        os.close(int(fd))


if __name__ == "__main__":
    raise SystemExit(main())
