# SPDX-License-Identifier: MIT
"""Provisioning operations through the worker API (docs/specs/conda-provisioning.md).

A throwaway environment (a venv with a ``conda-meta`` directory) stands in for the sandbox's
Conda prefix, and fake_manager.py, with a fake index, for micromamba and pip. A small
package cache stands in for the one behind OpenShell: it asks the real policy middleware
about every request, as OpenShell would, so the middleware's grants are what lets a file
download. Permission requests and their answers go through the middleware's event store
and its command line, as on a pool. Nothing reaches a network.

The acceptance fixtures the slice covers carry their names (CP-F1 and so on); the rest
check the decision against the middleware's, the queue, cancellation, and the access
checks for clients limited to projects (D78).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sandbox_science.contracts import (
    CellRequest,
    EnvironmentInfo,
    EnvironmentState,
    ExecutionContext,
    ExecutionState,
    JobRequest,
    KernelRestartRequest,
    Lockfile,
    ProvisioningQueue,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionState,
    ReadFileRequest,
    ResolvedFile,
    RevisionHistory,
    ShellRequest,
)
from sandbox_science.event_store import EventStore, Record, StoreReader
from sandbox_science.policy import HardFloor, LoadedPolicy, PoolCeiling
from sandbox_science.policy.approvals import Approvals, RequestKey
from sandbox_science.policy.store import PolicyStore
from sandbox_science.policy_middleware.cli import _read_records  # pyright: ignore[reportPrivateUsage]
from sandbox_science.policy_middleware.cli import main as middleware
from sandbox_science.policy_middleware.evaluator import HttpRequestFacts, PolicyMiddleware
from sandbox_science.policy_middleware.events import StoreSink
from sandbox_science.worker import provisioner as provisioner_module
from sandbox_science.worker import resolution
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.catalog import Catalog
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.permissions import Permissions, PermissionStoreError
from sandbox_science.worker.provisioner import Governing, Provisioner
from sandbox_science.worker.settings import ClientSettings, LocalBackendSettings, token_digest
from sandbox_science.worker.store import Store

FAKE = Path(__file__).with_name("fake_manager.py")
TOKEN = "p" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SANDBOX = "sbx-provisioning"
CONTEXT = {"project_id": "p1", "session_id": "s1", "environment_id": "env1"}
ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
ServerFactory = Callable[..., Awaitable[TestServer]]

POLICY = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: provisioning-test
defaults:
  action: escalate
resources:
  wall_time: 10m
packages:
  conda:
    channels: [conda-forge]
    action: allow
  pypi:
    action: allow
"""
ESCALATE = POLICY.replace(
    "channels: [conda-forge]\n    action: allow", "channels: [conda-forge]\n    action: escalate"
)
EXCEPTED = POLICY + "  agpl_exceptions:\n    conda: [pymupdf]\n"

GP_OUTPUT = "[274177, 1; 67280421310721, 1]"
INDEX: dict[str, dict[str, dict[str, Any]]] = {
    "conda": {
        "pari": {
            "version": "2.15.4",
            "build": "h0_0",
            "subdir": "linux-64",
            "license": "GPL-2.0-or-later",
            "depends": ["gmp >=6"],
            "provides": {"bin/fake-gp": f"#!/bin/sh\ncat > /dev/null\necho '{GP_OUTPUT}'\n"},
        },
        "gmp": {
            "version": "6.3.0",
            "build": "h0_1",
            "subdir": "linux-64",
            "license": "LGPL-3.0-only",
        },
        "fpylll": {
            "version": "0.6.1",
            "build": "py313h0_0",
            "subdir": "linux-64",
            "license": "GPL-2.0-or-later",
        },
        "pymupdf": {
            "version": "1.24.10",
            "build": "py313h0_0",
            "subdir": "linux-64",
            "license": "AGPL-3.0-only",
        },
        "badpostlink": {
            "version": "1.0",
            "build": "h0_0",
            "subdir": "linux-64",
            "postlink": "fail",
        },
        "slowlink": {"version": "1.0", "build": "h0_0", "subdir": "linux-64", "postlink": "slow"},
        "otherbad": {"version": "1.0", "build": "h0_0", "subdir": "linux-64", "postlink": "fail"},
        "lostlink": {"version": "1.0", "build": "h0_0", "subdir": "linux-64", "postlink": "lost"},
        "wronggp": {
            "version": "1.0",
            "build": "h0_0",
            "provides": {"bin/fake-gp": "#!/bin/sh\necho 42\n"},
        },
    },
    "pypi": {
        "fakeflint": {"version": "0.7.1", "license": "MIT", "depends": ["fakedep"]},
        "fakedep": {"version": "2.0", "license": "BSD-3-Clause"},
        "agplpy": {"version": "1.0", "license": "AGPL-3.0-or-later"},
        "gmp": {"version": "6.3.0", "license": "LGPL-3.0-only"},  # as the Conda gmp installs it
    },
}
CATALOG = f"""\
# SPDX-License-Identifier: MIT
version = 3

[capabilities.gp]
description = "a stand-in for PARI/GP"
conda = ["pari"]

[[capabilities.gp.probes]]
id = "gp-factor"
argv = ["sh", "-c", "printf 'print(factor(2^64+1))\\\\n' | fake-gp -q -f"]
stdout = "{GP_OUTPUT}"
timeout_seconds = 60

[capabilities.wrong]
description = "a capability whose program answers wrongly"
conda = ["wronggp"]

[[capabilities.wrong.probes]]
id = "wrong-factor"
argv = ["sh", "-c", "fake-gp"]
stdout = "{GP_OUTPUT}"
"""


# A throwaway environment with stand-in package managers.


@dataclass
class Prefix:
    root: Path
    python: Path
    site: Path
    log: Path


def make_prefix(directory: Path) -> Prefix:
    root = directory / "prefix"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True)  # noqa: S603
    python = root / "bin" / "python"
    purelib = "import sysconfig; print(sysconfig.get_paths()['purelib'])"
    site = Path(subprocess.check_output([python, "-c", purelib], text=True).strip())  # noqa: S603
    (root / "conda-meta").mkdir()
    (root / "conda-meta" / "history").write_text("")
    shutil.copy(FAKE, site / "fakepm.py")
    (site / "pip").mkdir()
    (site / "pip" / "__init__.py").write_text("")
    (site / "pip" / "__main__.py").write_text(
        "import sys\nimport fakepm\nsys.exit(fakepm.main(['pip', *sys.argv[1:]]))\n"
    )
    for tool in ("pip", "pip3", "micromamba"):
        script = root / "bin" / tool
        script.write_text(f'#!/bin/sh\nexec "{python}" -m fakepm {tool} "$@"\n')
        script.chmod(0o755)
    return Prefix(root, python, site, directory / "installs.log")


# The policy side: what governs the environments, and a package cache behind the middleware.


class Governed:
    """Stands in for the worker's environments: env1 and env2 run under the bound policy."""

    def __init__(self, policies: PolicyStore, digest: str) -> None:
        self.policies = policies
        self.digest = digest
        self.environments = {"env1": SANDBOX, "env2": "sbx-other"}

    def governing(self, environment_id: str) -> Governing | None:
        sandbox = self.environments.get(environment_id)
        if sandbox is None:
            return None
        policy = self.policies.policy(self.digest)
        return Governing(policy, PoolCeiling.unrestricted(), HardFloor(), sandbox)


@dataclass
class Fetch:
    path: str
    allowed: bool
    code: str


@dataclass
class Cache:
    """A package cache that asks the policy middleware about each request, as OpenShell
    does, for the environment's sandbox; it serves a few bytes for every allowed file."""

    middleware: PolicyMiddleware
    loaded: Callable[[], LoadedPolicy]
    port: int = 0
    fetched: list[Fetch] = field(default_factory=list[Fetch])

    async def handle(self, request: web.Request) -> web.Response:
        policy = self.loaded()
        facts = HttpRequestFacts(
            sandbox_id=SANDBOX,
            request_id=f"req-{len(self.fetched)}",
            scheme="http",
            host="127.0.0.1",
            port=self.port,
            method=request.method,
            path=request.raw_path.split("?", 1)[0],
            query=request.query_string,
            headers=(),
            body=b"",
            config={
                "policy": {"name": policy.policy.metadata.name, "digest": policy.digest},
                "package_cache": {"host": "127.0.0.1", "port": self.port},
            },
        )
        verdict = await asyncio.to_thread(self.middleware.evaluate_http, facts)
        self.fetched.append(Fetch(facts.path, verdict.allowed, verdict.reason_code))
        if not verdict.allowed:
            return web.Response(status=403, text=verdict.reason_code)
        return web.Response(body=b"package bytes")

    def artifacts(self) -> list[Fetch]:
        return [fetch for fetch in self.fetched if fetch.path.endswith((".conda", ".whl"))]


@dataclass
class World:
    client: TestClient[Any, Any]
    manager: ExecutionManager
    provisioner: Provisioner
    store: Store
    policies: PolicyStore
    events: Path
    cache: Cache
    prefix: Prefix
    settings: Path
    governed: Governed
    clock: Clock
    sink: EventStore

    async def provision(
        self, operation_id: str, *, expected: int = 201, **fields: Any
    ) -> dict[str, Any]:
        context = {**CONTEXT, **fields.pop("context", {})}
        body = {"operation_id": operation_id, "context": context, "purpose": "a test", **fields}
        response = await self.client.post("/v1/provisioning", json=body, headers=AUTH)
        answer = await response.json()
        assert response.status == expected, answer
        return answer

    async def get(self, provision_id: str, **query: str) -> ProvisionOperation:
        response = await self.client.get(
            f"/v1/provisioning/{provision_id}", params=query, headers=AUTH
        )
        assert response.status == 200, await response.text()
        return ProvisionOperation.model_validate_json(await response.read())

    async def until(
        self, provision_id: str, done: Callable[[ProvisionOperation], bool], timeout: float = 120
    ) -> ProvisionOperation:
        deadline = time.monotonic() + timeout
        seq = -1
        operation = await self.get(provision_id)
        while time.monotonic() < deadline:
            operation = await self.get(provision_id, after=str(seq), wait="5")
            if done(operation):
                return operation
            seq = operation.seq
        raise AssertionError(f"{provision_id} never got there: {operation}")

    async def finished(self, provision_id: str) -> ProvisionOperation:
        return await self.until(provision_id, lambda op: op.state == "finished")

    async def waiting(self, provision_id: str) -> ProvisionOperation:
        return await self.until(provision_id, lambda op: op.state == "awaiting_permission")

    async def queue(self, environment_id: str = "env1") -> ProvisioningQueue:
        response = await self.client.get(
            f"/v1/environments/{environment_id}/provisioning", headers=AUTH
        )
        assert response.status == 200, await response.text()
        return ProvisioningQueue.model_validate_json(await response.read())

    async def revisions(self) -> RevisionHistory:
        response = await self.client.get("/v1/environments/env1/revisions", headers=AUTH)
        assert response.status == 200
        return RevisionHistory.model_validate_json(await response.read())

    async def lockfile(self, revision: int) -> Lockfile:
        response = await self.client.get(
            f"/v1/environments/env1/revisions/{revision}/lockfile", headers=AUTH
        )
        assert response.status == 200, await response.text()
        return Lockfile.model_validate_json(await response.read())

    async def execution(self, execution_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/v1/executions/{execution_id}", headers=AUTH)
        assert response.status == 200
        return await response.json()

    def answer(self, command: str, seq: int) -> None:
        """The operator's answer on the pool host: the middleware's command line."""
        assert middleware([command, "--settings", str(self.settings), "--request", str(seq)]) == 0

    def records(self) -> list[Record]:
        with StoreReader(self.events) as reader:
            return list(reader.records())

    def withdrawn(self, seq: int) -> bool:
        """Whether the worker closed permission request ``seq`` for good."""
        approvals = Approvals()
        approvals.load(self.records())
        return approvals.withdrawn(seq)

    def use(self, text: str) -> LoadedPolicy:
        """Store a policy and put the environment and its sandbox under it."""
        loaded = self.policies.add_policy(text.encode())
        self.policies.bind(SANDBOX, loaded.digest)
        self.governed.digest = loaded.digest
        return loaded


class Clock:
    """The provisioner's idea of now, which a test can move forward."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


Factory = Callable[..., Awaitable[World]]


@pytest_asyncio.fixture
async def make_world(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Factory]:
    made: list[World] = []

    async def make(
        policy: str = POLICY,
        *,
        restorer: Any = None,
        environments: Any = None,
        clients: tuple[ClientSettings, ...] = (),
        kind: type[Provisioner] = Provisioner,
    ) -> World:
        prefix = make_prefix(tmp_path)
        index = tmp_path / "index.json"
        index.write_text(json.dumps(INDEX))
        state = tmp_path / "middleware"
        policies = PolicyStore(state)
        loaded = policies.add_policy(policy.encode())
        policies.bind(SANDBOX, loaded.digest)
        events = state / "events.db"
        sink = EventStore(events, timeout=5)
        governed = Governed(policies, loaded.digest)
        cache = Cache(
            PolicyMiddleware(
                policies,
                ceiling=PoolCeiling.unrestricted(),
                floor=HardFloor(),
                events=StoreSink(sink),
                requests=partial(_read_records, events),
            ),
            lambda: policies.policy(governed.digest),
        )
        app = web.Application()
        app.router.add_get("/{tail:.*}", cache.handle)
        server = await aiohttp_server(app)
        assert server.port is not None
        cache.port = server.port
        url = f"http://127.0.0.1:{server.port}"
        # Only the stand-ins: the host's own package managers are not on the executor's PATH.
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("FAKEPM_LOG", str(prefix.log))
        monkeypatch.setenv("FAKEPM_INDEX", str(index))
        monkeypatch.setenv("FAKEPM_CACHE", url)
        root = tmp_path / "worker"
        store = Store(root / "worker.sqlite3")
        manager = ExecutionManager(
            store,
            LocalBackend(LocalBackendSettings(python=str(prefix.python)), root),
            Blobs(root / "artifacts"),
            pool_id="test-pool",
            max_artifact_size=1 << 20,
            state_dir=root,
            package_cache=url,
        )
        await manager.start()
        clock = Clock()
        provisioner = kind(
            manager,
            catalog=Catalog.parse(CATALOG.encode()),
            policies=governed,
            permissions=Permissions(events),
            cache=url,
            restorer=restorer,
            clock=clock,
            poll=0.1,
        )
        await provisioner.start()
        client = await aiohttp_client(
            create_app(manager, TOKEN, environments, clients, provisioner)
        )
        settings = tmp_path / "middleware.toml"
        (tmp_path / "ceiling.yaml").write_text(
            "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
        )
        settings.write_text(
            f'state_dir = "{state}"\nceiling = "ceiling.yaml"\nallow_unauthenticated = true\n'
        )
        world = World(
            client,
            manager,
            provisioner,
            store,
            policies,
            events,
            cache,
            prefix,
            settings,
            governed,
            clock,
            sink,
        )
        made.append(world)
        return world

    yield make
    for world in made:
        await world.provisioner.close()
        await world.manager.close()
        if world.provisioner.permissions is not None:
            world.provisioner.permissions.close()
        world.sink.close()
        world.store.close()


def gate(world: World) -> Path:
    return world.prefix.root.parent / "gate"


# The acceptance fixtures.


@pytest.mark.asyncio
async def test_cp_f1_a_capability_installs_in_its_turn_records_and_probes(
    make_world: Factory,
) -> None:
    world = await make_world()
    submitted = await world.provision("op-gp", capability="gp")
    assert submitted["state"] == "queued"
    done = await world.finished(submitted["provision_id"])
    assert done.outcome == "completed", done
    assert (done.reason, done.stage, done.error) == (None, None, None)
    assert done.catalog is not None
    assert done.catalog.version == 3
    # It probed first (CP-2), observed the installer, resolved, installed and probed again.
    check, version, resolve, install, probe = done.executions
    for execution_id in done.executions:
        execution = await world.execution(execution_id)
        assert execution["origin"] == done.provision_id
        assert execution["operation_id"].startswith(f"{done.provision_id}.")
    assert (await world.execution(version))["operation_id"].endswith(".version-conda")
    assert done.installer_steps is not None
    assert [step.label for step in done.installer_steps] == [
        "version-conda",
        "resolve-conda",
        "install-conda",
    ]
    assert done.installer_steps[0].observed_version == "2.9.0"
    assert all(step.log_status == "stored" for step in done.installer_steps)
    # Every resolved file, with what the policy decided for its download (CP-6, CP-7).
    files = {file.name: file for file in done.files}
    assert set(files) == {"pari", "gmp"}
    pari = files["pari"]
    assert (pari.channel, pari.subdir, pari.file) == (
        "conda-forge",
        "linux-64",
        "pari-2.15.4-h0_0.conda",
    )
    assert (pari.action, pari.rule, pari.license, pari.agpl) == (
        "allow",
        "packages.conda",
        "GPL-2.0-or-later",
        False,
    )
    assert pari.sha256 is not None
    assert len(pari.sha256) == 64
    # One managed revision, made by the operation's install, with its lockfile (CP-16, CP-18).
    assert (done.revision_before, done.revision_after) == (0, 1)
    history = await world.revisions()
    last = history.revisions[-1]
    assert (last.revision, last.managed, last.operation) == (1, True, done.provision_id)
    assert last.changed_by == (await world.execution(install))["attempt_id"]
    assert last.commands[0].argv[:2] == ("micromamba", "install")
    lockfile = await world.lockfile(1)
    [listed] = [package for package in lockfile.conda if package.name == "pari"]
    assert (listed.channel, listed.build, listed.sha256, listed.license) == (
        "conda-forge",
        "h0_0",
        pari.sha256,
        "GPL-2.0-or-later",
    )
    assert str(world.cache.port) not in lockfile.model_dump_json()
    assert {(change.name, change.before, change.after) for change in done.changes} == {
        ("pari", None, "2.15.4"),
        ("gmp", None, "6.3.0"),
    }
    # The probe passed, functionally, and belongs to revision 1 (CP-21, CP-24).
    passed = [p for p in done.probes if p.execution_id == probe]
    assert [(p.probe, p.kind, p.status, p.revision) for p in passed] == [
        ("gp-factor", "functional", "passed", 1)
    ]
    failed_first = [p for p in done.probes if p.execution_id == check]
    assert [p.status for p in failed_first] == ["failed"]  # before the install, at revision 0
    assert [p.probe for p in last.probes] == ["gp-factor"]
    # Every package file came through the cache, decided under `packages`.
    assert {fetch.path for fetch in world.cache.artifacts()} == {
        "/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda",
        "/conda/conda-forge/linux-64/gmp-6.3.0-h0_1.conda",
    }
    assert all(fetch.allowed for fetch in world.cache.fetched)
    del resolve
    owner = world.provisioner._ops[done.provision_id].task
    assert owner is not None
    await owner
    observations = [r for r in world.records() if r.kind.startswith("provision.")]
    assert [r.kind for r in observations] == ["provision.requested", "provision.finished"]
    assert observations[-1].data()["outcome"] == "completed"
    await world.provision("op-gp", capability="gp", expected=200)
    assert len([r for r in world.records() if r.kind.startswith("provision.")]) == 2


@pytest.mark.asyncio
async def test_cp_f2_a_capability_that_works_is_already_satisfied(make_world: Factory) -> None:
    world = await make_world()
    first = await world.finished((await world.provision("op-1", capability="gp"))["provision_id"])
    again = await world.finished((await world.provision("op-2", capability="gp"))["provision_id"])
    assert again.outcome == "already_satisfied", again
    assert (again.revision_before, again.revision_after) == (1, 1)
    assert again.executions == ()  # nothing ran: the revision 1 probe record stands
    [cited] = again.probes
    [recorded] = [p for p in first.probes if p.revision == 1]
    assert cited == recorded
    assert (await world.revisions()).revisions[-1].revision == 1


@pytest.mark.asyncio
async def test_cp_f3_an_escalated_install_is_denied_then_approved(make_world: Factory) -> None:
    world = await make_world(ESCALATE)
    first = await world.waiting((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    assert first.outcome == "awaiting_permission"
    seq = first.permission_request
    assert seq is not None
    assert first.next_step is not None
    assert f"approve --request {seq}" in first.next_step
    [request] = [record for record in world.records() if record.seq == seq]
    payload = request.data()
    assert (request.kind, request.source) == ("escalation", "worker")
    target = payload["target"]
    assert isinstance(target, dict)
    assert target["kind"] == "packages"
    listed = payload["files"]
    assert isinstance(listed, list)
    assert [(f["name"], f["build"], len(f["sha256"])) for f in listed] == [
        ("fpylll", "py313h0_0", 64)
    ]
    assert world.cache.artifacts() == []  # nothing downloads while it waits
    world.answer("deny", seq)
    denied = await world.finished(first.provision_id)
    assert (denied.outcome, denied.reason, denied.stage) == (
        "denied",
        "permission_denied",
        "permission",
    )
    assert denied.revision_after is None
    assert world.cache.artifacts() == []
    # Asked again, it is a new request; once approved, exactly those files install.
    second = await world.waiting((await world.provision("op-2", conda=["fpylll"]))["provision_id"])
    assert second.permission_request not in (None, seq)
    assert second.permission_request is not None
    world.answer("approve", second.permission_request)
    done = await world.finished(second.provision_id)
    assert done.outcome == "completed", done
    assert [(f.path, f.allowed, f.code) for f in world.cache.artifacts()] == [
        ("/conda/conda-forge/linux-64/fpylll-0.6.1-py313h0_0.conda", True, "")
    ]
    assert [p.kind for p in done.probes] == ["import_check"]
    assert done.probes[0].status == "passed"
    # A download of another file, outside the approved set, is refused.
    other = f"http://127.0.0.1:{world.cache.port}/conda/conda-forge/linux-64/gmp-6.3.0-h0_1.conda"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(urllib.error.HTTPError) as refused:
        await asyncio.to_thread(opener.open, other, timeout=10)
    assert refused.value.code == 403
    assert world.cache.fetched[-1].code == "approval_required"
    # The grant ended with the operation: the worker withdrew the request for good, so
    # neither research code nor a later approval can fetch its files.
    approved = f"http://127.0.0.1:{world.cache.port}{world.cache.artifacts()[0].path}"
    with pytest.raises(urllib.error.HTTPError) as closed:
        await asyncio.to_thread(opener.open, approved, timeout=10)
    assert closed.value.code == 403
    assert world.withdrawn(second.permission_request)
    arguments = ["approve", "--settings", str(world.settings)]
    assert middleware([*arguments, "--request", str(second.permission_request)]) == 1


@pytest.mark.asyncio
async def test_cp_f4_an_unlisted_channel_is_denied_at_resolve(make_world: Factory) -> None:
    world = await make_world()
    done = await world.finished(
        (await world.provision("op-1", conda=["bioconda::samtools"]))["provision_id"]
    )
    assert (done.outcome, done.reason, done.stage) == ("denied", "policy_denied", "resolve")
    assert done.error is not None
    assert "bioconda" in done.error.message
    assert "packages.conda" in done.error.message
    assert done.executions == ()
    assert world.cache.fetched == []


@pytest.mark.asyncio
async def test_cp_f5_a_failed_post_link_script_leaves_the_environment_needing_recovery(
    make_world: Factory,
) -> None:
    world = await make_world()
    first = await world.finished((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    assert first.outcome == "completed"
    done = await world.finished(
        (await world.provision("op-2", conda=["badpostlink"]))["provision_id"]
    )
    assert (done.outcome, done.reason, done.stage) == (
        "needs_recovery",
        "environment_changed",
        "post-link",
    )
    assert (done.revision_before, done.revision_after) == (1, 2)
    assert done.error is not None
    assert "post-link script failed" in done.error.message
    assert done.next_step is not None
    assert "revision 1" in done.next_step
    queue = await world.queue()
    assert (queue.status, queue.revision, queue.operations) == ("needs_recovery", 2, ())
    later = await world.provision("op-3", conda=["fpylll"], expected=503)
    assert later["error"]["kind"] == "unavailable"
    assert done.provision_id in later["error"]["message"]


@pytest.mark.asyncio
async def test_recovery_refuses_all_new_work_but_preserves_running_status_files_and_replay(
    make_world: Factory,
) -> None:
    world = await make_world()
    context = ExecutionContext(**CONTEXT)
    original = ShellRequest(
        operation_id="already-running",
        context=context,
        script="printf preserved > saved; sleep 1; printf finished",
    )
    running, _ = await world.manager.submit(original)
    async with asyncio.timeout(10):
        async for event in world.manager.events(running.execution_id, 0):
            if getattr(event, "state", None) is ExecutionState.RUNNING:
                break
    failed = await world.finished(
        (await world.provision("failed", conda=["badpostlink"]))["provision_id"]
    )
    assert failed.outcome == "needs_recovery"
    for request in (
        ShellRequest(operation_id="refused-shell", context=context, argv=("true",)),
        CellRequest(operation_id="refused-cell", context=context, language="python", code="1"),
        JobRequest(
            operation_id="refused-job",
            context=context,
            argv=("true",),
            purpose="test",
            expected_progress="none",
        ),
    ):
        with pytest.raises(WorkerError, match="needs_recovery"):
            await world.manager.submit(request)
        assert world.store.find_operation("p1", request.operation_id) is None
    with pytest.raises(WorkerError, match="needs_recovery"):
        await world.manager.restart_kernel(KernelRestartRequest(context=context, language="python"))
    replay, created = await world.manager.submit(original)
    assert replay.execution_id == running.execution_id
    assert not created
    assert (await world.manager.wait(running.execution_id)).state is ExecutionState.SUCCEEDED
    content = await world.manager.read_file(ReadFileRequest(context=context, path="saved"))
    assert base64.b64decode(content.content_b64) == b"preserved"
    healthy = ShellRequest(
        operation_id="healthy",
        context=context.model_copy(update={"environment_id": "env2"}),
        argv=("true",),
    )
    admitted, _ = await world.manager.submit(healthy)
    assert (await world.manager.wait(admitted.execution_id)).state is ExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_queued_provision_cannot_run_after_preceding_install_needs_recovery(
    make_world: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await make_world()
    held = gate(world)
    monkeypatch.setenv("FAKEPM_GATE", str(held))
    first = await world.provision("first", conda=["badpostlink"])
    second = await world.provision("queued", conda=["fpylll"])
    await world.until(first["provision_id"], lambda op: op.state == "installing")
    held.touch()
    assert (await world.finished(first["provision_id"])).outcome == "needs_recovery"
    refused = await world.finished(second["provision_id"])
    assert refused.outcome == "failed"
    assert refused.stage == "queue"
    assert refused.error is not None
    assert refused.error.kind == "unavailable"
    assert refused.executions == ()


class Restoring:
    def __init__(self) -> None:
        self.asked: list[tuple[str, int]] = []

    async def restore(self, environment_id: str, revision: int) -> bool:
        self.asked.append((environment_id, revision))
        return True


@pytest.mark.asyncio
async def test_a_restorer_turns_a_failed_link_into_a_rollback(make_world: Factory) -> None:
    restorer = Restoring()
    world = await make_world(restorer=restorer)
    done = await world.finished(
        (await world.provision("op-1", conda=["badpostlink"]))["provision_id"]
    )
    assert (done.outcome, done.stage) == ("rolled_back", "post-link")
    assert restorer.asked == [("env1", 0)]
    assert (await world.queue()).status == "ready"


@pytest.mark.asyncio
async def test_cp_f7_operations_take_turns_in_request_order(
    make_world: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world()
    held = gate(world)
    monkeypatch.setenv("FAKEPM_GATE", str(held))  # before the executor starts: it inherits it
    a = await world.provision("op-a", conda=["fpylll"])
    b = await world.provision("op-b", conda=["gmp"], context={"session_id": "s2"})
    c = await world.provision("op-c", pip=["fakeflint"], pip_reason="not on conda-forge")
    await world.until(a["provision_id"], lambda op: op.state == "installing")
    queue = await world.queue()
    assert queue.status == "changing"
    assert [op.provision_id for op in queue.operations] == [
        a["provision_id"],
        b["provision_id"],
        c["provision_id"],
    ]
    assert [op.queue_position for op in queue.operations] == [None, 1, 2]
    assert [op.state for op in queue.operations[1:]] == ["queued", "queued"]
    assert (await world.get(b["provision_id"])).queue_position == 1
    held.touch()
    first = await world.finished(a["provision_id"])
    second = await world.finished(b["provision_id"])
    third = await world.finished(c["provision_id"])
    assert [op.outcome for op in (first, second, third)] == ["completed"] * 3
    assert [(op.revision_before, op.revision_after) for op in (first, second, third)] == [
        (0, 1),
        (1, 2),
        (2, 3),
    ]
    # Each was planned against the revision when it was asked for, and ran later.
    assert second.planned_revision in (None, 0)
    assert first.ended_at is not None
    assert second.started_at is not None
    assert first.ended_at <= second.started_at
    history = (await world.revisions()).revisions
    assert [(r.revision, r.operation) for r in history[1:]] == [
        (1, first.provision_id),
        (2, second.provision_id),
        (3, third.provision_id),
    ]
    assert all(r.lockfile is not None for r in history[1:])
    assert (await world.queue()).operations == ()


@pytest.mark.asyncio
async def test_cp_f10_an_agpl_package_is_refused_before_download(make_world: Factory) -> None:
    world = await make_world()
    done = await world.finished((await world.provision("op-1", conda=["pymupdf"]))["provision_id"])
    assert (done.outcome, done.reason, done.stage) == ("denied", "agpl_license", "resolve")
    assert done.error is not None
    assert "pymupdf" in done.error.message
    assert "AGPL-3.0-only" in done.error.message
    assert [f.agpl for f in done.files] == [True]
    assert world.cache.artifacts() == []
    assert (await world.revisions()).revisions[-1].revision == 0


@pytest.mark.asyncio
async def test_an_agpl_package_the_policy_excepts_installs(make_world: Factory) -> None:
    world = await make_world(EXCEPTED)
    done = await world.finished((await world.provision("op-1", conda=["pymupdf"]))["provision_id"])
    assert done.outcome == "completed", done


# More of the spec.


@pytest.mark.asyncio
async def test_system_packages_are_refused(make_world: Factory) -> None:
    world = await make_world()
    done = await world.finished(
        (await world.provision("op-1", system=["libgmp-dev"]))["provision_id"]
    )
    assert (done.outcome, done.reason) == ("denied", "base_image_change_required")
    assert done.error is not None
    assert done.error.message.startswith("base_image_change_required: libgmp-dev")


@pytest.mark.asyncio
async def test_pip_specs_install_exactly_the_resolved_files(make_world: Factory) -> None:
    world = await make_world()
    done = await world.finished(
        (await world.provision("op-1", pip=["fakeflint>=0.7"], pip_reason="conda-forge lacks it"))[
            "provision_id"
        ]
    )
    assert done.outcome == "completed", done
    assert done.request.pip_reason == "conda-forge lacks it"
    files = {file.name: file for file in done.files}
    assert set(files) == {"fakeflint", "fakedep"}
    assert (
        files["fakeflint"].url
        == "https://files.pythonhosted.org/packages/fa/ke/fakeflint-0.7.1-py3-none-any.whl"
    )
    assert files["fakeflint"].rule == "packages.pypi"
    install = (await world.revisions()).revisions[-1].commands[0].argv
    assert install[:2] == ("pip", "install")
    assert "--require-hashes" in install
    assert [(p.probe, p.kind, p.status) for p in done.probes] == [
        ("installed:pypi:fakeflint", "import_check", "passed")
    ]
    assert {fetch.path.split("/")[3] for fetch in world.cache.artifacts()} == {
        "fakeflint",
        "fakedep",
    }
    agpl = await world.finished(
        (await world.provision("op-2", pip=["agplpy"], pip_reason="conda-forge lacks it"))[
            "provision_id"
        ]
    )
    assert (agpl.outcome, agpl.reason) == ("denied", "agpl_license")


@pytest.mark.asyncio
async def test_packages_installed_already_are_already_satisfied(make_world: Factory) -> None:
    world = await make_world()
    await world.finished((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    again = await world.finished(
        (await world.provision("op-2", conda=["fpylll>=0.6"]))["provision_id"]
    )
    assert again.outcome == "already_satisfied"
    assert [(p.probe, p.status, p.output) for p in again.probes] == [
        ("installed:conda:fpylll", "passed", "fpylll 0.6.1 py313h0_0")
    ]


@pytest.mark.asyncio
async def test_a_pip_name_a_conda_package_satisfies_is_already_satisfied(
    make_world: Factory,
) -> None:
    world = await make_world()
    await world.finished((await world.provision("op-1", conda=["gmp"]))["provision_id"])
    again = await world.finished(
        (await world.provision("op-2", pip=["gmp"], pip_reason="r"))["provision_id"]
    )
    assert again.outcome == "already_satisfied", again
    assert [(p.probe, p.status, p.output) for p in again.probes] == [
        ("installed:pypi:gmp", "passed", "gmp 6.3.0 (the Conda package gmp)")
    ]


@pytest.mark.asyncio
async def test_a_failing_probe_leaves_the_revision_standing(make_world: Factory) -> None:
    world = await make_world()
    done = await world.finished((await world.provision("op-1", capability="wrong"))["provision_id"])
    assert done.outcome == "completed", done
    assert done.revision_after == 1
    after = [p for p in done.probes if p.revision == 1]
    assert [(p.probe, p.status) for p in after] == [("wrong-factor", "failed")]
    assert done.next_step is not None
    assert "did not pass" in done.next_step
    history = (await world.revisions()).revisions
    assert [p.status for p in history[-1].probes] == ["failed"]


@pytest.mark.asyncio
async def test_the_decision_matches_the_middlewares(make_world: Factory) -> None:
    world = await make_world()
    paths = [
        "/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda",
        "/conda/conda-forge/noarch/toolz-1.0-pyhd8ed1ab_0.conda",
        "/conda/bioconda/linux-64/samtools-1.20-h0_0.conda",
        f"/pypi/files/fakeflint/{'a' * 64}/packages/fa/ke/fakeflint-0.7.1-py3-none-any.whl",
        "/conda/conda-forge/linux-64/broken path.conda",
    ]
    policies = [
        POLICY,
        ESCALATE,
        POLICY.replace("action: allow\n  pypi", "action: deny\n  pypi"),
        POLICY.replace("  pypi:\n    action: allow\n", ""),  # unconfigured: defaults.action
        POLICY.replace("pypi:\n    action: allow", "pypi:\n    action: alert"),
    ]
    codes = {"allow": "", "alert": "", "escalate": "approval_required", "deny": "policy_denied"}
    for text in policies:
        loaded = world.use(text)
        items = [resolution.Resolved(_file_at(path), path) for path in paths]
        decided = resolution.decide(items, loaded, PoolCeiling.unrestricted(), HardFloor())
        for path, file in zip(paths, decided.files, strict=True):
            facts = _facts(world, loaded, path)
            verdict = world.cache.middleware.evaluate_http(facts)
            expected = codes[file.action]
            got = verdict.reason_code
            if file.action == "deny":
                assert not verdict.allowed, path
                assert got in ("policy_denied", "request_unclassifiable"), (path, got)
            else:
                assert (verdict.allowed, got) == (file.action != "escalate", expected), (
                    path,
                    file.action,
                    got,
                )


PLACEHOLDER = ResolvedFile(
    ecosystem="conda", name="x", version="1", file="x-1-0.conda", action="allow"
)


def _file_at(path: str) -> ResolvedFile:
    """The record of the file at ``path``, as the parsers make sure it is; the placeholder
    for a path that names no file."""
    parts = path.split("/")
    if parts[1] == "pypi":
        return ResolvedFile(
            ecosystem="pypi", name=parts[3], version="0.7.1", file=parts[-1], action="allow"
        )
    try:
        name, version, build = parts[4].removesuffix(".conda").rsplit("-", 2)
        return ResolvedFile(
            ecosystem="conda",
            name=name,
            version=version,
            build=build,
            channel=parts[2],
            subdir=parts[3],
            file=parts[4],
            action="allow",
        )
    except ValueError:
        return PLACEHOLDER


def _facts(world: World, loaded: LoadedPolicy, path: str) -> HttpRequestFacts:
    return HttpRequestFacts(
        sandbox_id=SANDBOX,
        request_id="req",
        scheme="http",
        host="127.0.0.1",
        port=world.cache.port,
        method="GET",
        path=path,
        query="",
        headers=(),
        body=b"",
        config={
            "policy": {"name": loaded.policy.metadata.name, "digest": loaded.digest},
            "package_cache": {"host": "127.0.0.1", "port": world.cache.port},
        },
    )


# Cancellation, timeouts and restarts.


@pytest.mark.asyncio
async def test_cancelling_a_queued_operation_ends_it_before_its_turn(
    make_world: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world()
    held = gate(world)
    monkeypatch.setenv("FAKEPM_GATE", str(held))
    first = await world.provision("op-1", conda=["fpylll"])
    second = await world.provision("op-2", conda=["gmp"])
    await world.until(first["provision_id"], lambda op: op.state == "installing")
    path = f"/v1/provisioning/{second['provision_id']}/cancel"
    response = await world.client.post(path, headers=AUTH)
    assert response.status == 200
    cancelled = await world.finished(second["provision_id"])
    held.touch()
    done = await world.finished(first["provision_id"])
    assert (cancelled.outcome, cancelled.reason, cancelled.stage) == (
        "failed",
        "cancelled",
        "queue",
    )
    assert cancelled.executions == ()
    assert done.outcome == "completed"


@pytest.mark.asyncio
async def test_cancelling_an_operation_awaiting_permission_withdraws_its_request(
    make_world: Factory,
) -> None:
    world = await make_world(ESCALATE)
    waiting = await world.waiting((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    response = await world.client.post(
        f"/v1/provisioning/{waiting.provision_id}/cancel", headers=AUTH
    )
    assert response.status == 200
    done = await world.finished(waiting.provision_id)
    assert (done.outcome, done.reason, done.stage) == ("failed", "cancelled", "permission")
    [denial] = [r for r in world.records() if r.kind == "denial"]
    assert (denial.source, denial.data()["request_seq"]) == ("worker", waiting.permission_request)
    assert denial.data()["withdrawn"] is True
    # A late approval is refused: the request is closed for good.
    assert waiting.permission_request is not None
    command = ["approve", "--settings", str(world.settings)]
    assert middleware([*command, "--request", str(waiting.permission_request)]) == 1
    assert world.cache.artifacts() == []


@pytest.mark.asyncio
async def test_cancelling_after_the_approval_closes_the_grant(
    make_world: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world(ESCALATE)
    monkeypatch.setenv("FAKEPM_GATE", str(gate(world)))  # never opened: the install waits
    waiting = await world.waiting((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    assert waiting.permission_request is not None
    world.answer("approve", waiting.permission_request)
    installing = await world.until(waiting.provision_id, lambda op: op.state == "installing")
    await asyncio.sleep(0.5)
    path = f"/v1/provisioning/{installing.provision_id}/cancel"
    assert (await world.client.post(path, headers=AUTH)).status == 200
    done = await world.finished(waiting.provision_id)
    assert (done.outcome, done.reason) == ("failed", "cancelled")
    assert world.withdrawn(waiting.permission_request)
    approved = f"http://127.0.0.1:{world.cache.port}{_path_of(done.files[0])}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(urllib.error.HTTPError) as closed:
        await asyncio.to_thread(opener.open, approved, timeout=10)
    assert closed.value.code == 403


def _path_of(file: ResolvedFile) -> str:
    return f"/conda/{file.channel}/{file.subdir}/{file.file}"


@pytest.mark.asyncio
async def test_cancelling_an_install_stops_it_and_records_where(
    make_world: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world()
    monkeypatch.setenv("FAKEPM_GATE", str(gate(world)))  # never opened: the install waits
    operation = await world.provision("op-1", conda=["fpylll"])
    await world.until(operation["provision_id"], lambda op: len(op.executions) >= 3)
    await world.until(operation["provision_id"], lambda op: op.state == "installing")
    await asyncio.sleep(0.5)
    path = f"/v1/provisioning/{operation['provision_id']}/cancel"
    response = await world.client.post(path, headers=AUTH)
    assert response.status == 200
    done = await world.finished(operation["provision_id"])
    assert (done.outcome, done.reason) == ("failed", "cancelled")
    assert done.error is not None
    assert done.error.kind == "cancelled"
    install = await world.execution(done.executions[-1])
    assert install["state"] == "cancelled"
    assert (await world.revisions()).revisions[-1].revision == 0


@pytest.mark.asyncio
async def test_a_cancelled_install_that_linked_needs_recovery(make_world: Factory) -> None:
    # CP-14: a cancelled install follows CP-13; this one had linked its package.
    world = await make_world()
    operation = await world.provision("op-1", conda=["slowlink"])
    deadline = time.monotonic() + 60
    while not (world.prefix.log.exists() and "postlink slowlink" in world.prefix.log.read_text()):
        assert time.monotonic() < deadline, "the install never linked"
        await asyncio.sleep(0.05)
    path = f"/v1/provisioning/{operation['provision_id']}/cancel"
    assert (await world.client.post(path, headers=AUTH)).status == 200
    done = await world.finished(operation["provision_id"])
    assert (done.outcome, done.reason, done.stage) == (
        "needs_recovery",
        "environment_changed",
        "link",
    )
    assert done.error is not None
    assert done.error.kind == "cancelled"
    assert (done.revision_before, done.revision_after) == (0, 1)
    assert (await world.queue()).status == "needs_recovery"


@pytest.mark.asyncio
async def test_conda_and_pip_packages_come_in_separate_requests(make_world: Factory) -> None:
    # pip resolves against the Conda packages once they are installed, never beside them.
    world = await make_world()
    refused = await world.provision(
        "op-1", conda=["fpylll"], pip=["fakeflint"], pip_reason="r", expected=400
    )
    assert "separate requests" in refused["error"]["message"]


@pytest.mark.asyncio
async def test_an_install_whose_executor_is_lost_is_looked_at_again(make_world: Factory) -> None:
    # CP-13: an install step that ends without a revision is no proof that nothing changed.
    world = await make_world()
    done = await world.finished((await world.provision("op-1", conda=["lostlink"]))["provision_id"])
    assert (done.outcome, done.reason, done.stage) == (
        "needs_recovery",
        "environment_changed",
        "link",
    )
    assert (done.revision_before, done.revision_after) == (0, 1)
    labels = [(await world.execution(e))["operation_id"].rsplit(".", 1)[1] for e in done.executions]
    assert labels == ["version-conda", "resolve-conda", "install-conda", "install-conda-look"]
    assert (await world.execution(done.executions[2]))["state"] == "lost"
    assert (await world.queue()).status == "needs_recovery"


class Blind(Provisioner):
    """Cannot look at the packages after an install step."""

    async def _look(self, op: Any, label: str) -> int | None:
        return None


@pytest.mark.asyncio
async def test_an_install_whose_effect_is_unseen_needs_recovery(make_world: Factory) -> None:
    # CP-13: what nobody saw may have changed: the environment is marked, never left mixed.
    world = await make_world(kind=Blind)
    done = await world.finished((await world.provision("op-1", conda=["lostlink"]))["provision_id"])
    assert (done.outcome, done.reason, done.stage) == ("needs_recovery", "install_failed", "link")
    assert (done.revision_before, done.revision_after) == (0, None)
    assert any("packages may have changed" in note for note in done.notes)
    assert done.next_step is not None
    assert "may have changed packages" in done.next_step
    assert "revision 0" in done.next_step
    assert (await world.queue()).status == "needs_recovery"


class IndexMoves(Provisioner):
    """Between the dry run and the install, the index changes as ``moves`` says, as when a
    new build is published."""

    moves: Callable[[dict[str, Any]], None] = staticmethod(lambda index: None)

    async def _install(self, op: Any, governing: Any, conda: Any, decided: Any) -> Any:
        path = Path(os.environ["FAKEPM_INDEX"])
        index = json.loads(path.read_text())
        self.moves(index)
        path.write_text(json.dumps(index))
        return await super()._install(op, governing, conda, decided)


class NewBuild(IndexMoves):
    moves = staticmethod(lambda index: index["conda"]["gmp"].update(build="h0_2"))


class AgplDependency(IndexMoves):
    moves = staticmethod(lambda index: index["conda"]["fpylll"].update(depends=["pymupdf"]))


@pytest.mark.asyncio
async def test_the_install_is_pinned_to_the_decided_files(make_world: Factory) -> None:
    world = await make_world(kind=NewBuild)
    done = await world.finished((await world.provision("op-1", conda=["pari"]))["provision_id"])
    # The new gmp build is not the decided one: the pinned solve fails, and nothing changed.
    assert (done.outcome, done.reason, done.stage) == ("failed", "install_failed", "resolve")
    assert done.revision_after == done.revision_before
    pins = [line for line in world.prefix.log.read_text().splitlines() if line.startswith("pin")]
    assert pins == [
        "pinned conda-forge/linux-64::gmp==6.3.0=h0_1,conda-forge/linux-64::pari==2.15.4=h0_0"
    ]


@pytest.mark.asyncio
async def test_an_agpl_package_the_decision_never_saw_needs_recovery(make_world: Factory) -> None:
    # CP-9 refuses before download; were one to come all the same, it is no silent success.
    world = await make_world(kind=AgplDependency)
    done = await world.finished((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    assert (done.outcome, done.reason, done.stage) == ("needs_recovery", "agpl_license", "link")
    assert done.error is not None
    assert "pymupdf" in done.error.message
    assert any("installed beyond the decided set" in note for note in done.notes)
    assert (await world.queue()).status == "needs_recovery"


@pytest.mark.asyncio
async def test_nothing_to_install_unconfirmed_by_a_lockfile_fails(
    make_world: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world()
    first = await world.finished((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    assert first.outcome == "completed"
    monkeypatch.setattr(provisioner_module, "LOCKFILE_WAIT", 0.2)
    monkeypatch.setattr(world.provisioner.revisions, "lockfile_path", lambda *args: None)
    again = await world.finished((await world.provision("op-2", conda=["fpylll"]))["provision_id"])
    assert (again.outcome, again.reason, again.stage) == ("failed", "probe_failed", "probe")
    assert [probe.status for probe in again.probes] == ["error"]


@pytest.mark.asyncio
async def test_the_earliest_recovery_mark_holds(make_world: Factory) -> None:
    world = await make_world()
    first = await world.finished(
        (await world.provision("op-1", conda=["badpostlink"]))["provision_id"]
    )
    assert (first.outcome, first.revision_before, first.revision_after) == ("needs_recovery", 0, 1)
    # A stale/later classification cannot replace the original recovery reference.
    world.store.set_package_status("env1", "needs_recovery", "prv-later", 1)
    assert world.store.package_status("env1") == ("needs_recovery", first.provision_id, 0)
    second = await world.provision("op-2", conda=["otherbad"], expected=503)
    assert first.provision_id in second["error"]["message"]
    assert "revision 0" in second["error"]["message"]


@pytest.mark.asyncio
async def test_an_unanswered_permission_request_times_out_denied(make_world: Factory) -> None:
    world = await make_world(ESCALATE)
    waiting = await world.waiting((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    world.clock.offset = timedelta(minutes=31)
    done = await world.finished(waiting.provision_id)
    assert (done.outcome, done.reason, done.stage) == (
        "denied",
        "permission_timed_out",
        "permission",
    )
    assert [r.kind for r in world.records()].count("denial") == 1
    assert world.cache.artifacts() == []
    assert waiting.permission_request is not None
    assert world.withdrawn(waiting.permission_request)  # a late approval opens nothing


@pytest.mark.asyncio
async def test_a_restarted_worker_ends_what_it_left(make_world: Factory) -> None:
    world = await make_world(ESCALATE)
    waiting = await world.waiting((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    task = world.provisioner._ops[waiting.provision_id].task  # pyright: ignore[reportPrivateUsage]
    assert task is not None
    task.cancel()  # as a worker that stops
    with pytest.raises(asyncio.CancelledError):
        await task
    stopped = world.store.provision(waiting.provision_id)
    assert stopped is not None
    assert (stopped.outcome, stopped.reason, stopped.stage) == (
        "failed",
        "worker_restarted",
        "permission",
    )
    assert [r.kind for r in world.records()].count("denial") == 1
    # A record a crash left unfinished is ended when the worker starts.
    left = stopped.model_copy(
        update={
            "state": ProvisionState.INSTALLING,
            "outcome": None,
            "provision_id": "prv-" + "0" * 24,
            "installer_steps": None,  # explicitly model a legacy crash record
        }
    )
    world.store._db.execute(  # pyright: ignore[reportPrivateUsage]
        "INSERT INTO provisions VALUES (?, ?, ?, ?, 0, 0, ?)",
        (left.provision_id, "p1/left", "x", "env1", left.model_dump_json()),
    )
    # It had recorded a permission request, and crashed before noting it.
    path = "/conda/conda-forge/linux-64/fpylll-0.6.1-py313h0_0.conda"
    key = RequestKey.packages(SANDBOX, world.governed.digest, left.provision_id, [path])
    payload: dict[str, object] = {
        "target": key.payload(),
        "escalation": {"timeout_seconds": 1800, "on_timeout": "deny"},
        "valid_until": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "files": [{"path": path}],
    }
    unnoted = world.sink.append("escalation", "worker", payload).seq
    # Another was installing: its install step had started, and nobody saw it end.
    started = "prv-" + "1" * 24
    step, _ = await world.manager.submit(
        ShellRequest(
            operation_id=f"{started}.install-conda",
            context=ExecutionContext.model_validate(CONTEXT),
            argv=("true",),
        )
    )
    await world.manager.wait(step.execution_id)
    cut = left.model_copy(update={"provision_id": started, "executions": (step.execution_id,)})
    world.store._db.execute(  # pyright: ignore[reportPrivateUsage]
        "INSERT INTO provisions VALUES (?, ?, ?, ?, 0, 0, ?)",
        (cut.provision_id, "p1/cut", "x", "env1", cut.model_dump_json()),
    )
    await world.provisioner.start()
    # Its install step was never submitted: nothing changed.
    ended = world.store.provision(left.provision_id)
    assert ended is not None
    assert (ended.state, ended.outcome, ended.reason, ended.stage) == (
        "finished",
        "failed",
        "worker_restarted",
        "link",
    )
    assert world.withdrawn(unnoted)
    ended = world.store.provision(started)
    assert ended is not None
    assert (ended.outcome, ended.reason, ended.stage) == (
        "needs_recovery",
        "worker_restarted",
        "link",
    )
    assert any("may have changed" in note for note in ended.notes)
    assert (await world.queue()).status == "needs_recovery"


class StoreOutOfReach(Permissions):
    """A permission store whose withdrawals fail until ``reachable`` is set."""

    reachable = False

    def close_operation(self, operation: str, seq: int | None, reason: str, now: Any) -> None:
        if not self.reachable:
            raise PermissionStoreError("the event store is locked")
        super().close_operation(operation, seq, reason, now)


@pytest.mark.asyncio
async def test_a_withdrawal_that_fails_is_owed_and_the_grant_still_ends(
    make_world: Factory,
) -> None:
    world = await make_world(ESCALATE)
    permissions = StoreOutOfReach(world.events)
    world.provisioner.permissions = permissions
    first = await world.waiting((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    assert first.permission_request is not None
    world.answer("approve", first.permission_request)
    done = await world.finished(first.provision_id)
    assert done.outcome == "completed"
    # The withdrawal failed: the request's own deadline still ends the grant.
    assert not world.withdrawn(first.permission_request)
    [request] = [r for r in world.records() if r.seq == first.permission_request]
    until = datetime.fromisoformat(str(request.data()["valid_until"]))
    assert until - datetime.now(UTC) < timedelta(minutes=30 + 60 + 31)
    # The next operation to end pays what is owed, once the store answers again.
    permissions.reachable = True
    second = await world.finished((await world.provision("op-2", system=["x"]))["provision_id"])
    assert second.outcome == "denied"
    assert world.withdrawn(first.permission_request)
    permissions.close()


# The API: idempotency, reserved IDs, bounds, access checks.


@pytest.mark.asyncio
async def test_an_environment_queues_a_bounded_number_of_operations(
    make_world: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provisioner_module, "MAX_QUEUED", 2)
    world = await make_world(ESCALATE)
    await world.waiting((await world.provision("op-1", conda=["fpylll"]))["provision_id"])
    await world.provision("op-2", conda=["gmp"])  # waits behind the first
    refused = await world.provision("op-3", conda=["pari"], expected=503)
    assert refused["error"]["kind"] == "unavailable"
    # Another environment has a queue of its own.
    await world.provision("op-4", conda=["gmp"], context={"environment_id": "env2"})


@pytest.mark.asyncio
async def test_operation_ids_are_idempotent_per_project(make_world: Factory) -> None:
    world = await make_world()
    first = await world.provision("op-1", system=["libgmp-dev"])
    again = await world.provision("op-1", system=["libgmp-dev"], expected=200)
    assert again["provision_id"] == first["provision_id"]
    conflict = await world.provision("op-1", system=["libssl-dev"], expected=409)
    assert conflict["error"]["kind"] == "conflict"
    unknown = await world.provision("op-2", capability="magma", expected=400)
    assert "gp" in unknown["error"]["message"]
    ungoverned = await world.provision(
        "op-3", conda=["x"], context={"environment_id": "env9"}, expected=422
    )
    assert ungoverned["error"]["kind"] == "unsupported"
    bad = await world.provision("op-4", conda=["--channel=bioconda"], expected=400)
    assert bad["error"]["kind"] == "invalid_request"
    reserved = await world.client.post(
        "/v1/executions",
        json={
            "operation_id": f"prv-{'0' * 24}.install-conda",
            "context": CONTEXT,
            "kind": "shell",
            "script": "true",
        },
        headers=AUTH,
    )
    assert reserved.status == 400
    capabilities = await world.client.get(
        "/v1/capabilities", params={"environment_id": "env1"}, headers=AUTH
    )
    assert "provisioning" in (await capabilities.json())["features"]
    other = await world.client.get(
        "/v1/capabilities", params={"environment_id": "env9"}, headers=AUTH
    )
    assert "provisioning" not in (await other.json())["features"]


@pytest.mark.asyncio
async def test_delete_drain_requires_durable_classification_and_retries_sql_failure(
    make_world: Factory,
) -> None:
    entered = asyncio.Event()

    class PausedInstall(Provisioner):
        async def _operate(self, op: Any) -> None:
            self._update(op, state=ProvisionState.INSTALLING, revision_before=0)
            entered.set()
            await asyncio.Event().wait()

    world = await make_world(kind=PausedInstall)
    world.manager.store.bind_package_gate("env1", "original-synthetic-binding")
    submitted = await world.provision("delete-classification", conda=["example"])
    await asyncio.wait_for(entered.wait(), 5)
    store = world.manager.store
    store._db.execute(
        "CREATE TRIGGER fail_classification BEFORE UPDATE ON provisions"
        " BEGIN SELECT RAISE(ABORT,'synthetic classification failure'); END"
    )
    with pytest.raises(WorkerError, match="not durably finished"):
        await world.provisioner.forget("env1")
    assert not store.provisioning_finished("env1")
    assert store.package_gate("env1").mode == "hold"
    with pytest.raises(WorkerError, match="not durably finished"):
        store.forget_package_gate("env1", binding="original-synthetic-binding")
    store._db.execute("DROP TRIGGER fail_classification")
    await world.provisioner.forget("env1")
    assert store.provisioning_finished("env1")
    record = store.provision(submitted["provision_id"])
    assert record is not None
    assert record.state is ProvisionState.FINISHED
    store.forget_package_gate("env1", binding="original-synthetic-binding")
    assert store.package_gate("env1").mode == "ready"


class _Records:
    def __init__(self, infos: dict[str, EnvironmentInfo]) -> None:
        self.infos = infos

    def get(self, environment_id: str) -> EnvironmentInfo | None:
        return self.infos.get(environment_id)


class _Environments:
    """The worker's environments as the API checks them: env1 is p1's, env2 is p2's."""

    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.records = _Records(
            {
                environment_id: EnvironmentInfo(
                    environment_id=environment_id,
                    project_id=project_id,
                    profile="minimal",
                    image="image",
                    sandbox=sandbox,
                    sandbox_id=sandbox,
                    policy="sha256:" + "0" * 64,
                    state=EnvironmentState.READY,
                    created_at=now,
                    updated_at=now,
                )
                for environment_id, project_id, sandbox in (
                    ("env1", "p1", SANDBOX),
                    ("env2", "p2", "sbx-other"),
                )
            }
        )
        self.deleted: list[Any] = []
        self.required_deleted: list[Any] = []
        self.required_deleting: list[Any] = []
        self.confirmed_deleted: list[Any] = []
        self.reusing: list[Any] = []

    def handmade(self, environment_id: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_clients_limited_to_projects_reach_only_their_operations(make_world: Factory) -> None:
    clients = (
        ClientSettings(name="mine", token_sha256=token_digest("m" * 40), projects=("p1",)),
        ClientSettings(name="other", token_sha256=token_digest("o" * 40), projects=("p2",)),
    )
    world = await make_world(environments=_Environments(), clients=clients)
    mine = {"Authorization": f"Bearer {'m' * 40}"}
    other = {"Authorization": f"Bearer {'o' * 40}"}
    body = {
        "operation_id": "op-1",
        "context": CONTEXT,
        "purpose": "a test",
        "system": ["libgmp-dev"],
    }
    response = await world.client.post("/v1/provisioning", json=body, headers=mine)
    assert response.status == 201
    provision_id = (await response.json())["provision_id"]
    await world.finished(provision_id)
    for method, path, expected in (
        ("GET", f"/v1/provisioning/{provision_id}", 404),
        ("POST", f"/v1/provisioning/{provision_id}/cancel", 404),
        ("GET", "/v1/environments/env1/provisioning", 404),
    ):
        response = await world.client.request(method, path, headers=other)
        assert response.status == expected, (method, path)
        response = await world.client.request(method, path, headers=mine)
        assert response.status == 200, (method, path)
    # Naming another project is refused; another project's environment does not exist.
    response = await world.client.post("/v1/provisioning", json=body, headers=other)
    assert (response.status, (await response.json())["error"]["kind"]) == (403, "policy_denied")
    elsewhere = {**body, "operation_id": "op-2", "context": {**CONTEXT, "project_id": "p2"}}
    response = await world.client.post("/v1/provisioning", json=elsewhere, headers=other)
    assert response.status == 404  # env1 is not p2's
    response = await world.client.get(
        f"/v1/provisioning/{provision_id}", params={"project_id": "p2"}, headers=AUTH
    )
    assert response.status == 404
    # Even with the pool's token, naming p2's environment for p1 does not name p2.
    named = {**body, "operation_id": "op-3", "context": {**CONTEXT, "environment_id": "env2"}}
    response = await world.client.post("/v1/provisioning", json=named, headers=AUTH)
    assert response.status == 404
    assert "p2" not in (await response.json())["error"]["message"]
    # The pool's token names the project too, for the queue: env1 is p1's.
    for project_id, expected in (("p2", 404), ("p1", 200)):
        response = await world.client.get(
            "/v1/environments/env1/provisioning", params={"project_id": project_id}, headers=AUTH
        )
        assert response.status == expected, project_id


@pytest.mark.asyncio
async def test_explicit_conda_upgrade_uses_update_records_revision_and_replays_without_effect(
    make_world: Factory,
) -> None:
    world = await make_world()
    first = await world.finished(
        (await world.provision("install-original", conda=["fpylll"]))["provision_id"]
    )
    assert first.outcome is ProvisionOutcome.COMPLETED
    index_path = world.prefix.root.parent / "index.json"
    index = json.loads(index_path.read_text())
    index["conda"]["fpylll"]["version"] = "0.6.2"
    index_path.write_text(json.dumps(index))
    original = await world.provision("upgrade-original", conda=["fpylll"], action="upgrade")
    done = await world.finished(original["provision_id"])
    assert done.request.action == "upgrade"
    assert done.outcome is ProvisionOutcome.COMPLETED
    assert done.revision_before == first.revision_after
    assert done.revision_after == (first.revision_after or 0) + 1
    assert done.revision_after is not None
    assert any(
        change.before == "0.6.1" and change.after == "0.6.2" and change.name == "fpylll"
        for change in done.changes
    )
    history = await world.revisions()
    assert history.revisions[-1].commands[0].argv[:2] == ("micromamba", "update")
    assert [
        p.version for p in (await world.lockfile(done.revision_after)).conda if p.name == "fpylll"
    ] == ["0.6.2"]
    assert done.installer_steps is not None
    assert [s.label for s in done.installer_steps] == [
        "version-conda",
        "resolve-conda",
        "install-conda",
    ]
    assert all(s.log_status == "stored" for s in done.installer_steps)
    before = world.prefix.log.read_bytes()
    replay = await world.provision(
        "upgrade-original", conda=["fpylll"], action="upgrade", expected=200
    )
    assert replay["provision_id"] == done.provision_id
    assert world.prefix.log.read_bytes() == before
    await world.provision("upgrade-original", conda=["fpylll"], expected=409)
    assert world.prefix.log.read_bytes() == before


@pytest.mark.asyncio
async def test_upgrade_of_absent_target_refuses_before_resolve_or_install(
    make_world: Factory,
) -> None:
    world = await make_world()
    done = await world.finished(
        (await world.provision("missing-upgrade", conda=["fpylll"], action="upgrade"))[
            "provision_id"
        ]
    )
    assert done.outcome is ProvisionOutcome.FAILED
    assert done.reason == "resolve_failed"
    assert done.installer_steps is not None
    assert [s.label for s in done.installer_steps] == ["version-conda"]
    assert world.cache.artifacts() == []
    assert not world.prefix.log.exists()


@pytest.mark.asyncio
async def test_upgrade_already_current_records_checks_without_an_install(
    make_world: Factory,
) -> None:
    world = await make_world()
    await world.finished(
        (await world.provision("initial-current", conda=["fpylll"]))["provision_id"]
    )
    before = world.prefix.log.read_bytes()
    done = await world.finished(
        (await world.provision("already-current", conda=["fpylll"], action="upgrade"))[
            "provision_id"
        ]
    )
    assert done.outcome is ProvisionOutcome.ALREADY_SATISFIED
    assert done.revision_before == done.revision_after
    assert done.probes
    assert all(p.kind == "import_check" for p in done.probes)
    assert world.prefix.log.read_bytes() == before


@pytest.mark.asyncio
async def test_upgrade_preserves_agpl_refusal_before_download(make_world: Factory) -> None:
    world = await make_world()
    await world.finished(
        (await world.provision("initial-permitted", conda=["fpylll"]))["provision_id"]
    )
    before = world.prefix.log.read_bytes()
    fetched = len(world.cache.artifacts())
    index_path = world.prefix.root.parent / "index.json"
    index = json.loads(index_path.read_text())
    index["conda"]["fpylll"].update(version="0.6.2", license="AGPL-3.0-only")
    index_path.write_text(json.dumps(index))
    done = await world.finished(
        (await world.provision("refused-upgrade", conda=["fpylll"], action="upgrade"))[
            "provision_id"
        ]
    )
    assert done.outcome is ProvisionOutcome.DENIED
    assert done.reason == "agpl_license"
    assert len(world.cache.artifacts()) == fetched
    assert world.prefix.log.read_bytes() == before


@pytest.mark.parametrize("action", [None, True, "remove", "restore", "upgrade-all"])
@pytest.mark.asyncio
async def test_unknown_upgrade_action_refuses_at_request_boundary(
    make_world: Factory, action: object
) -> None:
    world = await make_world()
    await world.provision("unknown-action", conda=["fpylll"], action=action, expected=400)
    assert world.store._db.execute("SELECT count(*) FROM provisions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_upgrade_escalation_denial_keeps_original_prefix(make_world: Factory) -> None:
    world = await make_world()
    await world.finished(
        (await world.provision("installed-before-denial", conda=["fpylll"]))["provision_id"]
    )
    before = world.prefix.log.read_bytes()
    index_path = world.prefix.root.parent / "index.json"
    index = json.loads(index_path.read_text())
    index["conda"]["fpylll"]["version"] = "0.6.2"
    index_path.write_text(json.dumps(index))
    world.use(ESCALATE)
    operation = await world.provision("upgrade-needs-answer", conda=["fpylll"], action="upgrade")
    pending = await world.waiting(operation["provision_id"])
    assert pending.permission_request is not None
    assert pending.files
    world.answer("deny", pending.permission_request)
    done = await world.finished(operation["provision_id"])
    assert done.outcome is ProvisionOutcome.DENIED
    assert done.reason == "permission_denied"
    assert world.prefix.log.read_bytes() == before


@pytest.mark.asyncio
async def test_cancelled_changed_upgrade_keeps_cp13_recovery_hold(make_world: Factory) -> None:
    world = await make_world()
    first = await world.finished(
        (await world.provision("installed-before-cancel", conda=["fpylll"]))["provision_id"]
    )
    index_path = world.prefix.root.parent / "index.json"
    index = json.loads(index_path.read_text())
    index["conda"]["fpylll"].update(version="0.6.2", postlink="slow")
    index_path.write_text(json.dumps(index))
    operation = await world.provision("upgrade-cancelled", conda=["fpylll"], action="upgrade")
    deadline = time.monotonic() + 10
    while "postlink fpylll" not in world.prefix.log.read_text():
        assert time.monotonic() < deadline, "the update did not link"
        await asyncio.sleep(0.02)
    assert (
        await world.client.post(
            f"/v1/provisioning/{operation['provision_id']}/cancel", headers=AUTH
        )
    ).status == 200
    done = await world.finished(operation["provision_id"])
    assert done.outcome is ProvisionOutcome.NEEDS_RECOVERY
    assert done.reason == "environment_changed"
    assert done.revision_before == first.revision_after
    assert (await world.queue()).status == "needs_recovery"
    before = world.prefix.log.read_bytes()
    refused = await world.provision(
        "no-retry-after-unknown", conda=["fpylll"], action="upgrade", expected=503
    )
    assert refused["error"]["kind"] == "unavailable"
    assert world.prefix.log.read_bytes() == before
