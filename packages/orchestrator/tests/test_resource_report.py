# SPDX-License-Identifier: MIT
"""The usage view's reads (D102): a session's soft caps with their use, its check-ins
counted by how they went, and usage at its finest grain. All of them read only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sandbox_science.orchestrator.continuity.models import ModelIntents
from sandbox_science.orchestrator.continuity.resource_report import (
    check_in_counts,
    merge_counts,
    work_caps,
)
from sandbox_science.orchestrator.continuity.resources import (
    CheckInSummary,
    ResourceApprovalRequest,
)
from sandbox_science.orchestrator.delegation.records import DelegationRecords
from sandbox_science.orchestrator.projects import ModelUsage
from sandbox_science.orchestrator.runstate import RunState

from .test_accounting import answered
from .test_resource_requests import decide, setup
from .test_tool_journal import RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CHILD


def observations(projects) -> int:
    with projects.control.read() as tx:
        return tx.sql.execute("SELECT count(*) FROM research_observations").fetchone()[0]


def caps_of(found, work):
    return {
        cap.resource: (cap.used, cap.total)
        for item in found
        if item.work == work
        for cap in item.caps
    }


def test_the_caps_read_as_the_resource_hook_pins_them_and_reading_writes_nothing(projects):
    _, parent, _, requests = setup(projects, turns=5, tokens=20_000_000)
    answered(projects, RUN, 1, cost=2.0, tokens=1000)
    answered(projects, CHILD, 1, cost=1.0, tokens=300)
    before = observations(projects)
    found = work_caps(projects.control, "p", "s")
    assert observations(projects) == before  # no allowance pinned, no time sampled
    assert [item.work for item in found] == [RUN, CHILD]  # the root first
    assert [(item.role, item.parent, item.state) for item in found] == [
        ("main", None, "running"),
        ("delegated", RUN, "running"),
    ]
    assert found[1].task == "Derive and verify a lemma"
    assert found[1].runs == (CHILD,)
    # A child's tokens and turns are its blueprint's; a root's turn limit is unknown until
    # the hook pins it; time and cost are the settings' own.
    assert caps_of(found, CHILD) == {
        "tokens": (300, 20_000_000),
        "turns": (1, 5),
        "elapsed_seconds": (0, 14_400),
        "cost": (100, 2_500),
    }
    assert caps_of(found, RUN)["turns"] == (1, None)
    assert caps_of(found, RUN)["tokens"] == (1000, 100_000_000)
    # What the hook pins is what the report then reads.
    point = requests.safe_point(parent, seconds=30, turns=2000, tokens=None)
    pinned = caps_of(work_caps(projects.control, "p", "s"), RUN)
    assert {resource: total for resource, total, _ in point.caps} == {
        resource: total for resource, (_, total) in pinned.items()
    }
    assert pinned["elapsed_seconds"] == (30, 14_400)


def test_an_extension_raises_the_cap_it_reports_and_an_off_cap_is_null(projects):
    _, parent, child, requests = setup(projects, turns=5)
    warning = requests.observe(child, "turns", used=5, total=5, revision=0, force=True)
    assert warning is not None
    decide(requests, parent, warning)  # a child's turn cap grows by one step of 300
    assert caps_of(work_caps(projects.control, "p", "s"), CHILD)["turns"] == (0, 305)
    DelegationRecords(projects.control).configure("p", soft_cost=None, research_seconds=None)
    caps = caps_of(work_caps(projects.control, "p", "s"), CHILD)
    assert caps["cost"][1] is None
    assert caps["elapsed_seconds"][1] is None


def loop_in(requests, owner, rule, *, revision=0):
    return requests.observe(
        owner,
        "loop",
        used=8,
        total=8,
        revision=revision,
        force=True,
        summary=CheckInSummary(rule=rule, detail="8 calls"),
    )


def test_check_ins_are_counted_by_resource_rule_answerer_and_action(projects):
    works, parent, child, requests = setup(projects)
    first = loop_in(requests, child, "same_call")
    second = loop_in(requests, child, "same_error", revision=1)
    turns = requests.observe(child, "turns", used=5, total=5, revision=0, force=True)
    assert first is not None
    assert second is not None
    assert turns is not None
    counts = check_in_counts(projects.control, "p")["s"]
    assert (counts.total, counts.waiting) == (3, 3)
    assert counts.by_resource == {"loop": 2, "turns": 1}
    assert counts.loops == {"same_call": 1, "same_error": 1}
    assert counts.answered_by == {"none": 3}
    decide(requests, parent, first, action="continue")
    requests.self_check(child, second, ask_user=False)
    counts = check_in_counts(projects.control, "p")["s"]
    assert counts.answered_by == {"parent": 1, "automatic": 1, "none": 1}
    assert counts.actions == {"continue": 1, "extend": 1}
    assert counts.waiting == 1
    # Work that ended no longer waits for anyone.
    works.stop("p", "s", CHILD)
    assert check_in_counts(projects.control, "p")["s"].waiting == 0
    # The scope and the window narrow the count.
    assert check_in_counts(projects.control, "p", session="other") == {}
    later = datetime.now(UTC) + timedelta(days=1)
    assert check_in_counts(projects.control, "p", since=later) == {}
    assert merge_counts(list(check_in_counts(projects.control, "p").values())).total == 3


def test_a_users_decision_counts_as_the_users(projects):
    _, parent, _, requests = setup(projects)
    warning = loop_in(requests, parent, "no_progress")
    assert warning is not None
    with projects.control.write() as tx:
        tx.sql.execute("UPDATE research_work SET state='paused'")
    requests.approve(
        "p",
        "s",
        ResourceApprovalRequest(
            request_id=warning.request_id,
            action="extend",
            reason="The plan still advances",
            current=RUN,
            epoch=1,
            total=8,
        ),
        operator="user",
    )
    counts = check_in_counts(projects.control, "p")["s"]
    assert counts.answered_by == {"user": 1}
    assert counts.actions == {"extend": 1}
    assert counts.waiting == 0


def test_usage_breakdown_names_the_run_role_and_model_and_honours_the_window(projects):
    _, _, child, _ = setup(projects)
    answered(projects, RUN, 1, cost=2.0, tokens=1000, model="big")
    answered(projects, RUN, 2, cost=None, tokens=500, model="big")
    answered(projects, CHILD, 1, cost=1.0, tokens=300)
    intents = ModelIntents(projects.control)
    identity = intents.begin(child, kind="reviewer", alias="fixture")
    intents.observe(
        child, identity, ModelUsage(model="small", input_tokens=40, output_tokens=10, cost=0.5)
    )
    runs = RunState(projects.control)
    found = {row[:4]: row[4:] for row in runs.usage_breakdown("p")}
    assert found == {
        ("s", RUN, "main", "big"): (2.0, 1500, 2, 1),
        ("s", CHILD, "delegated", None): (1.0, 300, 1, 0),
        ("s", CHILD, "reviewer", "small"): (0.5, 50, 1, 0),
    }
    assert runs.usage_breakdown("p", "other") == []
    later = datetime.now(UTC) + timedelta(days=1)
    assert runs.usage_breakdown("p", since=later) == []
    assert runs.usage_rows("p", since=later) == []
