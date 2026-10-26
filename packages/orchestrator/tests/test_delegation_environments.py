# SPDX-License-Identifier: MIT
"""Delegated runs in environments of their own (docs/specs/child-environments.md): the
project's setting and a brief's ``environment`` (CE-2), admission and its refusal
(CE-9), the run in its own environment, the hand-back and the environment's end in the
outcome (CE-4, CE-6, CE-11), and a Resume's choice (CE-12).

End to end, the in-process worker of ``loop_harness`` answers the child environment
routes with ``ChildService``, a fake of the worker's side: it makes an environment ready
at once (or never), copies a hand-back's files through the worker's own file calls, and
releases or keeps the environment as the worker does. The worker's own side is tested in
the worker's ``test_child_environments``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import sqlite3
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import JsonValue

from sandbox_science.contracts import (
    ErrorKind,
    ExecutionContext,
    ReadFileRequest,
    WriteFileRequest,
)
from sandbox_science.contracts.child_environments import (
    ChildEnvironment,
    ChildEnvironmentEnd,
    ChildEnvironmentRequest,
    ChildHandBack,
    HandBackRequest,
    RetainedFile,
    hand_back_target,
)
from sandbox_science.contracts.worker import ManifestFile, UncollectedFile
from sandbox_science.orchestrator.delegation.environments import (
    OWN_UNDER_OFF,
    ChildPlacement,
    continue_in,
    file_inputs,
    hand_back_plan,
    placement,
)
from sandbox_science.orchestrator.delegation.hook import Child, ChildEnd, observed
from sandbox_science.orchestrator.delegation.records import (
    Delegation,
    DelegationRecords,
    Ending,
)
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError
from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.backends import LocalBackend
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.settings import LocalBackendSettings
from sandbox_science.worker.store import Store

from .control_versions import older_store
from .loop_harness import PROJECT, SESSION, TOKEN, Harness, Lineage, Reply, Worker, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .test_delegation import children, delegation, started, wait
from .test_delegation_lifecycle import _delegations
from .test_delegation_tasks import DONE

pytestmark = pytest.mark.slow

DELEGATION = "dlg-" + "c" * 24


# The fake worker side.


@dataclass
class ChildService:
    """The worker's child environment routes, as a test wants them."""

    manager: ExecutionManager | None = None
    limit: int = 2
    ready: bool = True
    """Whether a new child environment becomes ready; otherwise it fails unplaced."""
    made: dict[str, ChildEnvironment] = field(default_factory=dict[str, ChildEnvironment])
    requests: list[ChildEnvironmentRequest] = field(default_factory=list[ChildEnvironmentRequest])
    hand_backs: list[HandBackRequest] = field(default_factory=list[HandBackRequest])
    ended: list[str] = field(default_factory=list[str])

    def alive(self) -> list[ChildEnvironment]:
        return [c for c in self.made.values() if c.phase in ("creating", "ready", "collecting")]

    def admit(self, request: ChildEnvironmentRequest) -> ChildEnvironment:
        self.requests.append(request)
        for found in self.made.values():
            if found.delegation_id == request.delegation_id:
                return found
        if len(self.alive()) >= self.limit:
            raise WorkerAPIError(
                ErrorKind.UNAVAILABLE,
                f"limit_reached: this project's child_environments_max of {self.limit} is "
                "reached; child environments alive: "
                + ", ".join(c.environment_id for c in self.alive()),
            )
        now = datetime.now(UTC)
        context = request.context
        made = ChildEnvironment(
            environment_id=f"{context.project_id}-child-{secrets.token_hex(6)}",
            delegation_id=request.delegation_id,
            project_id=context.project_id,
            session_id=context.session_id,
            session_environment=context.environment_id,
            profile=request.profile or "standard",
            phase="creating",
            created_at=now,
            ready_by=now + timedelta(seconds=60),
            expires_at=now + timedelta(days=7),
        )
        settled = (
            made.model_copy(update={"phase": "ready", "ready_at": now, "isolation_tier": 3})
            if self.ready
            else made.model_copy(
                update={
                    "phase": "released",
                    "failure": "environment_unavailable",
                    "message": "the sandbox was not placed within 60 s; no node had room",
                }
            )
        )
        self.made[made.environment_id] = settled
        return made

    async def hand_back(self, environment_id: str, request: HandBackRequest) -> ChildHandBack:
        self.hand_backs.append(request)
        found = self.made[environment_id]
        assert self.manager is not None
        child = request.context.model_copy(update={"environment_id": environment_id})
        target = hand_back_target(request.delegation_id)
        collected: list[ManifestFile] = []
        left: list[UncollectedFile] = []
        for path in request.files:
            try:
                read = await self.manager.read_file(ReadFileRequest(context=child, path=path))
            except Exception as error:
                left.append(UncollectedFile(path=path, reason=str(error)[:300]))
                continue
            await self.manager.write_file(
                WriteFileRequest(
                    context=request.context, path=f"{target}/{path}", content_b64=read.content_b64
                )
            )
            size = len(base64.b64decode(read.content_b64))
            collected.append(ManifestFile(path=f"{target}/{path}", size=size, sha256=read.sha256))
        back = ChildHandBack(
            delegation_id=request.delegation_id,
            environment_id=environment_id,
            target=target,
            collected=tuple(collected),
            retained=request.retained,
            not_collected=tuple(left),
            complete=not left,
            ended_at=datetime.now(UTC),
        )
        self.made[environment_id] = found.model_copy(
            update={"phase": "collecting", "hand_back": back}
        )
        return back

    def end(self, environment_id: str, request: ChildEnvironmentEnd) -> ChildEnvironment:
        self.ended.append(environment_id)
        found = self.made[environment_id]
        if found.phase in ("released", "retained"):
            return found
        back = found.hand_back
        if request.reason == "session_closed" or (back is not None and back.complete):
            ended = found.model_copy(update={"phase": "released"})
        else:
            ended = found.model_copy(
                update={
                    "phase": "retained",
                    "retained_until": datetime.now(UTC) + timedelta(days=3),
                    "message": "no hand-back was made before its delegation ended"
                    if back is None
                    else f"its hand-back left {len(back.not_collected)} files behind",
                }
            )
        self.made[environment_id] = ended
        return ended

    # As the worker client, for the hook's own tests.

    async def create_child_environment(self, request: ChildEnvironmentRequest) -> ChildEnvironment:
        return self.admit(request)

    async def child_environment(
        self, environment_id: str, context: ExecutionContext
    ) -> ChildEnvironment:
        if environment_id not in self.made:
            raise WorkerAPIError(ErrorKind.NOT_FOUND, f"no child environment {environment_id!r}")
        return self.made[environment_id]

    async def end_child_environment(
        self, environment_id: str, request: ChildEnvironmentEnd
    ) -> ChildEnvironment:
        return self.end(environment_id, request)

    # As the worker's routes, for the end-to-end tests.

    @web.middleware
    async def routes(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        path = request.path
        if not path.startswith("/v1/child-environments"):
            return await handler(request)
        try:
            if path == "/v1/child-environments" and request.method == "POST":
                body = ChildEnvironmentRequest.model_validate_json(await request.read())
                return _json(self.admit(body), 201)
            environment_id = path.split("/")[3]
            if path.endswith("/hand-back"):
                body = HandBackRequest.model_validate_json(await request.read())
                return _json(await self.hand_back(environment_id, body))
            if path.endswith("/end"):
                body = ChildEnvironmentEnd.model_validate_json(await request.read())
                return _json(self.end(environment_id, body))
            return _json(self.made[environment_id])
        except WorkerAPIError as error:
            return web.json_response(
                {"error": {"kind": error.kind.value, "message": error.message}}, status=503
            )


def _json(model: ChildEnvironment | ChildHandBack, status: int = 200) -> web.Response:
    return web.Response(
        text=model.model_dump_json(), status=status, content_type="application/json"
    )


@pytest.fixture
def service() -> ChildService:
    return ChildService()


@pytest_asyncio.fixture
async def worker(tmp_path: Path, service: ChildService) -> AsyncIterator[Worker]:
    """``loop_harness``'s worker, with the child environment routes of ``service``."""
    root = tmp_path / "worker"
    root.mkdir()
    store = Store(root / "worker.sqlite3")
    manager = ExecutionManager(
        store,
        LocalBackend(LocalBackendSettings(python=sys.executable), root),
        Blobs(root / "artifacts"),
        pool_id="test-pool",
        max_artifact_size=1 << 20,
        state_dir=root,
    )
    service.manager = manager
    app = create_app(manager, TOKEN)
    app.middlewares.append(service.routes)
    await manager.start()
    try:
        async with TestServer(app) as server:
            yield Worker(str(server.make_url("/")).rstrip("/"), Lineage())
    finally:
        await manager.close()
        store.close()


@pytest.fixture
def owning(harness: Harness) -> Harness:
    """A project whose main runs delegate tasks, each in an environment of its own."""
    store = ProjectStore(harness.state)
    try:
        DelegationRecords(store.control).configure(PROJECT, depth=1, child_environments="tasks")
    finally:
        store.close()
    return harness


# The setting and the brief (CE-2).


def test_the_setting_and_the_brief_place_a_delegation() -> None:
    assert placement("off", "task", None) == ("shared", None)
    assert placement("off", "task", "own") == ("shared", OWN_UNDER_OFF)
    assert placement("tasks", "task", None) == ("own", None)
    assert placement("tasks", "task", "shared") == ("shared", None)
    assert placement("tasks", "check", None) == ("shared", None)
    assert placement("all", "check", None) == ("own", None)
    assert placement("all", "task", "shared") == ("shared", None)


def test_only_path_shaped_inputs_are_copied() -> None:
    assert file_inputs(
        ["data/a.csv", "./notes.md", "the paper's Table 2", "../up", "/abs", "data/a.csv", "."]
    ) == ("data/a.csv", "notes.md")


def test_the_hand_back_copies_results_and_what_was_not_retained() -> None:
    """CE-6: declared results are copied, unless published (named by revision); files the
    recorder kept no content of are copied; deleted ones are not."""
    recorded: dict[str, dict[str, object]] = {
        "out/a.md": {"artifact_id": "artifact-1", "revision": 1, "content_retained": True},
        "out/b.md": {"artifact_id": "artifact-2", "revision": 3, "content_retained": True},
    }
    result: dict[str, JsonValue] = {
        "result_files": ["out/a.md"],
        "deliverables": [{"id": "d1", "status": "produced", "path": "./out/b.md"}],
    }
    files: dict[str, JsonValue] = {
        "files": [
            {"path": "big.bin", "content_retained": False, "deleted": False},
            {"path": "gone.bin", "content_retained": False, "deleted": True},
            {"path": "small.txt", "content_retained": True, "deleted": False},
        ]
    }
    copy, retained = hand_back_plan(result, recorded, {"out/b.md"}, files)
    assert copy == ("out/a.md", "big.bin")
    assert retained == (RetainedFile(path="out/b.md", artifact_id="artifact-2", revision=3),)


def test_a_wall_time_ends_a_run_as_a_limit() -> None:
    end = ChildEnd("environment_time", False, None)
    assert observed(end) == "limit_reached"
    assert observed(ChildEnd("environment_unavailable", True, None)) == "failed"


# Admission and refusal, at the hook (CE-2, CE-9).


def _placed(tmp_path: Path, runner: Any, service: ChildService, setting: str) -> Any:
    made, records, projects, documents = _delegations(tmp_path, runner)
    DelegationRecords(projects.control).configure(PROJECT, depth=1, child_environments=setting)
    made.limits = DelegationRecords(projects.control).settings(PROJECT)
    made.placement = ChildPlacement(service, made.context)
    return made, records, projects, documents


async def test_a_shared_brief_runs_shared_and_one_past_the_limit_is_refused(
    tmp_path: Path, service: ChildService
) -> None:
    """CE-F2: with one child in its own environment and a limit of one, a task that asks
    to share runs in the session's environment, and one more is refused, not queued."""
    service.limit = 1
    release = asyncio.Event()
    places: dict[str, str | None] = {}

    async def runner(child: Child, stop: asyncio.Event) -> ChildEnd:
        places[child.delegation_id] = child.environment_id
        await release.wait()
        return ChildEnd("answered", False, json.dumps(DONE), result=DONE)

    made, records, projects, documents = _placed(tmp_path, runner, service, "tasks")
    try:
        async with made.lease():
            first, _ = await made.call("delegate", {"task": "Sieve to 10^9."})
            second, _ = await made.call(
                "delegate", {"task": "Say whether 17 is prime.", "environment": "shared"}
            )
            with pytest.raises(WorkerAPIError, match=r"limit_reached: .*child_environments_max"):
                await made.call("delegate", {"task": "Sieve to 10^10."})
            first_id = json.loads(first)["delegation_id"]
            second_id = json.loads(second)["delegation_id"]
            shown = json.loads(first)["environment"]
            assert shown["state"] == "creating"
            assert "environment of its own" in shown["notice"]
            assert "environment" not in json.loads(second)
            await asyncio.sleep(0)
            assert places[second_id] is None
            assert places[first_id] == shown["environment_id"]
            # Nothing was written for the refused one.
            assert len(made.children) == 2
            release.set()
            await asyncio.gather(*(c.task for c in made.children.values()))
        kept = records.get(PROJECT, SESSION, first_id)
        assert kept.environment_id == shown["environment_id"]
        assert records.get(PROJECT, SESSION, second_id).environment_id is None
        assert records.get(PROJECT, SESSION, second_id).brief["environment"] == "shared"
        # Its runner never reached a hand-back: the environment ended without one, kept.
        assert kept.environment is not None
        assert kept.environment["end"] == "retained"
    finally:
        documents.close()
        projects.close()


async def test_own_under_off_is_a_notice_and_runs_shared(
    tmp_path: Path, service: ChildService
) -> None:
    async def runner(child: Child, stop: asyncio.Event) -> ChildEnd:
        assert child.environment_id is None
        return ChildEnd("answered", False, json.dumps(DONE), result=DONE)

    made, _, projects, documents = _placed(tmp_path, runner, service, "off")
    try:
        async with made.lease():
            text, _ = await made.call("delegate", {"task": "Sieve.", "environment": "own"})
            await asyncio.gather(*(c.task for c in made.children.values()))
        assert json.loads(text)["environment"] == {"placement": "shared", "notice": OWN_UNDER_OFF}
        assert service.requests == []
    finally:
        documents.close()
        projects.close()


async def test_a_delegation_the_records_refuse_ends_its_environment(
    tmp_path: Path, service: ChildService
) -> None:
    """The environment is admitted first; a delegation the records then refuse (the
    session's cap, held here by another parent's delegation) ends it at once."""

    async def runner(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError("no child starts")

    made, records, projects, documents = _placed(tmp_path, runner, service, "tasks")
    DelegationRecords(projects.control).configure(PROJECT, cap=1)
    made.limits = DelegationRecords(projects.control).settings(PROJECT)
    try:
        async with made.lease():
            records.start(PROJECT, SESSION, made_delegation(9))  # another parent's, running
            with pytest.raises(WorkerAPIError, match="limit_reached"):
                await made.call("delegate", {"task": "Two."})
        (admitted,) = service.made
        assert service.ended == [admitted]
        assert made.children == {}
    finally:
        documents.close()
        projects.close()


# Records (CE-11).


def made_delegation(n: int, environment_id: str | None = None) -> Delegation:
    return Delegation.model_validate(
        {
            "delegation_id": f"dlg-{n:024x}",
            "parent": "20261015T120000000000Z-root",
            "run": f"20261015T12000{n}000000Z-child",
            "kind": "task",
            "brief": {"task": "Do."},
            "started_at": "2026-10-15T12:00:01+00:00",
            "environment_id": environment_id,
        }
    )


def test_a_delegation_names_its_environment_and_its_end_once(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    try:
        store.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        store.open_session(PROJECT, SESSION, dedicated=False)
        records = DelegationRecords(store.control)
        records.start(PROJECT, SESSION, made_delegation(0, "p-child-0123456789ab"))
        records.start(PROJECT, SESSION, made_delegation(1))
        # A continuation's is named once while it runs (CE-12).
        records.place(PROJECT, SESSION, made_delegation(1).delegation_id, "p-child-ba9876543210")
        records.place(PROJECT, SESSION, made_delegation(1).delegation_id, "p-child-ba9876543210")
        with pytest.raises(Exception, match="works elsewhere"):
            records.place(PROJECT, SESSION, made_delegation(0).delegation_id, "p-child-x")
        environment: dict[str, JsonValue] = {
            "environment_id": "p-child-0123456789ab",
            "end": "released",
            "hand_back": {"copied": [f"work/x/f{n}" * 40 for n in range(400)], "left": []},
        }
        kept = records.end(
            PROJECT, SESSION, made_delegation(0).delegation_id,
            Ending("returned", "answered", "x", environment=environment),
        )  # fmt: skip
        assert kept.environment_id == "p-child-0123456789ab"
        assert kept.environment is not None
        assert kept.environment["end"] == "released"
        assert len(json.dumps(kept.environment).encode()) <= 65536
        with pytest.raises(sqlite3.IntegrityError), store.control.write() as tx:
            tx.sql.execute("UPDATE delegations SET environment = '{}' WHERE ended IS NOT NULL")
        with pytest.raises(sqlite3.IntegrityError), store.control.write() as tx:
            tx.sql.execute("UPDATE delegations SET environment_id = 'other'")
    finally:
        store.close()


def test_a_version_86_store_migrates_with_no_environments(tmp_path: Path) -> None:
    current = ProjectStore(tmp_path / "current")
    try:
        current.create(PROJECT, "minimal", "sha256:" + "a" * 64)
        current.open_session(PROJECT, SESSION, dedicated=False)
        DelegationRecords(current.control).start(PROJECT, SESSION, made_delegation(0))
    finally:
        current.close()
    target = tmp_path / "old"
    target.mkdir()
    older_store(tmp_path / "current" / "control.sqlite3", target / "control.sqlite3", 86)
    with sqlite3.connect(target / "control.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(delegations)")}
    assert "environment_id" not in columns
    migrated = ProjectStore(target)
    try:
        found = DelegationRecords(migrated.control).get(
            PROJECT, SESSION, made_delegation(0).delegation_id
        )
        assert (found.environment_id, found.environment) == (None, None)
    finally:
        migrated.close()


# End to end (CE-F1, CE-F3, CE-F4).

TASK = call(
    "delegate",
    task="Tabulate a^16 mod 17 into table.txt and notes.txt; publish the table.",
    inputs=["data/primes.txt"],
    deliverables=[
        {"title": "The table", "kind": "file", "description": "table.txt"},
        {"title": "The notes", "kind": "file", "description": "notes.txt"},
    ],
)
RESULT = {
    **DONE,
    "deliverables": [
        {"id": "d1", "status": "produced", "path": "table.txt"},
        {"id": "d2", "status": "produced", "path": "notes.txt"},
    ],
}


async def test_a_task_in_its_own_environment_hands_back_and_is_released(
    owning: Harness, service: ChildService
) -> None:
    """CE-F1 at the fakes' level: the child's calls run in its own environment; at its
    end the unpublished deliverable comes back under work/<delegation>/, the published
    one is named by its revision, and the outcome names the environment released."""
    root = await owning.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[call("write_file", path="table.txt", content="1 1 1\n")]),
            Reply(calls=[call("write_file", path="notes.txt", content="All ones.\n")]),
            Reply(calls=[call("publish", path="table.txt")]),
            Reply(json.dumps(RESULT)),
        ],
    )
    (delegation_id,) = started(root)
    found = delegation(owning, delegation_id)
    (child_environment,) = service.made
    assert found.environment_id == child_environment
    assert service.requests[0].inputs == ("data/primes.txt",)

    # The child's calls ran in its own environment, not the session's.
    def workspace(environment: str) -> Path:
        return (
            owning.tmp / "worker" / "environments" / environment / "project" / "sessions" / SESSION
        )

    assert (workspace(child_environment) / "table.txt").exists()
    session = owning.context().environment_id
    assert not (workspace(session) / "table.txt").exists()
    # The hand-back: the unpublished deliverable copied, the published one named.
    (back,) = service.hand_backs
    assert back.files == ("notes.txt",)
    assert [r.path for r in back.retained] == ["table.txt"]
    target = workspace(session) / "work" / delegation_id
    assert (target / "notes.txt").read_text() == "All ones.\n"
    assert found.environment is not None
    assert found.environment["end"] == "released"
    hand_back = found.environment["hand_back"]
    assert isinstance(hand_back, dict)
    assert hand_back["copied"] == [f"work/{delegation_id}/notes.txt"]
    # The parent's outcome says so.
    (child,) = children(owning, root)
    assert child.parent is not None


async def test_an_environment_never_ready_fails_the_delegation(
    owning: Harness, service: ChildService
) -> None:
    """CE-F4: the delegation ends failed with environment_unavailable; its environment
    is released; the parent may delegate again."""
    service.ready = False
    root = await owning.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[],
    )
    (delegation_id,) = started(root)
    found = delegation(owning, delegation_id)
    assert (found.ended, found.stopped) == ("failed", "environment_unavailable")
    assert found.environment is not None
    assert (found.environment["end"], found.environment["failure"]) == (
        "failed",
        "environment_unavailable",
    )
    assert any("environment_unavailable" in error for error in found.errors)


async def test_a_waiting_child_keeps_its_environment_until_it_ends(
    owning: Harness, service: ChildService
) -> None:
    """CE-F3 at the fakes' level: while the child runs (or waits for its parent) its
    environment is not ended; it is ended once, after the hand-back."""
    seen: list[list[str]] = []

    async def look() -> None:
        seen.append(list(service.ended))

    await owning.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[call("write_file", path="table.txt", content="1\n")], before=look),
            Reply(calls=[call("write_file", path="notes.txt", content="n\n")], before=look),
            Reply(json.dumps(RESULT), before=look),
        ],
    )
    assert seen == [[], [], []]
    assert len(service.ended) == 1


# A Resume (CE-12).

SESSION_CONTEXT = ExecutionContext(project_id=PROJECT, session_id=SESSION, environment_id="e")


async def test_a_resume_continues_in_a_ready_environment(service: ChildService) -> None:
    place = ChildPlacement(service, SESSION_CONTEXT)
    first = await place.admit(DELEGATION, ["data/a.csv"])
    assert await continue_in(place, first.environment_id, "dlg-" + "d" * 24, {}) == (
        first.environment_id,
        None,
    )
    assert len(service.made) == 1


async def test_a_resume_restarts_in_a_new_one_with_its_inputs(service: ChildService) -> None:
    place = ChildPlacement(service, SESSION_CONTEXT)
    first = await place.admit(DELEGATION, ["data/a.csv"])
    service.end(first.environment_id, ChildEnvironmentEnd(context=SESSION_CONTEXT))
    service.made[first.environment_id] = service.made[first.environment_id].model_copy(
        update={"phase": "released"}
    )
    brief: dict[str, JsonValue] = {"inputs": ["data/a.csv", "a note"], "profile": "large"}
    chosen, replaced = await continue_in(place, first.environment_id, "dlg-" + "d" * 24, brief)
    assert (replaced, chosen != first.environment_id) == (first.environment_id, True)
    again = service.requests[-1]
    assert (again.delegation_id, again.inputs, again.profile) == (
        "dlg-" + "d" * 24,
        ("data/a.csv",),
        "large",
    )
    service.ready = False
    with pytest.raises(WorkerAPIError, match="environment_unavailable"):
        await continue_in(place, "p-child-000000000000", "dlg-" + "e" * 24, brief)
