# SPDX-License-Identifier: MIT
"""Spend and tokens per project, session, run and role, and cost notices (D102).

Accounting stops nothing: a crossed notice is recorded once and paid calls go on.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sandbox_science.orchestrator.accounting import account
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.usage import AnsweredUsage
from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.projects import ModelUsage
from sandbox_science.orchestrator.runstate import RunState

from .test_resource_requests import setup
from .test_tool_journal import RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD


def answered(
    projects, run: str, ordinal: int, *, cost: float | None, tokens: int, model: str | None = None
) -> None:
    Archive(projects.control).append(
        "p",
        "s",
        run,
        Entry("assistant", "assistant", f"answer {ordinal}"),
        model_usage=AnsweredUsage(
            ordinal=ordinal,
            observed_at=datetime.now(UTC),
            usage=ModelUsage(model=model, input_tokens=tokens, output_tokens=0, cost=cost),
        ),
        parent=RUN if run == CHILD else None,
    )


def test_usage_is_accounted_per_session_run_and_role_the_reviewer_included(projects):
    _, _, child, _ = setup(projects)
    answered(projects, RUN, 1, cost=2.0, tokens=1000)
    answered(projects, RUN, 2, cost=None, tokens=500)  # unpriced: counted, never as zero
    answered(projects, CHILD, 1, cost=1.0, tokens=300)
    intents = ModelIntents(projects.control)
    for kind, cost in (("reviewer", 0.5), ("fold", 0.25)):
        identity = intents.begin(child, kind=kind, alias="fixture")
        intents.observe(child, identity, ModelUsage(input_tokens=40, output_tokens=10, cost=cost))
    found = account(RunState(projects.control), "p")
    assert found.total.cost == pytest.approx(3.75)
    assert found.total.unpriced == 1
    roles = {item.role: item for item in found.total.by_role}
    assert [item.role for item in found.total.by_role] == ["main", "delegated", "reviewer"]
    assert (roles["main"].cost, roles["main"].tokens, roles["main"].calls) == (2.0, 1500, 2)
    assert roles["delegated"].cost == pytest.approx(1.25)  # its answer and its fold
    assert (roles["reviewer"].cost, roles["reviewer"].tokens) == (0.5, 50)
    assert [item.run for item in found.runs] == [RUN, CHILD]
    assert [item.session for item in found.sessions] == ["s"]
    scoped = account(RunState(projects.control), "p", "s").total
    assert scoped == found.total.model_copy(update={"session": "s"})
    assert account(RunState(projects.control), "p", "other").total.calls == 0
    assert found.daily_cost_limit is None


def test_usage_by_model_names_each_model_and_leaves_unnamed_ones_unknown(projects):
    # The Monitor view's spend by model (D103), from the same sources as the costs.
    _, _, child, _ = setup(projects)
    answered(projects, RUN, 1, cost=2.0, tokens=1000, model="gpt-x")
    answered(projects, RUN, 2, cost=None, tokens=500, model="gpt-x")
    answered(projects, CHILD, 1, cost=1.0, tokens=300)  # the gateway named no model
    intents = ModelIntents(projects.control)
    identity = intents.begin(child, kind="reviewer", alias="fixture")
    usage = ModelUsage(model="small", input_tokens=40, output_tokens=10, cost=0.5)
    intents.observe(child, identity, usage)
    rows = RunState(projects.control).usage_by_model("p")
    found = {
        (role, model): (cost, tokens, calls, unpriced)
        for role, model, cost, tokens, calls, unpriced in rows
    }
    assert found == {
        ("main", "gpt-x"): (2.0, 1500, 2, 1),
        ("delegated", None): (1.0, 300, 1, 0),
        ("reviewer", "small"): (0.5, 50, 1, 0),
    }
    later = datetime.now(UTC).replace(year=datetime.now(UTC).year + 1)
    assert RunState(projects.control).usage_by_model("p", later) == []


def test_cost_notices_are_recorded_once_and_never_stop(projects):
    _, parent, _, _ = setup(projects)
    runs = RunState(projects.control)
    runs.set_cost_notices("p", [1.0, 10.0])
    assert runs.noticed_projects() == ["p"]
    assert runs.note_cost_crossings("p") == []
    answered(projects, RUN, 1, cost=2.0, tokens=10)
    assert runs.note_cost_crossings("p") == [1.0]
    assert runs.note_cost_crossings("p") == []
    notices = account(runs, "p").notices
    assert [(n.threshold, n.spent) for n in notices] == [(1.0, 2.0), (10.0, None)]
    assert notices[0].crossed_at is not None
    assert ModelIntents(projects.control).begin(parent, kind="main", alias="fixture")
    runs.set_cost_notices("p", [10.0])  # a kept threshold keeps its state
    assert [n.threshold for n in account(runs, "p").notices] == [10.0]
    with pytest.raises(ControlError, match="at most 20 thresholds"):
        runs.set_cost_notices("p", [-1.0])
    runs.set_cost_notices("p", ())
    assert runs.noticed_projects() == []
