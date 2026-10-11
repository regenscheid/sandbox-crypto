# SPDX-License-Identifier: MIT
"""The worker creates, revises and deletes research sandboxes (D73), against a fake ``openshell``.

The fake keeps its state in a JSON file and behaves as OpenShell v0.1.3 did in the babbage
labs: ``sandbox get`` of an unknown name fails with "not found", ``policy get --base``
returns the filesystem section with OpenShell's baseline paths added, and ``policy set``
refuses a document that removes any filesystem path.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import EnvironmentRequest, EnvironmentState, ErrorKind
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.policy import HardFloor
from sandbox_science.worker import environments as environments_module
from sandbox_science.worker.admission import (
    MAX_DRAFTS,
    MAX_ENDPOINTS,
    MAX_FAILURES,
    Draft,
    DraftWatcher,
    parse_drafts,
)
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend, OpenShellBackend
from sandbox_science.worker.cli import main
from sandbox_science.worker.environments import (
    MAX_RECORDED,
    Admission,
    Environments,
    sandbox_name,
)
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import Activity, ExecutionManager
from sandbox_science.worker.settings import (
    EnvironmentSettings,
    LocalBackendSettings,
    OpenShellBackendSettings,
    load_settings,
)
from sandbox_science.worker.store import Store
from sandbox_science.worker.suspend import Suspender

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "policy.yaml"
TARGETS = ROOT / "examples" / "openshell" / "targets.yaml"
IMAGE = "docker.io/library/buildpack-deps:noble-scm"
TOKEN = "t" * 40
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]

FAKE_OPENSHELL = r"""
import json, os, sys, uuid
import yaml

BASELINE = {"read_only": ["/usr", "/lib", "/etc", "/var/log", "/proc", "/dev/urandom"],
            "read_write": ["/tmp", "/dev/null"]}
path = os.environ["FAKE_OPENSHELL_STATE"]
with open(path) as handle:
    state = json.load(handle)
argv = sys.argv[1:]
state["calls"].append(argv)
sandboxes = state["sandboxes"]


def fail(message):
    with open(path, "w") as handle:
        json.dump(state, handle)
    print("Error:   x " + message, file=sys.stderr)
    sys.exit(1)


def option(name):
    return argv[argv.index(name) + 1]


def enrich(fs):
    fs = dict(fs or {"include_workdir": True})
    for key, paths in BASELINE.items():
        fs[key] = list(fs.get(key, [])) + [p for p in paths if p not in fs.get(key, [])]
    return fs


command = argv[:2]
out = None
if command == ["sandbox", "create"]:
    if state.get("fail_create"):
        fail("the VM driver could not start the sandbox")
    name = option("--name")
    providers = [argv[i + 1] for i, a in enumerate(argv) if a == "--provider"]
    for provider in providers:
        if provider not in state["providers"]:
            fail(f"provider '{provider}' not found")
    if name in sandboxes:
        fail(f"sandbox '{name}' already exists")
    with open(option("--policy")) as handle:
        policy = yaml.safe_load(handle)
    policy["filesystem_policy"] = enrich(policy.get("filesystem_policy"))
    sandboxes[name] = {"id": str(uuid.uuid4()), "name": name, "image": option("--from"),
                       "providers": providers, "policy": policy, "version": 1}
    out = "created"
elif command == ["sandbox", "get"]:
    name = argv[2]
    if name not in sandboxes:
        fail("code: 'Some requested entity was not found', message: \"sandbox not found\"")
    box = sandboxes[name]
    out = json.dumps({"id": box["id"], "name": name, "phase": box.get("phase", "Ready"),
                      "configuration_admission": {"state": "accepted", "error": ""}})
elif command == ["sandbox", "stop"]:
    sandboxes[argv[2]]["phase"] = "Stopped"
    out = "stopped"
elif command == ["sandbox", "start"]:
    box = sandboxes[argv[2]]
    box["phase"] = "Ready"
    if state.get("new_id_on_start"):
        box["id"] = str(uuid.uuid4())
    out = "started"
elif command == ["sandbox", "delete"]:
    sandboxes.pop(argv[2], None)
    out = "deleted"
elif command == ["policy", "get"]:
    box = sandboxes[argv[2]]
    out = json.dumps({"policy": box["policy"], "version": box["version"], "status": "effective"})
elif command == ["policy", "set"]:
    if state.get("fail_policy_set"):
        fail("policy serialized size exceeds maximum")
    box = sandboxes[argv[2]]
    with open(option("--policy")) as handle:
        policy = yaml.safe_load(handle)
    old, new = box["policy"]["filesystem_policy"], policy.get("filesystem_policy")
    kept = new is not None and all(
        p in new.get(k, []) for k in ("read_only", "read_write") for p in old.get(k, [])
    )
    if not kept:
        fail("message: \"filesystem policy cannot be removed on a live sandbox\"")
    box["policy"], box["version"] = policy, box["version"] + 1
    out = "set"
elif command == ["rule", "get"]:
    if state.get("fail_rules"):
        fail("the gateway is unavailable")
    name = argv[2]
    status = option("--status") if "--status" in argv else None
    drafts = [d for d in state["drafts"].get(name, []) if status in (None, d["status"])]
    if not drafts:
        out = f"No network rules for sandbox '{name}'"
    else:
        # The text of OpenShell v0.1.3's sandbox_draft_get, colors off.
        lines = [f"Network Rules:  (version 3, {len(drafts)} chunks)", ""]
        for d in drafts:
            lines += [f"  Chunk: {d['id']}", f"  Status: {d['status']}",
                      f"  Rule: {d['rule']}", f"  Binary: {d['binary']}",
                      "  Confidence: 65%",
                      f"  Rationale: Allow {d['binary']} to connect to {d['endpoint']} (HTTPS)."]
            if d.get("security"):
                lines.append(f"  Security: {d['security']}")
            lines += [f"  Endpoints: {d['endpoint']} [L4]", f"  Binaries: {d['binary']}", ""]
        out = "\n".join(lines)
elif command == ["rule", "reject"]:
    name, chunk = argv[2], option("--chunk-id")
    for d in state["drafts"].get(name, []):
        if d["id"] == chunk:
            d["status"], d["reason"] = "rejected", option("--reason")
            break
    else:
        fail(f"draft chunk '{chunk}' not found")
    out = "OK Chunk rejected."
elif command == ["provider", "list"]:
    out = json.dumps({"providers": [{"name": n, "type": t} for n, t in state["providers"].items()]})
elif command == ["profile", "list"]:
    out = json.dumps([{"id": i, "endpoints": e} for i, e in state["profiles"].items()])
else:
    fail(f"unexpected command {argv}")
with open(path, "w") as handle:
    json.dump(state, handle)
print(out)
"""


class Fake:
    """The fake CLI's state file."""

    def __init__(self, root: Path) -> None:
        self.path = root / "openshell-state.json"
        self.cli = root / "openshell"
        self.cli.write_text(f"#!{sys.executable}\n{FAKE_OPENSHELL}")
        self.cli.chmod(0o755)
        self.write(
            {
                "calls": [],
                "sandboxes": {},
                "providers": {"github-readonly": "github-token"},
                "profiles": {"github-token": []},
                "drafts": {},
            }
        )

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text())

    def write(self, state: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(state))

    def update(self, **changes: Any) -> None:
        self.write({**self.read(), **changes})

    def calls(self, *command: str) -> list[list[str]]:
        return [call for call in self.read()["calls"] if call[: len(command)] == list(command)]

    def draft(
        self, sandbox: str, endpoint: str, binary: str = "/usr/bin/curl", **extra: str
    ) -> str:
        """Add a pending draft, as OpenShell's supervisor submits one for a refused connection."""
        state = self.read()
        drafts = state["drafts"].setdefault(sandbox, [])
        chunk = f"chunk-{len(drafts) + 1}"
        host, _, port = endpoint.rpartition(":")
        rule = "allow_" + re.sub(r"[^A-Za-z0-9_]", "", host.replace(".", "_").replace("-", "_"))
        drafts.append(
            {
                "id": chunk,
                "status": "pending",
                "rule": f"{rule}_{port}",
                "binary": binary,
                "endpoint": endpoint,
                **extra,
            }
        )
        self.write(state)
        return chunk

    def drafts(self, sandbox: str) -> dict[str, dict[str, str]]:
        return {draft["id"]: draft for draft in self.read()["drafts"].get(sandbox, [])}


def variant(tmp_path: Path, name: str, *, extra_rule: bool = False, providers: bool = True) -> Path:
    text = EXAMPLE.read_text()
    if extra_rule:
        marker = "    - name: read-only-web\n"
        assert marker in text
        text = text.replace(
            marker,
            "    - name: example-post\n      operation: http\n"
            "      match: { hosts: [example.org], methods: [POST] }\n      action: allow\n"
            + marker,
            1,
        )
    if not providers:
        text = re.sub(r"\ncredentials:\n(?:  .*\n|\n)*", "\n", text, count=1)
        assert "github-readonly" not in text
    path = tmp_path / name
    path.write_text(text)
    return path


class Setup:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.fake = Fake(tmp_path)
        ceiling = tmp_path / "pool-ceiling.yaml"
        ceiling.write_text("apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n")
        self.settings = OpenShellBackendSettings(
            cli=str(self.fake.cli),
            isolation_tier=3,
            environments=EnvironmentSettings(
                policy_store=tmp_path / "middleware",
                targets=TARGETS,
                ceiling=ceiling,
                profiles={"minimal": IMAGE},
            ),
        )
        self.state_dir = tmp_path / "worker"
        env = {"FAKE_OPENSHELL_STATE": str(self.fake.path), "PATH": os.environ["PATH"]}
        assert self.settings.environments is not None
        self.environments = Environments(
            self.settings, self.settings.environments, self.state_dir, env
        )
        self.released: list[str] = []

        async def release(environment_id: str) -> None:
            self.released.append(environment_id)

        self.environments.release = release
        self.policy = self.add(EXAMPLE)

    def add(self, path: Path) -> str:
        return self.environments.store.add_policy(path.read_bytes()).digest

    def request(self, environment_id: str = "env-1", **changes: str) -> EnvironmentRequest:
        fields = {
            "environment_id": environment_id,
            "project_id": "p1",
            "profile": "minimal",
            "policy": self.policy,
        }
        return EnvironmentRequest.model_validate({**fields, **changes})


@pytest_asyncio.fixture
async def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


async def test_create_compiles_attaches_exactly_the_listed_providers_and_binds(
    setup: Setup,
) -> None:
    info, created = await setup.environments.create(setup.request())
    assert created
    assert info.state is EnvironmentState.READY
    assert info.sandbox == sandbox_name("env-1")
    assert info.providers == ("github-readonly",)
    assert info.sandbox_id is not None
    box = setup.fake.read()["sandboxes"][info.sandbox]
    assert box["image"] == IMAGE
    assert box["providers"] == ["github-readonly"]
    assert "/dev/shm" in box["policy"]["filesystem_policy"]["read_write"]  # noqa: S108 - a device path
    config = box["policy"]["network_middlewares"]["sandbox-science-policy"]["config"]
    assert config["policy"]["digest"] == setup.policy
    [create] = setup.fake.calls("sandbox", "create")
    assert "--no-auto-providers" in create
    assert setup.environments.store.binding(info.sandbox_id) == setup.policy
    assert setup.environments.sandbox("env-1") == info.sandbox
    assert setup.environments.get("env-1") == info


async def test_create_is_idempotent_and_refuses_another_request(setup: Setup) -> None:
    first, _ = await setup.environments.create(setup.request())
    again, created = await setup.environments.create(setup.request())
    assert (again, created) == (first, False)
    assert len(setup.fake.calls("sandbox", "create")) == 1
    other = setup.add(variant(setup.tmp, "v2.yaml", extra_rule=True))
    with pytest.raises(WorkerError) as refused:
        await setup.environments.create(setup.request(policy=other))
    assert refused.value.kind is ErrorKind.CONFLICT


@pytest.mark.parametrize(
    ("changes", "kind"),
    [
        ({"profile": "crypto-base"}, ErrorKind.UNSUPPORTED),
        ({"policy": "sha256:" + "0" * 64}, ErrorKind.NOT_FOUND),
    ],
)
async def test_unknown_profiles_and_revisions_are_refused(
    setup: Setup, changes: dict[str, str], kind: ErrorKind
) -> None:
    with pytest.raises(WorkerError) as refused:
        await setup.environments.create(setup.request(**changes))
    assert refused.value.kind is kind
    assert setup.fake.calls("sandbox", "create") == []


@pytest.mark.parametrize(
    ("providers", "profiles", "message"),
    [
        ({}, {"github-token": []}, "not registered"),
        ({"github-readonly": "github-app"}, {"github-app": []}, "the policy says"),
        (
            {"github-readonly": "github-token"},
            {"github-token": [{"host": "api.github.com", "port": 443}]},
            "names endpoints",
        ),
    ],
)
async def test_providers_must_be_registered_with_profiles_without_endpoints(
    setup: Setup, providers: dict[str, str], profiles: dict[str, Any], message: str
) -> None:
    setup.fake.update(providers=providers, profiles=profiles)
    with pytest.raises(WorkerError, match=message) as refused:
        await setup.environments.create(setup.request())
    assert refused.value.kind is ErrorKind.UNAVAILABLE
    assert setup.fake.calls("sandbox", "create") == []
    with pytest.raises(WorkerError):
        setup.environments.get("env-1")


async def test_a_revision_carries_the_live_filesystem_section_and_rebinds(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    assert info.sandbox_id is not None
    revision = setup.add(variant(setup.tmp, "v2.yaml", extra_rule=True))
    revised = await setup.environments.revise("env-1", revision)
    assert revised.policy == revision
    box = setup.fake.read()["sandboxes"][info.sandbox]
    assert box["version"] == 2
    # The baseline paths OpenShell added at creation are still there.
    assert "/usr" in box["policy"]["filesystem_policy"]["read_only"]
    config = box["policy"]["network_middlewares"]["sandbox-science-policy"]["config"]
    assert config["policy"]["digest"] == revision
    assert setup.environments.store.binding(info.sandbox_id) == revision
    await setup.environments.revise("env-1", revision)
    assert len(setup.fake.calls("policy", "set")) == 1


async def test_a_revision_with_other_providers_is_refused(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    assert info.sandbox_id is not None
    revision = setup.add(variant(setup.tmp, "no-providers.yaml", providers=False))
    with pytest.raises(WorkerError) as refused:
        await setup.environments.revise("env-1", revision)
    assert refused.value.kind is ErrorKind.CONFLICT
    assert setup.fake.calls("policy", "set") == []
    assert setup.environments.store.binding(info.sandbox_id) == setup.policy


async def test_delete_stops_the_executor_unbinds_and_allows_a_new_sandbox(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    assert info.sandbox_id is not None
    deleted = await setup.environments.delete("env-1")
    assert deleted.state is EnvironmentState.DELETED
    assert setup.released == ["env-1"]
    assert info.sandbox not in setup.fake.read()["sandboxes"]
    assert setup.environments.store.binding(info.sandbox_id) is None
    assert setup.environments.sandbox("env-1") is None
    assert await setup.environments.delete("env-1") == deleted
    assert len(setup.fake.calls("sandbox", "delete")) == 1
    again, created = await setup.environments.create(setup.request())
    assert created
    assert again.state is EnvironmentState.READY
    assert again.sandbox_id != info.sandbox_id


async def test_a_sandbox_left_by_a_crash_is_replaced(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    # As if the worker had died between creating the sandbox and recording it as ready.
    setup.environments.records.save(info.model_copy(update={"state": EnvironmentState.CREATING}))
    replaced, created = await setup.environments.create(setup.request())
    assert created
    assert replaced.sandbox_id != info.sandbox_id
    assert len(setup.fake.calls("sandbox", "delete")) == 1


async def test_a_failed_creation_leaves_no_sandbox_or_binding(setup: Setup) -> None:
    setup.fake.update(fail_create=True)
    with pytest.raises(WorkerError, match="could not start") as refused:
        await setup.environments.create(setup.request())
    assert refused.value.kind is ErrorKind.UNAVAILABLE
    assert setup.environments.get("env-1").state is EnvironmentState.DELETED
    assert setup.environments.sandbox("env-1") is None
    assert setup.fake.read()["sandboxes"] == {}


async def test_the_backend_finds_sandboxes_the_worker_created(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    static = setup.settings.model_copy(update={"sandboxes": {"env-2": "made-by-hand"}})
    backend = OpenShellBackend(static, setup.state_dir, setup.environments.sandbox)
    assert backend.sandbox_id("env-1") == info.sandbox
    assert backend.sandbox_id("env-2") == "made-by-hand"
    assert backend.sandbox_id("env-3") is None


def idle_manager(root: Path) -> ExecutionManager:
    """An execution manager that never starts an executor in these tests."""
    return ExecutionManager(
        Store(root / "worker.sqlite3"),
        LocalBackend(LocalBackendSettings(), root),
        Blobs(root / "artifacts"),
        pool_id="test",
        max_artifact_size=1 << 20,
        state_dir=root,
    )


async def test_the_api_manages_environments(
    setup: Setup, aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    manager = idle_manager(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    client = await aiohttp_client(create_app(manager, TOKEN, setup.environments))
    body = setup.request().model_dump(mode="json")
    response = await client.post("/v1/environments", json=body, headers=headers)
    assert response.status == 201
    response = await client.post("/v1/environments", json=body, headers=headers)
    assert response.status == 200
    response = await client.get("/v1/environments/env-1", headers=headers)
    assert (await response.json())["state"] == "ready"
    revision = setup.add(variant(setup.tmp, "v2.yaml", extra_rule=True))
    response = await client.post(
        "/v1/environments/env-1/policy", json={"policy": revision}, headers=headers
    )
    assert (await response.json())["policy"] == revision
    response = await client.post(
        "/v1/environments/env-1/policy", json={"policy": "not-a-digest"}, headers=headers
    )
    assert response.status == 400
    response = await client.delete("/v1/environments/env-1", headers=headers)
    assert (await response.json())["state"] == "deleted"
    response = await client.get("/v1/environments/nope", headers=headers)
    assert response.status == 404
    plain = await aiohttp_client(create_app(manager, TOKEN))
    response = await plain.post("/v1/environments", json=body, headers=headers)
    assert response.status == 422
    assert (await response.json())["error"]["kind"] == "unsupported"


async def test_the_harness_client_manages_environments(
    setup: Setup, aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    client = await aiohttp_client(create_app(idle_manager(tmp_path), TOKEN, setup.environments))
    async with WorkerClient(str(client.make_url("")).rstrip("/"), TOKEN) as worker:
        info = await worker.create_environment(setup.request())
        assert info.state is EnvironmentState.READY
        assert await worker.environment("env-1") == info
        revision = setup.add(variant(setup.tmp, "v2.yaml", extra_rule=True))
        assert (await worker.revise_environment("env-1", revision)).policy == revision
        deleted = await worker.delete_environment("env-1")
        assert deleted.state is EnvironmentState.DELETED
        with pytest.raises(WorkerAPIError) as missing:
            await worker.environment("nope")
        assert missing.value.kind is ErrorKind.NOT_FOUND


async def test_policy_add_stores_revisions_for_environments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pool-ceiling.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
    )
    settings = tmp_path / "worker.toml"
    settings.write_text(
        'token_file = "worker.token"\nstate_dir = "state"\n\n[openshell]\nisolation_tier = 3\n\n'
        "[openshell.environments]\n"
        f'policy_store = "middleware"\ntargets = "{TARGETS}"\nceiling = "pool-ceiling.yaml"\n'
        f'profiles = {{minimal = "{IMAGE}"}}\n'
    )
    loaded = load_settings(settings)
    assert loaded.openshell is not None
    assert loaded.openshell.environments is not None
    assert loaded.openshell.environments.policy_store == tmp_path / "middleware"
    assert main(["policy", "add", "--settings", str(settings), str(EXAMPLE)]) == 0
    output = capsys.readouterr().out
    digest = re.search(r"as (sha256:[0-9a-f]{64})", output)
    assert digest is not None
    assert (tmp_path / "middleware" / "policies" / f"{digest[1][7:]}.yaml").exists()
    broken = tmp_path / "broken.yaml"
    broken.write_text("kind: ProjectPolicy\n")
    assert main(["policy", "add", "--settings", str(settings), str(broken)]) == 1


async def test_sandbox_names_are_stable_short_and_distinct() -> None:
    names = {
        sandbox_name(environment_id) for environment_id in ("env-1", "ENV-1", "env_1", "env.1")
    }
    assert len(names) == 4
    for name in names:
        assert re.fullmatch(r"ss-[a-z0-9-]+-[0-9a-f]{8}", name)
    assert sandbox_name("env-1") == sandbox_name("env-1")
    assert len(sandbox_name("x" * 128)) <= 52


# On-demand admission (D69).


def deny_variant(tmp_path: Path, name: str, host: str) -> Path:
    """The example policy with exact deny rules for one host, which shadow its '*' rules.

    Without ``ports`` a rule matches only its scheme's default port, so plain HTTP to
    port 443 would still take the default action.
    """
    text = EXAMPLE.read_text()
    marker = "    - name: read-only-web\n"
    text = text.replace(
        marker,
        "".join(
            f"    - name: blocked-{kind}\n      operation: {kind}\n"
            f"      match: {{ hosts: [{host}], ports: [80, 443] }}\n      action: deny\n"
            for kind in ("http", "git")
        )
        + marker,
        1,
    )
    path = tmp_path / name
    path.write_text(text)
    return path


def admitted_endpoints(setup: Setup, sandbox: str) -> list[str]:
    policies = setup.fake.read()["sandboxes"][sandbox]["policy"]["network_policies"]
    section = policies.get("sandbox-science-admitted", {"endpoints": []})
    return [endpoint["host"] for endpoint in section["endpoints"]]


async def test_unnamed_hosts_the_policy_reaches_are_admitted_on_demand(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    eprint = setup.fake.draft(info.sandbox, "eprint.iacr.org:443")
    registry = setup.fake.draft(info.sandbox, "pypi.org:443", "/usr/bin/python3.13")
    lan = setup.fake.draft(info.sandbox, "printer.local:443")
    decisions = await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert {(d.host, d.admitted) for d in decisions} == {
        ("eprint.iacr.org", True),
        ("pypi.org", False),
        ("printer.local", False),
    }
    # One revision of the same policy names the admitted host, with the address ranges.
    assert len(setup.fake.calls("policy", "set")) == 1
    assert admitted_endpoints(setup, info.sandbox) == ["eprint.iacr.org"]
    box = setup.fake.read()["sandboxes"][info.sandbox]["policy"]
    [endpoint] = box["network_policies"]["sandbox-science-admitted"]["endpoints"]
    assert (
        endpoint["allowed_ips"]
        == box["network_policies"]["sandbox-science-egress"]["endpoints"][0]["allowed_ips"]
    )
    revised = setup.environments.get("env-1")
    assert revised.admitted_hosts == ("eprint.iacr.org",)
    assert revised.policy == setup.policy
    assert setup.environments.store.binding(info.sandbox_id or "") == setup.policy
    # Every draft is rejected, never approved, with the decision as the reason.
    drafts = setup.fake.drafts(info.sandbox)
    assert {draft["status"] for draft in drafts.values()} == {"rejected"}
    assert "admitted on demand" in drafts[eprint]["reason"]
    assert "package registry" in drafts[registry]["reason"]
    assert "not public" in drafts[lan]["reason"]
    assert setup.fake.calls("rule", "approve") == []
    recorded = setup.environments.records.admissions("env-1")
    assert {(a.host, a.port, a.admitted) for a in recorded} == {
        ("eprint.iacr.org", 443, True),
        ("pypi.org", 443, False),
        ("printer.local", 443, False),
    }


async def test_drafts_for_admitted_hosts_need_no_revision(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    watcher = DraftWatcher(setup.environments, 1.0)
    setup.fake.draft(info.sandbox, "eprint.iacr.org:443")
    await watcher.check("env-1", info.sandbox)
    # Another program, raced with the revision; a name the policy names, refused by
    # OpenShell's address check; and nothing pending at all.
    again = setup.fake.draft(info.sandbox, "eprint.iacr.org:443", "/usr/bin/wget")
    private = setup.fake.draft(
        info.sandbox,
        "github.com:443",
        security="This connection was blocked by SSRF protection. Private IP access requires "
        "an explicit `allowed_ips` policy entry.",
    )
    await watcher.check("env-1", info.sandbox)
    assert await watcher.check("env-1", info.sandbox) == ()
    assert len(setup.fake.calls("policy", "set")) == 1
    drafts = setup.fake.drafts(info.sandbox)
    assert "admitted already" in drafts[again]["reason"]
    assert "not public" in drafts[private]["reason"]
    assert setup.environments.get("env-1").admitted_hosts == ("eprint.iacr.org",)


async def test_admission_stops_at_the_environments_limit(setup: Setup) -> None:
    setup.environments.settings = setup.environments.settings.model_copy(
        update={"max_admitted_hosts": 1}
    )
    info, _ = await setup.environments.create(setup.request())
    setup.fake.draft(info.sandbox, "a.example.net:443")
    second = setup.fake.draft(info.sandbox, "b.example.net:443")
    await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert setup.environments.get("env-1").admitted_hosts == ("a.example.net",)
    assert "its limit" in setup.fake.drafts(info.sandbox)[second]["reason"]


async def test_a_revision_decides_recorded_hosts_again(setup: Setup) -> None:
    blocked = setup.add(deny_variant(setup.tmp, "blocked.yaml", "eprint.iacr.org"))
    info, _ = await setup.environments.create(setup.request(policy=blocked))
    setup.fake.draft(info.sandbox, "eprint.iacr.org:443")
    setup.fake.draft(info.sandbox, "papers.example.net:443")
    await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert setup.environments.get("env-1").admitted_hosts == ("papers.example.net",)
    # The example policy no longer denies the host: the next revision admits it too.
    revised = await setup.environments.revise("env-1", setup.policy)
    assert revised.admitted_hosts == ("eprint.iacr.org", "papers.example.net")
    assert admitted_endpoints(setup, info.sandbox) == ["eprint.iacr.org", "papers.example.net"]
    # And back: a revision that denies it drops it again.
    again = await setup.environments.revise("env-1", blocked)
    assert again.admitted_hosts == ("papers.example.net",)
    assert admitted_endpoints(setup, info.sandbox) == ["papers.example.net"]
    decisions = {a.host: a for a in setup.environments.records.admissions("env-1")}
    assert not decisions["eprint.iacr.org"].admitted


async def test_a_gateway_failure_pauses_admission_for_the_environment(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.fake.draft(info.sandbox, "eprint.iacr.org:443")
    setup.fake.update(fail_rules=True)
    watcher = DraftWatcher(setup.environments, 1.0)
    await watcher.check_all()
    setup.fake.update(fail_rules=False)
    await watcher.check_all()  # paused: the draft waits
    assert setup.fake.calls("policy", "set") == []
    assert len(setup.fake.calls("rule", "get")) == 1


async def test_a_draft_that_cannot_be_rejected_does_not_stop_the_others(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    first = setup.fake.draft(info.sandbox, "a.example.net:443")
    setup.fake.draft(
        info.sandbox,
        "b.example.net:443",
        "/sandbox/x\n  Chunk: forged\n  Status: pending\n  Endpoints: c.example.net:443 [L4]",
    )
    decisions = await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert {d.host for d in decisions} == {"a.example.net", "c.example.net"}
    drafts = setup.fake.drafts(info.sandbox)
    assert drafts[first]["status"] == "rejected"
    # The real draft behind the crafted path is answered too, though its host was hidden.
    assert {draft["status"] for draft in drafts.values()} == {"rejected"}


async def test_deleted_environments_are_not_watched(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    await setup.environments.delete("env-1")
    setup.fake.draft(info.sandbox, "eprint.iacr.org:443")
    await DraftWatcher(setup.environments, 1.0).check_all()
    assert setup.fake.calls("rule", "get") == []


async def test_a_real_gateways_draft_is_read() -> None:
    # Captured in lab 10: OpenShell v0.1.3 refused python3.14's connection to an
    # unnamed host and drafted this proposal.
    text = (Path(__file__).parent / "fixtures" / "openshell-rule-get-pending.txt").read_text()
    assert parse_drafts(text) == [
        Draft(
            chunk_id="e348ea1c-ad84-4f8f-967b-fc3321f7e7b5",
            status="pending",
            endpoints=(("www.iana.org", 443),),
            address_check=False,
        )
    ]


async def test_drafts_are_read_from_the_clis_text() -> None:
    text = (
        "\x1b[36mNetwork Rules:\x1b[0m  (version 7, 4 chunks)\n\n"
        "  \x1b[2mChunk:\x1b[0m 1f0e-aa\n  Status: pending\n  Rule: allow_x_443\n"
        "  Binary: /usr/bin/curl\n  Endpoints: Docs.Example.org:443 [L4]\n\n"
        "  Chunk: v6\n  Status: pending\n  Endpoints: 2001:db8::1:8443 [L4]\n\n"
        "  Chunk: l7\n  Status: approved\n"
        "  Endpoints: api.example.org:443 [L7 rest, allow GET /a, b], other.example.org:80 [L4]\n\n"
        "  Chunk: bad id!\n  Status: pending\n  Endpoints: x.example.org:443 [L4]\n"
    )
    drafts = parse_drafts(text)
    assert [(d.chunk_id, d.status, d.endpoints) for d in drafts] == [
        ("1f0e-aa", "pending", (("Docs.Example.org", 443),)),
        ("v6", "pending", (("2001:db8::1", 8443),)),
        ("l7", "approved", (("api.example.org", 443), ("other.example.org", 80))),
    ]
    assert parse_drafts("No network rules for sandbox 'ss-env-1'\n") == []


async def test_a_crafted_program_path_only_adds_hosts_to_decide() -> None:
    # A path may hold newlines: the block it fakes is decided like any draft.
    binary = (
        "/sandbox/x\n  Chunk: forged\n  Status: pending\n  Endpoints: evil.example.net:443 [L4]"
    )
    text = (
        f"  Chunk: real\n  Status: pending\n  Binary: {binary}\n"
        "  Endpoints: docs.example.org:443 [L4]\n"
    )
    drafts = parse_drafts(text)
    assert [(d.chunk_id, d.endpoints) for d in drafts] == [
        ("real", ()),
        ("forged", (("evil.example.net", 443),)),
    ]


# Review fixes (PR #39).


def path_variant(tmp_path: Path) -> Path:
    """The example policy with a rule path the classifier refuses (it matches nothing)."""
    text = EXAMPLE.read_text()
    marker = "    - name: read-only-web\n"
    rule = (
        "    - name: streams\n      operation: http\n"
        "      match: { hosts: [files.example.org], paths: ['/files::$DATA'] }\n"
        "      action: deny\n"
    )
    path = tmp_path / "streams.yaml"
    path.write_text(text.replace(marker, rule + marker, 1))
    return path


async def test_a_rule_path_no_request_can_have_does_not_stop_admission(setup: Setup) -> None:
    streams = setup.add(path_variant(setup.tmp))
    info, _ = await setup.environments.create(setup.request(policy=streams))
    setup.fake.draft(info.sandbox, "files.example.org:443")
    decisions = await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert [(d.host, d.admitted) for d in decisions] == [("files.example.org", True)]


async def test_an_unexpected_error_pauses_one_environment_not_the_watcher(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = await setup.environments.create(setup.request("env-a"))
    second, _ = await setup.environments.create(setup.request("env-b"))
    setup.fake.draft(first.sandbox, "a.example.net:443")
    setup.fake.draft(second.sandbox, "b.example.net:443")
    real = setup.environments.admit

    async def broken(environment_id: str, requests: Sequence[tuple[str, int]]) -> list[Admission]:
        if environment_id == "env-a":
            raise RuntimeError("database is locked")
        return await real(environment_id, requests)

    monkeypatch.setattr(setup.environments, "admit", broken)
    await DraftWatcher(setup.environments, 1.0).check_all()
    assert setup.environments.get("env-b").admitted_hosts == ("b.example.net",)
    assert setup.environments.get("env-a").admitted_hosts == ()


async def test_one_check_handles_a_bounded_number_of_drafts_and_endpoints(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    for index in range(MAX_DRAFTS + 8):
        setup.fake.draft(info.sandbox, f"h{index}.example.net:443")
    decisions = await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert len(decisions) == MAX_DRAFTS
    pending = [d for d in setup.fake.drafts(info.sandbox).values() if d["status"] == "pending"]
    assert len(pending) == 8
    # A crafted program path can fake many endpoints; one check decides at most this many.
    other, _ = await setup.environments.create(setup.request("env-2"))
    forged = ", ".join(f"f{i}.example.org:443 [L4]" for i in range(MAX_ENDPOINTS + 40))
    setup.fake.draft(other.sandbox, f"{forged}, x.example.org:443")
    decisions = await DraftWatcher(setup.environments, 1.0).check("env-2", other.sandbox)
    assert len(decisions) == MAX_ENDPOINTS


async def test_decision_records_are_bounded_and_forgotten_on_delete(setup: Setup) -> None:
    await setup.environments.create(setup.request())
    now = datetime.now(UTC)
    refusals = [Admission(f"r{i}.example.net", 443, False, "no", now) for i in range(600)]
    setup.environments.records.decide(
        "env-1", [Admission("kept.example.net", 443, True, "admitted", now)]
    )
    setup.environments.records.decide("env-1", refusals)
    recorded = setup.environments.records.admissions("env-1")
    assert len(recorded) == MAX_RECORDED
    assert any(a.host == "kept.example.net" for a in recorded)
    await setup.environments.delete("env-1")
    assert setup.environments.records.admissions("env-1") == []


async def test_a_revision_keeps_the_hosts_in_use_at_the_limit(setup: Setup) -> None:
    setup.environments.settings = setup.environments.settings.model_copy(
        update={"max_admitted_hosts": 2}
    )
    info, _ = await setup.environments.create(setup.request())
    watcher = DraftWatcher(setup.environments, 1.0)
    for host in ("a.example.net", "b.example.net", "c.example.net"):
        setup.fake.draft(info.sandbox, f"{host}:443")
    await watcher.check("env-1", info.sandbox)
    assert setup.environments.get("env-1").admitted_hosts == ("a.example.net", "b.example.net")
    # Another program's draft for a host in use refreshes its record (review L1).
    setup.fake.draft(info.sandbox, "a.example.net:443", "/usr/bin/wget")
    await watcher.check("env-1", info.sandbox)
    revision = setup.add(variant(setup.tmp, "v2.yaml", extra_rule=True))
    revised = await setup.environments.revise("env-1", revision)
    assert revised.admitted_hosts == ("a.example.net", "b.example.net")


async def test_a_batch_whose_revision_keeps_failing_is_refused(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    chunk = setup.fake.draft(info.sandbox, "a.example.net:443")
    setup.fake.update(fail_policy_set=True)
    watcher = DraftWatcher(setup.environments, 1.0)
    for _ in range(MAX_FAILURES - 1):
        with pytest.raises(WorkerError):
            await watcher.check("env-1", info.sandbox)
    assert setup.fake.drafts(info.sandbox)[chunk]["status"] == "pending"
    decisions = await watcher.check("env-1", info.sandbox)
    assert [(d.host, d.admitted) for d in decisions] == [("a.example.net", False)]
    assert "could not revise" in setup.fake.drafts(info.sandbox)[chunk]["reason"]
    assert setup.environments.get("env-1").admitted_hosts == ()


async def test_admission_stops_before_the_policy_grows_too_large(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    info, _ = await setup.environments.create(setup.request())
    monkeypatch.setattr(environments_module, "MAX_POLICY_YAML", 1)
    chunk = setup.fake.draft(info.sandbox, "a.example.net:443")
    decisions = await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert [(d.host, d.admitted) for d in decisions] == [("a.example.net", False)]
    assert "size limit" in setup.fake.drafts(info.sandbox)[chunk]["reason"]
    assert setup.fake.calls("policy", "set") == []


async def test_admitted_names_cannot_reach_the_deployments_admin_addresses(setup: Setup) -> None:
    # The middleware sees only names, so the ranges must leave these out (review H1).
    setup.environments.floor = HardFloor(frozenset({"34.120.1.2"}))
    info, _ = await setup.environments.create(setup.request())
    setup.fake.draft(info.sandbox, "34-120-1-2.sslip.io:443")
    await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    policies = setup.fake.read()["sandboxes"][info.sandbox]["policy"]["network_policies"]
    [endpoint] = policies["sandbox-science-admitted"]["endpoints"]
    covered = [
        block
        for block in endpoint["allowed_ips"]
        if ipaddress.ip_address("34.120.1.2") in ipaddress.ip_network(block)
        or ipaddress.ip_address("168.63.129.16") in ipaddress.ip_network(block)
    ]
    assert covered == []


async def test_text_from_the_sandbox_is_made_printable(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    chunk = setup.fake.draft(info.sandbox, "evil\x01.example.org:443")
    decisions = await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert all(d.host.isprintable() for d in decisions)
    assert setup.fake.drafts(info.sandbox)[chunk]["reason"].isprintable()


# Idle suspension (W3).


async def always(environment_id: str, idle_since: float) -> bool:
    return True


async def test_a_suspended_environment_keeps_its_sandbox_and_resumes_on_use(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.environments.release_idle = always
    assert await setup.environments.suspend("env-1", time.monotonic())
    stopped = setup.environments.get("env-1")
    assert stopped.state is EnvironmentState.STOPPED
    assert setup.fake.calls("sandbox", "stop") == [["sandbox", "stop", info.sandbox]]
    # Stopped: no executor may run there, no drafts are read, and it is not suspended again.
    assert setup.environments.sandbox("env-1") is None
    assert setup.environments.records.ready() == []
    assert not await setup.environments.suspend("env-1", time.monotonic())
    await setup.environments.resume("env-1")
    resumed = setup.environments.get("env-1")
    assert resumed.state is EnvironmentState.READY
    assert resumed.sandbox_id == info.sandbox_id
    assert setup.environments.sandbox("env-1") == info.sandbox
    assert len(setup.fake.calls("sandbox", "start")) == 1
    await setup.environments.resume("env-1")  # ready: nothing to do
    assert len(setup.fake.calls("sandbox", "start")) == 1
    # The middleware's binding never changed.
    assert setup.environments.store.binding(info.sandbox_id or "") == setup.policy


async def test_an_environment_in_use_is_not_suspended(setup: Setup) -> None:
    await setup.environments.create(setup.request())

    async def busy(environment_id: str, idle_since: float) -> bool:
        return False

    setup.environments.release_idle = busy
    assert not await setup.environments.suspend("env-1", time.monotonic())
    assert setup.environments.get("env-1").state is EnvironmentState.READY
    assert setup.fake.calls("sandbox", "stop") == []


async def test_create_and_revise_start_a_suspended_environment(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.environments.release_idle = always
    await setup.environments.suspend("env-1", time.monotonic())
    again, created = await setup.environments.create(setup.request())
    assert not created
    assert again.state is EnvironmentState.READY
    await setup.environments.suspend("env-1", time.monotonic())
    revision = setup.add(variant(setup.tmp, "v2.yaml", extra_rule=True))
    revised = await setup.environments.revise("env-1", revision)
    assert (revised.state, revised.policy) == (EnvironmentState.READY, revision)
    calls = [call[:2] for call in setup.fake.read()["calls"]]
    assert calls.index(["sandbox", "start"]) < calls.index(["policy", "set"])
    assert info.sandbox_id == revised.sandbox_id


async def test_a_sandbox_that_comes_back_as_another_is_refused(setup: Setup) -> None:
    await setup.environments.create(setup.request())
    setup.environments.release_idle = always
    await setup.environments.suspend("env-1", time.monotonic())
    setup.fake.update(new_id_on_start=True)
    with pytest.raises(WorkerError, match="came back as") as refused:
        await setup.environments.resume("env-1")
    assert refused.value.kind is ErrorKind.UNAVAILABLE
    assert setup.environments.get("env-1").state is EnvironmentState.STOPPED


class StubManager:
    """What the suspender asks the execution manager, set by each test."""

    def __init__(self) -> None:
        self.activities: dict[str, Activity | None] = {}

    async def activity(self, environment_id: str) -> Activity | None:
        return self.activities.get(environment_id)


async def test_the_suspender_stops_only_idle_environments(setup: Setup) -> None:
    for name in ("idle", "running", "computing", "unknown", "quiet", "recent"):
        await setup.environments.create(setup.request(f"env-{name}"))
    setup.environments.release_idle = always
    stub = StubManager()
    long_ago = time.monotonic() - 100
    stub.activities = {
        "env-idle": Activity(long_ago, 0, None, 0),
        "env-running": Activity(long_ago, 1, 2.0, 1),
        "env-computing": Activity(long_ago, 0, 1.0, 1),
        "env-unknown": None,
        "env-quiet": Activity(long_ago, 0, 3.0, 1),
        "env-recent": Activity(time.monotonic(), 0, None, 0),
    }
    suspender = Suspender(setup.environments, cast(ExecutionManager, stub), 10.0, interval=1)
    suspender.started = long_ago
    # An executor's CPU needs two samples to tell.
    assert await suspender.check() == ["env-idle"]
    stub.activities["env-computing"] = Activity(long_ago, 0, 30.0, 1)
    assert await suspender.check() == ["env-quiet"]
    states = {
        info.environment_id: info.state
        for name in ("idle", "running", "computing", "unknown", "quiet", "recent")
        if (info := setup.environments.get(f"env-{name}"))
    }
    assert {name for name, state in states.items() if state is EnvironmentState.STOPPED} == {
        "env-idle",
        "env-quiet",
    }


async def test_a_failed_suspension_leaves_the_others_to_go_on(setup: Setup) -> None:
    for name in ("a", "b"):
        await setup.environments.create(setup.request(f"env-{name}"))
    setup.environments.release_idle = always
    stub = StubManager()
    long_ago = time.monotonic() - 100
    stub.activities = {name: Activity(long_ago, 0, None, 0) for name in ("env-a", "env-b")}
    suspender = Suspender(setup.environments, cast(ExecutionManager, stub), 10.0, interval=1)
    suspender.started = long_ago
    real = setup.environments.cli.run

    async def flaky(*args: str, timeout: float = 120.0) -> str:
        if args[:2] == ("sandbox", "stop") and args[2] == sandbox_name("env-a"):
            raise WorkerError(ErrorKind.UNAVAILABLE, "openshell sandbox stop failed")
        return await real(*args, timeout=timeout)

    setup.environments.cli.run = flaky  # type: ignore[method-assign]
    assert await suspender.check() == ["env-b"]
    assert setup.environments.get("env-a").state is EnvironmentState.READY


async def test_admission_notices_say_what_was_admitted_and_refused(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    before = datetime.now(UTC)
    setup.fake.draft(info.sandbox, "eprint.iacr.org:443")
    setup.fake.draft(info.sandbox, "pypi.org:443")
    watcher = DraftWatcher(setup.environments, 1.0)
    await watcher.check("env-1", info.sandbox)
    notices = [text for _, text in setup.environments.notices("env-1", before)]
    assert notices[0].startswith("eprint.iacr.org was admitted on demand")
    assert notices[1].startswith("pypi.org:443 was not admitted: ")
    seen = max(at for at, _ in setup.environments.notices("env-1", before))
    # Another program for an admitted host is no news.
    setup.fake.draft(info.sandbox, "eprint.iacr.org:443", "/usr/bin/wget")
    await watcher.check("env-1", info.sandbox)
    assert setup.environments.notices("env-1", seen) == []


async def test_operators_can_list_an_environments_admissions(
    setup: Setup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.fake.draft(info.sandbox, "eprint.iacr.org:443")
    setup.fake.draft(info.sandbox, "pypi.org:443")
    await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    settings = tmp_path / "worker.toml"
    settings.write_text(
        f'token_file = "worker.token"\nstate_dir = "{setup.state_dir}"\n\n'
        "[openshell]\nisolation_tier = 3\n\n[openshell.environments]\n"
        f'policy_store = "{tmp_path / "middleware"}"\ntargets = "{TARGETS}"\n'
        f'ceiling = "{tmp_path / "pool-ceiling.yaml"}"\nprofiles = {{minimal = "{IMAGE}"}}\n'
    )
    assert main(["admissions", "--settings", str(settings), "--environment", "env-1"]) == 0
    out = capsys.readouterr().out
    assert "admitted eprint.iacr.org" in out
    assert "pypi.org:443 refused: " in out
    assert main(["admissions", "--settings", str(settings), "--environment", "nope"]) == 1
