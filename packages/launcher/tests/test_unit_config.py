# SPDX-License-Identifier: MIT
"""The unit's config file: its three sections are checked as one, before anything starts."""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from sandbox_science.launcher import unit
from sandbox_science.launcher.config import LauncherError
from sandbox_science.launcher.unit import UnitConfig

GOOD = """
[worker]
listen = "127.0.0.1:18100"
pool_id = "test"

[middleware]
listen = "127.0.0.1:18101"

[cache]
listen = "127.0.0.1:18102"
max_size = "1Gi"
"""


def write(directory: Path, text: str, name: str = "unit.toml") -> Path:
    path = directory / name
    path.write_text(text)
    return path


def refused(path: Path, text: str, only: Sequence[str] = (), read: bool = True) -> None:
    with pytest.raises(LauncherError, match=text):
        unit.load(path, only=only, read=read)


def test_the_three_sections_load_in_start_order(tmp_path: Path) -> None:
    loaded = unit.load(write(tmp_path, GOOD))
    assert list(loaded.services) == ["cache", "middleware", "worker"]
    assert [s.listen for s in loaded.services.values()] == [
        "127.0.0.1:18102",
        "127.0.0.1:18101",
        "127.0.0.1:18100",
    ]
    assert loaded.options.log_level == "info"
    assert loaded.options.restart is False
    assert loaded.runtime_dir == tmp_path
    assert loaded.services["worker"].inline == {"listen": "127.0.0.1:18100", "pool_id": "test"}


def test_the_cache_is_optional_the_worker_and_the_middleware_are_not(tmp_path: Path) -> None:
    without_cache = GOOD.split("[cache]")[0]
    assert list(unit.load(write(tmp_path, without_cache)).services) == ["middleware", "worker"]
    refused(write(tmp_path, GOOD.replace("[worker]", "[workers]")), r"no such section: workers")
    only_worker = '[worker]\nlisten = "127.0.0.1:18100"\n'
    refused(write(tmp_path, only_worker), r"no \[middleware\] section")
    refused(write(tmp_path, ""), r"no \[worker\]\[middleware\] section")


def test_a_section_that_is_not_a_table_or_is_in_the_wrong_shape_is_refused(tmp_path: Path) -> None:
    refused(write(tmp_path, GOOD + "\n[unit]\nspeed = 3\n"), r"invalid \[unit\]")
    refused(write(tmp_path, GOOD.replace("[worker]", "worker = 3\n[x]")), r"not a table|no such")
    refused(
        write(tmp_path, GOOD + '\n[unit.pass_env]\nsandbox = ["X"]\n'), r"no such service: sandbox"
    )
    refused(write(tmp_path, GOOD + '\n[unit.pass_env]\nworker = ["not a name"]\n'), r"pass_env")
    refused(write(tmp_path, "[worker\n"), r"not TOML")
    refused(tmp_path / "missing.toml", r"no unit config")
    refused(write(tmp_path, "#" * (unit.MAX_CONFIG + 1)), r"larger than")


@pytest.mark.parametrize(
    "listen",
    [
        '"nowhere"',
        '"127.0.0.1:0"',
        '"127.0.0.1:70000"',
        '"[::1]:8100"',
        '":8100"',
        "8100",
        '"::1:8100"',
    ],
)
def test_every_service_must_give_a_host_and_port_the_unit_can_wait_on(
    tmp_path: Path, listen: str
) -> None:
    text = GOOD.replace('listen = "127.0.0.1:18100"', f"listen = {listen}")
    refused(write(tmp_path, text), r"\[worker\]")


def test_a_service_without_a_listen_address_is_refused(tmp_path: Path) -> None:
    text = GOOD.replace('listen = "127.0.0.1:18101"\n', "")
    refused(write(tmp_path, text), r"\[middleware\] has no listen")


@pytest.mark.parametrize(
    ("first", "second"),
    [("127.0.0.1:9000", "127.0.0.1:9000"), ("0.0.0.0:9000", "127.0.0.1:9000")],
)
def test_two_services_on_one_port_are_refused_before_either_starts(
    tmp_path: Path, first: str, second: str
) -> None:
    text = GOOD.replace("127.0.0.1:18100", first).replace("127.0.0.1:18101", second)
    refused(write(tmp_path, text), r"both listen on port 9000")
    # Another port on the same host, or the same port on another host, is fine.
    ok = GOOD.replace("127.0.0.1:18100", "127.0.0.1:9000").replace(
        "127.0.0.1:18101", "127.0.0.2:9000"
    )
    assert unit.load(write(tmp_path, ok)).services


def test_a_section_naming_a_settings_file_holds_nothing_else(tmp_path: Path) -> None:
    (tmp_path / "w.toml").write_text('listen = "127.0.0.1:18100"\n')
    text = GOOD.replace(
        '[worker]\nlisten = "127.0.0.1:18100"\npool_id = "test"',
        '[worker]\nsettings_file = "w.toml"\npool_id = "test"',
    )
    refused(write(tmp_path, text), r"holds nothing else")
    named = GOOD.replace(
        '[worker]\nlisten = "127.0.0.1:18100"\npool_id = "test"',
        '[worker]\nsettings_file = "w.toml"',
    )
    loaded = unit.load(write(tmp_path, named))
    worker = loaded.services["worker"]
    assert worker.settings_file == tmp_path / "w.toml"
    assert worker.inline is None
    assert worker.listen == "127.0.0.1:18100"
    assert loaded.settings(worker) == tmp_path / "w.toml"
    # Its address comes from the file it names, so a file without one is refused.
    (tmp_path / "w.toml").write_text("pool_id = 'x'\n")
    refused(write(tmp_path, named), r"no listen")
    # ... unless the file is not read yet, as when the launcher checks its config.
    assert unit.load(write(tmp_path, named), read=False).services["worker"].listen is None
    (tmp_path / "w.toml").unlink()
    refused(write(tmp_path, named), r"no unit config|no config|cannot read")
    assert unit.load(write(tmp_path, named), read=False).problems()  # the file is not there


def test_a_relative_settings_file_is_against_the_config_s_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "etc"
    site.mkdir()
    (site / "w.toml").write_text('listen = "127.0.0.1:18100"\n')
    text = GOOD.replace(
        '[worker]\nlisten = "127.0.0.1:18100"\npool_id = "test"',
        '[worker]\nsettings_file = "./w.toml"',
    )
    path = write(site, text)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    loaded = unit.load(path)
    assert loaded.services["worker"].settings_file == site / "w.toml"


def test_only_runs_one_service_and_reads_only_its_file(tmp_path: Path) -> None:
    (tmp_path / "w.toml").write_text('listen = "0.0.0.0:8100"\n')
    text = (
        '[worker]\nsettings_file = "w.toml"\n'
        '[middleware]\nsettings_file = "absent-m.toml"\n'
        '[cache]\nsettings_file = "absent-c.toml"\n'
    )
    path = write(tmp_path, text)
    loaded = unit.load(path, only=["worker"])
    assert list(loaded.services) == ["worker"]
    assert loaded.sections == ("cache", "middleware", "worker")
    refused(path, r"cannot read|no unit config")  # all three: the other files are missing
    refused(
        write(tmp_path, GOOD.split("[cache]")[0]), r"no \[cache\] section to run", only=["cache"]
    )


def test_the_commands_and_files_are_checked_before_any_starts(tmp_path: Path) -> None:
    loaded = unit.load(write(tmp_path, GOOD + f'\n[unit]\nbin_dir = "{tmp_path / "bin"}"\n'))
    assert sorted(loaded.problems()) == [
        f"no sandbox-science-package-cache in {tmp_path / 'bin'}",
        f"no sandbox-science-policy-middleware in {tmp_path / 'bin'}",
        f"no sandbox-science-worker in {tmp_path / 'bin'}",
    ]
    with pytest.raises(LauncherError, match="cannot start"):
        asyncio.run(unit.run(loaded))


def test_part_runs_the_services_own_command_on_a_settings_file(tmp_path: Path) -> None:
    named = '[unit]\nbin_dir = "bin"\nlog_level = "warning"\n' + GOOD
    loaded = unit.load(write(tmp_path, named))
    worker = loaded.part(loaded.services["worker"])
    assert worker.argv == [
        str(tmp_path / "bin/sandbox-science-worker"),
        "serve",
        "--settings",
        str(tmp_path / ".unit-worker.toml"),
        "--log-level",
        "warning",
    ]
    assert worker.address == "127.0.0.1:18100"
    assert worker.required
    assert not worker.restart
    cache = loaded.part(loaded.services["cache"])
    assert cache.argv[0].endswith("sandbox-science-package-cache")
    assert (
        loaded.part(loaded.services["middleware"])
        .argv[0]
        .endswith("sandbox-science-policy-middleware")
    )


def test_inline_sections_are_written_private_and_read_back_as_the_same_settings(
    tmp_path: Path,
) -> None:
    text = GOOD.replace(
        'pool_id = "test"', 'pool_id = "test"\n[worker.openshell]\ncli = "/bin/true"'
    )
    loaded = unit.load(write(tmp_path, text + f'\n[unit]\nruntime_dir = "{tmp_path / "run"}"\n'))
    written = loaded.materialize()
    assert sorted(path.name for path in written) == [
        ".unit-cache.toml",
        ".unit-middleware.toml",
        ".unit-worker.toml",
    ]
    for path in written:
        assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "run").stat().st_mode & 0o077 == 0
    assert tomllib.loads((tmp_path / "run/.unit-worker.toml").read_text()) == {
        "listen": "127.0.0.1:18100",
        "pool_id": "test",
        "openshell": {"cli": "/bin/true"},
    }


def test_materialize_says_where_to_write_when_the_directory_is_not_writable(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    loaded = unit.load(write(tmp_path, GOOD + f'\n[unit]\nruntime_dir = "{blocker / "run"}"\n'))
    with pytest.raises(LauncherError, match="set \\[unit\\] runtime_dir"):
        loaded.materialize()


def free_address() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{probe.getsockname()[1]}"


def listening(unit_config: UnitConfig) -> list[socket.socket]:
    sockets = []
    for service in unit_config.services.values():
        address = service.address
        assert address is not None
        listener = socket.socket()
        listener.bind(address)
        listener.listen()
        sockets.append(listener)
    return sockets


def two_services(tmp_path: Path) -> UnitConfig:
    text = f'[worker]\nlisten = "{free_address()}"\n[middleware]\nlisten = "{free_address()}"\n'
    return unit.load(write(tmp_path, text))


def test_health_is_all_services_answering(tmp_path: Path) -> None:
    loaded = two_services(tmp_path)
    assert [ok for _, _, ok in unit.health(loaded)] == [False, False]
    sockets = listening(loaded)
    try:
        assert [ok for _, _, ok in unit.health(loaded)] == [True, True]
        sockets.pop().close()  # the worker (the last started) stops answering
        states = unit.health(loaded)
        assert [(name, ok) for name, _, ok in states] == [("middleware", True), ("worker", False)]
        assert "worker on " in unit.describe(states)
        assert "is silent" in unit.describe(states)
    finally:
        for listener in sockets:
            listener.close()


async def test_a_launcher_learns_of_a_unit_that_went_silent_not_of_a_blip(tmp_path: Path) -> None:
    loaded = two_services(tmp_path)
    stop = asyncio.Event()
    # Nothing answers: unhealthy after the strikes.
    reason = await asyncio.wait_for(
        unit.unhealthy(loaded, stop, interval=0.01, strikes=2), timeout=30
    )
    assert reason is not None
    assert "no longer healthy" in reason
    assert "is silent" in reason
    # Healthy and then stopped: no reason.
    sockets = listening(loaded)
    try:
        waiting = asyncio.ensure_future(unit.unhealthy(loaded, stop, interval=0.01, strikes=2))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        stop.set()
        assert await asyncio.wait_for(waiting, timeout=30) is None
    finally:
        for listener in sockets:
            listener.close()


def test_check_and_status_report_each_service(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in unit.COMMANDS.values():
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    loaded_text = (
        f'[unit]\nbin_dir = "bin"\n[worker]\nlisten = "{free_address()}"\n'
        f'[middleware]\nlisten = "{free_address()}"\n'
    )
    path = write(tmp_path, loaded_text)

    def run(command: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "sandbox_science.launcher.unit",
                command,
                "--config",
                str(path),
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    checked = run("check")
    assert checked.returncode == 0, checked.stderr
    assert "middleware: " in checked.stdout
    assert "worker: " in checked.stdout
    assert checked.stdout.rstrip().endswith("valid; 2 services")
    silent = run("status")
    assert silent.returncode == 1
    assert silent.stdout.count("is silent") == 2
    only = run("status", "--only", "worker")
    assert only.stdout.count("is silent") == 1
    path.write_text(loaded_text.replace("[middleware]", "[middlewares]"))
    broken = run("check")
    assert broken.returncode == 1
    assert "no such section: middlewares" in broken.stderr
