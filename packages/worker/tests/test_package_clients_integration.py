# SPDX-License-Identifier: MIT
"""Real package clients, configured as the worker configures a sandbox (``SS_INTEGRATION=1``).

Starts ``sandbox-science-package-cache serve`` on 127.0.0.1 and a real executor
whose home is a temporary directory. The OpenShell backend's ``configure`` step
writes the rendered configuration, and pip, uv, conda, mamba, micromamba, cargo,
go and npm then run as shell commands through the executor without any registry
option: only that configuration points them at the cache. The proxy variables
name a trap that refuses and records every request, so a client that tries
anything but the cache is caught, and the cache's log must show each client's
requests. A client that is not installed is skipped. The cache needs access to
the public registries.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from sandbox_science.worker.backends import EXECUTOR_SOURCE, configure_package_clients
from sandbox_science.worker.channel import ExecutorChannel, Message
from sandbox_science.worker.package_clients import render

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SS_INTEGRATION") != "1",
        reason="needs SS_INTEGRATION=1 and access to the public registries",
    ),
]

SETTINGS = """\
listen = "127.0.0.1:{port}"
public_url = "http://127.0.0.1:{port}"
cache_dir = "cache"
max_size = "10Gi"

[pypi]
[conda]
[cargo]
[go]
[npm]
"""

HOST_SETTINGS = ("PIP_", "UV_", "CONDA", "MAMBA", "NPM_CONFIG_", "CARGO_", "XDG_")
"""Variables of the host's own client setup, which would override the files under test."""
GO_SETTINGS = frozenset(
    {
        "GOPROXY",
        "GOSUMDB",
        "GONOSUMDB",
        "GONOSUMCHECK",
        "GOPRIVATE",
        "GONOPROXY",
        "GOINSECURE",
        "GOENV",
        "GOFLAGS",
        "GOPATH",
        "GOMODCACHE",
        "GOTOOLCHAIN",
    }
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Trap(socketserver.ThreadingTCPServer):
    """A proxy that refuses every request and records its first line."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Refuse)
        self.seen: list[str] = []


class _Refuse(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        cast(Trap, self.server).seen.append(self.rfile.readline(4096).decode("latin-1").strip())
        self.wfile.write(
            b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )


@dataclass
class Network:
    """The cache, its log, and the trap every other request goes to."""

    url: str
    log: Path
    trap: Trap

    def mark(self) -> tuple[int, int]:
        return self.log.stat().st_size, len(self.trap.seen)

    def check(self, since: tuple[int, int], *paths: str) -> None:
        """Since ``since``, the cache saw each path (or one it begins), and the trap nothing."""
        log = self.log.read_bytes()[since[0] :].decode()
        for path in paths:
            assert f"path={path}" in log, f"the cache never saw {path}"
        assert self.trap.seen[since[1] :] == [], "a client went around the cache"


@pytest.fixture(scope="module")
def network(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Network]:
    root = tmp_path_factory.mktemp("cache")
    port = free_port()
    settings = root / "cache.toml"
    settings.write_text(SETTINGS.format(port=port))
    log_path = root / "serve.log"
    log = log_path.open("w")
    server = subprocess.Popen(  # noqa: S603 - our own command
        [
            sys.executable,
            "-c",
            "import sys; from sandbox_science.package_cache.cli import main; sys.exit(main())",
            "serve",
            "--settings",
            str(settings),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    trap = Trap()
    threading.Thread(target=trap.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"{url}/-/health", timeout=1):  # noqa: S310
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail(f"the cache did not start: {log_path.read_text()}")
        yield Network(url, log_path, trap)
    finally:
        trap.shutdown()
        trap.server_close()
        server.terminate()
        server.wait(timeout=30)
        log.close()


class Sandbox:
    """A real executor, configured like a sandbox's, that runs shell commands."""

    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.home = Path(env["HOME"])
        self.workspace = root / "sessions" / "s1"
        self._ends: dict[str, asyncio.Future[Message]] = {}
        self._count = 0
        argv = [sys.executable, "-I", str(EXECUTOR_SOURCE), "--stdio", "--root", str(root)]
        self.channel = ExecutorChannel(argv, self._on_event, self._on_closed, env=env)

    async def _on_event(self, message: Message) -> None:
        future = self._ends.get(str(message.get("execution")))
        if message.get("event") == "end" and future is not None and not future.done():
            future.set_result(message)

    async def _on_closed(self, channel: ExecutorChannel) -> None:
        for future in self._ends.values():
            if not future.done():
                future.set_exception(AssertionError("the executor went away"))

    async def run(self, script: str, timeout: float = 900) -> str:
        """Run ``script`` in the session workspace; return its output, failing on an error."""
        self._count += 1
        execution = f"exe-{self._count}"
        self._ends[execution] = asyncio.get_running_loop().create_future()
        await self.channel.request(
            "shell", execution=execution, session="s1", script=script, timeout=timeout
        )
        end = await asyncio.wait_for(self._ends[execution], timeout + 30)
        result = end["result"]
        output = f"{result['stdout']['text']}\n{result['stderr']['text']}"
        assert end["state"] == "succeeded", f"{script} failed:\n{output}"
        return result["stdout"]["text"]


def sandbox_env(home: Path, trap: Trap) -> dict[str, str]:
    """The host's environment without its own client settings, and no way out but loopback."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(HOST_SETTINGS) and name not in GO_SETTINGS
    }
    rustup = Path.home() / ".rustup"
    if "RUSTUP_HOME" not in env and rustup.is_dir():
        env["RUSTUP_HOME"] = str(rustup)  # rustup's proxies find their toolchains there
    proxy = f"http://127.0.0.1:{trap.server_address[1]}"
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env[name] = proxy
    env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1,localhost"
    env["HOME"] = str(home)
    env["GOFLAGS"] = "-modcacherw"  # lets pytest delete the module cache
    return env


@pytest_asyncio.fixture
async def sandbox(network: Network, tmp_path: Path) -> AsyncIterator[Sandbox]:
    home = tmp_path / "home"
    home.mkdir()
    started = Sandbox(tmp_path / "project", sandbox_env(home, network.trap))
    await started.channel.start()
    try:
        await configure_package_clients(started.channel, render(network.url), "env-integration")
        started.workspace.mkdir(parents=True, exist_ok=True)
        yield started
    finally:
        await started.channel.close()


def need(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        pytest.skip(f"{tool} is not installed")
    return path


@pytest.mark.asyncio
async def test_pip(network: Network, sandbox: Sandbox) -> None:
    pip = need("pip")
    since = network.mark()
    await sandbox.run(f"{pip} download --no-deps --no-cache-dir --dest dl six==1.16.0")
    assert list((sandbox.workspace / "dl").glob("six-1.16.0-*.whl"))
    network.check(since, "/pypi/simple/six/", "/pypi/files/six/")


@pytest.mark.asyncio
async def test_uv(network: Network, sandbox: Sandbox) -> None:
    uv = need("uv")
    since = network.mark()
    await sandbox.run(
        f"{uv} pip install --no-cache --python {sys.executable} --target target attrs==24.2.0"
    )
    assert (sandbox.workspace / "target" / "attrs").is_dir()
    network.check(since, "/pypi/simple/attrs/", "/pypi/files/attrs/")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["conda", "mamba", "micromamba"])
async def test_conda_clients(network: Network, sandbox: Sandbox, tool: str) -> None:
    client = need(tool)
    since = network.mark()
    await sandbox.run(
        f"CONDA_PKGS_DIRS=$PWD/pkgs MAMBA_ROOT_PREFIX=$PWD/root "
        f"{client} create --yes --quiet --prefix $PWD/env zlib"
    )
    assert list((sandbox.workspace / "env" / "conda-meta").glob("zlib-*.json"))
    network.check(since, "/conda/conda-forge/linux-64/", "/conda/conda-forge/noarch/")


@pytest.mark.asyncio
async def test_cargo(network: Network, sandbox: Sandbox) -> None:
    cargo = need("cargo")
    project = sandbox.workspace / "smoke"
    (project / "src").mkdir(parents=True)
    (project / "src" / "lib.rs").write_text("")
    (project / "Cargo.toml").write_text(
        '[package]\nname = "smoke"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[dependencies]\nitoa = "=1.0.11"\n'
    )
    since = network.mark()
    await sandbox.run(f"cd smoke && {cargo} fetch")
    assert any(sandbox.home.glob(".cargo/registry/cache/*/itoa-1.0.11.crate"))
    network.check(
        since, "/cargo/index/config.json", "/cargo/index/it/oa/itoa", "/cargo/dl/itoa/1.0.11/"
    )


@pytest.mark.asyncio
async def test_go(network: Network, sandbox: Sandbox) -> None:
    go = need("go")
    since = network.mark()
    settings = await sandbox.run(f"{go} env GOPROXY GOSUMDB GOTOOLCHAIN")
    assert settings.split() == [f"{network.url}/go", "sum.golang.org", "local"]
    await sandbox.run(f"{go} mod download -json golang.org/x/text@v0.20.0")
    assert (sandbox.home / "go" / "pkg" / "mod" / "golang.org" / "x" / "text@v0.20.0").is_dir()
    # The module was checked against the checksum database, through the cache.
    network.check(
        since,
        "/go/golang.org/x/text/@v/v0.20.0.zip",
        "/go/sumdb/sum.golang.org/supported",
        "/go/sumdb/sum.golang.org/lookup/golang.org/x/text@v0.20.0",
    )


@pytest.mark.asyncio
async def test_npm(network: Network, sandbox: Sandbox) -> None:
    npm = need("npm")
    since = network.mark()
    await sandbox.run(
        f"{npm} install --prefix project is-number@7.0.0 @isaacs/string-locale-compare@1.1.0"
    )
    modules = sandbox.workspace / "project" / "node_modules"
    assert (modules / "is-number").is_dir()
    assert (modules / "@isaacs" / "string-locale-compare").is_dir()
    network.check(since, "/npm/is-number", "/npm/files/")


@pytest.mark.asyncio
async def test_npm_installs_from_a_lockfile_made_against_the_public_registry(
    network: Network, sandbox: Sandbox
) -> None:
    # npm rebases lockfile URLs on the default registry onto the configured one.
    npm = need("npm")
    with urllib.request.urlopen(f"{network.url}/npm/is-number", timeout=30) as response:  # noqa: S310
        integrity = json.load(response)["versions"]["7.0.0"]["dist"]["integrity"]
    project = sandbox.workspace / "locked"
    project.mkdir()
    (project / "package.json").write_text(
        json.dumps({"name": "smoke", "version": "1.0.0", "dependencies": {"is-number": "7.0.0"}})
    )
    lock = {
        "name": "smoke",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {"name": "smoke", "version": "1.0.0", "dependencies": {"is-number": "7.0.0"}},
            "node_modules/is-number": {
                "version": "7.0.0",
                "resolved": "https://registry.npmjs.org/is-number/-/is-number-7.0.0.tgz",
                "integrity": integrity,
            },
        },
    }
    (project / "package-lock.json").write_text(json.dumps(lock, indent=2))
    since = network.mark()
    await sandbox.run(f"cd locked && {npm} ci")
    assert (project / "node_modules" / "is-number" / "package.json").is_file()
    network.check(since, "/npm/is-number/-/is-number-7.0.0.tgz")
