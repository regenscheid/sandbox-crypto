# SPDX-License-Identifier: MIT
"""The direct MCP path's launcher wiring (D98): with ``[mcp_direct]``, ``up`` compiles the
agent runtime sandbox's egress to the configured servers from the project policy, attaches
their provider, binds the sandbox, and hands the runtime its servers' tool admissions and
control the run service's direct access. The OpenShell CLI is a stand-in."""

from __future__ import annotations

import json
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from sandbox_science.launcher import orchestrator, parts
from sandbox_science.launcher.config import LauncherError, load_config
from sandbox_science.launcher.state import Layout
from sandbox_science.policy.store import Acceptance, PolicyStore

ROOT = Path(__file__).parents[3]
SERVER = "crypto-research-mcp"
URL = "https://mcp.example.invalid/crypto-research"
"""The example policy's placeholder address for the server."""
DIRECT: dict[str, Any] = {
    "servers": {SERVER: {"credentials": {"CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID"}}},
    "providers": [SERVER],
    "about": {SERVER: "Cryptography research servers"},
}
PROFILE = ROOT / "examples/openshell/profiles/cloudflare-access.yaml"
ID = "test-secret-client-id-direct"
SECRET = "test-secret-client-secret-direct"


def direct_files(tmp_path: Path) -> Path:
    """The site's policy, targets and pool ceiling as a deployment has them (the config
    fixture writes placeholders)."""
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    (site / "policy.yaml").write_text((ROOT / "examples/policy.yaml").read_text())
    (site / "targets.yaml").write_text((ROOT / "examples/openshell/targets.yaml").read_text())
    (site / "pool-ceiling.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
    )
    return site


def test_prepare_compiles_the_runtimes_egress_beside_its_platform_services(
    make_config: Any, tmp_path: Path
) -> None:
    direct_files(tmp_path)
    config = load_config(make_config({"mcp_direct": DIRECT}))
    directory, upload = orchestrator.prepare(config, parts.prepare(config))
    policy = yaml.safe_load((directory / "policy.yaml").read_text())
    networks = policy["network_policies"]
    # The platform services, as without the direct path, and one mcp endpoint.
    assert {"sandbox-science-worker-api", "sandbox-science-state-service"} <= networks.keys()
    assert "sandbox-science-mcp-proxy" not in networks
    (endpoint,) = networks[f"mcp-{SERVER}"]["endpoints"]
    assert (endpoint["host"], endpoint["protocol"], endpoint["path"]) == (
        "mcp.example.invalid",
        "mcp",
        "/crypto-research",
    )
    assert endpoint["credential_binding"]["provider"] == SERVER
    assert networks[f"mcp-{SERVER}"]["binaries"] == [{"path": "/opt/sandbox-science/bin/python*"}]
    # The policy middleware decides the server's host, for the project, as the runtime.
    (middleware,) = policy["network_middlewares"].values()
    assert middleware["middleware"] == "sandbox-science-policy"  # the targets file's name
    assert middleware["endpoints"] == {"include": ["mcp.example.invalid"]}
    assert middleware["config"]["mcp"] == {"project": "crypto", "caller": "agent-runtime"}
    # The runtime's client: the policy's URL, the placeholders' variables, and the tools
    # the compiled endpoint admits, from the same endpoint.
    runtime = json.loads((upload / "runtime.json").read_text())["mcp"]
    server = runtime["servers"][SERVER]
    assert runtime["project"] == "crypto"
    assert server["url"] == URL
    assert server["credentials"] == {"CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID"}
    allowed = {
        name
        for rule in endpoint["rules"]
        if rule["allow"]["method"] == "tools/call"
        for name in rule["allow"]["tool"]["any"]
    }
    assert allowed
    assert set(server["tools"]["allow"]) == allowed
    # Control's run service makes connector calls through the runtime, with no proxy.
    host = json.loads((directory / "control.json").read_text())
    assert "proxy" not in host
    assert host["direct"] == {
        "project": "crypto",
        "servers": [SERVER],
        "about": {SERVER: "Cryptography research servers"},
        "index": None,
    }


def test_without_the_direct_path_nothing_of_it_is_compiled(
    make_config: Any, tmp_path: Path
) -> None:
    config = load_config(make_config())
    directory, upload = orchestrator.prepare(config, parts.prepare(config))
    policy = yaml.safe_load((directory / "policy.yaml").read_text())
    assert "network_middlewares" not in policy
    assert not any(name.startswith("mcp-") for name in policy["network_policies"])
    assert "mcp" not in json.loads((upload / "runtime.json").read_text())
    assert "direct" not in json.loads((directory / "control.json").read_text())


@pytest.mark.parametrize(
    ("servers", "message"),
    [
        ({"papers": {}}, "is not one of the project policy's MCP servers"),
        ({SERVER: {"url": "https://elsewhere.example/mcp"}}, "differs from the project policy"),
    ],
)
def test_a_server_the_policy_does_not_declare_so_is_refused(
    make_config: Any, tmp_path: Path, servers: dict[str, Any], message: str
) -> None:
    direct_files(tmp_path)
    config = load_config(make_config({"mcp_direct": {"servers": servers}}))
    with pytest.raises(LauncherError) as error:
        orchestrator.prepare(config, parts.prepare(config))
    assert message in str(error.value)


def test_a_server_the_ceiling_rules_out_is_refused(make_config: Any, tmp_path: Path) -> None:
    site = direct_files(tmp_path)
    (site / "pool-ceiling.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\nmcp_servers: [other-server]\n"
    )
    config = load_config(make_config({"mcp_direct": DIRECT}))
    with pytest.raises(LauncherError) as error:
        orchestrator.prepare(config, parts.prepare(config))
    assert f"mcp_direct.servers.{SERVER}: not compiled" in str(error.value)


READ_TOOL = "attack-catalog-mcp_search_attacks"
NEW_TOOL = "attack-catalog-mcp_brand_new"


def admitted(directory: Path, upload: Path) -> tuple[set[str], set[str]]:
    """The tools the compiled endpoint allows and denies by name, and the runtime's."""
    policy = yaml.safe_load((directory / "policy.yaml").read_text())
    (endpoint,) = policy["network_policies"][f"mcp-{SERVER}"]["endpoints"]
    allowed = {
        name
        for rule in endpoint["rules"]
        if rule["allow"]["method"] == "tools/call"
        for name in rule["allow"]["tool"]["any"]
    }
    runtime = json.loads((upload / "runtime.json").read_text())["mcp"]["servers"][SERVER]
    assert set(runtime["tools"]["allow"]) == allowed  # the runtime's, from the same endpoint
    return allowed, set(runtime["tools"]["deny"])


def accept_revision(tmp_path: Path, tools: dict[str, str]) -> str:
    """A revision the user accepted for the project, in the store the worker and the
    middleware share (``[openshell.environments] policy_store``)."""
    entries = "".join(
        f'  {name}:\n    class: {effect}\n    reason: "Reviewed."\n'
        for name, effect in tools.items()
    )
    data = (
        "apiVersion: sandbox-science/v0alpha1\nkind: ToolClassification\n"
        f"server: {SERVER}\ntools:\n{entries}"
    ).encode()
    found = PolicyStore(tmp_path / "site" / "policy-store").accept_classification(
        "crypto", SERVER, data, Acceptance("user", datetime.now(UTC), "reviewed")
    )
    assert found.revision is not None
    return found.revision


def test_the_fence_follows_the_projects_revision_at_each_compile(
    make_config: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    direct_files(tmp_path)
    config = load_config(make_config({"mcp_direct": DIRECT}))
    layout = parts.prepare(config)
    # No revision: the checked-in classification.
    before, _ = admitted(*orchestrator.prepare(config, layout))
    assert READ_TOOL in before
    assert NEW_TOOL not in before
    assert "classification revision" not in capsys.readouterr().out
    # A revision accepted while the runtime runs reaches the fence at the next compile,
    # which every replacement makes (``replace`` calls ``prepare``).
    hex_ = accept_revision(tmp_path, {READ_TOOL: "browse", NEW_TOOL: "read"})
    after, _ = admitted(*orchestrator.prepare(config, layout))
    assert NEW_TOOL in after  # widened
    assert READ_TOOL not in after  # narrowed: browse is denied while automation is off
    assert f"accepted classification revision {hex_[:12]}" in capsys.readouterr().out
    egress = orchestrator.direct_egress(config)
    assert egress is not None
    assert egress.revisions == {SERVER: hex_}


@pytest.mark.parametrize("binding", ["not json", '{"revision": "' + "c" * 64 + '"}'])
def test_an_unreadable_binding_leaves_the_fence_on_the_checked_in_file(
    make_config: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str], binding: str
) -> None:
    direct_files(tmp_path)
    config = load_config(make_config({"mcp_direct": DIRECT}))
    layout = parts.prepare(config)
    catalog = admitted(*orchestrator.prepare(config, layout))
    accept_revision(tmp_path, {READ_TOOL: "read", NEW_TOOL: "read"})
    current = tmp_path / "site/policy-store/classifications/crypto" / SERVER / "current.json"
    current.write_text(binding)
    capsys.readouterr()
    assert admitted(*orchestrator.prepare(config, layout)) == catalog
    out = capsys.readouterr().out
    assert "classification binding cannot be read" in out
    assert "the policy middleware refuses the server's calls" in out


@pytest.mark.slow
def test_up_registers_attaches_and_binds_the_runtimes_provider(
    site: dict[str, Any], fake_openshell: Path, tmp_path: Path
) -> None:
    direct_files(tmp_path)
    state = tmp_path / "openshell.json"
    state.write_text(json.dumps({"providers": [], "sandboxes": {}}))
    owners = tmp_path / "cloudflare.env"
    owners.write_text(f"CF_ACCESS_CLIENT_ID={ID}\nCF_ACCESS_CLIENT_SECRET={SECRET}\n")
    owners.chmod(0o600)
    launcher = site["launch"](
        mcp_direct={
            **DIRECT,
            "provider_env_file": str(owners),
            "provider_profile": str(PROFILE),
        }
    )
    launcher.expect("Stop:")
    steps = site["journal"].read_text().splitlines()
    # The provider is registered from the owner's file before anything starts.
    assert steps[:3] == [
        "openshell provider profile lint cloudflare-access.yaml",
        "openshell provider profile import cloudflare-access.yaml",
        f"openshell provider create cloudflare-access {SERVER} "
        "CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET",
    ]
    made = json.loads(state.read_text())["sandboxes"]
    (name,) = [name for name in made if name.startswith("ss-orch-")]
    runtime = made[name]
    # Attached to the runtime's sandbox, whose policy has the server's endpoint.
    assert runtime["providers"] == [SERVER]
    # Labelled with its project, which the worker reads before a scoped client binds it.
    assert runtime["labels"] == {"sandbox-science.io/project": "crypto"}
    assert f"mcp-{SERVER}" in yaml.safe_load(runtime["policy"])["network_policies"]
    assert any(f"sandbox create {name}" in step and f"provider {SERVER}" in step for step in steps)
    # Bound to the direct path's project's revision once Ready.
    bindings = site["journal"].with_name("journal.bindings").read_text().splitlines()
    assert bindings == [f"bind crypto {runtime['id']}"]
    uploaded = json.loads((tmp_path / "uploads" / name / "runtime.json").read_text())
    assert uploaded["mcp"]["servers"][SERVER]["tools"]["allow"]
    launcher.process.send_signal(signal.SIGINT)
    assert launcher.finish() == 0
    assert ID not in launcher.output
    assert SECRET not in launcher.output
    layout = Layout(load_config(launcher.config).state_dir)
    for path in layout.root.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path


@pytest.mark.slow
def test_up_refuses_a_missing_provider_of_the_direct_path(
    site: dict[str, Any], tmp_path: Path
) -> None:
    direct_files(tmp_path)
    (tmp_path / "openshell.json").write_text(json.dumps({"providers": [], "sandboxes": {}}))
    launcher = site["launch"](mcp_direct=DIRECT)
    assert launcher.finish() == 1
    assert f"the OpenShell provider {SERVER} does not exist" in launcher.output
    assert site["journal"].read_text() == ""
