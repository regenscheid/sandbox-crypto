# SPDX-License-Identifier: MIT
"""Package commands: turns, waits and revisions with lockfiles (docs/specs/conda-provisioning.md).

A throwaway environment (a venv with a ``conda-meta`` directory) stands in for the
sandbox's Conda prefix, and fake_manager.py for pip and micromamba, so nothing here
installs a real package or reaches the network. The wrappers and the hook run as real
processes; the worker tests use the local backend and real executors.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
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
from sandbox_science.worker import provisioning
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import ClosedHandler, LocalBackend
from sandbox_science.worker.channel import EventHandler, ExecutorChannel
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.revisions import Revisions
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

FAKE = Path(__file__).with_name("fake_manager.py")
CACHE_HOST = "host.openshell.internal"
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


def test_a_lockfile_names_packages_by_channel_never_by_the_cache(tmp_path: Path) -> None:
    meta, site, user = tmp_path / "conda-meta", tmp_path / "site", tmp_path / "user"
    for directory in (meta, site, user):
        directory.mkdir()
    sha = "a" * 64
    cache = f"http://{CACHE_HOST}:8090"
    numpy = "numpy-2.1.0-py313h0_0.conda"
    _meta(
        meta,
        "numpy-2.1.0-py313h0_0",
        {
            "name": "numpy",
            "version": "2.1.0",
            "build": "py313h0_0",
            "url": f"{cache}/conda/conda-forge/linux-64/{numpy}",
            "channel": f"{cache}/conda/conda-forge/linux-64",
            "fn": numpy,
            "sha256": sha,
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
    wheel = "ab/cd/cached-1.0-py3-none-any.whl"
    _dist(
        site,
        "cached",
        "1.0",
        "uv",
        direct_url_json=json.dumps({"url": f"{cache}/pypi/files/cached/{'c' * 64}/{wheel}"}),
    )
    local = "file:///sandbox/project/sessions/s1/dist/local_pkg-0.1-py3-none-any.whl"
    archive = {"url": local, "archive_info": {"hashes": {"sha256": "d" * 64}}}
    _dist(site, "local_pkg", "0.1", "pip", direct_url_json=json.dumps(archive))
    vcs = {"url": "git+https://user:test-secret@github.com/o/r.git", "vcs_info": {"vcs": "git"}}
    _dist(site, "fromgit", "0.2", "pip", direct_url_json=json.dumps(vcs))
    (site / "legacy-1.0-py3.13.egg-info").write_text("Metadata-Version: 1.1\nName: legacy\n")
    _dist(user, "userpkg", "1.0", "pip")
    places = provisioning.Places(str(meta), (str(site),), str(user))

    made = provisioning.lockfile(places)
    lockfile = Lockfile.model_validate(made)
    text = json.dumps(made)
    for secret in (CACHE_HOST, ":8090", "test-secret", "cache.example"):
        assert secret not in text
    conda = {package.name: package for package in lockfile.conda}
    assert sorted(conda) == ["dev", "elsewhere", "local", "numpy", "old", "python", "secret"]
    assert (conda["numpy"].channel, conda["numpy"].subdir, conda["numpy"].file) == (
        "conda-forge",
        "linux-64",
        numpy,
    )
    assert (conda["numpy"].sha256, conda["numpy"].license) == (sha, "BSD-3-Clause")
    assert conda["python"].channel == "conda-forge"
    assert conda["dev"].channel == "conda-forge/label/dev"
    assert conda["secret"].channel == "private"
    assert conda["local"].channel == "file:///tmp/channel"
    assert conda["elsewhere"].channel == "bioconda"
    assert (conda["old"].channel, conda["old"].subdir) == ("conda-forge", "linux-64")
    pip = {item.name: item for item in lockfile.pip}
    assert sorted(pip) == ["cached", "fromgit", "legacy", "local_pkg", "requests", "userpkg"]
    assert (pip["requests"].version, pip["requests"].installer) == ("2.32.0", "pip")
    assert pip["cached"].url == f"https://files.pythonhosted.org/{wheel}"
    assert pip["cached"].sha256 == "c" * 64
    assert (pip["local_pkg"].url, pip["local_pkg"].sha256) == (local, "d" * 64)
    assert pip["fromgit"].url == "git+https://github.com/o/r.git"
    assert pip["legacy"].version == "1.0"
    assert pip["userpkg"].location == "user"
    assert lockfile.python == ".".join(map(str, sys.version_info[:3]))


def test_the_worker_names_channels_again_whatever_the_executor_wrote(tmp_path: Path) -> None:
    # Research code can write any lockfile; the stored one never names the cache.
    revisions = Revisions(Store(tmp_path / "worker.sqlite3"), Blobs(tmp_path / "artifacts"))
    forged = {
        "conda": [
            {
                "name": "toolz",
                "version": "1.0",
                "build": "0",
                "channel": f"http://{CACHE_HOST}:8090/conda/conda-forge",
            }
        ],
        "pip": [
            {
                "name": "x",
                "version": "1",
                "url": f"https://u:p@{CACHE_HOST}/pypi/files/x/{'e' * 64}/x-1-py3-none-any.whl",
            }
        ],
    }
    try:
        stored = revisions.store_lockfile(forged)
        assert stored is not None
        data = revisions.blobs.path(stored.sha256).read_text()
        assert CACHE_HOST not in data
        lockfile = Lockfile.model_validate_json(data)
        assert lockfile.conda[0].channel == "conda-forge"
        assert lockfile.pip[0].url == "https://files.pythonhosted.org/x-1-py3-none-any.whl"
        assert revisions.store_lockfile({"conda": [{"name": "no version"}]}) is None
        assert revisions.store_lockfile("not a lockfile") is None
    finally:
        revisions.store.close()


# The wrappers and the hook, as processes.


def test_wrappers_are_written_for_the_managers_on_path(sandbox: Sandbox) -> None:
    bin_dir = sandbox.state.bin
    assert sorted(path.name for path in bin_dir.iterdir()) == ["micromamba", "pip", "pip3"]
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
    assert records[0]["before_lockfile"] is None  # nothing changed before it


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


def test_a_change_before_a_command_is_kept_apart(sandbox: Sandbox) -> None:
    (sandbox.prefix.site / "sneaky-1.0.dist-info").mkdir()  # no wrapper saw this
    assert sandbox.run("pip", "install", "delta").returncode == 0
    [record] = sandbox.records()
    assert [item["name"] for item in record["before_lockfile"]["pip"]] == ["sneaky"]
    observed = provisioning.observe(sandbox.state, sandbox.config.places, "0" * 32)
    assert [entry["command"] for entry in observed.records] == [["pip", "install", "delta"]]
    assert observed.fingerprint == record["after"]
    assert observed.lockfile is None  # the record accounts for the packages now
    assert sandbox.records() == []  # taken from the journal


# The worker: executions wait while a package command runs, and each change is a revision.


class Worker:
    """A worker on the local backend whose executor runs with the throwaway prefix."""

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

    async def revisions(self) -> RevisionHistory:
        status, body = await self.get("/v1/environments/env1/revisions")
        assert status == 200, body
        return RevisionHistory.model_validate(body)

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
    assert (zero.managed, zero.changed_by, zero.command) == (None, None, None)
    assert (one.managed, one.changed_by) == (True, first["attempt_id"])
    assert (two.managed, two.changed_by) == (True, second["attempt_id"])
    assert one.command is not None
    assert two.command is not None
    assert one.command.argv == ("pip", "install", "alpha")
    assert two.command.argv == ("pip", "install", "beta")
    assert (one.command.via, one.command.exit_code) == ("wrapper", 0)
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
    assert (unmanaged.managed, unmanaged.changed_by, unmanaged.command) == (
        False,
        bypass["attempt_id"],
        None,
    )
    assert unmanaged.lockfile is not None
    assert (by_wrapper.managed, by_wrapper.changed_by) == (True, wrapped["attempt_id"])
    assert (by_module.managed, by_module.changed_by) == (True, module["attempt_id"])
    assert by_module.command is not None
    assert by_module.command.via == "module"
    assert by_module.command.argv[1:] == ("-m", "pip", "install", "zeta")
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
    assert last.command is not None
    assert last.command.argv[1:] == ("-m", "pip", "install", "kappa")
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
    ):
        status, body = await worker.get(path)
        assert status == expected, (path, body)
    status, body = await worker.get("/v1/environments/never-used/revisions")
    assert (status, body) == (200, {"environment_id": "never-used", "revisions": []})


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
        records = Records()

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


@pytest.mark.asyncio
async def test_revisions_from_a_taken_over_executor_are_bounded_and_checked(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        HostileBackend("revisions"),
        Blobs(tmp_path / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=tmp_path,
    )
    await manager.start()
    worker = Worker(
        await aiohttp_client(create_app(manager, TOKEN)), manager, tmp_path, make_prefix(tmp_path)
    )
    try:
        execution = await worker.run("op-hostile", script="true")
        assert execution["state"] == "succeeded"
        revisions = (await worker.revisions()).revisions
        assert len(revisions) == 64  # MAX_REVISIONS for one execution
        # Nothing names an attempt of no one's, and only the real lockfile is kept,
        # named by its channel.
        assert all(entry.changed_by is None for entry in revisions)
        assert revisions[0].lockfile is not None
        assert all(entry.lockfile is None for entry in revisions[1:])
        lockfile, data = await worker.lockfile(0)
        assert lockfile.conda[0].channel == "conda-forge"
        assert CACHE_HOST.encode() not in data
        command = revisions[1].command
        assert command is not None
        assert len(command.argv[2]) == 1024
    finally:
        await manager.close()
        store.close()
