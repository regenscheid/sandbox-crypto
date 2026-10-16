# SPDX-License-Identifier: MIT
"""A launcher config with every file it names, and the launcher run with stand-ins."""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher import toml

ConfigMaker = Callable[..., Path]
PARTS = (
    "sandbox-science-worker",
    "sandbox-science-harness",
    "sandbox-science-workspace",
    "sandbox-science-mcp-proxy",
)


def _merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        elif value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


@pytest.fixture
def make_config(tmp_path: Path) -> ConfigMaker:
    """Write ``sandbox-science.toml`` with ``changes`` merged in (None removes a key)."""
    site = tmp_path / "site"
    for directory in ("policy-store", "web/dist", "bin"):
        (site / directory).mkdir(parents=True, exist_ok=True)
    (site / "model.key").write_text("not read by the launcher\n")
    (site / "model.key").chmod(0o600)
    for name in ("policy.yaml", "targets.yaml", "pool-ceiling.yaml", "web/dist/index.html"):
        (site / name).write_text("# test\n")
    openshell = site / "bin" / "openshell"
    openshell.write_text("#!/bin/sh\nexit 0\n")
    openshell.chmod(0o755)

    def make(changes: dict[str, Any] | None = None, name: str = "sandbox-science.toml") -> Path:
        base: dict[str, Any] = {
            "state_dir": "state",
            "projects": [{"id": "crypto", "profile": "minimal", "sessions": ["main"]}],
            "model": {
                "url": "https://gateway.example/v1",
                "name": "sandbox-orchestrator",
                "api_key_file": "model.key",
            },
            "worker": {"listen": "127.0.0.1:18100", "policy": "policy.yaml"},
            "workspace": {"listen": "127.0.0.1:18200", "static": "web/dist"},
            "openshell": {
                "cli": "bin/openshell",
                "python": "/opt/sandbox-science/bin/python",
                "isolation_tier": 3,
                "env": {"XDG_CONFIG_HOME": "~/openshell-config"},
                "environments": {
                    "policy_store": "policy-store",
                    "targets": "targets.yaml",
                    "ceiling": "pool-ceiling.yaml",
                    "profiles": {"minimal": "sandbox-science/minimal:abc1234"},
                },
            },
        }
        path = site / name
        path.write_text(toml.dumps(_merge(base, changes or {})))
        return path

    return make


@pytest.fixture
def commands(tmp_path: Path) -> Path:
    """A directory with stand-ins for the parts' commands, each doing nothing."""
    directory = tmp_path / "commands"
    directory.mkdir()
    for name in PARTS:
        command = directory / name
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o755)
    return directory


@pytest.fixture
def fake_parts(tmp_path: Path) -> Path:
    """A directory of the parts' commands as ``fake_part.py`` plays them.

    Each is a Python script, as the real ones are, so its command line names it.
    """
    directory = tmp_path / "fake-commands"
    directory.mkdir()
    script = Path(__file__).with_name("fake_part.py")
    for name in PARTS:
        command = directory / name
        command.write_text(
            f"#!{sys.executable}\n"
            "import runpy, sys\n"
            f"sys.argv = [{str(script)!r}, {name!r}, *sys.argv[1:]]\n"
            f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
        )
        command.chmod(0o755)
    return directory


@pytest.fixture
def fake_openshell(tmp_path: Path) -> Path:
    """The OpenShell CLI as ``fake_openshell.py`` plays it, with the state and journal
    of ``site`` unless the environment names others."""
    return openshell_stand_in(
        tmp_path / "fake-openshell",
        FAKE_OPENSHELL=tmp_path / "openshell.json",
        FAKE_JOURNAL=tmp_path / "journal",
    )


def openshell_stand_in(command: Path, **defaults: Path | str) -> Path:
    """A command that runs ``fake_openshell.py`` with ``defaults`` in its environment,
    where the environment has none of its own."""
    script = Path(__file__).with_name("fake_openshell.py")
    settings = "".join(
        f"os.environ.setdefault({name!r}, {str(value)!r})\n" for name, value in defaults.items()
    )
    command.parent.mkdir(parents=True, exist_ok=True)
    command.write_text(
        f"#!{sys.executable}\n"
        "import os, runpy, sys\n"
        f"{settings}"
        f"sys.argv = [{str(script)!r}, *sys.argv[1:]]\n"
        f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
    )
    command.chmod(0o755)
    return command


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    assert os.path.expanduser("~") == str(path)
    return path


def bound_port() -> int:
    """A loopback port the system has free now, once its probe closes."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def free_port(taken: Collection[int], probe: Callable[[], int] = bound_port) -> int:
    """A free loopback port that is none of ``taken``. A port picked by binding and
    closing is free only until the system hands it out again, which it may do at once: a
    CI run gave the MCP proxy's forward the workspace's port, and the launcher refused
    the config (``both listen on``)."""
    for _ in range(100):
        port = probe()
        if port not in taken:
            return port
    raise AssertionError(f"no free loopback port outside {sorted(taken)}")


def free_ports() -> tuple[int, int]:
    """A port for the worker and one for the workspace, whose next port is free too."""
    while True:
        sockets = [socket.socket() for _ in range(2)]
        for item in sockets:
            item.bind(("127.0.0.1", 0))
        worker, workspace = (item.getsockname()[1] for item in sockets)
        for item in sockets:
            item.close()
        if workspace + 1 != worker and workspace < 65535:
            return worker, workspace


class Launcher:
    """``sandbox-science`` as a process, its output read as it comes."""

    def __init__(
        self, config: Path, env: dict[str, str], command: str = "up", *options: str
    ) -> None:
        self.config = config
        self.env = env
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.seen: list[str] = []
        self.ended = False
        """Whether its output has ended."""
        self.process = subprocess.Popen(
            [sys.executable, "-m", "sandbox_science.launcher", command, "--config", str(config),
             *options],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )  # fmt: skip
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)  # the end of its output

    def expect(self, text: str, timeout: float = 30) -> None:
        """Wait until the launcher says ``text``: at most ``timeout`` seconds, and no
        longer than its output lasts, so that a launcher that stopped early fails at once
        with what it said."""
        deadline = time.monotonic() + timeout
        while not any(text in line for line in self.seen):
            if self.ended:
                raise AssertionError(
                    f"no {text!r}: the launcher ended (status {self.process.wait(timeout)}), "
                    "saying:\n" + "\n".join(self.seen)
                )
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"no {text!r} in:\n" + "\n".join(self.seen)) from None
            if line is None:
                self.ended = True
            else:
                self.seen.append(line)

    def finish(self, timeout: float = 30) -> int:
        status = self.process.wait(timeout)
        # All it said: the reader ends at the end of its output, however loaded the host.
        self.reader.join(timeout)
        while not self.lines.empty():
            line = self.lines.get()
            if line is not None:
                self.seen.append(line)
        self.ended = True
        return status

    @property
    def output(self) -> str:
        return "\n".join(self.seen)

    def command(self, name: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "sandbox_science.launcher", name,
             "--config", str(self.config), *extra],
            capture_output=True, text=True, env=self.env, timeout=60, check=False,
        )  # fmt: skip


@pytest.fixture
def site(make_config: ConfigMaker, fake_parts: Path, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Launch ``sandbox-science up`` with the stand-ins, on free loopback ports."""
    worker, workspace = free_ports()
    journal = tmp_path / "journal"
    journal.touch()

    def launch(
        parts: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        options: tuple[str, ...] = (),
        **changes: Any,
    ) -> Launcher:
        fakes = ["FAKE_JOURNAL", "FAKE_PARTS", "FAKE_OPENSHELL"]
        extra = changes.pop("pass_env", {})
        config = make_config(
            {
                "bin_dir": str(fake_parts),
                "start_timeout": 20,
                "stop_timeout": changes.pop("stop_timeout", 5),
                "worker": {
                    "listen": f"127.0.0.1:{worker}",
                    "pass_env": [*fakes, *extra.get("worker", [])],
                },
                "run_service": {"pass_env": [*fakes, *extra.get("run_service", [])]},
                "workspace": {
                    "listen": f"127.0.0.1:{workspace}",
                    "pass_env": [*fakes, *extra.get("workspace", [])],
                },
                **changes,
            }
        )
        environment = {
            **os.environ,
            "FAKE_JOURNAL": str(journal),
            "FAKE_PARTS": json.dumps(parts or {}),
            "FAKE_OPENSHELL": str(tmp_path / "openshell.json"),
            **(env or {}),
        }
        return Launcher(config, environment, "up", *options)

    started: list[Launcher] = []

    def tracked(*args: Any, **kwargs: Any) -> Launcher:
        launcher = launch(*args, **kwargs)
        started.append(launcher)
        return launcher

    def spawn(config: Path, env: dict[str, str], command: str) -> Launcher:
        launcher = Launcher(config, env, command)
        started.append(launcher)
        return launcher

    yield {
        "launch": tracked,
        "spawn": spawn,
        "journal": journal,
        "worker": worker,
        "workspace": workspace,
        # The workspace's next port is its own too (``free_ports``).
        "taken": frozenset({worker, workspace, workspace + 1}),
    }
    for launcher in started:
        if launcher.process.poll() is None:
            launcher.process.send_signal(signal.SIGINT)
            launcher.process.wait(30)
