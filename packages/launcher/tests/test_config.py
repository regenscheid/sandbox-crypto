# SPDX-License-Identifier: MIT
"""The launcher's config: paths against the file, loopback only, consistent projects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.config import LauncherError, load_config


def test_paths_are_against_the_file_and_home(make_config: Any, home: Path) -> None:
    path = make_config({"state_dir": "~/sandbox-state"})
    config = load_config(path)
    site = path.parent
    assert config.state_dir == home / "sandbox-state"
    assert config.model.api_key_file == site / "model.key"
    assert config.worker.policy == site / "policy.yaml"
    assert config.workspace.static == site / "web/dist"
    assert config.openshell.cli == str(site / "bin/openshell")
    assert config.openshell.env == {"XDG_CONFIG_HOME": str(home / "openshell-config")}
    environments = config.openshell.environments
    assert environments.policy_store == site / "policy-store"
    assert environments.targets == site / "targets.yaml"
    # Keys the launcher does not name pass on to the worker as they are.
    assert config.openshell.model_dump()["isolation_tier"] == 3
    assert config.workspace.preview() == "127.0.0.1:18201"


def test_ports_are_plain_numbers_and_loopback_gateways_are_ip_addresses(
    make_config: Any,
) -> None:
    config = load_config(
        make_config(
            {
                "worker": {"listen": "127.0.0.1:08100"},
                "model": {"url": "http://[::1]:4000/v1"},
                "openshell": {"env": {"XDG_STATE_HOME": "lab/state", "TELEMETRY": "false"}},
            }
        )
    )
    assert config.worker.listen == "127.0.0.1:8100"
    assert config.model.url == "http://[::1]:4000/v1"
    # A value with a / is a path, against the file, so every part reaches one gateway.
    site = config.worker.policy.parent
    assert config.openshell.env["XDG_STATE_HOME"] == str(site / "lab/state")
    assert config.openshell.env["TELEMETRY"] == "false"
    loopback = load_config(make_config({"model": {"url": "http://127.0.0.1:4000/v1"}}))
    assert loopback.model.url == "http://127.0.0.1:4000/v1"


def test_a_command_name_is_left_to_the_path(make_config: Any) -> None:
    config = load_config(make_config({"openshell": {"cli": "openshell"}}))
    assert config.openshell.cli == "openshell"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"worker": {"listen": "0.0.0.0:8100"}}, "is not on loopback"),
        ({"workspace": {"listen": "localhost:8200"}}, "is not on loopback"),
        ({"workspace": {"preview_listen": "192.168.1.2:8201"}}, "is not on loopback"),
        ({"worker": {"listen": "127.0.0.1"}}, "is not host:port"),
        ({"workspace": {"listen": "127.0.0.1:18099"}}, "the worker and the workspace preview"),
        ({"model": {"url": "http://gateway.example/v1"}}, "http on a loopback IP address"),
        ({"model": {"url": "http://127.evil.example/v1"}}, "http on a loopback IP address"),
        ({"model": {"url": "http://127.0.0.1.nip.io/v1"}}, "http on a loopback IP address"),
        ({"model": {"url": "http://localhost:4000/v1"}}, "http on a loopback IP address"),
        ({"worker": {"listen": "127.0.0.1:\uff18\uff11\uff10\uff10"}}, "a port from 1 to 65535"),
        ({"workspace": {"listen": "127.0.0.1:65535"}}, "set preview_listen"),
        ({"workspace": {"preview_listen": "127.0.0.1:65536"}}, "a port from 1 to 65535"),
        ({"projects": [{"id": "crypto", "profile": "crypto-base"}]}, "is not one of"),
        (
            {"projects": [{"id": "a", "profile": "minimal"}, {"id": "a", "profile": "minimal"}]},
            "each project is listed once",
        ),
        ({"projects": [{"id": "a", "profile": "minimal", "sessions": ["s", "s"]}]}, "twice"),
        ({"projects": [{"id": "../a", "profile": "minimal"}]}, "projects.0.id"),
        ({"projects": []}, "projects"),
        ({"worker": {"token_file": "mine.token"}}, "worker.token_file"),
        ({"model": None}, "model"),
    ],
)
def test_a_config_that_cannot_work_is_refused_by_what_is_wrong(
    make_config: Any, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(LauncherError, match="is not a launcher config") as error:
        load_config(make_config(changes))
    assert message in str(error.value)


def test_an_absent_or_broken_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(LauncherError, match=r"no config file at .*QUICKSTART"):
        load_config(tmp_path / "missing.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("state_dir = \n")
    with pytest.raises(LauncherError, match="is not TOML"):
        load_config(broken)
