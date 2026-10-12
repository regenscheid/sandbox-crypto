# SPDX-License-Identifier: MIT
"""The package clients' configuration: rendering, the executor's ``configure``, the backend.

The OpenShell backend runs against a stand-in ``openshell`` CLI that runs each
command here, so the whole path from the worker setting to a shell command's
and a kernel's environment is exercised without a gateway.
test_package_clients_integration.py runs the real clients.
"""

from __future__ import annotations

import asyncio
import configparser
import dataclasses
import re
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml
from pydantic import ValidationError

from sandbox_science.contracts import (
    CellRequest,
    Execution,
    ExecutionContext,
    ResultEvent,
    ShellRequest,
)
from sandbox_science.policy.package_requests import classify_package_request
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import OpenShellBackend
from sandbox_science.worker.cli import _environments as cli_environments  # pyright: ignore[reportPrivateUsage]
from sandbox_science.worker.cli import main
from sandbox_science.worker.executor import Executor, Message
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.package_clients import (
    CacheUrlError,
    ClientConfiguration,
    cache_endpoint,
    check_cache_url,
    render,
)
from sandbox_science.worker.settings import OpenShellBackendSettings, load_settings
from sandbox_science.worker.store import Store

CACHE = "http://host.openshell.internal:8090"
TARGETS = Path(__file__).resolve().parents[3] / "examples" / "openshell" / "targets.yaml"
CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
VARIABLES = ("PIP_CONFIG_FILE", "CONDARC", "GOENV", "NPM_CONFIG_USERCONFIG", "UV_DEFAULT_INDEX")

FAKE_OPENSHELL = """\
import os
import shutil
import sys

# Stands in for the openshell CLI: `sandbox exec` runs the command here, and
# `sandbox upload` copies the file.
args = sys.argv[1:]
if args[:2] == ["sandbox", "upload"]:
    shutil.copy(args[-2], args[-1])
elif args[:2] == ["sandbox", "exec"] and "--" in args:
    command = args[args.index("--") + 1 :]
    os.execvp(command[0], command)
else:
    sys.exit(f"unexpected arguments: {args}")
"""


@pytest.fixture(autouse=True)
def no_host_client_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the host's own client settings from the executor."""
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def files_of(clients: ClientConfiguration) -> dict[str, str]:
    return {file.path: file.content for file in clients.files}


def assignments(content: str) -> dict[str, str]:
    """The ``key=value`` lines of an npmrc or a go env file."""
    pairs: dict[str, str] = {}
    for line in content.splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            pairs[key] = value
    return pairs


# Rendering.


def test_the_configuration_for_a_plain_http_cache() -> None:
    clients = render(CACHE)
    assert [(file.base, file.path, file.variable) for file in clients.files] == [
        ("config", "pip.conf", "PIP_CONFIG_FILE"),
        ("config", "condarc", "CONDARC"),
        ("home", ".cargo/config.toml", None),
        ("config", "go.env", "GOENV"),
        ("config", "npmrc", "NPM_CONFIG_USERCONFIG"),
    ]
    assert clients.env == {"UV_DEFAULT_INDEX": f"{CACHE}/pypi/simple/"}
    files = files_of(clients)
    pip = configparser.ConfigParser(interpolation=None)
    pip.read_string(files["pip.conf"])
    assert dict(pip["global"]) == {
        "index-url": f"{CACHE}/pypi/simple/",
        "trusted-host": "host.openshell.internal:8090",
    }
    assert yaml.safe_load(files["condarc"]) == {
        "channel_alias": f"{CACHE}/conda",
        "channels": ["conda-forge"],
        "default_channels": [],
    }
    assert tomllib.loads(files[".cargo/config.toml"]) == {
        "source": {
            "crates-io": {"replace-with": "package-cache"},
            "package-cache": {"registry": f"sparse+{CACHE}/cargo/index/"},
        },
        # Over plain HTTP, Cargo would otherwise ask for an h2c upgrade (lab 2).
        "http": {"multiplexing": False},
    }
    assert assignments(files["go.env"]) == {
        "GOPROXY": f"{CACHE}/go",
        "GOSUMDB": "sum.golang.org",
        "GOTOOLCHAIN": "local",
    }
    assert assignments(files["npmrc"]) == {
        "registry": f"{CACHE}/npm/",
        "audit": "false",
        "fund": "false",
        "update-notifier": "false",
    }


def test_an_https_cache_needs_no_plain_http_settings() -> None:
    files = files_of(render("https://cache.example"))
    pip = configparser.ConfigParser(interpolation=None)
    pip.read_string(files["pip.conf"])
    assert dict(pip["global"]) == {"index-url": "https://cache.example/pypi/simple/"}
    assert "http" not in tomllib.loads(files[".cargo/config.toml"])


@pytest.mark.parametrize(
    ("url", "normalized", "trusted"),
    [
        ("http://host.openshell.internal:8090/", CACHE, "host.openshell.internal:8090"),
        (
            "http://Cache.Example:8090/mirror/",
            "http://cache.example:8090/mirror",
            "cache.example:8090",
        ),
        ("http://cache", "http://cache", "cache"),
        ("http://127.0.0.1:8090", "http://127.0.0.1:8090", "127.0.0.1:8090"),
        ("http://[::1]:8090", "http://[::1]:8090", "[::1]:8090"),
    ],
)
def test_urls_are_normalized(url: str, normalized: str, trusted: str | None) -> None:
    assert check_cache_url(url) == normalized
    clients = render(url)
    assert clients.env["UV_DEFAULT_INDEX"] == f"{normalized}/pypi/simple/"
    if trusted is not None:
        assert f"trusted-host = {trusted}\n" in files_of(clients)["pip.conf"]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "host.openshell.internal:8090",
        "ftp://cache",
        "http://",
        "http://user@cache",
        "http://user:secret@cache",
        "http://cache?x=1",
        "http://cache#x",
        "http://cache/a b",
        "http://cache/a%20b",
        "http://cache/{crate}",
        'http://cache/"',
        "http://cache\n[http]",
        "http://cache:0",
        "http://cache:65536",
        "http://cache/../x",
        "http://cache/./x",
        "http://cache//x",
        "http://[not-an-address]",
        "http://[1:2:3]:8090",
        "https://cache,direct",
        "http://cache|direct",
    ],
)
def test_unusable_urls_are_refused(url: str) -> None:
    with pytest.raises(CacheUrlError):
        render(url)


@pytest.mark.parametrize("cache", [CACHE, "https://cache.example/mirror"])
def test_clients_reach_the_cache_and_nothing_else(cache: str) -> None:
    clients = render(cache)
    texts = [*files_of(clients).values(), *clients.env.values()]
    urls = [url for text in texts for url in re.findall(r"https?://[^\s\"']+", text)]
    assert urls
    assert all(url.startswith(f"{cache}/") for url in urls), urls
    go = assignments(files_of(clients)["go.env"])
    # One proxy, no "direct" or "off" fallback, and the checksum database stays on.
    assert go["GOPROXY"] == f"{cache}/go"
    assert go["GOSUMDB"] == "sum.golang.org"
    assert not {"GONOSUMDB", "GONOSUMCHECK", "GOPRIVATE", "GONOPROXY", "GOINSECURE"} & set(go)
    pip = files_of(clients)["pip.conf"]
    assert "extra-index-url" not in pip
    assert "find-links" not in pip
    assert "defaults" not in yaml.safe_load(files_of(clients)["condarc"])["channels"]


def test_the_paths_follow_the_cache_layout() -> None:
    # The policy middleware classifies every request to the cache, and refuses
    # anything outside the layout the cache serves.
    clients = render(CACHE)
    files = files_of(clients)
    pip = configparser.ConfigParser(interpolation=None)
    pip.read_string(files["pip.conf"])
    conda = yaml.safe_load(files["condarc"])["channel_alias"]
    cargo = tomllib.loads(files[".cargo/config.toml"])["source"]["package-cache"]["registry"]
    go = assignments(files["go.env"])["GOPROXY"]
    npm = assignments(files["npmrc"])["registry"]
    requests = [
        (pip["global"]["index-url"] + "six/", "pypi", "six"),
        (clients.env["UV_DEFAULT_INDEX"] + "attrs/", "pypi", "attrs"),
        (f"{conda}/conda-forge/linux-64/repodata.json", "conda", None),
        (f"{conda}/conda-forge/noarch/repodata_shards.msgpack.zst", "conda", None),
        (cargo.removeprefix("sparse+") + "config.json", "cargo", None),
        (cargo.removeprefix("sparse+") + "it/oa/itoa", "cargo", "itoa"),
        (f"{go}/golang.org/x/text/@v/list", "go", "golang.org/x/text"),
        (f"{go}/sumdb/sum.golang.org/supported", "go", None),
        (npm + "is-number", "npm", "is-number"),
    ]
    for url, ecosystem, name in requests:
        operation = classify_package_request("GET", urlsplit(url).path)
        assert (operation.ecosystem, operation.name) == (ecosystem, name), url


# The executor's configure operation.


class Replies:
    """Collects what an in-process executor sends."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)


async def configure(executor: Executor, replies: Replies, **request: Any) -> Message:
    await executor.handle({"id": len(replies.messages), "op": "configure", **request})
    return replies.messages[-1]


def request_for(clients: ClientConfiguration) -> dict[str, Any]:
    """The request the OpenShell backend sends."""
    return {
        "files": [dataclasses.asdict(file) for file in clients.files],
        "env": dict(clients.env),
    }


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.asyncio
async def test_configure_writes_the_files_and_sets_the_variables(
    tmp_path: Path, home: Path
) -> None:
    replies = Replies()
    executor = Executor(tmp_path / "project", replies)
    clients = render(CACHE)
    reply = await configure(executor, replies, **request_for(clients))
    config = tmp_path / "project" / ".executor" / "config"
    assert reply["ok"] is True
    assert reply["skipped"] == []
    assert sorted(reply["written"]) == sorted(
        [str(config / name) for name in ("pip.conf", "condarc", "go.env", "npmrc")]
        + [str(home / ".cargo" / "config.toml")]
    )
    files = files_of(clients)
    assert (home / ".cargo" / "config.toml").read_text() == files[".cargo/config.toml"]
    env = executor.environment("s1")
    assert env["PIP_CONFIG_FILE"] == str(config / "pip.conf")
    assert env["CONDARC"] == str(config / "condarc")
    assert env["GOENV"] == str(config / "go.env")
    assert env["NPM_CONFIG_USERCONFIG"] == str(config / "npmrc")
    assert env["UV_DEFAULT_INDEX"] == f"{CACHE}/pypi/simple/"
    for variable, name in [("PIP_CONFIG_FILE", "pip.conf"), ("CONDARC", "condarc")]:
        assert Path(env[variable]).read_text() == files[name]
    # Unchanged files are not written again.
    again = await configure(executor, replies, **request_for(clients))
    assert (again["written"], again["skipped"]) == ([], [])


@pytest.mark.asyncio
async def test_configure_keeps_a_file_changed_since_it_wrote_it(tmp_path: Path, home: Path) -> None:
    replies = Replies()
    executor = Executor(tmp_path / "project", replies)
    await configure(executor, replies, **request_for(render(CACHE)))
    npmrc = tmp_path / "project" / ".executor" / "config" / "npmrc"
    changed = npmrc.read_text() + "loglevel=verbose\n"
    npmrc.write_text(changed)
    (home / ".cargo" / "config.toml").unlink()
    # The cache moved: every file the user left alone is written again.
    moved = render("http://cache.example:8090")
    restarted = Executor(tmp_path / "project", replies)
    reply = await configure(restarted, replies, **request_for(moved))
    assert reply["skipped"] == [
        {"path": str(npmrc), "reason": "it changed after the executor wrote it"}
    ]
    assert len(reply["written"]) == 4
    assert npmrc.read_text() == changed
    assert (home / ".cargo" / "config.toml").read_text() == files_of(moved)[".cargo/config.toml"]
    # The kept file is still the one npm reads.
    assert restarted.environment("s1")["NPM_CONFIG_USERCONFIG"] == str(npmrc)


@pytest.mark.asyncio
async def test_configure_keeps_files_it_did_not_write(tmp_path: Path, home: Path) -> None:
    replies = Replies()
    executor = Executor(tmp_path / "project", replies)
    cargo = home / ".cargo" / "config.toml"
    cargo.parent.mkdir()
    cargo.write_text("[build]\njobs = 2\n")
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text("mine\n")
    linked = tmp_path / "project" / ".executor" / "config" / "pip.conf"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(elsewhere)
    reply = await configure(executor, replies, **request_for(render(CACHE)))
    assert reply["skipped"] == [
        {"path": str(linked), "reason": "it is not a regular file"},
        {"path": str(cargo), "reason": "the executor did not write it"},
    ]
    assert cargo.read_text() == "[build]\njobs = 2\n"
    assert elsewhere.read_text() == "mine\n"
    # pip still reads the file the link names.
    assert executor.environment("s1")["PIP_CONFIG_FILE"] == str(linked)


@pytest.mark.parametrize(
    "request_fields",
    [
        {"files": "pip.conf"},
        {"env": ["UV_DEFAULT_INDEX"]},
        {"env": {"NOT A NAME": "x"}},
        {"env": {"UV_DEFAULT_INDEX": 1}},
        {"files": [{"base": "root", "path": "pip.conf", "content": ""}]},
        {"files": [{"base": "home", "path": "/etc/pip.conf", "content": ""}]},
        {"files": [{"base": "home", "path": "../pip.conf", "content": ""}]},
        {"files": [{"base": "home", "path": ".", "content": ""}]},
        {"files": [{"base": "home", "path": "pip.conf", "content": 1}]},
        {"files": [{"base": "config", "path": "pip.conf", "content": "", "variable": "A-B"}]},
    ],
)
@pytest.mark.asyncio
async def test_configure_refuses_malformed_requests(
    tmp_path: Path, home: Path, request_fields: dict[str, Any]
) -> None:
    replies = Replies()
    executor = Executor(tmp_path / "project", replies)
    reply = await configure(executor, replies, **request_fields)
    assert reply["ok"] is False
    assert reply["error"]["kind"] == "invalid_request"
    # The executor's state directory holds its package-command wrappers from the start.
    state = tmp_path / "project" / ".executor"
    assert not (state / "config").exists()
    assert not (state / "configured.json").exists()
    assert list(home.iterdir()) == []
    assert executor.configured == {}


# The OpenShell backend, through a stand-in CLI.


def openshell_settings(tmp_path: Path, **changes: Any) -> OpenShellBackendSettings:
    script = tmp_path / "fake_openshell.py"
    script.write_text(FAKE_OPENSHELL)
    cli = tmp_path / "openshell"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    cli.chmod(0o755)
    sandbox = tmp_path / "sandbox"
    return OpenShellBackendSettings(
        cli=str(cli),
        daemon=False,  # This stand-in exercises the original stdio transport.
        python=sys.executable,
        project_root=str(sandbox / "project"),
        executor_dir=str(sandbox / ".sandbox-science"),
        isolation_tier=3,  # the probe lowers it: the stand-in runs everything here
        sched_shim=False,
        env={"HOME": str(sandbox)},
        sandboxes={"env1": "research-1"},
        **changes,
    )


async def run(manager: ExecutionManager, request: ShellRequest | CellRequest) -> Execution:
    async def follow() -> Execution:
        execution, _ = await manager.submit(request)
        async for event in manager.events(execution.execution_id, 0):
            if isinstance(event, ResultEvent):
                return event.execution
        raise AssertionError("the event stream ended without a result")

    return await asyncio.wait_for(follow(), 120)


async def with_manager(
    tmp_path: Path, settings: OpenShellBackendSettings, *requests: ShellRequest | CellRequest
) -> list[Execution]:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        OpenShellBackend(settings, tmp_path / "state"),
        Blobs(tmp_path / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=tmp_path / "state",
    )
    await manager.start()
    try:
        return [await run(manager, request) for request in requests]
    finally:
        await manager.close()
        store.close()


def stdout(execution: Execution) -> str:
    assert execution.result is not None
    assert execution.result.stdout is not None, execution.result
    return execution.result.stdout.text


SHOW = "; ".join(f'echo "{name}=${{{name}-unset}}"' for name in VARIABLES)


@pytest.mark.asyncio
async def test_openshell_sandboxes_get_the_configuration(tmp_path: Path) -> None:
    settings = openshell_settings(tmp_path, package_cache=f"{CACHE}/")
    shell, cell = await with_manager(
        tmp_path,
        settings,
        ShellRequest(
            operation_id="op-shell",
            context=CONTEXT,
            script=f'{SHOW}; cat "$PIP_CONFIG_FILE" ~/.cargo/config.toml',
        ),
        CellRequest(
            operation_id="op-cell",
            context=CONTEXT,
            code="import os\nprint(os.environ['UV_DEFAULT_INDEX'], os.environ['GOENV'])",
        ),
    )
    config = tmp_path / "sandbox" / "project" / ".executor" / "config"
    files = files_of(render(CACHE))
    assert stdout(shell) == (
        f"PIP_CONFIG_FILE={config / 'pip.conf'}\n"
        f"CONDARC={config / 'condarc'}\n"
        f"GOENV={config / 'go.env'}\n"
        f"NPM_CONFIG_USERCONFIG={config / 'npmrc'}\n"
        f"UV_DEFAULT_INDEX={CACHE}/pypi/simple/\n" + files["pip.conf"] + files[".cargo/config.toml"]
    )
    assert stdout(cell) == f"{CACHE}/pypi/simple/ {config / 'go.env'}\n"


@pytest.mark.asyncio
async def test_without_a_package_cache_nothing_is_configured(tmp_path: Path) -> None:
    (shell,) = await with_manager(
        tmp_path,
        openshell_settings(tmp_path),
        ShellRequest(operation_id="op-shell", context=CONTEXT, script=SHOW),
    )
    assert stdout(shell) == "".join(f"{name}=unset\n" for name in VARIABLES)
    assert not (tmp_path / "sandbox" / "project" / ".executor" / "config").exists()
    assert not (tmp_path / "sandbox" / ".cargo").exists()


@pytest.mark.asyncio
async def test_the_worker_says_which_files_it_left_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cargo = tmp_path / "sandbox" / ".cargo" / "config.toml"
    cargo.parent.mkdir(parents=True)
    cargo.write_text("[build]\njobs = 2\n")
    caplog.set_level("INFO", logger="sandbox_science.worker.backends")
    (shell,) = await with_manager(
        tmp_path,
        openshell_settings(tmp_path, package_cache=CACHE),
        ShellRequest(operation_id="op-shell", context=CONTEXT, script="cat ~/.cargo/config.toml"),
    )
    assert stdout(shell) == "[build]\njobs = 2\n"
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "sandbox_science.worker.backends" and record.levelname == "WARNING"
    ]
    assert warnings == [
        f"environment env1: left {cargo} as it is (the executor did not write it); "
        "check that its client uses the package cache"
    ]


def test_the_package_cache_setting_is_checked(tmp_path: Path) -> None:
    settings = openshell_settings(tmp_path, package_cache="http://Cache.Example:8090/")
    assert settings.package_cache == "http://cache.example:8090"
    with pytest.raises(ValidationError, match="package cache URL"):
        openshell_settings(tmp_path, package_cache="http://cache:8090,direct")


@pytest.mark.parametrize(
    ("url", "endpoint"),
    [
        (CACHE, ("host.openshell.internal", 8090)),
        ("http://cache.example/", ("cache.example", 80)),
        ("https://Cache.Example/mirror", ("cache.example", 443)),
    ],
)
def test_the_cache_endpoint_is_the_host_and_port_clients_connect_to(
    url: str, endpoint: tuple[str, int]
) -> None:
    assert cache_endpoint(url) == endpoint


def environment_settings(tmp_path: Path, package_cache: str) -> Path:
    """A worker settings file whose worker creates sandboxes, with the example targets."""
    (tmp_path / "worker.token").write_text("t" * 40)
    (tmp_path / "pool-ceiling.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
    )
    path = tmp_path / "worker.toml"
    path.write_text(
        'token_file = "worker.token"\nstate_dir = "state"\n\n'
        f'[openshell]\nisolation_tier = 3\npackage_cache = "{package_cache}"\n\n'
        f'[openshell.environments]\npolicy_store = "middleware"\ntargets = "{TARGETS}"\n'
        'ceiling = "pool-ceiling.yaml"\nprofiles = {minimal = "docker.io/library/python:3.13"}\n'
    )
    return path


def test_the_package_cache_must_be_where_the_targets_file_puts_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Policies compiled for the worker's sandboxes admit the cache only at the
    # targets file's host and port, so clients pointed elsewhere would reach nothing.
    assert main(["serve", "--settings", str(environment_settings(tmp_path, CACHE))]) == 1
    assert capsys.readouterr().err == (
        f"error: package_cache {CACHE} is not where the targets file puts the package cache "
        "(package-cache.sandbox-science.internal:8090)\n"
    )
    matching = environment_settings(tmp_path, "http://package-cache.sandbox-science.internal:8090")
    environments = cli_environments(load_settings(matching))
    assert environments is not None
    environments.close()
