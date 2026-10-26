# SPDX-License-Identifier: MIT
"""The worker creates, revises and deletes research sandboxes (D73), against a fake ``openshell``.

The fake keeps its state in a JSON file and behaves as OpenShell v0.1.3 did in the babbage
labs: ``sandbox get`` of an unknown name fails with "not found", ``policy get --base``
returns the filesystem section with OpenShell's baseline paths added, and ``policy set``
refuses a document that removes any filesystem path.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from pydantic import ValidationError

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
    ExecutionContext,
    PackageCommand,
    ProvisionOperation,
    ProvisionRequest,
    ProvisionState,
    new_id,
)
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.policy import HardFloor
from sandbox_science.policy.store import PolicyStore
from sandbox_science.testing import FakeClock
from sandbox_science.worker import environments as environments_module
from sandbox_science.worker import suspend as suspend_module
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
from sandbox_science.worker.build import build_backend as cli_backend
from sandbox_science.worker.build import build_lifecycle, build_worker
from sandbox_science.worker.cli import main
from sandbox_science.worker.connections import Activity
from sandbox_science.worker.environments import (
    MAX_RECORDED,
    Admission,
    EnvironmentRecords,
    Environments,
    SandboxMissingError,
    new_sandbox_name,
)
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.kubernetes import KubernetesProfile, KubernetesResources
from sandbox_science.worker.lifecycle import EnvironmentLifecycle
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.provision_types import Operation as _Op
from sandbox_science.worker.provisioner import Provisioner
from sandbox_science.worker.settings import (
    ClientSettings,
    EnvironmentSettings,
    LocalBackendSettings,
    OpenShellBackendSettings,
    Settings,
    SettingsError,
    load_settings,
    token_digest,
)
from sandbox_science.worker.store import Store
from sandbox_science.worker.suspend import Suspender

from .harness import view

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "policy.yaml"
TARGETS = ROOT / "examples" / "openshell" / "targets.yaml"
IMAGE = "docker.io/library/buildpack-deps:noble-scm"
TOKEN = "t" * 40
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]

FAKE_OPENSHELL = r"""
import json, os, sys, time, uuid
import yaml

BASELINE = {"read_only": ["/usr", "/lib", "/etc", "/var/log", "/proc", "/dev/urandom"],
            "read_write": ["/tmp", "/dev/null"]}
path = os.environ["FAKE_OPENSHELL_STATE"]
with open(path) as handle:
    state = json.load(handle)
argv = sys.argv[1:]
state["calls"].append(argv)
sandboxes = state["sandboxes"]


def save():
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    with open(temporary, "x") as handle:
        json.dump(state, handle)
    os.replace(temporary, path)


def fail(message):
    save()
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
    if state.get("create_gate"):
        # Hold the command until the test opens the gate, as a slow gateway does.
        open(state["create_gate"] + ".started", "w").close()
        while not os.path.exists(state["create_gate"]):
            time.sleep(0.01)
    if state.get("fail_create"):
        fail("the VM driver could not start the sandbox")
    name = option("--name")
    providers = [argv[i + 1] for i, a in enumerate(argv) if a == "--provider"]
    labels = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--label")
    for provider in providers:
        if provider not in state["providers"]:
            fail(f"provider '{provider}' not found")
    if name in sandboxes:
        fail(f"sandbox '{name}' already exists")
    with open(option("--policy")) as handle:
        policy = yaml.safe_load(handle)
    policy["filesystem_policy"] = enrich(policy.get("filesystem_policy"))
    sandboxes[name] = {"id": str(uuid.uuid4()), "name": name, "image": option("--from"),
                       "providers": providers, "policy": policy, "version": 1,
                       "phase": state.get("phase_on_create", "Ready"),
                       **({"labels": labels} if labels else {})}
    if state.get("fail_create_after"):
        fail("synthetic creation response lost")
    out = "created"
elif command == ["sandbox", "get"]:
    if state.get("fail_get"):
        fail(state.get("get_error", "synthetic gateway unavailable"))
    name = argv[2]
    if name not in sandboxes:
        fail("code: 'Some requested entity was not found', message: \"sandbox not found\"")
    box = sandboxes[name]
    out = json.dumps({"id": box["id"], "name": name, "phase": box.get("phase", "Ready"),
                      "configuration_admission": {"state": "accepted", "error": ""}})
elif command == ["sandbox", "list"]:
    if state.get("fail_list"):
        fail("synthetic gateway unavailable")
    listed = [] if state.get("list_empty") else [
        {"id": box["id"], "name": name, "phase": box.get("phase", "Ready"),
         **({"labels": box["labels"]} if "labels" in box else {})}
        for name, box in sorted(sandboxes.items())]
    listed += state.get("extra_listed", [])
    size = int(option("--page-size")) if "--page-size" in argv else 100
    start = int(option("--page-token")) if "--page-token" in argv else 0
    end = start + size
    token = str(end) if end < len(listed) or state.get("endless_pages") else ""
    out = json.dumps({"sandboxes": listed[start:end], "next_page_token": token})
elif command == ["sandbox", "stop"]:
    if state.get("fail_stop"):
        fail("synthetic stop failure")
    sandboxes[argv[2]]["phase"] = state.get("phase_on_stop", "Stopped")
    if state.get("new_id_on_stop"):
        sandboxes[argv[2]]["id"] = str(uuid.uuid4())
    out = "stopped"
elif command == ["sandbox", "start"]:
    box = sandboxes[argv[2]]
    box["phase"] = state.get("phase_on_start", "Ready")
    if state.get("new_id_on_start"):
        box["id"] = str(uuid.uuid4())
    out = "started"
elif command == ["sandbox", "delete"]:
    if state.get("fail_delete"):
        fail("synthetic deletion failed")
    if state.get("delete_pending"):
        sandboxes[argv[2]]["phase"] = "Deleting"
    else:
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
    if box["policy"].get("landlock") != policy.get("landlock"):
        fail("landlock policy cannot be changed on a live sandbox (applied at startup)")
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
save()
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
        # A lifecycle timeout may kill the fake CLI during its write. Readers must
        # see the previous or next complete state, never a truncated JSON file.
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(self.path)

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
    def __init__(
        self,
        tmp_path: Path,
        sandboxes: dict[str, str] | None = None,
        clock: FakeClock | None = None,
        **timeouts: float,
    ) -> None:
        self.tmp = tmp_path
        self.clock = clock
        self.fake = Fake(tmp_path)
        ceiling = tmp_path / "pool-ceiling.yaml"
        ceiling.write_text("apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n")
        self.settings = OpenShellBackendSettings(
            cli=str(self.fake.cli),
            isolation_tier=3,
            sandboxes=sandboxes or {},
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
            self.settings,
            self.settings.environments,
            self.state_dir,
            env,
            clock=clock or SYSTEM_CLOCK,
            **timeouts,
        )
        self.released: list[str] = []

        async def release(environment_id: str) -> None:
            self.released.append(environment_id)

        self.environments.release = release
        self.policy = self.add(EXAMPLE)

    def fake_clock(self) -> FakeClock:
        assert self.clock is not None, "these environments run on the system clock"
        return self.clock

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


@pytest_asyncio.fixture
async def paused(tmp_path: Path) -> Setup:
    """Environments whose time moves only when the test advances it."""
    return Setup(tmp_path, clock=FakeClock(datetime.now(UTC)))


async def test_create_compiles_attaches_exactly_the_listed_providers_and_binds(
    setup: Setup,
) -> None:
    info, created = await setup.environments.create(setup.request())
    assert created
    assert info.state is EnvironmentState.READY
    assert re.fullmatch(r"ss-[0-9a-f]{16}", info.sandbox)
    assert info.providers == ("github-readonly",)
    assert info.sandbox_id is not None
    box = setup.fake.read()["sandboxes"][info.sandbox]
    assert box["image"] == IMAGE
    assert box["providers"] == ["github-readonly"]
    assert "/dev/shm" in box["policy"]["filesystem_policy"]["read_write"]
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


async def test_kubernetes_creation_pins_trusted_settings_and_resume_keeps_allocation(
    setup: Setup,
) -> None:
    requests = KubernetesResources(cpu_millicores=1000, memory_mib=512, ephemeral_storage_mib=256)
    limits = KubernetesResources(cpu_millicores=4000, memory_mib=4096, ephemeral_storage_mib=1024)
    placement = KubernetesProfile(
        node_selector={"kubernetes.io/arch": "amd64", "example.org/capacity": "large"},
        requests=requests,
        limits=limits,
    )
    original_settings = setup.environments.settings
    setup.environments.settings = original_settings.model_copy(
        update={"kubernetes": {"minimal": placement}}
    )
    info, _ = await setup.environments.create(setup.request())
    [call] = setup.fake.calls("sandbox", "create")
    pinned = call[call.index("--driver-config-json") + 1]
    assert json.loads(pinned)["kubernetes"]["pod"]["node_selector"] == {
        "kubernetes.io/arch": "amd64",
        "example.org/capacity": "large",
    }
    assert setup.environments.records.driver_config(info.sandbox) == pinned
    assert setup.environments.requires_workspace_prefix(info.environment_id)
    setup.environments.close()
    restarted = Environments(
        setup.settings,
        original_settings,
        setup.state_dir,
        {"FAKE_OPENSHELL_STATE": str(setup.fake.path), "PATH": os.environ["PATH"]},
    )
    try:
        same, created = await restarted.create(setup.request())
        assert (same.sandbox, created) == (info.sandbox, False)
        assert restarted.records.driver_config(info.sandbox) == pinned
        assert restarted.requires_workspace_prefix(info.environment_id)
        assert len(setup.fake.calls("sandbox", "create")) == 1
        with pytest.raises(WorkerError, match="launch settings changed"):
            restarted.records.save(same, driver_config="{}")
        assert restarted.records.driver_config(info.sandbox) == pinned
    finally:
        restarted.close()


@pytest.mark.parametrize("change", ["revision", "ceiling"])
async def test_kubernetes_existing_allocation_cannot_claim_tighter_resource_caps(
    setup: Setup,
    change: str,
) -> None:
    requests = KubernetesResources(cpu_millicores=1000, memory_mib=512, ephemeral_storage_mib=256)
    limits = KubernetesResources(cpu_millicores=4000, memory_mib=4096, ephemeral_storage_mib=1024)
    configured = setup.environments.settings.model_copy(
        update={
            "kubernetes": {
                "minimal": KubernetesProfile(
                    node_selector={"kubernetes.io/arch": "amd64"},
                    requests=requests,
                    limits=limits,
                )
            }
        }
    )
    setup.environments.settings = configured
    info, _ = await setup.environments.create(setup.request())
    if change == "revision":
        policy = setup.tmp / "smaller.yaml"
        policy.write_text(
            EXAMPLE.read_text()
            .replace("cpu: 16", "cpu: 0.5")
            .replace("memory: 64Gi", "memory: 256Mi")
        )
        revision = setup.add(policy)
        with pytest.raises(WorkerError, match="saved Kubernetes allocation exceeds"):
            await setup.environments.revise(info.environment_id, revision)
        assert setup.environments.get(info.environment_id).policy == info.policy
        assert not setup.fake.calls("policy", "set")
    else:
        setup.environments.close()
        configured.ceiling.write_text(
            "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
            "resources: {cpu: 0.5, memory: 256Mi}\n"
        )
        restarted = Environments(
            setup.settings,
            configured,
            setup.state_dir,
            {"FAKE_OPENSHELL_STATE": str(setup.fake.path), "PATH": os.environ["PATH"]},
        )
        try:
            with pytest.raises(WorkerError, match="saved Kubernetes allocation exceeds"):
                await restarted.create(setup.request())
            assert restarted.get(info.environment_id).sandbox == info.sandbox
        finally:
            restarted.close()
    assert len(setup.fake.calls("sandbox", "create")) == 1


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


async def test_a_revision_keeps_the_live_landlock_section(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    state = setup.fake.read()
    box = state["sandboxes"][info.sandbox]
    assert box["policy"]["landlock"] == {"compatibility": "hard_requirement"}
    # A sandbox created before compiled policies carried a Landlock section has none, and
    # OpenShell refuses a revision that adds one.
    del box["policy"]["landlock"]
    setup.fake.write(state)
    revision = setup.add(variant(setup.tmp, "v2.yaml", extra_rule=True))
    await setup.environments.revise("env-1", revision)
    revised = setup.fake.read()["sandboxes"][info.sandbox]["policy"]
    assert "landlock" not in revised
    assert (
        revised["network_middlewares"]["sandbox-science-policy"]["config"]["policy"]["digest"]
        == revision
    )


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


async def test_an_unobserved_creation_keeps_its_cleanup_obligation(setup: Setup) -> None:
    setup.fake.update(fail_create=True)
    with pytest.raises(WorkerError, match="could not start") as refused:
        await setup.environments.create(setup.request())
    assert refused.value.kind is ErrorKind.UNAVAILABLE
    original = setup.environments.get("env-1")
    assert original.state is EnvironmentState.CREATING
    assert original.sandbox_id is None
    assert setup.environments.sandbox("env-1") is None
    assert setup.fake.read()["sandboxes"] == {}
    setup.fake.update(fail_create=False)
    for action in (setup.environments.create(setup.request()), setup.environments.delete("env-1")):
        with pytest.raises(WorkerError, match="creation outcome is unknown"):
            await action
        assert setup.environments.get("env-1") == original
    assert len(setup.fake.calls("sandbox", "create")) == 1
    assert setup.fake.calls("sandbox", "delete") == []


@pytest.mark.parametrize("problem", ["fail_delete", "delete_pending", "fail_get"])
async def test_failed_creation_cleanup_retains_identity_across_restart(
    setup: Setup, problem: str
) -> None:
    other, _ = await setup.environments.create(setup.request("env-2"))
    setup.fake.update(fail_create_after=True, **{problem: True})
    with pytest.raises(WorkerError, match="creation response lost"):
        await setup.environments.create(setup.request())
    original = setup.environments.get("env-1")
    assert original.state is EnvironmentState.CREATING
    assert original.sandbox in setup.fake.read()["sandboxes"]
    if problem != "fail_get":
        assert original.sandbox_id == setup.fake.read()["sandboxes"][original.sandbox]["id"]
    assert original.environment_id in setup.environments.records.live(["p1"])

    setup.environments.close()
    assert setup.settings.environments is not None
    restarted = Environments(
        setup.settings,
        setup.settings.environments,
        setup.state_dir,
        {"FAKE_OPENSHELL_STATE": str(setup.fake.path), "PATH": os.environ["PATH"]},
    )
    try:
        with pytest.raises(WorkerError):
            await restarted.create(setup.request())
        assert restarted.get("env-1") == original
        assert len(setup.fake.calls("sandbox", "create")) == 2
        setup.fake.update(fail_create_after=False, **{problem: False})
        replacement, created = await restarted.create(setup.request())
        assert created
        assert replacement.sandbox != original.sandbox
        assert original.sandbox not in setup.fake.read()["sandboxes"]
        assert other.sandbox in setup.fake.read()["sandboxes"]
        assert all(call[2] == original.sandbox for call in setup.fake.calls("sandbox", "delete"))
    finally:
        restarted.close()


async def test_an_unknown_creation_that_appears_later_is_reconciled_first(setup: Setup) -> None:
    setup.fake.update(fail_create=True)
    with pytest.raises(WorkerError):
        await setup.environments.create(setup.request())
    original = setup.environments.get("env-1")
    # The disconnected upstream creation completes after the worker saw absence.
    setup.fake.update(
        fail_create=False,
        sandboxes={original.sandbox: {"id": "original-late-id", "phase": "Ready"}},
    )
    replacement, created = await setup.environments.create(setup.request())
    assert created
    assert replacement.sandbox != original.sandbox
    assert original.sandbox not in setup.fake.read()["sandboxes"]
    calls = setup.fake.read()["calls"]
    deletion = next(i for i, call in enumerate(calls) if call[:2] == ["sandbox", "delete"])
    creation = max(i for i, call in enumerate(calls) if call[:2] == ["sandbox", "create"])
    assert deletion < creation


async def test_readiness_retains_identity_before_phase_failure_and_refuses_replacement(
    setup: Setup,
) -> None:
    setup.fake.update(phase_on_create="Error", fail_delete=True)
    with pytest.raises(WorkerError, match="is Error"):
        await setup.environments.create(setup.request())
    original = setup.environments.get("env-1")
    assert original.sandbox_id is not None
    state = setup.fake.read()
    state["sandboxes"][original.sandbox]["id"] = "another-sandbox"
    state["fail_delete"] = False
    setup.fake.write(state)
    before = setup.fake.calls("sandbox", "delete")
    with pytest.raises(WorkerError, match="identity changed"):
        await setup.environments.create(setup.request())
    with pytest.raises(WorkerError, match="identity changed"):
        await setup.environments.delete("env-1")
    assert setup.environments.get("env-1") == original
    assert setup.fake.calls("sandbox", "delete") == before
    assert len(setup.fake.calls("sandbox", "create")) == 1


async def test_invalid_replacement_request_does_not_mutate_pending_creation(setup: Setup) -> None:
    setup.fake.update(fail_create_after=True, fail_delete=True)
    with pytest.raises(WorkerError):
        await setup.environments.create(setup.request())
    original = setup.environments.get("env-1")
    before = setup.fake.calls("sandbox", "delete")
    setup.fake.update(fail_create_after=False, fail_delete=False)
    with pytest.raises(WorkerError, match="no profile"):
        await setup.environments.create(setup.request(profile="unknown"))
    assert setup.environments.get("env-1") == original
    assert setup.fake.calls("sandbox", "delete") == before


async def test_creation_cleanup_stays_owned_through_repeat_cancellation(setup: Setup) -> None:
    entered, free = asyncio.Event(), asyncio.Event()

    async def release(environment: str) -> None:
        entered.set()
        await free.wait()

    setup.environments.release = release
    setup.fake.update(fail_create_after=True)
    creation = asyncio.create_task(setup.environments.create(setup.request()))
    await entered.wait()
    original = setup.environments.get("env-1")
    assert original.sandbox_id is not None
    try:
        for _ in range(2):
            creation.cancel()
            await asyncio.sleep(0)  # deliver each cancellation to the owned cleanup
            assert not creation.done()
        setup.fake.update(fail_create_after=False)
        retry = asyncio.create_task(setup.environments.create(setup.request()))
        await asyncio.sleep(0)
        assert not retry.done()
        assert len(setup.fake.calls("sandbox", "create")) == 1
        assert setup.environments.get("env-1") == original
    finally:
        free.set()
    with pytest.raises(asyncio.CancelledError):
        await creation
    replacement, created = await retry
    assert created
    assert replacement.sandbox != original.sandbox
    assert original.sandbox not in setup.fake.read()["sandboxes"]


async def test_the_backend_finds_sandboxes_the_worker_created(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    static = setup.settings.model_copy(update={"sandboxes": {"env-2": "made-by-hand"}})
    backend = OpenShellBackend(
        static,
        setup.state_dir,
        setup.environments.sandbox,
        manages=setup.environments.manages,
    )
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


@pytest.mark.parametrize("binding", ["held", "active", "unknown", "replacement"])
async def test_capture_cleanup_uses_only_confirmed_original_vm_deletion(
    setup: Setup, aiohttp_client: ClientFactory, tmp_path: Path, binding: str
) -> None:
    manager = idle_manager(tmp_path)
    client = await aiohttp_client(create_app(manager, TOKEN, setup.environments))
    info, _ = await setup.environments.create(setup.request())
    assert info.sandbox_id is not None
    lease = manager.store.reserve_capture(new_id("pub"), info.environment_id)
    original = (
        None
        if binding == "unknown"
        else (info.sandbox, "another-immutable-id" if binding == "replacement" else info.sandbox_id)
    )
    lease = manager.store.dispatch_capture(lease, new_id("inc"), new_id("cap"), original)
    if binding != "active":
        manager.store.finish_capture(lease, certain=False)
    before = manager.store.capture_leases()
    try:
        # Neither advisory forgetting nor a forged logical DELETED state is proof.
        await setup.environments._forget(info.environment_id)
        assert manager.store.capture_leases() == before
        response = await client.delete(
            f"/v1/environments/{info.environment_id}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status == 200, await response.text()
        if binding == "held":
            assert not manager.store.capture_leases()
        elif binding == "active":
            assert manager.store.capture_leases()[0].guest_clean == 1
            assert manager.store.finish_capture(lease, certain=False)
        else:
            assert manager.store.capture_leases() == before
    finally:
        await manager.close()
        manager.store.close()


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


MISSING = "status: 'Some requested entity was not found', self: \"sandbox not found\""
"""How OpenShell 0.1.5's CLI (tonic 0.14) shows the gateway's NotFound for a name."""


@pytest.mark.parametrize(
    "said",
    [
        MISSING,
        MISSING + ", metadata: " + "x" * 600,
        "status: 'Some requested entity was not found', self: \"sandbox not\n  │ found\"",
    ],
)
async def test_only_the_gateways_not_found_for_the_name_means_absent(
    setup: Setup, said: str
) -> None:
    setup.fake.update(fail_get=True, get_error=said)
    assert await setup.environments.cli.sandbox("missing") is None


@pytest.mark.parametrize(
    "said",
    [
        "status: 'Some requested entity was not found', self: \"service endpoint not found\"",
        "gateway 'babbage' not found",
        "error: /home/user/.config/openshell/client.pem: file not found",
        "sandbox research-1 not found",
        "synthetic gateway unavailable",
    ],
)
async def test_other_failures_never_count_as_absence(setup: Setup, said: str) -> None:
    setup.fake.update(fail_get=True, get_error=said)
    with pytest.raises(WorkerError, match="openshell sandbox get failed") as raised:
        await setup.environments.cli.sandbox("missing")
    assert not isinstance(raised.value, SandboxMissingError)


async def test_sandbox_names_fit_openshells_limit_and_are_random() -> None:
    # OpenShell refuses names over 19 characters (v0.1.3 in lab 12, unchanged in v0.1.5),
    # and uses "--" itself.
    # A name derived from the ID had room for 32 bits of a hash, so another ID could be
    # found to share it (review of D78); 64 random bits cannot be matched.
    names = {new_sandbox_name() for _ in range(1000)}
    assert len(names) == 1000
    for name in names:
        assert re.fullmatch(r"ss-[0-9a-f]{16}", name)
        assert len(name) == 19


async def test_no_two_environments_share_a_sandbox(setup: Setup) -> None:
    first, _ = await setup.environments.create(setup.request("env-1"))
    second, _ = await setup.environments.create(setup.request("env-2"))
    assert first.sandbox != second.sandbox
    clash = second.model_copy(update={"environment_id": "env-3", "sandbox": first.sandbox})
    with pytest.raises(sqlite3.IntegrityError):
        setup.environments.records.save(clash)


async def test_creating_again_deletes_only_the_records_own_leftover(setup: Setup) -> None:
    # A crash left env-1 creating, with a sandbox of its own; env-2's sandbox is untouched.
    other, _ = await setup.environments.create(setup.request("env-2"))
    first, _ = await setup.environments.create(setup.request("env-1"))
    setup.environments.records.save(first.model_copy(update={"state": EnvironmentState.CREATING}))
    again, created = await setup.environments.create(setup.request("env-1"))
    assert created
    assert again.sandbox != first.sandbox
    assert [call[2] for call in setup.fake.calls("sandbox", "delete")] == [first.sandbox]
    assert other.sandbox in setup.fake.read()["sandboxes"]


async def test_a_ready_environment_whose_sandbox_changed_is_given_up(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    state = setup.fake.read()
    state["sandboxes"][info.sandbox]["id"] = "another-sandbox"
    setup.fake.write(state)
    with pytest.raises(WorkerError, match="not " + re.escape(str(info.sandbox_id))):
        await setup.environments.resume("env-1")
    assert setup.environments.get("env-1").state is EnvironmentState.DELETED


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
    eprint = setup.fake.draft(info.sandbox, "eprint.example.net:443")
    registry = setup.fake.draft(info.sandbox, "pypi.org:443", "/usr/bin/python3.13")
    lan = setup.fake.draft(info.sandbox, "printer.local:443")
    decisions = await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert {(d.host, d.admitted) for d in decisions} == {
        ("eprint.example.net", True),
        ("pypi.org", False),
        ("printer.local", False),
    }
    # One revision of the same policy names the admitted host, with the address ranges.
    assert len(setup.fake.calls("policy", "set")) == 1
    assert admitted_endpoints(setup, info.sandbox) == ["eprint.example.net"]
    box = setup.fake.read()["sandboxes"][info.sandbox]["policy"]
    [endpoint] = box["network_policies"]["sandbox-science-admitted"]["endpoints"]
    assert (
        endpoint["allowed_ips"]
        == box["network_policies"]["sandbox-science-egress"]["endpoints"][0]["allowed_ips"]
    )
    revised = setup.environments.get("env-1")
    assert revised.admitted_hosts == ("eprint.example.net",)
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
        ("eprint.example.net", 443, True),
        ("pypi.org", 443, False),
        ("printer.local", 443, False),
    }


async def test_drafts_for_admitted_hosts_need_no_revision(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    watcher = DraftWatcher(setup.environments, 1.0)
    setup.fake.draft(info.sandbox, "eprint.example.net:443")
    await watcher.check("env-1", info.sandbox)
    # Another program, raced with the revision; a name the policy names, refused by
    # OpenShell's address check; and nothing pending at all.
    again = setup.fake.draft(info.sandbox, "eprint.example.net:443", "/usr/bin/wget")
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
    assert setup.environments.get("env-1").admitted_hosts == ("eprint.example.net",)


async def test_other_ports_are_admitted_on_demand(setup: Setup) -> None:
    # D95: the example's reads are for any port, and OpenShell endpoints list their
    # ports, so a refused connection to another port admits that port for that host.
    info, _ = await setup.environments.create(setup.request())
    watcher = DraftWatcher(setup.environments, 1.0)
    setup.fake.draft(info.sandbox, "papers.example.net:8080")
    setup.fake.draft(info.sandbox, "github.com:8443")  # pre-admitted, on another port
    control = setup.fake.draft(info.sandbox, "papers.example.net:6443")
    decisions = await watcher.check("env-1", info.sandbox)
    assert {(d.host, d.port, d.admitted) for d in decisions} == {
        ("papers.example.net", 8080, True),
        ("github.com", 8443, True),
        ("papers.example.net", 6443, False),
    }
    assert "port 6443 is not admitted" in setup.fake.drafts(info.sandbox)[control]["reason"]
    revised = setup.environments.get("env-1")
    assert revised.admitted_hosts == ("github.com:8443", "papers.example.net:8080")
    policies = setup.fake.read()["sandboxes"][info.sandbox]["policy"]["network_policies"]
    ports = {
        endpoint["host"]: endpoint["ports"]
        for section in ("sandbox-science-egress", "sandbox-science-admitted")
        for endpoint in policies[section]["endpoints"]
    }
    assert ports["papers.example.net"] == [8080]
    assert ports["github.com"] == [80, 8443]  # its HTTPS port is its credential endpoint's
    # The same port again needs no revision; the default ports of that host do.
    setup.fake.draft(info.sandbox, "papers.example.net:8080", "/usr/bin/wget")
    await watcher.check("env-1", info.sandbox)
    assert len(setup.fake.calls("policy", "set")) == 1
    setup.fake.draft(info.sandbox, "papers.example.net:443")
    await watcher.check("env-1", info.sandbox)
    assert setup.environments.get("env-1").admitted_hosts == (
        "github.com:8443",
        "papers.example.net",
        "papers.example.net:8080",
    )


async def test_the_admission_cap_is_a_setting_above_what_fits() -> None:
    default = EnvironmentSettings.model_fields["max_admitted_hosts"].default
    assert default == 128
    with pytest.raises(ValidationError):
        EnvironmentSettings.model_validate(
            {
                "policy_store": "s",
                "targets": "t",
                "ceiling": "c",
                "profiles": {"minimal": "i"},
                "max_admitted_hosts": 1025,
            }
        )


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
    blocked = setup.add(deny_variant(setup.tmp, "blocked.yaml", "eprint.example.net"))
    info, _ = await setup.environments.create(setup.request(policy=blocked))
    setup.fake.draft(info.sandbox, "eprint.example.net:443")
    setup.fake.draft(info.sandbox, "papers.example.net:443")
    await DraftWatcher(setup.environments, 1.0).check("env-1", info.sandbox)
    assert setup.environments.get("env-1").admitted_hosts == ("papers.example.net",)
    # The example policy no longer denies the host: the next revision admits it too.
    revised = await setup.environments.revise("env-1", setup.policy)
    assert revised.admitted_hosts == ("eprint.example.net", "papers.example.net")
    assert admitted_endpoints(setup, info.sandbox) == ["eprint.example.net", "papers.example.net"]
    # And back: a revision that denies it drops it again.
    again = await setup.environments.revise("env-1", blocked)
    assert again.admitted_hosts == ("papers.example.net",)
    assert admitted_endpoints(setup, info.sandbox) == ["papers.example.net"]
    decisions = {a.host: a for a in setup.environments.records.admissions("env-1")}
    assert not decisions["eprint.example.net"].admitted


async def test_a_gateway_failure_pauses_admission_for_the_environment(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.fake.draft(info.sandbox, "eprint.example.net:443")
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
    setup.fake.draft(info.sandbox, "eprint.example.net:443")
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

    async def broken(
        environment_id: str, requests: Sequence[tuple[str, int]], *, wait: bool = True
    ) -> list[Admission] | None:
        if environment_id == "env-a":
            raise RuntimeError("database is locked")
        return await real(environment_id, requests, wait=wait)

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


async def test_lost_executor_recycle_keeps_disk_identity_binding_and_lifecycle_order(
    setup: Setup,
) -> None:
    info, _ = await setup.environments.create(setup.request())
    await setup.environments.recycle_executor("env-1", info.sandbox, info.sandbox_id or "")
    ready = setup.environments.get("env-1")
    assert ready.state is EnvironmentState.READY
    assert ready.sandbox_id == info.sandbox_id
    assert setup.environments.store.binding(info.sandbox_id or "") == setup.policy
    commands = [call[:2] for call in setup.fake.read()["calls"]]
    assert commands.index(["sandbox", "stop"]) < commands.index(["sandbox", "start"])
    assert setup.released == []  # no manager callback under the lifecycle lock


async def test_recycle_stop_failure_is_not_recorded_as_confirmed_stopped(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.fake.update(fail_stop=True)
    with pytest.raises(WorkerError, match="synthetic stop failure"):
        await setup.environments.recycle_executor("env-1", info.sandbox, info.sandbox_id or "")
    assert setup.environments.get("env-1").state is EnvironmentState.READY
    assert setup.fake.calls("sandbox", "start") == []


async def test_recycle_waits_for_actual_stop_and_refuses_changed_identity(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path, resume_timeout=0.1, poll_seconds=0.01)
    info, _ = await setup.environments.create(setup.request())
    setup.fake.update(phase_on_stop="Ready")
    with pytest.raises(WorkerError, match="did not confirm stopping"):
        await setup.environments.recycle_executor("env-1", info.sandbox, info.sandbox_id or "")
    assert setup.environments.get("env-1").state is EnvironmentState.READY
    assert setup.fake.calls("sandbox", "start") == []
    # The first look after the stop finds the new identity: no deadline is involved,
    # so a slow fake CLI under load must not reach the 0.1 s one the case above needs.
    setup.environments.resume_timeout = 60
    setup.fake.update(phase_on_stop="Stopped", new_id_on_stop=True)
    with pytest.raises(WorkerError, match="changed while stopping"):
        await setup.environments.recycle_executor("env-1", info.sandbox, info.sandbox_id or "")
    assert setup.environments.get("env-1").state is EnvironmentState.READY
    assert setup.fake.calls("sandbox", "start") == []


async def test_resume_recovers_crash_after_actual_stop_before_record_update(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    await setup.environments.cli.run("sandbox", "stop", info.sandbox)
    assert setup.environments.get("env-1").state is EnvironmentState.READY
    await setup.environments.resume("env-1")
    assert setup.environments.get("env-1").state is EnvironmentState.READY
    assert setup.environments.get("env-1").sandbox_id == info.sandbox_id
    assert len(setup.fake.calls("sandbox", "start")) == 1


async def test_recycle_refuses_start_with_a_replaced_sandbox(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.fake.update(new_id_on_start=True)
    with pytest.raises(WorkerError, match="came back as"):
        await setup.environments.recycle_executor("env-1", info.sandbox, info.sandbox_id or "")
    assert setup.environments.get("env-1").state is EnvironmentState.STOPPED


async def test_waiting_recycle_does_not_touch_a_rebound_healthy_environment(setup: Setup) -> None:
    old, _ = await setup.environments.create(setup.request())
    async with setup.environments._lock("env-1"):
        waiting = asyncio.create_task(
            setup.environments.recycle_executor("env-1", old.sandbox, old.sandbox_id or "")
        )
        await asyncio.sleep(0)
        # A lifecycle owner finished replacing the binding before this fence got its lock.
        current = old.model_copy(update={"sandbox": "new-healthy", "sandbox_id": "new-id"})
        setup.environments.records.save(current)
    with pytest.raises(WorkerError, match="environment binding changed"):
        await waiting
    assert setup.fake.calls("sandbox", "stop") == []
    assert setup.fake.calls("sandbox", "start") == []
    assert setup.environments.get("env-1") == current


@pytest.mark.parametrize("phase", ["Ready", "Deleting", "Error", "Completed", "Stopped"])
async def test_recycle_intent_requires_actual_termination_and_clears_quarantine(
    setup: Setup, phase: str
) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.environments.records.save(info.model_copy(update={"state": EnvironmentState.STOPPED}))
    state = setup.fake.read()
    state["sandboxes"][info.sandbox]["phase"] = phase
    setup.fake.write(state)
    settings = Settings(
        token_file=setup.tmp / "synthetic-token",
        state_dir=setup.state_dir,
        openshell=setup.settings.model_copy(
            update={"env": {"FAKE_OPENSHELL_STATE": str(setup.fake.path)}}
        ),
    )
    store = Store(setup.tmp / "recycle-worker.sqlite")
    assert settings.openshell is not None
    backend = OpenShellBackend(settings.openshell, setup.state_dir)
    # The loss check as the worker builds it, from the settings, backend and environments.
    lifecycle = build_lifecycle(settings, backend, setup.environments, store)
    confirmed = lifecycle.confirmed_daemon_loss
    assert confirmed is not None
    manager = ExecutionManager(
        store,
        backend,
        Blobs(setup.tmp / "recycle-blobs"),
        pool_id="test",
        max_artifact_size=1 << 20,
        state_dir=setup.state_dir,
        lifecycle=EnvironmentLifecycle(confirmed_daemon_loss=confirmed),
    )
    try:
        store.remember_endpoint("env-1", "inc-" + "1" * 24, (info.sandbox, info.sandbox_id or ""))
        store.require_recycle("env-1", "inc-" + "1" * 24)
        view(manager).quarantine("env-1")

        assert await confirmed("env-1") is (phase == "Stopped")
        if phase == "Stopped":
            await view(manager).recover("env-1", startup=True)
            assert not store.recycle_needed("env-1")
            assert not view(manager).quarantined("env-1")
    finally:
        await manager.close()
        store.close()


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
    assert "create the environment again" in refused.value.message
    assert setup.environments.get("env-1").state is EnvironmentState.DELETED
    assert len(setup.fake.calls("sandbox", "stop")) == 1
    # The replacement remains healthy under its name; recreating uses a fresh name.
    again, created = await setup.environments.create(setup.request())
    assert created
    assert again.state is EnvironmentState.READY


def age(setup: Setup, environment_id: str, seconds: float) -> None:
    """Pretend the environment last changed ``seconds`` ago."""
    info = setup.environments.get(environment_id)
    setup.environments.records.save(
        info.model_copy(
            update={"updated_at": setup.environments.clock.now() - timedelta(seconds=seconds)}
        )
    )


class StubManager:
    """What the suspender asks the execution manager, set by each test."""

    def __init__(self) -> None:
        self.activities: dict[str, Activity | None] = {}

    async def activity(self, environment_id: str) -> Activity | None:
        return self.activities.get(environment_id)


async def test_the_suspender_stops_only_idle_environments(paused: Setup) -> None:
    clock = paused.fake_clock()
    for name in ("idle", "running", "computing", "unknown", "quiet", "recent"):
        await paused.environments.create(paused.request(f"env-{name}"))
        age(paused, f"env-{name}", 100)
    paused.environments.release_idle = always
    stub = StubManager()
    long_ago = clock.monotonic() - 100
    stub.activities = {
        "env-idle": Activity(long_ago, 0, None, 0),
        "env-running": Activity(long_ago, 1, 2.0, 1),
        "env-computing": Activity(long_ago, 0, 1.0, 1),
        "env-unknown": None,
        "env-quiet": Activity(long_ago, 0, 3.0, 1),
        "env-recent": Activity(clock.monotonic(), 0, None, 0),
    }
    suspender = Suspender(
        paused.environments, cast(ExecutionManager, stub), 10.0, interval=1, clock=clock
    )
    suspender.started = long_ago
    # An executor's CPU needs two samples to tell.
    assert await suspender.check() == ["env-idle"]
    stub.activities["env-computing"] = Activity(long_ago, 0, 30.0, 1)
    assert await suspender.check() == ["env-quiet"]
    states = {
        info.environment_id: info.state
        for name in ("idle", "running", "computing", "unknown", "quiet", "recent")
        if (info := paused.environments.get(f"env-{name}"))
    }
    assert {name for name, state in states.items() if state is EnvironmentState.STOPPED} == {
        "env-idle",
        "env-quiet",
    }


async def test_a_failed_suspension_leaves_the_others_to_go_on(paused: Setup) -> None:
    clock = paused.fake_clock()
    for name in ("a", "b"):
        await paused.environments.create(paused.request(f"env-{name}"))
        age(paused, f"env-{name}", 100)
    paused.environments.release_idle = always
    stub = StubManager()
    long_ago = clock.monotonic() - 100
    stub.activities = {name: Activity(long_ago, 0, None, 0) for name in ("env-a", "env-b")}
    suspender = Suspender(
        paused.environments, cast(ExecutionManager, stub), 10.0, interval=1, clock=clock
    )
    suspender.started = long_ago
    real = paused.environments.cli.run

    async def flaky(*args: str, timeout: float = 120.0) -> str:
        if args[:2] == ("sandbox", "stop") and args[2] == paused.environments.get("env-a").sandbox:
            raise WorkerError(ErrorKind.UNAVAILABLE, "openshell sandbox stop failed")
        return await real(*args, timeout=timeout)

    paused.environments.cli.run = flaky
    assert await suspender.check() == ["env-b"]
    # Recorded stopped before the stop failed: its next use finds the sandbox still
    # ready, and carries on without starting it (review finding 2).
    assert paused.environments.get("env-a").state is EnvironmentState.STOPPED
    paused.environments.cli.run = real
    await paused.environments.resume("env-a")
    assert paused.environments.get("env-a").state is EnvironmentState.READY
    assert paused.fake.calls("sandbox", "start") == []


async def test_admission_notices_say_what_was_admitted_and_refused(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    before = datetime.now(UTC)
    setup.fake.draft(info.sandbox, "eprint.example.net:443")
    setup.fake.draft(info.sandbox, "pypi.org:443")
    watcher = DraftWatcher(setup.environments, 1.0)
    await watcher.check("env-1", info.sandbox)
    notices = [text for _, text in setup.environments.notices("env-1", before)]
    assert notices[0].startswith("eprint.example.net was admitted on demand")
    assert notices[1].startswith("pypi.org:443 was not admitted: ")
    seen = max(at for at, _ in setup.environments.notices("env-1", before))
    # Another program for an admitted host is no news.
    setup.fake.draft(info.sandbox, "eprint.example.net:443", "/usr/bin/wget")
    await watcher.check("env-1", info.sandbox)
    assert setup.environments.notices("env-1", seen) == []


async def test_operators_can_list_an_environments_admissions(
    setup: Setup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.fake.draft(info.sandbox, "eprint.example.net:443")
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
    assert "admitted eprint.example.net" in out
    assert "pypi.org:443 refused: " in out
    assert main(["admissions", "--settings", str(settings), "--environment", "nope"]) == 1


async def test_a_new_environment_gets_its_full_idle_time(paused: Setup) -> None:
    clock = paused.fake_clock()
    # The worker has run for long, but the environment was created just now (review 3).
    await paused.environments.create(paused.request())
    paused.environments.release_idle = always
    stub = StubManager()
    long_ago = clock.monotonic() - 1000
    stub.activities = {"env-1": Activity(None, 0, None, 0)}
    suspender = Suspender(
        paused.environments, cast(ExecutionManager, stub), 60.0, interval=1, clock=clock
    )
    suspender.started = long_ago
    assert await suspender.check() == []
    clock.advance(120)
    assert await suspender.check() == ["env-1"]


async def test_a_cpu_counter_that_drops_or_restarts_is_no_sign_of_idleness(paused: Setup) -> None:
    clock = paused.fake_clock()
    await paused.environments.create(paused.request())
    age(paused, "env-1", 100)
    paused.environments.release_idle = always
    stub = StubManager()
    long_ago = clock.monotonic() - 100
    suspender = Suspender(
        paused.environments, cast(ExecutionManager, stub), 10.0, interval=1, clock=clock
    )
    suspender.started = long_ago
    stub.activities = {"env-1": Activity(long_ago, 0, 50.0, 1, "inc-a")}
    assert await suspender.check() == []  # a first sample
    stub.activities = {"env-1": Activity(long_ago, 0, 5.0, 1, "inc-a")}
    assert await suspender.check() == []  # the counter went down
    stub.activities = {"env-1": Activity(long_ago, 0, 5.0, 1, "inc-b")}
    assert await suspender.check() == []  # another executor
    assert await suspender.check() == ["env-1"]  # quiet, and comparable


async def test_an_executor_that_does_not_answer_holds_up_no_other(
    paused: Setup,
) -> None:
    clock = paused.fake_clock()
    for name in ("frozen", "idle"):
        await paused.environments.create(paused.request(f"env-{name}"))
        age(paused, f"env-{name}", 100)
    paused.environments.release_idle = always
    long_ago = clock.monotonic() - 100

    class Frozen(StubManager):
        async def activity(self, environment_id: str) -> Activity | None:
            if environment_id == "env-frozen":
                await asyncio.sleep(3600)
            return Activity(long_ago, 0, None, 0)

    suspender = Suspender(
        paused.environments,
        cast(ExecutionManager, Frozen()),
        10.0,
        interval=1,
        sample_timeout=0.2,
        clock=clock,
    )
    suspender.started = long_ago
    assert await asyncio.wait_for(suspender.check(), 10) == ["env-idle"]


async def test_a_sandbox_in_a_dead_phase_fails_fast_and_backs_off(setup: Setup) -> None:
    await setup.environments.create(setup.request())
    setup.environments.release_idle = always
    await setup.environments.suspend("env-1", time.monotonic())
    setup.fake.update(phase_on_start="Error")
    started = time.monotonic()
    with pytest.raises(WorkerError, match="is Error"):
        await setup.environments.resume("env-1")
    assert time.monotonic() - started < 10
    # Queued uses fail at once instead of each waiting out a start (review finding 4).
    with pytest.raises(WorkerError, match="failed to start a moment ago"):
        await setup.environments.resume("env-1")
    assert len(setup.fake.calls("sandbox", "start")) == 1


# Per-project credentials: a client's token acts for the projects its settings list (D78).

A_TOKEN = "test-secret-client-a-" + "0" * 24
B_TOKEN = "test-secret-client-b-" + "0" * 24
C_TOKEN = "test-secret-client-c-" + "0" * 24
CLIENTS = (
    ClientSettings(name="client-a", token_sha256=token_digest(A_TOKEN), projects=("p1",)),
    ClientSettings(name="client-b", token_sha256=token_digest(B_TOKEN), projects=("p2",)),
    ClientSettings(name="client-c", token_sha256=token_digest(C_TOKEN), projects=("p1", "p3")),
)


LAUNCHER_TOKEN = "test-secret-launcher-" + "0" * 24


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Api:
    """The worker API as the tests call it, with the clients' tokens."""

    def __init__(self, client: TestClient[Any, Any], setup: Setup) -> None:
        self.client = client
        self.setup = setup

    async def call(
        self, method: str, path: str, token: str, body: object = None
    ) -> tuple[int, Any]:
        response = await self.client.request(method, path, json=body, headers=bearer(token))
        return response.status, await response.json()

    async def refused(
        self, method: str, path: str, token: str, body: object = None
    ) -> tuple[int, str]:
        status, reply = await self.call(method, path, token, body)
        return status, reply["error"]["kind"]

    async def finish(self, execution_id: str, token: str) -> dict[str, Any]:
        response = await self.client.get(
            f"/v1/executions/{execution_id}/events", headers=bearer(token)
        )
        async for line in response.content:
            if line.strip() and (event := json.loads(line))["type"] == "result":
                return event["execution"]
        raise AssertionError("the event stream ended without a result")

    def environment(self, environment_id: str, project_id: str = "p1") -> dict[str, Any]:
        return self.setup.request(environment_id, project_id=project_id).model_dump(mode="json")


async def test_the_runtime_sandbox_is_bound_to_the_projects_revision_for_an_operator(
    setup: Setup, aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    manager = idle_manager(tmp_path / "worker-api")
    await manager.start()
    launcher = ClientSettings(
        name="launcher", token_sha256=token_digest(LAUNCHER_TOKEN), projects=("p1",), operator=True
    )
    client = await aiohttp_client(
        create_app(manager, TOKEN, setup.environments, (*CLIENTS, launcher))
    )
    api = Api(client, setup)
    store = setup.environments.store
    body = {"project_id": "p1", "sandbox_id": "sbx-agent"}
    # Neither a client that is no operator, nor an operator for another project.
    assert await api.refused("POST", "/v1/runtime-sandboxes", A_TOKEN, body) == (
        403,
        "policy_denied",
    )
    other = {"project_id": "p2", "sandbox_id": "sbx-agent"}
    assert await api.refused("POST", "/v1/runtime-sandboxes", LAUNCHER_TOKEN, other) == (
        403,
        "policy_denied",
    )
    # A project with no revision has nothing to bind to.
    assert await api.refused("POST", "/v1/runtime-sandboxes", LAUNCHER_TOKEN, body) == (
        404,
        "not_found",
    )
    assert store.binding("sbx-agent") is None
    # The revision granted to it last, until the store binds the project to another.
    setup.environments.records.grant("p1", setup.policy)
    status, bound = await api.call("POST", "/v1/runtime-sandboxes", LAUNCHER_TOKEN, body)
    assert (status, bound["policy"]) == (200, setup.policy)
    assert store.binding("sbx-agent") == setup.policy
    newer = setup.add(variant(setup.tmp, "no-providers.yaml", providers=False))
    PolicyStore(setup.environments.settings.policy_store, "projects").bind("p1", newer)
    status, bound = await api.call("POST", "/v1/runtime-sandboxes", LAUNCHER_TOKEN, body)
    assert (status, bound["policy"]) == (200, newer)
    assert store.binding("sbx-agent") == newer
    # A research environment's sandbox is bound by its own steps: never by this route.
    info, _ = await setup.environments.create(setup.request())
    assert info.sandbox_id is not None
    taken = {"project_id": "p1", "sandbox_id": info.sandbox_id}
    assert await api.refused("POST", "/v1/runtime-sandboxes", LAUNCHER_TOKEN, taken) == (
        409,
        "conflict",
    )
    path = f"/v1/runtime-sandboxes/{info.sandbox_id}"
    assert (await api.refused("DELETE", path, LAUNCHER_TOKEN))[0] == 409
    assert store.binding(info.sandbox_id) == setup.policy
    # Unbinding is the operator's too, and unbinding twice is no error.
    path = "/v1/runtime-sandboxes/sbx-agent"
    assert (await api.refused("DELETE", path, A_TOKEN))[0] == 403
    assert await api.call("DELETE", path, LAUNCHER_TOKEN) == (
        200,
        {"sandbox_id": "sbx-agent", "unbound": True},
    )
    assert store.binding("sbx-agent") is None
    assert (await api.call("DELETE", path, LAUNCHER_TOKEN))[1]["unbound"] is False


BINDER_TOKEN = "test-secret-binder-" + "0" * 24


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/v1/permission-requests?project=p1", None),
        ("POST", "/v1/permission-requests/1/answer", {"approve": True, "scope": "call"}),
        ("POST", "/v1/permission-requests/1/confirm", {}),
        ("POST", "/v1/classifications/papers/drafts/" + "a" * 64 + "/accept", {}),
        ("POST", "/v1/classifications/papers/drift/accept", {}),
        ("POST", "/v1/mcp-listings", {}),
        ("POST", "/v1/job-sandboxes/exec-1/release", {}),
        ("POST", "/v1/recovery/fence", {}),
        ("GET", "/v1/environments", None),
        ("POST", "/v1/environments", {}),
        ("DELETE", "/v1/environments/env-1", None),
        ("GET", "/v1/ready", None),
        ("GET", "/v1/runtime-sandboxes", None),
        ("GET", "/v1/no-such-route", None),
    ],
)
async def test_a_binding_only_client_reaches_the_runtime_binding_routes_alone(
    setup: Setup,
    aiohttp_client: ClientFactory,
    tmp_path: Path,
    method: str,
    path: str,
    body: object,
) -> None:
    """Review of a2/direct-wiring: the Kubernetes runtime manager's worker client binds and
    unbinds the runtime's sandbox for its project and does nothing else."""
    manager = idle_manager(tmp_path / "worker-api")
    await manager.start()
    binder = ClientSettings(
        name="manager", token_sha256=token_digest(BINDER_TOKEN), projects=("p1",),
        binding_only=True,
    )  # fmt: skip
    client = await aiohttp_client(
        create_app(manager, TOKEN, setup.environments, (*CLIENTS, binder))
    )
    api = Api(client, setup)
    try:
        assert await api.refused(method, path, BINDER_TOKEN, body) == (403, "policy_denied")
        setup.environments.records.grant("p1", setup.policy)
        bound = {"project_id": "p1", "sandbox_id": "sbx-agent"}
        status, reply = await api.call("POST", "/v1/runtime-sandboxes", BINDER_TOKEN, bound)
        assert (status, reply["policy"]) == (200, setup.policy)
        other = {"project_id": "p2", "sandbox_id": "sbx-agent"}
        assert await api.refused("POST", "/v1/runtime-sandboxes", BINDER_TOKEN, other) == (
            403,
            "policy_denied",
        )
        status, reply = await api.call("DELETE", "/v1/runtime-sandboxes/sbx-agent", BINDER_TOKEN)
        assert (status, reply["unbound"]) == (200, True)
    finally:
        await manager.close()
        manager.store.close()


async def test_a_binding_only_client_has_no_other_permission() -> None:
    for extra in ("operator", "recovery_control", "artifact_retention", "document_catalog"):
        with pytest.raises(ValueError, match="binding_only"):
            ClientSettings(
                name="manager", token_sha256="0" * 64, projects=("p1",),
                binding_only=True, **{extra: True},
            )  # fmt: skip


async def start_api(
    setup: Setup, aiohttp_client: ClientFactory, root: Path
) -> tuple[Api, ExecutionManager]:
    manager = idle_manager(root)
    await manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN, setup.environments, CLIENTS))
    return Api(client, setup), manager


@pytest_asyncio.fixture
async def api(setup: Setup, aiohttp_client: ClientFactory, tmp_path: Path) -> AsyncIterator[Api]:
    started, manager = await start_api(setup, aiohttp_client, tmp_path)
    for project in ("p1", "p2", "p3"):
        setup.environments.records.grant(project, setup.policy)
    yield started
    await manager.close()
    manager.store.close()


async def test_a_client_acts_for_its_own_projects_only(api: Api) -> None:
    for headers in (
        {},
        bearer("test-secret-unknown-" + "0" * 24),
        {"Authorization": A_TOKEN},
        {"Authorization": f"bearer {A_TOKEN}"},
        {"Authorization": f"Bearer  {A_TOKEN}"},
        {"Authorization": f"Bearer\t{A_TOKEN}"},
        {"Authorization": "Bearer "},
        bearer(token_digest(A_TOKEN)),
    ):
        response = await api.client.get("/v1/environments/env-a", headers=headers)
        assert response.status == 401, headers

    # Each client creates its own project's environment and cannot see the other's.
    assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[0] == 201
    env_b = api.environment("env-b", "p2")
    assert (await api.call("POST", "/v1/environments", B_TOKEN, env_b))[0] == 201
    assert await api.refused("POST", "/v1/environments", A_TOKEN, env_b) == (403, "policy_denied")
    status, taken = await api.call(
        "POST", "/v1/environments", B_TOKEN, api.environment("env-a", "p2")
    )
    assert (status, taken["error"]["kind"]) == (409, "conflict")
    assert "p1" not in taken["error"]["message"]
    for method, path, body in (
        ("GET", "/v1/environments/env-a", None),
        ("POST", "/v1/environments/env-a/policy", {"policy": api.setup.policy}),
        ("GET", "/v1/capabilities?environment_id=env-a", None),
        ("DELETE", "/v1/environments/env-a", None),
    ):
        assert await api.refused(method, path, B_TOKEN, body) == (404, "not_found"), path
    assert (await api.call("GET", "/v1/environments/env-a", TOKEN))[0] == 200

    # Executions, files and artifacts of another project do not exist for a client.
    context_a = {"project_id": "p1", "session_id": "s1", "environment_id": "env-a"}
    # Observation inherits exact project/environment scope, before any channel lookup.
    observation = {"context": context_a}
    foreign = {"context": {**context_a, "project_id": "p2"}}
    for route in ("/v1/kernels/observe",):
        assert await api.refused("POST", route, B_TOKEN, observation) == (
            403,
            "policy_denied",
        )
        assert await api.refused("POST", route, B_TOKEN, foreign) == (404, "not_found")
        assert await api.refused("POST", route, TOKEN, foreign) == (404, "not_found")
        assert await api.refused("POST", route, A_TOKEN, observation) == (
            503,
            "unavailable",
        )
    shell = {"kind": "shell", "operation_id": "op-1", "script": "echo hi | tee note.txt"}
    status, started = await api.call(
        "POST", "/v1/executions", A_TOKEN, {**shell, "context": context_a}
    )
    assert status == 201
    execution_id = started["execution_id"]
    finished = await api.finish(execution_id, A_TOKEN)
    assert finished["result"]["stdout"]["text"] == "hi\n"
    for method, path in (
        ("GET", f"/v1/executions/{execution_id}"),
        ("GET", f"/v1/executions/{execution_id}/events"),
        ("POST", f"/v1/executions/{execution_id}/cancel"),
        ("GET", f"/v1/executions/{execution_id}?project_id=p2"),
    ):
        assert await api.refused(method, path, B_TOKEN) == (404, "not_found"), path
    assert (await api.call("GET", f"/v1/executions/{execution_id}?project_id=p1", A_TOKEN))[
        0
    ] == 200
    assert await api.refused("GET", f"/v1/executions/{execution_id}?project_id=p2", A_TOKEN) == (
        404,
        "not_found",
    )
    # A client for several projects acts for each of them.
    assert (await api.call("GET", f"/v1/executions/{execution_id}", C_TOKEN))[0] == 200
    assert (await api.call("GET", f"/v1/executions/{execution_id}", TOKEN))[0] == 200
    borrowed = {**context_a, "project_id": "p2"}
    for path, body in (
        ("/v1/executions", {**shell, "context": borrowed}),
        ("/v1/files/read", {"context": borrowed, "path": "note.txt"}),
        ("/v1/files/write", {"context": borrowed, "path": "x.txt", "content_b64": "eA=="}),
        ("/v1/files/list", {"context": borrowed, "path": "."}),
        ("/v1/kernels/restart", {"context": borrowed, "language": "python"}),
        ("/v1/artifacts", {"context": borrowed, "path": "note.txt"}),
    ):
        assert await api.refused("POST", path, B_TOKEN, body) == (404, "not_found"), path
    assert await api.refused(
        "POST", "/v1/files/read", B_TOKEN, {"context": context_a, "path": "x"}
    ) == (403, "policy_denied")
    # Closing a session elsewhere stops nothing and says so, as where nothing runs.
    status, closed = await api.call("POST", "/v1/sessions/close", B_TOKEN, {"context": borrowed})
    assert (status, closed) == (200, {"kernels": []})
    # The pool's own token may not mix projects in an environment either.
    assert await api.refused("POST", "/v1/executions", TOKEN, {**shell, "context": borrowed}) == (
        409,
        "conflict",
    )
    # Operation IDs are per project: another project's op-1 is a new execution, while
    # the same project's op-1 with another request is still a conflict.
    context_b = {"project_id": "p2", "session_id": "s1", "environment_id": "env-b"}
    status, other = await api.call(
        "POST", "/v1/executions", B_TOKEN, {**shell, "context": context_b}
    )
    assert status == 201
    assert other["execution_id"] != execution_id
    changed = {**shell, "script": "echo changed", "context": context_a}
    assert await api.refused("POST", "/v1/executions", A_TOKEN, changed) == (409, "conflict")

    status, artifact = await api.call(
        "POST", "/v1/artifacts", A_TOKEN, {"context": context_a, "path": "note.txt"}
    )
    assert status == 201
    artifact_id = artifact["artifact_id"]
    for path in (f"/v1/artifacts/{artifact_id}", f"/v1/artifacts/{artifact_id}/content"):
        assert await api.refused("GET", path, B_TOKEN) == (404, "not_found"), path
        response = await api.client.get(path, headers=bearer(A_TOKEN))
        assert response.status == 200
    assert (await api.call("GET", f"/v1/artifacts/{artifact_id}", TOKEN))[0] == 200
    status, read = await api.call(
        "POST", "/v1/files/read", A_TOKEN, {"context": context_a, "path": "note.txt"}
    )
    assert (status, read["size"]) == (200, 3)


async def test_a_client_lists_and_suspends_only_its_projects_environments(api: Api) -> None:
    assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[0] == 201
    env_b = api.environment("env-b", "p2")
    assert (await api.call("POST", "/v1/environments", B_TOKEN, env_b))[0] == 201
    status, listed = await api.call("GET", "/v1/environments", A_TOKEN)
    assert status == 200
    assert [item["environment_id"] for item in listed["environments"]] == ["env-a"]
    _, everything = await api.call("GET", "/v1/environments", TOKEN)
    assert [item["environment_id"] for item in everything["environments"]] == ["env-a", "env-b"]
    # Another project's environment is not there for this client, and keeps running.
    refused = await api.refused("POST", "/v1/environments/env-b/suspend", A_TOKEN)
    assert refused == (404, "not_found")
    status, result = await api.call("POST", "/v1/environments/env-a/suspend", A_TOKEN)
    assert (status, result["suspended"]) == (200, True)
    assert result["environment"]["state"] == "stopped"
    sandbox = result["environment"]["sandbox"]
    assert api.setup.fake.calls("sandbox", "stop") == [["sandbox", "stop", sandbox]]
    # Already stopped: it says so, and stops nothing again.
    _, again = await api.call("POST", "/v1/environments/env-a/suspend", A_TOKEN)
    assert again["suspended"] is True
    assert len(api.setup.fake.calls("sandbox", "stop")) == 1
    _, after = await api.call("GET", "/v1/environments", TOKEN)
    states = {item["environment_id"]: item["state"] for item in after["environments"]}
    assert states == {"env-a": "stopped", "env-b": "ready"}


async def test_a_stop_that_failed_is_tried_again_and_answered_by_the_phase(api: Api) -> None:
    assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[0] == 201
    api.setup.fake.update(fail_stop=True)
    status, first = await api.call("POST", "/v1/environments/env-a/suspend", A_TOKEN)
    # The record says stopped before the stop runs; OpenShell says the sandbox is not.
    assert (status, first["suspended"]) == (200, False)
    assert first["environment"]["state"] == "stopped"
    assert len(api.setup.fake.calls("sandbox", "stop")) == 2  # the stop, then once more
    api.setup.fake.update(fail_stop=False)
    _, second = await api.call("POST", "/v1/environments/env-a/suspend", A_TOKEN)
    assert second["suspended"] is True
    assert len(api.setup.fake.calls("sandbox", "stop")) == 3
    _, third = await api.call("POST", "/v1/environments/env-a/suspend", A_TOKEN)
    assert third["suspended"] is True
    assert len(api.setup.fake.calls("sandbox", "stop")) == 3  # stopped: not again


async def test_kernels_computing_keep_an_environment_up_on_request(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[0] == 201
    monkeypatch.setattr(suspend_module, "SAMPLE_GAP", 0.05)
    samples: list[float] = []

    async def activity(self: ExecutionManager, environment_id: str) -> Activity:
        return Activity(
            last_used=None, running=0, cpu_seconds=samples.pop(0), kernels=1, executor="exe-1"
        )

    monkeypatch.setattr(ExecutionManager, "activity", activity)
    samples.extend([1.0, 2.0])  # a thread a cell left computing: a core's worth
    status, result = await api.call("POST", "/v1/environments/env-a/suspend", A_TOKEN)
    assert (status, result["suspended"]) == (200, False)
    assert result["environment"]["state"] == "ready"
    assert api.setup.fake.calls("sandbox", "stop") == []
    samples.extend([5.0, 5.0])  # idle kernels
    _, idle = await api.call("POST", "/v1/environments/env-a/suspend", A_TOKEN)
    assert idle["suspended"] is True


async def test_an_environment_in_use_is_not_suspended_on_request(api: Api) -> None:
    assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[0] == 201

    async def busy(environment_id: str, idle_since: float) -> bool:
        return False

    api.setup.environments.release_idle = busy
    status, result = await api.call("POST", "/v1/environments/env-a/suspend", A_TOKEN)
    assert (status, result["suspended"]) == (200, False)
    assert result["environment"]["state"] == "ready"
    assert api.setup.fake.calls("sandbox", "stop") == []


async def test_an_environment_id_stays_with_its_project(api: Api) -> None:
    # Deleted or not, an ID belongs to the project that first created it, so nothing a
    # check let through for one project can later reach another's sandbox (review of D78).
    assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[0] == 201
    assert (await api.call("DELETE", "/v1/environments/env-a", A_TOKEN))[0] == 200
    for token in (B_TOKEN, TOKEN):
        status, taken = await api.call(
            "POST", "/v1/environments", token, api.environment("env-a", "p2")
        )
        assert (status, taken["error"]["kind"]) == (409, "conflict"), token
        assert "p1" not in taken["error"]["message"]
    context = {"project_id": "p2", "session_id": "s1", "environment_id": "env-a"}
    shell = {"kind": "shell", "operation_id": "op-x", "script": "true", "context": context}
    assert await api.refused("POST", "/v1/executions", TOKEN, shell) == (409, "conflict")
    assert await api.refused("POST", "/v1/executions", B_TOKEN, shell) == (404, "not_found")
    # A deleted environment is not one a client can use, even its own.
    own = {**shell, "context": {**context, "project_id": "p1"}}
    assert await api.refused("POST", "/v1/executions", A_TOKEN, own) == (404, "not_found")
    status, again = await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a"))
    assert (status, again["state"]) == (201, "ready")


async def test_a_client_uses_only_revisions_granted_to_its_project(
    setup: Setup, aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    api, manager = await start_api(setup, aiohttp_client, tmp_path)
    try:
        unknown = "sha256:" + "0" * 64
        answers = []
        for policy in (setup.policy, unknown):
            body = {**api.environment("env-a"), "policy": policy}
            status, reply = await api.call("POST", "/v1/environments", A_TOKEN, body)
            answers.append((status, reply["error"]["message"].replace(policy, "<revision>")))
        # A revision stored but not granted looks like one never stored.
        assert answers[0] == answers[1]
        assert answers[0][0] == 404
        setup.environments.records.grant("p1", setup.policy)
        assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[
            0
        ] == 201
        revision = setup.add(variant(setup.tmp, "v2.yaml", extra_rule=True))
        assert await api.refused(
            "POST", "/v1/environments/env-a/policy", A_TOKEN, {"policy": revision}
        ) == (404, "not_found")
        setup.environments.records.grant("p1", revision)
        status, revised = await api.call(
            "POST", "/v1/environments/env-a/policy", A_TOKEN, {"policy": revision}
        )
        assert (status, revised["policy"]) == (200, revision)
        # The pool's own token needs no grant.
        status, _ = await api.call(
            "POST", "/v1/environments", TOKEN, api.environment("env-pool", "p9")
        )
        assert status == 201
    finally:
        await manager.close()
        manager.store.close()


async def test_sandboxes_made_by_hand_are_the_pools_alone(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    setup = Setup(tmp_path, sandboxes={"env-hand": "research-1"})
    api, manager = await start_api(setup, aiohttp_client, tmp_path / "worker-api")
    try:
        setup.environments.records.grant("p1", setup.policy)
        for token in (A_TOKEN, TOKEN):
            status, refused = await api.call(
                "POST", "/v1/environments", token, api.environment("env-hand")
            )
            assert (status, refused["error"]["kind"]) == (409, "conflict"), token
        context = {"project_id": "p1", "session_id": "s1", "environment_id": "env-hand"}
        shell = {"kind": "shell", "operation_id": "op-h", "script": "true", "context": context}
        assert await api.refused("POST", "/v1/executions", A_TOKEN, shell) == (404, "not_found")
        status, execution = await api.call("POST", "/v1/executions", TOKEN, shell)
        assert status == 201, execution
        nowhere = {**shell, "context": {**context, "environment_id": "env-nowhere"}}
        assert await api.refused("POST", "/v1/executions", TOKEN, nowhere) == (404, "not_found")
    finally:
        await manager.close()
        manager.store.close()


async def test_a_new_environment_starts_a_history_of_its_own(
    aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    # Review of package commands (2026-10-11), M3: the pool's own token ran package
    # commands for p1 in a sandbox made by hand, "lab"; once the operator retired it, a
    # client of p2 created "lab" and read p1's commands, attempts and lockfiles (D78).
    setup = Setup(tmp_path, sandboxes={"lab": "research-1"})
    api, manager = await start_api(setup, aiohttp_client, tmp_path / "worker-api")
    credential = "test-secret-p1-index-password"
    index = f"--index-url=https://p1user:{credential}@private.p1.example/simple"
    command = PackageCommand(argv=("pip", "install", index, "p1-tool"), via="wrapper")
    lockfile = manager.revisions.prepare({"pip": [{"name": "p1-tool", "version": "4.2"}]})
    assert lockfile is not None
    manager.revisions.observe("lab", "a" * 32)
    manager.revisions.observe(
        "lab",
        "b" * 32,
        "att-0123456789abcdef01234567",
        managed=True,
        commands=(command,),
        lockfile=lockfile,
    )
    await manager.close()
    manager.store.close()
    setup = Setup(tmp_path)  # the sandbox made by hand is retired
    api, manager = await start_api(setup, aiohttp_client, tmp_path / "worker-api")
    try:
        setup.environments.records.grant("p2", setup.policy)
        status, created = await api.call(
            "POST", "/v1/environments", B_TOKEN, api.environment("lab", project_id="p2")
        )
        assert status == 201, created
        for token in (B_TOKEN, TOKEN):
            status, history = await api.call("GET", "/v1/environments/lab/revisions", token)
            assert (status, history["revisions"]) == (200, []), token
            path = "/v1/environments/lab/revisions/1/lockfile"
            assert await api.refused("GET", path, token) == (404, "not_found"), token
        assert not manager.revisions.blobs.path(lockfile.ref.sha256).exists()
        # What it records from now on is its own.
        manager.revisions.observe("lab", "c" * 32)
        status, history = await api.call("GET", "/v1/environments/lab/revisions", B_TOKEN)
        assert [entry["fingerprint"] for entry in history["revisions"]] == ["c" * 32]
    finally:
        await manager.close()
        manager.store.close()


async def test_review2_m1_a_deleted_environment_takes_its_revisions_and_lockfiles_along(
    setup: Setup, aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    # Second review of package commands (2026-10-11), M1: revisions were forgotten only
    # when an ID was created again, so each environment ever created kept up to 64 MiB of
    # lockfiles and 500 revisions.
    api, manager = await start_api(setup, aiohttp_client, tmp_path / "worker-api")
    blobs = tmp_path / "worker-api" / "lockfiles" / "blobs"
    try:
        setup.environments.records.grant("p1", setup.policy)
        for environment in ("env-a", "env-b"):
            status, created = await api.call(
                "POST", "/v1/environments", A_TOKEN, api.environment(environment)
            )
            assert status == 201, created
            for index in range(3):
                lockfile = manager.revisions.prepare(
                    {"pip": [{"name": f"{environment}-p{index}", "version": "1.0"}]}
                )
                assert lockfile is not None
                manager.revisions.observe(environment, f"{index + 1:032x}", lockfile=lockfile)
        assert len(list(blobs.glob("*/*"))) == 6
        status, deleted = await api.call("DELETE", "/v1/environments/env-a", A_TOKEN)
        assert (status, deleted["state"]) == (200, "deleted"), deleted
        assert manager.revisions.history("env-a").revisions == ()
        assert manager.store.lockfiles("env-a") == []
        assert len(list(blobs.glob("*/*"))) == 3  # env-b's stay
        assert len(manager.revisions.history("env-b").revisions) == 3
    finally:
        await manager.close()
        manager.store.close()


async def test_review3_l2_an_environment_the_worker_gives_up_forgets_its_revisions(
    setup: Setup, aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    # Third review of package commands (2026-10-11), L2: an environment the worker gave up
    # itself, its sandbox gone or come back as another, was marked deleted with its
    # revisions and lockfiles kept.
    api, manager = await start_api(setup, aiohttp_client, tmp_path / "worker-api")
    blobs = tmp_path / "worker-api" / "lockfiles" / "blobs"
    try:
        setup.environments.records.grant("p1", setup.policy)
        status, created = await api.call(
            "POST", "/v1/environments", A_TOKEN, api.environment("env-1")
        )
        assert status == 201, created
        for index in range(3):
            lockfile = manager.revisions.prepare({"pip": [{"name": f"p{index}", "version": "1.0"}]})
            assert lockfile is not None
            manager.revisions.observe("env-1", f"{index + 1:032x}", lockfile=lockfile)
        state = setup.fake.read()
        state["sandboxes"][created["sandbox"]]["id"] = "another-sandbox"
        setup.fake.write(state)
        with pytest.raises(WorkerError):
            await setup.environments.resume("env-1")
        assert setup.environments.get("env-1").state is EnvironmentState.DELETED
        assert len(manager.revisions.history("env-1").revisions) == 3
        state = setup.fake.read()
        del state["sandboxes"][created["sandbox"]]
        setup.fake.write(state)
        await setup.environments.delete("env-1")
        assert manager.revisions.history("env-1").revisions == ()
        assert list(blobs.glob("*/*")) == []
    finally:
        await manager.close()
        manager.store.close()


async def test_abandon_retains_evidence_until_explicit_delete_or_reuse(setup: Setup) -> None:
    # Abandonment may originate inside an operation; cleanup waits for explicit reuse.
    told: list[tuple[str, str]] = []

    async def first(environment_id: str) -> None:
        told.append(("first", environment_id))

    async def second(environment_id: str) -> None:
        told.append(("second", environment_id))

    setup.environments.deleted.extend([first, second])
    info, _ = await setup.environments.create(setup.request())
    state = setup.fake.read()
    state["sandboxes"][info.sandbox]["id"] = "another-sandbox"
    setup.fake.write(state)
    with pytest.raises(WorkerError):
        await setup.environments.resume("env-1")
    assert told == []
    await setup.environments.create(setup.request())
    assert told == [("first", "env-1"), ("second", "env-1")]
    await setup.environments.create(setup.request("env-2"))
    await setup.environments.delete("env-2")
    assert told[2:] == [("first", "env-2"), ("second", "env-2")]


async def test_abandon_from_provisioning_task_does_not_wait_for_its_own_drain(
    setup: Setup, tmp_path: Path
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    info, _ = await setup.environments.create(setup.request())
    record = ProvisionOperation(
        provision_id="prv-self",
        request=ProvisionRequest(
            operation_id="self",
            purpose="synthetic abandonment cycle",
            conda=("example",),
            context=ExecutionContext(project_id="p1", session_id="s1", environment_id="env-1"),
        ),
        state=ProvisionState.INSTALLING,
        submitted_at=datetime.now(UTC),
    )
    store.insert_provision(record, "digest")
    store.set_package_status("env-1", "needs_recovery", "prv-self", 0)
    operation = _Op(record, asyncio.get_running_loop().create_future())
    provisioner = Provisioner.__new__(Provisioner)
    provisioner.store = store
    provisioner._ops = {record.provision_id: operation}
    setup.environments.required_deleted.append(provisioner.forget)
    try:
        task = asyncio.create_task(setup.environments._abandon(info, "synthetic replacement"))
        operation.task = task
        with pytest.raises(WorkerError, match="synthetic replacement"):
            await asyncio.wait_for(task, 1)
        assert task.cancelling() == 0
        assert not store.provisioning_finished("env-1")
        assert store.package_gate("env-1").operation == "prv-self"
        assert setup.environments.get("env-1").state is EnvironmentState.DELETED
    finally:
        store.close()


async def test_required_delete_drain_owns_double_cancellation_and_name_reuse(
    setup: Setup, tmp_path: Path
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    entered, finish = asyncio.Event(), asyncio.Event()
    order: list[str] = []
    original, _ = await setup.environments.create(setup.request())
    store.bind_package_gate("env-1", repr((original.sandbox, original.sandbox_id)))

    async def drain(environment_id: str) -> None:
        entered.set()
        await finish.wait()
        # An old delayed classification is still part of the owned drain.
        store.bind_package_gate(environment_id, repr((original.sandbox, original.sandbox_id)))
        store.set_package_status(environment_id, "needs_recovery", "prv-old", 1)
        order.append("drained")

    async def clear(environment_id: str) -> None:
        assert order[-1] == "drained"
        store.forget_package_gate(
            environment_id, binding=repr((original.sandbox, original.sandbox_id))
        )
        order.append("cleared")

    setup.environments.required_deleted.append(drain)
    setup.environments.confirmed_deleted.append(clear)
    deletion = asyncio.create_task(setup.environments.delete("env-1"))
    recreation: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        deletion.cancel()
        await asyncio.sleep(0)
        deletion.cancel()
        recreation = asyncio.create_task(setup.environments.create(setup.request()))
        await asyncio.sleep(0.02)
        assert not deletion.done()
        assert not recreation.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await deletion
        replacement, created = await asyncio.wait_for(recreation, 5)
        assert created
        assert replacement.sandbox_id != original.sandbox_id
        store.bind_package_gate("env-1", repr((replacement.sandbox, replacement.sandbox_id)))
        assert store.package_gate("env-1").mode == "ready"
        assert order == ["drained", "cleared", "drained", "cleared"]
    finally:
        finish.set()
        await asyncio.gather(
            deletion, *([recreation] if recreation is not None else []), return_exceptions=True
        )
        store.close()


async def test_failed_required_delete_cleanup_retains_fence_and_retries_exact_proof(
    setup: Setup, tmp_path: Path
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    original, _ = await setup.environments.create(setup.request())
    store.bind_package_gate("env-1", repr((original.sandbox, original.sandbox_id)))
    store.set_package_status("env-1", "needs_recovery", "prv-old", 2)
    failed = True
    ordinary: list[str] = []

    async def drain(environment_id: str) -> None:
        if failed:
            raise WorkerError(ErrorKind.UNAVAILABLE, "classification write failed")

    async def clear(environment_id: str) -> None:
        store.forget_package_gate(
            environment_id, binding=repr((original.sandbox, original.sandbox_id))
        )

    async def told(environment_id: str) -> None:
        ordinary.append(environment_id)

    setup.environments.required_deleted.append(drain)
    setup.environments.confirmed_deleted.append(clear)
    setup.environments.deleted.append(told)
    try:
        with pytest.raises(WorkerError, match="classification write failed"):
            await setup.environments.delete("env-1")
        assert setup.environments.get("env-1").state is EnvironmentState.DELETED
        assert setup.environments.records.deletion_confirmed(original)
        assert ordinary == []
        assert store.package_gate("env-1").mode == "needs_recovery"
        failed = False
        await setup.environments.delete("env-1")
        assert ordinary == ["env-1"]
        assert store.package_gate("env-1").mode == "ready"
        replacement, _ = await setup.environments.create(setup.request())
        assert not setup.environments.records.deletion_confirmed(replacement)
    finally:
        store.close()


async def test_delete_never_certifies_or_mutates_same_name_replacement(setup: Setup) -> None:
    original, _ = await setup.environments.create(setup.request())
    state = setup.fake.read()
    state["sandboxes"][original.sandbox]["id"] = "healthy-replacement"
    setup.fake.write(state)
    before_delete = setup.fake.calls("sandbox", "delete")
    before_stop = setup.fake.calls("sandbox", "stop")
    with pytest.raises(WorkerError, match="replacement deletion refused"):
        await setup.environments.delete("env-1")
    assert setup.fake.calls("sandbox", "delete") == before_delete
    assert setup.fake.calls("sandbox", "stop") == before_stop
    assert not setup.environments.records.deletion_confirmed(original)
    assert setup.fake.read()["sandboxes"][original.sandbox].get("phase", "Ready") == "Ready"


async def test_a_known_environment_never_falls_back_to_a_sandbox_made_by_hand(
    tmp_path: Path,
) -> None:
    # An ID the worker created, then deleted, is never sent to a static mapping that an
    # operator added for the same ID later (review of D78).
    setup = Setup(tmp_path)
    await setup.environments.create(setup.request("env-1"))
    await setup.environments.delete("env-1")
    settings = setup.settings.model_copy(
        update={"sandboxes": {"env-1": "research-1", "env-2": "research-2"}}
    )
    backend = OpenShellBackend(
        settings,
        tmp_path / "state",
        setup.environments.sandbox,
        manages=setup.environments.manages,
    )
    assert backend.sandbox_id("env-1") is None
    assert backend.sandbox_id("env-2") == "research-2"


async def test_a_busy_environment_does_not_hold_up_admission(setup: Setup) -> None:
    info, _ = await setup.environments.create(setup.request())
    setup.fake.draft(info.sandbox, "eprint.example.net:443")
    watcher = DraftWatcher(setup.environments, 1.0)
    lock = setup.environments._lock("env-1")
    async with lock:
        assert await watcher.check("env-1", info.sandbox) == ()
    decisions = await watcher.check("env-1", info.sandbox)
    assert [(d.host, d.admitted) for d in decisions] == [("eprint.example.net", True)]


async def test_clients_are_named_by_their_tokens_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pool-ceiling.yaml").write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
    )
    assert main(["token", "--out", str(tmp_path / "worker.token")]) == 0
    pool_line = capsys.readouterr().out.strip()
    assert main(["token", "--out", str(tmp_path / "client.token")]) == 0
    client_line = capsys.readouterr().out.strip()
    assert re.fullmatch(r'token_sha256 = "[0-9a-f]{64}"', client_line)
    assert client_line != pool_line
    assert main(["token", "--sha256", str(tmp_path / "client.token")]) == 0
    assert capsys.readouterr().out.strip() == client_line
    token = (tmp_path / "client.token").read_text().strip()
    assert client_line == f'token_sha256 = "{token_digest(token)}"'
    assert main(["token", "--sha256", str(tmp_path / "missing.token")]) == 1
    (tmp_path / "short.token").write_text("test-secret-short\n")
    assert main(["token", "--sha256", str(tmp_path / "short.token")]) == 1

    base = (
        'token_file = "worker.token"\nstate_dir = "state"\n\n[openshell]\nisolation_tier = 3\n\n'
        "[openshell.environments]\n"
        f'policy_store = "middleware"\ntargets = "{TARGETS}"\nceiling = "pool-ceiling.yaml"\n'
        f'profiles = {{minimal = "{IMAGE}"}}\n'
    )
    settings = tmp_path / "worker.toml"

    def client(name: str, line: str, projects: str = '["p1"]') -> str:
        return f'\n[[clients]]\nname = "{name}"\n{line}\nprojects = {projects}\n'

    settings.write_text(base + client("orchestrator-p1", client_line))
    loaded = load_settings(settings)
    assert [(c.name, c.projects) for c in loaded.clients] == [("orchestrator-p1", ("p1",))]
    assert loaded.token()
    # policy add --project grants the revision to the project's clients.
    assert (
        main(["policy", "add", "--settings", str(settings), "--project", "p1", str(EXAMPLE)]) == 0
    )
    digest = re.search(r"as (sha256:[0-9a-f]{64})", capsys.readouterr().out)
    assert digest is not None
    records = EnvironmentRecords(tmp_path / "state" / "environments.sqlite3")
    try:
        assert records.granted("p1", digest[1])
        assert not records.granted("p2", digest[1])
    finally:
        records.close()
    with pytest.raises(SystemExit) as refused:
        main(["policy", "add", "--settings", str(settings), "--project", "../p1", str(EXAMPLE)])
    assert refused.value.code == 2
    settings.write_text(base + client("orchestrator-p1", pool_line))
    with pytest.raises(SettingsError, match="pool's own token"):
        load_settings(settings).token()
    for clients in (
        client("c1", client_line) + client("c1", pool_line),
        client("c1", client_line) + client("c2", client_line),
    ):
        settings.write_text(base + clients)
        with pytest.raises(SettingsError, match="its own name and token"):
            load_settings(settings)
    for broken in (
        client("c1", client_line, "[]"),
        client("c1", 'token_sha256 = "abc"'),
        client("c1", client_line, '["../p1"]'),
    ):
        settings.write_text(base + broken)
        with pytest.raises(SettingsError):
            load_settings(settings)
    settings.write_text(
        'token_file = "worker.token"\nstate_dir = "state"\n\n[local]\n' + client("c1", client_line)
    )
    with pytest.raises(SettingsError, match="clients need"):
        load_settings(settings)
    with pytest.raises(ValueError, match="managed environments"):
        create_app(idle_manager(tmp_path), TOKEN, None, CLIENTS)
    pool_twin = ClientSettings(name="twin", token_sha256=token_digest(TOKEN), projects=("p1",))
    (tmp_path / "twin").mkdir()
    environments = Setup(tmp_path / "twin").environments
    try:
        with pytest.raises(ValueError, match="pool's own token"):
            create_app(idle_manager(tmp_path / "twin"), TOKEN, environments, (pool_twin,))
    finally:
        environments.close()


async def test_admission_does_not_wait_for_a_change_under_way(setup: Setup) -> None:
    # One client's revisions must not hold up admission for every project (review of D78).
    info, _ = await setup.environments.create(setup.request())
    setup.fake.draft(info.sandbox, "eprint.example.net:443")
    real = setup.environments.cli.run
    release = asyncio.Event()

    async def slow(*args: str, timeout: float = 120.0) -> str:
        if args[:2] == ("policy", "set"):
            await release.wait()
        return await real(*args, timeout=timeout)

    setup.environments.cli.run = slow
    revision = setup.add(variant(setup.tmp, "v2.yaml", extra_rule=True))
    revising = asyncio.create_task(setup.environments.revise("env-1", revision))
    queued = asyncio.create_task(setup.environments.revise("env-1", setup.policy))
    await asyncio.sleep(0.3)
    watcher = DraftWatcher(setup.environments, 1.0)
    started = time.monotonic()
    assert await watcher.check("env-1", info.sandbox) == ()
    assert (
        await setup.environments.admit("env-1", [("eprint.example.net", 443)], wait=False) is None
    )
    assert time.monotonic() - started < 5
    release.set()
    await revising
    await queued
    setup.environments.cli.run = real
    assert not setup.environments.busy("env-1")
    decisions = await watcher.check("env-1", info.sandbox)
    assert [(d.host, d.admitted) for d in decisions] == [("eprint.example.net", True)]


async def test_changes_are_logged_by_route_and_cannot_be_forged(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sandbox_science.worker.app")
    forged = (
        "/v1/x%0a2026-10-11 INFO sandbox_science.worker.app: "
        "DELETE /v1/environments/p2-default by client-b"
    )
    response = await api.client.post(forged, json={}, headers=bearer(A_TOKEN))
    assert response.status == 404
    assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[0] == 201
    context = {"project_id": "p1", "session_id": "s1", "environment_id": "env-a"}
    assert (await api.call("POST", "/v1/files/list", A_TOKEN, {"context": context}))[0] == 200
    messages = [r.getMessage() for r in caplog.records if r.name == "sandbox_science.worker.app"]
    assert "POST an unknown route by client-a: 404" in messages
    assert "POST /v1/environments by client-a: 201" in messages
    assert not any("\n" in message or "client-b" in message for message in messages)
    assert not any("/v1/files/list" in message for message in messages)


async def test_closing_a_session_where_no_environment_is_left_is_no_error(api: Api) -> None:
    assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[0] == 201
    assert (await api.call("DELETE", "/v1/environments/env-a", A_TOKEN))[0] == 200
    context = {"project_id": "p1", "session_id": "s1", "environment_id": "env-a"}
    for token, where in ((A_TOKEN, context), (TOKEN, {**context, "environment_id": "env-x"})):
        status, closed = await api.call("POST", "/v1/sessions/close", token, {"context": where})
        assert (status, closed) == (200, {"kernels": []}), token
    # A project the client may not act for is still refused.
    borrowed = {**context, "project_id": "p2"}
    assert await api.refused("POST", "/v1/sessions/close", A_TOKEN, {"context": borrowed}) == (
        403,
        "policy_denied",
    )


async def test_the_backend_never_resolves_without_knowing_the_workers_environments(
    setup: Setup,
) -> None:
    with pytest.raises(ValueError, match="go together"):
        OpenShellBackend(setup.settings, setup.tmp / "state", setup.environments.sandbox)
    settings = Settings.model_validate(
        {
            "token_file": setup.tmp / "worker.token",
            "state_dir": setup.tmp / "state",
            "openshell": setup.settings.model_dump(),
        }
    )
    backend = cli_backend(settings, setup.environments)
    assert isinstance(backend, OpenShellBackend)
    assert backend.manages is not None


async def test_the_built_lifecycle_gives_the_policys_job_limits(tmp_path: Path) -> None:
    """Environments.job_resources, given to the manager when the worker is built."""
    setup = Setup(tmp_path)
    settings = Settings.model_validate(
        {
            "token_file": tmp_path / "worker.token",
            "state_dir": tmp_path / "built",
            "openshell": {**setup.settings.model_dump(), "sandboxes": {"env-hand": "research-1"}},
        }
    )
    parts = build_worker(settings, "test-secret-" + "j" * 40)
    try:
        environments = parts.environments
        assert environments is not None
        environments.cli.env = setup.environments.cli.env  # the fake openshell
        job_resources = parts.manager.lifecycle.job_resources
        assert job_resources is not None
        with pytest.raises(WorkerError) as refused:
            job_resources("env-unknown")
        assert refused.value.kind is ErrorKind.POLICY_DENIED
        assert job_resources("env-hand") == environments.ceiling.resources
    finally:
        await parts.close()
        setup.environments.close()


async def test_a_worker_built_on_managed_environments_gets_their_lifecycle(setup: Setup) -> None:
    """The manager learns the environments' bindings, recycles, notices and job limits
    when the worker is built, not from assignments after."""
    settings = Settings.model_validate(
        {
            "token_file": setup.tmp / "worker.token",
            "state_dir": setup.tmp / "built",
            "openshell": setup.settings.model_dump(),
        }
    )
    parts = build_worker(settings, "test-secret-" + "b" * 40)
    try:
        environments = parts.environments
        assert environments is not None
        lifecycle = parts.manager.lifecycle
        assert lifecycle.daemon_identity == environments.daemon_identity
        assert lifecycle.recycle_daemon == environments.recycle_executor
        assert lifecycle.notices == environments.notices
        assert lifecycle.job_resources == environments.job_resources
        assert lifecycle.confirmed_daemon_loss is not None
        assert parts.manager.backend.daemon
        assert environments.release == parts.manager.release
    finally:
        await parts.close()


async def test_hosts_are_admitted_ahead_of_use(api: Api) -> None:
    assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-a")))[0] == 201
    path = "/v1/environments/env-a/admissions"
    body = {
        "hosts": [{"host": "EPrint.example.net"}, {"host": "pypi.org"}, {"host": "printer.local"}]
    }
    status, result = await api.call("POST", path, A_TOKEN, body)
    assert status == 200, result
    assert {(d["host"], d["port"], d["admitted"]) for d in result["decisions"]} == {
        ("eprint.example.net", 443, True),
        ("pypi.org", 443, False),
        ("printer.local", 443, False),
    }
    assert result["environment"]["admitted_hosts"] == ["eprint.example.net"]
    # One revision for the batch, and none when nothing new is admitted.
    assert len(api.setup.fake.calls("policy", "set")) == 1
    assert (await api.call("POST", path, A_TOKEN, body))[0] == 200
    assert len(api.setup.fake.calls("policy", "set")) == 1
    assert await api.refused("POST", path, B_TOKEN, body) == (404, "not_found")
    too_many = {"hosts": [{"host": f"h{index}.example.org"} for index in range(33)]}
    assert await api.refused("POST", path, A_TOKEN, too_many) == (400, "invalid_request")
    status, offered = await api.call("GET", "/v1/capabilities?environment_id=env-a", A_TOKEN)
    assert (status, "admission" in offered["features"]) == (200, True)
    # A suspended environment is started first.
    environments = api.setup.environments
    environments.release_idle = always
    assert await environments.suspend("env-a", time.monotonic())
    status, resumed = await api.call(
        "POST", path, A_TOKEN, {"hosts": [{"host": "www.iacr.org", "port": 443}]}
    )
    assert status == 200, resumed
    assert resumed["environment"]["state"] == "ready"
    assert resumed["decisions"][0]["admitted"]
    environments.settings = environments.settings.model_copy(update={"admission": False})
    assert await api.refused("POST", path, A_TOKEN, body) == (422, "unsupported")


async def test_a_client_has_at_most_its_limit_of_environments(
    setup: Setup, aiohttp_client: ClientFactory, tmp_path: Path
) -> None:
    limited = (
        ClientSettings(
            name="client-a",
            token_sha256=token_digest(A_TOKEN),
            projects=("p1", "p3"),
            max_environments=2,
        ),
    )
    manager = idle_manager(tmp_path)
    api = Api(await aiohttp_client(create_app(manager, TOKEN, setup.environments, limited)), setup)
    for project in ("p1", "p3"):
        setup.environments.records.grant(project, setup.policy)
    # The fake CLI keeps its state in one file, which concurrent calls would garble; the
    # requests still race through the worker's check of the limit.
    cli, serial = setup.environments.cli, asyncio.Lock()
    run = cli.run

    async def run_serially(*args: str, timeout: float = 120.0) -> str:
        async with serial:  # every CLI call, sandbox() included, goes through run()
            return await run(*args, timeout=timeout)

    cli.run = run_serially
    try:
        # Concurrent requests across the client's projects: exactly its limit succeed.
        results = await asyncio.gather(
            *(
                api.call(
                    "POST",
                    "/v1/environments",
                    A_TOKEN,
                    api.environment(f"env-{index}", "p1" if index % 2 else "p3"),
                )
                for index in range(4)
            )
        )
        assert sorted(status for status, _ in results) == [201, 201, 403, 403]
        made = [body for status, body in results if status == 201]
        refused = [body for status, body in results if status == 403]
        assert "limit" in refused[0]["error"]["message"]
        # Asking again for one it has is no new environment.
        again = api.environment(made[0]["environment_id"], made[0]["project_id"])
        assert (await api.call("POST", "/v1/environments", A_TOKEN, again))[0] == 200
        assert await api.refused("POST", "/v1/environments", A_TOKEN, api.environment("env-9")) == (
            403,
            "policy_denied",
        )
        # Deleting one makes room.
        path = f"/v1/environments/{made[0]['environment_id']}"
        assert (await api.call("DELETE", path, A_TOKEN))[0] == 200
        assert (await api.call("POST", "/v1/environments", A_TOKEN, api.environment("env-9")))[
            0
        ] == 201
        # The pool's own token has no such limit.
        status, _ = await api.call(
            "POST", "/v1/environments", TOKEN, api.environment("env-pool", "p9")
        )
        assert status == 201
        assert (
            ClientSettings(name="c", token_sha256="0" * 64, projects=("p1",)).max_environments == 8
        )
    finally:
        await manager.close()
        manager.store.close()


async def test_explicit_delete_after_abandon_requires_original_absence_before_clear(
    setup: Setup, tmp_path: Path
) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
    )
    original, _ = await setup.environments.create(setup.request())
    store.bind_package_gate("env-1", repr((original.sandbox, original.sandbox_id)))
    store.set_package_status("env-1", "needs_recovery", "prv-original", 1)
    state = setup.fake.read()
    state["sandboxes"][original.sandbox]["id"] = "replacement"
    setup.fake.write(state)

    async def clear(environment_id: str) -> None:
        store.forget_package_gate(
            environment_id, binding=repr((original.sandbox, original.sandbox_id))
        )

    setup.environments.confirmed_deleted.append(clear)
    setup.environments.reusing.append(manager.guard_package_reuse)
    try:
        with pytest.raises(WorkerError):
            await setup.environments.resume("env-1")
        assert setup.environments.get("env-1").state is EnvironmentState.DELETED
        creates = setup.fake.calls("sandbox", "create")
        with pytest.raises(WorkerError, match="requires confirmed sandbox deletion"):
            await setup.environments.create(setup.request())
        assert setup.fake.calls("sandbox", "create") == creates
        with pytest.raises(WorkerError, match="replacement deletion refused"):
            await setup.environments.delete("env-1")
        assert store.package_gate("env-1").mode == "needs_recovery"
        state = setup.fake.read()
        del state["sandboxes"][original.sandbox]  # operator-confirmed absence
        setup.fake.write(state)
        await setup.environments.delete("env-1")
        assert setup.environments.records.deletion_confirmed(original)
        assert store.package_gate("env-1").mode == "ready"
        assert setup.fake.calls("sandbox", "stop") == []
        assert setup.fake.calls("sandbox", "delete") == []
    finally:
        await manager.close()
        store.close()


@pytest.mark.parametrize("proof", ["missing", "wrong", "bound", "endpoint", "ready"])
async def test_required_original_binding_guard_precedes_guest_delete(
    setup: Setup, tmp_path: Path, proof: str
) -> None:
    store = Store(tmp_path / "worker.sqlite3")

    def identity(environment_id: str) -> tuple[str, str] | None:
        info = setup.environments.get(environment_id)
        return (info.sandbox, info.sandbox_id) if info.sandbox_id is not None else None

    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(), tmp_path),
        Blobs(tmp_path / "artifacts"),
        pool_id="test",
        max_artifact_size=1024,
        state_dir=tmp_path,
        lifecycle=EnvironmentLifecycle(daemon_identity=identity),
    )
    original, _ = await setup.environments.create(setup.request())
    # create_app registers these required guards independently of the optional
    # executor release callback. Exercise that supported composition directly.
    setup.environments.release = None
    setup.environments.required_deleting.append(manager.guard_package_deletion)
    setup.environments.confirmed_deleted.append(manager.forget_package_gate)
    setup.environments.reusing.append(manager.guard_package_reuse)
    binding = repr(identity("env-1"))
    if proof in {"wrong", "bound"}:
        store.bind_package_gate("env-1", "another-original" if proof == "wrong" else binding)
    elif proof == "endpoint":
        store.remember_endpoint("env-1", "incarnation", identity("env-1"))
    if proof != "ready":
        store.set_package_status("env-1", "needs_recovery", "prv-old", 1)
    try:
        if proof in {"missing", "wrong"}:
            with pytest.raises(WorkerError):
                await setup.environments.delete("env-1")
            assert setup.environments.get("env-1") == original
            assert not setup.environments.records.deletion_confirmed(original)
            assert setup.fake.calls("sandbox", "stop") == []
            assert setup.fake.calls("sandbox", "delete") == []
            assert store.package_gate("env-1").mode == "needs_recovery"
            # Even a stale/direct callback with current metadata proof must not
            # reset an unproven or differently bound original hold.
            setup.environments.records.confirm_deletion(original)
            with pytest.raises(WorkerError):
                await setup.environments.delete("env-1")
            with pytest.raises(WorkerError):
                await manager.forget_package_gate("env-1")
            assert store.package_gate("env-1").mode == "needs_recovery"
        else:
            await setup.environments.delete("env-1")
            await setup.environments.delete("env-1")
            assert setup.environments.records.deletion_confirmed(original)
            assert store.package_gate("env-1").mode == "ready"
            replacement, created = await setup.environments.create(setup.request())
            assert created
            assert replacement.sandbox_id != original.sandbox_id
            store.bind_package_gate("env-1", repr(identity("env-1")))
    finally:
        await manager.close()
        store.close()


@pytest.mark.parametrize("name", ["poll_seconds", "resume_timeout", "start_backoff"])
async def test_environment_waits_must_be_positive(tmp_path: Path, name: str) -> None:
    waits: dict[str, float] = {name: 0.0}
    with pytest.raises(ValueError, match=name):
        Setup(tmp_path, None, None, **waits)
