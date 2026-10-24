# SPDX-License-Identifier: MIT
"""Job sandboxes (D93), against the fake ``openshell`` and a fake of the executors' side.

The sandbox's lifecycle (records, creation, placement, stop, deletion, loss) runs against
the fake OpenShell CLI of ``test_environments``. What needs an executor inside a sandbox
(the isolation probe, files, package replay, the command) goes through ``FakePort``, the
``JobPort`` a test controls; the worker's own port runs them through the execution
manager and provisioner. Live Kata acceptance is separate (JS-30).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import tomllib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from pydantic import ValidationError

from sandbox_science.contracts import (
    TERMINAL_STATES,
    Capabilities,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
    Execution,
    ExecutionContext,
    ExecutionState,
    FileContent,
    FileEntry,
    FileListing,
    JobDetails,
    JobRequest,
    ListFilesRequest,
    Location,
    Lockfile,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionRequest,
    ProvisionState,
    ReadFileRequest,
    Result,
    WriteFileRequest,
    WriteFileResult,
    new_id,
)
from sandbox_science.contracts.revisions import CondaPackage, PipDistribution
from sandbox_science.contracts.worker import PROTOCOL_VERSION
from sandbox_science.policy import effective_resources, load_ceiling, load_policy
from sandbox_science.policy.model import Resources
from sandbox_science.testing import eventually
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.environments import JOB_ENVIRONMENT, Environments
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.identity import execution_digest, same_execution
from sandbox_science.worker.job_sandboxes import (
    JobFailureError,
    JobSandboxes,
    launch_settings,
    missing_packages,
    offers,
    replay_specs,
)
from sandbox_science.worker.lifecycle import EnvironmentLifecycle
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import (
    JOB_SANDBOX_LABEL,
    EnvironmentSettings,
    JobProfile,
    LocalBackendSettings,
    OpenShellBackendSettings,
)
from sandbox_science.worker.store import Store

from .test_environments import EXAMPLE, IMAGE, TARGETS, TOKEN, ClientFactory, Fake

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[3]
SESSION = "env-1"


def profile(**changes: Any) -> JobProfile:
    values: dict[str, Any] = {
        "description": "6 cores, 16 GiB memory, up to 7 days",
        "node_selector": {"kubernetes.io/arch": "amd64"},
        "runtime_class_name": "kata",
        "requests": {"cpu_millicores": 6000, "memory_mib": 16384, "ephemeral_storage_mib": 4096},
        "limits": {"cpu_millicores": 6000, "memory_mib": 16384, "ephemeral_storage_mib": 4096},
    }
    return JobProfile.model_validate({**values, **changes})


def granting(tmp_path: Path, name: str = "granted.yaml", **resources: object) -> Path:
    """The example policy, granting job sandboxes as ``resources`` say."""
    text = EXAMPLE.read_text()
    marker = "resources:                  # capped by the pool ceiling (docs/POLICY.md)\n"
    assert marker in text
    lines = "".join(f"  {key}: {json.dumps(value)}\n" for key, value in resources.items())
    path = tmp_path / name
    path.write_text(text.replace(marker, marker + lines, 1))
    return path


def lockfile(conda: dict[str, str] | None = None, pip: dict[str, str] | None = None) -> Lockfile:
    return Lockfile(
        python="3.13.1",
        platform="linux-64",
        conda=tuple(
            CondaPackage(name=name, version=version, build="h0_0", channel="conda-forge")
            for name, version in sorted({"python": "3.13.1", **(conda or {})}.items())
        ),
        pip=tuple(
            PipDistribution(name=name, version=version)
            for name, version in sorted((pip or {}).items())
        ),
    )


class FakePort:
    """Executors as a test wants them: files, tiers, lockfiles, provisioning, the command."""

    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}
        self.links: set[tuple[str, str]] = set()
        self.tiers: dict[str, int] = {}
        self.submitted: list[JobRequest] = []
        self.cancelled: list[str] = []
        self.provisions: list[ProvisionRequest] = []
        self.provision_outcome = ProvisionOutcome.COMPLETED
        self.replay_installs = True
        self.revisions: dict[str, int] = {}
        self.lockfiles: dict[tuple[str, int], Lockfile] = {}
        self.writes: dict[str, bytes] = {"out/result.txt": b"42\n"}
        self.exit_code = 0
        self.hold = asyncio.Event()
        self.hold.set()
        self._jobs: dict[str, Execution] = {}

    async def location(self, environment_id: str) -> Location:
        tier = self.tiers.get(environment_id, 3)
        return Location(
            pool_id="test",
            backend="openshell",
            sandbox_id=environment_id,
            hostname="sandbox",
            isolation_tier=tier,
            declared_isolation_tier=3,
        )

    async def submit(self, request: JobRequest) -> Execution:
        self.submitted.append(request)
        environment = request.context.environment_id
        execution_id = new_id("exe")
        details = JobDetails(
            purpose=request.purpose,
            expected_progress=request.expected_progress,
            timeout_seconds=request.timeout_seconds or 60,
            stdout_path=f".jobs/{execution_id}/stdout.log",
            stderr_path=f".jobs/{execution_id}/stderr.log",
        )
        self.files[environment, details.stdout_path] = b"working\ndone\n"
        self.files[environment, details.stderr_path] = b""
        for path, data in self.writes.items():
            self.files[environment, path] = data
        now = datetime.now(UTC)
        execution = Execution(
            execution_id=execution_id,
            attempt_id=new_id("att"),
            operation_id=request.operation_id,
            kind="job",
            context=request.context,
            state=ExecutionState.SUCCEEDED if self.exit_code == 0 else ExecutionState.FAILED,
            submitted_at=now,
            started_at=now,
            ended_at=now,
            result=Result(
                exit_code=self.exit_code,
                written_files=(details.stdout_path, details.stderr_path, *self.writes),
            ),
            job=details,
        )
        self._jobs[execution_id] = execution
        return execution.model_copy(update={"state": ExecutionState.QUEUED, "result": None})

    async def wait(self, execution_id: str) -> Execution:
        await self.hold.wait()
        return self._jobs[execution_id]

    async def cancel(self, execution_id: str) -> None:
        self.cancelled.append(execution_id)

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        key = (request.context.environment_id, request.path)
        if key in self.links or key not in self.files:
            raise WorkerError(ErrorKind.NOT_FOUND, f"no file {request.path}")
        data = self.files[key]
        part = data[request.offset : request.offset + request.length]
        return FileContent(
            path=request.path,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            offset=request.offset,
            content_b64=base64.b64encode(part).decode(),
            eof=request.offset + len(part) >= len(data),
        )

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        data = base64.b64decode(request.content_b64)
        self.files[request.context.environment_id, request.path] = data
        return WriteFileResult(sha256=hashlib.sha256(data).hexdigest(), size=len(data))

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        environment, base = request.context.environment_id, request.path
        prefix = "" if base == "." else base + "/"
        entries: dict[str, FileEntry] = {}
        now = datetime.now(UTC)
        for env, path in [*self.files, *self.links]:
            if env != environment or not path.startswith(prefix):
                continue
            name, _, rest = path[len(prefix) :].partition("/")
            kind = "directory" if rest else "symlink" if (env, path) in self.links else "file"
            entries[name] = FileEntry(name=name, type=kind, size=0, modified_at=now)
        if not entries and base != ".":
            raise WorkerError(ErrorKind.NOT_FOUND, f"no directory {base}")
        return FileListing(path=base, entries=tuple(entries[name] for name in sorted(entries)))

    async def provision(self, request: ProvisionRequest) -> ProvisionOperation:
        self.provisions.append(request)
        environment = request.context.environment_id
        if self.provision_outcome is ProvisionOutcome.COMPLETED and self.replay_installs:
            # A new job sandbox has its image's packages: the session's at revision 0.
            current = self.lockfiles.get(
                (environment, self.revisions.get(environment, 0))
            ) or self.lockfiles.get((SESSION, 0))
            assert current is not None
            conda = {p.name: p.version for p in current.conda}
            pip = {p.name: p.version for p in current.pip}
            for spec in request.conda:
                name, version, _ = spec.split("::")[1].split("=")
                conda[name] = version
            for spec in request.pip:
                name, version = spec.split("==")
                pip[name] = version
            revision = self.revisions.get(environment, 0) + 1
            self.revisions[environment] = revision
            self.lockfiles[environment, revision] = lockfile(conda, pip)
        now = datetime.now(UTC)
        return ProvisionOperation(
            provision_id=new_id("prv"),
            request=request,
            state=ProvisionState.FINISHED,
            outcome=self.provision_outcome,
            reason=None
            if self.provision_outcome is ProvisionOutcome.COMPLETED
            else "policy_denied",
            submitted_at=now,
            ended_at=now,
        )

    async def revision(self, environment_id: str) -> int | None:
        return self.revisions.get(environment_id)

    async def lockfile(
        self, environment_id: str, revision: int | None
    ) -> tuple[Lockfile, str] | None:
        if revision is None:
            revision = self.revisions.get(environment_id, 0)
        found = self.lockfiles.get((environment_id, revision))
        if found is None:
            return None
        return found, hashlib.sha256(found.model_dump_json().encode()).hexdigest()


class Pool:
    """A worker's environments, store, manager and job sandboxes over the fake CLI."""

    def __init__(self, tmp_path: Path, **settings: Any) -> None:
        self.tmp = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.fake = Fake(tmp_path)
        ceiling = tmp_path / "pool-ceiling.yaml"
        ceiling.write_text("apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n")
        profiles = settings.pop(
            "job_profiles",
            {"medium": profile(), "large": profile(description="12 cores, 32 GiB memory")},
        )
        self.backend = OpenShellBackendSettings(
            cli=str(self.fake.cli),
            isolation_tier=3,
            environments=EnvironmentSettings(
                policy_store=tmp_path / "middleware",
                targets=TARGETS,
                ceiling=ceiling,
                profiles={"minimal": IMAGE},
                job_profiles=profiles,
                **settings,
            ),
        )
        assert self.backend.environments is not None
        env = {"FAKE_OPENSHELL_STATE": str(self.fake.path), "PATH": os.environ["PATH"]}
        self.environments = Environments(
            self.backend, self.backend.environments, tmp_path / "worker", env, poll_seconds=0.05
        )
        self.released: list[str] = []

        async def release(environment_id: str) -> None:
            self.released.append(environment_id)

        self.environments.release = release
        self.store = Store(tmp_path / "worker" / "worker.sqlite3")
        self.manager = ExecutionManager(
            self.store,
            LocalBackend(LocalBackendSettings(), tmp_path / "worker"),
            Blobs(tmp_path / "worker" / "artifacts"),
            pool_id="test",
            max_artifact_size=1 << 20,
            state_dir=tmp_path / "worker",
            lifecycle=EnvironmentLifecycle(job_resources=self.environments.job_resources),
        )
        self.sandboxes = JobSandboxes(self.environments, self.store, poll_seconds=0.05)
        self.port = FakePort()
        self.sandboxes.port = self.port
        self.manager.job_sandboxes = self.sandboxes
        self.policy = self.add(
            granting(tmp_path, job_profiles=["medium", "large"], job_sandboxes=1)
        )

    def add(self, path: Path) -> str:
        return self.environments.store.add_policy(path.read_bytes()).digest

    async def session(self, policy: str | None = None) -> None:
        await self.environments.create(
            EnvironmentRequest(
                environment_id=SESSION,
                project_id="p1",
                profile="minimal",
                policy=policy or self.policy,
            )
        )
        self.port.files[SESSION, "data/input.txt"] = b"input\n"
        self.port.files[SESSION, "data/more/b.bin"] = b"\x00\x01"

    def request(self, operation: str = "big", **fields: Any) -> JobRequest:
        values: dict[str, Any] = {
            "operation_id": operation,
            "context": ExecutionContext(project_id="p1", session_id="s1", environment_id=SESSION),
            "script": "python big.py",
            "purpose": "a big search",
            "expected_progress": "a line a minute",
            "profile": "medium",
        }
        return JobRequest.model_validate({**values, **fields})

    async def run(self, request: JobRequest) -> Execution:
        execution, _ = await self.manager.submit(request)
        return await asyncio.wait_for(self.manager.wait(execution.execution_id), 30)

    def job_environment(self, execution_id: str) -> str:
        record = self.store.job_sandboxes.get(execution_id)
        assert record is not None
        return record.environment_id

    def sandbox_of(self, execution_id: str) -> str:
        info = self.environments.records.get(self.job_environment(execution_id))
        assert info is not None
        return info.sandbox

    async def close(self) -> None:
        await self.manager.close()
        self.environments.close()
        self.store.close()


@pytest_asyncio.fixture
async def pool(tmp_path: Path) -> AsyncIterator[Pool]:
    pool = Pool(tmp_path)
    await pool.session()
    yield pool
    await pool.close()


# Profiles, settings and the drain label.


def test_job_profiles_need_kata_and_bounded_times() -> None:
    for name in ("kata", "kata-qemu", "kata-clh"):
        assert profile(runtime_class_name=name).runtime_class_name == name
    for refused in ({"runtime_class_name": None}, {"runtime_class_name": "runc"},
                    {"runtime_class_name": "gvisor"}, {"runtime_class_name": "katana"},
                    {"wall_time_max": 7 * 24 * 3600 + 1}, {"schedule_timeout": 24 * 3600 + 1},
                    {"node_selector": {}}, {"pod_json": {"volumes": []}}):  # fmt: skip
        with pytest.raises(ValidationError):
            profile(**refused)
    assert profile().schedule_timeout == 1800
    assert profile().wall_time_max == 7 * 24 * 3600
    assert profile().collect_max_bytes == 4 * 1024**3


def test_the_example_offers_the_owners_sizes() -> None:
    raw = tomllib.loads((ROOT / "examples/kubernetes/job-profiles.toml").read_text())
    environments = raw["openshell"]["environments"]
    profiles = {
        name: JobProfile.model_validate(value)
        for name, value in environments["job_profiles"].items()
    }
    assert {
        name: (p.limits.cpu_millicores, p.limits.memory_mib) for name, p in profiles.items()
    } == {
        "medium": (6000, 16384),
        "large": (12000, 32768),
    }
    assert {p.runtime_class_name for p in profiles.values()} == {"kata"}
    assert environments["max_job_sandboxes"] == 2
    gateway = tomllib.loads((ROOT / "examples/kubernetes/gateway-driver.toml").read_text())
    allowed = gateway["openshell"]["drivers"]["kubernetes"]["allowed_pod_labels"]
    assert JOB_SANDBOX_LABEL[0] in allowed


def test_the_driver_config_carries_the_drain_label_and_the_profiles_own_size() -> None:
    document = json.loads(launch_settings(profile()))["kubernetes"]
    pod = document["pod"]
    assert pod["labels"] == {JOB_SANDBOX_LABEL[0]: JOB_SANDBOX_LABEL[1]}
    assert pod["runtime_class_name"] == "kata"
    # The session environment's CPU and memory caps do not narrow a job profile (JS-9).
    assert document["containers"]["agent"]["resources"]["limits"]["cpu"] == "6000m"
    # Labels are the one key beyond OpenShell 0.1.5's typed driver config that the worker
    # emits: allowed_pod_labels admits them (qualification item, docs/WORKER.md).
    assert set(pod) == {"node_selector", "runtime_class_name", "labels"}


def test_the_policy_grants_profiles_and_counts_within_the_ceiling(tmp_path: Path) -> None:
    ceiling_file = tmp_path / "ceiling.yaml"
    ceiling_file.write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
        "resources:\n  job_profiles: [medium]\n  job_sandboxes: 1\n"
    )
    ceiling = load_ceiling(ceiling_file)
    granted = load_policy(granting(tmp_path, job_profiles=["medium", "large"], job_sandboxes=3))
    resources, notices = effective_resources(granted.policy, ceiling)
    assert resources.job_profiles == ("medium",)
    assert resources.job_sandboxes == 1
    assert any("large" in notice for notice in notices)
    plain = load_policy(EXAMPLE)
    resources, _ = effective_resources(plain.policy, ceiling)
    assert resources.job_profiles is None
    assert resources.job_sandboxes is None  # off until the project grants it
    offered = offers(
        EnvironmentSettings(
            policy_store=tmp_path,
            targets=tmp_path,
            ceiling=tmp_path,
            profiles={"minimal": IMAGE},
            job_profiles={"medium": profile()},
        ),
        Resources(job_profiles=("medium", "unknown"), job_sandboxes=1),
    )
    assert [offer.name for offer in offered] == ["medium"]
    assert offered[0].cpu_millicores == 6000


def test_a_profile_job_request_is_checked_and_an_old_one_keeps_its_digest() -> None:
    context = ExecutionContext(project_id="p1", session_id="s1", environment_id=SESSION)
    base = {
        "operation_id": "op",
        "context": context,
        "script": "true",
        "purpose": "p",
        "expected_progress": "e",
    }
    for refused in (
        {"inputs": ["a.txt"]},
        {"min_isolation_tier": 3},
        {"profile": "medium", "inputs": ["a"] * 2},
        {"profile": "medium", "inputs": [".jobs/x"]},
        {"profile": "medium", "inputs": ["."]},
        {"profile": "medium", "inputs": [f"f{i}" for i in range(65)]},
        {"profile": "medium", "min_isolation_tier": 4},
    ):
        with pytest.raises(ValidationError):
            JobRequest.model_validate({**base, **refused})
    old = JobRequest.model_validate(base)
    # A request without the new fields has the digest it had before protocol 13.
    assert "profile" not in old.model_dump(exclude_defaults=True)
    assert same_execution(execution_digest(old), old)
    sized = JobRequest.model_validate({**base, "profile": "medium"})
    assert execution_digest(sized) != execution_digest(old)


def test_replay_specs_pin_the_sessions_changes() -> None:
    base = lockfile({"pari": "2.15"})
    current = lockfile({"pari": "2.17", "g6k": "0.1.2"}, {"fpylll": "0.6.1"})
    conda, pip = replay_specs(current, base)
    assert conda == ["conda-forge::g6k=0.1.2=h0_0", "conda-forge::pari=2.17=h0_0"]
    assert pip == ["fpylll==0.6.1"]
    assert missing_packages(current, base, current) == []
    assert missing_packages(current, base, base) == ["g6k 0.1.2", "pari 2.17", "fpylll 0.6.1 (pip)"]
    with pytest.raises(JobFailureError, match="removed packages"):
        replay_specs(lockfile(), base)
    url = Lockfile(
        conda=base.conda,
        pip=(PipDistribution(name="tool", version="1", url="https://example.org/tool.whl"),),
    )
    with pytest.raises(JobFailureError, match="URL"):
        replay_specs(url, base)


# Admission.


async def test_refusals_create_nothing(pool: Pool, tmp_path: Path) -> None:
    with pytest.raises(WorkerError, match=r"not granted.*granted: medium, large") as refused:
        await pool.manager.submit(pool.request("unknown", profile="huge"))
    assert refused.value.kind is ErrorKind.POLICY_DENIED
    pool.port.hold.clear()
    first, _ = await pool.manager.submit(pool.request("first"))
    with pytest.raises(
        WorkerError, match=f"project's job sandbox limit of 1.*{first.execution_id}"
    ):
        await pool.manager.submit(pool.request("second", profile="large"))
    pool.port.hold.set()
    await pool.manager.wait(first.execution_id)
    creates = pool.fake.calls("sandbox", "create")
    assert len(creates) == 2  # the session's and the first job's

    other = Pool(tmp_path / "ungranted")
    try:
        await other.session(other.add(EXAMPLE))
        with pytest.raises(WorkerError, match=r"not granted.*\(granted: none\)"):
            await other.manager.submit(other.request())
        assert len(other.fake.calls("sandbox", "create")) == 1
        other.manager.job_sandboxes = None
        with pytest.raises(WorkerError, match="offers no job sandboxes"):
            await other.manager.submit(other.request("again"))
    finally:
        await other.close()


async def test_the_pool_limit_counts_every_project(tmp_path: Path) -> None:
    pool = Pool(tmp_path, max_job_sandboxes=1)
    try:
        await pool.session()
        await pool.environments.create(
            EnvironmentRequest(
                environment_id="env-2", project_id="p2", profile="minimal", policy=pool.policy
            )
        )
        pool.port.hold.clear()
        first, _ = await pool.manager.submit(pool.request())
        context = ExecutionContext(project_id="p2", session_id="s1", environment_id="env-2")
        with pytest.raises(WorkerError, match="pool's job sandbox limit of 1"):
            await pool.manager.submit(pool.request("other", context=context))
        pool.port.hold.set()
        await pool.manager.wait(first.execution_id)
    finally:
        await pool.close()


# A job's run.


async def test_a_job_runs_in_its_own_sandbox_from_the_sessions_image(pool: Pool) -> None:
    ended = await pool.run(pool.request(inputs=["data"]))
    assert ended.state is ExecutionState.SUCCEEDED, ended.result
    # The job keeps the session's identity (JS-2); its own execution is the sandbox's.
    assert ended.context.environment_id == SESSION
    job_environment = pool.job_environment(ended.execution_id)
    assert JOB_ENVIRONMENT.fullmatch(job_environment)
    (inner,) = pool.port.submitted
    assert inner.context.environment_id == job_environment
    assert inner.context.session_id == "s1"
    assert inner.profile is None
    assert inner.script == "python big.py"
    # Created through OpenShell from the session's recorded image, policy and providers.
    session = pool.environments.get(SESSION)
    info = pool.environments.get(job_environment)
    assert (info.kind, info.job_id) == ("job", ended.execution_id)
    assert (info.image, info.policy, info.providers) == (
        session.image,
        session.policy,
        session.providers,
    )
    create = pool.fake.calls("sandbox", "create")[-1]
    assert create[create.index("--from") + 1] == IMAGE
    launch = json.loads(create[create.index("--driver-config-json") + 1])
    assert launch["kubernetes"]["pod"]["labels"] == {JOB_SANDBOX_LABEL[0]: "true"}
    assert launch["kubernetes"]["pod"]["runtime_class_name"] == "kata"
    assert [create[i + 1] for i, arg in enumerate(create) if arg == "--provider"] == list(
        session.providers
    )
    # Inputs went to the same paths; results came back under .jobs/<ID>/.
    assert pool.port.files[job_environment, "data/input.txt"] == b"input\n"
    assert pool.port.files[job_environment, "data/more/b.bin"] == b"\x00\x01"
    files = f".jobs/{ended.execution_id}/files/out/result.txt"
    assert pool.port.files[SESSION, files] == b"42\n"
    assert pool.port.files[SESSION, f".jobs/{ended.execution_id}/stdout.log"] == b"working\ndone\n"
    assert ended.result is not None
    assert files in ended.result.written_files
    # The manifest is stored once and returned with its digest; its copy is untrusted.
    status = await pool.manager.job_status(ended.execution_id)
    assert status.manifest is not None
    assert status.execution.job is not None
    sandbox = status.execution.job.sandbox
    assert sandbox is not None
    assert sandbox.phase == "released"
    assert sandbox.manifest_sha256 is not None
    assert [file.path for file in status.manifest.inputs] == ["data/input.txt", "data/more/b.bin"]
    assert status.manifest.collection == "complete"
    assert status.manifest.isolation_tier == 3
    copy = json.loads(pool.port.files[SESSION, f".jobs/{ended.execution_id}/manifest.json"])
    assert copy["job_id"] == ended.execution_id
    # Released at once after a complete success, by its recorded identity.
    assert info.sandbox not in pool.fake.read()["sandboxes"]
    assert pool.environments.get(job_environment).state is EnvironmentState.DELETED
    # A retry is the same job, never a second sandbox.
    again, created = await pool.manager.submit(pool.request(inputs=["data"]))
    assert not created
    assert again.execution_id == ended.execution_id
    assert len(pool.fake.calls("sandbox", "create")) == 2


async def test_a_manifest_is_never_rewritten(pool: Pool) -> None:
    ended = await pool.run(pool.request())
    found = pool.store.job_sandboxes.manifest(ended.execution_id)
    assert found is not None
    manifest, digest = found
    assert pool.store.job_sandboxes.save_manifest(manifest, datetime.now(UTC)) == digest
    with pytest.raises(WorkerError, match="recorded already"):
        pool.store.job_sandboxes.save_manifest(
            manifest.model_copy(update={"exit_code": 1}), datetime.now(UTC)
        )
    db = pool.store.job_sandboxes.db
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute("UPDATE job_manifests SET manifest='{}'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute("DELETE FROM job_manifests")


async def test_the_sessions_packages_are_replayed_at_their_versions(pool: Pool) -> None:
    pool.port.revisions[SESSION] = 2
    pool.port.lockfiles[SESSION, 0] = lockfile()
    pool.port.lockfiles[SESSION, 2] = lockfile({"g6k": "0.1.2"}, {"fpylll": "0.6.1"})
    ended = await pool.run(pool.request())
    assert ended.state is ExecutionState.SUCCEEDED, ended.result
    conda, pip = pool.port.provisions
    job_environment = pool.job_environment(ended.execution_id)
    assert conda.context.environment_id == job_environment
    assert conda.conda == ("conda-forge::g6k=0.1.2=h0_0",)
    assert pip.pip == ("fpylll==0.6.1",)
    assert pip.pip_reason
    status = await pool.manager.job_status(ended.execution_id)
    assert status.manifest is not None
    assert status.manifest.packages.session_revision == 2
    assert len(status.manifest.packages.operations) == 2
    assert ended.result is not None
    assert any("replayed 1 Conda and 1 pip" in notice for notice in ended.result.notices)


@pytest.mark.parametrize(
    ("outcome", "installs", "reason"),
    [
        (ProvisionOutcome.DENIED, True, "package_replay_failed"),
        (ProvisionOutcome.COMPLETED, False, "environment_mismatch"),
    ],
)
async def test_a_replay_failure_fails_the_job_before_its_command(
    pool: Pool, outcome: ProvisionOutcome, installs: bool, reason: str
) -> None:
    pool.port.revisions[SESSION] = 1
    pool.port.lockfiles[SESSION, 0] = lockfile()
    pool.port.lockfiles[SESSION, 1] = lockfile({"g6k": "0.1.2"})
    pool.port.provision_outcome = outcome
    pool.port.replay_installs = installs
    ended = await pool.run(pool.request())
    assert ended.state is ExecutionState.FAILED
    assert ended.result is not None
    assert ended.result.error is not None
    assert ended.result.error.message.startswith(reason)
    assert pool.port.submitted == []  # the command never ran
    # A failure keeps the sandbox, stopped, for inspection (JS-25).
    record = pool.store.job_sandboxes.get(ended.execution_id)
    assert record is not None
    assert record.phase == "retained"
    assert pool.fake.read()["sandboxes"][pool.sandbox_of(ended.execution_id)]["phase"] == "Stopped"


async def test_a_tier_below_the_sessions_fails_before_the_command(pool: Pool) -> None:
    pool.port.tiers[SESSION] = 3
    job, _ = await pool.manager.submit(pool.request())
    environment = pool.job_environment(job.execution_id)
    pool.port.tiers[environment] = 1
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.FAILED
    assert ended.result is not None
    assert ended.result.error is not None
    assert ended.result.error.message.startswith("isolation_insufficient")
    assert ended.result.error.kind is ErrorKind.POLICY_DENIED
    assert pool.port.submitted == []
    assert pool.sandbox_of(job.execution_id) not in pool.fake.read()["sandboxes"]  # released


async def test_the_requests_minimum_tier_applies_too(pool: Pool) -> None:
    job, _ = await pool.manager.submit(pool.request(min_isolation_tier=3))
    pool.port.tiers[pool.job_environment(job.execution_id)] = 2
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.result is not None
    assert ended.result.error is not None
    assert "isolation_insufficient" in ended.result.error.message


async def test_copy_back_keeps_to_its_cap_and_an_incomplete_one_retains(tmp_path: Path) -> None:
    pool = Pool(tmp_path, job_profiles={"medium": profile(collect_max_bytes=64)})
    try:
        await pool.session()
        pool.port.writes = {"small.txt": b"ok", "big.bin": b"x" * 1000}
        ended = await pool.run(pool.request())
        assert ended.state is ExecutionState.SUCCEEDED
        status = await pool.manager.job_status(ended.execution_id)
        manifest = status.manifest
        assert manifest is not None
        assert manifest.collection == "incomplete"
        assert [item.path for item in manifest.not_collected] == ["big.bin"]
        assert "remaining byte limit" in manifest.not_collected[0].reason
        assert (SESSION, f".jobs/{ended.execution_id}/files/small.txt") in pool.port.files
        assert (SESSION, f".jobs/{ended.execution_id}/files/big.bin") not in pool.port.files
        assert ended.result is not None
        assert ended.result.written_files_truncated
        record = pool.store.job_sandboxes.get(ended.execution_id)
        assert record is not None
        assert record.phase == "retained"
    finally:
        await pool.close()


async def test_an_unplaced_sandbox_fails_after_its_schedule_timeout(tmp_path: Path) -> None:
    pool = Pool(tmp_path, job_profiles={"medium": profile(schedule_timeout=0.3)})
    try:
        await pool.session()
        pool.fake.update(phase_on_create="Provisioning")  # pending: no node has room
        ended = await pool.run(pool.request())
        assert ended.state is ExecutionState.FAILED
        assert ended.result is not None
        assert ended.result.error is not None
        assert ended.result.error.message.startswith("capacity_unavailable")
        assert pool.port.submitted == []
        environment = pool.job_environment(ended.execution_id)
        assert pool.environments.get(environment).state is EnvironmentState.DELETED
        assert pool.fake.calls("sandbox", "delete")  # deleted by its recorded identity
        assert len(pool.fake.read()["sandboxes"]) == 1  # the session's only
        record = pool.store.job_sandboxes.get(ended.execution_id)
        assert record is not None
        assert record.phase == "released"
    finally:
        await pool.close()


async def test_failures_are_retained_two_a_project_then_released(tmp_path: Path) -> None:
    pool = Pool(tmp_path, job_retention=0.5)
    try:
        await pool.session()
        pool.port.exit_code = 1
        ended = [await pool.run(pool.request(f"fail-{n}")) for n in range(3)]
        assert {job.state for job in ended} == {ExecutionState.FAILED}
        phases = [pool.store.job_sandboxes.get(job.execution_id) for job in ended]
        assert [record.phase if record else None for record in phases] == [
            "released",
            "retained",
            "retained",
        ]
        boxes = pool.fake.read()["sandboxes"]
        assert pool.sandbox_of(ended[0].execution_id) not in boxes  # the oldest went early
        for job in ended[1:]:
            assert boxes[pool.sandbox_of(job.execution_id)]["phase"] == "Stopped"
        status = await pool.manager.job_status(ended[2].execution_id)
        assert status.execution.job is not None
        assert status.execution.job.sandbox is not None
        assert status.execution.job.sandbox.retained_until is not None
        await asyncio.sleep(0.6)
        released = await pool.sandboxes.sweep()
        assert sorted(released) == sorted(job.execution_id for job in ended[1:])
        assert len(pool.fake.read()["sandboxes"]) == 1
    finally:
        await pool.close()


async def test_a_lost_sandbox_makes_the_job_lost_and_nothing_reruns_it(pool: Pool) -> None:
    pool.port.hold.clear()  # the command runs until its sandbox goes
    job, _ = await pool.manager.submit(pool.request())

    def running() -> bool:
        return bool(pool.port.submitted)

    await eventually(running, timeout=15)
    sandbox = pool.sandbox_of(job.execution_id)
    state = pool.fake.read()
    del state["sandboxes"][sandbox]  # a node drain or failure took the pod
    pool.fake.write(state)
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.LOST
    assert ended.result is not None
    assert ended.result.error is not None
    assert ended.result.error.message.startswith("sandbox_lost")
    assert len(pool.port.submitted) == 1  # never submitted again
    assert pool.port.cancelled  # the lost attempt was told to stop, in case it still runs
    record = pool.store.job_sandboxes.get(job.execution_id)
    assert record is not None
    assert record.phase == "released"
    assert len(pool.fake.calls("sandbox", "create")) == 2


async def test_cancelling_a_running_job_cancels_its_own_execution(pool: Pool) -> None:
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    cancelling = await pool.manager.cancel(job.execution_id)
    assert cancelling.state is ExecutionState.CANCELLING
    assert pool.port.cancelled
    pool.port.hold.set()  # the fake's command ends as it would, and is collected
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state in TERMINAL_STATES


async def test_after_a_restart_an_unfinished_job_is_lost_and_its_sandbox_kept(
    tmp_path: Path,
) -> None:
    pool = Pool(tmp_path)
    await pool.session()
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    await pool.manager.close()  # the worker stops; the job's outcome is unknown to it
    assert pool.manager.get(job.execution_id).state is ExecutionState.LOST
    restarted = JobSandboxes(pool.environments, pool.store, poll_seconds=0.05)
    restarted.port = pool.port
    await restarted.start()
    try:
        record = pool.store.job_sandboxes.get(job.execution_id)
        assert record is not None
        assert record.phase == "retained"
        manifest = restarted.manifest(job.execution_id)
        assert manifest is not None
        assert manifest.reason == "worker_restarted"
        assert len(pool.port.submitted) == 1
    finally:
        await restarted.close()
        pool.environments.close()
        pool.store.close()


async def test_a_job_without_a_profile_runs_in_the_session_as_before(pool: Pool) -> None:
    request = pool.request("plain", script="echo plain", profile=None)
    job, _ = await pool.manager.submit(request)
    assert job.job is not None
    assert job.job.profile is None
    assert job.job.sandbox is None
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 60)
    assert ended.state is ExecutionState.SUCCEEDED, ended.result
    assert pool.store.job_sandboxes.get(job.execution_id) is None
    assert pool.port.submitted == []
    assert len(pool.fake.calls("sandbox", "create")) == 1


# Clients never see job sandboxes (JS-1).


async def test_no_client_names_a_job_sandbox(pool: Pool, aiohttp_client: ClientFactory) -> None:
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    environment = pool.job_environment(job.execution_id)
    client: TestClient[Any, Any] = await aiohttp_client(
        create_app(pool.manager, TOKEN, pool.environments)
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = await client.get(f"/v1/environments/{environment}", headers=headers)
    assert response.status == 404
    listed = await (await client.get("/v1/environments", headers=headers)).json()
    assert [info["environment_id"] for info in listed["environments"]] == [SESSION]
    context = {"project_id": "p1", "session_id": "s1", "environment_id": environment}
    response = await client.post(
        "/v1/executions",
        json={"kind": "shell", "operation_id": "probe", "context": context, "script": "true"},
        headers=headers,
    )
    assert response.status == 404
    inner = pool.port.submitted[0]
    reserved = {
        "kind": "shell",
        "operation_id": inner.operation_id,
        "context": {**context, "environment_id": SESSION},
        "script": "true",
    }
    response = await client.post("/v1/executions", json=reserved, headers=headers)
    assert response.status == 400
    response = await client.post(
        "/v1/environments",
        json={"environment_id": environment.replace(environment[-12:], "0" * 12),
              "project_id": "p1", "profile": "minimal", "policy": pool.policy},
        headers=headers,
    )  # fmt: skip
    assert response.status == 400
    pool.port.hold.set()
    await pool.manager.wait(job.execution_id)


async def test_capabilities_offer_the_granted_profiles(
    pool: Pool, aiohttp_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def capabilities(_environment: str) -> Capabilities:
        return Capabilities(
            protocol=PROTOCOL_VERSION, languages=(), location=await pool.port.location(SESSION)
        )

    monkeypatch.setattr(pool.manager, "capabilities", capabilities)
    client: TestClient[Any, Any] = await aiohttp_client(
        create_app(pool.manager, TOKEN, pool.environments)
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = await client.get(f"/v1/capabilities?environment_id={SESSION}", headers=headers)
    body = await response.json()
    assert "job_sandboxes" in body["features"]
    assert [offer["name"] for offer in body["job_profiles"]] == ["medium", "large"]
