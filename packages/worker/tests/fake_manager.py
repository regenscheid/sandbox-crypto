# SPDX-License-Identifier: MIT
"""A stand-in for pip and micromamba in a throwaway environment (test_provisioning.py).

The tests copy it into the environment's site-packages as ``fakepm``. ``bin/pip`` and
``bin/micromamba`` run it, and so does ``python -m pip``, through a ``pip`` package. It
installs a package by writing the records pip or micromamba would, once the file
``FAKEPM_GATE`` names exists and ``FAKEPM_DELAY`` seconds have passed, and appends when
each install starts and ends to the file ``FAKEPM_LOG`` names. It never reaches the
network.

Some package names do more: ``broken`` fails with exit code 3, ``sleepy`` waits for
SIGTERM, ``counting`` logs each SIGTERM and SIGINT and ends half a second after the
first, ``rollback`` takes half a second to roll back on SIGTERM, ``killed`` ends by
SIGKILL, ``spawning`` starts a step (as a post-link script is) that installs
``<name>-step`` after ``FAKEPM_STEP`` seconds and waits for it, ``detaching`` leaves
such a step running, ``withscript`` installs a program too, ``nested`` and ``orphan``
install ``inner`` with ``python -m pip`` first (the second with a cleared environment),
and ``--echo`` prints the arguments and stdin.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

CACHE = "http://host.openshell.internal:8090"
STEP = """
import pathlib, sys, time
time.sleep(float(sys.argv[1]))
info = pathlib.Path(sys.argv[2]) / (sys.argv[3] + "-step-1.0.dist-info")
info.mkdir(exist_ok=True)
(info / "METADATA").write_text("Metadata-Version: 2.1\\nName: x\\nVersion: 1.0\\n\\n")
if sys.argv[4]:
    with open(sys.argv[4], "a") as handle:
        handle.write("step wrote " + sys.argv[3] + "\\n")
"""


def log(*words: object) -> None:
    path = os.environ.get("FAKEPM_LOG")
    if path:
        with open(path, "a") as handle:
            handle.write(" ".join(str(word) for word in words) + "\n")


def pause() -> None:
    """Hold the install until the file FAKEPM_GATE names exists, then FAKEPM_DELAY seconds."""
    gate = os.environ.get("FAKEPM_GATE")
    deadline = time.monotonic() + 60
    while gate and not os.path.exists(gate) and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(float(os.environ.get("FAKEPM_DELAY", "0")))


def arguments(args: list[str]) -> tuple[str, list[str]]:
    words = [word for word in args if not word.startswith("-")]
    return (words[0], words[1:]) if words else ("", [])


def special(name: str) -> None:
    if name == "broken":
        sys.exit(3)
    if name == "sleepy":

        def terminated(signum: int, frame: object) -> None:
            log("got", "TERM")
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

        signal.signal(signal.SIGTERM, terminated)
        log("ready", "sleepy")
        while True:
            time.sleep(0.1)
    if name == "counting":
        first: list[float] = []

        def counted(signum: int, frame: object) -> None:
            log("got", "INT" if signum == signal.SIGINT else "TERM")
            first.append(time.monotonic())

        signal.signal(signal.SIGTERM, counted)
        signal.signal(signal.SIGINT, counted)
        log("ready", "counting", os.getpid())
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (first and time.monotonic() > first[0] + 0.5):
            time.sleep(0.01)
        sys.exit(143)
    if name == "killed":  # as the kernel's OOM killer ends a process
        os.kill(os.getpid(), signal.SIGKILL)
    if name == "rollback":  # as conda puts the prefix back on SIGTERM

        def rolled(signum: int, frame: object) -> None:
            log("got", "TERM")
            time.sleep(0.5)
            log("rolled", "back")
            sys.exit(1)

        signal.signal(signal.SIGTERM, rolled)
        log("ready", "rollback", os.getpid())
        time.sleep(60)
    if name in ("spawning", "detaching"):
        site = sysconfig.get_paths()["purelib"]
        wait = os.environ.get("FAKEPM_STEP", "30")
        where = os.environ.get("FAKEPM_LOG", "")
        step = subprocess.Popen([sys.executable, "-c", STEP, wait, site, name, where])  # noqa: S603
        log("ready", name, os.getpid(), step.pid)
        if name == "spawning":
            step.wait()
    if name in ("nested", "orphan"):
        env = None
        if name == "orphan":  # a step that lost the turn's mark
            env = {"PATH": os.environ["PATH"], "PYTHONPATH": os.environ.get("PYTHONPATH", "")}
        command = [sys.executable, "-m", "pip", "install", "inner"]
        subprocess.run(command, env=env, check=True, timeout=60)  # noqa: S603 - a fixed command


def pip(args: list[str]) -> int:
    site = Path(sysconfig.get_paths()["purelib"])
    command, names = arguments(args)
    if "--echo" in args:
        print(json.dumps({"args": args, "stdin": sys.stdin.read()}))
        return 0
    if command in ("list", "freeze"):
        for entry in sorted(site.glob("*.dist-info")):
            print(entry.name.removesuffix(".dist-info"))
        return 0
    if command == "uninstall":
        for name in names:
            for entry in site.glob(f"{name}-*.dist-info"):
                for item in entry.iterdir():
                    item.unlink()
                entry.rmdir()
        return 0
    if command != "install":
        return 0
    for name in names:
        special(name)
        log("start", name, time.time(), os.getpid())
        pause()
        info = site / f"{name}-1.0.dist-info"
        info.mkdir(exist_ok=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n\n")
        (info / "INSTALLER").write_text("pip\n")
        if name == "withscript":  # a distribution with a program of its own
            program = Path(sys.prefix) / "bin" / name
            program.write_text(f"#!/bin/sh\necho ran-{name}\n")
            program.chmod(0o755)
        log("end", name, time.time())
    return 0


def conda(tool: str, args: list[str]) -> int:
    prefix = Path(sys.prefix)
    command, names = arguments(args)
    if command != "install":
        return 0
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for name in names:
        log("start", name, time.time())
        pause()
        file = f"{name}-1.0-pyhd8ed1ab_0.conda"
        digest = hashlib.sha256(name.encode()).hexdigest()
        record = {
            "name": name,
            "version": "1.0",
            "build": "pyhd8ed1ab_0",
            "build_number": 0,
            # As micromamba records a package that came through the package cache.
            "channel": f"{CACHE}/conda/conda-forge/noarch",
            "subdir": "noarch",
            "fn": file,
            "url": f"{CACHE}/conda/conda-forge/noarch/{file}",
            "md5": hashlib.md5(name.encode()).hexdigest(),  # noqa: S324 - a stand-in's digest
            "sha256": digest,
            "license": "BSD-3-Clause",
            "depends": ["python >=3.9"],
            "files": [
                f"lib/{version}/site-packages/{name}/__init__.py",
                f"lib/{version}/site-packages/{name}-1.0.dist-info/INSTALLER",
            ],
        }
        (prefix / "conda-meta" / f"{name}-1.0-pyhd8ed1ab_0.json").write_text(json.dumps(record))
        site = Path(sysconfig.get_paths()["purelib"])
        (site / f"{name}-1.0.dist-info").mkdir(exist_ok=True)
        (site / f"{name}-1.0.dist-info" / "INSTALLER").write_text("conda\n")
        with open(prefix / "conda-meta" / "history", "a") as history:
            history.write(f"==> {time.ctime()} <==\n# cmd: {tool} {' '.join(args)}\n+{file}\n")
        log("end", name, time.time())
    return 0


def main(argv: list[str]) -> int:
    tool, args = argv[0], argv[1:]
    if tool.startswith("pip"):
        return pip(args)
    if tool in ("conda", "mamba", "micromamba"):
        return conda(tool, args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
