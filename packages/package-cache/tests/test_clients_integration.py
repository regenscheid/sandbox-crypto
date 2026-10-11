# SPDX-License-Identifier: MIT
"""Real package clients through a running cache (``SS_INTEGRATION=1``; needs the network).

Starts ``sandbox-science-package-cache serve`` on 127.0.0.1 with all five
ecosystems and installs a small package with pip, uv, mamba, cargo, go and npm.
Each client works in a temporary directory with its own caches and
configuration. A client that is not installed is skipped.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

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


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def stats(url: str) -> dict[str, int]:
    with urllib.request.urlopen(f"{url}/-/stats", timeout=10) as response:  # noqa: S310 - loopback
        return json.load(response)


@pytest.fixture(scope="module")
def cache_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    root = tmp_path_factory.mktemp("cache")
    port = free_port()
    settings = root / "cache.toml"
    settings.write_text(SETTINGS.format(port=port))
    log = (root / "serve.log").open("w")
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
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"{url}/-/health", timeout=1):  # noqa: S310
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail(f"the cache did not start: {(root / 'serve.log').read_text()}")
        yield url
    finally:
        server.terminate()
        server.wait(timeout=30)
        log.close()


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    result = subprocess.run(  # noqa: S603 - fixed client commands
        command, cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True, timeout=600
    )
    assert result.returncode == 0, f"{command[0]} failed:\n{result.stdout}\n{result.stderr}"


def need(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        pytest.skip(f"{tool} is not installed")
    return path


def test_pip(cache_url: str, tmp_path: Path) -> None:
    pip = need("pip")
    run(
        [
            pip,
            "download",
            "--no-deps",
            "--no-cache-dir",
            "--index-url",
            f"{cache_url}/pypi/simple/",
            "--trusted-host",
            "127.0.0.1",
            "--dest",
            str(tmp_path),
            "six==1.16.0",
        ],
        cwd=tmp_path,
        env={"PIP_CONFIG_FILE": os.devnull},
    )
    assert list(tmp_path.glob("six-1.16.0-*.whl"))


def test_uv(cache_url: str, tmp_path: Path) -> None:
    uv = need("uv")
    run(
        [
            uv,
            "pip",
            "install",
            "--no-cache",
            "--no-config",
            "--python",
            sys.executable,
            "--target",
            str(tmp_path / "target"),
            "--index-url",
            f"{cache_url}/pypi/simple/",
            "attrs==24.2.0",
        ],
        cwd=tmp_path,
        env={},
    )
    assert (tmp_path / "target" / "attrs").is_dir()


def test_mamba(cache_url: str, tmp_path: Path) -> None:
    mamba = need("mamba")
    (tmp_path / "condarc").write_text("")
    run(
        [
            mamba,
            "create",
            "--yes",
            "--prefix",
            str(tmp_path / "env"),
            "--override-channels",
            "--channel",
            f"{cache_url}/conda/conda-forge",
            "zlib",
        ],
        cwd=tmp_path,
        env={
            "MAMBA_ROOT_PREFIX": str(tmp_path / "root"),
            "CONDA_PKGS_DIRS": str(tmp_path / "pkgs"),
            "CONDARC": str(tmp_path / "condarc"),
        },
    )
    assert list((tmp_path / "env" / "conda-meta").glob("zlib-*.json"))


def test_cargo(cache_url: str, tmp_path: Path) -> None:
    cargo = need("cargo")
    home = tmp_path / "cargo-home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[source.crates-io]\nreplace-with = "cache"\n'
        f'[source.cache]\nregistry = "sparse+{cache_url}/cargo/index/"\n'
    )
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "lib.rs").write_text("")
    (project / "Cargo.toml").write_text(
        '[package]\nname = "smoke"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[dependencies]\nitoa = "=1.0.11"\n'
    )
    run([cargo, "fetch"], cwd=project, env={"CARGO_HOME": str(home)})
    assert any(home.glob("registry/cache/*/itoa-1.0.11.crate"))


def test_go(cache_url: str, tmp_path: Path) -> None:
    go = need("go")
    run(
        [go, "mod", "download", "-json", "golang.org/x/text@v0.20.0"],
        cwd=tmp_path,
        env={
            "GOPROXY": f"{cache_url}/go",
            "GOSUMDB": "sum.golang.org",
            "GONOSUMDB": "",
            "GOPRIVATE": "",
            "GOFLAGS": "-modcacherw",
            "GOMODCACHE": str(tmp_path / "modcache"),
            "GOPATH": str(tmp_path / "gopath"),
            "GOTOOLCHAIN": "local",
            "GOTELEMETRY": "off",
            "GOENV": "off",
        },
    )
    assert any((tmp_path / "modcache" / "golang.org" / "x").glob("text@v0.20.0"))


def test_npm(cache_url: str, tmp_path: Path) -> None:
    npm = need("npm")
    run(
        [
            npm,
            "install",
            "--prefix",
            str(tmp_path / "project"),
            "--registry",
            f"{cache_url}/npm/",
            "--cache",
            str(tmp_path / "npm-cache"),
            "--no-audit",
            "--no-fund",
            "--no-update-notifier",
            "is-number@7.0.0",
            "@isaacs/string-locale-compare@1.1.0",
        ],
        cwd=tmp_path,
        env={"npm_config_userconfig": os.devnull},
    )
    modules = tmp_path / "project" / "node_modules"
    assert (modules / "is-number").is_dir()
    assert (modules / "@isaacs" / "string-locale-compare").is_dir()


def test_the_cache_counted_the_traffic(cache_url: str) -> None:
    counters = stats(cache_url)
    assert counters["misses"] > 0
    assert counters["entries"] > 0
