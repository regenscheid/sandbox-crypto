# SPDX-License-Identifier: MIT
"""The MCP proxy's sandbox: made on ``up``, deleted on stopping, and never another's.

The OpenShell CLI is a stand-in (fake_openshell.py) whose sandboxes are a JSON file.
"""

from __future__ import annotations

import hashlib
import json
import re
import signal
import socket
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.launcher.config import load_config
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
    steps = journal(mcp["journal"])
    made = steps.index("sandbox-science-mcp-proxy compile-policy")
    assert steps[made:] == [
        "sandbox-science-mcp-proxy compile-policy",
        f"openshell sandbox create {sandbox} {IMAGE} provider crypto-research-mcp "
        "/opt/sandbox-science/bin/mcp-proxy-run",
        f"openshell exec {sandbox} mkdir -p {remote}",
        f"openshell upload {sandbox} pool-ceiling.yaml {remote}/",
        f"openshell upload {sandbox} policy.yaml {remote}/",
        f"openshell upload {sandbox} proxy.toml {remote}/",
        f"openshell exec {sandbox} /opt/sandbox-science/bin/sandbox-science-mcp-proxy bind "
        f"--state-dir {remote}/state --project crypto --policy {remote}/policy.yaml",
        "openshell forward start",
        f"sandbox-science-harness has MCP http://{mcp['listen']} for crypto: "
        "['crypto-research-mcp']",
        "sandbox-science-harness start",
        "sandbox-science-workspace start",
    ]
    # The proxy's one caller is the run service, by its token's digest only.
    layout = Layout(load_config(launcher.config).state_dir)
    token = layout.token("mcp-orchestrator").read_text().strip()
    proxy = (layout.mcp / "upload" / "proxy.toml").read_text()
    (caller,) = tomllib.loads(proxy)["callers"]
    assert caller == {
        "name": "orchestrator",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "kind": "orchestrator",
        "project": "crypto",
    }
    assert token not in proxy
    assert tomllib.loads(proxy)["credentials"] == {
        "crypto-research-mcp.CF_ACCESS_CLIENT_ID": "CF_ACCESS_CLIENT_ID"
    }
    assert f"the MCP proxy's sandbox: {sandbox}" in launcher.command("status").stdout
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert journal(mcp["journal"])[-5:] == [
        "sandbox-science-workspace stop",
        "sandbox-science-harness stop",
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
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert sandboxes(mcp["state"]) == others
    deleted = [step for step in journal(mcp["journal"]) if "delete" in step]
    assert deleted == [f"openshell sandbox delete {sandbox}"]


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
