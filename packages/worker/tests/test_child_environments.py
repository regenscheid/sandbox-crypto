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
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
    FileListing,
    ListFilesRequest,
    SessionCloseRequest,
)
from sandbox_science.contracts.child_environments import (
    ChildEnvironment,
    ChildEnvironmentEnd,
    ChildEnvironmentRequest,
    HandBackRequest,
    RetainedFile,
    is_child_environment,
)
from sandbox_science.contracts.worker import PROTOCOL_VERSION
from sandbox_science.policy import effective_resources, load_ceiling, load_policy
from sandbox_science.testing import eventually
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.child_environments import ChildEnvironments
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import (
    ChildProfile,
    EnvironmentSettings,
    LocalBackendSettings,
    OpenShellBackendSettings,
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
    """A ``FakePort`` whose listings carry each file's size, as the worker's do."""

    async def list_files(self, request: ListFilesRequest) -> FileListing:
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
        refusals = (
            (pool.request(profile="huge"), "not offered"),
            (pool.request(profile="small", min_isolation_tier=3), "isolation_insufficient"),
            (pool.request(profile="small", inputs=("data",)), "inputs_too_large: input"),
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
    copy = json.loads(pool.port.files[SESSION, f"{target}/manifest.json"])
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
    client: TestClient[Any, Any] = await aiohttp_client(
        create_app(manager, TOKEN, pool.environments, children=pool.children)
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
    listed = await (await client.get(f"/v1/child-environments?{query}", headers=headers)).json()
    assert [c["environment_id"] for c in listed["environments"]] == [child]
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
            context=CONTEXT, delegation_id=DELEGATION, inputs=tuple(f"f{n}" for n in range(21))
        )
    with pytest.raises(ValidationError):
        ChildEnvironmentRequest(context=CONTEXT, delegation_id=DELEGATION, inputs=("../up",))
