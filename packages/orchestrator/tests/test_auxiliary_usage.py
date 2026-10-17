# SPDX-License-Identifier: MIT
"""Canonical auxiliary spend survives the materialization crash window exactly once."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import cast

import pytest
from pydantic import JsonValue

from sandbox_science.orchestrator.continuity.materializations import Table, link
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords, authority
from sandbox_science.orchestrator.history.collections import Collection
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.skills.store import SkillStore
from sandbox_science.testing import FakeClock

from .test_model_usage_journal import START
from .test_tool_journal import RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import record

SOURCES: dict[str, tuple[Table, str]] = {
    "reviewer": ("review_calls", "reviewer"),
    "watcher": ("job_watcher_calls", "watcher"),
    "skills": ("skill_metadata_calls", "metadata"),
    "embedding": ("skill_embedding_calls", "embeddings"),
}


def legacy(db, kind, usage):
    if kind == "reviewer":
        return db.execute(
            "INSERT INTO review_calls(project,review_id,created,estimated_tokens,"
            "input_tokens,output_tokens,cost,cost_source) VALUES('p','review',?,10,10,2,?,?)",
            (START.timestamp(), usage.cost, usage.cost_source),
        ).lastrowid
    if kind == "watcher":
        return db.execute(
            "INSERT INTO job_watcher_calls(project,requested_at,input_tokens,"
            "output_tokens,cost,cost_source) VALUES('p',?,10,2,?,?)",
            (START.timestamp(), usage.cost, usage.cost_source),
        ).lastrowid
    if kind == "skills":
        return db.execute(
            "INSERT INTO skill_metadata_calls(project,created,snapshot,model,"
            "input_tokens,output_tokens,cost,cost_source) VALUES('p',?,'s','m',10,2,?,?)",
            (START.timestamp(), usage.cost, usage.cost_source),
        ).lastrowid
    return db.execute(
        "INSERT INTO skill_embedding_calls VALUES('p',?,'m',1,?)", (START.timestamp(), usage.cost)
    ).lastrowid


@pytest.mark.parametrize("kind", SOURCES)
@pytest.mark.parametrize("cost", [0.25, None, 0.0])
def test_canonical_auxiliary_usage_precedes_and_suppresses_exact_legacy_copy(
    projects: ProjectStore, kind: str, cost: float | None
) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    intents = ModelIntents(projects.control, clock=FakeClock(START))
    source = "gateway" if kind == "embedding" else "model_info"
    usage = ModelUsage(
        input_tokens=10,
        output_tokens=2,
        cost=cost,
        cost_source=source if cost is not None else None,
    )
    identity = intents.begin(owner, kind=kind, alias="fixture")
    intents.observe(owner, identity, usage)
    state = RunState(projects.control)
    _, role = SOURCES[kind]
    expected = {} if cost is None else {role: cost}
    assert state.model_costs("p") == expected
    assert state.unpriced_calls("p", START) == (1 if cost is None else 0)
    with authority(owner), projects.control.write() as tx:
        row = legacy(tx.sql, kind, usage)
        assert row is not None
        link(tx.sql, SOURCES[kind][0], row, identity)
    projects.control.close()
    assert state.model_costs("p") == expected
    assert state.unpriced_calls("p", START) == (1 if cost is None else 0)
    assert state.model_info_cost("p") == (
        cost if cost is not None and kind != "embedding" else None
    )
    state.set_cost_limit("p", 10)
    assert state.over_cost_limit("p", START + timedelta(minutes=1)) == (cost is None)
    # The canonical dispatch time wins even if a legacy consumer writes much later.
    with projects.control.write() as tx:
        table, _ = SOURCES[kind]
        column = "requested_at" if kind == "watcher" else "created"
        tx.sql.execute(
            f"UPDATE {table} SET {column}=? WHERE rowid=?",
            ((START + timedelta(days=2)).timestamp(), row),
        )
    assert state.model_costs("p", START + timedelta(days=1)) == {}
    assert state.unpriced_calls("p", START + timedelta(days=1)) == 0


def test_distinct_equal_charges_and_unlinked_legacy_are_not_coalesced(
    projects: ProjectStore,
) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    intents = ModelIntents(projects.control)
    store = SkillStore(projects.control)
    for _ in range(2):
        identity = intents.begin(owner, kind="embedding", alias="fixture")
        intents.observe(owner, identity, ModelUsage(cost=0.25))
        with authority(owner):
            store.add_embedding_call("p", "m", 1, 0.25, intent_id=identity)
            store.add_embedding_call("p", "m", 1, 0.25, intent_id=identity)
    store.add_embedding_call("p", "m", 1, 0.25)
    assert RunState(projects.control).model_costs("p") == {"embeddings": 0.75}
    with projects.control.read() as tx:
        assert tx.sql.execute("SELECT count(*) FROM skill_embedding_calls").fetchone() == (3,)


def test_link_failure_rolls_back_legacy_write_but_preserves_observed_charge(
    projects: ProjectStore,
) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    intents = ModelIntents(projects.control)
    identity = intents.begin(owner, kind="embedding", alias="fixture")
    intents.observe(owner, identity, ModelUsage(cost=0.25))
    with projects.control.write() as tx:
        tx.sql.execute(
            "CREATE TRIGGER refuse_materialization BEFORE INSERT ON research_observations "
            "WHEN new.kind='model_materialization' BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with authority(owner), pytest.raises(sqlite3.IntegrityError, match="fixture"):
        SkillStore(projects.control).add_embedding_call("p", "m", 1, 0.25, intent_id=identity)
    with projects.control.read() as tx:
        assert tx.sql.execute("SELECT count(*) FROM skill_embedding_calls").fetchone() == (0,)
    assert RunState(projects.control).model_costs("p") == {"embeddings": 0.25}


@pytest.mark.parametrize("storage", ["collection", "record", "event"])
def test_fold_copy_identity_survives_every_legacy_form(
    projects: ProjectStore, storage: str
) -> None:
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    intents = ModelIntents(projects.control)
    identity = intents.begin(owner, kind="fold", alias="fixture")
    usage = ModelUsage(cost=0.25, cost_source="model_info", input_tokens=10, output_tokens=2)
    intents.observe(owner, identity, usage)
    copy = usage.model_copy(update={"intent_id": identity}).model_dump(mode="json")
    if storage == "collection":
        Collection(projects.control, ("p", "s", RUN), "fold_calls").append(copy)
    elif storage == "record":
        projects.save_run(
            record(RUN).model_copy(update={"summaries": {"folds": {"calls": [copy]}}})
        )
    else:
        RunState(projects.control).append("p", "s", RUN, "fold", {"calls": [copy]})
    state = RunState(projects.control)
    assert state.model_costs("p") == {"main": 0.25}
    assert state.model_info_cost("p") == 0.25


async def test_runtime_auxiliary_identity_reaches_legacy_metadata_writer(
    projects: ProjectStore,
) -> None:
    from sandbox_science.orchestrator.confinement.models import CallbackModel, RuntimeModels
    from sandbox_science.orchestrator.confinement.transport import Capability, Channel
    from sandbox_science.orchestrator.confinement.wire import ModelIntent, ModelObserved
    from sandbox_science.orchestrator.model import ModelGateway, ModelTurn

    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    intents = ModelIntents(projects.control)

    class Gateway:
        def model(self, alias):
            return self

        def limited(self, **kwargs):
            return self

        def choosing(self, choice):
            return self

        async def complete(self, messages, tools):
            return ModelTurn("Metadata", cost=0.25, input_tokens=10, output_tokens=2)

    async def host(kind, value) -> JsonValue:
        if kind is Capability.MODEL_INTENT:
            request = ModelIntent.model_validate(value)
            return {"identity": intents.begin(owner, kind=request.kind, alias=request.alias)}
        assert kind is Capability.MODEL_OBSERVED
        request = ModelObserved.model_validate(value)
        intents.observe(owner, request.identity, request.usage)
        return None

    async def runtime(kind, value):
        assert kind is Capability.MODEL
        return await models.complete(value)

    left = Channel(lambda frame: right.receive(frame), host)
    right = Channel(lambda frame: left.receive(frame), runtime)
    models = RuntimeModels(cast(ModelGateway, Gateway()), frozenset({"fixture"}), right)
    try:
        callback = CallbackModel(left, "fixture", kind="skills", scope=lambda: "cap-" + "a" * 48)
        turn = await callback.complete([], [])
        assert turn.intent_id is not None
        assert RunState(projects.control).model_costs("p") == {"metadata": 0.25}
        with authority(owner):
            SkillStore(projects.control).add_metadata_call(
                "p", "snapshot", "m", (10, 2), 0.25, intent_id=turn.intent_id
            )
        assert RunState(projects.control).model_costs("p") == {"metadata": 0.25}
    finally:
        await left.close()
        await right.close()


@pytest.mark.parametrize("malformed", [False, True])
async def test_runtime_embedding_paid_identity_survives_charged_errors(
    projects: ProjectStore, malformed: bool
) -> None:
    from sandbox_science.orchestrator.confinement.embeddings import (
        CallbackEmbedder,
        RuntimeEmbeddings,
    )
    from sandbox_science.orchestrator.confinement.transport import Capability, Channel
    from sandbox_science.orchestrator.confinement.wire import ModelIntent, ModelObserved
    from sandbox_science.orchestrator.skills.embeddings import (
        Embedded,
        Embedder,
        EmbeddingError,
        PaidCall,
        Space,
        count_paid,
    )

    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    intents = ModelIntents(projects.control)

    class Peer(Embedder):
        def __init__(self):
            self.alias = "embed"

        async def embed(self, texts, paid=None):
            assert paid is not None
            paid.append(("model", len(texts), 0.25))
            if malformed:
                raise EmbeddingError("charged malformed embeddings")
            return Embedded(Space("model", 2), [[1.0, 0.0]], 0.25)

    async def host(kind, value) -> JsonValue:
        if kind is Capability.MODEL_INTENT:
            request = ModelIntent.model_validate(value)
            return {"identity": intents.begin(owner, kind=request.kind, alias=request.alias)}
        assert kind is Capability.MODEL_OBSERVED
        request = ModelObserved.model_validate(value)
        intents.observe(owner, request.identity, request.usage)
        return None

    async def runtime(kind, value):
        assert kind is Capability.EMBEDDING
        return await embeddings.embed(value)

    left = Channel(lambda frame: right.receive(frame), host)
    right = Channel(lambda frame: left.receive(frame), runtime)
    embeddings = RuntimeEmbeddings(Peer(), right)
    paid = []
    try:
        callback = CallbackEmbedder(left, "embed", lambda: "cap-" + "b" * 48)
        if malformed:
            with pytest.raises(EmbeddingError, match="charged malformed"):
                await callback.embed(["one"], paid)
        else:
            assert (await callback.embed(["one"], paid)).space == Space("model", 2)
        assert len(paid) == 1
        assert isinstance(paid[0], PaidCall)
        assert RunState(projects.control).model_costs("p") == {"embeddings": 0.25}
        with authority(owner):
            await count_paid(SkillStore(projects.control), "p", paid)
        assert RunState(projects.control).model_costs("p") == {"embeddings": 0.25}
    finally:
        await left.close()
        await right.close()
