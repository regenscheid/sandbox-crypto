# SPDX-License-Identifier: MIT
"""Projects and sessions as the orchestrator records them, and their environments (W3)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
)
from sandbox_science.orchestrator.projects import (
    ProjectError,
    ProjectRecord,
    ProjectStore,
    ensure_environment,
    environment_id,
)
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

POLICY = "sha256:" + "a" * 64
OTHER_POLICY = "sha256:" + "b" * 64


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path / "state")


def test_a_project_is_created_once(store: ProjectStore) -> None:
    record, created = store.create("p1", "minimal", POLICY)
    assert created
    assert record.default_environment_id == "p1-default"
    assert (record.profile, record.policy, record.sessions) == ("minimal", POLICY, {})
    again, created = store.create("p1", "minimal", POLICY)
    assert (again, created) == (record, False)
    with pytest.raises(ProjectError) as conflict:
        store.create("p1", "crypto-base", POLICY)
    assert conflict.value.kind is ErrorKind.CONFLICT
    assert store.get("p1") == record


@pytest.mark.parametrize(
    ("project_id", "kind"),
    [("../escape", ErrorKind.INVALID_REQUEST), ("missing", ErrorKind.NOT_FOUND)],
)
def test_unknown_and_invalid_projects_are_refused(
    store: ProjectStore, project_id: str, kind: ErrorKind
) -> None:
    with pytest.raises(ProjectError) as refused:
        store.get(project_id)
    assert refused.value.kind is kind


def test_an_invalid_policy_revision_is_refused(store: ProjectStore) -> None:
    with pytest.raises(ProjectError) as refused:
        store.create("p1", "minimal", "latest")
    assert refused.value.kind is ErrorKind.INVALID_REQUEST


def test_sessions_share_the_default_environment_unless_dedicated(store: ProjectStore) -> None:
    store.create("p1", "minimal", POLICY)
    a = store.open_session("p1", "a", dedicated=False)
    b = store.open_session("p1", "b", dedicated=False)
    c = store.open_session("p1", "c", dedicated=True)
    assert a.environment_id == b.environment_id == "p1-default"
    assert (c.environment_id, c.dedicated) == ("p1-c", True)
    assert store.open_session("p1", "a", dedicated=False) == a
    with pytest.raises(ProjectError) as conflict:
        store.open_session("p1", "a", dedicated=True)
    assert conflict.value.kind is ErrorKind.CONFLICT
    assert store.get("p1").environments() == ["p1-default", "p1-c"]


def test_a_closed_session_opens_again(store: ProjectStore) -> None:
    store.create("p1", "minimal", POLICY)
    store.open_session("p1", "a", dedicated=False)
    closed = store.close_session("p1", "a")
    assert closed.closed_at is not None
    reopened = store.open_session("p1", "a", dedicated=False)
    assert reopened.closed_at is None
    assert reopened.environment_id == closed.environment_id
    with pytest.raises(ProjectError) as missing:
        store.close_session("p1", "nope")
    assert missing.value.kind is ErrorKind.NOT_FOUND


def test_a_policy_revision_is_recorded(store: ProjectStore) -> None:
    store.create("p1", "minimal", POLICY)
    revised = store.set_policy("p1", OTHER_POLICY)
    assert revised.policy == OTHER_POLICY
    assert store.get("p1").policy == OTHER_POLICY


def test_concurrent_writers_lose_no_session(store: ProjectStore) -> None:
    store.create("p1", "minimal", POLICY)
    errors: list[BaseException] = []

    def open_many(prefix: str) -> None:
        try:
            for index in range(25):
                store.open_session("p1", f"{prefix}{index}", dedicated=index % 2 == 0)
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=open_many, args=(prefix,)) for prefix in "abcd"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(store.get("p1").sessions) == 100


def test_long_ids_make_valid_environment_ids() -> None:
    project, session = "p" * 128, "s" * 128
    first = environment_id(project, session)
    assert len(first) == 128
    assert first != environment_id(project, "t" * 128)
    assert environment_id("p1", "s1") == "p1-s1"


class FakeWorker:
    """The environment calls ensure_environment makes, against an in-memory worker."""

    def __init__(self, *, manages: bool = True) -> None:
        self.manages = manages
        self.environments: dict[str, EnvironmentInfo] = {}
        self.calls: list[str] = []

    def _check(self) -> None:
        if not self.manages:
            raise WorkerAPIError(ErrorKind.UNSUPPORTED, "this worker does not manage environments")

    async def environment(self, environment_id: str) -> EnvironmentInfo:
        self.calls.append("get")
        self._check()
        info = self.environments.get(environment_id)
        if info is None:
            raise WorkerAPIError(ErrorKind.NOT_FOUND, f"no environment {environment_id!r}")
        return info

    async def create_environment(self, request: EnvironmentRequest) -> EnvironmentInfo:
        self.calls.append("create")
        now = datetime.now(UTC)
        info = EnvironmentInfo(
            environment_id=request.environment_id,
            project_id=request.project_id,
            profile=request.profile,
            image=f"sandbox-science/{request.profile}:test",
            sandbox=f"ss-{request.environment_id}",
            policy=request.policy,
            state=EnvironmentState.READY,
            created_at=now,
            updated_at=now,
        )
        self.environments[request.environment_id] = info
        return info

    async def revise_environment(self, environment_id: str, policy: str) -> EnvironmentInfo:
        self.calls.append("revise")
        info = self.environments[environment_id].model_copy(update={"policy": policy})
        self.environments[environment_id] = info
        return info


@pytest.mark.asyncio
async def test_the_environment_is_created_then_reused_then_revised(store: ProjectStore) -> None:
    project, _ = store.create("p1", "minimal", POLICY)
    worker = FakeWorker()
    client = cast(WorkerClient, worker)
    created = await ensure_environment(client, project, "p1-default")
    assert created is not None
    assert (created.profile, created.policy) == ("minimal", POLICY)
    assert await ensure_environment(client, project, "p1-default") == created
    revised_project = store.set_policy("p1", OTHER_POLICY)
    revised = await ensure_environment(client, revised_project, "p1-default")
    assert revised is not None
    assert revised.policy == OTHER_POLICY
    assert worker.calls == ["get", "create", "get", "get", "revise"]


@pytest.mark.asyncio
async def test_an_environment_of_another_project_is_refused(store: ProjectStore) -> None:
    worker = FakeWorker()
    client = cast(WorkerClient, worker)
    other, _ = store.create("p2", "minimal", POLICY)
    await ensure_environment(client, other, "shared-id")
    project, _ = store.create("p1", "minimal", POLICY)
    with pytest.raises(WorkerAPIError) as refused:
        await ensure_environment(client, project, "shared-id")
    assert refused.value.kind is ErrorKind.CONFLICT


@pytest.mark.asyncio
async def test_a_deleted_environment_is_created_again(store: ProjectStore) -> None:
    project, _ = store.create("p1", "minimal", POLICY)
    worker = FakeWorker()
    client = cast(WorkerClient, worker)
    first = await ensure_environment(client, project, "p1-default")
    assert first is not None
    worker.environments["p1-default"] = first.model_copy(update={"state": EnvironmentState.DELETED})
    again = await ensure_environment(client, project, "p1-default")
    assert again is not None
    assert again.state is EnvironmentState.READY


@pytest.mark.asyncio
async def test_a_worker_without_environments_uses_the_id_as_it_is(store: ProjectStore) -> None:
    project, _ = store.create("p1", "minimal", POLICY)
    worker = FakeWorker(manages=False)
    assert await ensure_environment(cast(WorkerClient, worker), project, "p1-default") is None


def test_records_survive_a_new_store(tmp_path: Path, store: ProjectStore) -> None:
    store.create("p1", "minimal", POLICY)
    store.open_session("p1", "a", dedicated=True)
    reopened = ProjectStore(tmp_path / "state")
    record: ProjectRecord = reopened.get("p1")
    assert record.sessions["a"].environment_id == "p1-a"
