# SPDX-License-Identifier: MIT
"""The launcher's config: paths against the file, loopback only, consistent projects."""

from __future__ import annotations

import os
import subprocess
import sys
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
                "openshell": {
                    "env": {
                        "XDG_STATE_HOME": "lab/state",
                        "TELEMETRY": "false",
                        "OPENSHELL_GATEWAY": "https://127.0.0.1:17670/v1",
                    }
                },
            }
        )
    )
    assert config.worker.listen == "127.0.0.1:8100"
    assert config.model.url == "http://[::1]:4000/v1"
    # A value with a / is a path, against the file, so every part reaches one gateway.
    site = config.worker.policy.parent
    assert config.openshell.env["XDG_STATE_HOME"] == str(site / "lab/state")
    assert config.openshell.env["TELEMETRY"] == "false"
    assert config.openshell.env["OPENSHELL_GATEWAY"] == "https://127.0.0.1:17670/v1"  # a URL
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


def test_the_example_config_is_a_launcher_config(home: Path) -> None:
    example = Path(__file__).parents[3] / "examples" / "sandbox-science.toml"
    config = load_config(example)
    assert config.state_dir == home / ".local/state/sandbox-science"
    assert config.worker.policy == example.parent / "policy.yaml"
    assert config.worker.policy.is_file()
    assert config.workspace.static == example.parent / "../web/workspace/dist"
    assert config.workspace.preview() == "127.0.0.2:8201"
    assert config.openshell.model_dump()["environments"]["suspend_after"] == 3600.0
    assert config.run_service.math_checks == example.parent / "../web/workspace/dist-math"
    assert config.model.watcher == config.model.name == config.model.reviewer
    assert config.model.delegated == config.model.name
    (project,) = config.projects
    assert [(item.id, item.dedicated) for item in project.sessions] == [
        ("main", False),
        ("scratch", True),
    ]


def test_the_labs_config_needs_nothing_added_by_hand(home: Path, tmp_path: Path) -> None:
    """Lab 40, B7: the lab's config script left out the delegated alias and the tool
    index's ``index`` and ``about``, which the lab then added by hand."""
    script = Path(__file__).parents[3] / ".agents/labs/launcher_config.py"
    out = tmp_path / "lab" / "sandbox-science.toml"
    owner = tmp_path / "test-secret-cloudflare.env"  # only named: the script opens nothing
    done = subprocess.run(
        [
            sys.executable, "-I", str(script), str(out), "--state-dir", str(tmp_path / "state"),
            "--project", "lab41", "--mcp-proxy", "sandbox-science/mcp-proxy:test",
            "--provider-env-file", str(owner),
        ],
        capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(home)},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert out.stat().st_mode & 0o777 == 0o600
    config = load_config(out)
    assert config.model.delegated == config.model.reviewer == config.model.name
    proxy = config.mcp_proxy
    assert proxy is not None
    assert proxy.provider_env_file == owner
    assert not owner.exists()
    listed = ("attack-catalog-mcp", "e-print-mcp", "nist-csrc-mcp", "paper-search-mcp")
    assert proxy.index == tuple(f"crypto-research-mcp/{n}" for n in (*listed, "web-fetch-mcp"))
    assert len(proxy.about) == 7
    assert proxy.about["crypto-research-mcp/web-fetch-mcp"] == (
        "fetch a web page, or its links, as text"
    )
    (project,) = config.projects
    assert project.id == "lab41"


def test_the_examples_mcp_proxy_works_uncommented(home: Path, tmp_path: Path) -> None:
    example = Path(__file__).parents[3] / "examples" / "sandbox-science.toml"
    lines = example.read_text().splitlines()
    start = lines.index("# [mcp_proxy]")
    uncommented = [line.removeprefix("# ").removeprefix("#") for line in lines[start:]]
    path = tmp_path / "sandbox-science.toml"
    path.write_text("\n".join([*lines[:start], *uncommented]) + "\n")
    proxy = load_config(path).mcp_proxy
    assert proxy is not None
    assert proxy.providers == ("crypto-research-mcp",)
    assert proxy.credentials["crypto-research-mcp.CF_ACCESS_CLIENT_SECRET"] == (
        "CF_ACCESS_CLIENT_SECRET"
    )
    # Only named: loading the config opens nothing there.
    assert proxy.provider_env_file == Path("/path/to/the/owners/cloudflare.env")
    assert proxy.provider_profile == tmp_path / "openshell/profiles/cloudflare-access.yaml"
    # The tool index (lab 39, B5): what each server behind the portal is for.
    listed = ("attack-catalog-mcp", "e-print-mcp", "nist-csrc-mcp", "paper-search-mcp")
    assert proxy.index == tuple(f"crypto-research-mcp/{n}" for n in (*listed, "web-fetch-mcp"))
    assert proxy.about["crypto-research-mcp/e-print-mcp"].startswith("the IACR Cryptology ePrint")
    assert len(proxy.about) == 7


MCP = {"image": "sandbox-science/mcp-proxy:abc1234", "providers": ["p"], "servers": ["s"]}


def test_the_mcp_proxy_defaults_to_the_only_project(make_config: Any) -> None:
    config = load_config(make_config({"mcp_proxy": MCP}))
    assert config.mcp_project() == "crypto"
    assert config.mcp_proxy is not None
    assert config.mcp_proxy.sandbox == "ss-mcp-proxy"
    assert config.addresses()["MCP proxy's forward"] == "127.0.0.1:18080"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"projects": [{"id": "a", "profile": "minimal"}, {"id": "b", "profile": "minimal"}]},
            "mcp_proxy.project names the project",
        ),
        ({"mcp_proxy": {"project": "other"}}, "is not a project"),
        ({"mcp_proxy": {"sandbox": "a-name-far-too-long-for-it"}}, "mcp_proxy.sandbox"),
        ({"mcp_proxy": {"listen": "127.0.0.1:18100"}}, "the worker and the MCP proxy's forward"),
        ({"mcp_proxy": {"servers": []}}, "mcp_proxy.servers"),
        (
            {"mcp_proxy": {"providers": ["crypto-research-mcp"], "provider_env_file": "a.env"}},
            "mcp_proxy.provider_env_file needs provider_profile",
        ),
        (
            {"mcp_proxy": {"provider_env_file": "a.env", "provider_profile": "a.yaml"}},
            "registers the provider crypto-research-mcp, which mcp_proxy.providers must name",
        ),
    ],
)
def test_an_mcp_proxy_that_cannot_work_is_refused(
    make_config: Any, changes: dict[str, Any], message: str
) -> None:
    proxy = {**MCP, **changes.get("mcp_proxy", {})}
    with pytest.raises(LauncherError) as error:
        load_config(make_config({**changes, "mcp_proxy": proxy}))
    assert message in str(error.value)


def test_sessions_are_names_or_tables_with_dedicated(make_config: Any) -> None:
    config = load_config(
        make_config(
            {
                "projects": [
                    {
                        "id": "crypto",
                        "profile": "minimal",
                        "sessions": ["a", {"id": "c", "dedicated": True}],
                    }
                ]
            }
        )
    )
    (project,) = config.projects
    assert [(session.id, session.dedicated) for session in project.sessions] == [
        ("a", False),
        ("c", True),
    ]
    with pytest.raises(LauncherError, match="lists a session twice"):
        load_config(
            make_config(
                {
                    "projects": [
                        {
                            "id": "crypto",
                            "profile": "minimal",
                            "sessions": ["a", {"id": "a", "dedicated": True}],
                        }
                    ]
                }
            )
        )


def test_the_run_service_takes_a_watcher_and_the_math_checks(make_config: Any) -> None:
    config = load_config(
        make_config(
            {
                "model": {"watcher": "sandbox-orchestrator", "watcher_interval": 600},
                "run_service": {"math_checks": "dist-math", "node": "bin/node"},
            }
        )
    )
    site = config.worker.policy.parent
    model, service = config.model, config.run_service
    assert (model.watcher, model.watcher_interval) == ("sandbox-orchestrator", 600.0)
    assert (model.watcher_calls_per_hour, model.watcher_first_check) == (None, None)
    with pytest.raises(LauncherError, match="watcher_interval"):
        load_config(make_config({"model": {"watcher_interval": 0}}))
    assert service.math_checks == site / "dist-math"
    assert service.node == str(site / "bin/node")
    assert load_config(make_config()).run_service.node == "node"  # a name on PATH
