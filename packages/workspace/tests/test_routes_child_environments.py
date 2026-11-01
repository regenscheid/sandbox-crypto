# SPDX-License-Identifier: MIT
"""``GET …/sessions/{session}/child-environments`` and its kernels, through HTTP only
(docs/specs/child-environments.md, CE-8, CE-11): an open session's child environments
with their delegations, hand-backs, retention and idle release, and the kernels of one
that is ready, read in that environment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.contracts.child_environments import ChildEnvironment, ChildHandBack
from sandbox_science.contracts.worker import ManifestFile, UncollectedFile
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords, Ending
from sandbox_science.orchestrator.projects import ProjectStore

from .route_support import (
    ClientFactory,
    FakeWorker,
    ServerFactory,
    context_of,
    error_of,
    fake_worker,
    workspace,
)

PATH = "/api/v0/projects/{project}/sessions/{session}/child-environments"
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
ROOT = "20261027T090000000000Z-root"


def children(project: str = "p1", session: str = "a") -> str:
    return PATH.format(project=project, session=session)


def child(
    n: int, phase: str, *, session: str = "a", session_environment: str, **fields: Any
) -> ChildEnvironment:
    return ChildEnvironment.model_validate(
        {
            "environment_id": f"p1-child-{n:012x}",
            "delegation_id": f"dlg-{n:024x}",
            "project_id": "p1",
            "session_id": session,
            "session_environment": session_environment,
            "profile": "standard",
            "phase": phase,
            "created_at": NOW,
            "ready_by": NOW + timedelta(minutes=30),
            "ready_at": NOW + timedelta(minutes=2) if phase != "creating" else None,
            "expires_at": NOW + timedelta(days=7),
            **fields,
        }
    )


def delegate(state_dir: Path, n: int, ending: Ending | None) -> None:
    """Delegation ``n`` of session a, placed in child environment ``n``, ended as given."""
    store = ProjectStore(state_dir)
    try:
        records = DelegationRecords(store.control)
        made = Delegation.model_validate(
            {
                "delegation_id": f"dlg-{n:024x}",
                "parent": ROOT,
                "run": f"20261027T09000{n}000000Z-child",
                "kind": "task",
                "brief": {"task": f"Sieve block {n}.\nThen report."},
                "started_at": NOW,
                "environment_id": f"p1-child-{n:012x}",
            }
        )
        records.start("p1", "a", made)
        if ending is not None:
            records.end("p1", "a", made.delegation_id, ending)
    finally:
        store.close()


async def test_a_sessions_child_environments_show_their_delegation_use_and_retention(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    session = context_of(state_dir).environment_id
    elsewhere = context_of(state_dir, "p1", "c").environment_id
    worker = FakeWorker()
    left = tuple(
        UncollectedFile(path=f"out/huge-{i}.bin", reason="1.5 GiB is over the 100 MiB")
        for i in range(25)
    )
    back = ChildHandBack(
        delegation_id=f"dlg-{4:024x}",
        environment_id=f"p1-child-{4:012x}",
        target=f"work/dlg-{4:024x}",
        collected=(ManifestFile(path=f"work/dlg-{4:024x}/a.txt", size=1, sha256="0" * 64),),
        not_collected=left,
        complete=False,
        ended_at=NOW,
    )
    for made in (
        child(1, "ready", session_environment=session),
        child(2, "ready", session_environment=session),
        child(3, "ready", session_environment=session),
        child(
            4,
            "retained",
            session_environment=session,
            hand_back=back,
            retained_until=NOW + timedelta(days=3),
            message="its hand-back left 25 files behind",
        ),
        child(5, "creating", session_environment=session),
        # Another session's is not listed.
        child(6, "ready", session="c", session_environment=elsewhere),
    ):  # fmt: skip
        worker.children[made.environment_id] = made
    delegate(state_dir, 1, None)
    delegate(state_dir, 2, Ending("returned", "answered", result={"status": "partial"}))
    delegate(state_dir, 3, Ending("interrupted", "interrupted"))
    delegate(state_dir, 4, Ending("cancelled", "cancelled"))
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    response = await client.get(children())
    assert response.status == 200, await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    body = await response.json()
    assert (body["project_id"], body["session_id"]) == ("p1", "a")
    shown = {view["environment_id"]: view for view in body["environments"]}
    assert sorted(shown) == [f"p1-child-{n:012x}" for n in (1, 2, 3, 4, 5)]
    assert {view["kind"] for view in shown.values()} == {"child"}
    use = {key[-1]: view["use"] for key, view in shown.items()}
    assert use == {
        "1": "running",
        "2": "awaiting_review",
        "3": "idle",
        "4": "ended",
        "5": "preparing",
    }
    running = shown[f"p1-child-{1:012x}"]
    assert running["delegation"]["brief"]["task"] == "Sieve block 1.\nThen report."
    assert running["delegation"]["kind"] == "task"
    assert running["idle_release_after"] is None
    awaiting = shown[f"p1-child-{2:012x}"]["delegation"]
    assert (awaiting["ended"], awaiting["review"]) == ("returned", "awaiting")
    # Settled: released by the next sweep 24 hours (child_orphan_idle) after its end.
    idle = shown[f"p1-child-{3:012x}"]
    ended_at = datetime.fromisoformat(idle["delegation"]["ended_at"])
    assert datetime.fromisoformat(idle["idle_release_after"]) == ended_at + timedelta(days=1)
    retained = shown[f"p1-child-{4:012x}"]
    assert retained["retained_until"] is not None
    assert retained["message"] == "its hand-back left 25 files behind"
    hand_back = retained["hand_back"]
    assert (hand_back["complete"], hand_back["copied"], hand_back["left_count"]) == (False, 1, 25)
    assert len(hand_back["left"]) == 20
    assert shown[f"p1-child-{5:012x}"]["delegation"] is None  # admitted, not yet written
    # The worker was asked about the session's own environment only.
    asked = [query for _, path, query in worker.calls if path == "/v1/child-environments"]
    assert asked == [{"project_id": "p1", "session_id": "a", "environment_id": session}]


async def test_a_ready_child_environments_kernels_are_read_in_it(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    session = context_of(state_dir).environment_id
    worker = FakeWorker()
    ready = child(1, "ready", session_environment=session)
    kept = child(2, "retained", session_environment=session)
    other = child(3, "ready", session="c", session_environment="elsewhere")
    for made in (ready, kept, other):
        worker.children[made.environment_id] = made
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    response = await client.get(children() + f"/{ready.environment_id}/kernels")
    assert response.status == 200, await response.text()
    body = await response.json()
    assert (body["project_id"], body["session_id"]) == ("p1", "a")
    assert body["environment_id"] == ready.environment_id
    assert [(c.session_id, c.environment_id) for c in worker.observed] == [
        ("a", ready.environment_id)
    ]
    # Not ready, another session's, or not a child environment's ID: not found.
    for environment in (kept.environment_id, other.environment_id, session):
        await error_of(await client.get(children() + f"/{environment}/kernels"), 404, "not_found")
    assert len(worker.observed) == 1


@pytest.mark.parametrize(
    ("project", "session"),
    [("missing", "a"), ("p1", "missing"), ("p1", "b")],
    ids=["unknown-project", "unknown-session", "closed-session"],
)
async def test_an_unknown_or_closed_session_is_not_found_without_asking_the_worker(
    aiohttp_client: ClientFactory,
    aiohttp_server: ServerFactory,
    state_dir: Path,
    static_dir: Path,
    project: str,
    session: str,
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    await error_of(await client.get(children(project, session)), 404, "not_found")
    kernels = children(project, session) + f"/p1-child-{1:012x}/kernels"
    await error_of(await client.get(kernels), 404, "not_found")
    assert worker.calls == []


async def test_a_query_is_refused(
    aiohttp_client: ClientFactory, aiohttp_server: ServerFactory, state_dir: Path, static_dir: Path
) -> None:
    worker = FakeWorker()
    url = await fake_worker(aiohttp_server, worker)
    client = await workspace(aiohttp_client, state_dir, static_dir, worker_url=url)
    await error_of(await client.get(children() + "?environment_id=forged"), 400, "invalid_request")
    assert worker.calls == []
