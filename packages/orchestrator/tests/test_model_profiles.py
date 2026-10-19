# SPDX-License-Identifier: MIT
"""Caller-specific requests and immutable continuation pins, without paid calls."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.confinement.models import CallbackGateway
from sandbox_science.orchestrator.confinement.transport import Channel
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.delegation.hook import Child
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path
from sandbox_science.orchestrator.harness import Admitted, Harness, Role, RunRequest, Stores
from sandbox_science.orchestrator.jobstate import JobState
from sandbox_science.orchestrator.messages import MessagesModel
from sandbox_science.orchestrator.model import Limits, ModelError, ModelGateway
from sandbox_science.orchestrator.model_profiles import ModelProfile, ModelRole, profiles
from sandbox_science.orchestrator.model_settings import AgentModelRole, ProjectModels
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.worker_client import WorkerClient

from .test_responses_model import KEY, Fake, response, said
from .test_responses_model import fake as fake
from .test_tool_journal import RUN
from .test_tool_journal import projects as projects


async def test_same_alias_different_roles_and_omitted_defaults(fake: Fake) -> None:
    gateway = ModelGateway(
        fake.url,
        KEY,
        apis={"gpt": "responses"},
        reasoning_effort={"main": "max", "delegated": "low"},
    )
    try:
        for role in ("main", "delegated", "reviewer"):
            fake.then(response(said("done")))
            await gateway.model("gpt", role=role).complete([], [])
        assert [body.get("reasoning") for body in fake.bodies] == [
            {"effort": "max"},
            {"effort": "low"},
            None,
        ]
        assert all("reasoning_effort" not in body for body in fake.bodies)
        original = ModelProfile(alias="gpt", api="responses", reasoning_effort="high")
        with profiles({"main": original}):
            fake.then(response(said("resumed")))
            await gateway.model("gpt", role="main").complete([], [])
        assert fake.bodies[-1]["reasoning"] == {"effort": "high"}
        with profiles({"main": original.model_copy(update={"reasoning_effort": None})}):
            fake.then(response(said("legacy")))
            await gateway.model("gpt", role="main").complete([], [])
        assert "reasoning" not in fake.bodies[-1]
    finally:
        await gateway.close()


async def test_chat_effort_and_messages_fail_without_dispatch(fake: Fake) -> None:
    gateway = ModelGateway(fake.url, KEY, reasoning_effort={"reviewer": "max"})
    try:
        fake.then('{"choices":[{"message":{"content":"done"},"finish_reason":"stop"}]}')
        await gateway.model("gpt", role="reviewer").complete([], [])
        assert fake.bodies[-1]["reasoning_effort"] == "max"
        assert "reasoning" not in fake.bodies[-1]
        model = MessagesModel(fake.url, "gpt", KEY).for_role(
            "main", ModelProfile(alias="gpt", api="messages", reasoning_effort="max")
        )
        try:
            with pytest.raises(ModelError, match="unsupported"):
                await model.complete([], [])
        finally:
            await model.close()
        assert len(fake.bodies) == 1
    finally:
        await gateway.close()


def test_cli_role_validation() -> None:
    assert cli.effort_setting("main=max") == ("main", "max")
    with pytest.raises(Exception, match="ROLE=EFFORT"):
        cli.effort_setting("embedding=max")
    with pytest.raises(ValidationError):
        ModelProfile(alias="x", reasoning_effort="MAX is not a wire value")


async def test_project_agent_models_are_snapshotted_for_a_new_tree(tmp_path: Path) -> None:
    projects = ProjectStore(tmp_path)
    projects.create("p", "minimal", "sha256:" + "a" * 64)
    documents = DocumentStore(store_path(tmp_path), producer="harness")
    gateway = ModelGateway(
        "http://gateway.invalid",
        KEY,
        apis={"sol": "responses", "luna": "responses"},
        reasoning_effort={"main": "max", "delegated": "max", "reviewer": "medium"},
    )
    harness = Harness(
        cast(WorkerClient, object()),
        Role(gateway.model("sol", role="main"), "sol"),
        delegated=Role(gateway.model("sol", role="delegated"), "sol"),
        reviewer=Role(gateway.model("luna", role="reviewer"), "luna"),
        gateway=gateway,
        stores=Stores(projects, documents, JobState(tmp_path), RunState(projects.control)),
    )
    defaults: dict[AgentModelRole, ModelProfile] = {
        "main": gateway.profile("sol", role="main"),
        "delegated": gateway.profile("sol", role="delegated"),
        "reviewer": gateway.profile("luna", role="reviewer"),
    }
    roles = {
        "main": {"alias": "luna", "reasoning_effort": "high"},
        "delegated": {"alias": "luna", "reasoning_effort": "medium"},
        "reviewer": {"alias": "luna", "reasoning_effort": "low"},
    }
    try:
        ProjectModels(documents).change(
            "p",
            {"expected": 0, "roles": roles, "operation_id": "settings-1"},
            USER,
            defaults=defaults,
            available={"sol": "responses", "luna": "responses"},
        )
        request = RunRequest("p", "s", "research")
        pinned = harness.model_profiles(request)
        assert (pinned["main"].alias, pinned["main"].reasoning_effort) == ("luna", "high")
        assert (pinned["delegated"].alias, pinned["delegated"].reasoning_effort) == (
            "luna",
            "medium",
        )
        assert (pinned["reviewer"].alias, pinned["reviewer"].reasoning_effort) == (
            "luna",
            "low",
        )
        assert pinned["fold"].alias == pinned["overview"].alias == "luna"
        frozen = RunRequest("p", "s", "resume", model_profiles=pinned)
        assert harness.model_profiles(frozen) == pinned
        assert harness.role(frozen).alias == "luna"
        reviewer = harness.reviewer_role(frozen)
        assert reviewer is not None
        assert reviewer.alias == "luna"
        harness._limits_seen = {
            "sol": Limits(200_000, 10_000, "openai"),
            "luna": Limits(32_000, 8_000, "openai"),
        }
        assert await harness._ceiling(harness.role(frozen)) == 32_000

        legacy = harness.model_profiles(RunRequest("p", "s", "resume", alias="sol"), legacy=True)
        assert legacy["main"].alias == legacy["fold"].alias == "sol"
        assert all(profile.reasoning_effort is None for profile in legacy.values())
        legacy_child = harness.model_profiles(
            RunRequest("p", "s", "resume", parent="root", delegation="delegation", alias="luna"),
            legacy=True,
        )
        assert legacy_child["delegated"].alias == "luna"
        assert legacy_child["fold"].alias == legacy_child["overview"].alias == "luna"
        assert all(profile.reasoning_effort is None for profile in legacy_child.values())

        parent = Admitted(
            frozen,
            "root",
            ExecutionContext(project_id="p", session_id="s", environment_id="e"),
            (),
            (),
            True,
            datetime.now(UTC),
        )
        child = Child("d", "child", datetime.now(UTC), "brief", 30, 10_000, None, "sol")
        admitted_child = harness._child_admitted(parent, None, child)
        child_profiles = admitted_child.request.model_profiles or {}
        assert child_profiles["delegated"].alias == "sol"
        assert child_profiles["delegated"].reasoning_effort == "medium"
        assert child_profiles["fold"].alias == child_profiles["overview"].alias == "sol"
    finally:
        documents.close()
        await gateway.close()


def test_callback_gateway_preserves_the_paid_kind_for_each_role() -> None:
    async def send(_: str) -> None:
        pass

    async def handle(_: object, __: object) -> None:
        pass

    gateway = CallbackGateway(Channel(send, handle))
    assert gateway.model("other", role="reviewer").kind == "reviewer"
    assert gateway.model("other", role="overview").kind == "fold"


def test_paid_intent_profile_is_atomic_and_remains_when_usage_unknown(
    projects: ProjectStore,
) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    profile = ModelProfile(alias="fixture", api="responses", reasoning_effort="max")
    identity = ModelIntents(projects.control).begin(
        owner, kind="main", alias="fixture", profile=profile
    )
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (identity,)
    with projects.control.read() as tx:
        row = tx.sql.execute(
            "SELECT json_extract(data,'$.profile.reasoning_effort'),"
            "json_extract(data,'$.identity') FROM run_events WHERE kind='notice'"
        ).fetchone()
    assert row == ("max", identity)
    with pytest.raises(ValueError, match="alias differs"):
        ModelIntents(projects.control).begin(owner, kind="main", alias="other", profile=profile)
    assert ModelIntents(projects.control).pending("p", "s", RUN) == (identity,)


async def test_fold_overview_and_former_alias_have_distinct_requested_profiles(fake: Fake) -> None:
    gateway = ModelGateway(fake.url, KEY, apis={"old": "responses", "new": "responses"})
    try:
        with profiles(
            {
                "fold": ModelProfile(alias="new", api="responses", reasoning_effort="high"),
                "overview": ModelProfile(alias="new", api="responses", reasoning_effort="low"),
            }
        ):
            for role in ("fold", "overview"):
                fake.then(response(said("narrative")))
                await gateway.model("new", role=role).complete([], [])
            with pytest.raises(ModelError, match="alias/API"):
                await gateway.model("old", role="fold").complete([], [])
            former = gateway.model("old", role="fold").for_former()
            for role in ("fold", "overview"):
                fake.then(response(said("refit")))
                await former.for_role(role).complete([], [])
        assert [(b["model"], b["reasoning"]["effort"]) for b in fake.bodies] == [
            ("new", "high"),
            ("new", "low"),
            ("old", "high"),
            ("old", "low"),
        ]
    finally:
        await gateway.close()


async def test_confined_embeddings_do_not_require_a_reasoning_profile(
    projects: ProjectStore,
) -> None:
    from sandbox_science.orchestrator.confinement.host import HostLoops
    from sandbox_science.orchestrator.confinement.transport import (
        Capability,
        Channel,
        TransportError,
    )
    from sandbox_science.orchestrator.confinement.wire import ModelIntent, payload
    from sandbox_science.orchestrator.continuity.records import authority

    owner = WorkRecords(projects.control).admit("p", "s", RUN)

    async def send(frame: str) -> None:
        pass

    async def handle(kind, value):
        return None

    channel = Channel(send, handle)
    host = HostLoops(
        channel,
        frozenset({"p"}),
        control=projects.control,
        model_roles={
            "embedding": frozenset({"embed"}),
            "reviewer": frozenset({"fixture"}),
            "fold": frozenset({"fixture"}),
        },
    )
    pinned: dict[ModelRole, ModelProfile] = {
        "main": ModelProfile(alias="fixture", reasoning_effort="max"),
        "reviewer": ModelProfile(alias="fixture", reasoning_effort="low"),
        "fold": ModelProfile(alias="fixture", reasoning_effort="high"),
        "overview": ModelProfile(alias="fixture", reasoning_effort="medium"),
    }
    try:
        with authority(owner), profiles(pinned):
            async with host.model_scope():
                attempt = host.model_capability()
                result = await host.capability(
                    Capability.MODEL_INTENT,
                    payload(ModelIntent(attempt=attempt, alias="embed", kind="embedding")),
                )
                assert result is not None
                for role in ("fold", "overview"):
                    other = "overview" if role == "fold" else "fold"
                    with pytest.raises(TransportError, match="admitted role"):
                        await host.capability(
                            Capability.MODEL_INTENT,
                            payload(
                                ModelIntent(
                                    attempt=attempt,
                                    alias="fixture",
                                    kind="fold",
                                    role=role,
                                    profile=pinned[other],
                                )
                            ),
                        )
                    await host.capability(
                        Capability.MODEL_INTENT,
                        payload(
                            ModelIntent(
                                attempt=attempt,
                                alias="fixture",
                                kind="fold",
                                role=role,
                                profile=pinned[role],
                            )
                        ),
                    )
                with pytest.raises(TransportError, match="admitted role"):
                    await host.capability(
                        Capability.MODEL_INTENT,
                        payload(
                            ModelIntent(
                                attempt=attempt,
                                alias="fixture",
                                kind="reviewer",
                                profile=pinned["main"],
                            )
                        ),
                    )
    finally:
        await channel.close()


async def test_confined_child_summary_requires_its_exact_admitted_profile(projects):
    from types import SimpleNamespace

    from sandbox_science.orchestrator.confinement.host import HostLoops
    from sandbox_science.orchestrator.confinement.transport import (
        Capability,
        Channel,
        TransportError,
    )
    from sandbox_science.orchestrator.confinement.wire import ModelIntent, payload
    from sandbox_science.orchestrator.continuity.records import authority

    owner = WorkRecords(projects.control).admit("p", "s", RUN)

    async def send(frame):
        pass

    async def handle(kind, value):
        return None

    channel = Channel(send, handle)
    host = HostLoops(
        channel,
        frozenset({"p"}),
        control=projects.control,
        model_roles={"fold": frozenset({"main"})},
    )
    pin = ModelProfile(alias="selected-child", api="responses", reasoning_effort="high")
    try:
        with authority(owner), profiles({"fold": pin, "overview": pin}):
            async with host.model_scope():
                attempt = host.model_capability()
                host.attempts[attempt] = cast(
                    Any, SimpleNamespace(given=SimpleNamespace(model="selected-child"))
                )
                for role in ("fold", "overview"):
                    ticket = await host.capability(
                        Capability.MODEL_INTENT,
                        payload(
                            ModelIntent(
                                attempt=attempt,
                                alias=pin.alias,
                                kind="fold",
                                role=role,
                                profile=pin,
                            )
                        ),
                    )
                    assert ticket is not None
                host.attempts[attempt].given.model = "different-child"
                with pytest.raises(TransportError, match="outside configuration"):
                    await host.capability(
                        Capability.MODEL_INTENT,
                        payload(
                            ModelIntent(
                                attempt=attempt,
                                alias=pin.alias,
                                kind="fold",
                                role="fold",
                                profile=pin,
                            )
                        ),
                    )
    finally:
        await channel.close()
