# SPDX-License-Identifier: MIT
"""Projects and sessions as the orchestrator records them, and their environments (W3)."""

from __future__ import annotations

import gc
import re
import sqlite3
import threading
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from sandbox_science.contracts import (
    EnvironmentInfo,
    EnvironmentRequest,
    EnvironmentState,
    ErrorKind,
    ExecutionContext,
)
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.control import migrations, store_path
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.projects import (
    ProjectError,
    ProjectRecord,
    ProjectStore,
    RunRecord,
    ensure_environment,
    environment_id,
)
from sandbox_science.orchestrator.worker_client import WorkerAPIError, WorkerClient

POLICY = "sha256:" + "a" * 64
OTHER_POLICY = "sha256:" + "b" * 64
STARTED = datetime(2026, 10, 11, 13, 1, 2, 345678, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path / "state")


def test_a_project_is_created_once(store: ProjectStore) -> None:
    record, created = store.create("p1", "minimal", POLICY)
    assert created
    assert re.fullmatch(r"p1-default-[0-9a-f]{12}", record.default_environment_id)
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
    default = store.get("p1").default_environment_id
    assert a.environment_id == b.environment_id == default
    assert re.fullmatch(r"p1-c-[0-9a-f]{12}", c.environment_id)
    assert c.dedicated
    assert store.open_session("p1", "a", dedicated=False) == a
    with pytest.raises(ProjectError) as conflict:
        store.open_session("p1", "a", dedicated=True)
    assert conflict.value.kind is ErrorKind.CONFLICT
    assert store.get("p1").environments() == [default, c.environment_id]


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
    assert re.fullmatch(r"p1-a-[0-9a-f]{12}", record.sessions["a"].environment_id)


def test_environment_ids_are_unpredictable(tmp_path: Path) -> None:
    # Two pools' records for the same project and session get different IDs, so no
    # other project's client can take one first (D78).
    first, second = ProjectStore(tmp_path / "one"), ProjectStore(tmp_path / "two")
    ids = []
    for store in (first, second):
        record, _ = store.create("p1", "minimal", POLICY)
        session = store.open_session("p1", "s1", dedicated=True)
        ids.append((record.default_environment_id, session.environment_id))
    assert ids[0][0] != ids[1][0]
    assert ids[0][1] != ids[1][1]


# Runs a session keeps (W4).


def run_record(
    *, session: str = "a", run_id: str = "run-1", started: datetime = STARTED, text: str = "done"
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        context=ExecutionContext(project_id="p1", session_id=session, environment_id="env1"),
        task="Compute $x^2$",
        started_at=started,
        ended_at=started + timedelta(seconds=5),
        stopped="answered",
        final_text=text,
        messages=[
            {"role": "system", "content": "You are a research assistant."},
            {"role": "user", "content": "Compute $x^2$"},
            {"role": "assistant", "content": text},
        ],
        tools=[{"tool": "python", "arguments": '{"code": "x**2"}', "replayed": False}],
    )


@pytest.fixture
def session_store(store: ProjectStore) -> ProjectStore:
    store.create("p1", "minimal", POLICY)
    store.open_session("p1", "a", dedicated=False)
    return store


def test_a_sessions_runs_are_kept_in_order(session_store: ProjectStore) -> None:
    first = session_store.save_run(run_record())
    # The same run ID again adds a record; nothing is replaced.
    again = session_store.save_run(run_record(started=STARTED + timedelta(minutes=1)))
    assert first.name == "20261011T130102345678Z-run-1"
    assert (first.run_id, first.started_at) == ("run-1", STARTED)
    assert session_store.runs("p1", "a") == [first, again]
    assert session_store.run("p1", "a", first.name) == run_record()
    assert session_store.runs("p1", "b") == []


def test_a_record_is_never_replaced(session_store: ProjectStore) -> None:
    first = session_store.save_run(run_record(text="first"))
    with pytest.raises(ProjectError) as refused:
        session_store.save_run(run_record(text="second"))
    assert refused.value.kind is ErrorKind.CONFLICT
    assert session_store.run("p1", "a", first.name).final_text == "first"
    assert session_store.runs("p1", "a") == [first]


def test_runs_stay_clear_of_other_projects_files(session_store: ProjectStore) -> None:
    # Project IDs may contain dots, so a project's runs must not live where another
    # project's record (a.json) or lock (a.lock) is.
    for project in ("a", "a.json", "a.lock"):
        session_store.create(project, "minimal", POLICY)
        session_store.open_session(project, "s", dedicated=False)
        session_store.save_run(
            run_record(session="s").model_copy(
                update={
                    "context": ExecutionContext(
                        project_id=project, session_id="s", environment_id="e"
                    )
                }
            )
        )
    for project in ("a", "a.json", "a.lock"):
        assert session_store.get(project).project_id == project
        assert len(session_store.runs(project, "s")) == 1
    assert session_store.project_ids() == ["a", "a.json", "a.lock", "p1"]


def test_a_reply_that_is_not_valid_unicode_is_kept(session_store: ProjectStore) -> None:
    # A model can send a lone surrogate (half an emoji); the run must still be kept.
    record = run_record(text="half an emoji: \ud83d")
    entry = session_store.save_run(record)
    assert session_store.run("p1", "a", entry.name) == record


@pytest.mark.parametrize(
    "name",
    [
        "",
        "run-1",
        "../p1",
        "20261011T130102345678Z-../../p1",
        "20261011T130102345678Z-run-1/../../p1",
        "20261311T130102345678Z-run-1",
        chr(0xFF12) + "0261011T130102345678Z-run-1",  # a full-width digit
    ],
)
def test_names_that_are_not_run_records_are_refused(session_store: ProjectStore, name: str) -> None:
    session_store.save_run(run_record())
    with pytest.raises(ProjectError) as refused:
        session_store.run("p1", "a", name)
    assert refused.value.kind is ErrorKind.INVALID_REQUEST


def test_missing_damaged_and_oversized_runs_are_refused(session_store: ProjectStore) -> None:
    with pytest.raises(ProjectError) as missing:
        session_store.run("p1", "a", "20261011T130102345678Z-run-9")
    assert missing.value.kind is ErrorKind.NOT_FOUND
    entry = session_store.save_run(run_record())
    with pytest.raises(ProjectError) as large:
        session_store.run("p1", "a", entry.name, max_bytes=100)
    assert large.value.kind is ErrorKind.UNSUPPORTED
    database = sqlite3.connect(session_store.control.path)
    database.execute("DROP TRIGGER runs_no_update")
    database.execute("UPDATE runs SET record = ?", ('{"run_id": "run-1"}',))
    database.commit()
    database.close()
    with pytest.raises(ProjectError) as damaged:
        session_store.run("p1", "a", entry.name)
    assert damaged.value.kind is ErrorKind.INTERNAL


def test_a_run_needs_its_session_and_a_run_id_that_can_name_it(
    session_store: ProjectStore,
) -> None:
    with pytest.raises(ProjectError) as no_session:
        session_store.save_run(run_record(session="nope"))
    assert no_session.value.kind is ErrorKind.NOT_FOUND
    with pytest.raises(ProjectError) as bad_id:
        session_store.save_run(run_record(run_id="has space"))
    assert bad_id.value.kind is ErrorKind.INVALID_REQUEST
    with pytest.raises(ProjectError) as bad_session:
        session_store.runs("p1", "../a")
    assert bad_session.value.kind is ErrorKind.INVALID_REQUEST
    assert session_store.runs("p1", "a") == []


def test_a_project_recorded_only_in_a_json_file_says_so(store: ProjectStore) -> None:
    # Before projects moved into the control store, each was a file of its own.
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "old.json").write_text("{}")
    with pytest.raises(ProjectError) as refused:
        store.get("old")
    assert refused.value.kind is ErrorKind.UNAVAILABLE
    assert "create the project again" in refused.value.message
    with pytest.raises(ProjectError) as missing:
        store.get("other")
    assert missing.value.kind is ErrorKind.NOT_FOUND


def test_project_ids_name_only_project_records(session_store: ProjectStore) -> None:
    session_store.create("p2", "minimal", POLICY)
    session_store.save_run(run_record())
    (session_store.root / "notes.txt").write_text("not a project")
    (session_store.root / "bad name.json").write_text("{}")
    (session_store.root / "x.json").mkdir()
    assert session_store.project_ids() == ["p1", "p2"]


def test_a_read_only_store_reads_without_making_the_database(tmp_path: Path) -> None:
    reader = ProjectStore(tmp_path, readonly=True)
    assert reader.project_ids() == []
    with pytest.raises(ProjectError) as missing:
        reader.get("p1")
    assert missing.value.kind is ErrorKind.NOT_FOUND
    assert not (tmp_path / "control.sqlite3").exists()
    writer = ProjectStore(tmp_path)
    writer.create("p1", "minimal", POLICY)
    writer.open_session("p1", "a", dedicated=False)
    assert reader.get("p1") == writer.get("p1")
    assert reader.runs("p1", "a") == []
    reader.close()
    writer.close()


def _v2_store(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A control store as the code before projects moved in made it: version 2."""
    with monkeypatch.context() as old:
        old.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:1])
        old.setattr(migrations, "VERSION", migrations.BASELINE)
        store = DocumentStore(store_path(state))
        store.initialize()
        store.close()


def test_a_v2_store_migrates_once_and_keeps_its_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _v2_store(tmp_path, monkeypatch)
    database = sqlite3.connect(store_path(tmp_path))
    database.execute("INSERT INTO job_runs (project) VALUES ('kept')")  # data that stays
    database.commit()
    database.close()
    store = ProjectStore(tmp_path)
    store.create("p1", "minimal", POLICY)
    store.close()
    again = ProjectStore(tmp_path)  # a second open runs nothing
    assert again.project_ids() == ["p1"]
    again.close()
    database = sqlite3.connect(store_path(tmp_path))
    assert database.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
    assert database.execute("PRAGMA application_id").fetchone() == (migrations.APPLICATION_ID,)
    assert database.execute("SELECT project FROM job_runs").fetchall() == [("kept",)]
    database.close()


def test_a_failed_projects_step_leaves_the_v2_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _v2_store(tmp_path, monkeypatch)
    projects_step = migrations.MIGRATIONS[1]

    def failing(schema: migrations.Schema) -> None:
        projects_step(schema)
        schema.execute("SELECT no_such_function()")

    with monkeypatch.context() as broken:
        broken.setattr(migrations, "MIGRATIONS", (migrations.MIGRATIONS[0], failing))
        store = ProjectStore(tmp_path)
        with pytest.raises(sqlite3.OperationalError):
            store.project_ids()
        store.close()
    database = sqlite3.connect(store_path(tmp_path))
    assert database.execute("PRAGMA user_version").fetchone() == (migrations.BASELINE,)
    assert database.execute("SELECT 1 FROM sqlite_master WHERE name='projects'").fetchone() is None
    database.close()


def test_a_damaged_run_list_is_a_project_error(session_store: ProjectStore) -> None:
    session_store.save_run(run_record())
    database = sqlite3.connect(session_store.control.path)
    database.execute("DROP TRIGGER runs_no_update")
    database.execute("UPDATE runs SET started_at = 'garbage'")
    database.commit()
    database.close()
    with pytest.raises(ProjectError) as damaged:
        session_store.runs("p1", "a")
    assert damaged.value.kind is ErrorKind.INTERNAL


def test_projects_recorded_only_in_json_files_are_listed(store: ProjectStore) -> None:
    store.create("p1", "minimal", POLICY)
    (store.root / "old.json").write_text("{}")
    assert store.project_ids() == ["old", "p1"]
    with pytest.raises(ProjectError, match="create the project again"):
        store.get("old")


def test_a_session_lookup_leaves_no_connection_open(tmp_path: Path) -> None:
    writer = ProjectStore(tmp_path)
    writer.create("p1", "minimal", POLICY)
    writer.open_session("p1", "a", dedicated=False)
    writer.close()
    documents = DocumentStore(store_path(tmp_path), readonly=True)
    gc.collect()  # what earlier tests left behind is not this test's
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(3):
            ReviewStore(documents).scope("p1", "a")  # as each workspace review request does
        gc.collect()
    documents.close()
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []


def test_a_read_never_makes_the_control_store(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)  # a writer, as the harness has it
    assert store.project_ids() == []
    with pytest.raises(ProjectError) as missing:
        store.get("p1")
    assert missing.value.kind is ErrorKind.NOT_FOUND
    assert store.runs("p1", "a") == []
    store.close()
    assert not store_path(tmp_path).exists()
