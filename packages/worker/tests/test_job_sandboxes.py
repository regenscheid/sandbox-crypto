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
from collections import Counter
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    SessionCloseRequest,
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
from sandbox_science.worker.errors import PrefixRecoveryError, WorkerError
from sandbox_science.worker.identity import execution_digest, same_execution
from sandbox_science.worker.job_sandboxes import (
    JobFailureError,
    JobSandboxes,
    ManagerPort,
    launch_settings,
    missing_packages,
    offers,
    replay_specs,
)
from sandbox_science.worker.lifecycle import EnvironmentLifecycle
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.prefix_continuity import hold as hold_prefix
from sandbox_science.worker.provisioner import Provisioner
from sandbox_science.worker.settings import (
    JOB_SANDBOX_LABEL,
    ClientSettings,
    EnvironmentSettings,
    JobProfile,
    LocalBackendSettings,
    OpenShellBackendSettings,
    token_digest,
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
        self.located = asyncio.Event()
        """Cleared, the probe of a job sandbox's executor waits: preparation stalls."""
        self.located.set()
        self.opening: Callable[[str], None] = lambda environment_id: None
        """Called as a job sandbox's executor opens, before its probe; may raise."""
        self.gates: dict[str, asyncio.Event] = {}
        """Writes to the session of a path ending so wait for the event."""
        self.gated: list[str] = []
        self.write_counts: Counter[tuple[str, str]] = Counter()
        self._jobs: dict[str, Execution] = {}

    def lose_all(self) -> None:
        """Every job's own execution is lost, as recovery reports one whose daemon went."""
        for execution_id, execution in self._jobs.items():
            self._jobs[execution_id] = execution.model_copy(
                update={"state": ExecutionState.LOST, "result": Result()}
            )
        self.hold.set()

    async def location(self, environment_id: str) -> Location:
        if environment_id != SESSION:
            await self.located.wait()
            self.opening(environment_id)
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

    async def find(self, context: ExecutionContext, operation_id: str) -> Execution | None:
        for execution in self._jobs.values():
            if (execution.operation_id, execution.context) == (operation_id, context):
                if self.hold.is_set():
                    return execution
                return execution.model_copy(
                    update={"state": ExecutionState.RUNNING, "result": None}
                )
        return None

    async def wait(self, execution_id: str) -> Execution:
        await self.hold.wait()
        return self._jobs[execution_id]

    async def cancel(self, execution_id: str) -> None:
        self.cancelled.append(execution_id)
        execution = self._jobs.get(execution_id)
        if execution is not None and execution.state is not ExecutionState.LOST:
            self._jobs[execution_id] = execution.model_copy(
                update={"state": ExecutionState.CANCELLED}
            )

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
        environment = request.context.environment_id
        for suffix, gate in self.gates.items():
            if environment == SESSION and request.path.endswith(suffix):
                self.gated.append(request.path)
                await gate.wait()
        data = base64.b64decode(request.content_b64)
        self.write_counts[environment, request.path] += 1
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
            await self.manager.release(environment_id)

        async def guarding(environment_id: str) -> None:
            await self.manager.guard_package_deletion(environment_id)

        async def deleting(environment_id: str) -> None:
            await self.manager.end_job_sandboxes(environment_id)

        async def forgetting(environment_id: str) -> None:
            await self.manager.forget_package_gate(environment_id)

        # Wired to the current worker, as ``create_app`` and ``build`` wire the real one.
        self.environments.release = release
        self.environments.required_deleting.append(guarding)
        self.environments.required_deleting.append(deleting)
        self.environments.confirmed_deleted.append(forgetting)
        self.environments.given_up.append(lambda found: self.manager.environment_given_up(found))
        self.store = Store(tmp_path / "worker" / "worker.sqlite3")
        self.port = FakePort()
        self.manager, self.sandboxes = self._worker()
        self.policy = self.add(
            granting(tmp_path, job_profiles=["medium", "large"], job_sandboxes=1)
        )

    def _worker(self) -> tuple[ExecutionManager, JobSandboxes]:
        manager = ExecutionManager(
            self.store,
            LocalBackend(LocalBackendSettings(), self.tmp / "worker"),
            Blobs(self.tmp / "worker" / "artifacts"),
            pool_id="test",
            max_artifact_size=1 << 20,
            state_dir=self.tmp / "worker",
            lifecycle=EnvironmentLifecycle(job_resources=self.environments.job_resources),
        )
        sandboxes = JobSandboxes(self.environments, self.store, poll_seconds=0.05)
        sandboxes.port = self.port
        manager.job_sandboxes = sandboxes
        return manager, sandboxes

    async def restart(self, meanwhile: Callable[[], None] = lambda: None) -> None:
        """The worker stops, ``meanwhile`` happens, and a new one starts over the same
        store and sandboxes. The fake port keeps the jobs' own executions, as their
        daemons do, and stands for the recovery that reattaches or loses them."""
        await self.manager.close()
        meanwhile()
        self.manager, self.sandboxes = self._worker()
        await self.manager.start()

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


# Worker restarts (JS-19, JS-20, JS-26) and closing the session (JS-25).


def collected_once(pool: Pool, execution_id: str) -> list[str]:
    """The paths the job's collection wrote to the session, each checked written once."""
    prefix = f".jobs/{execution_id}/"
    written = {
        path: count
        for (environment, path), count in pool.port.write_counts.items()
        if environment == SESSION and path.startswith(prefix) and path != prefix + "manifest.json"
    }
    assert written
    assert set(written.values()) == {1}, written
    return sorted(written)


async def test_a_restart_while_the_job_runs_reattaches_it_and_collects_once(pool: Pool) -> None:
    pool.port.hold.clear()  # the command runs across the restart
    job, _ = await pool.manager.submit(pool.request(inputs=["data"]))
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    await pool.restart()
    # The job is still running after the restart, not lost.
    assert pool.manager.get(job.execution_id).state is ExecutionState.RUNNING
    pool.port.hold.set()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.SUCCEEDED, ended.result
    assert len(pool.port.submitted) == 1  # never submitted again
    paths = collected_once(pool, job.execution_id)
    assert f".jobs/{job.execution_id}/files/out/result.txt" in paths
    status = await pool.manager.job_status(job.execution_id)
    manifest = status.manifest
    assert manifest is not None
    assert manifest.collection == "complete"
    assert sorted(file.path for file in manifest.collected) == paths
    # What preparation found before the restart is in the manifest.
    assert [file.path for file in manifest.inputs] == ["data/input.txt", "data/more/b.bin"]
    assert manifest.isolation_tier == 3
    record = pool.store.job_sandboxes.get(job.execution_id)
    assert record is not None
    assert record.phase == "released"
    assert pool.sandbox_of(job.execution_id) not in pool.fake.read()["sandboxes"]


async def test_a_restart_during_collection_finishes_it_without_duplicates(pool: Pool) -> None:
    pool.port.writes = {"out/a.txt": b"a", "out/b.txt": b"b", "out/c.txt": b"c"}
    gate = pool.port.gates["files/out/b.txt"] = asyncio.Event()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.gated), timeout=15)
    record = pool.store.job_sandboxes.get(job.execution_id)
    assert record is not None
    assert record.phase == "collecting"
    await pool.restart()  # the worker stops with b.txt's copy under way
    gate.set()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.SUCCEEDED, ended.result
    assert len(pool.port.submitted) == 1
    paths = collected_once(pool, job.execution_id)
    files = f".jobs/{job.execution_id}/files/out/"
    assert [path for path in paths if path.startswith(files)] == [
        files + name for name in ("a.txt", "b.txt", "c.txt")
    ]
    manifest = pool.sandboxes.manifest(job.execution_id)
    assert manifest is not None
    assert sorted(file.path for file in manifest.collected) == paths
    assert manifest.collection == "complete"
    assert ended.result is not None
    assert sorted(ended.result.written_files) == paths
    steps = pool.store.job_sandboxes.steps(job.execution_id)
    assert {"prepared", "collecting", "copy-0", "copy-4"} <= set(steps)


async def test_a_restart_after_the_manifest_reports_it_unchanged(pool: Pool) -> None:
    gate = pool.port.gates["manifest.json"] = asyncio.Event()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.gated), timeout=15)
    stored = pool.store.job_sandboxes.manifest(job.execution_id)
    assert stored is not None  # stored before its copy, the release and the job's end
    await pool.restart()
    gate.set()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.SUCCEEDED
    assert pool.store.job_sandboxes.manifest(job.execution_id) == stored
    status = await pool.manager.job_status(job.execution_id)
    assert status.execution.job is not None
    assert status.execution.job.sandbox is not None
    assert status.execution.job.sandbox.manifest_sha256 == stored[1]
    assert status.execution.job.sandbox.phase == "released"
    collected_once(pool, job.execution_id)


async def test_a_sandbox_gone_at_restart_makes_the_job_lost_and_nothing_reruns_it(
    pool: Pool,
) -> None:
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    sandbox = pool.sandbox_of(job.execution_id)

    def gone() -> None:
        state = pool.fake.read()
        del state["sandboxes"][sandbox]  # the pod went while the worker was down
        pool.fake.write(state)
        pool.port.lose_all()  # recovery found the daemon gone with it

    await pool.restart(gone)
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.LOST
    assert ended.result is not None
    assert ended.result.error is not None
    assert ended.result.error.message.startswith("sandbox_lost")
    assert len(pool.port.submitted) == 1  # never run again
    assert len(pool.fake.calls("sandbox", "create")) == 2
    # Nothing is left to keep: the record is released once the gateway confirms the
    # recorded sandbox is gone, and holds none of the project's retention places.
    record = pool.store.job_sandboxes.get(job.execution_id)
    assert record is not None
    assert record.phase == "released"
    manifest = pool.sandboxes.manifest(job.execution_id)
    assert manifest is not None
    assert (manifest.outcome, manifest.reason) == (ExecutionState.LOST, "sandbox_lost")
    assert manifest.collected == ()


async def test_a_daemon_back_without_the_job_makes_it_lost_and_keeps_the_sandbox(
    pool: Pool,
) -> None:
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    # The sandbox stays, but its daemon is a new incarnation that never had the job.
    await pool.restart(pool.port.lose_all)
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.LOST
    assert ended.result is not None
    assert ended.result.error is not None
    assert ended.result.error.message.startswith("sandbox_lost")
    assert len(pool.port.submitted) == 1
    # Loss retains the sandbox, stopped, with what was reached collected (JS-17, JS-25).
    record = pool.store.job_sandboxes.get(job.execution_id)
    assert record is not None
    assert record.phase == "retained"
    assert pool.fake.read()["sandboxes"][pool.sandbox_of(job.execution_id)]["phase"] == "Stopped"
    collected_once(pool, job.execution_id)


async def test_a_restart_before_the_command_fails_the_job_and_releases_its_sandbox(
    pool: Pool,
) -> None:
    pool.port.located.clear()  # preparation stalls in the job sandbox
    job, _ = await pool.manager.submit(pool.request())

    def preparing() -> bool:
        record = pool.store.job_sandboxes.get(job.execution_id)
        return record is not None and record.phase == "preparing"

    await eventually(preparing, timeout=15)
    await pool.restart()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.FAILED
    assert ended.result is not None
    assert ended.result.error is not None
    assert ended.result.error.message.startswith("worker_restarted")
    assert pool.port.submitted == []  # nothing ran, and nothing will
    record = pool.store.job_sandboxes.get(job.execution_id)
    assert record is not None
    assert record.phase == "released"
    assert pool.sandbox_of(job.execution_id) not in pool.fake.read()["sandboxes"]


async def test_a_job_an_older_worker_reported_lost_keeps_its_sandbox(pool: Pool) -> None:
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)

    def lost() -> None:
        execution = pool.store.execution(job.execution_id)
        assert execution is not None
        pool.store.update_execution(execution.model_copy(update={"state": ExecutionState.LOST}))

    await pool.restart(lost)
    record = pool.store.job_sandboxes.get(job.execution_id)
    assert record is not None
    assert record.phase == "retained"
    manifest = pool.sandboxes.manifest(job.execution_id)
    assert manifest is not None
    assert manifest.reason == "worker_restarted"
    assert len(pool.port.submitted) == 1


async def test_closing_the_session_cancels_the_job_and_releases_its_sandbox(pool: Pool) -> None:
    pool.port.hold.clear()
    request = pool.request()
    job, _ = await pool.manager.submit(request)
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    closing = asyncio.create_task(
        pool.manager.close_session(SessionCloseRequest(context=request.context))
    )
    await eventually(lambda: bool(pool.port.cancelled), timeout=15)
    pool.port.hold.set()  # the command ends as cancelled
    await asyncio.wait_for(closing, 30)
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.CANCELLED
    # Collected, then released without retention, though a cancellation retains otherwise.
    collected_once(pool, job.execution_id)
    record = pool.store.job_sandboxes.get(job.execution_id)
    assert record is not None
    assert record.phase == "released"
    assert pool.sandbox_of(job.execution_id) not in pool.fake.read()["sandboxes"]


async def test_another_sessions_close_leaves_the_job_alone(pool: Pool) -> None:
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    other = ExecutionContext(project_id="p1", session_id="s2", environment_id=SESSION)
    await pool.manager.close_session(SessionCloseRequest(context=other))
    assert pool.port.cancelled == []
    assert "release" not in pool.store.job_sandboxes.steps(job.execution_id)
    # Nor is a job of another project marked for release.
    execution = pool.manager.get(job.execution_id)
    foreign = execution.model_copy(
        update={"context": execution.context.model_copy(update={"project_id": "p2"})}
    )
    assert not pool.sandboxes.release_on_close(foreign)
    pool.port.hold.set()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.SUCCEEDED


def deleted_ending(pool: Pool, ended: Execution) -> None:
    """A job its session environment's deletion ended: cancelled, never lost, nothing
    copied back into the environment that went, its sandbox released, and the manifest
    saying why."""
    assert ended.state is ExecutionState.CANCELLED, ended.result
    assert ended.result is not None
    assert any("session environment was deleted" in notice for notice in ended.result.notices)
    prefix = f".jobs/{ended.execution_id}/"
    copied = [path for (env, path) in pool.port.write_counts if env == SESSION and prefix in path]
    assert copied == []
    assert len(pool.port.submitted) == 1  # never run again
    record = pool.store.job_sandboxes.get(ended.execution_id)
    assert record is not None
    assert record.phase == "released"
    assert pool.sandbox_of(ended.execution_id) not in pool.fake.read()["sandboxes"]
    manifest = pool.sandboxes.manifest(ended.execution_id)
    assert manifest is not None
    assert manifest.outcome is ExecutionState.CANCELLED
    assert manifest.collection == "incomplete"
    assert any("deleted" in file.reason for file in manifest.not_collected)


async def test_deleting_the_session_environment_ends_the_job_and_releases_its_sandbox(
    pool: Pool,
) -> None:
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    deleting = asyncio.create_task(pool.environments.delete(SESSION))
    await eventually(lambda: bool(pool.port.cancelled), timeout=15)
    pool.port.hold.set()  # the command ends as cancelled
    deleted = await asyncio.wait_for(deleting, 30)
    assert deleted.state is EnvironmentState.DELETED
    # The deletion waited for the job, which ended as it does, not as lost.
    ended = pool.manager.get(job.execution_id)
    deleted_ending(pool, ended)
    assert pool.store.job_sandboxes.steps(job.execution_id)["release"] == '"environment_deleted"'


async def test_deleting_the_session_environment_releases_its_retained_sandboxes(
    pool: Pool,
) -> None:
    pool.port.exit_code = 1  # a failure retains its sandbox
    ended = await pool.run(pool.request())
    assert ended.state is ExecutionState.FAILED
    record = pool.store.job_sandboxes.get(ended.execution_id)
    assert record is not None
    assert record.phase == "retained"
    collected_once(pool, ended.execution_id)  # collected before the deletion, and kept
    await pool.environments.delete(SESSION)
    record = pool.store.job_sandboxes.get(ended.execution_id)
    assert record is not None
    assert record.phase == "released"
    assert pool.sandbox_of(ended.execution_id) not in pool.fake.read()["sandboxes"]
    # The job's outcome and manifest are not changed by the release.
    assert pool.manager.get(ended.execution_id).state is ExecutionState.FAILED
    manifest = pool.sandboxes.manifest(ended.execution_id)
    assert manifest is not None
    assert manifest.outcome is ExecutionState.FAILED


async def test_deleting_another_environment_leaves_the_job_alone(pool: Pool) -> None:
    await pool.environments.create(
        EnvironmentRequest(
            environment_id="env-2", project_id="p1", profile="minimal", policy=pool.policy
        )
    )
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    await pool.environments.delete("env-2")
    assert pool.port.cancelled == []
    assert "release" not in pool.store.job_sandboxes.steps(job.execution_id)
    pool.port.hold.set()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.SUCCEEDED
    collected_once(pool, job.execution_id)


async def test_a_session_environment_given_up_ends_its_job_sandboxes(pool: Pool) -> None:
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    session = pool.environments.records.get(SESSION)
    assert session is not None
    state = pool.fake.read()
    del state["sandboxes"][session.sandbox]  # the session's own pod went
    pool.fake.write(state)
    with pytest.raises(WorkerError, match="create the environment again"):
        await pool.environments.resume(SESSION)
    await eventually(lambda: bool(pool.port.cancelled), timeout=15)
    pool.port.hold.set()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    deleted_ending(pool, ended)


async def test_a_session_environment_deleted_while_the_worker_was_down(pool: Pool) -> None:
    pool.port.exit_code = 1
    retained = await pool.run(pool.request("failed"))
    pool.port.exit_code = 0
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: len(pool.port.submitted) == 2, timeout=15)

    def deleted() -> None:
        # As the offline reconciliation command, or a deletion this worker did not see
        # through, leaves it: the session environment's record says deleted.
        info = pool.environments.records.get(SESSION)
        assert info is not None
        pool.environments.records.save(info.model_copy(update={"state": EnvironmentState.DELETED}))

    await pool.restart(deleted)
    await eventually(lambda: len(pool.port.cancelled) == 1, timeout=15)
    pool.port.hold.set()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.CANCELLED
    assert len(pool.port.submitted) == 2
    record = pool.store.job_sandboxes.get(job.execution_id)
    assert record is not None
    assert record.phase == "released"
    manifest = pool.sandboxes.manifest(job.execution_id)
    assert manifest is not None
    assert any("deleted" in file.reason for file in manifest.not_collected)
    # The earlier job's retained sandbox has nothing to be reached through: released.
    kept = pool.store.job_sandboxes.get(retained.execution_id)
    assert kept is not None
    assert kept.phase == "released"
    assert pool.sandbox_of(retained.execution_id) not in pool.fake.read()["sandboxes"]


# A job sandbox is disposable: the package guard never keeps it (JS-25, JS-26).


def prefix_missing(pool: Pool) -> Callable[[str], None]:
    """The executor's preflight as it fails for an image without the workspace prefix the
    profile requires: the job environment's package gate is held, then the open fails."""

    def opening(environment_id: str) -> None:
        hold_prefix(pool.store, environment_id)
        raise PrefixRecoveryError(
            ErrorKind.UNAVAILABLE, "required workspace prefix is missing or incompatible"
        )

    return opening


async def test_a_job_sandbox_without_its_workspace_prefix_fails_and_is_released(
    pool: Pool,
) -> None:
    pool.port.opening = prefix_missing(pool)
    ended = await pool.run(pool.request())
    assert ended.state is ExecutionState.FAILED
    assert ended.result is not None
    assert ended.result.error is not None
    message = ended.result.error.message
    assert message.startswith("sandbox_unavailable"), message
    assert "workspace prefix" in message
    assert pool.port.submitted == []  # nothing ran
    record = pool.store.job_sandboxes.get(ended.execution_id)
    assert record is not None
    assert record.phase == "released"
    assert pool.sandbox_of(ended.execution_id) not in pool.fake.read()["sandboxes"]
    manifest = pool.sandboxes.manifest(ended.execution_id)
    assert manifest is not None
    assert manifest.reason == "sandbox_unavailable"
    # Its package gate went with it: nothing is held for an environment that is gone.
    assert pool.store.package_gate(pool.job_environment(ended.execution_id)).mode == "ready"


async def test_the_sweep_releases_a_job_sandbox_whose_package_gate_is_held(
    pool: Pool,
) -> None:
    pool.port.opening = prefix_missing(pool)
    pool.fake.update(fail_delete=True)  # the first deletion fails: retained, due at once
    ended = await pool.run(pool.request())
    assert ended.state is ExecutionState.FAILED
    record = pool.store.job_sandboxes.get(ended.execution_id)
    assert record is not None
    assert record.phase == "retained"
    pool.fake.update(fail_delete=False)
    assert await pool.sandboxes.sweep() == [ended.execution_id]
    assert pool.sandbox_of(ended.execution_id) not in pool.fake.read()["sandboxes"]


async def test_a_held_package_gate_does_not_keep_a_retained_job_sandbox(pool: Pool) -> None:
    pool.port.exit_code = 1  # a failure retains its sandbox, stopped
    ended = await pool.run(pool.request())
    environment = pool.job_environment(ended.execution_id)
    hold_prefix(pool.store, environment)  # its package state needs recovery
    await pool.manager.guard_package_deletion(environment)  # a job sandbox's: no refusal
    released = await pool.sandboxes.release_now(ended.execution_id, "click-1")
    assert released.execution_id == ended.execution_id
    assert pool.sandbox_of(ended.execution_id) not in pool.fake.read()["sandboxes"]


async def test_retention_never_stops_a_sandbox_that_reused_the_name(pool: Pool) -> None:
    pool.port.hold.clear()
    pool.port.exit_code = 1  # the job fails: its sandbox would be stopped and retained
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    sandbox = pool.sandbox_of(job.execution_id)
    state = pool.fake.read()
    state["sandboxes"][sandbox]["id"] = "someone-elses"  # the name now another's
    pool.fake.write(state)
    pool.port.hold.set()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.FAILED
    assert not [call for call in pool.fake.calls("sandbox", "stop") if call[2] == sandbox]
    assert not [call for call in pool.fake.calls("sandbox", "delete") if call[2] == sandbox]
    assert pool.fake.read()["sandboxes"][sandbox]["phase"] == "Ready"
    with pytest.raises(WorkerError, match="refusing to stop another sandbox"):
        await pool.environments.stop_job(pool.job_environment(job.execution_id))


async def test_a_held_package_gate_still_keeps_a_session_environment(pool: Pool) -> None:
    hold_prefix(pool.store, SESSION)
    with pytest.raises(WorkerError, match="no proven original sandbox binding"):
        await pool.manager.guard_package_deletion(SESSION)
    with pytest.raises(WorkerError):
        await pool.environments.delete(SESSION)
    session = pool.environments.records.get(SESSION)
    assert session is not None
    assert session.sandbox in pool.fake.read()["sandboxes"]


async def test_the_workers_port_finds_an_operation_without_submitting_it(pool: Pool) -> None:
    port = ManagerPort(pool.manager, cast(Provisioner, None))
    context = ExecutionContext(project_id="p1", session_id="s1", environment_id=SESSION)
    assert await port.find(context, "plain") is None
    assert pool.store.find_operation("p1", "plain") is None  # nothing was submitted
    job, _ = await pool.manager.submit(pool.request("plain", script="true", profile=None))
    found = await port.find(context, "plain")
    assert found is not None
    assert found.execution_id == job.execution_id
    elsewhere = context.model_copy(update={"environment_id": "env-2"})
    assert await port.find(elsewhere, "plain") is None
    assert await port.find(context.model_copy(update={"project_id": "p2"}), "plain") is None
    await asyncio.wait_for(pool.manager.wait(job.execution_id), 60)


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


# Releasing a retained sandbox now (JS-24).


async def retained(pool: Pool, operation: str = "fail") -> str:
    """A failed job, whose sandbox the worker stopped and kept."""
    pool.port.exit_code = 1
    ended = await pool.run(pool.request(operation))
    assert ended.state is ExecutionState.FAILED
    record = pool.store.job_sandboxes.get(ended.execution_id)
    assert record is not None
    assert record.phase == "retained"
    return ended.execution_id


async def test_release_now_deletes_a_retained_sandbox_and_answers_a_repeat(pool: Pool) -> None:
    job = await retained(pool)
    box = pool.sandbox_of(job)
    assert pool.fake.read()["sandboxes"][box]["phase"] == "Stopped"
    released = await pool.sandboxes.release_now(job, "click-1")
    assert (released.execution_id, released.phase) == (job, "released")
    record = pool.store.job_sandboxes.get(job)
    assert record is not None
    assert record.phase == "released"
    assert box not in pool.fake.read()["sandboxes"]
    # The manifest stays what it was; the job's status says released.
    status = await pool.manager.job_status(job)
    assert status.execution.job is not None
    assert status.execution.job.sandbox is not None
    assert status.execution.job.sandbox.phase == "released"
    assert status.manifest is not None
    # The same request again is the first answer; another one finds nothing to release.
    assert await pool.sandboxes.release_now(job, "click-1") == released
    with pytest.raises(WorkerError, match="only a retained one") as refused:
        await pool.sandboxes.release_now(job, "click-2")
    assert refused.value.kind is ErrorKind.CONFLICT


async def test_release_now_refuses_what_is_not_retained(pool: Pool) -> None:
    pool.port.hold.clear()
    job, _ = await pool.manager.submit(pool.request())
    await eventually(lambda: bool(pool.port.submitted), timeout=15)
    with pytest.raises(WorkerError) as running:
        await pool.sandboxes.release_now(job.execution_id, "click-1")
    assert running.value.kind is ErrorKind.CONFLICT
    assert pool.sandbox_of(job.execution_id) in pool.fake.read()["sandboxes"]
    with pytest.raises(WorkerError) as unknown:
        await pool.sandboxes.release_now("exe-none", "click-1")
    assert unknown.value.kind is ErrorKind.NOT_FOUND
    pool.port.hold.set()
    ended = await asyncio.wait_for(pool.manager.wait(job.execution_id), 30)
    assert ended.state is ExecutionState.SUCCEEDED  # released by itself, after collection
    with pytest.raises(WorkerError) as gone:
        await pool.sandboxes.release_now(job.execution_id, "click-1")
    assert gone.value.kind is ErrorKind.CONFLICT


async def test_an_unconfirmed_deletion_leaves_the_sandbox_retained_for_the_sweep(
    pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await retained(pool)

    async def refuse(_environment_id: str) -> None:
        raise WorkerError(ErrorKind.UNAVAILABLE, "gateway unreachable")

    with monkeypatch.context() as patch:
        patch.setattr(pool.environments, "delete", refuse)
        with pytest.raises(WorkerError) as failed:
            await pool.sandboxes.release_now(job, "click-1")
    assert failed.value.kind is ErrorKind.UNAVAILABLE
    record = pool.store.job_sandboxes.get(job)
    assert record is not None
    assert record.phase == "retained"
    assert "release-now" not in pool.store.job_sandboxes.steps(job)  # no receipt yet
    assert await pool.sandboxes.sweep() == [job]  # due at once, so the sweep finishes it
    record = pool.store.job_sandboxes.get(job)
    assert record is not None
    assert record.phase == "released"


def operator_app(pool: Pool) -> Any:
    clients = [
        ClientSettings(
            name=name, token_sha256=token_digest(f"test-secret-release-{name}-0000"),
            projects=("p1",), operator=name == "workspace",
        )
        for name in ("workspace", "harness")
    ]  # fmt: skip
    return create_app(pool.manager, TOKEN, pool.environments, clients)


def release_headers(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer test-secret-release-{name}-0000"}


async def test_the_release_route_is_the_operators_and_the_sessions_own(
    pool: Pool, aiohttp_client: ClientFactory
) -> None:
    job = await retained(pool)
    client: TestClient[Any, Any] = await aiohttp_client(operator_app(pool))
    path = f"/v1/job-sandboxes/{job}/release"
    context = {"project_id": "p1", "session_id": "s1", "environment_id": SESSION}
    body = {"context": context, "operation_id": "click-1"}
    # The run service's own client may not release a sandbox, though it owns the job.
    denied = await client.post(path, json=body, headers=release_headers("harness"))
    assert denied.status == 403
    assert pool.store.job_sandboxes.get(job) is not None
    assert pool.sandbox_of(job) in pool.fake.read()["sandboxes"]
    # Another session's context, another project's, and a job that does not exist.
    for other, status in (
        ({**context, "session_id": "s2"}, 404),
        ({**context, "project_id": "p2"}, 403),
        ({**context, "environment_id": "env-2"}, 404),
    ):
        response = await client.post(
            path, json={**body, "context": other}, headers=release_headers("workspace")
        )
        assert response.status == status, other
    missing = await client.post(
        "/v1/job-sandboxes/exe-none/release", json=body, headers=release_headers("workspace")
    )
    assert missing.status == 404
    assert pool.sandbox_of(job) in pool.fake.read()["sandboxes"]
    # The session's own, by the operator.
    response = await client.post(path, json=body, headers=release_headers("workspace"))
    assert response.status == 200
    assert await response.json() == {"execution_id": job, "phase": "released"}
    assert pool.sandbox_of(job) not in pool.fake.read()["sandboxes"]
    repeat = await client.post(path, json=body, headers=release_headers("workspace"))
    assert repeat.status == 200
    again = await client.post(
        path, json={**body, "operation_id": "click-2"}, headers=release_headers("workspace")
    )
    assert again.status == 409
    invalid = await client.post(
        path, json={"context": context}, headers=release_headers("workspace")
    )
    assert invalid.status == 400


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
