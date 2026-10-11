# SPDX-License-Identifier: MIT
"""The worker creates, revises and deletes research sandboxes (D73), against a fake ``openshell``.

The fake keeps its state in a JSON file and behaves as OpenShell v0.1.3 did in the babbage
labs: ``sandbox get`` of an unknown name fails with "not found", ``policy get --base``
returns the filesystem section with OpenShell's baseline paths added, and ``policy set``
refuses a document that removes any filesystem path.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient

from sandbox_science.contracts import EnvironmentRequest, EnvironmentState, ErrorKind
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend, OpenShellBackend
from sandbox_science.worker.cli import main
from sandbox_science.worker.environments import Environments, sandbox_name
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import (
    EnvironmentSettings,
    LocalBackendSettings,
    OpenShellBackendSettings,
    load_settings,
)
from sandbox_science.worker.store import Store

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
    out = json.dumps({"id": box["id"], "name": name, "phase": "Ready",
                      "configuration_admission": {"state": "accepted", "error": ""}})
elif command == ["sandbox", "delete"]:
    sandboxes.pop(argv[2], None)
    out = "deleted"
elif command == ["policy", "get"]:
    box = sandboxes[argv[2]]
    out = json.dumps({"policy": box["policy"], "version": box["version"], "status": "effective"})
elif command == ["policy", "set"]:
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
