# SPDX-License-Identifier: MIT
"""``scripts/personal_up.sh`` and ``personal_down.sh`` against stand-ins for systemd and the
tools they call, so the units they define are checked without starting anything.

The stand-ins (``systemd-run``, ``systemctl``, ``ss``, ``journalctl``, the OpenShell
binaries and the repository's ``.venv/bin`` commands) write what they were asked to a log
in ``FAKE_STATE``, and keep the units they started "active" there.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts.openshell_version import OPENSHELL_VERSION
from sandbox_science.launcher import unit

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"

FAKES = {
    "systemd-run": r"""
import json, os, sys
state = os.environ["FAKE_STATE"]
args = sys.argv[1:]
name, props = "", []
i = 0
while i < len(args) and args[i].startswith("-"):
    arg = args[i]
    if arg.startswith("--unit="):
        name = arg.removeprefix("--unit=")
    elif arg in ("-p", "--setenv"):
        i += 1
        props.append(args[i])
    i += 1
with open(f"{state}/log.jsonl", "a") as log:
    entry = {"call": "systemd-run", "unit": name, "props": props, "argv": args[i:]}
    log.write(json.dumps(entry) + "\n")
open(f"{state}/active-{name}", "w").close()
if name.endswith("-gateway"):
    with open(f"{state}/ports", "a") as ports:
        ports.write("17680\n")
""",
    "systemctl": r"""
import json, os, sys
state = os.environ["FAKE_STATE"]
args = [a for a in sys.argv[1:] if a not in ("--user", "-q")]
if args[0] == "is-active":
    on = os.path.exists(f"{state}/active-{args[1]}")
    if "-q" not in sys.argv:
        print("active" if on else "inactive")
    sys.exit(0 if on else 3)
if args[0] == "show":
    print("invocation")
if args[0] == "stop":
    for name in args[1:]:
        with open(f"{state}/log.jsonl", "a") as log:
            log.write(json.dumps({"call": "stop", "unit": name}) + "\n")
        if os.path.exists(f"{state}/active-{name}"):
            os.remove(f"{state}/active-{name}")
""",
    "ss": r"""
import os
path = os.environ["FAKE_STATE"] + "/ports"
if os.path.exists(path):
    for port in open(path).read().split():
        print(f"LISTEN 0 128 127.0.0.1:{port} 0.0.0.0:*")
""",
    "journalctl": "print('Sandbox Science is running.')",
    "openssl": "import sys; sys.exit(1)  # the tests leave nothing for it to make",
}
OPENSHELL = {
    "openshell-gateway": "pass",
    "openshell": r"""
import os, sys
from sandbox_science.contracts.openshell_version import OPENSHELL_VERSION_OUTPUT
if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_OPENSHELL_SAYS", OPENSHELL_VERSION_OUTPUT))
if sys.argv[1:3] == ["gateway", "list"]:
    print("openshell 127.0.0.1:17680")
if sys.argv[1:3] == ["provider", "list"]:
    print("github-readonly")
""",
}
COMMANDS = {
    "sandbox-science": r"""
import json, os, sys
state = os.environ["FAKE_STATE"]
with open(f"{state}/log.jsonl", "a") as log:
    log.write(json.dumps({"call": "sandbox-science", "argv": sys.argv[1:]}) + "\n")
if sys.argv[1] == "down" and "--keep-sandboxes" not in sys.argv:
    # The real one stops the launcher, and with it the unit that is the launcher's.
    launcher = state + "/active-ss-personal-launcher"
    if os.path.exists(launcher):
        os.remove(launcher)
if sys.argv[1] == "prepare":
    settings = os.environ["FAKE_WORKER_SETTINGS"]
    with open(settings, "w") as out:
        out.write(os.environ.get("FAKE_WORKER_TEXT", 'listen = "127.0.0.1:8310"\n'))
    print("worker_settings=" + settings)
    print("unit=" + os.environ.get("FAKE_CONFIG_UNIT", "none"))
""",
    "sandbox-science-unit": r"""
import json, os, sys
state = os.environ["FAKE_STATE"]
with open(f"{state}/log.jsonl", "a") as log:
    log.write(json.dumps({"call": "sandbox-science-unit", "argv": sys.argv[1:]}) + "\n")
""",
}


# scripts/backup_stores.py, which personal_backup.sh runs: the store checks and the snapshot
# are not made, only logged, and answer as FAKE_VERSIONS_STATUS and FAKE_BACKUP_STATUS say.
BACKUP_STORES = r"""
import json, os, sys
state = os.environ["FAKE_STATE"]
with open(f"{state}/log.jsonl", "a") as log:
    log.write(json.dumps({"call": "backup_stores", "argv": sys.argv[1:]}) + "\n")
command = sys.argv[1]
if command == "versions":
    print("control/control.sqlite3: version 1, this checkout expects 2 (would migrate)")
    sys.exit(int(os.environ.get("FAKE_VERSIONS_STATUS", "0")))
if command == "backup":
    if os.environ.get("FAKE_BACKUP_STATUS", "0") != "0":
        print("backup_stores: no space left", file=sys.stderr)
        sys.exit(int(os.environ["FAKE_BACKUP_STATUS"]))
    print("/backups/20261023T000000Z-before-migration")
"""


def program(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Instance:
    """A repository checkout, a home and a systemd, all stand-ins, for the two scripts."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.root = self.home / ".local/state/sandbox-science-personal"
        self.openshell = self.root / "openshell"
        self.repo = tmp_path / "repo"
        self.state = tmp_path / "fake-state"
        self.bin = tmp_path / "fake-bin"
        self.os_bin = tmp_path / "openshell-bin"
        self.config = self.home / ".config/sandbox-science/personal.toml"
        self.worker_settings = self.root / "launcher/worker/worker.toml"
        for directory in (self.state, self.bin, self.os_bin, self.repo / ".venv/bin"):
            directory.mkdir(parents=True)
        for name, body in FAKES.items():
            program(self.bin, name, body)
        for name, body in OPENSHELL.items():
            program(self.os_bin, name, body)
        for name, body in COMMANDS.items():
            program(self.repo / ".venv/bin", name, body)
        # The checkout's interpreter, which reads the config and the policy before anything
        # starts: this one, with the policy library's YAML.
        python = self.repo / ".venv/bin/python"
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        (self.repo / "examples").mkdir()
        (self.repo / "examples/policy.yaml").write_text(
            "mcp:\n  limits: {max_argument_bytes: 1Ki}\n"
        )
        (self.repo / "scripts").mkdir()
        program(self.repo / "scripts", "backup_stores.py", BACKUP_STORES)
        # What the script makes once and keeps, already there: nothing needs openssl.
        for path in (
            self.openshell / "state/openshell/tls/ca.crt",
            self.openshell / "middleware/tls/middleware.pem",
            self.openshell / "middleware/pool-ceiling.yaml",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# test\n")
        self.worker_settings.parent.mkdir(parents=True)
        self.config.parent.mkdir(parents=True)
        self.config.write_text("# the launcher's config, which the stand-in `prepare` ignores\n")

    @property
    def unit_config(self) -> Path:
        return self.openshell / "unit.toml"

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "R": str(self.repo),
            "OS_BIN": str(self.os_bin),
            "FAKE_STATE": str(self.state),
            "FAKE_WORKER_SETTINGS": str(self.worker_settings),
            "FAKE_CONFIG_UNIT": str(self.unit_config),
            **extra,
        }

    def run(self, script: str, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPTS / script), *args],
            capture_output=True, text=True, env=self.env(**extra), check=False, timeout=60,
        )  # fmt: skip

    def log(self) -> list[dict[str, Any]]:
        path = self.state / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def snapshot_calls(self) -> list[list[str]]:
        """What the scripts asked backup_stores.py to do, one argv each."""
        return [item["argv"] for item in self.log() if item["call"] == "backup_stores"]

    def started(self) -> list[str]:
        return [str(item["unit"]) for item in self.log() if item["call"] == "systemd-run"]

    def stopped(self) -> list[str]:
        return [str(item["unit"]) for item in self.log() if item["call"] == "stop"]

    def run_unit(self, name: str) -> None:
        (self.state / f"active-ss-personal-{name}").touch()


@pytest.fixture
def instance(tmp_path: Path) -> Instance:
    return Instance(tmp_path)


def test_up_starts_one_unit_then_the_gateway_then_the_launcher(instance: Instance) -> None:
    done = instance.run("personal_up.sh")
    assert done.returncode == 0, done.stdout + done.stderr
    # No separate middleware or cache: the unit holds them with the worker.
    assert instance.started() == ["ss-personal-unit", "ss-personal-gateway", "ss-personal-launcher"]
    called = [item for item in instance.log() if item["call"] == "sandbox-science"]
    assert called[0]["argv"] == ["prepare", "--config", str(instance.config)]
    assert "ss-personal-unit (cache, middleware, worker) and ss-personal-gateway" in done.stdout


def test_up_checks_the_store_versions_and_takes_no_snapshot_when_none_would_migrate(
    instance: Instance,
) -> None:
    done = instance.run("personal_up.sh")
    assert done.returncode == 0, done.stdout + done.stderr
    assert [argv[0] for argv in instance.snapshot_calls()] == ["versions"]
    assert "ss-personal-unit" in instance.started()


def test_up_snapshots_before_starting_anything_when_a_store_would_migrate(
    instance: Instance,
) -> None:
    done = instance.run("personal_up.sh", FAKE_VERSIONS_STATUS="3")
    assert done.returncode == 0, done.stdout + done.stderr
    calls = instance.snapshot_calls()
    assert [argv[0] for argv in calls] == ["versions", "backup", "verify"]
    backup = calls[1]
    assert backup[backup.index("--label") + 1] == "before-migration"
    assert "--live" not in backup
    assert "taking a snapshot first" in done.stderr
    # The snapshot is complete before the first unit starts.
    order = [item["call"] for item in instance.log()]
    last_snapshot = max(i for i, call in enumerate(order) if call == "backup_stores")
    assert last_snapshot < order.index("systemd-run")
    assert instance.started() == ["ss-personal-unit", "ss-personal-gateway", "ss-personal-launcher"]


def test_up_starts_nothing_when_the_snapshot_fails(instance: Instance) -> None:
    done = instance.run("personal_up.sh", FAKE_VERSIONS_STATUS="3", FAKE_BACKUP_STATUS="1")
    assert done.returncode == 1
    assert "the snapshot failed, so nothing was started" in done.stderr
    assert instance.started() == []
    assert not instance.unit_config.exists()


def test_up_starts_nothing_when_a_store_is_newer_than_the_checkout(instance: Instance) -> None:
    done = instance.run("personal_up.sh", FAKE_VERSIONS_STATUS="1")
    assert done.returncode == 1
    assert "nothing was started" in done.stderr
    assert [argv[0] for argv in instance.snapshot_calls()] == ["versions"]
    assert instance.started() == []


@pytest.mark.parametrize("how", ["flag", "environment"])
def test_the_snapshot_can_be_skipped_explicitly(instance: Instance, how: str) -> None:
    if how == "flag":
        done = instance.run("personal_up.sh", "--skip-snapshot", FAKE_VERSIONS_STATUS="3")
    else:
        done = instance.run(
            "personal_up.sh", SS_PERSONAL_SKIP_SNAPSHOT="1", FAKE_VERSIONS_STATUS="3"
        )
    assert done.returncode == 0, done.stdout + done.stderr
    assert instance.snapshot_calls() == []
    assert "snapshot: skipped" in done.stderr
    assert instance.started() == ["ss-personal-unit", "ss-personal-gateway", "ss-personal-launcher"]


def test_a_start_refused_for_other_reasons_takes_no_snapshot(instance: Instance) -> None:
    done = instance.run("personal_up.sh", FAKE_CONFIG_UNIT="none", FAKE_VERSIONS_STATUS="3")
    assert done.returncode == 1
    assert instance.snapshot_calls() == []


def test_the_unit_is_defined_to_stop_its_services_in_order_and_open_many_files(
    instance: Instance,
) -> None:
    assert instance.run("personal_up.sh").returncode == 0
    (definition,) = [
        item
        for item in instance.log()
        if item["call"] == "systemd-run" and item["unit"].endswith("-unit")
    ]
    # Only the unit's own process gets the stop signal, so it stops worker, then middleware,
    # then cache; the rest of the cgroup is killed only after the timeout.
    assert "KillMode=mixed" in definition["props"]
    assert "TimeoutStopSec=150" in definition["props"]
    assert "LimitNOFILE=65536" in definition["props"]
    assert definition["argv"] == [
        str(instance.repo / ".venv/bin/sandbox-science-unit"),
        "serve",
        "--config",
        str(instance.unit_config),
    ]
    launcher = next(
        item
        for item in instance.log()
        if item["call"] == "systemd-run" and item["unit"].endswith("-launcher")
    )
    assert "KillMode=mixed" in launcher["props"]


def test_the_units_config_is_a_valid_one_with_the_launchers_worker_settings(
    instance: Instance,
) -> None:
    assert instance.run("personal_up.sh").returncode == 0
    path = instance.unit_config
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = unit.load(path)  # the real loader
    assert list(loaded.services) == ["cache", "middleware", "worker"]
    assert loaded.services["worker"].settings_file == instance.worker_settings
    assert loaded.services["middleware"].listen == "127.0.0.1:50061"
    assert loaded.services["cache"].listen == "127.0.0.1:8390"
    assert loaded.bin_dir == instance.repo / ".venv/bin"
    middleware = loaded.services["middleware"].inline
    cache = loaded.services["cache"].inline
    assert middleware is not None
    assert cache is not None
    assert middleware["tls"] == {
        "certificate": str(instance.openshell / "middleware/tls/middleware.pem"),
        "private_key": str(instance.openshell / "middleware/tls/middleware-key.pem"),
    }
    assert cache["public_url"] == "http://host.openshell.internal:8390"
    assert {"pypi", "conda", "cargo", "go", "npm"} <= set(cache)
    # The settings are stated once, here: the earlier separate files are no longer written.
    assert not (instance.openshell / "middleware/middleware.toml").exists()
    assert not (instance.openshell / "cache.toml").exists()
    # What the gateway registers is the middleware the unit serves.
    gateway = (instance.openshell / "gateway.toml").read_text()
    assert 'grpc_endpoint     = "https://127.0.0.1:50061"' in gateway


def test_ports_come_from_the_environment_into_the_units_config(instance: Instance) -> None:
    done = instance.run(
        "personal_up.sh",
        "--openshell-only",
        SS_PERSONAL_MIDDLEWARE_PORT="51061",
        SS_PERSONAL_CACHE_PORT="9390",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    loaded = unit.load(instance.unit_config)
    assert loaded.services["middleware"].listen == "127.0.0.1:51061"
    assert loaded.services["cache"].listen == "127.0.0.1:9390"


def test_openshell_only_starts_the_unit_and_the_gateway_but_not_the_launcher(
    instance: Instance,
) -> None:
    assert instance.run("personal_up.sh", "--openshell-only").returncode == 0
    assert instance.started() == ["ss-personal-unit", "ss-personal-gateway"]


def test_a_config_that_does_not_name_the_unit_is_refused_before_anything_starts(
    instance: Instance,
) -> None:
    for said in ("none", str(instance.root / "elsewhere.toml")):
        done = instance.run("personal_up.sh", FAKE_CONFIG_UNIT=said)
        assert done.returncode == 1
        assert f'under [worker], unit = "{instance.unit_config}"' in done.stderr
        assert instance.started() == []


def test_a_config_is_needed_even_to_start_only_openshell(instance: Instance) -> None:
    instance.config.unlink()
    done = instance.run("personal_up.sh", "--openshell-only")
    assert done.returncode == 1
    assert "no config at" in done.stderr
    assert instance.started() == []


def test_a_config_with_the_retired_mcp_proxy_is_refused_before_anything_starts(
    instance: Instance,
) -> None:
    instance.config.write_text('[mcp_proxy]\nservers = ["papers"]\n')
    for args in ((), ("--openshell-only",)):
        done = instance.run("personal_up.sh", *args)
        assert done.returncode == 1
        assert "has [mcp_proxy], which the launcher refuses" in done.stderr
        assert "migrate-mcp-config" in done.stderr
        assert instance.started() == []
        assert not instance.unit_config.exists()


@pytest.mark.parametrize(
    "proxy", ["{worker_access: true}", "{worker_tools: [search]}", "{worker_access: 'yes'}"]
)
@pytest.mark.parametrize("which", ["example", "config"])
def test_a_policy_giving_research_code_mcp_is_refused_before_anything_starts(
    instance: Instance, proxy: str, which: str
) -> None:
    body = f"mcp:\n  proxy: {proxy}\n"
    if which == "example":
        (instance.repo / "examples/policy.yaml").write_text(body)
        named = instance.repo / "examples/policy.yaml"
    else:
        named = instance.config.parent / "personal-policy.yaml"
        named.write_text(body)
        instance.config.write_text('[worker]\npolicy = "personal-policy.yaml"\n')
    done = instance.run("personal_up.sh")
    assert done.returncode == 1
    assert str(named) in done.stdout
    assert "sets mcp.proxy.worker_access or worker_tools, which retired" in done.stderr
    assert instance.started() == []


def test_a_policy_with_the_defaults_of_the_retired_keys_starts(instance: Instance) -> None:
    named = instance.config.parent / "personal-policy.yaml"
    named.write_text("mcp:\n  proxy: {worker_access: false, worker_tools: inherit}\n")
    instance.config.write_text('[worker]\npolicy = "personal-policy.yaml"\n')
    done = instance.run("personal_up.sh")
    assert done.returncode == 0, done.stderr


def test_a_unit_of_the_earlier_layout_must_be_stopped_first(instance: Instance) -> None:
    instance.run_unit("middleware")
    done = instance.run("personal_up.sh")
    assert done.returncode == 1
    assert "ss-personal-middleware, a unit of the earlier layout, runs" in done.stderr
    assert instance.started() == []


def test_a_busy_port_stops_the_start_before_the_unit(instance: Instance) -> None:
    (instance.state / "ports").write_text("8390\n")
    done = instance.run("personal_up.sh")
    assert done.returncode == 1
    assert "port 8390 is busy" in done.stderr
    assert instance.started() == []


def test_a_kept_unit_and_gateway_are_reused_when_the_worker_settings_are_the_same(
    instance: Instance,
) -> None:
    assert instance.run("personal_up.sh", "--openshell-only").returncode == 0
    before = instance.started()
    again = instance.run("personal_up.sh")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "ss-personal-unit and ss-personal-gateway already run" in again.stdout
    assert instance.started() == [*before, "ss-personal-launcher"]


def test_a_kept_unit_with_older_worker_settings_is_not_reused(instance: Instance) -> None:
    assert instance.run("personal_up.sh", "--openshell-only").returncode == 0
    before = instance.started()
    changed = instance.run("personal_up.sh", FAKE_WORKER_TEXT='listen = "127.0.0.1:8311"\n')
    assert changed.returncode == 1
    assert "older worker settings" in changed.stderr
    assert "personal_down.sh" in changed.stderr
    assert instance.started() == before


def test_only_one_of_the_two_running_is_refused(instance: Instance) -> None:
    instance.run_unit("gateway")
    done = instance.run("personal_up.sh")
    assert done.returncode == 1
    assert "only one of ss-personal-unit and ss-personal-gateway runs" in done.stderr


def test_a_second_launcher_is_refused(instance: Instance) -> None:
    instance.run_unit("launcher")
    done = instance.run("personal_up.sh")
    assert done.returncode == 1
    assert "ss-personal-launcher is already running" in done.stderr


@pytest.mark.parametrize("prefix", ["openshell-lab", "openshell-lab-x"])
def test_the_labs_prefix_is_refused(instance: Instance, prefix: str) -> None:
    done = instance.run("personal_up.sh", SS_PERSONAL_UNIT=prefix)
    assert done.returncode == 2
    assert instance.started() == []


def test_down_stops_the_launcher_through_its_command_then_the_unit_then_the_gateway(
    instance: Instance,
) -> None:
    for name in ("unit", "gateway", "launcher"):
        instance.run_unit(name)
    done = instance.run("personal_down.sh")
    assert done.returncode == 0, done.stdout + done.stderr
    calls = [item["argv"] for item in instance.log() if item["call"] == "sandbox-science"]
    assert calls == [["down", "--config", str(instance.config)]]
    # The unit goes first, as the worker did with the launcher; the earlier layout's units
    # are named too, in case one still runs.
    assert instance.stopped() == [
        "ss-personal-unit",
        "ss-personal-gateway",
        "ss-personal-middleware",
        "ss-personal-cache",
    ]
    assert "ss-personal-launcher: inactive" in done.stdout
    assert "ss-personal-unit: inactive" in done.stdout
    assert "ss-personal-gateway: inactive" in done.stdout


def test_down_keeping_the_sandboxes_leaves_the_unit_and_the_gateway_running(
    instance: Instance,
) -> None:
    for name in ("unit", "gateway"):
        instance.run_unit(name)
    done = instance.run("personal_down.sh", "--keep-sandboxes")
    assert done.returncode == 0, done.stdout + done.stderr
    calls = [item["argv"] for item in instance.log() if item["call"] == "sandbox-science"]
    assert calls == [["down", "--config", str(instance.config), "--keep-sandboxes"]]
    assert "ss-personal-unit" not in instance.stopped()
    assert "ss-personal-gateway: active" in done.stdout
    assert "ss-personal-unit: active" in done.stdout


def test_a_bad_option_is_a_usage_error(instance: Instance) -> None:
    assert instance.run("personal_up.sh", "--bogus").returncode == 2
    assert instance.run("personal_down.sh", "--bogus").returncode == 2


@pytest.mark.parametrize("says", ["openshell 0.1.3", "openshell 0.1.50", ""])
def test_up_starts_nothing_on_another_release_of_openshell(instance: Instance, says: str) -> None:
    done = instance.run("personal_up.sh", FAKE_OPENSHELL_SAYS=says)
    assert done.returncode == 1
    assert f"needs OpenShell {OPENSHELL_VERSION}" in done.stderr
    assert instance.started() == []


def test_the_openshell_directory_defaults_to_the_pinned_releases(instance: Instance) -> None:
    env = instance.env()
    del env["OS_BIN"]
    done = subprocess.run(
        ["bash", str(SCRIPTS / "personal_up.sh")],
        capture_output=True, text=True, env=env, check=False, timeout=60,
    )  # fmt: skip
    # No such release is installed under the test's home: the message names where it looked.
    assert done.returncode == 1
    assert f"{instance.home}/.local/opt/openshell-{OPENSHELL_VERSION}/bin/openshell" in done.stderr
    assert instance.started() == []
