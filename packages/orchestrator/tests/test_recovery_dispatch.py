# SPDX-License-Identifier: MIT
"""Crash windows preserve dispatch uncertainty and child admission inputs."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from sandbox_science.orchestrator.continuity.blueprints import Blueprint, Blueprints, reserve_child
from sandbox_science.orchestrator.continuity.models import IntentModel, ModelIntents
from sandbox_science.orchestrator.continuity.records import WorkRecords, authority
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.usage import AnsweredUsage
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ModelUsage, ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD, CONTINUATION


def blueprint() -> Blueprint:
    return Blueprint(context=CONTEXT, request={"project": "p", "session": "s", "task": "Compute"})


async def test_model_intent_survives_until_assistant_and_usage_commit(projects: ProjectStore):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    intents = ModelIntents(projects.control)
    wrapped = IntentModel(ScriptedModel([ModelTurn("Done")]), intents, owner, "fixture")
    response = await wrapped.complete([], [])
    assert response.intent_id is not None
    assert intents.pending("p", "s", RUN) == (response.intent_id,)
    projects.control.close()
    archive = Archive(projects.control)
    with pytest.raises(ControlError):
        archive.append(
            "p",
            "s",
            RUN,
            Entry(role="assistant", kind="assistant", content="Done"),
            model_usage=AnsweredUsage(
                ordinal=1,
                observed_at=datetime.now(UTC),
                usage=ModelUsage(cost=1),
                intent_id="wrong",
            ),
        )
    assert not archive.messages("p", "s", run=RUN)
    archive.append(
        "p",
        "s",
        RUN,
        Entry(role="assistant", kind="assistant", content="Done"),
        model_usage=AnsweredUsage(
            ordinal=1,
            observed_at=datetime.now(UTC),
            usage=ModelUsage(cost=1),
            intent_id=response.intent_id,
        ),
    )
    assert intents.pending("p", "s", RUN) == ()
    with pytest.raises(ControlError, match="no pending intent"):
        intents.observe(owner, response.intent_id, ModelUsage(cost=0))


async def test_model_failure_and_stale_attempt_remain_uncertain(projects: ProjectStore):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    intents = ModelIntents(projects.control)
    identity = intents.begin(owner, kind="delegated", alias="fixture")
    works.interrupt("p", "s", RUN)
    with pytest.raises(ControlError, match="not running"):
        intents.begin(owner, kind="main", alias="fixture")
    works.admit("p", "s", CONTINUATION)
    with pytest.raises(ControlError, match="lost ownership"):
        intents.observe(owner, identity, ModelUsage(cost=0))
    assert intents.pending("p", "s", RUN) == (identity,)


def test_child_inputs_and_ownership_commit_before_task_scheduling(projects: ProjectStore):
    works = WorkRecords(projects.control)
    owner = works.admit("p", "s", RUN)
    with authority(owner), projects.control.write() as tx:
        reserve_child(tx.sql, "p", "s", CHILD, RUN, blueprint())
    projects.control.close()
    assert works.get("p", "s", CHILD).state == "waiting"
    assert Blueprints(projects.control).get("p", "s", CHILD) == blueprint()
    child_owner = works.admit("p", "s", CHILD, parent=RUN)
    assert child_owner.epoch == owner.epoch
    assert works.get("p", "s", CHILD).state == "running"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), projects.control.write() as tx:
        tx.sql.execute("UPDATE research_blueprints SET snapshot='{}'")


def test_continuation_archive_sees_own_predecessor_only(projects: ProjectStore):
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.admit("p", "s", CHILD, parent=RUN)
    archive = Archive(projects.control)
    archive.append("p", "s", RUN, Entry(role="assistant", kind="assistant", content="Root"))
    archive.append(
        "p", "s", CHILD, Entry(role="assistant", kind="assistant", content="Child"), parent=RUN
    )
    works.interrupt("p", "s", RUN)
    request = works.request("p", "s")
    assert request
    RunState(projects.control).acquire("p", "s", CONTINUATION, "fixture")
    _, epoch = works.claim("p", "s", request, lease_run=CONTINUATION)
    works.admit("p", "s", CONTINUATION, work=CHILD, predecessor=CHILD, epoch=epoch)
    chain = Archive(projects.control, chain=True)
    assert [m.entry.content for m in chain.messages("p", "s", run=CONTINUATION)] == ["Child"]
    assert not archive.messages("p", "s", run=CONTINUATION)
