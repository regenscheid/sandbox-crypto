# SPDX-License-Identifier: MIT
"""Package commands: turns, waits and revisions with lockfiles (docs/specs/conda-provisioning.md).

A throwaway environment (a venv with a ``conda-meta`` directory) stands in for the
sandbox's Conda prefix, and fake_manager.py for pip and micromamba, so nothing here
installs a real package or reaches the network. The wrappers and the hook run as real
processes; the worker tests use the local backend and real executors, or
hostile_executor.py for an executor that research code has taken over.

The last two sections hold the regression tests for the adversarial reviews of package
commands (2026-10-11). Each names the finding it covers, of the first review (C1, H1,
M1 to M6, L1 to L5) or of the second (review2, M1 to M3 and L1 to L6), and repeats its
probe.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import sysconfig
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentState,
    Lockfile,
    RevisionHistory,
)
from sandbox_science.contracts.revisions import MAX_REVISION_PAGE_BYTES
from sandbox_science.worker import executor as executor_module
from sandbox_science.worker import provisioning
from sandbox_science.worker import revisions as revisions_module
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import ClosedHandler, LocalBackend
from sandbox_science.worker.channel import EventHandler, ExecutorChannel
from sandbox_science.worker.executor import Executor, Running, signal_processes, without_wrapped
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.revisions import Prepared, Revisions, package_command
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

FAKE = Path(__file__).with_name("fake_manager.py")
CACHE_HOST = "host.openshell.internal"
CACHE = f"http://{CACHE_HOST}:8090"
"""The package cache's URL, as the worker's settings name it (package_cache)."""
TOKEN = "p" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CONTEXT = {"project_id": "p1", "session_id": "s1", "environment_id": "env1"}

ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]


# A throwaway environment with stand-in package managers.


@dataclass
class Prefix:
    """A venv standing in for the sandbox's Conda prefix."""

    root: Path
    python: Path
    site: Path
    log: Path

    def spans(self) -> dict[str, tuple[float, float]]:
        """When each fake install started and ended, by package name."""
        starts: dict[str, float] = {}
        spans: dict[str, tuple[float, float]] = {}
        for line in self.log.read_text().splitlines() if self.log.exists() else []:
            word, name, *rest = line.split()
            if word == "start":
                starts[name] = float(rest[0])
            elif word == "end":
                spans[name] = (starts[name], float(rest[0]))
        return spans

    def pid(self, word: str, name: str, timeout: float = 30) -> int:
        """The pid the fake manager logged with ``word`` (start, ready) for ``name``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.log.read_text().splitlines() if self.log.exists() else []:
                words = line.split()
                if words[:2] == [word, name]:
                    return int(words[-1])
            time.sleep(0.02)
        raise AssertionError(f"{word} {name} never appeared in the log")


def make_prefix(directory: Path, *, kernels: bool = False) -> Prefix:
    root = directory / "prefix"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True)  # noqa: S603
    python = root / "bin" / "python"
    purelib = "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    site = Path(subprocess.check_output([python, "-c", purelib], text=True).strip())  # noqa: S603
    (root / "conda-meta").mkdir()
    (root / "conda-meta" / "history").write_text("")
    shutil.copy(FAKE, site / "fakepm.py")
    (site / "pip").mkdir()
    (site / "pip" / "__init__.py").write_text("")
    (site / "pip" / "__main__.py").write_text(
        "import sys\nimport fakepm\nsys.exit(fakepm.main(['pip', *sys.argv[1:]]))\n"
    )
    for tool in ("pip", "pip3", "micromamba"):
        script = root / "bin" / tool
        script.write_text(f'#!/bin/sh\nexec "{python}" -m fakepm {tool} "$@"\n')
        script.chmod(0o755)
    if kernels:
        # The kernel packages of the test's own environment, after the prefix's own.
        (site / "test-kernels.pth").write_text(sysconfig.get_paths()["purelib"] + "\n")
    return Prefix(root, python, site, directory / "installs.log")


@dataclass
class Sandbox:
    """A prefix with wrappers and the hook in place, as an executor sets them up."""

    prefix: Prefix
    state: provisioning.State
    config: provisioning.Config
    env: dict[str, str]

    def run(self, *argv: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        options: dict[str, Any] = {"capture_output": True, "text": True, "timeout": 60}
        return subprocess.run(argv, env=self.env, check=False, **(options | kwargs))  # noqa: S603

    def start(self, *argv: str, **env: str) -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603
            argv, env={**self.env, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

    def records(self) -> list[dict[str, Any]]:
        journal = sorted(self.state.journal.glob("*.json"))
        return [json.loads(path.read_text()) for path in journal]

    def wait_for(self, line: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.prefix.log.exists() and line in self.prefix.log.read_text():
                return
            time.sleep(0.05)
        raise AssertionError(f"{line!r} never appeared in the log")


def hermetic_path(prefix: Prefix, directory: Path) -> str:
    """``PATH`` with the prefix's programs and a shell, and no package manager of the host's."""
    tools = directory / "tools"
    tools.mkdir(exist_ok=True)
    with contextlib.suppress(FileExistsError):
        (tools / "sh").symlink_to("/bin/sh")
    return f"{prefix.root / 'bin'}:{tools}"


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    prefix = make_prefix(tmp_path)
    state = provisioning.State(tmp_path / "state")
    places = provisioning.Places(str(prefix.root / "conda-meta"), (str(prefix.site),))
    script = str(Path(provisioning.__file__))
    config = provisioning.Config(str(prefix.python), script, str(prefix.root), places)
    provisioning.prepare(state, config)
    path = hermetic_path(prefix, tmp_path)
    wrapped = provisioning.refresh(state, config, path)
    env = {"PATH": path, "HOME": str(tmp_path), "FAKEPM_LOG": str(prefix.log)}
    env[provisioning.OWNER] = "call-test"
    env.update(provisioning.variables(state, env, wrapped))
    provisioning.baseline(state, places)
    return Sandbox(prefix, state, config, env)


def waiters(lock: Path) -> int:
    """How many processes wait for ``lock``: /proc/locks shows a blocked flock with ``->``."""
    inode = str(lock.stat().st_ino)
    lines = Path("/proc/locks").read_text().splitlines()
    return sum(1 for line in lines if "->" in line.split() and line.split()[-3].endswith(inode))


def until_waiting(lock: Path, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not waiters(lock):
        assert time.monotonic() < deadline, "nothing came to wait for the lock"
        time.sleep(0.02)


@contextlib.contextmanager
def held(state: provisioning.State) -> Iterator[None]:
    """The environment's lock, held as a package command holds it."""
    descriptor = state.try_lock(exclusive=True)
    assert descriptor is not None
    try:
        yield
    finally:
        os.close(descriptor)


def alive(pid: int) -> bool:
    """Whether a process runs: neither gone nor a zombie."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


# Which commands take a turn.


COMMANDS = [
    ("pip", ["install", "sympy"], True),
    ("pip3.14", ["--quiet", "install", "-r", "requirements.txt"], True),
    ("pip", ["uninstall", "-y", "sympy"], True),
    ("pip", ["list"], False),
    ("pip", ["--version"], False),
    ("pip", ["download", "sympy"], False),
    ("uv", ["pip", "install", "sympy"], True),
    ("uv", ["pip", "sync", "requirements.txt"], True),
    ("uv", ["pip", "list"], False),
    ("uv", ["sync"], False),  # a project's own .venv, not the prefix
    ("uv", ["tool", "install", "ruff"], False),
    ("micromamba", ["install", "-y", "toolz"], True),
    ("mamba", ["remove", "toolz"], True),
    ("conda", ["env", "update", "-f", "environment.yml"], True),
    ("conda", ["update", "--all"], True),
    ("conda", ["create", "-n", "other", "python"], False),
    ("conda", ["list"], False),
    ("conda", ["info"], False),
    ("python", ["install"], False),
]


@pytest.mark.parametrize(("tool", "args", "changes"), COMMANDS)
def test_commands_that_change_the_environment_take_a_turn(
    tool: str, args: list[str], changes: bool
) -> None:
    assert provisioning.changes_environment(tool, args) is changes


# Lockfiles name channels and upstream URLs, never the package cache (CP-18).


def _meta(directory: Path, name: str, record: dict[str, Any]) -> None:
    (directory / f"{name}.json").write_text(json.dumps(record))


def _dist(site: Path, name: str, version: str, installer: str | None, **files: str) -> Path:
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n")
    if installer is not None:
        (info / "INSTALLER").write_text(installer + "\n")
    for file, content in files.items():
        (info / file.replace("_json", ".json")).write_text(content)
    return info


NUMPY = "numpy-2.1.0-py313h0_0.conda"
WHEEL = "ab/cd/cached-1.0-py3-none-any.whl"


def installed(tmp_path: Path) -> provisioning.Places:
    """An environment with Conda packages from the cache, conda.anaconda.org and elsewhere,
    and distributions pip and uv installed beside them."""
    meta, site, user = tmp_path / "conda-meta", tmp_path / "site", tmp_path / "user"
    for directory in (meta, site, user):
        directory.mkdir()
    _meta(
        meta,
        "numpy-2.1.0-py313h0_0",
        {
            "name": "numpy",
            "version": "2.1.0",
            "build": "py313h0_0",
            "url": f"{CACHE}/conda/conda-forge/linux-64/{NUMPY}",
            "channel": f"{CACHE}/conda/conda-forge/linux-64",
            "fn": NUMPY,
            "sha256": "a" * 64,
            "md5": "b" * 32,
            "license": "BSD-3-Clause",
            "files": ["lib/python3.13/site-packages/numpy-2.1.0.dist-info/METADATA"],
        },
    )
    upstream = {
        "python": "https://conda.anaconda.org/conda-forge/linux-64/python-3.13.0-h0_cp313.conda",
        "dev": "https://conda.anaconda.org/conda-forge/label/dev/noarch/dev-1.0-0.conda",
        "secret": "https://conda.anaconda.org/t/test-secret-token/private/noarch/secret-1.0-0.conda",
        "local": "file:///tmp/channel/noarch/local-1.0-0.tar.bz2",
        "elsewhere": "https://cache.example/mirror/conda/bioconda/linux-64/elsewhere-1.0-0.conda",
    }
    for name, url in upstream.items():
        version, build = url.rsplit("/", 1)[1].split("-")[1:3]
        build = build.split(".")[0]
        _meta(meta, name, {"name": name, "version": version, "build": build, "url": url})
    # No URL: conda's channel names its subdir too.
    channel = "https://user:test-secret@conda.anaconda.org/conda-forge/linux-64"
    old = {"name": "old", "version": "1", "build": "0", "channel": channel, "subdir": "linux-64"}
    _meta(meta, "old-1-0", {**old, "fn": "old-1-0.tar.bz2"})
    (meta / "broken.json").write_text("{not json")
    (meta / "list.json").write_text("[]")
    _dist(site, "numpy", "2.1.0", "conda")  # Conda's, by its file list
    _dist(site, "certifi", "2024.8.30", "conda")  # Conda's, by its installer
    _dist(site, "requests", "2.32.0", "pip")
    cached = {"url": f"{CACHE}/pypi/files/cached/{'c' * 64}/{WHEEL}"}
    _dist(site, "cached", "1.0", "uv", direct_url_json=json.dumps(cached))
    local = "file:///sandbox/project/sessions/s1/dist/local_pkg-0.1-py3-none-any.whl"
    archive = {"url": local, "archive_info": {"hashes": {"sha256": "d" * 64}}}
    _dist(site, "local_pkg", "0.1", "pip", direct_url_json=json.dumps(archive))
    vcs = {"url": "git+https://user:test-secret@github.com/o/r.git", "vcs_info": {"vcs": "git"}}
    _dist(site, "fromgit", "0.2", "pip", direct_url_json=json.dumps(vcs))
    (site / "legacy-1.0-py3.13.egg-info").write_text("Metadata-Version: 1.1\nName: legacy\n")
    _dist(user, "userpkg", "1.0", "pip")
    return provisioning.Places(str(meta), (str(site),), str(user))


def test_a_lockfile_names_packages_by_channel(tmp_path: Path) -> None:
    made = provisioning.lockfile(installed(tmp_path))
    lockfile = Lockfile.model_validate(made)
    assert "test-secret" not in json.dumps(made)
    assert lockfile.skipped == 2  # broken.json and list.json
    conda = {package.name: package for package in lockfile.conda}
    assert sorted(conda) == ["dev", "elsewhere", "local", "numpy", "old", "python", "secret"]
    numpy = conda["numpy"]
    assert (numpy.subdir, numpy.file, numpy.sha256) == ("linux-64", NUMPY, "a" * 64)
    assert numpy.license == "BSD-3-Clause"
    # The executor cannot tell the package cache's URL: the worker names its channels.
    assert numpy.channel == f"{CACHE}/conda/conda-forge"
    assert conda["python"].channel == "conda-forge"
    assert conda["dev"].channel == "conda-forge/label/dev"
    assert conda["secret"].channel == "private"
    assert conda["local"].channel == "file:///tmp/channel"
    # Any other host keeps its URL, laid out like the cache or not.
    assert conda["elsewhere"].channel == "https://cache.example/mirror/conda/bioconda"
    assert (conda["old"].channel, conda["old"].subdir) == ("conda-forge", "linux-64")
    pip = {item.name: item for item in lockfile.pip}
    assert sorted(pip) == ["cached", "fromgit", "legacy", "local_pkg", "requests", "userpkg"]
    assert (pip["requests"].version, pip["requests"].installer) == ("2.32.0", "pip")
    assert (
        pip["local_pkg"].url
        == "file:///sandbox/project/sessions/s1/dist/local_pkg-0.1-py3-none-any.whl"
    )
    assert pip["local_pkg"].sha256 == "d" * 64
    assert pip["fromgit"].url == "git+https://github.com/o/r.git"
    assert pip["legacy"].version == "1.0"
    assert pip["userpkg"].location == "user"
    assert lockfile.python == ".".join(map(str, sys.version_info[:3]))


def test_the_worker_names_the_caches_channels_and_files_upstream(tmp_path: Path) -> None:
    made = provisioning.lockfile(installed(tmp_path))
    revisions = Revisions(
        Store(tmp_path / "worker.sqlite3"), Blobs(tmp_path / "lockfiles"), cache=CACHE
    )
    try:
        prepared = revisions.prepare(made)
        assert prepared is not None
        assert revisions.blobs.path(prepared.ref.sha256).read_bytes() == prepared.data
        assert CACHE_HOST.encode() not in prepared.data
        lockfile = Lockfile.model_validate_json(prepared.data)
        conda = {package.name: package for package in lockfile.conda}
        assert (conda["numpy"].channel, conda["numpy"].file) == ("conda-forge", NUMPY)
        assert conda["elsewhere"].channel == "https://cache.example/mirror/conda/bioconda"
        pip = {item.name: item for item in lockfile.pip}
        assert pip["cached"].url == f"https://files.pythonhosted.org/{WHEEL}"
        assert pip["cached"].sha256 == "c" * 64
        assert revisions.prepare({"conda": [{"name": "no version"}]}) is None
        assert revisions.prepare("not a lockfile") is None
    finally:
        revisions.store.close()


# The wrappers and the hook, as processes.


def test_wrappers_are_written_for_the_managers_on_path(sandbox: Sandbox) -> None:
    bin_dir = sandbox.state.bin
    wrappers = {"micromamba", "pip", "pip3"}
    assert wrappers <= {path.name for path in bin_dir.iterdir()}
    assert sandbox.env["PATH"].split(":")[0] == str(bin_dir)
    assert sandbox.env["PYTHONPATH"].split(":")[0] == str(sandbox.state.hook)
    assert sandbox.env["MAMBA_EXE"] == str(bin_dir / "micromamba")  # what %micromamba runs
    assert "CONDA_EXE" not in sandbox.env
    found = sandbox.run("sh", "-c", "command -v pip")
    assert found.stdout.strip() == str(bin_dir / "pip")
    # A manager that goes loses its wrapper, so that `command -v` tells the truth.
    (sandbox.prefix.root / "bin" / "micromamba").unlink()
    path = hermetic_path(sandbox.prefix, sandbox.prefix.root.parent)
    assert provisioning.refresh(sandbox.state, sandbox.config, path) == ["pip", "pip3"]
    assert not (bin_dir / "micromamba").exists()
    # A variable already set stays: IPython's %conda then runs what it names.
    preset = provisioning.variables(sandbox.state, {"CONDA_EXE": "/opt/conda/bin/conda"}, ["conda"])
    assert "CONDA_EXE" not in preset


def test_the_shell_wrappers_tell_changing_commands_as_the_executor_does(sandbox: Sandbox) -> None:
    # A wrapper runs reading commands itself, without Python: its test of the arguments
    # must agree with changes_environment, which the turn checks again.
    for tool in ("uv", "conda", "mamba"):
        script = sandbox.prefix.root / "bin" / tool
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
    path = hermetic_path(sandbox.prefix, sandbox.prefix.root.parent)
    wrapped = provisioning.refresh(sandbox.state, sandbox.config, path)
    for tool, args, changes in COMMANDS:
        if tool not in wrapped:
            continue
        before = len(sandbox.records())
        assert sandbox.run(tool, *args).returncode == 0, (tool, args)
        assert len(sandbox.records()) == before + changes, (tool, args)


def test_two_installs_take_turns(sandbox: Sandbox) -> None:
    gate = sandbox.prefix.root.parent / "gate"  # alpha's install holds its turn until it opens
    first = sandbox.start("pip", "install", "alpha", FAKEPM_GATE=str(gate))
    sandbox.wait_for("start alpha")
    second = sandbox.start("pip", "install", "beta")
    until_waiting(sandbox.state.lock)
    gate.touch()
    assert first.wait(30) == 0
    assert second.wait(30) == 0
    assert second.stderr is not None
    assert "waiting for a package command to finish" in second.stderr.read()
    spans = sandbox.prefix.spans()
    assert spans["alpha"][1] <= spans["beta"][0]
    records = sandbox.records()
    assert [record["command"] for record in records] == [
        ["pip", "install", "alpha"],
        ["pip", "install", "beta"],
    ]
    assert records[0]["after"] == records[1]["before"] != records[0]["before"]
    assert [record["via"] for record in records] == ["wrapper", "wrapper"]
    assert [record["owner"] for record in records] == ["call-test", "call-test"]
    assert [item["name"] for item in records[1]["lockfile"]["pip"]] == ["alpha", "beta"]


def test_python_m_pip_waits_for_its_turn(sandbox: Sandbox) -> None:
    with held(sandbox.state):
        process = sandbox.start(str(sandbox.prefix.python), "-m", "pip", "install", "gamma")
        time.sleep(1)
        assert process.poll() is None
        assert sandbox.prefix.spans() == {}
    assert process.wait(30) == 0
    assert process.stderr is not None
    assert "waiting for a package command to finish" in process.stderr.read()
    [record] = sandbox.records()
    assert record["via"] == "module"
    assert record["command"][1:] == ["-m", "pip", "install", "gamma"]
    assert record["exit_code"] == 0
    assert [item["name"] for item in record["lockfile"]["pip"]] == ["gamma"]


def test_reading_commands_do_not_wait(sandbox: Sandbox) -> None:
    with held(sandbox.state):
        started = time.monotonic()
        listed = sandbox.run("pip", "list")
        version = sandbox.run(str(sandbox.prefix.python), "-m", "pip", "list")
        assert time.monotonic() - started < 10
    assert (listed.returncode, version.returncode) == (0, 0)
    assert sandbox.records() == []


def test_wrappers_pass_arguments_stdin_and_exit_codes_through(sandbox: Sandbox) -> None:
    echoed = sandbox.run("pip", "install", "--echo", "two words", "x", input="from stdin")
    assert echoed.returncode == 0
    assert json.loads(echoed.stdout) == {
        "args": ["install", "--echo", "two words", "x"],
        "stdin": "from stdin",
    }
    failed = sandbox.run("pip", "install", "broken")
    assert failed.returncode == 3
    assert sandbox.records()[-1]["exit_code"] == 3


def test_a_signal_to_a_wrapper_reaches_its_command(sandbox: Sandbox) -> None:
    process = sandbox.start("pip", "install", "sleepy")
    sandbox.wait_for("ready sleepy")
    process.send_signal(signal.SIGTERM)
    assert process.wait(30) == -signal.SIGTERM
    sandbox.wait_for("got TERM")
    [record] = sandbox.records()
    assert (record["exit_code"], record["signal"]) == (None, signal.SIGTERM)
    with held(sandbox.state):  # given up
        pass


@pytest.mark.parametrize("name", ["nested", "orphan"])
def test_an_install_inside_another_runs_in_its_turn(sandbox: Sandbox, name: str) -> None:
    # pip's build isolation runs `python -m pip`, inside the outer turn: no deadlock,
    # with or without the turn's mark in its environment.
    result = sandbox.run("pip", "install", name)
    assert result.returncode == 0, result.stderr
    [record] = sandbox.records()
    assert record["command"] == ["pip", "install", name]
    assert [item["name"] for item in record["lockfile"]["pip"]] == ["inner", name]


def test_a_change_before_a_command_is_no_part_of_its_record(sandbox: Sandbox) -> None:
    known = provisioning.fingerprint(sandbox.config.places)
    (sandbox.prefix.site / "sneaky-1.0.dist-info").mkdir()  # no wrapper saw this
    assert sandbox.run("pip", "install", "delta").returncode == 0
    observed = provisioning.observe(sandbox.state, sandbox.config.places, known)
    [(name, record)] = observed.records
    assert record["command"] == ["pip", "install", "delta"]
    assert record["before"] != known  # the executor reports that change apart, unmanaged
    assert observed.fingerprint == record["after"]
    assert observed.lockfile is None  # not asked for
    # The record stays in the journal until the worker has it.
    assert [entry["command"] for entry in sandbox.records()] == [["pip", "install", "delta"]]
    sandbox.state.remove_records([name])
    assert sandbox.records() == []


# The worker: executions wait while a package command runs, and each change is a revision.


class Worker:
    """A worker whose executor runs with the throwaway prefix, or a hostile stand-in."""

    def __init__(
        self, client: TestClient[Any, Any], manager: ExecutionManager, root: Path, prefix: Prefix
    ) -> None:
        self.client = client
        self.manager = manager
        self.root = root
        self.prefix = prefix

    @property
    def state(self) -> provisioning.State:
        """The executor's state directory, shared with its package commands."""
        project = self.root / "environments" / "env1" / "project"
        return provisioning.State(project / ".executor" / "provisioning")

    async def get(self, path: str) -> tuple[int, dict[str, Any]]:
        response = await self.client.get(path, headers=AUTH)
        return response.status, await response.json()

    async def submit(self, operation_id: str, session: str = "s1", **request: Any) -> str:
        context = {**CONTEXT, "session_id": session}
        body = {"operation_id": operation_id, "context": context, "kind": "shell", **request}
        response = await self.client.post("/v1/executions", json=body, headers=AUTH)
        execution = await response.json()
        assert response.status == 201, execution
        return str(execution["execution_id"])

    async def finish(self, execution_id: str, timeout: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, execution = await self.get(f"/v1/executions/{execution_id}")
            if execution["result"] is not None:
                return execution
            await asyncio.sleep(0.05)
        raise AssertionError(f"{execution_id} did not finish")

    async def run(self, operation_id: str, session: str = "s1", **request: Any) -> dict[str, Any]:
        return await self.finish(await self.submit(operation_id, session, **request))

    async def until(self, execution_id: str, state: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, execution = await self.get(f"/v1/executions/{execution_id}")
            if execution["state"] == state:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"{execution_id} never reached {state}")

    async def waiting(self, timeout: float = 60) -> None:
        """Until a package command waits for its turn."""
        deadline = time.monotonic() + timeout
        while not (self.state.lock.exists() and waiters(self.state.lock)):
            assert time.monotonic() < deadline, "nothing came to wait for the lock"
            await asyncio.sleep(0.02)

    async def logged(self, line: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.prefix.log.exists() and line in self.prefix.log.read_text():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"{line!r} never appeared in the log")

    async def page(self, after: int = -1) -> tuple[RevisionHistory, int]:
        """A page of the history, and its size in bytes."""
        response = await self.client.get(
            f"/v1/environments/env1/revisions?after={after}", headers=AUTH
        )
        data = await response.read()
        assert response.status == 200, data[:500]
        return RevisionHistory.model_validate_json(data), len(data)

    async def revisions(self) -> RevisionHistory:
        return (await self.page())[0]

    async def lockfile(self, revision: int) -> tuple[Lockfile, bytes]:
        response = await self.client.get(
            f"/v1/environments/env1/revisions/{revision}/lockfile", headers=AUTH
        )
        assert response.status == 200, await response.text()
        data = await response.read()
        assert response.headers["X-Content-SHA256"] == hashlib.sha256(data).hexdigest()
        return Lockfile.model_validate_json(data), data


async def start_worker(
    aiohttp_client: ClientFactory, root: Path, prefix: Prefix
) -> tuple[Worker, Store]:
    store = Store(root / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(python=str(prefix.python)), root),
        Blobs(root / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=root,
        package_cache=CACHE,
    )
    await manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN))
    return Worker(client, manager, root, prefix), store


@pytest_asyncio.fixture
async def worker(
    aiohttp_client: ClientFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Worker]:
    prefix = make_prefix(tmp_path, kernels=True)
    # Only the stand-ins: the host's own package managers are not on the executor's PATH.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("FAKEPM_LOG", str(prefix.log))
    started, store = await start_worker(aiohttp_client, tmp_path / "worker", prefix)
    yield started
    await started.manager.close()
    store.close()


@pytest.mark.asyncio
async def test_installs_from_two_sessions_take_turns_and_each_is_a_revision(
    worker: Worker,
) -> None:
    # CP-F7: B runs first and installs later; A's install comes in between, and holds its
    # turn until B's install is waiting for it.
    await worker.run("op-0", script="true")
    after_alpha = 'until grep -q "start alpha" "$FAKEPM_LOG"; do sleep 0.05; done'
    b = await worker.submit("op-b", "s2", script=f"{after_alpha}; pip install beta")
    await worker.until(b, "running")
    gate = worker.root.parent / "gate"
    a = await worker.submit("op-a", "s1", script=f"FAKEPM_GATE={gate} pip install alpha")
    await worker.waiting()
    gate.touch()
    first, second = await worker.finish(a), await worker.finish(b)
    assert (first["state"], second["state"]) == ("succeeded", "succeeded"), second["result"]
    assert "waiting for a package command to finish" in second["result"]["stderr"]["text"]
    spans = worker.prefix.spans()
    assert spans["alpha"][1] <= spans["beta"][0]
    history = await worker.revisions()
    assert [entry.revision for entry in history.revisions] == [0, 1, 2]
    zero, one, two = history.revisions
    assert (zero.managed, zero.changed_by, zero.commands) == (None, None, ())
    assert (one.managed, one.changed_by) == (True, first["attempt_id"])
    assert (two.managed, two.changed_by) == (True, second["attempt_id"])
    assert [command.argv for command in one.commands] == [("pip", "install", "alpha")]
    assert [command.argv for command in two.commands] == [("pip", "install", "beta")]
    assert (one.commands[0].via, one.commands[0].exit_code) == ("wrapper", 0)
    # Each with its own lockfile, whichever attempt's end reported them.
    for revision, names in ((1, ["alpha"]), (2, ["alpha", "beta"])):
        lockfile, _ = await worker.lockfile(revision)
        assert [item.name for item in lockfile.pip if item.location == "prefix"] == names
    # Both ran while the packages changed under them.
    assert first["result"]["overlapping_change"] is True
    revisions = (
        second["location"]["environment_revision"],
        second["result"]["environment_revision"],
    )
    assert revisions == ("0", "2")
    assert second["result"]["overlapping_change"] is True


@pytest.mark.asyncio
async def test_new_executions_wait_while_packages_link_and_running_ones_do_not(
    worker: Worker,
) -> None:
    # CP-F7 and CP-12: an execution submitted during an install starts after it.
    await worker.run("op-0", script="true")
    running = await worker.submit("op-running", "s1", script="sleep 1.5; echo done")
    await worker.until(running, "running")
    gate = worker.root.parent / "gate"
    install = await worker.submit(
        "op-install", "s2", script=f"FAKEPM_GATE={gate} pip install gamma"
    )
    await worker.logged("start gamma")
    waiting = await worker.submit(
        "op-waiting", "s3", script='python -c "import time; print(time.time())"'
    )
    await asyncio.sleep(0.5)
    status, queued = await worker.get(f"/v1/executions/{waiting}")
    assert (status, queued["state"]) == (200, "queued")
    gate.touch()
    later = await worker.finish(waiting)
    assert later["state"] == "succeeded", later["result"]
    assert float(later["result"]["stdout"]["text"]) >= worker.prefix.spans()["gamma"][1]
    assert later["location"]["environment_revision"] == "1"
    assert later["result"]["overlapping_change"] is False
    ran = await worker.finish(running)
    assert (ran["state"], ran["result"]["stdout"]["text"]) == ("succeeded", "done\n")
    assert ran["location"]["environment_revision"] == "0"
    # It ended after the install (revision 1), or while its turn still ran (still 0).
    assert ran["result"]["environment_revision"] in ("0", "1")
    assert ran["result"]["overlapping_change"] is True
    assert (await worker.finish(install))["state"] == "succeeded"


@pytest.mark.asyncio
async def test_changes_are_recorded_as_managed_or_unmanaged(worker: Worker) -> None:
    await worker.run("op-0", script="true")
    real_pip = worker.prefix.root / "bin" / "pip"
    bypass = await worker.run("op-bypass", script=f'"{real_pip}" install delta')
    wrapped = await worker.run("op-wrapped", script="pip install epsilon")
    module = await worker.run("op-module", script="python -m pip install zeta")
    conda = await worker.run("op-conda", script="micromamba install -y toolz")
    for execution in (bypass, wrapped, module, conda):
        assert execution["state"] == "succeeded", execution["result"]
    history = await worker.revisions()
    _, unmanaged, by_wrapper, by_module, by_micromamba = history.revisions
    # CP-4: a change no wrapper saw is unmanaged, set against the attempt it followed.
    assert (unmanaged.managed, unmanaged.changed_by, unmanaged.commands) == (
        False,
        bypass["attempt_id"],
        (),
    )
    assert unmanaged.lockfile is not None
    assert (by_wrapper.managed, by_wrapper.changed_by) == (True, wrapped["attempt_id"])
    assert (by_module.managed, by_module.changed_by) == (True, module["attempt_id"])
    [command] = by_module.commands
    assert command.via == "module"
    assert command.argv[1:] == ("-m", "pip", "install", "zeta")
    assert (by_micromamba.managed, by_micromamba.changed_by) == (True, conda["attempt_id"])
    lockfile, data = await worker.lockfile(by_micromamba.revision)
    # CP-18: a package that came through the cache is named by channel, never by the cache.
    [toolz] = lockfile.conda
    assert (toolz.name, toolz.channel, toolz.subdir) == ("toolz", "conda-forge", "noarch")
    assert (toolz.file, toolz.license) == ("toolz-1.0-pyhd8ed1ab_0.conda", "BSD-3-Clause")
    assert toolz.sha256 is not None
    assert CACHE_HOST.encode() not in data
    # The Conda package's own dist-info is not listed again as pip's.
    assert "toolz" not in [item.name for item in lockfile.pip]
    assert by_micromamba.lockfile is not None
    assert by_micromamba.lockfile.conda_packages == 1


@pytest.mark.asyncio
async def test_revision_zero_gets_its_lockfile(worker: Worker) -> None:
    await worker.run("op-0", script="true")
    deadline = time.monotonic() + 30
    while (zero := (await worker.revisions()).revisions[0]).lockfile is None:
        assert time.monotonic() < deadline, "revision 0 never got its lockfile"
        await asyncio.sleep(0.05)
    lockfile, _ = await worker.lockfile(0)
    assert lockfile.python == ".".join(map(str, sys.version_info[:3]))
    assert lockfile.conda == ()
    assert zero.lockfile is not None
    assert zero.lockfile.conda_packages == 0


@pytest.mark.asyncio
async def test_an_execution_waits_no_longer_than_its_time_limit(worker: Worker) -> None:
    await worker.run("op-0", script="true")
    with held(worker.state):
        busy = await worker.run("op-busy", script="echo ran", timeout_seconds=1)
    assert busy["state"] == "timed_out"
    message = busy["result"]["error"]["message"]
    assert re.search(r"waited \d+ s for a package command in this environment to finish", message)
    assert "did not start" in message
    after = await worker.run("op-after", script="echo ran")
    assert after["result"]["stdout"]["text"] == "ran\n"


@pytest.mark.asyncio
async def test_a_cell_waits_for_a_package_command_and_pip_in_a_kernel_takes_its_turn(
    worker: Worker,
) -> None:
    await worker.run("op-0", script="true")
    with held(worker.state):
        cell = await worker.submit("op-cell", kind="cell", code="1 + 1")
        await asyncio.sleep(1)
        status, queued = await worker.get(f"/v1/executions/{cell}")
        assert (status, queued["state"]) == (200, "queued")
    done = await worker.finish(cell)
    assert done["state"] == "succeeded", done["result"]
    assert done["result"]["outputs"][0]["data"]["text/plain"] == "2"
    installed = await worker.run("op-pip", kind="cell", code="%pip install kappa")
    assert installed["state"] == "succeeded", installed["result"]
    last = (await worker.revisions()).revisions[-1]
    assert (last.managed, last.changed_by) == (True, installed["attempt_id"])
    [command] = last.commands
    assert command.argv[1:] == ("-m", "pip", "install", "kappa")
    assert installed["result"]["environment_revision"] == str(last.revision)


@pytest.mark.asyncio
async def test_research_code_that_removes_the_state_breaks_nothing(worker: Worker) -> None:
    await worker.run("op-0", script="true")
    removed = await worker.run("op-rm", script=f'rm -rf "{worker.state.directory}"')
    assert removed["state"] == "succeeded", removed["result"]
    # The next attempt puts the wrappers back, and installs take their turns again.
    again = await worker.run("op-again", script="pip install omega")
    assert again["state"] == "succeeded", again["result"]
    last = (await worker.revisions()).revisions[-1]
    assert (last.managed, last.changed_by) == (True, again["attempt_id"])


@pytest.mark.asyncio
async def test_revision_routes_check_what_they_are_asked(worker: Worker) -> None:
    await worker.run("op-0", script="true")
    for path, expected in (
        ("/v1/environments/env1/revisions/x/lockfile", 400),
        ("/v1/environments/env1/revisions/99/lockfile", 404),
        ("/v1/environments/not%20an%20id/revisions", 400),
        ("/v1/environments/env1/revisions?after=x", 400),
        ("/v1/environments/env1/revisions?after=-2", 400),
        ("/v1/environments/env1/revisions?limit=0", 400),
        ("/v1/environments/env1/revisions?limit=101", 400),
    ):
        status, body = await worker.get(path)
        assert status == expected, (path, body)
    status, body = await worker.get("/v1/environments/never-used/revisions")
    assert (status, body) == (
        200,
        {"environment_id": "never-used", "revisions": [], "next_after": None},
    )


@pytest.mark.asyncio
async def test_a_limited_client_sees_only_its_projects_revisions(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=tmp_path,
    )
    created = datetime.now(UTC)
    info = EnvironmentInfo(
        environment_id="env-a",
        project_id="p1",
        profile="minimal",
        image="image",
        sandbox="ss-0000000000000000",
        policy="sha256:" + "0" * 64,
        state=EnvironmentState.READY,
        created_at=created,
        updated_at=created,
    )

    class Records:
        def get(self, environment_id: str) -> EnvironmentInfo | None:
            return info if environment_id == "env-a" else None

    class Environments:
        def __init__(self) -> None:
            self.records = Records()
            self.deleted: list[Any] = []
            self.required_deleted: list[Any] = []
            self.required_deleting: list[Any] = []
            self.confirmed_deleted: list[Any] = []
            self.reusing: list[Any] = []

    manager.revisions.observe("env-a", "a" * 32)
    clients = [
        ClientSettings(name="mine", token_sha256=token_digest("m" * 40), projects=("p1",)),
        ClientSettings(name="other", token_sha256=token_digest("o" * 40), projects=("p2",)),
    ]
    environments: Any = Environments()
    client = await aiohttp_client(create_app(manager, TOKEN, environments, clients))
    try:
        for token, environment_id, expected in (
            ("m" * 40, "env-a", 200),
            ("o" * 40, "env-a", 404),
            ("o" * 40, "made-by-hand", 404),
            (TOKEN, "made-by-hand", 200),
        ):
            response = await client.get(
                f"/v1/environments/{environment_id}/revisions",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status == expected, (token[0], environment_id)
    finally:
        await manager.close()
        store.close()


class HostileBackend:
    """Starts hostile_executor.py, a stand-in for an executor research code took over."""

    name: Literal["local"] = "local"

    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def prepare(self, environment_id: str) -> None:
        return None

    async def open(
        self, environment_id: str, on_event: EventHandler, on_closed: ClosedHandler
    ) -> ExecutorChannel:
        hostile = Path(__file__).with_name("hostile_executor.py")
        channel = ExecutorChannel(
            [sys.executable, "-I", str(hostile), self.mode], on_event, on_closed
        )
        await channel.start()
        return channel

    def sandbox_id(self, environment_id: str) -> str | None:
        return None

    @property
    def isolation_tier(self) -> int:
        return 0


async def hostile_worker(
    aiohttp_client: ClientFactory, root: Path, mode: str
) -> tuple[Worker, Store]:
    """A worker whose executor is hostile_executor.py in ``mode``."""
    store = Store(root / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        HostileBackend(mode),
        Blobs(root / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=root,
        package_cache=CACHE,
    )
    await manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN))
    prefix = Prefix(root, Path(sys.executable), root, root / "installs.log")  # none runs
    return Worker(client, manager, root, prefix), store


@pytest.mark.asyncio
async def test_revisions_from_a_taken_over_executor_are_bounded_and_checked(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    worker, store = await hostile_worker(aiohttp_client, tmp_path, "revisions")
    try:
        execution = await worker.run("op-hostile", script="true")
        assert execution["state"] == "succeeded"
        revisions = (await worker.revisions()).revisions
        assert len(revisions) == 64  # MAX_REVISIONS for one execution
        # Revision 0 is nobody's change; the others name an attempt of no one's.
        assert all(entry.changed_by is None for entry in revisions)
        # Only the real lockfile is kept, named by its channel.
        assert revisions[0].lockfile is not None
        assert all(entry.lockfile is None for entry in revisions[1:])
        lockfile, data = await worker.lockfile(0)
        assert lockfile.conda[0].channel == "conda-forge"
        assert CACHE_HOST.encode() not in data
        [command] = revisions[1].commands
        assert command.argv == ("pip", "install", "x" * 512)
    finally:
        await worker.manager.close()
        store.close()


# Regression tests for the adversarial review of package commands (2026-10-11).


@pytest.mark.asyncio
async def test_c1_a_lockfile_from_the_executor_is_checked_off_the_event_loop(
    aiohttp_client: ClientFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One revision event whose lockfile has 7,000 Conda entries with channels of 2,047
    # characters in ~1,000 path segments stalled the worker's event loop for ~15 s.
    monkeypatch.setenv("HOSTILE_ENTRIES", "7000")
    worker, store = await hostile_worker(aiohttp_client, tmp_path, "slow_lockfile")
    gaps: list[float] = []

    async def ticker() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.ensure_future(ticker())
    try:
        execution = await worker.run("op-slow", script="true")
        assert execution["state"] == "succeeded"
        [entry] = (await worker.revisions()).revisions
        assert entry.lockfile is None  # over 8 MiB once checked: refused
    finally:
        task.cancel()
        await worker.manager.close()
        store.close()
    assert max(gaps) < 2.0


@pytest.mark.asyncio
async def test_h1_lockfiles_are_kept_one_for_each_attempt_and_the_newest_only(
    aiohttp_client: ClientFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each revision event could store a distinct lockfile of up to 8 MiB, 64 of them an
    # execution, kept forever.
    monkeypatch.setattr(revisions_module, "MAX_LOCKFILES", 3)
    monkeypatch.setenv("HOSTILE_COUNT", "8")
    worker, store = await hostile_worker(aiohttp_client, tmp_path, "lockfiles")
    try:
        for index in range(5):
            execution = await worker.run(f"op-{index}", script="true")
            assert execution["state"] == "succeeded"
        history = (await worker.revisions()).revisions
        assert len(history) == 40
        kept = [entry.lockfile for entry in history if entry.lockfile is not None]
        # Each attempt brought eight; its first was kept, and of those the newest three.
        assert [entry.revision for entry in history if entry.lockfile] == [16, 24, 32]
        stored = (tmp_path / "lockfiles" / "blobs").glob("*/*")
        assert sorted(path.name for path in stored) == sorted(ref.sha256 for ref in kept)
    finally:
        await worker.manager.close()
        store.close()


def test_h1_an_environment_keeps_its_newest_revisions_and_lockfiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(revisions_module, "MAX_HISTORY", 6)
    revisions = Revisions(Store(tmp_path / "worker.sqlite3"), Blobs(tmp_path / "lockfiles"))
    try:
        made: list[Prepared] = []
        for index in range(10):
            prepared = revisions.prepare({"pip": [{"name": f"p{index}", "version": "1.0"}]})
            assert prepared is not None
            made.append(prepared)
        monkeypatch.setattr(
            revisions_module, "MAX_LOCKFILE_STORE", 2 * max(item.ref.size for item in made)
        )
        for index, prepared in enumerate(made):
            revisions.observe("env1", f"{index + 1:032x}", lockfile=prepared)
        history = revisions.history("env1").revisions
        assert [entry.revision for entry in history] == [4, 5, 6, 7, 8, 9]
        assert [entry.revision for entry in history if entry.lockfile] == [8, 9]
        stored = (tmp_path / "lockfiles" / "blobs").glob("*/*")
        assert sorted(path.name for path in stored) == sorted(
            prepared.ref.sha256 for prepared in made[8:]
        )
        # A lockfile no revision takes is not kept either.
        spare = revisions.prepare({"pip": [{"name": "spare", "version": "1.0"}]})
        assert spare is not None
        assert not revisions.attach("env1", "f" * 32, spare)
        assert not revisions.blobs.path(spare.ref.sha256).exists()
        # Pages, oldest first.
        first = revisions.history("env1", limit=4)
        assert ([entry.revision for entry in first.revisions], first.next_after) == (
            [4, 5, 6, 7],
            7,
        )
        rest = revisions.history("env1", after=7, limit=4)
        assert ([entry.revision for entry in rest.revisions], rest.next_after) == ([8, 9], None)
    finally:
        revisions.store.close()


@pytest.mark.asyncio
async def test_h1_the_history_is_bounded_and_served_in_pages(
    aiohttp_client: ClientFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Revisions with long command lines, 64 an execution, grew the history by ~4 MiB an
    # execution, all of it served by one response.
    monkeypatch.setattr(revisions_module, "MAX_HISTORY", 100)
    monkeypatch.setenv("HOSTILE_COUNT", "64")
    worker, store = await hostile_worker(aiohttp_client, tmp_path, "big_commands")
    try:
        for index in range(3):
            execution = await worker.run(f"op-cmd-{index}", script="true")
            assert execution["state"] == "succeeded"
        seen: list[int] = []
        sizes: list[int] = []
        after = -1
        while True:
            page, size = await worker.page(after)
            sizes.append(size)
            seen += [entry.revision for entry in page.revisions]
            for entry in page.revisions:
                for command in entry.commands:
                    assert sum(map(len, command.argv[:-1])) <= 4096
                    assert command.argv[-1] == "[… 32 more arguments]"
            if page.next_after is None:
                break
            after = page.next_after
        assert seen == list(range(92, 192))  # the newest 100 of 192
        assert len(sizes) > 1
        assert max(sizes) <= MAX_REVISION_PAGE_BYTES
    finally:
        await worker.manager.close()
        store.close()


CACHE_FORMS = [
    # The cache's address, otherwise than as the cache lays out its channels and files.
    {"conda": [{"channel": f"{CACHE_HOST}:8090/conda/conda-forge"}]},
    {"conda": [{"channel": f"http://{CACHE_HOST}:99999/conda/conda-forge"}]},
    {"conda": [{"channel": f"http://{CACHE_HOST}:8090/pypi/simple"}]},
    {"conda": [{"channel": f"HTTP://{CACHE_HOST.upper()}:8091/conda/conda-forge"}]},
    {"conda": [{"channel": "conda-forge", "file": f"{CACHE}/conda/conda-forge/noarch/d.conda"}]},
    {"pip": [{"url": f"{CACHE}/pypi/simple/e/e-1.tar.gz"}]},
    {"pip": [{"url": f"{CACHE}/pypi/files/f/{'e' * 64}"}]},
    # Line breaks and other controls.
    {"conda": [{"file": "numpy-2.1.0-0.conda\nhttps://evil.example/payload-1.0-0.conda"}]},
    {"conda": [{"channel": "conda-forge\n@EXPLICIT"}]},
    {"conda": [{"name": "safe\u202egnp.exe"}]},
    {"pip": [{"url": "https://files.pythonhosted.org/x.whl\r\nhttps://evil.example/x.whl"}]},
    # Look-alike hosts, as they read (review M2).
    {"conda": [{"channel": "https://c\u043enda.anaconda.org/conda-forge"}]},
    {"pip": [{"url": "https://files.pythonh\u043ested.org/packages/ab/cd/numpy.whl"}]},
    # URLs that do not parse.
    {"conda": [{"channel": "http://[::1/conda-forge"}]},
]


@pytest.mark.parametrize("forged", CACHE_FORMS)
def test_m1_a_lockfile_that_names_the_cache_or_holds_controls_is_refused(
    tmp_path: Path, forged: dict[str, list[dict[str, str]]]
) -> None:
    entries = {
        "conda": {"name": "numpy", "version": "2.1.0", "build": "0"},
        "pip": {"name": "numpy", "version": "2.1.0"},
    }
    lockfile = {
        section: [{**entries[section], **fields} for fields in items]
        for section, items in forged.items()
    }
    revisions = Revisions(
        Store(tmp_path / "worker.sqlite3"), Blobs(tmp_path / "lockfiles"), cache=CACHE
    )
    try:
        assert revisions.prepare(lockfile) is None
        assert list((tmp_path / "lockfiles" / "blobs").glob("*/*")) == []
    finally:
        revisions.store.close()


def test_m2_only_the_cache_and_conda_anaconda_org_are_named_upstream(tmp_path: Path) -> None:
    # A forged or a real package from any host laid out like the cache read as conda-forge's
    # or PyPI's, and a look-alike host as the real one.
    meta = tmp_path / "conda-meta"
    meta.mkdir()
    numpy = f"https://attacker.example/conda/conda-forge/linux-64/{NUMPY}"
    look_alike = "https://c\u043enda.anaconda.org/conda-forge/noarch/toolz-1.0-0.conda"
    _meta(meta, "numpy", {"name": "numpy", "version": "2.1.0", "build": "0", "url": numpy})
    _meta(meta, "toolz", {"name": "toolz", "version": "1.0", "build": "0", "url": look_alike})
    made = provisioning.lockfile(provisioning.Places(str(meta), ()))
    channels = {item["name"]: item["channel"] for item in made["conda"]}
    ascii_host = "c\u043enda.anaconda.org".encode("idna").decode()
    assert ascii_host.startswith("xn--")
    assert channels == {
        "numpy": "https://attacker.example/conda/conda-forge",
        "toolz": f"https://{ascii_host}/conda-forge",
    }
    wheel = (
        f"https://attacker.example/pypi/files/numpy/{'9' * 64}/aa/bb/numpy-2.1.0-py3-none-any.whl"
    )
    assert provisioning.canonical_url(wheel, CACHE) == (wheel, None)
    elsewhere = "https://attacker.example/x/conda/conda-forge"
    assert provisioning.canonical_channel(elsewhere, CACHE) == elsewhere
    # The cache's own, at its own host and port only, and conda.anaconda.org over HTTPS.
    assert provisioning.canonical_channel(f"{CACHE}/conda/conda-forge", CACHE) == "conda-forge"
    other_port = f"http://{CACHE_HOST}:8091/conda/conda-forge"
    assert provisioning.canonical_channel(other_port, CACHE) == other_port
    plain = "http://conda.anaconda.org/conda-forge"
    assert provisioning.canonical_channel(plain, CACHE) == plain
    file = f"{CACHE}/pypi/files/numpy/{'9' * 64}/aa/bb/numpy-2.1.0-py3-none-any.whl"
    assert provisioning.canonical_url(file, CACHE) == (
        "https://files.pythonhosted.org/aa/bb/numpy-2.1.0-py3-none-any.whl",
        "9" * 64,
    )


def test_m4_a_wrapper_killed_with_sigkill_takes_its_command_along(sandbox: Sandbox) -> None:
    # Only the wrapper held the lock: killed, it left the real command running outside
    # any turn, and the next install ran beside it.
    gate = sandbox.prefix.root.parent / "gate"
    wrapper = sandbox.start("pip", "install", "alpha", FAKEPM_GATE=str(gate))
    command = sandbox.prefix.pid("start", "alpha")
    assert command != wrapper.pid
    assert alive(command)
    wrapper.kill()
    assert wrapper.wait(30) == -signal.SIGKILL
    deadline = time.monotonic() + 10
    while alive(command):
        assert time.monotonic() < deadline, "the command outlived its wrapper"
        time.sleep(0.02)
    gate.touch()
    with held(sandbox.state):  # the turn ended with both
        time.sleep(0.2)
    assert not (sandbox.prefix.site / "alpha-1.0.dist-info").exists()
    assert sandbox.records() == []


def test_m5_the_executor_signals_a_wrapped_command_through_its_wrapper_only(
    sandbox: Sandbox,
) -> None:
    # The executor sent SIGTERM to each process of a call, the wrapper passed its own on,
    # and the command got two: the second can cut short the clean-up the first began.
    wrapper = sandbox.start("pip", "install", "counting")
    command = sandbox.prefix.pid("ready", "counting")
    table = executor_module.process_table()
    found = [table[wrapper.pid], table[command]]
    chosen = without_wrapped(found)
    assert [info.pid for info in chosen] == [wrapper.pid]
    signal_processes(chosen, signal.SIGTERM)
    assert wrapper.wait(30) == 143
    lines = sandbox.prefix.log.read_text().splitlines()
    assert lines.count("got TERM") == 1


@pytest.mark.asyncio
async def test_m5_a_timed_out_install_gets_one_sigterm(worker: Worker) -> None:
    await worker.run("op-0", script="true")
    done = await worker.run("op-t", script="exec pip install counting", timeout_seconds=2)
    assert done["state"] == "timed_out", done["result"]
    lines = worker.prefix.log.read_text().splitlines()
    assert lines.count("got TERM") == 1


@pytest.mark.asyncio
async def test_m6_many_installs_in_one_call_are_one_revision_with_its_lockfile(
    worker: Worker,
) -> None:
    # Past 64 package commands in one call, the records were lost, and so was the
    # lockfile of the state the call left.
    await worker.run("op-0", script="true")
    script = "for i in $(seq 1 70); do pip install pkg$i >/dev/null || exit 1; done"
    loop = await worker.run("op-loop", script=script, timeout_seconds=300)
    assert loop["state"] == "succeeded", loop["result"]
    history = (await worker.revisions()).revisions
    assert [entry.revision for entry in history] == [0, 1]
    last = history[-1]
    assert (last.managed, last.changed_by) == (True, loop["attempt_id"])
    assert [command.argv[2] for command in last.commands] == [f"pkg{i}" for i in range(63, 71)]
    assert last.omitted_commands == 62
    assert last.lockfile is not None
    lockfile, _ = await worker.lockfile(1)
    assert {item.name for item in lockfile.pip} == {f"pkg{i}" for i in range(1, 71)}


class Sent:
    """What an executor sends, kept."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def revisions(self) -> list[dict[str, Any]]:
        return [message for message in self.messages if message.get("event") == "revision"]


def bare_executor(tmp_path: Path) -> tuple[Executor, Sent, provisioning.Config]:
    """An executor whose environment is an empty ``conda-meta`` and site-packages, and the
    configuration its package commands record turns with."""
    sent = Sent()
    executor = Executor(tmp_path / "project", sent)
    assert executor.provisioning is not None
    meta, site = tmp_path / "conda-meta", tmp_path / "site"
    meta.mkdir()
    site.mkdir()
    places = provisioning.Places(str(meta), (str(site),))
    executor.places = places
    executor.reported = provisioning.baseline(executor.packages, places)
    python, script = executor.provisioning.python, executor.provisioning.script
    return executor, sent, provisioning.Config(python, script, str(tmp_path), places)


@pytest.mark.asyncio
async def test_m6_records_beyond_one_look_come_in_order_and_an_attempts_make_one_change(
    tmp_path: Path,
) -> None:
    executor, sent, _ = bare_executor(tmp_path)
    executor.own("call-x", "exe-x")
    fingerprints = [executor.reported, *(f"{index:032x}" for index in range(1, 301))]
    for index in range(300):
        record = {
            "version": provisioning.RECORD_VERSION,
            "command": ["pip", "install", f"pkg{index}"],
            "via": "wrapper",
            "owner": "call-x",
            "invoked": time.time(),
            "exit_code": 0,
            "before": fingerprints[index],
            "after": fingerprints[index + 1],
            "lockfile": {"pip": [{"name": f"pkg{index}", "version": "1.0"}]},
        }
        name = f"{index + 1:020d}-1.json"  # as the commands ended, one after another
        (executor.packages.journal / name).write_text(json.dumps(record))
    await executor.begin(Running(execution="exe-next", kind="shell"), 30)
    first, second, last = sent.revisions()
    # A look reads 256 records; the next, the rest; and then the packages themselves.
    assert (first["managed"], first["cause"], first["fingerprint"]) == (
        True,
        "exe-x",
        fingerprints[256],
    )
    assert [command["command"][2] for command in first["commands"]] == [
        f"pkg{index}" for index in range(248, 256)
    ]
    assert first["omitted_commands"] == 248
    assert first["lockfile"]["pip"][0]["name"] == "pkg255"
    assert (second["fingerprint"], second["omitted_commands"]) == (fingerprints[300], 36)
    assert second["lockfile"]["pip"][0]["name"] == "pkg299"
    assert (last["managed"], last["cause"], last["fingerprint"]) == (False, None, fingerprints[0])
    assert list(executor.packages.journal.glob("*.json")) == []


@pytest.mark.asyncio
async def test_l1_a_record_stays_in_the_journal_until_the_worker_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cell cancelled while it waited in the queue, as its start read the journal, lost
    # the record: the change was then reported as unmanaged.
    executor, sent, config = bare_executor(tmp_path)
    turn = provisioning.Turn(executor.packages, config, ["pip", "install", "alpha"], "wrapper")
    assert turn.begin()
    _dist(Path(config.places.site[0]), "alpha", "1.0", "pip")
    turn.end(0)
    release = threading.Event()
    real = provisioning.observe

    def slow_observe(*args: Any, **kwargs: Any) -> provisioning.Observation:
        observed = real(*args, **kwargs)
        release.wait(30)  # a large environment's look takes a while
        return observed

    monkeypatch.setattr(executor_module.provisioning, "observe", slow_observe)
    queued = Running(execution="exe-cancelled", kind="cell", queued=True)
    task = asyncio.ensure_future(executor.begin(queued, 30))
    while not executor.observing.locked():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    task.cancel()  # what op_cancel does for a queued cell
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    monkeypatch.setattr(executor_module.provisioning, "observe", real)
    assert len(list(executor.packages.journal.glob("*.json"))) == 1
    await executor.begin(Running(execution="exe-next", kind="shell"), 30)
    [event] = sent.revisions()
    assert event["managed"] is True
    assert [command["command"] for command in event["commands"]] == [["pip", "install", "alpha"]]
    assert list(executor.packages.journal.glob("*.json")) == []


@pytest.mark.asyncio
async def test_l2_a_fifo_among_the_package_records_holds_nothing_up(tmp_path: Path) -> None:
    # A FIFO in conda-meta blocked the look at the packages, and every attempt with it.
    executor, sent, config = bare_executor(tmp_path)
    running = Running(execution="exe-a", kind="shell")
    await executor.begin(running, 60)
    info = Path(config.places.site[0]) / "evil-1.0.dist-info"
    info.mkdir()
    fifos = [
        Path(config.places.conda_meta) / "evil-1.0-0.json",
        info / "METADATA",
        info / "INSTALLER",
        executor.packages.journal / f"{1:020d}-1.json",
    ]
    for fifo in fifos:
        os.mkfifo(fifo)
    task = asyncio.ensure_future(executor.settle(running))
    try:
        done, _ = await asyncio.wait({task}, timeout=10)
        assert done, "the look at the packages waits for a FIFO"
    finally:
        # A reader that blocked would keep this process from ending: let each one go.
        deadline = time.monotonic() + 30
        while not task.done() and time.monotonic() < deadline:
            for fifo in fifos:
                with contextlib.suppress(OSError):
                    os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            await asyncio.sleep(0.05)
    settled = task.result()
    [event] = sent.revisions()
    assert (event["managed"], event["cause"]) == (False, "exe-a")
    assert event["lockfile"]["skipped"] == 1  # the conda-meta record
    assert [item["name"] for item in event["lockfile"]["pip"]] == ["evil"]  # by its directory
    assert settled["environment"] == event["fingerprint"]


def test_l3_scripts_find_the_environment_beside_its_package_managers(sandbox: Sandbox) -> None:
    # `command -v conda` names the wrapper: what scripts look for beside it must be there.
    root = sandbox.prefix.root
    conda = root / "bin" / "conda"
    conda.write_text("#!/bin/sh\nexit 0\n")
    conda.chmod(0o755)
    (root / "etc" / "profile.d").mkdir(parents=True)
    (root / "etc" / "profile.d" / "conda.sh").write_text("CONDA_SOURCED=1\n")
    path = hermetic_path(sandbox.prefix, root.parent)
    assert "conda" in provisioning.refresh(sandbox.state, sandbox.config, path)
    script = (
        '. "$(dirname "$(command -v conda)")/../etc/profile.d/conda.sh" && echo "$CONDA_SOURCED"'
        ' && "$(dirname "$(command -v pip)")/python" -c "import sys; print(sys.prefix)"'
        ' && "$(dirname "$(command -v pip)")/pip" install sigma'
    )
    env = {**sandbox.env, "PATH": f"{sandbox.state.bin}:{path}:/usr/bin:/bin"}
    result = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", script], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["1", str(root)]
    [record] = sandbox.records()  # pip beside the wrappers is the wrapper
    assert record["command"] == ["pip", "install", "sigma"]


def test_l4_a_command_ended_by_a_signal_ends_its_wrapper_alike(sandbox: Sandbox) -> None:
    real = sandbox.prefix.root / "bin" / "pip"
    direct = sandbox.run(str(real), "install", "killed")
    wrapped = sandbox.run("pip", "install", "killed")
    assert direct.returncode == wrapped.returncode == -signal.SIGKILL
    [record] = sandbox.records()
    assert (record["exit_code"], record["signal"]) == (None, signal.SIGKILL)


@pytest.mark.parametrize(
    ("argument", "kept"),
    [
        (
            "--index-url=https://user:test-secret-a@private.example/simple",
            "--index-url=https://***@private.example/simple",
        ),
        ("https://user:p@ss@host.example/simple", "https://***@host.example/simple"),
        (
            "https://host.example/simple?token=test-secret-b&x=1",
            "https://host.example/simple?token=***&x=1",
        ),
        ("--password=test-secret-c", "--password=***"),
        ("git+ssh://git@github.com/o/r.git", "git+ssh://***@github.com/o/r.git"),
        ("numpy>=2", "numpy>=2"),
    ],
)
def test_l5_credentials_on_a_command_line_are_redacted(argument: str, kept: str) -> None:
    assert provisioning.redact(argument) == kept


def test_l5_a_recorded_command_line_is_redacted_bounded_and_printable() -> None:
    bounded = provisioning.bounded_command
    assert bounded(["pip", "install", "--token", "test-secret-d", "x"]) == [
        "pip",
        "install",
        "--token",
        "***",
        "x",
    ]
    # Cut, the user information of a URL goes too, though its "@" was cut off.
    long = "https://user:test-secret-e" + "s" * 5000 + "@host.example/simple"
    assert bounded(["pip", "install", "--index-url", long]) == [
        "pip",
        "install",
        "--index-url",
        "https://***",
    ]
    words = bounded(["pip", "install", *(f"p{index}" for index in range(100))])
    assert (len(words), words[-1]) == (33, "[… 70 more arguments]")
    assert bounded(words) == words
    assert bounded(["a\x1b[2Jb"]) == ["a?[2Jb"]
    started = time.monotonic()
    bounded(["--" + "token" * 10_000 + "!", "x://" * 100_000, "?" + "a" * 1_000_000])
    assert time.monotonic() - started < 1  # no pattern backtracks over an argument


@pytest.mark.asyncio
async def test_l5_index_credentials_are_never_stored(worker: Worker) -> None:
    await worker.run("op-0", script="true")
    credential = "test-secret-index-token"
    script = f"pip install --index-url=https://user:{credential}@private.example/simple alpha"
    done = await worker.run("op-secret", script=script)
    assert done["state"] == "succeeded", done["result"]
    status, body = await worker.get("/v1/environments/env1/revisions")
    assert status == 200
    assert credential not in json.dumps(body)
    [command] = RevisionHistory.model_validate(body).revisions[-1].commands
    assert command.argv == (
        "pip",
        "install",
        "--index-url=https://***@private.example/simple",
        "alpha",
    )
    with contextlib.closing(sqlite3.connect(worker.root / "worker.sqlite3")) as database:
        rows = database.execute("SELECT record FROM revision_entries").fetchall()
    assert rows
    assert all(credential not in row[0] for row in rows)


# Regression tests for the second review of package commands (2026-10-11). Each names
# the finding it covers (review2: M1 to M3, L1 to L6) and repeats its probe.


def _logged_pids(log: Path, name: str, timeout: float = 30) -> tuple[int, int]:
    """The command's and its step's pids, as the fake manager logs them for ``name``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in log.read_text().splitlines() if log.exists() else []:
            words = line.split()
            if words[:2] == ["ready", name] and len(words) == 4:
                return int(words[2]), int(words[3])
        time.sleep(0.02)
    raise AssertionError(f"ready {name} never appeared in the log")


def _until_gone(pid: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while alive(pid):
        assert time.monotonic() < deadline, f"process {pid} is still running"
        time.sleep(0.02)


def test_review2_m2_the_prefixs_programs_are_beside_the_python_on_path(
    sandbox: Sandbox,
) -> None:
    # `command -v python` names the stand-in beside the wrappers: the prefix's other
    # programs must be there too, as they are beside the prefix's own Python.
    root = sandbox.prefix.root
    for tool in ("jupyter", "gp"):
        program = root / "bin" / tool
        program.write_text(f"#!/bin/sh\necho ran-{tool}\n")
        program.chmod(0o755)
    path = hermetic_path(sandbox.prefix, root.parent) + ":/usr/bin:/bin"
    provisioning.refresh(sandbox.state, sandbox.config, path)
    env = {**sandbox.env, "PATH": f"{sandbox.state.bin}:{path}"}

    def shell(script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", script], env=env, capture_output=True, text=True, check=False
        )

    found = shell(
        '"$(dirname "$(command -v python)")/jupyter"'
        ' && "$(dirname "$(command -v python3)")/gp"'
        ' && "$(dirname "$(command -v pip)")/python" -c "import sys; print(sys.executable)"'
    )
    assert found.returncode == 0, found.stderr
    assert found.stdout.split() == ["ran-jupyter", "ran-gp", str(sandbox.prefix.python)]
    # A program a package command installs is there as soon as the command ends.
    installed = shell('pip install withscript && "$(dirname "$(command -v python)")/withscript"')
    assert installed.returncode == 0, installed.stderr
    assert installed.stdout.split() == ["ran-withscript"]
    # A program that goes loses its link.
    (root / "bin" / "gp").unlink()
    provisioning.refresh(sandbox.state, sandbox.config, path)
    assert not os.path.lexists(sandbox.state.bin / "gp")
    assert (sandbox.state.bin / "pip").read_text().startswith("#!/bin/sh")  # still the wrapper


@pytest.mark.parametrize(
    "lockfile",
    [
        {f"k{index:x}": 0 for index in range(100)},  # keys no lockfile has
        {"conda": [{"name": "numpy", "version": "1", "build": "0", "extra": 1}]},
        {"conda": [{f"k{index}": 0 for index in range(20)}]},  # more keys than an entry has
        {"conda": ["numpy-1-0"]},  # an entry that is no object
        {"pip": [{"name": "x", "version": "1"}] * 20_001},  # more entries than a section has
    ],
)
def test_a_lockfile_of_another_shape_is_refused(tmp_path: Path, lockfile: dict[str, Any]) -> None:
    revisions = Revisions(Store(tmp_path / "worker.sqlite3"), Blobs(tmp_path / "lockfiles"))
    try:
        assert revisions.prepare(lockfile) is None
        assert revisions.prepare({"conda": [{"name": "numpy", "version": "1", "build": "0"}]})
    finally:
        revisions.store.close()


MEASURE = """
import resource, sys, tempfile
from pathlib import Path
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.channel import decode_line
from sandbox_science.worker.revisions import Revisions
from sandbox_science.worker.store import Store
d = Path(tempfile.mkdtemp(dir=sys.argv[1]))
revisions = Revisions(Store(d / "w.sqlite3"), Blobs(d / "lockfiles"))
keys = int(sys.argv[2])
body = ",".join(f'"{i:x}":0' for i in range(keys))
line = ('{"type":"event","lockfile":{' + body + "}}").encode()
message = decode_line(line)
size = len(line)
del line
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
assert revisions.prepare(message["lockfile"]) is None
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(size, (after - before) * 1024)
"""


def test_review2_m3_refusing_a_lockfile_of_unknown_keys_takes_no_memory(tmp_path: Path) -> None:
    # 1.5 million unknown keys in one object (14.7 MiB, as the channel decodes it): the
    # check took 873 MiB, building and formatting an error for each.
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", MEASURE, str(tmp_path), "1500000"],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    size, grown = (int(word) for word in done.stdout.split())
    assert grown < size // 4


async def _longest_stall(action: Callable[[], Awaitable[Any]]) -> tuple[float, Any]:
    """The longest the event loop waited while ``action`` ran, and its result."""
    gaps: list[float] = []
    done = False

    async def ticker() -> None:
        last = time.monotonic()
        while not done:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.ensure_future(ticker())
    await asyncio.sleep(0.05)
    result = await action()
    done = True
    await task
    return max(gaps), result


async def _check_lockfile_responsiveness(tmp_path: Path) -> None:
    # pydantic held the GIL for 0.6 s while it built an error for each of 1.5 million
    # unknown keys; a lockfile of the largest shape is checked an entry at a time.
    revisions = Revisions(Store(tmp_path / "worker.sqlite3"), Blobs(tmp_path / "lockfiles"))
    unknown = {f"{index:x}": 0 for index in range(1_500_000)}
    largest = {
        "conda": [
            {
                "name": f"n{index}" + "a" * 200,
                "version": "1.0",
                "build": "0",
                "channel": "conda-forge",
                "subdir": "noarch",
                "license": "L" * 200,
            }
            for index in range(20_000)
        ]
    }
    try:
        for lockfile in (unknown, largest):
            worst, prepared = await _longest_stall(
                lambda lockfile=lockfile: asyncio.to_thread(revisions.prepare, lockfile)
            )
            assert prepared is None  # unknown keys; over 8 MiB
            assert worst < 0.25, worst
    finally:
        revisions.store.close()


def test_review2_l1_checking_a_lockfile_in_a_thread_holds_the_interpreter_briefly(
    tmp_path: Path,
) -> None:
    # Keep the original 0.25 s bound and hostile inputs, but measure in a fresh
    # interpreter: an unrelated full-suite heap collection must not be attributed
    # to the lockfile validator. Deterministic byte/count/shape tests also run here.
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import asyncio,runpy,sys; from pathlib import Path; "
            "scope=runpy.run_path(sys.argv[1]); "
            "asyncio.run(scope['_check_lockfile_responsiveness'](Path(sys.argv[2])))",
            str(Path(__file__).resolve()),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.asyncio
async def test_review2_l1_an_execution_gets_at_most_four_lockfiles_checked(
    aiohttp_client: ClientFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The requests an attempt's start and end made were not counted: five a run.
    monkeypatch.setenv("HOSTILE_ENTRIES", "1000")
    worker, store = await hostile_worker(aiohttp_client, tmp_path, "lockfile_requests")
    checked: list[float] = []
    real = worker.manager.revisions.prepare

    def counting(raw: object) -> Prepared | None:
        checked.append(time.monotonic())
        return real(raw)

    monkeypatch.setattr(worker.manager.revisions, "prepare", counting)
    try:
        counts: list[int] = []
        for index in range(6):
            before = len(checked)
            execution = await worker.run(f"op-{index}", script="true")
            assert execution["state"] == "succeeded"
            await asyncio.sleep(0.3)  # what its end asked for
            counts.append(len(checked) - before)
    finally:
        await worker.manager.close()
        store.close()
    assert max(counts) <= 4, counts
    assert min(counts) >= 1, counts  # the budget is spent, not withheld


def test_review2_l2_the_cache_is_known_by_its_host_in_one_spelling(tmp_path: Path) -> None:
    def revisions(cache: str, name: str) -> Revisions:
        return Revisions(
            Store(tmp_path / f"{name}.sqlite3"), Blobs(tmp_path / f"lockfiles-{name}"), cache=cache
        )

    def conda(channel: str) -> dict[str, Any]:
        return {"conda": [{"name": "numpy", "version": "1", "build": "0", "channel": channel}]}

    # A cache URL without a port is no text to search for: honest lockfiles pass.
    honest = {"pip": [{"name": "cachetools", "version": "5.3.3", "installer": "pip"}]}
    assert revisions("http://cache", "a").prepare(honest) is not None
    assert revisions("http://conda", "b").prepare(conda("conda-forge")) is not None
    # The cache's host however spelled: renamed as the cache's, or refused.
    named = revisions(CACHE, "c")
    renamed = named.prepare(conda(f"http://{CACHE_HOST.upper()}.:8090/conda/conda-forge"))
    assert renamed is not None
    assert Lockfile.model_validate_json(renamed.data).conda[0].channel == "conda-forge"
    assert named.prepare(conda(f"http://{CACHE_HOST}.:8090/pypi/simple")) is None
    pip = {"pip": [{"name": "y", "version": "1", "url": f"http://{CACHE_HOST}.:8090/y.tar.gz"}]}
    assert named.prepare(pip) is None
    by_address = revisions("http://10.0.0.5:8090", "d")
    for spelling in ("167772165", "0xa.0.0.5", "012.0.0.5", "[::ffff:10.0.0.5]"):
        assert by_address.prepare(conda(f"http://{spelling}:8090/pypi/simple")) is None, spelling
        renamed = by_address.prepare(conda(f"http://{spelling}:8090/conda/conda-forge"))
        assert renamed is not None, spelling
        assert b"conda-forge" in renamed.data
        assert b"10.0.0.5" not in renamed.data


@pytest.mark.parametrize(
    ("argument", "kept"),
    [
        (
            "--channel=https://conda.anaconda.org/t/test-secret-anaconda-token/private",
            "--channel=https://conda.anaconda.org/t/***/private",
        ),
        (
            "--extra-index-url=https://dl.cloudsmith.io/test-secret-entitlement/org/repo/python/simple/",
            "--extra-index-url=https://dl.cloudsmith.io/***/org/repo/python/simple/",
        ),
        (
            "--index-url=https://pypi.fury.io/test-secret-fury-token/someone/",
            "--index-url=https://pypi.fury.io/***/someone/",
        ),
        (
            "--index-url=https://user:test-secret#pw@private.example/simple",
            "--index-url=https://***@private.example/simple",
        ),
        (
            "--index-url=https://user:test-secret/pw@private.example/simple",
            "--index-url=https://***@private.example/simple",
        ),
        (
            "--index-url=https://user:test-secret?pw@private.example/simple",
            "--index-url=https://***@private.example/simple",
        ),
    ],
)
def test_review2_l3_credentials_in_a_urls_path_are_redacted(argument: str, kept: str) -> None:
    assert provisioning.redact(argument) == kept
    assert provisioning.bounded_command(["conda", argument]) == ["conda", kept]


@pytest.mark.parametrize(
    "argument",
    [
        "https://dl.cloudsmith.io/public/org/repo/python/simple/",
        "https://pypi.fury.io/someone/",
        "git+https://github.com/o/r.git@release/1.0#subdirectory=pkg/sub",
        "git+https://github.com/o/r.git@main",
        "https://host.example:8080/path@x/y",
        "numpy>=2.1",
    ],
)
def test_redaction_keeps_what_is_no_credential(argument: str) -> None:
    assert provisioning.redact(argument) == argument


@pytest.mark.asyncio
async def test_review2_l3_an_anaconda_token_never_reaches_the_history(worker: Worker) -> None:
    await worker.run("op-0", script="true")
    channel = "--channel=https://conda.anaconda.org/t/test-secret-anaconda-token/private"
    done = await worker.run("op-token", script=f"micromamba install -y {channel} toolz")
    assert done["state"] == "succeeded", done["result"]
    status, body = await worker.get("/v1/environments/env1/revisions")
    assert status == 200
    assert "test-secret-anaconda-token" not in json.dumps(body)
    [command] = RevisionHistory.model_validate(body).revisions[-1].commands
    assert "--channel=https://conda.anaconda.org/t/***/private" in command.argv


def test_review2_l4_what_a_killed_wrapper_left_is_ended_before_the_next_turn(
    sandbox: Sandbox,
) -> None:
    # The death signal ends the command with its wrapper, not what the command started:
    # a post-link script or a build backend went on writing outside any turn.
    wrapper = sandbox.start("pip", "install", "spawning")
    command, step = _logged_pids(sandbox.prefix.log, "spawning")
    wrapper.kill()
    assert wrapper.wait(30) == -signal.SIGKILL
    _until_gone(command)
    assert alive(step)
    second = sandbox.run("pip", "install", "other")
    assert second.returncode == 0, second.stderr
    assert "ended 1 processes that a killed package command left running" in second.stderr
    assert not alive(step)
    assert not (sandbox.prefix.site / "spawning-step-1.0.dist-info").exists()
    assert [record["command"] for record in sandbox.records()] == [["pip", "install", "other"]]


def test_review2_l4_the_executors_look_ends_what_a_killed_wrapper_left(
    sandbox: Sandbox,
) -> None:
    # In a kernel, no other turn may come: the executor's look before the next attempt
    # ends the steps.
    wrapper = sandbox.start("pip", "install", "spawning")
    command, step = _logged_pids(sandbox.prefix.log, "spawning")
    wrapper.kill()
    wrapper.wait(30)
    _until_gone(command)
    known = provisioning.fingerprint(sandbox.config.places)
    provisioning.observe(sandbox.state, sandbox.config.places, known)
    assert not alive(step)
    assert not sandbox.state.holder.exists()


def test_cleanup_waits_for_exit_after_the_process_mark_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans = iter([[(123, 4)], []])
    pending = [True]
    signals: list[tuple[int, int]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(provisioning, "_marked", lambda _: next(scans))
    monkeypatch.setattr(provisioning, "process_start", lambda _: 4)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(provisioning, "_same_running", lambda pid, start: pending[0])

    def finish_exit(delay: float) -> None:
        sleeps.append(delay)
        pending[0] = False

    monkeypatch.setattr(time, "sleep", finish_exit)
    assert provisioning.end_marked("123:4") == 1
    assert signals == [(123, signal.SIGKILL)]
    assert sleeps == [0.01]


@pytest.mark.parametrize(
    ("error", "running"),
    [(FileNotFoundError(), False), (PermissionError(), True), (OSError(), True)],
)
def test_cleanup_exit_check_distinguishes_missing_from_unreadable_process(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    running: bool,
) -> None:
    def unreadable(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(provisioning, "open", unreadable, raising=False)
    assert provisioning._same_running(123, 4) is running


def test_cleanup_exit_check_cannot_confirm_exit_from_a_partial_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provisioning, "open", lambda *_: io.BytesIO(b"123 (process)"), raising=False
    )
    assert provisioning._same_running(123, 4)


@pytest.mark.parametrize("case", ["pending_exit", "denied_signal", "last_round_child"])
def test_cleanup_cannot_report_success_while_a_package_process_remains_running(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    scans = iter(
        [[(123, 4)]] * provisioning.KILL_ROUNDS + [[(456, 4)]]
        if case == "last_round_child"
        else [[(123, 4)], []]
    )
    clock = [0.0]
    monkeypatch.setattr(provisioning, "_marked", lambda _: next(scans))
    monkeypatch.setattr(provisioning, "process_start", lambda _: 4)

    def kill(*_: object) -> None:
        if case == "denied_signal":
            raise PermissionError("denied")

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(
        provisioning, "_same_running", lambda pid, start: case != "last_round_child" or pid == 456
    )
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    with pytest.raises(RuntimeError, match="has not exited"):
        provisioning.end_marked("123:4")
    assert clock[0] >= provisioning.KILL_WAIT


def test_failed_cleanup_at_turn_start_preserves_holder_and_releases_lock(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox.state.holder.write_text('{"pid":123,"start":4}')

    def pending(_: object) -> int:
        raise provisioning.CleanupPendingError("pending")

    monkeypatch.setattr(provisioning, "end_strays", pending)
    turn = provisioning.Turn(sandbox.state, sandbox.config, ["pip", "install", "x"], "wrapper")
    with pytest.raises(provisioning.CleanupPendingError):
        turn.begin()
    assert turn.descriptor is None
    assert sandbox.state.holder.exists()
    with held(sandbox.state):
        pass


def test_failed_cleanup_at_turn_end_preserves_holder_and_releases_lock(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = provisioning.Turn(sandbox.state, sandbox.config, ["pip", "install", "x"], "wrapper")
    assert turn.begin()

    def pending(*_: object) -> int:
        raise provisioning.CleanupPendingError("pending")

    monkeypatch.setattr(provisioning, "end_marked", pending)
    with pytest.raises(provisioning.CleanupPendingError):
        turn.end(0)
    assert turn.descriptor is None
    assert sandbox.state.holder.exists()
    assert sandbox.records() == []
    with held(sandbox.state):
        pass


def test_next_turn_retries_cleanup_before_replacing_a_still_live_holder(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid, start = os.getpid(), provisioning.process_start(os.getpid())
    holder = json.dumps({"pid": pid, "start": start})
    sandbox.state.holder.write_text(holder)
    retried: list[str] = []

    def pending(marker: str) -> int:
        retried.append(marker)
        raise provisioning.CleanupPendingError("pending")

    monkeypatch.setattr(provisioning, "end_marked", pending)
    turn = provisioning.Turn(sandbox.state, sandbox.config, ["pip", "install", "x"], "wrapper")
    with pytest.raises(provisioning.CleanupPendingError):
        turn.begin()
    assert retried == [f"{pid}:{start}"]
    assert sandbox.state.holder.read_text() == holder
    with held(sandbox.state):
        pass


@pytest.mark.parametrize("broken_stderr", [False, True])
def test_module_hook_exits_instead_of_running_after_failed_cleanup(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    broken_stderr: bool,
) -> None:
    def pending(_: object) -> int:
        raise provisioning.CleanupPendingError("pending")

    def exit_now(code: int) -> None:
        raise SystemExit(code)

    monkeypatch.setattr(provisioning, "end_strays", pending)
    monkeypatch.setattr(provisioning, "exit_as", exit_now)
    if broken_stderr:

        class BrokenStderr:
            def write(self, text: str) -> None:
                raise OSError("closed output")

        monkeypatch.setattr(sys, "stderr", BrokenStderr())
    with pytest.raises(SystemExit) as caught:
        provisioning.hook(
            str(sandbox.state.directory),
            "pip",
            ["python", "-m", "pip", "install", "x"],
            ["install", "x"],
        )
    assert caught.value.code == 1
    assert not sandbox.prefix.log.exists()
    with held(sandbox.state):
        pass


def test_review2_l4_what_a_command_leaves_running_ends_with_its_turn(sandbox: Sandbox) -> None:
    done = sandbox.run("pip", "install", "detaching")
    assert done.returncode == 0, done.stderr
    assert "ended 1 processes the package command left running" in done.stderr
    _, step = _logged_pids(sandbox.prefix.log, "detaching")
    assert not alive(step)


def test_review2_l5_a_signal_to_the_process_group_reaches_the_command_once(
    sandbox: Sandbox,
) -> None:
    # A kernel interrupt is killpg(SIGINT): off a terminal, the command no longer shares
    # its wrapper's group, and gets the signal once, from the wrapper.
    wrapper = subprocess.Popen(  # noqa: S603
        [str(sandbox.state.bin / "pip"), "install", "counting"],
        env=sandbox.env,
        start_new_session=True,  # a group of its own, as a kernel's
        stdin=subprocess.DEVNULL,
    )
    sandbox.prefix.pid("ready", "counting")
    os.killpg(wrapper.pid, signal.SIGINT)
    wrapper.wait(30)
    assert sandbox.prefix.log.read_text().splitlines().count("got INT") == 1


@pytest.mark.asyncio
async def test_review2_l6_a_timed_out_install_gets_its_grace_to_roll_back(worker: Worker) -> None:
    # A script of more than one command: bash ends on SIGTERM at once, and the call's
    # other processes got SIGKILL right after, in the middle of the rollback.
    await worker.run("op-0", script="true")
    done = await worker.run("op-t", script="pip install rollback; echo after", timeout_seconds=2)
    assert done["state"] == "timed_out", done["result"]
    lines = worker.prefix.log.read_text().splitlines()
    assert "got TERM" in lines
    assert "rolled back" in lines


# Regression tests for the third review of package commands and lab 17 (2026-10-11).


@pytest.mark.parametrize(
    "argument",
    [
        # A password with "#", "/" or "?" when no path follows the host.
        "--extra-index-url=https://user:test-secret#pw@private.example",
        "--extra-index-url=https://user:test-secret/pw@private.example",
        "--find-links=https://user:test-secret?pw@private.example:8443",
        "--find-links=https://user:test-secret/pw@private.example?x=1",
        # A bracketed IPv6 host.
        "--extra-index-url=https://user:test-secret/pw@[fd00::1]/simple",
        "--extra-index-url=https://user:test-secret#pw@[fd00::1]:8443",
    ],
)
def test_review3_l1_a_password_with_delimiters_is_redacted_whatever_follows_its_host(
    argument: str,
) -> None:
    words = provisioning.bounded_command(["pip", "install", argument, "x"])
    assert "test-secret" not in words[2]
    assert "://***@" in words[2]
    command = package_command({"command": words, "via": "wrapper"})  # the worker's pass
    assert command is not None
    assert command.argv == tuple(words)


def test_lab17_an_install_that_links_nothing_changes_no_fingerprint(sandbox: Sandbox) -> None:
    # Lab 17: `micromamba install -y toolz` with toolz installed linked nothing, but noted
    # the request in conda-meta/history, which the fingerprint covered: a revision with
    # the lockfile of the one before.
    places = sandbox.config.places
    before = provisioning.fingerprint(places)
    assert sandbox.run("micromamba", "install", "-y", "toolz").returncode == 0
    installed = provisioning.fingerprint(places)
    assert installed != before  # a real install still counts
    history = sandbox.prefix.root / "conda-meta" / "history"
    noted = history.stat().st_size
    assert sandbox.run("micromamba", "install", "-y", "toolz").returncode == 0
    assert history.stat().st_size > noted  # the request was noted...
    assert provisioning.fingerprint(places) == installed  # ...and changed nothing
    record = sandbox.prefix.root / "conda-meta" / "toolz-1.0-pyhd8ed1ab_0.json"
    record.write_text(record.read_text().replace('"1.0"', '"1.1"', 1))  # an unmanaged change
    assert provisioning.fingerprint(places) != installed


@pytest.mark.asyncio
async def test_lab17_an_install_that_links_nothing_makes_no_revision(worker: Worker) -> None:
    await worker.run("op-0", script="true")
    first = await worker.run("op-1", script="micromamba install -y toolz")
    again = await worker.run("op-2", script="micromamba install -y toolz")
    assert (first["state"], again["state"]) == ("succeeded", "succeeded"), again["result"]
    history = (await worker.revisions()).revisions
    assert [entry.revision for entry in history] == [0, 1]
    assert again["result"]["environment_revision"] == "1"
    assert again["result"]["overlapping_change"] is False
