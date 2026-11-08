# SPDX-License-Identifier: MIT
"""The user who writes a chat message, a plan edit or a Resume's reasoning effort is its
author in the records (identity.py, D104): ``authentik:<username>`` for a login through
Authentik, ``user`` for the login link. The agents still read each as the user's."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from sandbox_science.orchestrator.chat import _notice
from sandbox_science.orchestrator.continuity.effort import read_override
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.session_plans import SessionPlans

from .authentik_support import Authentik, as_user
from .route_support import ClientFactory, origin, workspace
from .test_routes_plans import change, seed
from .test_routes_recovery import RUN
from .test_routes_recovery import seed as seed_work

SESSION = "/api/v0/projects/p1/sessions/a"


async def test_an_authentik_users_writes_name_them_and_read_as_the_users(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    authentik = Authentik()
    client = await workspace(
        aiohttp_client, state_dir, static_dir, login=False, forward_auth=authentik.verifier()
    )
    bob = {**as_user(authentik.token("bob")), **origin(client)}
    sent = await client.post(
        SESSION + "/messages", json={"message_id": "m-1", "text": "Try $p = 3$."}, headers=bob
    )
    assert sent.status == 200, await sent.text()
    assert (await sent.json())["message"]["author"] == "authentik:bob"
    # The run reads it as the user's own words, whoever sent it.
    with closing(ProjectStore(state_dir)) as store:
        [message] = RunState(store.control).messages("p1", "a")
    notice = _notice(message, {})
    assert (notice.user, notice.framing, notice.payload) == (True, "Try $p = 3$.", None)

    with closing(DocumentStore(store_path(state_dir))) as documents:
        base = seed(SessionPlans(documents))
    edited = await client.post(SESSION + "/plan/changes", json=change(base), headers=bob)
    assert edited.status == 200, await edited.text()
    plan = (await (await client.get(SESSION + "/plan", headers=bob)).json())["plan"]
    assert plan["version"]["author"]["role"] == "user"
    assert plan["version"]["author"]["user_id"] == "authentik:bob"

    seed_work(state_dir)
    effort = {
        "root": RUN,
        "reasoning_effort": {"main": "high"},
        "reason": "A deeper continuation within the original budget.",
    }
    resumed = await client.post(SESSION + "/resume", json=effort, headers=bob)
    assert resumed.status == 200, await resumed.text()
    receipt = await resumed.json()
    with closing(ProjectStore(state_dir)) as store, store.control.read() as tx:
        audit = read_override(tx.sql, "p1", "a", RUN, receipt["resume_request"])
    assert audit is not None
    assert audit.operator == "authentik:bob"


async def test_the_login_links_writes_name_the_operator_as_before(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path
) -> None:
    client = await workspace(aiohttp_client, state_dir, static_dir)
    with closing(DocumentStore(store_path(state_dir))) as documents:
        base = seed(SessionPlans(documents))
    edited = await client.post(SESSION + "/plan/changes", json=change(base), headers=origin(client))
    assert edited.status == 200, await edited.text()
    plan = (await (await client.get(SESSION + "/plan")).json())["plan"]
    assert plan["version"]["author"]["user_id"] == "user"
