# SPDX-License-Identifier: MIT
"""A stand-in for pip and micromamba in a throwaway environment (test_provisioning.py,
test_provisioner.py).

The tests copy it into the environment's site-packages as ``fakepm``. ``bin/pip`` and
``bin/micromamba`` run it, and so does ``python -m pip``, through a ``pip`` package. It
installs a package by writing the records pip or micromamba would, once the file
``FAKEPM_GATE`` names exists and ``FAKEPM_DELAY`` seconds have passed, and appends when
each install starts and ends to the file ``FAKEPM_LOG`` names. Like micromamba, it only
notes in ``conda-meta/history`` a Conda package installed already.

Some package names do more: ``broken`` fails with exit code 3, ``sleepy`` waits for
SIGTERM, ``counting`` logs each SIGTERM and SIGINT and ends half a second after the
first, ``rollback`` takes half a second to roll back on SIGTERM, ``killed`` ends by
SIGKILL, ``spawning`` starts a step (as a post-link script is) that installs
``<name>-step`` after ``FAKEPM_STEP`` seconds and waits for it, ``detaching`` leaves
such a step running, ``withscript`` installs a program too, ``nested`` and ``orphan``
install ``inner`` with ``python -m pip`` first (the second with a cleared environment),
and ``--echo`` prints the arguments and stdin.

For the provisioning tool, a fake index (the JSON file ``FAKEPM_INDEX`` names) describes
packages: ``{"conda": {name: {version, build, subdir, license, depends, provides,
postlink}}, "pypi": {name: {version, license, depends}}}``. ``--dry-run`` prints what an
install would bring, as micromamba's JSON (``actions.LINK``) or pip's report (``--report
-``). With ``FAKEPM_CACHE``, an install first downloads each package file from that
package cache (its URL layout), and fails before linking anything if a download fails.
A Conda package can bring files (``provides``: a path in the prefix and its text,
executable under ``bin/``), and one whose ``postlink`` is ``fail`` is linked, then fails
as a failed post-link script does; one whose ``postlink`` is ``slow`` is linked, then
takes half a minute; one whose ``postlink`` is ``lost`` is linked, then kills the
executor it runs under. Like micromamba, an install honors ``MAMBA_PINNED_PACKAGES``
(``channel/subdir::name==version=build``, separated by commas): it logs the pins, and
fails to solve if a package it would link is pinned to another version or build. It
reaches no network but the cache it is given.
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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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
VALUED = {
    "-p",
    "--prefix",
    "-c",
    "--channel",
    "-r",
    "--requirement",
    "--report",
    "-n",
    "--name",
    "-f",
    "--file",
    "--only-binary",
    "--no-binary",
}
"""Options whose value is the next argument."""


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
    """The subcommand and the other words that are not options or their values."""
    words: list[str] = []
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word in VALUED:
            skip = True
        elif not word.startswith("-"):
            words.append(word)
    return (words[0], words[1:]) if words else ("", [])


def option(args: list[str], *names: str) -> str | None:
    """The value of an option, given as ``--name value`` or ``--name=value``."""
    for index, word in enumerate(args):
        for name in names:
            if word == name and index + 1 < len(args):
                return args[index + 1]
            if word.startswith(name + "="):
                return word[len(name) + 1 :]
    return None


def cache() -> str:
    return os.environ.get("FAKEPM_CACHE", CACHE).rstrip("/")


def index(section: str) -> dict[str, dict[str, Any]]:
    path = os.environ.get("FAKEPM_INDEX")
    if not path:
        return {}
    return json.loads(Path(path).read_text()).get(section, {})


def spec_name(spec: str) -> str:
    """A spec's package name: past any ``channel::``, before any version constraint."""
    name = spec.split("::")[-1]
    for index_, char in enumerate(name):
        if char in " =<>!~[;":
            return name[:index_]
    return name


def download(url: str) -> None:
    """Fetch a package file from the cache, as a client does; raise on a refusal."""
    if "FAKEPM_CACHE" not in os.environ:
        return
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=30) as response:
        response.read()


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


# pip.


def pypi_closure(names: list[str], site: Path) -> list[tuple[str, dict[str, Any]]]:
    """The distributions an install of ``names`` would bring, dependencies first, without
    those installed at that version already."""
    known = index("pypi")
    found: dict[str, dict[str, Any]] = {}

    def visit(name: str) -> None:
        entry = {"version": "1.0", "license": "MIT", "depends": [], **known.get(name, {})}
        if name in found or (site / f"{name}-{entry['version']}.dist-info").exists():
            return
        for dependency in entry["depends"]:
            visit(spec_name(dependency))
        found[name] = entry

    for name in names:
        visit(name)
    return list(found.items())


def wheel_url(name: str, version: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{name}-{version}".encode()).hexdigest()
    file = f"{name}-{version}-py3-none-any.whl"
    return f"{cache()}/pypi/files/{name}/{digest}/packages/fa/ke/{file}", digest


def pip_report(names: list[str], site: Path) -> int:
    install = []
    for name, entry in pypi_closure(names, site):
        url, digest = wheel_url(name, entry["version"])
        install.append(
            {
                "download_info": {"url": url, "archive_info": {"hashes": {"sha256": digest}}},
                "is_direct": False,
                "requested": name in names,
                "metadata": {
                    "metadata_version": "2.4",
                    "name": name,
                    "version": entry["version"],
                    "license_expression": entry["license"],
                },
            }
        )
    print(json.dumps({"version": "1", "pip_version": "25.0", "install": install}))
    return 0


def pip_install_pinned(path: str, site: Path) -> int:
    """``--require-hashes -r FILE``: exactly the files named, each fetched from the cache."""
    wanted: list[tuple[str, str]] = []
    for line in Path(path).read_text().splitlines():
        requirement = line.split()[0] if line.split() else ""
        if "==" in requirement:
            name, _, version = requirement.partition("==")
            wanted.append((name, version))
    for name, version in wanted:
        url, _ = wheel_url(name, version)
        try:
            download(url)
        except (urllib.error.URLError, OSError) as exc:
            print(
                f"ERROR: Could not install packages: HTTP error for {url}: {exc}", file=sys.stderr
            )
            return 1
    for name, version in wanted:
        log("start", name, time.time(), os.getpid())
        pause()
        install_distribution(site, name, version)
        log("end", name, time.time())
    return 0


def install_distribution(site: Path, name: str, version: str) -> None:
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(exist_ok=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n")
    (info / "INSTALLER").write_text("pip\n")


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
    names = [spec_name(name) for name in names]
    if "--dry-run" in args:
        return pip_report(names, site)
    requirements = option(args, "-r", "--requirement")
    if requirements is not None:
        return pip_install_pinned(requirements, site)
    for name in names:
        special(name)
        log("start", name, time.time(), os.getpid())
        pause()
        install_distribution(site, name, "1.0")
        if name == "withscript":  # a distribution with a program of its own
            program = Path(sys.prefix) / "bin" / name
            program.write_text(f"#!/bin/sh\necho ran-{name}\n")
            program.chmod(0o755)
        log("end", name, time.time())
    return 0


# micromamba.


def conda_entry(name: str) -> dict[str, Any]:
    return {
        "version": "1.0",
        "build": "pyhd8ed1ab_0",
        "subdir": "noarch",
        "license": "BSD-3-Clause",
        "depends": [],
        "channel": "conda-forge",
        **index("conda").get(name, {}),
    }


def conda_record(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """A package as micromamba describes it, in its dry run's JSON and in ``conda-meta``."""
    file = f"{name}-{entry['version']}-{entry['build']}.conda"
    channel = f"{cache()}/conda/{entry['channel']}"
    return {
        "name": name,
        "version": entry["version"],
        "build": entry["build"],
        "build_string": entry["build"],
        "build_number": 0,
        # As micromamba records a package that came through the package cache.
        "channel": f"{channel}/{entry['subdir']}",
        "subdir": entry["subdir"],
        "fn": file,
        "url": f"{channel}/{entry['subdir']}/{file}",
        "md5": hashlib.md5(name.encode()).hexdigest(),  # noqa: S324 - a stand-in's digest
        "sha256": hashlib.sha256(f"{name}-{entry['version']}".encode()).hexdigest(),
        "license": entry["license"],
        "depends": entry["depends"],
    }


def conda_closure(names: list[str], prefix: Path) -> list[tuple[str, dict[str, Any]]]:
    """The packages an install of ``names`` would link, dependencies first, without those
    installed at that version and build already."""
    found: dict[str, dict[str, Any]] = {}

    def visit(name: str) -> None:
        entry = conda_entry(name)
        record = prefix / "conda-meta" / f"{name}-{entry['version']}-{entry['build']}.json"
        if name in found or record.exists():
            return
        for dependency in entry["depends"]:
            visit(spec_name(dependency))
        found[name] = entry

    for name in names:
        visit(name)
    return list(found.items())


def pins() -> dict[str, tuple[str, str]]:
    """The version and build MAMBA_PINNED_PACKAGES pins each package to."""
    pinned: dict[str, tuple[str, str]] = {}
    for spec in filter(None, os.environ.get("MAMBA_PINNED_PACKAGES", "").split(",")):
        name, _, rest = spec.split("::")[-1].partition("==")
        version, _, build = rest.partition("=")
        pinned[name] = (version, build)
    return pinned


def conda(tool: str, args: list[str]) -> int:
    prefix = Path(sys.prefix)
    command, names = arguments(args)
    if command not in ("install", "update"):
        return 0
    names = [spec_name(name) for name in names]
    if "--dry-run" in args:
        links = [conda_record(name, entry) for name, entry in conda_closure(names, prefix)]
        actions: dict[str, Any] = {"PREFIX": str(prefix)}
        if links:
            actions["LINK"] = links
            actions["FETCH"] = links
        print(
            json.dumps(
                {"actions": actions, "dry_run": True, "prefix": str(prefix), "success": True}
            )
        )
        return 0
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    planned = conda_closure(names, prefix)
    pinned = pins()
    if "MAMBA_PINNED_PACKAGES" in os.environ:
        log("pinned", os.environ["MAMBA_PINNED_PACKAGES"] or "-")
    for name, entry in planned:
        if name in pinned and pinned[name] != (entry["version"], entry["build"]):
            print(
                f"error    libmamba Could not solve for environment specs: {name} is pinned "
                f"to {'='.join(pinned[name])}",
                file=sys.stderr,
            )
            return 1
    for name, entry in planned:
        url = conda_record(name, entry)["url"]
        try:
            download(url)
        except (urllib.error.URLError, OSError) as exc:
            print(f"error    libmamba Download error: {url}: {exc}", file=sys.stderr)
            return 1
    linking = {name for name, _ in planned}
    for name in names:
        if name not in linking:  # installed already: micromamba links nothing
            with open(prefix / "conda-meta" / "history", "a") as history:
                history.write(f'==> {time.ctime()} <==\n# update specs: ["{name}"]\n')
    for name, entry in planned:
        log("start", name, time.time())
        pause()
        record = conda_record(name, entry)
        record["files"] = [
            f"lib/{version}/site-packages/{name}/__init__.py",
            f"lib/{version}/site-packages/{name}-{entry['version']}.dist-info/INSTALLER",
        ]
        record_path = prefix / "conda-meta" / f"{name}-{entry['version']}-{entry['build']}.json"
        if command == "update":
            for previous in (prefix / "conda-meta").glob(f"{name}-*.json"):
                if json.loads(previous.read_text()).get("name") == name:
                    previous.unlink()
        record_path.write_text(json.dumps(record))
        site = Path(sysconfig.get_paths()["purelib"])
        (site / f"{name}-{entry['version']}.dist-info").mkdir(exist_ok=True)
        (site / f"{name}-{entry['version']}.dist-info" / "INSTALLER").write_text("conda\n")
        for relative, text in entry.get("provides", {}).items():
            target = prefix / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            if relative.startswith("bin/"):
                target.chmod(0o755)
        with open(prefix / "conda-meta" / "history", "a") as history:
            history.write(
                f"==> {time.ctime()} <==\n# cmd: {tool} {' '.join(args)}\n+{record['fn']}\n"
            )
        log("end", name, time.time())
        if entry.get("postlink") == "fail":
            print(f"error    libmamba post-link script failed for {name}", file=sys.stderr)
            return 1
        if entry.get("postlink") == "slow":  # linked, and its post-link script takes a while
            log("postlink", name, os.getpid())
            time.sleep(30)
        if entry.get("postlink") == "lost":  # linked, and the executor dies meanwhile
            kill_executor()
            return 1
    return 0


def kill_executor() -> None:
    """Kill the executor this command runs under, as when a sandbox goes away."""
    pid = os.getppid()
    while pid > 1:
        try:
            words = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return
        if b"--stdio" in words:
            os.kill(pid, signal.SIGKILL)
            return
        pid = int(stat.rsplit(")", 1)[1].split()[1])  # the parent's


def main(argv: list[str]) -> int:
    tool, args = argv[0], argv[1:]
    if args == ["--version"]:
        print("pip 25.2 from /synthetic (python 3.13)" if tool.startswith("pip") else "2.9.0")
        return 0
    if tool.startswith("pip"):
        return pip(args)
    if tool in ("conda", "mamba", "micromamba"):
        return conda(tool, args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
