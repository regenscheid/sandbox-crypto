# SPDX-License-Identifier: MIT
"""The signal shim (``shim/signal_self.c``) in a held process (DA-20): with ``kill`` and
``rt_sigqueueinfo`` refused by the held run's seccomp filter, ``kill``, ``killpg`` and
``sigqueue`` still reach the run's own processes, through calls Landlock checks, and
nothing outside it."""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from sandbox_science.worker import sandbox_confine
from sandbox_science.worker.backends import SIGNAL_SHIM

_, UNAVAILABLE = sandbox_confine.abi()
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        UNAVAILABLE is not None
        or platform.machine() not in ("x86_64", "aarch64")
        or not shutil.which("cc"),
        reason=f"needs Landlock ABI 6 and a C compiler: {UNAVAILABLE}",
    ),
]


@pytest.fixture(scope="module")
def shim(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("shim") / SIGNAL_SHIM.name
    subprocess.run(
        [
            "cc", "-shared", "-fPIC", "-O2", "-Wall", "-Werror",
            f'-D{SIGNAL_SHIM.macro}="{SIGNAL_SHIM.revision}"',
            "-o", str(path), str(SIGNAL_SHIM.source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )  # fmt: skip
    return path


def held(area: Path, code: str, shim: Path | None, **env: str) -> dict[str, object]:
    """Run ``code`` with Python under the held wrapper, in a session of its own, with
    ``shim`` preloaded; it prints one JSON object."""
    variables = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    if shim is not None:
        variables["LD_PRELOAD"] = str(shim)
    argv = [
        *sandbox_confine.command(sys.executable, sandbox_confine.__file__, [str(area)]),
        sys.executable,
        "-c",
        code,
    ]
    done = subprocess.run(
        argv,
        cwd=area,
        env=variables,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        start_new_session=True,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.splitlines()[-1])


PRELUDE = """
import ctypes, errno, json, os, signal, subprocess, sys, time
libc = ctypes.CDLL(None, use_errno=True)
found = {}
def outcome(call):
    try:
        call()
        return "done"
    except OSError as error:
        return errno.errorcode[error.errno]
def c_outcome(result):
    return "done" if result == 0 else errno.errorcode[ctypes.get_errno()]
"""


def test_a_held_process_signals_its_own_and_nothing_outside(tmp_path: Path, shim: Path) -> None:
    outside = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        code = (
            PRELUDE
            + f"""
OUTSIDE = {outside.pid}
child = subprocess.Popen(["sleep", "30"])
child.terminate()
found["terminate"] = child.wait()
child = subprocess.Popen(["sleep", "30"])
child.kill()
found["kill"] = child.wait()
child = subprocess.Popen(["sleep", "30"])
found["exists"] = outcome(lambda: os.kill(child.pid, 0))
os.kill(child.pid, signal.SIGTERM)
found["os.kill"] = child.wait()
group = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30 & wait"], start_new_session=True)
time.sleep(0.3)
os.killpg(group.pid, signal.SIGTERM)
found["killpg"] = group.wait()
got = []
signal.signal(signal.SIGUSR1, lambda *_: got.append("usr1"))
os.kill(os.getpid(), signal.SIGUSR1)
found["self"] = list(got)
value = ctypes.c_void_p(0)
libc.sigqueue.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
found["sigqueue"] = c_outcome(libc.sigqueue(os.getpid(), signal.SIGUSR1, value))
found["after sigqueue"] = got
found["outside"] = outcome(lambda: os.kill(OUTSIDE, signal.SIGTERM))
found["outside 0"] = outcome(lambda: os.kill(OUTSIDE, 0))
found["outside group"] = outcome(lambda: os.killpg(OUTSIDE, signal.SIGTERM))
found["outside sigqueue"] = c_outcome(libc.sigqueue(OUTSIDE, signal.SIGTERM, value))
found["parent"] = outcome(lambda: os.kill(os.getppid(), 0))
found["every"] = outcome(lambda: os.kill(-1, 0))
found["no such"] = outcome(lambda: os.kill(-2147483648, 0))
found["bad signal"] = outcome(lambda: os.kill(os.getpid(), 99))
print(json.dumps(found))
"""
        )
        found = held(tmp_path, code, shim)
        assert outside.poll() is None  # untouched
    finally:
        outside.kill()
        outside.wait()
    assert found == {
        "terminate": -signal.SIGTERM,
        "kill": -signal.SIGKILL,
        "exists": "done",
        "os.kill": -signal.SIGTERM,
        "killpg": -signal.SIGTERM,
        "self": ["usr1"],
        "sigqueue": "done",
        "after sigqueue": ["usr1", "usr1"],
        "outside": "EPERM",
        "outside 0": "EPERM",
        "outside group": "EPERM",
        "outside sigqueue": "EPERM",
        "parent": "EPERM",  # the wrapper's parent, this test: outside
        "every": "done",  # kill(-1) reaches no one outside, and says so as Linux does
        "no such": "ESRCH",
        "bad signal": "EINVAL",
    }


def test_kill_zero_reaches_the_callers_group_and_the_caller_last(
    tmp_path: Path, shim: Path
) -> None:
    """``kill(0, sig)``, as timeout(1) sends it, reaches every member of the caller's
    process group, children forked meanwhile included, and the caller after them."""
    code = (
        PRELUDE
        + """
marks = []
children = [subprocess.Popen(
    [sys.executable, "-c",
     "import signal, sys, time\\n"
     "signal.signal(signal.SIGUSR2, lambda *_: (print('got', flush=True), sys.exit(0)))\\n"
     "print('ready', flush=True)\\n"
     "time.sleep(30)"],
    stdout=subprocess.PIPE, text=True) for _ in range(3)]
for child in children:
    child.stdout.readline()
signal.signal(signal.SIGUSR2, lambda *_: marks.append([child.poll() for child in children]))
os.kill(0, signal.SIGUSR2)
results = [child.wait(10) for child in children]
found["children"] = results
found["self"] = len(marks)
print(json.dumps(found))
"""
    )
    found = held(tmp_path, code, shim)
    assert found == {"children": [0, 0, 0], "self": 1}


def test_without_the_shim_a_held_process_cannot_kill_even_its_own(tmp_path: Path) -> None:
    """The filter, not Landlock alone, refuses ``kill`` and ``rt_sigqueueinfo``: the
    calls a supervisor could carry out for the process. ``tgkill`` stays, checked."""
    code = (
        PRELUDE
        + """
child = subprocess.Popen(["sleep", "30"])
found["kill own"] = outcome(lambda: os.kill(child.pid, signal.SIGTERM))
number = {"x86_64": 234, "aarch64": 131}[os.uname().machine]
found["tgkill own"] = c_outcome(libc.syscall(number, child.pid, child.pid, signal.SIGTERM))
found["status"] = child.wait(10)
print(json.dumps(found))
"""
    )
    found = held(tmp_path, code, None)
    assert found == {"kill own": "EPERM", "tgkill own": "done", "status": -signal.SIGTERM}


def test_the_shim_changes_nothing_in_an_unheld_process(tmp_path: Path, shim: Path) -> None:
    """Preloaded without the wrapper, the shim's signals still arrive (as tgkill)."""
    code = (
        PRELUDE
        + """
child = subprocess.Popen(["sleep", "30"])
child.terminate()
found["terminate"] = child.wait()
print(json.dumps(found))
"""
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "LD_PRELOAD": str(shim)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert json.loads(done.stdout) == {"terminate": -signal.SIGTERM}
