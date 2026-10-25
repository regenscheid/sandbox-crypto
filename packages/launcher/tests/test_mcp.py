# SPDX-License-Identifier: MIT
"""The MCP proxy's sandbox: made on ``up``, deleted on stopping, and never another's.

The OpenShell CLI is a stand-in (fake_openshell.py) whose sandboxes are a JSON file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.config import LauncherError, error_text, load_config
from sandbox_science.launcher.mcp import OpenShell, OpenShellError, SandboxMissingError
from sandbox_science.launcher.state import Layout

from .conftest import free_port

pytestmark = pytest.mark.slow

SANDBOX = "ss-mcp-proxy"
NAME = re.compile(r"ss-mcp-proxy-[0-9a-f]{6}")
IMAGE = "sandbox-science/mcp-proxy:test"
PROFILE = Path(__file__).parents[3] / "examples/openshell/profiles/cloudflare-access.yaml"
ID = "test-secret-client-id-launcher"
SECRET = "test-secret-client-secret-launcher"
REGISTERED = "crypto-research-mcp CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET"


@pytest.fixture
def mcp(site: dict[str, Any], fake_openshell: Path, tmp_path: Path) -> dict[str, Any]:
    state = tmp_path / "openshell.json"
    state.write_text(json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": {}}))
    listen = f"127.0.0.1:{free_port(site['taken'])}"

    def launch(
        parts: dict[str, str] | None = None,
        registration: Path | None = None,
        options: tuple[str, ...] = (),
    ) -> Any:
        registered = (
            {"provider_env_file": str(registration), "provider_profile": str(PROFILE)}
            if registration is not None
            else {}
        )
        return site["launch"](
            parts,
            options=options,
            openshell={"cli": str(fake_openshell)},
            mcp_proxy={
                "image": IMAGE,
                "providers": ["crypto-research-mcp"],
                "credentials": {"crypto-research-mcp.CF_ACCESS_CLIENT_ID": "CF_ACCESS_CLIENT_ID"},
                "servers": ["crypto-research-mcp"],
                "listen": listen,
                **registered,
            },
        )

    return {"launch": launch, "state": state, "journal": site["journal"], "listen": listen}


def journal(path: Path) -> list[str]:
    return path.read_text().splitlines()


def sandboxes(state: Path) -> dict[str, Any]:
    return json.loads(state.read_text())["sandboxes"]


def ours(state: Path, *, but: tuple[str, ...] = ()) -> str:
    """The one sandbox this launcher made: the configured name and six hex digits."""
    (name,) = [
        name for name in sandboxes(state) if name not in but and NAME.fullmatch(name) is not None
    ]
    return name


def test_up_makes_the_proxys_sandbox_and_deletes_it_when_it_stops(mcp: dict[str, Any]) -> None:
    launcher = mcp["launch"]()
    launcher.expect("Stop:")
    remote = "/sandbox/mcp-proxy"
    sandbox = ours(mcp["state"])
    (runtime,) = [name for name in sandboxes(mcp["state"]) if name.startswith("ss-orch-")]
    steps = journal(mcp["journal"])
    # Escalated tools pass the sandbox's fence, for the user's approval (approvals on).
    made = steps.index("sandbox-science-mcp-proxy compile-policy --admit-escalated")
    assert steps[made:] == [
        "sandbox-science-mcp-proxy compile-policy --admit-escalated",
        f"openshell sandbox create {sandbox} {IMAGE} provider crypto-research-mcp "
        "/opt/sandbox-science/bin/mcp-proxy-run",
        f"openshell exec {sandbox} mkdir -p {remote}",
        f"openshell upload {sandbox} pool-ceiling.yaml {remote}/",
        f"openshell upload {sandbox} policy.yaml {remote}/",
        f"openshell upload {sandbox} proxy.toml {remote}/",
        f"openshell exec {sandbox} /opt/sandbox-science/bin/sandbox-science-mcp-proxy bind "
        f"--state-dir {remote}/state --project crypto --policy {remote}/policy.yaml",
        "openshell forward start",
        "sandbox-science-control start",
        f"openshell sandbox create {runtime} sandbox-science/orchestrator:test sleep infinity",
        f"openshell exec {runtime} mkdir -p /sandbox/runtime",
        f"openshell upload {runtime} runtime-control.token /sandbox/runtime/runtime.token",
        f"openshell upload {runtime} model.key /sandbox/runtime/model.key",
        f"openshell upload {runtime} runtime.json /sandbox/runtime/runtime.json",
        f"openshell exec {runtime} chmod 600 "
        "/sandbox/runtime/runtime.token /sandbox/runtime/model.key",
        f"openshell exec {runtime} /opt/sandbox-science/bin/python -m "
        "sandbox_science.orchestrator.confinement.runtime --settings /sandbox/runtime/runtime.json",
        "sandbox-science-workspace start",
    ]
    # Only the trusted capability host receives the MCP caller credential/configuration.
    layout = Layout(load_config(launcher.config).state_dir)
    host = json.loads((layout.root / "orchestrator" / "control.json").read_text())
    assert host["proxy"]["url"] == f"http://{mcp['listen']}"
    assert host["proxy"]["project"] == "crypto"
    assert host["proxy"]["servers"] == ["crypto-research-mcp"]
    agent = json.loads((layout.root / "orchestrator" / "upload" / "runtime.json").read_text())
    assert "proxy" not in agent
    assert "sandbox-science-harness start" not in steps
    # The proxy's callers are the run service, and the workspace as an operator that
    # answers permission requests and calls no tool, each by its token's digest only.
    layout = Layout(load_config(launcher.config).state_dir)
    token = layout.token("mcp-orchestrator").read_text().strip()
    operator = layout.token("mcp-workspace").read_text().strip()
    proxy = (layout.mcp / "upload" / "proxy.toml").read_text()
    caller, workspace = tomllib.loads(proxy)["callers"]
    assert caller == {
        "name": "orchestrator",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "kind": "orchestrator",
        "project": "crypto",
    }
    assert workspace == {
        "name": "workspace",
        "token_sha256": hashlib.sha256(operator.encode()).hexdigest(),
        "kind": "operator",
        "project": "crypto",
    }
    assert token not in proxy
    assert operator not in proxy
    assert tomllib.loads(proxy)["credentials"] == {
        "crypto-research-mcp.CF_ACCESS_CLIENT_ID": "CF_ACCESS_CLIENT_ID"
    }
    assert f"the MCP proxy's sandbox: {sandbox}" in launcher.command("status").stdout
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert journal(mcp["journal"])[-6:] == [
        "sandbox-science-workspace stop",
        f"openshell sandbox delete {runtime}",
        "sandbox-science-control stop",
        "openshell forward stop",
        f"openshell sandbox delete {sandbox}",
        "sandbox-science-worker stop",
    ]
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


def test_the_forward_never_gets_a_port_the_site_took() -> None:
    """A CI run of the next test failed: the system handed the forward's probe the port it
    had just handed the workspace's, and the launcher refused its config. The pick skips
    the ports the site took, here with a probe that hands the workspace's out first."""
    worker, workspace = 18100, 18200
    handed = iter([workspace, workspace + 1, worker, 18300])
    taken = {worker, workspace, workspace + 1}
    assert free_port(taken, probe=lambda: next(handed)) == 18300


def test_a_launcher_that_stops_at_once_fails_its_wait_at_once(
    site: dict[str, Any], fake_openshell: Path
) -> None:
    """With the forward on the workspace's port, as in that run, the launcher stops at
    once; waiting for it to say ``Stop:`` fails as soon as its output ends, with what it
    said, rather than after the wait's 30 seconds."""
    launcher = site["launch"](
        openshell={"cli": str(fake_openshell)},
        mcp_proxy={
            "image": IMAGE,
            "providers": ["crypto-research-mcp"],
            "servers": ["crypto-research-mcp"],
            "listen": f"127.0.0.1:{site['workspace']}",
        },
    )
    began = time.monotonic()
    with pytest.raises(AssertionError, match="both listen on"):
        launcher.expect("Stop:", timeout=30)
    assert time.monotonic() - began < 20
    assert launcher.finish() != 0


def test_sandboxes_it_did_not_make_are_left_alone(mcp: dict[str, Any]) -> None:
    # One by hand with the configured name, and another launcher's with its own suffix.
    others = {
        SANDBOX: {"id": "by-hand", "phase": "Ready", "image": "other"},
        f"{SANDBOX}-cccccc": {"id": "another-launchers", "phase": "Ready", "image": IMAGE},
    }
    mcp["state"].write_text(json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": others}))
    launcher = mcp["launch"]()
    launcher.expect("Stop:")
    sandbox = ours(mcp["state"], but=tuple(others))
    (runtime,) = [name for name in sandboxes(mcp["state"]) if name.startswith("ss-orch-")]
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert sandboxes(mcp["state"]) == others
    deleted = [step for step in journal(mcp["journal"]) if "delete" in step]
    assert deleted == [f"openshell sandbox delete {runtime}", f"openshell sandbox delete {sandbox}"]


def test_a_record_without_an_id_deletes_only_the_name_it_recorded(mcp: dict[str, Any]) -> None:
    launcher = mcp["launch"]()
    launcher.expect("Stop:")
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    layout = Layout(load_config(launcher.config).state_dir)
    # A launcher that died while it created its sandbox, and another launcher's sandbox.
    (layout.mcp / "sandbox.json").write_text(json.dumps({"name": f"{SANDBOX}-aaaaaa", "id": None}))
    theirs = {f"{SANDBOX}-bbbbbb": {"id": "theirs", "phase": "Ready", "image": IMAGE}}
    mcp["state"].write_text(json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": theirs}))
    assert launcher.command("down").returncode == 0
    assert sandboxes(mcp["state"]) == theirs
    assert not (layout.mcp / "sandbox.json").exists()
    # When the recorded name is there, it is this launcher's, and goes.
    (layout.mcp / "sandbox.json").write_text(json.dumps({"name": f"{SANDBOX}-aaaaaa", "id": None}))
    left = {f"{SANDBOX}-aaaaaa": {"id": "half-made", "phase": "Ready", "image": IMAGE}}
    mcp["state"].write_text(
        json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": {**theirs, **left}})
    )
    stopped = launcher.command("down")
    assert f"deleted the sandbox {SANDBOX}-aaaaaa an earlier launcher made" in stopped.stdout
    assert sandboxes(mcp["state"]) == theirs


def test_a_failed_create_forgets_its_record_and_deletes_nothing(mcp: dict[str, Any]) -> None:
    theirs = {f"{SANDBOX}-cccccc": {"id": "theirs", "phase": "Ready", "image": IMAGE}}
    mcp["state"].write_text(json.dumps({"providers": ["crypto-research-mcp"], "sandboxes": theirs}))
    launcher = mcp["launch"]({"openshell": "create-fails"})
    assert launcher.finish() == 1
    assert "openshell sandbox create failed" in launcher.output
    layout = Layout(load_config(launcher.config).state_dir)
    assert not (layout.mcp / "sandbox.json").exists()
    assert sandboxes(mcp["state"]) == theirs
    assert not any("delete" in step for step in journal(mcp["journal"]))


def test_the_forward_starts_again_later_each_time_it_keeps_stopping(mcp: dict[str, Any]) -> None:
    launcher = mcp["launch"]({"openshell": "forward-exits-always"})
    launcher.expect("the mcp forward stopped, with status 1; starting it again in 2 s")
    launcher.expect("the mcp forward stopped, with status 1; starting it again in 4 s")
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert sandboxes(mcp["state"]) == {}


def test_the_forward_is_not_started_again_on_an_address_someone_took(
    mcp: dict[str, Any],
) -> None:
    host, port = mcp["listen"].rsplit(":", 1)
    launcher = mcp["launch"]({"openshell": "forward-exits-once"})
    launcher.expect("the mcp forward stopped, with status 1; starting it again in 2 s")
    with socket.socket() as squatter:
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind((host, int(port)))
        squatter.listen()
        assert launcher.finish() == 1
    assert f"something else listens on {mcp['listen']}" in launcher.output
    assert journal(mcp["journal"]).count("openshell forward start") == 1
    assert sandboxes(mcp["state"]) == {}


def test_a_killed_launchers_sandbox_is_deleted_by_down(mcp: dict[str, Any]) -> None:
    launcher = mcp["launch"]()
    launcher.expect("Stop:")
    sandbox = ours(mcp["state"])
    launcher.process.kill()
    launcher.finish()
    deadline = time.monotonic() + 15
    while "openshell forward stop" not in journal(mcp["journal"]):  # it dies with the launcher
        assert time.monotonic() < deadline, "the forward outlived the launcher"
        time.sleep(0.1)
    shown = launcher.command("status")
    assert shown.returncode == 4
    assert f"the sandbox {sandbox} it made may be left" in shown.stdout
    stopped = launcher.command("down")
    assert f"deleted the sandbox {sandbox} an earlier launcher made" in stopped.stdout
    assert sandboxes(mcp["state"]) == {}


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
