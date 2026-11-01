# SPDX-License-Identifier: MIT
"""``sandbox-science migrate-mcp-config``: a config's retired ``[mcp_proxy]`` rewritten as
``[mcp_direct]`` (D98, docs/MCP-CONNECTORS.md, "Migrating from [mcp_proxy]")."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.cli import main
from sandbox_science.launcher.config import LauncherError, load_config
from sandbox_science.launcher.migrate_mcp import migrate

from .test_direct_mcp import direct_files

SERVER = "crypto-research-mcp"
PROXY: dict[str, Any] = {
    "image": "sandbox-science/mcp-proxy:d27e41a",
    "providers": [SERVER],
    "servers": [SERVER],
    "provider_env_file": "/path/to/the/owners/cloudflare.env",
    "provider_profile": "openshell/profiles/cloudflare-access.yaml",
    "listen": "127.0.0.1:18080",
    "index": [f"{SERVER}/e-print-mcp"],
    "credentials": {
        f"{SERVER}.CF_ACCESS_CLIENT_ID": "CF_ACCESS_CLIENT_ID",
        f"{SERVER}.CF_ACCESS_CLIENT_SECRET": "CF_ACCESS_CLIENT_SECRET",
    },
    "about": {f"{SERVER}/e-print-mcp": "IACR ePrint papers"},
}
"""An ``[mcp_proxy]`` as the example config had it before the MCP proxy retired."""


@pytest.fixture
def old(make_config: Any, tmp_path: Path) -> Path:
    """A config with ``[mcp_proxy]``, a comment of the owner's and canaries, beside the
    example policy."""
    direct_files(tmp_path)
    path = make_config({"mcp_proxy": PROXY, "canaries": {"enabled": True}})
    path.write_text("# the owner's own note\n" + path.read_text())
    path.chmod(0o640)
    return path


def test_the_proxy_table_becomes_the_direct_one_and_the_rest_is_kept(
    old: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = old.read_text()
    with pytest.raises(LauncherError, match=r"\[mcp_proxy\] is retired"):
        load_config(old)
    assert main(["migrate-mcp-config", str(old)]) == 0
    said = capsys.readouterr()
    assert "rewrote [mcp_proxy] as [mcp_direct]" in said.out
    assert "mcp_proxy.image is dropped" in said.err
    assert "mcp_proxy.listen is dropped" in said.err
    backup = old.with_name(old.name + ".mcp-proxy.bak")
    assert backup.read_text() == before
    assert old.stat().st_mode & 0o777 == backup.stat().st_mode & 0o777 == 0o640
    text = old.read_text()
    assert text.startswith("# the owner's own note\n")
    assert "mcp_proxy" not in tomllib.loads(text)
    config = load_config(old)
    direct = config.mcp_direct
    assert direct is not None
    assert direct.providers == (SERVER,)
    # The same provider file, by its path, and the URL is the policy's.
    assert direct.provider_env_file == Path("/path/to/the/owners/cloudflare.env")
    assert direct.provider_profile == old.parent / "openshell/profiles/cloudflare-access.yaml"
    assert direct.servers[SERVER].url is None
    assert direct.servers[SERVER].credentials == {
        "CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID",
        "CF-Access-Client-Secret": "CF_ACCESS_CLIENT_SECRET",
    }
    assert direct.index == (f"{SERVER}/e-print-mcp",)
    assert direct.about == {f"{SERVER}/e-print-mcp": "IACR ePrint papers"}
    assert config.canaries.enabled
    # Run again, it says there is nothing to migrate and leaves the file alone.
    assert main(["migrate-mcp-config", str(old)]) == 1
    assert "no [mcp_proxy] table" in capsys.readouterr().err
    assert old.read_text() == text


def test_print_writes_nothing(old: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = old.read_text()
    assert main(["migrate-mcp-config", str(old), "--print"]) == 0
    printed = capsys.readouterr().out
    assert "[mcp_direct]" in printed
    assert old.read_text() == before
    assert not old.with_name(old.name + ".mcp-proxy.bak").exists()


def test_the_command_runs_without_a_loadable_config(old: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "sandbox_science.launcher", "migrate-mcp-config", str(old)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert load_config(old).mcp_direct is not None


def test_a_backup_already_there_is_never_overwritten(
    old: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = old.with_name(old.name + ".mcp-proxy.bak")
    backup.write_text("an earlier backup\n")
    before = old.read_text()
    assert main(["migrate-mcp-config", str(old)]) == 1
    assert "exists already" in capsys.readouterr().err
    assert (old.read_text(), backup.read_text()) == (before, "an earlier backup\n")


def config_text(proxy: str, policy: str = "policy.yaml") -> str:
    return (
        'state_dir = "state"\n'
        '[[projects]]\nid = "crypto"\nprofile = "minimal"\n'
        '[model]\nurl = "https://gateway.example/v1"\nname = "m"\napi_key_file = "k"\n'
        f'[worker]\npolicy = "{policy}"\n'
        '[orchestrator]\nimage = "o:t"\nrevision = "' + "a" * 40 + '"\n'
        '[workspace]\nstatic = "web"\n'
        '[openshell.environments]\npolicy_store = "s"\ntargets = "t"\nceiling = "c"\n'
        'profiles = { minimal = "m:t" }\n' + proxy
    )


def test_what_cannot_be_carried_over_is_said(tmp_path: Path) -> None:
    (tmp_path / "policy.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: t}\n"
        "mcp:\n  proxy: {worker_access: true}\n  servers:\n"
        "    papers: {url: 'https://p.example.org/mcp', revision: '1',"
        " auth: {mode: bearer, provider: papers}}\n"
        "    notes:\n      url: https://n.example.org/mcp\n      revision: '1'\n"
        "      auth:\n        mode: headers\n        headers:\n"
        "          X-Key: 'Key ${provider:notes.KEY}'\n"
        "          X-Id: '${provider:notes.ID}'\n"
    )
    proxy = (
        '[mcp_proxy]\nimage = "p:t"\nproviders = ["papers"]\nservers = ["papers", "notes"]\n'
        "approvals = false\n"
    )
    text, notes = migrate(config_text(proxy), tmp_path)
    said = "\n".join(notes)
    assert "papers is a bearer server" in said
    assert "notes's header X-Key has text around its placeholder" in said
    assert "gives no variable for notes.ID" in said
    assert "sets mcp.proxy.worker_access or worker_tools" in said
    assert "mcp_proxy.approvals was off" in said
    direct = tomllib.loads(text)["mcp_direct"]
    assert direct["servers"] == {"papers": {}, "notes": {}}


@pytest.mark.parametrize(
    ("proxy", "message"),
    [
        ("", r"no \[mcp_proxy\] table"),
        (
            '[mcp_proxy]\nservers = ["papers"]\n[mcp_direct.servers.papers]\n',
            r"has \[mcp_direct\] already",
        ),
        ('[mcp_proxy]\nservers = ["other"]\n', "which the project policy does not"),
        ("[mcp_proxy]\nservers = []\n", "names no server"),
    ],
)
def test_what_it_cannot_migrate_is_refused(tmp_path: Path, proxy: str, message: str) -> None:
    (tmp_path / "policy.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: t}\n"
        "mcp:\n  servers:\n"
        "    papers: {url: 'https://p.example.org/mcp', revision: '1', auth: {mode: none}}\n"
    )
    with pytest.raises(LauncherError, match=message):
        migrate(config_text(proxy), tmp_path)


def test_a_config_without_the_agent_runtime_is_refused(tmp_path: Path) -> None:
    (tmp_path / "policy.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\nmetadata: {name: t}\n"
        "mcp:\n  servers:\n"
        "    papers: {url: 'https://p.example.org/mcp', revision: '1', auth: {mode: none}}\n"
    )
    text = config_text('[mcp_proxy]\nservers = ["papers"]\n')
    text = text.replace('[orchestrator]\nimage = "o:t"\nrevision = "' + "a" * 40 + '"\n', "")
    with pytest.raises(LauncherError, match="mcp_direct's client runs in the agent runtime"):
        migrate(text, tmp_path)
