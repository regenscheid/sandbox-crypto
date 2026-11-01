# SPDX-License-Identifier: MIT
"""Child environments (docs/specs/child-environments.md), against the fake ``openshell``
and the fake executors' side of ``test_job_sandboxes``.

The sandbox's lifecycle (records, creation, placement, stop, deletion) runs against the
fake OpenShell CLI of ``test_environments``; what needs an executor in a sandbox (the
isolation probe, files, package replay) goes through ``SizedPort``, a ``FakePort`` whose
listings carry file sizes. The orchestrator's side is in the orchestrator's tests.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from pydantic import ValidationError

from sandbox_science.contracts import (
    Capabilities,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
    ExecutionContext,
    FileContent,
    FileListing,
    IsolationEvidence,
    ListFilesRequest,
    Location,
    ProvisionOperation,
    ProvisionRequest,
    ReadFileRequest,
    SessionCloseRequest,
    WriteFileRequest,
    WriteFileResult,
)
from sandbox_science.contracts.child_environments import (
    ChildEnvironment,
    ChildEnvironmentEnd,
    ChildEnvironmentRequest,
    ChildPhase,
    HandBackRequest,
    InputPart,
    RetainedFile,
    RetainedInput,
    is_child_environment,
)
from sandbox_science.contracts.context import ExecutionAuthority
from sandbox_science.contracts.recovery import RecoveryFence
from sandbox_science.contracts.worker import PROTOCOL_VERSION
from sandbox_science.policy import effective_resources, load_ceiling, load_policy
from sandbox_science.testing import eventually
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.build import build_worker
from sandbox_science.worker.child_environments import ChildEnvironments
from sandbox_science.worker.child_records import ACTIVE, ChildRecord
from sandbox_science.worker.cli import main
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.files import RangeWritten
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.monitor import MonitorDesk
from sandbox_science.worker.recovery_fence import OPERATOR, RecoveryFences, operator_scope
from sandbox_science.worker.settings import (
    ChildProfile,
    EnvironmentSettings,
    LocalBackendSettings,
    OpenShellBackendSettings,
    Settings,
)
from sandbox_science.worker.store import Store

from .test_environments import EXAMPLE, IMAGE, TARGETS, TOKEN, ClientFactory, Fake
from .test_job_sandboxes import SESSION, FakePort, granting, lockfile

pytestmark = pytest.mark.slow

DELEGATION = "dlg-" + "a" * 24
CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id=SESSION)


def child_profile(**changes: Any) -> ChildProfile:
    values: dict[str, Any] = {
        "description": "4 cores, 8 GiB memory",
        "isolation_tier": 3,
        "node_selector": {"kubernetes.io/arch": "amd64"},
        "runtime_class_name": "kata",
        "requests": {"cpu_millicores": 1000, "memory_mib": 1024, "ephemeral_storage_mib": 1024},
        "limits": {"cpu_millicores": 4000, "memory_mib": 8192, "ephemeral_storage_mib": 4096},
    }
    return ChildProfile.model_validate({**values, **changes})


class SizedPort(FakePort):
    """A ``FakePort`` whose listings carry each file's size, and whose probe gives its
    evidence, as the worker's do; ``unprobed`` environments report none. With ``fences``,
    every call that names a context passes the worker's recovery fence first, as the
    execution manager's do."""

    def __init__(self) -> None:
        super().__init__()
        self.unprobed: set[str] = set()
        self.fences: RecoveryFences | None = None

    def _fenced(self, context: ExecutionContext) -> None:
        if self.fences is not None:
            self.fences.check(context, operator=OPERATOR.get())

    async def read_file(self, request: ReadFileRequest) -> FileContent:
        self._fenced(request.context)
        return await super().read_file(request)

    async def write_file(self, request: WriteFileRequest) -> WriteFileResult:
        self._fenced(request.context)
        return await super().write_file(request)

    async def read_part(
        self, context: ExecutionContext, path: str, offset: int, length: int
    ) -> tuple[bytes, int]:
        self._fenced(context)
        return await super().read_part(context, path, offset, length)

    async def write_range(
        self,
        context: ExecutionContext,
        path: str,
        upload: str,
        offset: int,
        content: bytes,
        *,
        sha256: str | None = None,
        size: int | None = None,
        discard: bool = False,
    ) -> RangeWritten:
        self._fenced(context)
        return await super().write_range(
            context, path, upload, offset, content, sha256=sha256, size=size, discard=discard
        )

    async def provision(self, request: ProvisionRequest) -> ProvisionOperation:
        self._fenced(request.context)
        return await super().provision(request)

    async def location(self, environment_id: str) -> Location:
        found = await super().location(environment_id)
        if environment_id in self.unprobed:
            return found
        evidence = IsolationEvidence(kernel="separate", sentinel="hidden")
        return found.model_copy(update={"isolation_evidence": evidence})

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        self._fenced(request.context)
        listing = await super().list_files(request)
        environment = request.context.environment_id
        prefix = "" if request.path == "." else request.path + "/"
        entries = tuple(
            entry.model_copy(
                update={"size": len(self.files.get((environment, prefix + entry.name), b""))}
            )
            if entry.type == "file"
            else entry
            for entry in listing.entries
        )
        return listing.model_copy(update={"entries": entries})


class Pool:
    """A worker's environments, store and child environments over the fake CLI."""

    def __init__(self, tmp_path: Path, **settings: Any) -> None:
        self.tmp = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.fake = Fake(tmp_path)
        ceiling = tmp_path / "pool-ceiling.yaml"
        ceiling.write_text("apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n")
        profiles = settings.pop("child_profiles", {"standard": child_profile()})
        self.backend = OpenShellBackendSettings(
            cli=str(self.fake.cli),
            isolation_tier=3,
            environments=EnvironmentSettings(
                policy_store=tmp_path / "middleware",
                targets=TARGETS,
                ceiling=ceiling,
                profiles={"minimal": IMAGE},
                child_profiles=profiles,
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
        self.port = SizedPort()
        self.port.fences = self.store.recovery_fences
        self.children = self._children()
        self.policy = self.add(granting(tmp_path))

    def _children(self) -> ChildEnvironments:
        children = ChildEnvironments(self.environments, self.store)
        children.port = self.port
        return children

    async def restart(self) -> None:
        """The worker stops and a new one starts over the same store and sandboxes."""
        await self.children.close()
        self.children = self._children()
        await self.children.start()

    def add(self, path: Path) -> str:
        return self.environments.store.add_policy(path.read_bytes()).digest

    async def session(self, policy: str | None = None) -> None:
        await self.environments.create(
            EnvironmentRequest(
                environment_id=SESSION, project_id="p1", profile="minimal",
                policy=policy or self.policy,
            )
        )  # fmt: skip
        self.port.files[SESSION, "data/input.txt"] = b"input\n"
        self.port.files[SESSION, "data/more/b.bin"] = b"\x00\x01"

    def request(self, delegation: str = DELEGATION, **fields: Any) -> ChildEnvironmentRequest:
        values: dict[str, Any] = {"context": CONTEXT, "delegation_id": delegation}
        return ChildEnvironmentRequest.model_validate({**values, **fields})

    async def ready(self, delegation: str = DELEGATION, **fields: Any) -> ChildEnvironment:
        made, created = await self.children.admit(self.request(delegation, **fields))
        assert created
        return await self.settled(made.environment_id)

    async def settled(self, environment_id: str) -> ChildEnvironment:
        def done() -> bool:
            return self.children.get(environment_id, CONTEXT).phase not in (
                "creating",
                "preparing",
            )

        await eventually(done, timeout=20)
        return self.children.get(environment_id, CONTEXT)

    def end(self, reason: str = "delegation_ended") -> ChildEnvironmentEnd:
        return ChildEnvironmentEnd.model_validate({"context": CONTEXT, "reason": reason})

    def back(self, files: tuple[str, ...], **fields: Any) -> HandBackRequest:
        values: dict[str, Any] = {"context": CONTEXT, "delegation_id": DELEGATION, "files": files}
        return HandBackRequest.model_validate({**values, **fields})

    async def close(self) -> None:
        await self.children.close()
        self.environments.close()
        self.store.close()


@pytest_asyncio.fixture
async def pool(tmp_path: Path) -> AsyncIterator[Pool]:
    pool = Pool(tmp_path)
    await pool.session()
    yield pool
    await pool.close()


# Settings and policy (CE-3, CE-9).


def test_child_profiles_name_their_tier_and_bounded_times() -> None:
    assert child_profile().schedule_timeout == 1800
    assert child_profile().wall_time_max == 7 * 24 * 3600
    assert child_profile(isolation_tier=1, runtime_class_name=None).isolation_tier == 1
    for refused in ({"isolation_tier": 3, "runtime_class_name": None}, {"isolation_tier": 0},
                    {"wall_time_max": 7 * 24 * 3600 + 1}, {"schedule_timeout": 24 * 3600 + 1},
                    {"node_selector": {}}):  # fmt: skip
        with pytest.raises(ValidationError):
            child_profile(**refused)
    with pytest.raises(ValidationError, match="default_child_profile"):
        EnvironmentSettings(
            policy_store=Path("p"), targets=Path("t"), ceiling=Path("c"),
            profiles={"minimal": IMAGE}, child_profiles={"a": child_profile()},
            default_child_profile="b",
        )  # fmt: skip
    one = EnvironmentSettings(
        policy_store=Path("p"), targets=Path("t"), ceiling=Path("c"),
        profiles={"minimal": IMAGE}, child_profiles={"a": child_profile()},
    )  # fmt: skip
    assert (one.child_default, one.max_child_environments, one.child_retention) == (
        "a",
        2,
        3 * 24 * 3600,
    )


def test_the_policy_caps_child_environments_within_the_ceiling(tmp_path: Path) -> None:
    ceiling_file = tmp_path / "ceiling.yaml"
    ceiling_file.write_text(
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
        "resources:\n  child_environments_max: 1\n"
    )
    ceiling = load_ceiling(ceiling_file)
    resources, notices = effective_resources(
        load_policy(granting(tmp_path, child_environments_max=3)).policy, ceiling
    )
    assert resources.child_environments_max == 1
    assert any("child_environments_max" in notice for notice in notices)
    plain = effective_resources(load_policy(EXAMPLE).policy, ceiling)[0]
    assert plain.child_environments_max == 1  # the ceiling's, when the project sets none


# Admission, creation and preparation (CE-1, CE-3, CE-7, CE-10).


async def test_a_child_environment_is_made_from_the_session_environment(pool: Pool) -> None:
    pool.port.revisions[SESSION] = 1
    pool.port.lockfiles[SESSION, 0] = lockfile()
    pool.port.lockfiles[SESSION, 1] = lockfile({"sympy": "1.13"}, {"galois": "0.4.2"})
    made = await pool.ready(inputs=("data", "data/input.txt", "notes about the method"))
    assert is_child_environment(made.environment_id)
    assert made.environment_id.startswith("p1-child-")
    assert (made.phase, made.failure, made.isolation_tier) == ("ready", None, 3)
    session = pool.environments.get(SESSION)
    info = pool.environments.get(made.environment_id)
    assert (info.kind, info.delegation_id, info.session_id) == ("child", DELEGATION, "s1")
    assert (info.image, info.policy, info.providers) == (
        session.image,
        session.policy,
        session.providers,
    )
    assert info.state is EnvironmentState.READY
    # The session's packages are replayed before it is ready (CE-1).
    assert made.packages.conda
    assert made.packages.pip
    assert {r.operation_id.split(".")[0] for r in pool.port.provisions} == {
        "chd-" + made.environment_id[-12:]
    }
    # Inputs are copied to inputs/; text that names no file is listed as missing (CE-7).
    assert [f.path for f in made.inputs] == [
        "inputs/data/input.txt",
        "inputs/data/more/b.bin",
    ]
    assert made.inputs_missing == ("notes about the method",)
    assert pool.port.files[made.environment_id, "inputs/data/input.txt"] == b"input\n"
    # Child environments do not count against the session environments (CE-9).
    assert pool.environments.records.live(["p1"]) == [SESSION]
    # The same delegation again finds the same one.
    again, created = await pool.children.admit(pool.request())
    assert (again.environment_id, created) == (made.environment_id, False)


def retained(path: str, data: bytes, revision: int = 1) -> RetainedInput:
    return RetainedInput(
        path=path,
        artifact_id="artifact-" + hashlib.sha256(path.encode()).hexdigest()[:8],
        revision=revision,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


def part(index: int, offset: int, data: bytes) -> InputPart:
    return InputPart(
        context=CONTEXT,
        index=index,
        offset=offset,
        content_b64=base64.b64encode(data).decode(),
    )


async def test_inputs_from_controls_retained_content_are_planned_and_staged(
    pool: Pool,
) -> None:
    """CE-7: an input whose content control retains is planned at admission, counted in
    the staging bound, and staged part by part once ready, its digest checked; the
    session's file wins where it exists, and a missing path one gives is not missing."""
    report = b"# Report r2\n" * 1000
    figure = b"\x89PNG" + bytes(5000)
    items = (
        retained("data/input.txt", b"older input\n"),  # the session's file is copied
        retained("results/report.md", report, revision=2),
        retained("shared/fig.png", figure),
    )
    made = await pool.ready(inputs=("data/input.txt", "results/report.md"), retained=items)
    assert made.phase == "ready"
    assert made.retained == items[1:]
    assert made.inputs_missing == ()  # results/report.md comes from control
    assert [f.path for f in made.inputs] == ["inputs/data/input.txt"]
    child = made.environment_id
    # Parts of the first one, out of order: the worker says where its bytes end.
    first = await pool.children.stage_input(child, part(0, 0, report[:5000]))
    assert (first.size, first.accepted, first.staged) == (5000, True, None)
    skipped = await pool.children.stage_input(child, part(0, 9000, report[9000:]))
    assert (skipped.size, skipped.accepted) == (5000, False)
    done = await pool.children.stage_input(child, part(0, 5000, report[5000:]))
    assert done.staged is not None
    assert done.staged.path == "inputs/results/report.md"
    assert done.staged.sha256 == items[1].sha256
    assert pool.port.files[child, "inputs/results/report.md"] == report
    # Asked again, the staged input answers the same.
    again = await pool.children.stage_input(child, part(0, 0, b""))
    assert again.staged == done.staged
    # Content that is not the planned digest is refused, and its parts discarded.
    with pytest.raises(WorkerError) as caught:
        await pool.children.stage_input(child, part(1, 0, b"\x00" * len(figure)))
    assert caught.value.kind is ErrorKind.CONFLICT
    assert (child, "inputs/shared/fig.png") not in pool.port.files
    assert not pool.port.parts
    # A part past the input's size, or of an input not planned, is refused.
    with pytest.raises(WorkerError, match="may not end"):
        await pool.children.stage_input(child, part(1, 0, figure + b"x"))
    with pytest.raises(WorkerError, match="no input 2"):
        await pool.children.stage_input(child, part(2, 0, b"x"))
    await pool.children.stage_input(child, part(1, 0, figure))
    found = pool.children.get(child, CONTEXT)
    assert [f.path for f in found.inputs] == [
        "inputs/data/input.txt",
        "inputs/results/report.md",
        "inputs/shared/fig.png",
    ]
    # Only a ready one takes inputs.
    await pool.children.end(child, pool.end("abandoned"))
    with pytest.raises(WorkerError, match="inputs go to a ready one"):
        await pool.children.stage_input(child, part(1, 0, figure))


async def test_retained_inputs_count_in_the_staging_bound(tmp_path: Path) -> None:
    pool = Pool(tmp_path, child_profiles={"standard": child_profile(stage_max_bytes=100)})
    try:
        await pool.session()
        big = retained("results/big.bin", b"b" * 95)
        with pytest.raises(WorkerError, match=r"inputs_too_large: input results/big\.bin"):
            await pool.children.admit(pool.request(inputs=("data",), retained=(big,)))
        assert pool.store.child_environments.listed() == []
    finally:
        await pool.close()


async def test_no_client_may_create_an_environment_by_a_child_id(pool: Pool) -> None:
    with pytest.raises(WorkerError, match="child environments") as caught:
        await pool.environments.create(
            EnvironmentRequest(
                environment_id="p1-child-0123456789ab", project_id="p1", profile="minimal",
                policy=pool.policy,
            )
        )  # fmt: skip
    assert caught.value.kind is ErrorKind.INVALID_REQUEST


async def test_a_request_is_refused_before_anything_is_created(tmp_path: Path) -> None:
    pool = Pool(
        tmp_path,
        child_profiles={"standard": child_profile(), "small": child_profile(
            isolation_tier=1, runtime_class_name=None, stage_max_bytes=4)},
        default_child_profile="standard",
    )  # fmt: skip
    try:
        await pool.session()
        small = {"profile": "small", "project_profile": "small"}
        refusals = (
            (pool.request(profile="huge"), "'huge' is not granted"),
            # Without a policy list, only the project's own profile is granted (CE-3).
            (pool.request(profile="small"), r"not granted to this project \(granted: standard\)"),
            (pool.request(**small, min_isolation_tier=3), "isolation_insufficient"),
            (pool.request(**small, inputs=("data",)), "inputs_too_large: input"),
        )
        for request, reason in refusals:
            with pytest.raises(WorkerError, match=reason):
                await pool.children.admit(request)
        assert pool.store.child_environments.listed() == []
        assert pool.fake.calls("sandbox", "create") == [] or len(pool.fake.read()["sandboxes"]) == 1
    finally:
        await pool.close()


async def test_the_policy_can_turn_child_environments_off(tmp_path: Path) -> None:
    pool = Pool(tmp_path)
    try:
        await pool.session(pool.add(granting(tmp_path, "off.yaml", child_environments_max=0)))
        with pytest.raises(WorkerError, match="child_environments_max is 0"):
            await pool.children.admit(pool.request())
    finally:
        await pool.close()


async def test_counts_refuse_with_limit_reached_naming_the_limit(tmp_path: Path) -> None:
    """CE-F2's worker side: a second child environment past the project's limit is
    refused, not queued; one retained no longer counts."""
    pool = Pool(tmp_path, child_retention=60.0)
    try:
        await pool.session(pool.add(granting(tmp_path, "one.yaml", child_environments_max=1)))
        first = await pool.ready()
        with pytest.raises(
            WorkerError, match="limit_reached: this project's child_environments_max"
        ):
            await pool.children.admit(pool.request("dlg-" + "b" * 24))
        ended = await pool.children.end(first.environment_id, pool.end())
        assert ended.phase == "retained"  # no hand-back was made
        second = await pool.ready("dlg-" + "b" * 24)
        assert second.phase == "ready"
    finally:
        await pool.close()


async def test_the_pool_and_profile_counts_apply(tmp_path: Path) -> None:
    pool = Pool(
        tmp_path,
        child_profiles={"standard": child_profile(max_concurrent=1)},
        max_child_environments=1,
    )
    try:
        await pool.session()
        await pool.ready()
        with pytest.raises(WorkerError, match="limit_reached: child profile 'standard'"):
            await pool.children.admit(pool.request("dlg-" + "b" * 24))
    finally:
        await pool.close()


async def test_an_unplaced_child_environment_fails_and_is_released(tmp_path: Path) -> None:
    """CE-F4's worker side: not ready within the profile's schedule timeout."""
    pool = Pool(tmp_path, child_profiles={"standard": child_profile(schedule_timeout=0.3)})
    try:
        await pool.session()
        pool.fake.update(phase_on_create="Provisioning")  # pending: no node has room
        made = await pool.ready()
        assert (made.phase, made.failure) == ("released", "environment_unavailable")
        assert made.message is not None
        assert "not placed" in made.message
        assert pool.environments.get(made.environment_id).state is EnvironmentState.DELETED
        assert len(pool.fake.read()["sandboxes"]) == 1  # the session's only
        # Nothing is held: a new delegation is admitted.
        pool.fake.update(phase_on_create="Ready")
        assert (await pool.ready("dlg-" + "b" * 24)).phase == "ready"
    finally:
        await pool.close()


async def test_the_cost_view_counts_child_environments_seeding(tmp_path: Path) -> None:
    """CE-13: the child environments made within the window, how long their sandboxes took
    to be placed, seeded and started, and from admission to ready, from the worker's
    records; past what the worker keeps, the counts say from when they are complete."""
    pool = Pool(tmp_path, child_profiles={"standard": child_profile(schedule_timeout=0.3)})
    try:
        await pool.session()
        ready = await pool.ready()
        pool.fake.update(phase_on_create="Provisioning")  # pending: no node has room
        await pool.ready("dlg-" + "b" * 24)
        now = datetime.now(UTC)
        found = await pool.children.seeding("p1", now - timedelta(hours=24), now)
        assert (found.made, found.ready, found.failed, found.preparing) == (2, 1, 1, 0)
        assert found.created is not None
        assert found.created.count == 1  # the unplaced one's sandbox was never created
        assert found.to_ready is not None
        assert found.to_ready.count == 1
        assert ready.ready_at is not None
        assert found.to_ready.longest == (ready.ready_at - ready.created_at).total_seconds()
        assert (found.alive, found.retained, found.counted_since) == (1, 0, None)
        # A week reaches past the 3 days released ones are kept.
        week = await pool.children.seeding("p1", now - timedelta(days=7), now)
        assert week.counted_since == now - timedelta(days=3)
        # Another project's are not counted.
        assert (await pool.children.seeding("p2", now - timedelta(hours=24), now)).made == 0
    finally:
        await pool.close()


async def test_a_weaker_sandbox_than_required_is_released(pool: Pool) -> None:
    pool.port.located.clear()
    made, _ = await pool.children.admit(pool.request())
    pool.port.tiers[made.environment_id] = 1
    pool.port.located.set()
    found = await pool.settled(made.environment_id)
    assert (found.phase, found.failure) == ("released", "isolation_insufficient")


# The hand-back (CE-6) and the end (CE-4).


async def test_a_complete_hand_back_releases_the_child_environment(pool: Pool) -> None:
    """CE-F1's worker side: the files named are copied under work/<delegation>/, the
    retained one is named by its revision, the manifest is written once."""
    made = await pool.ready()
    child = made.environment_id
    pool.port.files[child, "out/report.md"] = b"# Report\n"
    pool.port.files[child, "out/big.bin"] = b"\x01" * 2048
    retained = RetainedFile(path="out/published.md", artifact_id="artifact-1", revision=2)
    back = await pool.children.hand_back(
        child, pool.back(("out/report.md", "out/big.bin"), retained=(retained,))
    )
    target = f"work/{DELEGATION}"
    assert back.complete
    assert [f.path for f in back.collected] == [f"{target}/out/report.md", f"{target}/out/big.bin"]
    assert back.retained == (retained,)
    assert pool.port.files[SESSION, f"{target}/out/report.md"] == b"# Report\n"
    copy = json.loads(pool.port.files[SESSION, f"{target}/.handback/manifest.json"])
    assert copy["complete"] is True
    # Asked again, the stored manifest answers; nothing is copied twice.
    again = pool.back(("out/report.md", "out/big.bin"), retained=(retained,))
    assert await pool.children.hand_back(child, again) == back
    assert pool.port.write_counts[SESSION, f"{target}/out/report.md"] == 1
    ended = await pool.children.end(child, pool.end())
    assert ended.phase == "released"
    assert ended.hand_back == back
    assert pool.environments.get(child).state is EnvironmentState.DELETED
    # Ending again answers the same.
    assert (await pool.children.end(child, pool.end())).phase == "released"


async def test_a_file_of_the_write_area_keeps_its_path(pool: Pool) -> None:
    """DA-19 with CE-6: a child in its own environment writes in its write area, and the
    hand-back brings that file to the same path in the session, where its parent reads
    it by the path the child named; a file elsewhere goes below the write area. Of two
    that would land on one path, the first listed is copied, and the hand-back's own
    directory takes none."""
    made = await pool.ready()
    child = made.environment_id
    area = f"work/{DELEGATION}"
    pool.port.files[child, f"{area}/notes.md"] = b"notes\n"
    pool.port.files[child, "out/fig.png"] = b"png"
    pool.port.files[child, "notes.md"] = b"elsewhere\n"
    pool.port.files[child, f"{area}/.handback/manifest.json"] = b"{}"
    back = await pool.children.hand_back(
        child,
        pool.back(
            (f"{area}/notes.md", "out/fig.png", "notes.md", f"{area}/.handback/manifest.json")
        ),
    )
    assert [f.path for f in back.collected] == [f"{area}/notes.md", f"{area}/out/fig.png"]
    assert pool.port.files[SESSION, f"{area}/notes.md"] == b"notes\n"
    assert (SESSION, f"{area}/{area}/notes.md") not in pool.port.files
    reasons = {f.path: f.reason for f in back.not_collected}
    assert reasons == {
        "notes.md": f"'{area}/notes.md' is handed back to the same path, '{area}/notes.md'",
        f"{area}/.handback/manifest.json": ".handback/ is the hand-back's own; no file is "
        "copied there",
    }
    assert not back.complete


async def test_a_continuations_hand_back_goes_to_its_works_write_area(pool: Pool) -> None:
    """DA-19 with CE-12: a Resume's continuation is a delegation of its own for the same
    research work, and writes in the work's area; its hand-back goes there, keeping
    the paths of the files it wrote there."""
    made = await pool.ready()
    child = made.environment_id
    first = "dlg-" + "f" * 24
    area = f"work/{first}"
    pool.port.files[child, f"{area}/notes.md"] = b"edited\n"
    pool.port.files[child, "out/fig.png"] = b"png"
    back = await pool.children.hand_back(
        child, pool.back((f"{area}/notes.md", "out/fig.png"), write_area=area)
    )
    assert back.target == area
    assert [f.path for f in back.collected] == [f"{area}/notes.md", f"{area}/out/fig.png"]
    assert pool.port.files[SESSION, f"{area}/notes.md"] == b"edited\n"
    assert (SESSION, f"{area}/.handback/manifest.json") in pool.port.files
    assert not any(path.startswith(f"work/{DELEGATION}/") for _, path in pool.port.files)


async def test_large_files_go_in_parts_to_the_works_write_area(pool: Pool) -> None:
    """The ranged hand-back keeps DA-19's targets: a continuation's files over 100 MiB go
    to its work's area, one written there at its own path, any other below it."""
    made = await pool.ready()
    child = made.environment_id
    area = "work/dlg-" + "f" * 24
    kept, other = b"k" * LARGE, b"o" * LARGE
    pool.port.files[child, f"{area}/big.bin"] = kept
    pool.port.files[child, "out/big.bin"] = other
    back = await pool.children.hand_back(
        child, pool.back((f"{area}/big.bin", "out/big.bin"), write_area=area)
    )
    assert back.complete
    assert [f.path for f in back.collected] == [f"{area}/big.bin", f"{area}/out/big.bin"]
    assert pool.port.files[SESSION, f"{area}/big.bin"] == kept
    assert pool.port.files[SESSION, f"{area}/out/big.bin"] == other
    assert pool.port.range_writes == 26  # 13 parts each
    assert not pool.port.parts


async def test_an_incomplete_hand_back_retains_two_a_project(tmp_path: Path) -> None:
    pool = Pool(
        tmp_path,
        child_profiles={"standard": child_profile(collect_max_bytes=8)},
        child_retention=0.5,
        child_retention_per_project=1,
    )
    try:
        await pool.session()
        made = await pool.ready()
        child = made.environment_id
        pool.port.files[child, "small.txt"] = b"ok\n"
        pool.port.files[child, "large.bin"] = b"x" * 64
        back = await pool.children.hand_back(child, pool.back(("small.txt", "large.bin", "gone")))
        assert not back.complete
        assert [f.path for f in back.not_collected] == ["large.bin", "gone"]
        ended = await pool.children.end(child, pool.end())
        assert ended.phase == "retained"
        assert ended.retained_until is not None
        assert ended.message is not None
        assert "2 files" in ended.message
        assert pool.environments.get(child).state is EnvironmentState.STOPPED
        # A second retained one releases the oldest (at most one here).
        second = await pool.ready("dlg-" + "b" * 24)
        await pool.children.end(second.environment_id, pool.end())
        assert pool.children.get(child, CONTEXT).phase == "released"
        # The sweep releases the rest at its deadline.
        await asyncio.sleep(0.6)
        assert await pool.children.sweep() == [second.environment_id]
    finally:
        await pool.close()


LARGE = 100 * 1024**2 + 5
"""Just over one 100 MiB write: copied in 13 parts of 8 MiB."""


async def test_a_file_over_100_mib_is_handed_back_in_parts(pool: Pool) -> None:
    """CE-F1's large file: over 100 MiB, it goes in parts of 8 MiB, and takes its name in
    the session only once its whole SHA-256 and size are checked; the manifest names it."""
    made = await pool.ready()
    child = made.environment_id
    data = bytes(range(256)) * (LARGE // 256) + b"tail!"
    assert len(data) == LARGE
    pool.port.files[child, "huge.bin"] = data
    pool.port.files[child, "small.txt"] = b"small\n"
    back = await pool.children.hand_back(child, pool.back(("small.txt", "huge.bin")))
    assert back.complete
    target = f"work/{DELEGATION}/huge.bin"
    found = {f.path: f for f in back.collected}
    assert found[target].size == LARGE
    assert found[target].sha256 == hashlib.sha256(data).hexdigest()
    assert pool.port.files[SESSION, target] == data
    assert pool.port.range_writes == 13
    assert not pool.port.parts  # nothing is left of the parts
    ended = await pool.children.end(child, pool.end())
    assert ended.phase == "released"


async def test_a_file_past_the_hand_backs_bytes_is_left_by_its_size(tmp_path: Path) -> None:
    """Past the bytes left of the profile's collect_max_bytes, a file stays in the child
    environment, which is retained; the reason names its size and the limit, and the
    environment's message the largest files left out."""
    pool = Pool(tmp_path, child_profiles={"standard": child_profile(collect_max_bytes=1000)})
    try:
        await pool.session()
        made = await pool.ready()
        child = made.environment_id
        pool.port.files[child, "small.txt"] = b"x" * 100
        pool.port.files[child, "huge.bin"] = b"y" * 2048
        pool.port.files[child, "big.bin"] = b"z" * 950
        back = await pool.children.hand_back(child, pool.back(("small.txt", "huge.bin", "big.bin")))
        assert not back.complete
        assert [f.path for f in back.collected] == [f"work/{DELEGATION}/small.txt"]
        reasons = {f.path: f.reason for f in back.not_collected}
        assert reasons["huge.bin"] == (
            "2 KiB (2048 bytes) is over the 900 bytes (900 bytes) left of the child "
            "profile's hand-back bytes (collect_max_bytes); it stays in the child "
            "environment while that is retained"
        )
        assert reasons["big.bin"].startswith("950 bytes (950 bytes) is over the 900 bytes")
        ended = await pool.children.end(child, pool.end())
        assert ended.phase == "retained"
        assert ended.message == (
            "its hand-back left 2 files behind; 2 too large to copy (past the hand-back's "
            "bytes), the largest huge.bin (2 KiB), big.bin (950 bytes)"
        )
    finally:
        await pool.close()


async def test_a_large_copy_cut_short_resumes_where_its_part_ends(pool: Pool) -> None:
    """A worker stopped during a ranged copy: the next hand-back goes on from the bytes
    the session's part holds, sending none of them again."""
    made = await pool.ready()
    child = made.environment_id
    data = b"\x07" * LARGE
    pool.port.files[child, "huge.bin"] = data
    pool.port.range_gates["huge.bin"] = asyncio.Event()  # never set: the worker stops
    cut = asyncio.ensure_future(pool.children.hand_back(child, pool.back(("huge.bin",))))
    await eventually(lambda: bool(pool.port.gated), timeout=10)
    cut.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cut
    assert [len(part) for part in pool.port.parts.values()] == [8 * 1024**2]
    del pool.port.range_gates["huge.bin"]
    await pool.restart()
    ended = await pool.children.end(child, pool.end())  # finishes the recorded hand-back
    assert ended.hand_back is not None
    assert ended.hand_back.complete
    assert pool.port.files[SESSION, f"work/{DELEGATION}/huge.bin"] == data
    # The first part was sent once: 13 parts in all.
    assert pool.port.range_writes == 13
    assert ended.phase == "released"


async def test_a_copy_that_gives_up_discards_its_parts(pool: Pool) -> None:
    """A part's read that fails once for a transient reason is tried again; one that
    fails again gives the file up, and its parts in the session go with it."""
    made = await pool.ready()
    child = made.environment_id
    pool.port.files[child, "a.bin"] = b"a" * LARGE
    pool.port.files[child, "b.bin"] = b"b" * LARGE
    read = pool.port.read_part
    failures = {"a.bin": 1, "b.bin": 2}

    async def flaky(context: ExecutionContext, path: str, offset: int, length: int):
        if offset > 0 and failures.get(path, 0) > 0:
            failures[path] -= 1
            raise WorkerError(ErrorKind.UNAVAILABLE, "the executor did not answer")
        return await read(context, path, offset, length)

    pool.port.read_part = flaky
    back = await pool.children.hand_back(child, pool.back(("a.bin", "b.bin")))
    assert [f.path for f in back.collected] == [f"work/{DELEGATION}/a.bin"]
    assert back.not_collected[0].path == "b.bin"
    assert back.not_collected[0].reason == "it was not copied: the executor did not answer"
    assert not pool.port.parts  # b.bin's first part was discarded


async def test_a_copy_the_deadline_ends_discards_its_parts(tmp_path: Path) -> None:
    pool = Pool(tmp_path)
    try:
        await pool.session()
        pool.children.collect_timeout = 0.5
        made = await pool.ready()
        child = made.environment_id
        pool.port.files[child, "huge.bin"] = b"h" * LARGE
        pool.port.range_gates["huge.bin"] = asyncio.Event()  # never set
        back = await pool.children.hand_back(child, pool.back(("huge.bin",)))
        assert not back.complete
        assert [f.path for f in back.not_collected] == ["."]
        assert not pool.port.parts
    finally:
        await pool.close()


async def test_session_inputs_share_the_bound_with_retained_ones(tmp_path: Path) -> None:
    """Staging starts from the bound less the retained inputs' bytes: a session file that
    grew after admission cannot take the room admission counted for them."""
    pool = Pool(tmp_path, child_profiles={"standard": child_profile(stage_max_bytes=100)})
    try:
        await pool.session()
        pool.port.located.clear()  # preparation waits before it stages
        item = retained("results/r.bin", b"r" * 60)
        made, _ = await pool.children.admit(
            pool.request(inputs=("data/input.txt",), retained=(item,))
        )
        pool.port.files[SESSION, "data/input.txt"] = b"i" * 50  # grew: 50 + 60 > 100
        pool.port.located.set()
        found = await pool.settled(made.environment_id)
        assert (found.phase, found.failure) == ("released", "inputs_too_large")
    finally:
        await pool.close()


async def test_inputs_are_refused_once_the_childs_run_began(
    pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"r" * 10
    made = await pool.ready(retained=(retained("results/r.bin", data),))
    monkeypatch.setattr(pool.store, "has_executions", lambda environment_id: True)
    with pytest.raises(WorkerError, match="run has begun"):
        await pool.children.stage_input(made.environment_id, part(0, 0, data))


async def test_a_file_changed_while_copied_is_left_out(pool: Pool) -> None:
    """The whole file's digest is checked at its end: a file that changed while its parts
    were copied takes no name in the session, and its parts are discarded."""
    made = await pool.ready()
    child = made.environment_id
    pool.port.files[child, "huge.bin"] = b"a" * LARGE
    pool.port.range_gates["huge.bin"] = gate = asyncio.Event()
    copying = asyncio.ensure_future(pool.children.hand_back(child, pool.back(("huge.bin",))))
    await eventually(lambda: bool(pool.port.gated), timeout=10)
    pool.port.files[child, "huge.bin"] = b"b" * LARGE  # same size, other content
    gate.set()
    back = await copying
    assert not back.complete
    assert back.not_collected[0].path == "huge.bin"
    assert back.not_collected[0].reason.startswith("it was not copied: ")
    assert (SESSION, f"work/{DELEGATION}/huge.bin") not in pool.port.files
    assert not pool.port.parts


async def test_a_hand_back_must_stay_the_same(pool: Pool) -> None:
    made = await pool.ready()
    pool.port.files[made.environment_id, "a.txt"] = b"a"
    pool.port.gates["a.txt"] = gate = asyncio.Event()
    first = asyncio.ensure_future(
        pool.children.hand_back(made.environment_id, pool.back(("a.txt",)))
    )
    await eventually(lambda: bool(pool.port.gated), timeout=10)
    gate.set()
    await first
    with pytest.raises(WorkerError, match=r"only a ready one|recorded"):
        await pool.children.hand_back(made.environment_id, pool.back(("b.txt",)))


async def test_another_sessions_context_finds_nothing(pool: Pool) -> None:
    made = await pool.ready()
    other = CONTEXT.model_copy(update={"session_id": "s2"})
    with pytest.raises(WorkerError) as caught:
        pool.children.get(made.environment_id, other)
    assert caught.value.kind is ErrorKind.NOT_FOUND
    with pytest.raises(WorkerError):
        await pool.children.end(made.environment_id, ChildEnvironmentEnd(context=other))


async def test_closing_the_session_releases_without_retention(pool: Pool) -> None:
    """CE-4 with JS-25's rule; CE-F3's end: a session close finds nothing more to keep."""
    made = await pool.ready()
    assert await pool.children.close_session(CONTEXT) == [made.environment_id]
    found = pool.children.get(made.environment_id, CONTEXT)
    assert found.phase == "released"
    assert await pool.children.close_session(CONTEXT) == []


async def test_ending_one_being_made_releases_it(pool: Pool) -> None:
    pool.port.located.clear()  # preparation stalls at the probe
    made, _ = await pool.children.admit(pool.request())
    await eventually(
        lambda: pool.children.get(made.environment_id, CONTEXT).phase == "preparing", timeout=10
    )
    ended = await pool.children.end(made.environment_id, pool.end())
    assert ended.phase == "released"
    assert ended.message == "its delegation ended before it was ready"
    pool.port.located.set()


# Restarts (CE-12).


async def test_a_restart_fails_one_being_made_and_keeps_a_ready_one(pool: Pool) -> None:
    ready = await pool.ready()
    pool.port.located.clear()
    making, _ = await pool.children.admit(pool.request("dlg-" + "b" * 24))
    await eventually(
        lambda: pool.children.get(making.environment_id, CONTEXT).phase == "preparing",
        timeout=10,
    )
    await pool.restart()
    pool.port.located.set()
    failed = pool.children.get(making.environment_id, CONTEXT)
    assert (failed.phase, failed.failure) == ("released", "worker_restarted")
    assert pool.children.get(ready.environment_id, CONTEXT).phase == "ready"
    assert pool.environments.get(ready.environment_id).state is EnvironmentState.READY


async def test_a_hand_back_cut_short_goes_on_without_copying_twice(pool: Pool) -> None:
    made = await pool.ready()
    child = made.environment_id
    for name in ("a", "b", "c"):
        pool.port.files[child, f"{name}.txt"] = name.encode()
    pool.port.gates["b.txt"] = asyncio.Event()  # never set: the worker stops here
    cut = asyncio.ensure_future(
        pool.children.hand_back(child, pool.back(("a.txt", "b.txt", "c.txt")))
    )
    await eventually(lambda: bool(pool.port.gated), timeout=10)
    cut.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cut
    del pool.port.gates["b.txt"]
    await pool.restart()
    assert pool.children.get(child, CONTEXT).phase == "collecting"
    ended = await pool.children.end(child, pool.end())  # finishes the recorded hand-back
    assert ended.phase == "released"
    assert ended.hand_back is not None
    assert ended.hand_back.complete
    assert pool.port.write_counts[SESSION, f"work/{DELEGATION}/a.txt"] == 1
    assert pool.port.files[SESSION, f"work/{DELEGATION}/c.txt"] == b"c"


async def test_a_ready_one_past_its_wall_time_is_kept(tmp_path: Path) -> None:
    pool = Pool(tmp_path, child_profiles={"standard": child_profile(wall_time_max=0.1)})
    try:
        await pool.session()
        made = await pool.ready()
        pool.store.child_environments.update(made.environment_id, now=datetime.now(UTC))
        from sandbox_science.worker import child_environments

        child_environments.WALL_TIME_GRACE = 0.0
        try:
            await asyncio.sleep(0.2)
            await pool.children.sweep()
        finally:
            child_environments.WALL_TIME_GRACE = 3600.0
        found = pool.children.get(made.environment_id, CONTEXT)
        assert (found.phase, found.failure) == ("retained", "wall_time")
    finally:
        await pool.close()


# The worker API (CE-11).


async def test_the_routes_serve_the_sessions_child_environments(
    pool: Pool, aiohttp_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def capabilities(_environment: str) -> Capabilities:
        return Capabilities(
            protocol=PROTOCOL_VERSION, languages=(), location=await pool.port.location(SESSION)
        )

    manager = ExecutionManager(
        pool.store,
        LocalBackend(LocalBackendSettings(), pool.tmp / "worker"),
        Blobs(pool.tmp / "worker" / "artifacts"),
        pool_id="test",
        max_artifact_size=1 << 20,
        state_dir=pool.tmp / "worker",
    )
    monkeypatch.setattr(manager, "capabilities", capabilities)
    desk = MonitorDesk(
        None, pool.environments.records.listed, declared_tier=3, measured_tier=lambda _: None
    )
    client: TestClient[Any, Any] = await aiohttp_client(
        create_app(manager, TOKEN, pool.environments, children=pool.children, monitor_desk=desk)
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}
    body = await (
        await client.get(f"/v1/capabilities?environment_id={SESSION}", headers=headers)
    ).json()
    assert "child_environments" in body["features"]
    context = CONTEXT.model_dump(mode="json")
    response = await client.post(
        "/v1/child-environments",
        json={"context": context, "delegation_id": DELEGATION},
        headers=headers,
    )
    assert response.status == 201
    child = (await response.json())["environment_id"]
    await pool.settled(child)
    query = "project_id=p1&session_id=s1&environment_id=" + SESSION
    found = await (
        await client.get(f"/v1/child-environments/{child}?{query}", headers=headers)
    ).json()
    assert found["phase"] == "ready"
    figures = await (await client.get("/v1/monitor?project=p1&window=24h", headers=headers)).json()
    assert (figures["child_environments"]["made"], figures["child_environments"]["alive"]) == (1, 1)
    listed = await (await client.get(f"/v1/child-environments?{query}", headers=headers)).json()
    assert [c["environment_id"] for c in listed["environments"]] == [child]
    response = await client.post(
        f"/v1/child-environments/{child}/inputs",
        json=part(0, 0, b"x").model_dump(mode="json"),
        headers=headers,
    )
    assert response.status == 400  # admission planned no retained input
    assert "no input 0" in (await response.json())["error"]["message"]
    # Another session's context does not find it, and the environment routes do not name it.
    other = query.replace("session_id=s1", "session_id=s2")
    response = await client.get(f"/v1/child-environments/{child}?{other}", headers=headers)
    assert response.status == 404
    assert (await client.get(f"/v1/environments/{child}", headers=headers)).status == 404
    response = await client.post(
        "/v1/files/list",
        json={"context": {**context, "session_id": "s2", "environment_id": child}, "path": "."},
        headers=headers,
    )
    assert response.status == 404
    response = await client.post(
        "/v1/provisioning",
        json={"operation_id": f"chd-{child[-12:]}.replay-0", "context": context,
              "purpose": "x", "pip": ["galois==0.4.2"], "pip_reason": "x"},
        headers=headers,
    )  # fmt: skip
    assert response.status == 400
    # The session's close releases it.
    response = await client.post(
        "/v1/sessions/close",
        json=SessionCloseRequest(context=CONTEXT).model_dump(mode="json"),
        headers=headers,
    )
    assert response.status == 200
    assert pool.children.get(child, CONTEXT).phase == "released"
    await manager.close()


def test_a_child_contract_bounds_its_inputs() -> None:
    with pytest.raises(ValidationError):
        ChildEnvironmentRequest(
            context=CONTEXT, delegation_id=DELEGATION, inputs=tuple(f"f{n}" for n in range(61))
        )
    with pytest.raises(ValidationError):
        ChildEnvironmentRequest(context=CONTEXT, delegation_id=DELEGATION, inputs=("../up",))
    with pytest.raises(ValidationError):
        RetainedInput(path="/etc/x", artifact_id="a", revision=1, sha256="0" * 64, size=1)
    with pytest.raises(ValidationError):
        InputPart(context=CONTEXT, index=0, offset=0, content_b64="A" * (12 * 1024**2))


# The review of 2026-10-26.


def _row(n: int, phase: ChildPhase, at: datetime) -> ChildRecord:
    return ChildRecord(
        f"p1-child-{n:012x}", f"dlg-{n:024x}", "p1", "s1", SESSION, "standard",
        phase, None, None, at, at, None, None, at, at,
    )  # fmt: skip


def test_listings_filter_by_phase_past_a_thousand_released_rows(tmp_path: Path) -> None:
    """Counts and sweeps see every active row, however many released rows came first;
    released rows past the retention are pruned with their steps and manifests."""
    store = Store(tmp_path / "worker.sqlite3")
    try:
        records = store.child_environments
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for n in range(1001):
            records.insert(
                _row(n, "released" if n < 1000 else "ready", start + timedelta(seconds=n))
            )
        records.record_step(f"p1-child-{0:012x}", "inputs", "{}", start)
        assert [r.environment_id for r in records.listed(phases=ACTIVE)] == [
            f"p1-child-{1000:012x}"
        ]
        assert len(records.listed(phases=ACTIVE, project_id="p1", session_id="s1")) == 1
        assert len(records.listed()) == 1000  # the newest thousand, the active one among them
        assert records.listed()[-1].phase == "ready"
        assert records.prune(start + timedelta(seconds=500)) == 500
        assert records.get(f"p1-child-{0:012x}") is None
        assert records.steps(f"p1-child-{0:012x}") == {}
        assert records.get(f"p1-child-{1000:012x}") is not None
        records.record_step(f"p1-child-{1000:012x}", "inputs", "{}", start)
        with pytest.raises(sqlite3.IntegrityError, match="pruned"):
            records.db.execute("DELETE FROM child_progress")
    finally:
        store.close()


async def test_admission_counts_past_a_thousand_released_rows(tmp_path: Path) -> None:
    pool = Pool(tmp_path)
    try:
        await pool.session(pool.add(granting(tmp_path, "one.yaml", child_environments_max=1)))
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for n in range(1000):
            pool.store.child_environments.insert(_row(n, "released", start))
        await pool.ready()
        with pytest.raises(WorkerError, match="limit_reached") as caught:
            await pool.children.admit(pool.request("dlg-" + "b" * 24))
        # A definite refusal: 403, never a 5xx a client would read as an unknown outcome.
        assert caught.value.kind is ErrorKind.POLICY_DENIED
        assert caught.value.status == 403
    finally:
        await pool.close()


async def test_the_limit_reaches_the_client_as_a_definite_refusal(
    tmp_path: Path, aiohttp_client: ClientFactory
) -> None:
    """Over HTTP, through the orchestrator's own client: a readable refusal naming the
    limit, not an uncertain failure."""
    from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

    pool = Pool(tmp_path)
    manager = _manager(pool)
    try:
        await pool.session(pool.add(granting(tmp_path, "one.yaml", child_environments_max=1)))
        await pool.ready()
        server: TestClient[Any, Any] = await aiohttp_client(
            create_app(manager, TOKEN, pool.environments, children=pool.children)
        )
        client = WorkerClient(str(server.make_url("/")).rstrip("/"), TOKEN)
        try:
            with pytest.raises(WorkerAPIError) as caught:
                await client.create_child_environment(pool.request("dlg-" + "b" * 24))
        finally:
            await client.close()
        assert (caught.value.kind, caught.value.status, caught.value.uncertain) == (
            ErrorKind.POLICY_DENIED,
            403,
            False,
        )
        assert "limit_reached: this project's child_environments_max of 1" in caught.value.message
    finally:
        await manager.close()
        await pool.close()


async def test_a_policy_list_grants_child_profiles(tmp_path: Path) -> None:
    pool = Pool(
        tmp_path,
        child_profiles={"standard": child_profile(), "large": child_profile()},
        default_child_profile="standard",
    )
    try:
        await pool.session(
            pool.add(granting(tmp_path, "list.yaml", child_profiles=["standard", "large"]))
        )
        made = await pool.ready(profile="large")
        assert made.profile == "large"
    finally:
        await pool.close()


async def test_a_declared_vm_tier_needs_the_probes_evidence(pool: Pool) -> None:
    pool.port.located.clear()
    made, _ = await pool.children.admit(pool.request())
    pool.port.unprobed.add(made.environment_id)
    pool.port.located.set()
    found = await pool.settled(made.environment_id)
    assert (found.phase, found.failure) == ("released", "isolation_insufficient")


async def test_the_manifest_has_a_reserved_name_and_is_written_again(pool: Pool) -> None:
    made = await pool.ready()
    child = made.environment_id
    pool.port.files[child, ".handback/manifest.json"] = b'{"forged": true}'
    pool.port.files[child, "out.txt"] = b"ok"
    back = await pool.children.hand_back(child, pool.back((".handback/manifest.json", "out.txt")))
    assert [f.path for f in back.not_collected] == [".handback/manifest.json"]
    target = f"work/{DELEGATION}/.handback/manifest.json"
    assert json.loads(pool.port.files[SESSION, target])["environment_id"] == child
    del pool.port.files[SESSION, target]  # as a restart between the copy and the record
    again = await pool.children.hand_back(child, pool.back((".handback/manifest.json", "out.txt")))
    assert again == back
    assert (SESSION, target) in pool.port.files


async def test_life_cycle_events_are_logged_once_a_transition(
    pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="sandbox_science.worker.child_environments")
    made = await pool.ready()
    pool.port.files[made.environment_id, "a.txt"] = b"a"
    await pool.children.hand_back(made.environment_id, pool.back(("a.txt",)))
    await pool.children.end(made.environment_id, pool.end())
    await pool.children.end(made.environment_id, pool.end())
    events = [
        (getattr(r, "event", None), getattr(r, "delegation", None), getattr(r, "session", None))
        for r in caplog.records
        if str(getattr(r, "event", "")).startswith("child_environment_")
    ]
    assert [e[0] for e in events] == [
        "child_environment_created",
        "child_environment_ready",
        "child_environment_collecting",
        "child_environment_released",
    ]
    assert {e[1:] for e in events} == {(DELEGATION, "s1")}


async def test_an_abandoned_one_is_released_without_retention(pool: Pool) -> None:
    made = await pool.ready()
    ended = await pool.children.end(made.environment_id, pool.end("abandoned"))
    assert ended.phase == "released"


async def test_a_session_close_after_the_hand_back_finds_nothing(pool: Pool) -> None:
    """CE-F3's end: the child environment went with its hand-back."""
    made = await pool.ready()
    await pool.children.hand_back(made.environment_id, pool.back(()))
    assert (await pool.children.end(made.environment_id, pool.end())).phase == "released"
    assert await pool.children.close_session(CONTEXT) == []


# The recovery fence (docs/specs/worker-recovery-authority.md): a session a run has
# fenced admits only calls with its current epoch's authority.


def owned(epoch: int) -> ExecutionContext:
    """The session's context with the research authority of a run of ``epoch``."""
    authority = ExecutionAuthority(work="w-root", run=f"r-{epoch}", epoch=epoch)
    return CONTEXT.model_copy(update={"authority": authority})


async def test_a_fenced_sessions_calls_with_its_authority_are_admitted(pool: Pool) -> None:
    """A session whose runs have fenced it (a second run: epoch 2): the delegation's
    admission with inputs, their copies, a retained input's parts, the hand-back, the
    end and the session's close all carry the current run's authority and are admitted;
    the same calls without it, or with the first run's, are refused."""
    pool.store.recovery_fences.install(RecoveryFence(project_id="p1", session_id="s1", epoch=2))
    current, stale = owned(2), owned(1)
    report = b"# Report\n"
    request = pool.request(
        inputs=("data/input.txt",), retained=(retained("results/report.md", report),)
    )
    for refused in (request, request.model_copy(update={"context": stale})):
        with pytest.raises(WorkerError, match="missing or superseded research authority"):
            await pool.children.admit(refused)
    assert pool.store.child_environments.listed() == []
    made, created = await pool.children.admit(request.model_copy(update={"context": current}))
    assert created
    made = await pool.settled(made.environment_id)
    assert (made.phase, [f.path for f in made.inputs]) == ("ready", ["inputs/data/input.txt"])
    child = made.environment_id
    data = base64.b64encode(report).decode()
    for context in (CONTEXT, stale):
        with pytest.raises(WorkerError, match="superseded"):
            await pool.children.stage_input(
                child, InputPart(context=context, index=0, offset=0, content_b64=data)
            )
    staged = await pool.children.stage_input(
        child, InputPart(context=current, index=0, offset=0, content_b64=data)
    )
    assert staged.staged is not None
    pool.port.files[child, "out/notes.md"] = b"notes\n"
    for context in (CONTEXT, stale):
        with pytest.raises(WorkerError, match="superseded"):
            await pool.children.hand_back(child, pool.back(("out/notes.md",), context=context))
        with pytest.raises(WorkerError, match="superseded"):
            await pool.children.end(
                child, ChildEnvironmentEnd(context=context, reason="delegation_ended")
            )
    back = await pool.children.hand_back(child, pool.back(("out/notes.md",), context=current))
    assert back.complete
    assert pool.port.files[SESSION, f"work/{DELEGATION}/out/notes.md"] == b"notes\n"
    ended = await pool.children.end(
        child, ChildEnvironmentEnd(context=current, reason="delegation_ended")
    )
    assert ended.phase == "released"
    # Another delegation's, ended by the session's close with its current authority.
    other, _ = await pool.children.admit(pool.request("dlg-" + "b" * 24, context=current))
    await pool.settled(other.environment_id)
    assert await pool.children.close_session(current) == [other.environment_id]
    assert pool.children.get(other.environment_id, CONTEXT).phase == "released"


async def test_a_later_run_finishes_a_hand_back_an_earlier_one_asked_for(pool: Pool) -> None:
    """The hand-back is the same whichever run of the session asks: a later run (a newer
    epoch) asking again is no other hand-back, and ending it copies with its authority."""
    fences = pool.store.recovery_fences
    fences.install(RecoveryFence(project_id="p1", session_id="s1", epoch=1))
    made, _ = await pool.children.admit(pool.request(context=owned(1)))
    child = (await pool.settled(made.environment_id)).environment_id
    pool.port.files[child, "a.txt"] = b"a"
    pool.port.gates["a.txt"] = gate = asyncio.Event()
    first = asyncio.ensure_future(
        pool.children.hand_back(child, pool.back(("a.txt",), context=owned(1)))
    )
    await eventually(lambda: bool(pool.port.gated), timeout=10)
    first.cancel()  # the first run's process stopped while it copied
    with contextlib.suppress(asyncio.CancelledError):
        await first
    assert pool.children.get(child, CONTEXT).phase == "collecting"
    fences.install(RecoveryFence(project_id="p1", session_id="s1", epoch=2))
    gate.set()
    back = await pool.children.hand_back(child, pool.back(("a.txt",), context=owned(2)))
    assert back.complete
    ended = await pool.children.end(
        child, ChildEnvironmentEnd(context=owned(2), reason="delegation_ended")
    )
    assert ended.phase == "released"


async def test_preparation_keeps_the_admitting_runs_authority(pool: Pool) -> None:
    """Preparation goes on after admission answered: its copies in both environments
    carry the admitting run's authority, and a newer epoch installed meanwhile refuses
    them, so the child environment fails instead of acting for a superseded run."""
    fences = pool.store.recovery_fences
    fences.install(RecoveryFence(project_id="p1", session_id="s1", epoch=3))
    pool.port.located.clear()  # preparation waits at the probe
    made, _ = await pool.children.admit(pool.request(inputs=("data",), context=owned(3)))
    fences.install(RecoveryFence(project_id="p1", session_id="s1", epoch=4))
    pool.port.located.set()
    found = await pool.settled(made.environment_id)
    assert found.phase == "released"
    assert found.failure is not None
    assert "superseded" in (found.message or "")


async def test_an_operator_client_acts_without_research_authority(pool: Pool) -> None:
    """The worker's explicit operator permission is the only way past the fence without
    the current authority (an operator's maintenance, the workspace's user actions)."""
    pool.store.recovery_fences.install(RecoveryFence(project_id="p1", session_id="s1", epoch=2))
    with operator_scope(True):
        made = await pool.ready()
        ended = await pool.children.end(made.environment_id, pool.end("abandoned"))
    assert ended.phase == "released"
    assert not OPERATOR.get()


# The operator's commands, with the worker stopped.


def test_an_operator_releases_a_retained_child_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``child-environments list`` shows the rows; ``release`` records the operator's
    release on the row, deletes the sandbox by its recorded identity and leaves the
    deletion hooks to the running worker; one in use is refused, and the pool's settings
    need no child profiles for either."""
    pool = Pool(tmp_path)

    async def prepare() -> tuple[str, str]:
        await pool.session()
        kept = await pool.ready()
        await pool.children.end(kept.environment_id, pool.end())  # no hand-back: retained
        busy = await pool.ready("dlg-" + "b" * 24)
        await pool.children.close()
        return kept.environment_id, busy.environment_id

    try:
        retained_id, busy_id = asyncio.run(prepare())
        sandbox = pool.environments.get(retained_id).sandbox
        settings = tmp_path / "worker.toml"
        settings.write_text(
            f'token_file = "worker.token"\nstate_dir = "{tmp_path / "worker"}"\n\n'
            f'[openshell]\nisolation_tier = 3\ncli = "{pool.fake.cli}"\n'
            f'env = {{FAKE_OPENSHELL_STATE = "{pool.fake.path}", PATH = "{os.environ["PATH"]}"}}'
            "\n\n[openshell.environments]\n"
            f'policy_store = "{tmp_path / "middleware"}"\ntargets = "{TARGETS}"\n'
            f'ceiling = "{tmp_path / "pool-ceiling.yaml"}"\nprofiles = {{minimal = "{IMAGE}"}}\n'
        )
        command = ["child-environments", "list", "--settings", str(settings)]
        assert main([*command, "--phase", "retained"]) == 0
        (line,) = capsys.readouterr().out.splitlines()
        assert line.startswith(f"{retained_id} retained project p1 session s1 delegation ")
        assert "retained until " in line
        assert main(command) == 0
        assert len(capsys.readouterr().out.splitlines()) == 2
        release = ["child-environments", "release", "--settings", str(settings)]
        assert main([*release, busy_id]) == 1
        assert "is ready; only a retained one is released" in capsys.readouterr().err
        assert main([*release, retained_id]) == 0
        assert capsys.readouterr().out.startswith(f"released: child environment {retained_id}")
        row = pool.store.child_environments.get(retained_id)
        assert row is not None
        assert row.phase == "released"
        steps = pool.store.child_environments.steps(retained_id)
        assert json.loads(steps["release"]) == "operator"
        assert sandbox not in pool.fake.read()["sandboxes"]
        info = pool.environments.records.get(retained_id)
        assert info is not None
        assert info.state is EnvironmentState.DELETED
        assert (retained_id, sandbox) in pool.environments.records.hooks_pending()
        assert main([*release, retained_id]) == 0  # released already: its record
        pool.environments.records.hooks_done(retained_id, sandbox)
        assert pool.environments.records.hooks_pending() == []
    finally:
        pool.environments.close()
        pool.store.close()


async def test_the_sweep_runs_while_rows_are_left_without_profiles(tmp_path: Path) -> None:
    """A pool whose child profiles were removed still builds its child environments, so
    the sweep releases a retained one at its deadline; admission refuses."""
    pool = Pool(tmp_path, child_retention=0)
    try:
        await pool.session()
        made = await pool.ready()
        await pool.children.end(made.environment_id, pool.end())
        assert pool.children.get(made.environment_id, CONTEXT).phase == "retained"
        await pool.children.close()
    finally:
        pool.environments.close()
        pool.store.close()
    ceiling = tmp_path / "pool-ceiling.yaml"
    settings = Settings.model_validate(
        {
            "token_file": "worker.token",
            "state_dir": tmp_path / "worker",
            "openshell": {
                "isolation_tier": 3,
                "cli": str(pool.fake.cli),
                "env": {"FAKE_OPENSHELL_STATE": str(pool.fake.path), "PATH": os.environ["PATH"]},
                "environments": {
                    "policy_store": tmp_path / "middleware",
                    "targets": TARGETS,
                    "ceiling": ceiling,
                    "profiles": {"minimal": IMAGE},
                },
            },
        }
    )
    parts = build_worker(settings, TOKEN)
    try:
        assert parts.children is not None
        await parts.children.start()
        found = parts.store.child_environments.get(made.environment_id)
        assert found is not None
        assert found.phase == "released"
        with pytest.raises(WorkerError, match="no child profiles"):
            await parts.children.admit(pool.request("dlg-" + "c" * 24))
    finally:
        await parts.close()


def _manager(pool: Pool) -> ExecutionManager:
    return ExecutionManager(
        pool.store,
        LocalBackend(LocalBackendSettings(), pool.tmp / "worker"),
        Blobs(pool.tmp / "worker" / "artifacts"),
        pool_id="test",
        max_artifact_size=1 << 20,
        state_dir=pool.tmp / "worker",
    )
