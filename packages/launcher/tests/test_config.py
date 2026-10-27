# SPDX-License-Identifier: MIT
"""The launcher's config: paths against the file, loopback only, consistent projects."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.config import LauncherConfig, LauncherError, load_config


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
        ({"workspace": {"preview_name": "viewer.example"}}, "preview_name is localhost"),
        ({"workspace": {"preview_name": "127.0.0.2"}}, "or 127.0.0.1, where the viewer"),
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
        (
            {"projects": [{"id": "a", "profile": "minimal", "skills": ["ss"]}]},
            "names 'ss', which skills.packs does not",
        ),
        ({"skills": {"packs": {"ss": {"git": "g", "archive": "a.tar"}}}}, "git or archive"),
        ({"skills": {"packs": {"ss": {}}}}, "git or archive"),
        ({"skills": {"packs": {"ss": {"archive": "a.tar", "ref": "v1"}}}}, "ref goes with git"),
        ({"skills": {"packs": {"builtin": {"git": "g"}}}}, "platform's own skills"),
        ({"skills": {"packs": {"Crypto": {"git": "g"}}}}, "skills.packs.Crypto"),
        ({"skills": {"embedding_model": ""}}, "skills.embedding_model"),
        ({"skills": {"embedding_model": "a" * 201}}, "an alias of at most 200 bytes"),
        ({"skills": {"metadata_model": "a" * 201}}, "an alias of at most 200 bytes"),
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
    assert config.workspace.preview() == "127.0.0.1:8201"
    assert config.workspace.preview_name is None  # the workspace's default, localhost
    # Idle suspension is off by default (D100), and the example leaves it off.
    assert config.openshell.model_dump()["environments"].get("suspend_after") is None
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
            "--project", "lab41", "--profile", "crypto-base", "--session", "a",
            "--mcp", "--provider-env-file", str(owner),
        ],
        capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(home)},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert out.stat().st_mode & 0o777 == 0o600
    config = load_config(out)
    assert config.model.delegated == config.model.reviewer == config.model.name
    direct = config.mcp_direct
    assert direct is not None
    assert direct.provider_env_file == owner
    assert not owner.exists()
    assert direct.servers["crypto-research-mcp"].credentials == {
        "CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID",
        "CF-Access-Client-Secret": "CF_ACCESS_CLIENT_SECRET",
    }
    listed = ("attack-catalog-mcp", "e-print-mcp", "nist-csrc-mcp", "paper-search-mcp")
    assert direct.index == tuple(f"crypto-research-mcp/{n}" for n in (*listed, "web-fetch-mcp"))
    assert len(direct.about) == 7
    assert direct.about["crypto-research-mcp/web-fetch-mcp"] == (
        "fetch a web page, or its links, as text"
    )
    (project,) = config.projects
    assert project.id == "lab41"
    # Lab 41: the project's profile and its one shared session, which the lab edited in.
    assert project.profile == "crypto-base"
    assert [(s.id, s.dedicated) for s in project.sessions] == [("a", False)]


def test_the_labs_config_sets_the_skills(home: Path, tmp_path: Path) -> None:
    """Lab 42, B8: the lab added [skills], its packs and the project's skills by hand."""
    script = Path(__file__).parents[3] / ".agents/labs/launcher_config.py"
    out = tmp_path / "lab" / "sandbox-science.toml"
    done = subprocess.run(
        [
            sys.executable, "-I", str(script), str(out), "--state-dir", str(tmp_path / "state"),
            "--project", "lab42",
            "--skills-pack", "crypto=https://github.com/OWNER/crypto-analysis-skills@098013c5",
            "--skills-pack", "mine=git@git.example:owner/mine",
            "--skills-embedding-model", "embed", "--skills-metadata-model", "pass",
        ],
        capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(home)},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    config = load_config(out)
    packs = config.skills.packs
    assert (packs["crypto"].git, packs["crypto"].ref) == (
        "https://github.com/OWNER/crypto-analysis-skills",
        "098013c5",
    )
    assert (packs["mine"].git, packs["mine"].ref) == ("git@git.example:owner/mine", None)
    assert (config.skills.embedding_model, config.skills.metadata_model) == ("embed", "pass")
    (project,) = config.projects
    assert project.skills == ("crypto", "mine")
    refused = subprocess.run(
        [sys.executable, "-I", str(script), str(out), "--state-dir", "s", "--skills-pack", "x"],
        capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(home)},
    )  # fmt: skip
    assert refused.returncode == 2
    assert "a pack is NAME=GIT[@REF]" in refused.stderr


def test_the_examples_mcp_direct_works_uncommented(home: Path, tmp_path: Path) -> None:
    example = Path(__file__).parents[3] / "examples" / "sandbox-science.toml"
    lines = example.read_text().splitlines()
    start = lines.index("# [mcp_direct]")
    uncommented = [line.removeprefix("# ").removeprefix("#") for line in lines[start:]]
    path = tmp_path / "sandbox-science.toml"
    path.write_text("\n".join([*lines[:start], *uncommented]) + "\n")
    loaded = load_config(path)
    assert loaded.canaries.enabled
    assert loaded.canaries.interval == 900
    direct = loaded.mcp_direct
    assert direct is not None
    assert direct.providers == ("crypto-research-mcp",)
    credentials = direct.servers["crypto-research-mcp"].credentials
    assert credentials["CF-Access-Client-Secret"] == "CF_ACCESS_CLIENT_SECRET"
    # Only named: loading the config opens nothing there.
    assert direct.provider_env_file == Path("/path/to/the/owners/cloudflare.env")
    assert direct.provider_profile == tmp_path / "openshell/profiles/cloudflare-access.yaml"
    # The tool index (lab 39, B5): what each server behind the portal is for.
    listed = ("attack-catalog-mcp", "e-print-mcp", "nist-csrc-mcp", "paper-search-mcp")
    assert direct.index == tuple(f"crypto-research-mcp/{n}" for n in (*listed, "web-fetch-mcp"))
    assert direct.about["crypto-research-mcp/e-print-mcp"].startswith("the IACR Cryptology ePrint")
    assert len(direct.about) == 7


def test_skill_packs_name_urls_as_they_are_and_paths_against_the_file(
    make_config: Any, home: Path
) -> None:
    packs = {
        "web": {"git": "https://git.example/pack.git", "ref": "v1"},
        "ssh": {"git": "git@git.example:owner/pack.git"},
        "local": {"git": "../packs/local"},
        "mine": {"git": "~/packs/mine"},
        "tarred": {"archive": "pack.tar.gz", "path": "skills"},
    }
    path = make_config(
        {
            "projects": [{"id": "crypto", "profile": "minimal", "skills": ["web", "tarred"]}],
            "skills": {"packs": packs, "embedding_model": "embed"},
        }
    )
    config = load_config(path)
    found = config.skills.packs
    assert (found["web"].git, found["web"].ref) == ("https://git.example/pack.git", "v1")
    assert found["ssh"].git == "git@git.example:owner/pack.git"
    assert found["local"].git == str(path.parent / "../packs/local")
    assert found["mine"].git == str(home / "packs/mine")
    assert (found["tarred"].archive, found["tarred"].path) == (
        path.parent / "pack.tar.gz",
        "skills",
    )
    assert config.projects[0].skills == ("web", "tarred")
    # Without [skills], projects pin only the built-in skills.
    assert load_config(make_config()).skills.packs == {}


def test_the_examples_skills_work_uncommented(home: Path, tmp_path: Path) -> None:
    example = Path(__file__).parents[3] / "examples" / "sandbox-science.toml"
    lines = example.read_text().splitlines()
    start = lines.index("# [skills]")
    end = lines.index("", start)
    uncommented = [line.removeprefix("# ").removeprefix("#") for line in lines[start:end]]
    lines = [*lines[:start], *uncommented, *lines[end:]]
    lines[lines.index('# skills = ["crypto"]')] = 'skills = ["crypto"]'
    path = tmp_path / "sandbox-science.toml"
    path.write_text("\n".join(lines) + "\n")
    config = load_config(path)
    assert config.projects[0].skills == ("crypto",)
    assert config.skills.packs["crypto"].git == (
        "https://github.com/regenscheid/crypto-analysis-skills"
    )
    assert config.skills.embedding_model == "sandbox-embedding"


PROXY = {"image": "sandbox-science/mcp-proxy:abc1234", "providers": ["p"], "servers": ["s"]}
"""An ``[mcp_proxy]`` as configs had it until the MCP proxy retired (D98, 2026-10-26)."""


@pytest.mark.parametrize("proxy", [PROXY, {}])
def test_the_retired_mcp_proxy_is_refused_with_its_replacement(
    make_config: Any, proxy: dict[str, Any]
) -> None:
    with pytest.raises(LauncherError) as error:
        load_config(make_config({"mcp_proxy": proxy}))
    said = str(error.value)
    assert "[mcp_proxy] is retired" in said
    assert "[mcp_direct]" in said
    assert "sandbox-science migrate-mcp-config" in said
    assert "Migrating from [mcp_proxy]" in said
    # Built in code rather than read from a file, it is refused the same.
    with pytest.raises(ValueError, match=r"\[mcp_proxy\] is retired"):
        LauncherConfig.model_validate({"mcp_proxy": proxy})


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


DIRECT = {
    "servers": {
        "papers": {
            "url": "https://mcp.example.test/mcp",
            "credentials": {"CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID"},
        }
    }
}


def test_the_canaries_ask_the_runtimes_client_and_are_off_by_default(
    make_config: Any, tmp_path: Path
) -> None:
    import json

    from sandbox_science.launcher import orchestrator, parts

    from .test_direct_mcp import DIRECT, direct_files

    direct_files(tmp_path)
    config = load_config(make_config({"mcp_direct": DIRECT}))
    assert not config.canaries.enabled
    assert config.canaries.interval == 900
    config = load_config(make_config({"mcp_direct": DIRECT, "canaries": {"enabled": True}}))
    layout = parts.prepare(config)
    # The control service runs them over the runtime's connection.
    directory, upload = orchestrator.prepare(config, layout)
    host = json.loads((directory / "control.json").read_text())
    assert host["canaries"] == {
        "project": "crypto",
        "servers": ["crypto-research-mcp"],
        "interval": 900.0,
    }
    assert "proxy" not in host
    assert json.loads((upload / "runtime.json").read_text())["mcp"]["project"] == "crypto"
    # Off, control runs none; the runtime keeps its client.
    config = load_config(make_config({"mcp_direct": DIRECT}))
    directory, upload = orchestrator.prepare(config, parts.prepare(config))
    assert "canaries" not in json.loads((directory / "control.json").read_text())
    assert "mcp" in json.loads((upload / "runtime.json").read_text())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"mcp_direct": DIRECT, "orchestrator": None}, "mcp_direct's client runs in the agent"),
        ({"mcp_direct": DIRECT, "mcp_proxy": PROXY}, "[mcp_proxy] is retired"),
        (
            {"mcp_direct": {**DIRECT, "provider_env_file": "cf.env"}},
            "provider_env_file needs provider_profile",
        ),
        (
            {
                "mcp_direct": {
                    **DIRECT,
                    "provider_env_file": "cf.env",
                    "provider_profile": "profile.yaml",
                }
            },
            "mcp_direct.providers must name",
        ),
        ({"mcp_direct": {**DIRECT, "about": {"other": "x"}}}, "mcp_direct.about and index"),
        ({"mcp_direct": {**DIRECT, "project": "other"}}, "is not a project"),
        ({"mcp_direct": {"servers": {}}}, "mcp_direct.servers"),
        ({"mcp_direct": {"servers": {"p": {"url": "ftp://x"}}}}, "mcp_direct.servers"),
        (
            {"mcp_direct": {"servers": {"p": {"url": "https://x", "credentials": {"A": "b c"}}}}},
            "mcp_direct.servers",
        ),
    ],
)
def test_a_direct_path_that_cannot_work_is_refused(
    make_config: Any, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(LauncherError) as error:
        load_config(make_config(changes))
    assert message in str(error.value)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"canaries": {"enabled": True}}, "canaries list the servers through the agent runtime"),
        ({"canaries": {"interval": 30}}, "canaries.interval"),
        ({"canaries": {"enabled": True, "every": 60}}, "canaries.every"),
    ],
)
def test_canaries_that_cannot_work_are_refused(
    make_config: Any, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(LauncherError) as error:
        load_config(make_config(changes))
    assert message in str(error.value)
