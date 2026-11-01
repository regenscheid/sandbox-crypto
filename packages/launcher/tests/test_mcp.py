# SPDX-License-Identifier: MIT
"""The launcher without the MCP proxy (D98: it retired on 2026-10-26): ``up`` makes no proxy
sandbox, forward or caller, registers the agent runtime's provider, and deletes a proxy
sandbox an earlier launcher recorded and left; and the OpenShell CLI's answers.

The OpenShell CLI is a stand-in (fake_openshell.py) whose sandboxes are a JSON file.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.config import LauncherError, error_text, load_config
from sandbox_science.launcher.mcp import OpenShell, OpenShellError, SandboxMissingError
from sandbox_science.launcher.state import Layout

from .test_direct_mcp import DIRECT, direct_files

pytestmark = pytest.mark.slow

LEFT = "ss-mcp-proxy-aaaaaa"
"""A proxy sandbox an earlier launcher made, by its configured name and six hex digits."""
PROFILE = Path(__file__).parents[3] / "examples/openshell/profiles/cloudflare-access.yaml"
ID = "test-secret-client-id-launcher"
SECRET = "test-secret-client-secret-launcher"
REGISTERED = "crypto-research-mcp CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET"


@pytest.fixture
def mcp(site: dict[str, Any], fake_openshell: Path, tmp_path: Path) -> dict[str, Any]:
    direct_files(tmp_path)
    state = tmp_path / "openshell.json"
    state.write_text(json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": {}}))

    def launch(
        parts: dict[str, str] | None = None,
        registration: Path | None = None,
        options: tuple[str, ...] = (),
        command: str = "up",
    ) -> Any:
        registered = (
            {"provider_env_file": str(registration), "provider_profile": str(PROFILE)}
            if registration is not None
            else {}
        )
        return site["launch"](
            parts,
            options=options,
            command=command,
            openshell={"cli": str(fake_openshell)},
            mcp_direct={**DIRECT, **registered},
        )

    return {"launch": launch, "state": state, "journal": site["journal"]}


def journal(path: Path) -> list[str]:
    return path.read_text().splitlines()


def sandboxes(state: Path) -> dict[str, Any]:
    return json.loads(state.read_text())["sandboxes"]


def test_up_starts_no_mcp_proxy(mcp: dict[str, Any]) -> None:
    launcher = mcp["launch"]()
    launcher.expect("Stop:")
    steps = journal(mcp["journal"])
    (runtime,) = [name for name in sandboxes(mcp["state"]) if name.startswith("ss-orch-")]
    assert not [step for step in steps if "mcp-proxy" in step or "forward" in step]
    assert [step for step in steps if step.startswith("openshell sandbox create")] == [
        f"openshell sandbox create {runtime} sandbox-science/orchestrator:test "
        "provider crypto-research-mcp sleep infinity"
    ]
    layout = Layout(load_config(launcher.config).state_dir)
    assert not (layout.mcp / "upload").exists()
    assert not layout.token("mcp-orchestrator").exists()
    assert not layout.token("mcp-workspace").exists()
    host = json.loads((layout.root / "orchestrator" / "control.json").read_text())
    assert "proxy" not in host
    assert host["direct"]["servers"] == ["crypto-research-mcp"]
    assert "MCP proxy" not in launcher.command("status").stdout
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert sandboxes(mcp["state"]) == {}


def test_a_missing_provider_is_refused_before_anything_starts(mcp: dict[str, Any]) -> None:
    mcp["state"].write_text(json.dumps({"providers": [], "sandboxes": {}}))
    launcher = mcp["launch"]()
    assert launcher.finish() == 1
    assert "the OpenShell provider crypto-research-mcp does not exist" in launcher.output
    assert "docs/MCP-CONNECTORS.md" in launcher.output
    assert journal(mcp["journal"]) == []


def owners_file(tmp_path: Path) -> Path:
    """A synthetic stand-in for the owner's file: obviously fake values."""
    path = tmp_path / "cloudflare.env"
    path.write_text(f"CF-Access-Client-Id: = {ID}\nCF-Access-Client-Secret: = '{SECRET}'\n")
    path.chmod(0o600)
    return path


def nothing_kept_them(launcher: Any, tmp_path: Path) -> None:
    """The values are in no output and in no file but the owner's."""
    assert ID not in launcher.output
    assert SECRET not in launcher.output
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "cloudflare.env":
            data = path.read_bytes()
            assert ID.encode() not in data, path
            assert SECRET.encode() not in data, path


def test_up_registers_a_missing_provider_from_the_owners_file(
    mcp: dict[str, Any], tmp_path: Path
) -> None:
    mcp["state"].write_text(json.dumps({"providers": [], "sandboxes": {}}))
    launcher = mcp["launch"](registration=owners_file(tmp_path))
    launcher.expect("Stop:")
    assert journal(mcp["journal"])[:4] == [
        "openshell provider profile lint cloudflare-access.yaml",
        "openshell provider profile import cloudflare-access.yaml",
        f"openshell provider create cloudflare-access {REGISTERED}",
        "sandbox-science-worker start",
    ]
    assert "created the OpenShell provider crypto-research-mcp from" in launcher.output
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    nothing_kept_them(launcher, tmp_path)


def test_up_registers_an_existing_provider_again_only_when_asked(
    mcp: dict[str, Any], tmp_path: Path
) -> None:
    path = owners_file(tmp_path)
    mcp["state"].write_text(
        json.dumps(
            {
                "providers": ["crypto-research-mcp"],
                "profiles": ["cloudflare-access"],
                "sandboxes": {},
            }
        )
    )
    launcher = mcp["launch"](registration=path)
    launcher.expect("Stop:")
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert not any(step.startswith("openshell provider") for step in journal(mcp["journal"]))
    mcp["journal"].write_text("")
    launcher = mcp["launch"](registration=path, options=("--refresh-provider",))
    launcher.expect("Stop:")
    assert journal(mcp["journal"])[:2] == [
        f"openshell provider update {REGISTERED}",
        "sandbox-science-worker start",
    ]
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    nothing_kept_them(launcher, tmp_path)


def left_behind(layout: Layout, identity: str | None) -> None:
    """The record an earlier launcher kept of the proxy sandbox it made."""
    layout.mcp.mkdir(mode=0o700, parents=True, exist_ok=True)
    (layout.mcp / "sandbox.json").write_text(json.dumps({"name": LEFT, "id": identity}))


def test_a_proxy_sandbox_an_earlier_launcher_left_is_deleted_by_up(
    mcp: dict[str, Any],
) -> None:
    launcher = mcp["launch"](command="prepare")
    assert launcher.finish() == 0
    layout = Layout(load_config(launcher.config).state_dir)
    left_behind(layout, "left-id")
    theirs = {"ss-mcp-proxy-bbbbbb": {"id": "theirs", "phase": "Ready", "image": "x"}}
    mine = {LEFT: {"id": "left-id", "phase": "Ready", "image": "x"}}
    mcp["state"].write_text(
        json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": {**theirs, **mine}})
    )
    shown = launcher.command("status")
    assert f"the retired MCP proxy's sandbox {LEFT} may be left" in shown.stdout
    launcher = mcp["launch"]()
    launcher.expect("Stop:")
    assert f"deleted the retired MCP proxy's sandbox {LEFT}" in launcher.output
    assert LEFT not in sandboxes(mcp["state"])
    assert not (layout.mcp / "sandbox.json").exists()
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert sandboxes(mcp["state"]) == theirs  # another launcher's is left alone


def test_a_proxy_sandbox_recorded_without_an_id_goes_only_by_its_name(
    mcp: dict[str, Any],
) -> None:
    launcher = mcp["launch"](command="prepare")
    assert launcher.finish() == 0
    layout = Layout(load_config(launcher.config).state_dir)
    left_behind(layout, None)
    theirs = {"ss-mcp-proxy-bbbbbb": {"id": "theirs", "phase": "Ready", "image": "x"}}
    mcp["state"].write_text(json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": theirs}))
    assert launcher.command("down").returncode == 0
    assert sandboxes(mcp["state"]) == theirs
    assert not (layout.mcp / "sandbox.json").exists()
    left_behind(layout, None)
    left = {LEFT: {"id": "half-made", "phase": "Ready", "image": "x"}}
    mcp["state"].write_text(
        json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": {**theirs, **left}})
    )
    stopped = launcher.command("down")
    assert f"deleted the retired MCP proxy's sandbox {LEFT}" in stopped.stdout
    assert sandboxes(mcp["state"]) == theirs


def test_a_proxy_sandbox_name_another_sandbox_reused_is_left_alone(
    mcp: dict[str, Any],
) -> None:
    launcher = mcp["launch"](command="prepare")
    assert launcher.finish() == 0
    layout = Layout(load_config(launcher.config).state_dir)
    left_behind(layout, "left-id")
    reused = {LEFT: {"id": "someone-elses", "phase": "Ready", "image": "x"}}
    mcp["state"].write_text(json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": reused}))
    stopped = launcher.command("down")
    assert stopped.returncode == 0
    assert f"left the sandbox {LEFT} alone: it is someone-elses" in stopped.stdout
    assert sandboxes(mcp["state"]) == reused
    assert not (layout.mcp / "sandbox.json").exists()


MISSING = "status: 'Some requested entity was not found', self: \"sandbox not found\""
"""How OpenShell 0.1.5's CLI (tonic 0.14) shows the gateway's NotFound for a name."""


def saying(tmp_path: Path, said: str) -> OpenShell:
    """An ``openshell`` whose every command fails with ``said`` on stderr."""
    script = tmp_path / "openshell"
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write({said!r})\nsys.exit(1)\n")
    script.chmod(0o755)
    cli = object.__new__(OpenShell)
    cli.cli, cli.env = str(script), dict(os.environ)
    return cli


@pytest.mark.parametrize(
    "said",
    [
        "\x1b[31mError:\x1b[0m   \u00d7 " + MISSING,
        MISSING + ", metadata: " + "x" * 600,
        "  \u00d7 status: 'Some requested entity was not found', self: \"sandbox not\n  │ found\"",
        "code: 'Some requested entity was not found', message: \"sandbox not found\"",
    ],
)
async def test_only_the_gateways_not_found_for_the_name_means_absent(
    tmp_path: Path, said: str
) -> None:
    assert await saying(tmp_path, said).sandbox("ss-orch-0123456789") is None


@pytest.mark.parametrize(
    "said",
    [
        "status: 'Some requested entity was not found', self: \"service endpoint not found\"",
        "error: gateway 'babbage' not found",
        "error: /home/user/.config/openshell/client.pem: file not found",
        "Error: sandbox ss-orch-0123456789 not found",
        "error: the gateway is not reachable",
    ],
)
async def test_other_failures_never_count_as_absence(tmp_path: Path, said: str) -> None:
    with pytest.raises(OpenShellError, match="openshell sandbox get failed") as raised:
        await saying(tmp_path, said).sandbox("ss-orch-0123456789")
    assert not isinstance(raised.value, SandboxMissingError)


@pytest.mark.parametrize("answer", ["null", "[]", '"ss-orch-0123456789"'])
async def test_a_successful_lookup_of_another_shape_is_not_absence(
    tmp_path: Path, answer: str
) -> None:
    script = tmp_path / "openshell"
    script.write_text(f"#!{sys.executable}\nprint({answer!r})\n")
    script.chmod(0o755)
    cli = object.__new__(OpenShell)
    cli.cli, cli.env = str(script), dict(os.environ)
    with pytest.raises(OpenShellError, match="no sandbox record") as raised:
        await cli.sandbox("ss-orch-0123456789")
    assert not isinstance(raised.value, SandboxMissingError)


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        (
            "\x1b[31mError:\x1b[0m   \u00d7 transport error\n"
            "  \u2570\u2500\u25b6 tcp connect error: Connection refused\n\n",
            "tcp connect error: Connection refused",
        ),
        ("", "no error text from openshell"),
        ("  \n \n", "no error text from openshell"),
    ],
)
async def test_a_failure_names_the_command_its_exit_and_the_last_line(
    tmp_path: Path, said: str, kept: str
) -> None:
    with pytest.raises(OpenShellError) as raised:
        await saying(tmp_path, said).run("sandbox", "create", "--name", "x")
    assert str(raised.value) == f"openshell sandbox create failed (exit 1): {kept}"


async def test_a_command_that_never_answers_is_named(tmp_path: Path) -> None:
    script = tmp_path / "openshell"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    script.chmod(0o755)
    cli = object.__new__(OpenShell)
    cli.cli, cli.env = str(script), dict(os.environ)
    with pytest.raises(OpenShellError, match=r"^openshell sandbox create gave no answer within"):
        await cli.run("sandbox", "create", timeout=0.5)


def test_an_error_without_text_is_named_by_its_type() -> None:
    assert error_text(TimeoutError()) == "TimeoutError (no error text)"
    assert error_text(LauncherError("  two\n lines ")) == "two lines"
    assert len(error_text(LauncherError("x" * 600))) == 512
