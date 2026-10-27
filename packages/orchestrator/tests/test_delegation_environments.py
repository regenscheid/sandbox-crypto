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
from typing import Any, cast

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import JsonValue

from sandbox_science.contracts import (
    Capabilities,
    ErrorKind,
    ExecutionContext,
    Location,
    ReadFileRequest,
    WriteFileRequest,
)
from sandbox_science.contracts.child_environments import (
    ChildEnvironment,
    ChildEnvironmentEnd,
    ChildEnvironmentList,
    ChildEnvironmentRequest,
    ChildHandBack,
    HandBackRequest,
    RetainedFile,
    hand_back_target,
)
from sandbox_science.contracts.worker import (
    PROTOCOL_VERSION,
    Feature,
    ManifestFile,
    UncollectedFile,
)
from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.delegation.environments import (
    NOT_OFFERED,
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
    reopen_in,
)
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.worker_client import WorkerAPIError
from sandbox_science.testing import FakeClock
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
    reasons: list[str] = field(default_factory=list[str])
    offers: bool = True
    """Whether the worker offers child environments (its capabilities' feature)."""
    failing_hand_back: bool = False

    def alive(self) -> list[ChildEnvironment]:
        return [c for c in self.made.values() if c.phase in ("creating", "ready", "collecting")]

    def admit(self, request: ChildEnvironmentRequest) -> ChildEnvironment:
        self.requests.append(request)
        for found in self.made.values():
            if found.delegation_id == request.delegation_id:
                return found
        if len(self.alive()) >= self.limit:
            raise WorkerAPIError(
                ErrorKind.POLICY_DENIED,
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
        if self.failing_hand_back:
            raise WorkerAPIError(ErrorKind.CONFLICT, "the hand-back failed")
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
        self.reasons.append(request.reason)
        found = self.made[environment_id]
        if found.phase in ("released", "retained"):
            return found
        back = found.hand_back
        if request.reason != "delegation_ended" or (back is not None and back.complete):
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

    async def capabilities(self, environment_id: str) -> Capabilities:
        location = Location(pool_id="test", backend="local", hostname="h")
        features: tuple[Feature, ...] = ("child_environments",) if self.offers else ()
        return Capabilities(protocol=PROTOCOL_VERSION, languages=(), location=location,
                            features=features)  # fmt: skip

    async def child_environments(self, context: ExecutionContext) -> ChildEnvironmentList:
        return ChildEnvironmentList(environments=tuple(self.made.values()))

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
        if path == "/v1/capabilities":
            # The session's environment offers child environments, as a pool with child
            # profiles does.
            response = await handler(request)
            assert isinstance(response, web.Response)
            assert response.text is not None
            found = json.loads(response.text)
            if self.offers and found.get("location"):
                found["features"] = [*found["features"], "child_environments"]
            return web.json_response(found, status=response.status)
        if not path.startswith("/v1/child-environments"):
            return await handler(request)
        try:
            if path == "/v1/child-environments" and request.method == "POST":
                body = ChildEnvironmentRequest.model_validate_json(await request.read())
                return _json(self.admit(body), 201)
            if path == "/v1/child-environments":
                return _json(ChildEnvironmentList(environments=tuple(self.made.values())))
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
                {"error": {"kind": error.kind.value, "message": error.message}},
                status=403 if error.kind is ErrorKind.POLICY_DENIED else 409,
            )


def _json(
    model: ChildEnvironment | ChildHandBack | ChildEnvironmentList, status: int = 200
) -> web.Response:
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


# The review of 2026-10-26.


async def test_a_brief_profile_is_the_workers_to_grant(
    tmp_path: Path, service: ChildService
) -> None:
    """CE-3: the brief's profile and the project's own reach the worker, which grants
    the policy's list or, without one, the project's own only."""

    async def runner(child: Child, stop: asyncio.Event) -> ChildEnd:
        return ChildEnd("answered", False, json.dumps(DONE), result=DONE)

    made, _, projects, documents = _placed(tmp_path, runner, service, "tasks")
    DelegationRecords(projects.control).configure(PROJECT, child_profile="standard")
    made.limits = DelegationRecords(projects.control).settings(PROJECT)
    try:
        async with made.lease():
            await made.call("delegate", {"task": "Sieve.", "profile": "large"})
            await asyncio.gather(*(c.task for c in made.children.values()))
        (asked,) = service.requests
        assert (asked.profile, asked.project_profile) == ("large", "standard")
    finally:
        documents.close()
        projects.close()


async def test_a_worker_offering_none_shares_with_a_notice(
    tmp_path: Path, service: ChildService
) -> None:
    """CE-9: ``child_environments_max: 0`` or a pool without child profiles is off."""
    service.offers = False

    async def runner(child: Child, stop: asyncio.Event) -> ChildEnd:
        assert child.environment_id is None
        return ChildEnd("answered", False, json.dumps(DONE), result=DONE)

    made, _, projects, documents = _placed(tmp_path, runner, service, "tasks")
    try:
        async with made.lease():
            text, _ = await made.call("delegate", {"task": "Sieve."})
            await asyncio.gather(*(c.task for c in made.children.values()))
        assert json.loads(text)["environment"] == {"placement": "shared", "notice": NOT_OFFERED}
        assert service.requests == []
    finally:
        documents.close()
        projects.close()


async def test_a_failed_hand_back_still_ends_the_environment(service: ChildService) -> None:
    place = ChildPlacement(service, SESSION_CONTEXT)
    made = await place.admit(DELEGATION, [])
    service.failing_hand_back = True
    kept = await place.close(made.environment_id, DELEGATION, (("a.txt",), ()))
    assert service.ended == [made.environment_id]
    assert kept["end"] == "retained"
    assert "the hand-back failed" in str(kept["error"])


async def test_orphans_and_idle_environments_are_released(
    tmp_path: Path, service: ChildService
) -> None:
    """An environment admitted for a delegation never written (the orchestrator stopped
    in between) is released past ``child_orphan_grace``; one whose run was interrupted
    and never resumed, past ``child_idle_max``."""

    async def runner(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError("no child starts")

    made, records, projects, documents = _placed(tmp_path, runner, service, "tasks")
    made.limits = DelegationRecords(projects.control).configure(
        PROJECT, child_orphan_grace=60, child_idle_max=60
    )
    assert made.placement is not None
    clock = FakeClock(datetime.now(UTC))
    made.placement.clock = clock
    try:
        # Admitted, then the process stopped before the delegation was written.
        orphan = await made.placement.admit("dlg-" + "f" * 24, [])
        # An interrupted run's environment, never resumed.
        idle_delegation = made_delegation(7)
        records.start(PROJECT, SESSION, idle_delegation)
        idle = await made.placement.admit(idle_delegation.delegation_id, [])
        records.place(PROJECT, SESSION, idle_delegation.delegation_id, idle.environment_id)
        records.end(PROJECT, SESSION, idle_delegation.delegation_id,
                    Ending("interrupted", "interrupted"))  # fmt: skip
        async with made.lease():
            pass  # within the bounds: both stay
        assert service.ended == []
        clock.advance(300)
        async with made.lease():
            pass
        assert sorted(service.ended) == sorted([orphan.environment_id, idle.environment_id])
        assert set(service.reasons) == {"abandoned"}
    finally:
        documents.close()
        projects.close()


async def test_child_idle_max_releases_a_settled_environment_during_a_run(
    tmp_path: Path, service: ChildService
) -> None:
    """``child_idle_max`` (CE-4, question 4) passes during a long main run, at a turn, as
    ``review_max`` does: a settled task's environment idle past it is released; one whose
    task awaits its parent's review is kept, however long it waits (PD-46). Unset, the
    bound is ``child_orphan_idle``, 24 hours."""

    async def runner(child: Child, stop: asyncio.Event) -> ChildEnd:
        raise AssertionError("no child starts")

    made, records, projects, documents = _placed(tmp_path, runner, service, "tasks")
    assert made.limits.child_idle_max is None
    assert made.limits.child_orphan_idle == 86_400
    made.limits = records.configure(PROJECT, child_idle_max=600)
    assert made.placement is not None
    clock = FakeClock(datetime.now(UTC))
    made.placement.clock = made.clock = clock
    turn = Transcript(context=AgentContext(messages=[]))
    try:
        settled, awaiting = made_delegation(7), made_delegation(8)
        environments: dict[str, str] = {}
        async with made.lease():
            # Both end during this run: a main run's start accepts what a stopped one left.
            for delegation, ending in (
                (settled, Ending("interrupted", "interrupted")),
                (awaiting, Ending("returned", "answered", result={"status": "partial"})),
            ):
                records.start(PROJECT, SESSION, delegation)
                found = await made.placement.admit(delegation.delegation_id, [])
                environments[delegation.delegation_id] = found.environment_id
                records.place(PROJECT, SESSION, delegation.delegation_id, found.environment_id)
                records.end(PROJECT, SESSION, delegation.delegation_id, ending)
            assert records.get(PROJECT, SESSION, awaiting.delegation_id).review == "awaiting"
            clock.advance(400)  # past the sweep's interval, within child_idle_max
            await made.notices("turn", turn)
            assert service.ended == []  # neither is idle past the bound yet
            clock.advance(300)
            await made.notices("turn", turn)
            assert service.ended == [environments[settled.delegation_id]]
            assert service.reasons == ["abandoned"]
            clock.advance(20 * 3600)
            await made.notices("turn", turn)
            # Within review_max (24 hours), the task awaiting review still holds its
            # environment, however far past child_idle_max.
            assert service.ended == [environments[settled.delegation_id]]
            assert records.get(PROJECT, SESSION, awaiting.delegation_id).review == "awaiting"
    finally:
        documents.close()
        projects.close()


async def test_a_resume_finishes_a_hand_back_cut_short(service: ChildService) -> None:
    place = ChildPlacement(service, SESSION_CONTEXT)
    first = await place.admit(DELEGATION, [])
    service.made[first.environment_id] = service.made[first.environment_id].model_copy(
        update={"phase": "collecting"}
    )
    chosen, replaced = await continue_in(place, first.environment_id, "dlg-" + "d" * 24, {})
    assert replaced == first.environment_id
    assert service.ended == [first.environment_id]
    assert service.reasons == ["delegation_ended"]
    assert chosen != first.environment_id


async def test_the_child_environments_own_tier_must_meet_the_minimum(
    tmp_path: Path, service: ChildService
) -> None:
    """CE-10: the tier in effect is the child environment's own, with the probe's
    evidence; a declared tier alone does not count."""
    from sandbox_science.contracts import IsolationEvidence
    from sandbox_science.orchestrator.harness import Harness as RealHarness
    from sandbox_science.orchestrator.harness import Settings
    from sandbox_science.orchestrator.worker_client import WorkerClient

    from .test_run_harness import main

    tiers: dict[str, Location] = {
        "unprobed": Location(pool_id="t", backend="openshell", hostname="h", isolation_tier=3),
        "probed": Location(
            pool_id="t", backend="openshell", hostname="h", isolation_tier=3,
            isolation_evidence=IsolationEvidence(kernel="separate", sentinel="hidden"),
        ),
    }  # fmt: skip

    class Tiers:
        async def capabilities(self, environment_id: str) -> Capabilities:
            return Capabilities(
                protocol=PROTOCOL_VERSION, languages=(), location=tiers[environment_id]
            )

    harness = RealHarness(
        cast(WorkerClient, Tiers()), main(), settings=Settings(min_isolation_tier=3)
    )
    assert await harness.child_tier_refusal("probed") is None
    refused = await harness.child_tier_refusal("unprobed")
    assert refused is not None
    assert refused.startswith("isolation_insufficient: ")


async def test_a_waiting_child_keeps_its_environment_and_an_ended_one_releases_it(
    owning: Harness, service: ChildService
) -> None:
    """CE-F3 as amended: a child waiting for its parent (``wait_agent_message``) is still
    running, and keeps its environment; once its delegation ends, the hand-back is made
    and the environment released (an ended delegation takes no message, PD-44)."""
    seen: list[list[str]] = []

    async def look() -> None:
        seen.append(list(service.ended))

    await owning.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[call("message_parent", text="Partial: table.txt so far.")]),
            Reply(calls=[call("wait_agent_message", timeout_seconds=0.2)], before=look),
            Reply(calls=[call("write_file", path="table.txt", content="1\n")], before=look),
            Reply(calls=[call("write_file", path="notes.txt", content="n\n")]),
            Reply(json.dumps(RESULT), before=look),
        ],
    )
    assert seen == [[], [], []]
    (environment,) = service.made.values()
    assert environment.phase == "released"
    assert service.ended == [environment.environment_id]


async def test_after_an_unavailable_environment_a_new_delegate_is_admitted(
    owning: Harness, service: ChildService
) -> None:
    """CE-F4's end: nothing is held after the failure; the next delegate gets one."""
    service.ready = False

    async def ready_again() -> None:
        service.ready = True

    root = await owning.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply(calls=[TASK], before=ready_again), wait(),
         Reply("Done.")],
        delegated=[
            Reply(calls=[call("write_file", path="table.txt", content="1\n")]),
            Reply(calls=[call("write_file", path="notes.txt", content="n\n")]),
            Reply(json.dumps(RESULT)),
        ],
    )  # fmt: skip
    first, second = started(root)
    assert delegation(owning, first).ended == "failed"
    assert delegation(owning, second).ended == "returned"
    assert len(service.requests) == 2


# A Resume through ``Recovery`` (CE-12).


def _interrupted_tree(
    tmp_path: Path, environment_id: str, *, reopened: bool = False
) -> tuple[Any, Any, Any, str]:
    """A root and its task child, interrupted, whose delegation worked in
    ``environment_id``; with ``reopened``, the child had returned a first result naming
    ``basis.txt``, its parent's message reopened it, and its next run was interrupted
    (PD-46)."""
    from pydantic import TypeAdapter

    from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints
    from sandbox_science.orchestrator.continuity.records import WorkRecords
    from sandbox_science.orchestrator.harness import RunRequest

    from .test_run_harness import stores
    from .test_tool_journal import RUN
    from .test_work_ownership import CHILD

    state, documents = stores(tmp_path)
    session = state.projects.open_session("p", "s", dedicated=False)
    context = ExecutionContext(
        project_id="p",
        session_id="s",
        environment_id=session.environment_id,
        policy_revision=state.projects.get("p").policy,
    )
    works = WorkRecords(state.projects.control)
    delegations = DelegationRecords(state.projects.control)
    delegation = "dlg-" + "9" * 24
    for run, parent in ((RUN, None), (CHILD, RUN)):
        owner = works.admit("p", "s", run, parent=parent)
        request = RunRequest(
            "p", "s", "Continue research", parent=parent,
            delegation=delegation if parent else None, tokens=100000 if parent else None,
            alias="scripted", memory_version=0, root=RUN if parent else None,
        )  # fmt: skip
        Blueprints(state.projects.control).save(
            owner,
            Blueprint(
                context=context,
                request=TypeAdapter(RunRequest).dump_python(
                    request, mode="json", exclude={"history", "stop_by"}
                ),
            ),
        )
        if parent:
            delegations.start(
                "p",
                "s",
                Delegation(
                    delegation_id=delegation, parent=parent, run=run, kind="task",
                    brief={"task": "Continue research", "inputs": ["data/a.csv"],
                           "environment": "own", "profile": "standard"},
                    started_at=datetime.now(UTC), environment_id=environment_id,
                ),
            )  # fmt: skip
            if not reopened:
                delegations.end("p", "s", delegation, Ending("interrupted", "interrupted"))
                continue
            held: dict[str, JsonValue] = {
                "environment_id": environment_id,
                "end": "held",
                "plan": {"files": ["basis.txt"], "retained": []},
            }
            delegations.end(
                "p", "s", delegation,
                Ending("returned", "answered", result={"status": "partial"}, environment=held),
            )  # fmt: skip
            with state.projects.control.write() as tx:
                tx.sql.execute(
                    "UPDATE research_work SET state='completed' WHERE project='p' AND work=?",
                    (run,),
                )
                found = reopen_in(tx.sql, "p", "s", delegation, AGAIN, cap=None)
            works.admit(
                "p", "s", AGAIN, parent=parent, work=found.work, predecessor=run,
                epoch=found.epoch,
            )  # fmt: skip
            delegations.abandon("p", "s")  # the reopened run was interrupted
    works.interrupt("p", "s", RUN)
    return state, documents, works, CHILD


AGAIN = "20261017T030009000000Z-run-reopened"


@pytest.mark.parametrize(("gone", "reopened"), [(False, False), (True, False), (False, True)])
async def test_a_resume_continues_in_its_environment_or_restarts_in_a_new_one(
    tmp_path: Path, gone: bool, reopened: bool
) -> None:
    from sandbox_science.contracts import KernelObservation
    from sandbox_science.orchestrator.continuity.recovery import Recovery
    from sandbox_science.orchestrator.harness import Harness as RealHarness
    from sandbox_science.orchestrator.harness import Role, Settings
    from sandbox_science.orchestrator.model import ModelTurn
    from sandbox_science.orchestrator.worker_client import WorkerClient

    from .scripted import ScriptedModel
    from .test_run_harness import Sink, main
    from .test_run_harness import Worker as LocalWorker
    from .test_tree_recovery import drive

    service = ChildService()
    session_context = ExecutionContext(project_id="p", session_id="s", environment_id="e")
    previous = service.admit(
        ChildEnvironmentRequest(context=session_context, delegation_id="dlg-" + "9" * 24)
    )
    if gone:
        service.made[previous.environment_id] = service.made[previous.environment_id].model_copy(
            update={"phase": "released"}
        )

    class Resuming(LocalWorker):
        async def observe_kernels(self, context: ExecutionContext) -> KernelObservation:
            return KernelObservation(
                environment_id=context.environment_id, session_id=context.session_id,
                observed_at=datetime.now(UTC), kernels=(),
            )  # fmt: skip

        async def close_session(self, context: ExecutionContext, *, run: str | None = None) -> None:
            return None

        async def child_environment(
            self, environment_id: str, context: ExecutionContext
        ) -> ChildEnvironment:
            return await service.child_environment(environment_id, context)

        async def create_child_environment(
            self, request: ChildEnvironmentRequest
        ) -> ChildEnvironment:
            return service.admit(request)

        async def hand_back(self, environment_id: str, request: HandBackRequest) -> ChildHandBack:
            service.hand_backs.append(request)
            back = ChildHandBack(
                delegation_id=request.delegation_id, environment_id=environment_id,
                target=hand_back_target(request.delegation_id), complete=True,
                ended_at=datetime.now(UTC),
            )  # fmt: skip
            service.made[environment_id] = service.made[environment_id].model_copy(
                update={"phase": "collecting", "hand_back": back}
            )
            return back

        async def end_child_environment(
            self, environment_id: str, request: ChildEnvironmentEnd
        ) -> ChildEnvironment:
            return service.end(environment_id, request)

    state, documents, works, child_run = _interrupted_tree(
        tmp_path, previous.environment_id, reopened=reopened
    )
    done = json.dumps({"status": "complete", "summary": "Recovered"})
    harness = RealHarness(
        cast(WorkerClient, Resuming()),
        main("Recovered root"),
        delegated=Role(ScriptedModel([ModelTurn(done) for _ in range(4)]), "scripted"),
        stores=state,
        settings=Settings(min_isolation_tier=None),
    )
    request = works.request("p", "s")
    assert request is not None
    try:
        sink = Sink()
        failed = await drive(Recovery(harness, "p", "s", request), sink, asyncio.Event())
        assert not failed, sink.errors
        records = DelegationRecords(state.projects.control)
        continuation = next(
            d
            for d in records.listed("p", "s")[0]
            if d.delegation_id != "dlg-" + "9" * 24 and d.kind == "task"
        )
        assert continuation.environment is not None
        if gone:
            (_, again) = service.requests
            assert again.delegation_id == continuation.delegation_id
            assert again.inputs == ("data/a.csv",)
            assert continuation.environment_id != previous.environment_id
            restarted = continuation.environment["restarted"]
            assert isinstance(restarted, dict)
            assert restarted["replaced"] == previous.environment_id
        else:
            assert len(service.requests) == 1
            assert continuation.environment_id == previous.environment_id
            assert "restarted" not in continuation.environment
        (back,) = service.hand_backs
        assert back.delegation_id == continuation.delegation_id
        assert continuation.environment["end"] == "released"
        assert works.get("p", "s", child_run).state == "completed"
        if reopened:
            # The first result's file comes back in the environment's one hand-back,
            # once, and the reopened delegation's account says with whom (PD-46).
            assert back.files == ("basis.txt",)
            original = records.get("p", "s", "dlg-" + "9" * 24)
            assert original.environment is not None
            assert original.environment["end"] == "released"
            assert original.environment["handed_back_with"] == continuation.delegation_id
            assert "plan" not in original.environment
    finally:
        documents.close()
        state.projects.close()
