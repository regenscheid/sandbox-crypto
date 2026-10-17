# SPDX-License-Identifier: MIT
"""A continuation consumes its work's child outcomes once across parent attempts."""

from datetime import UTC, datetime

import pytest

from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.delegation.hook import Delegations
from sandbox_science.orchestrator.delegation.records import Delegation, DelegationRecords, Ending
from sandbox_science.orchestrator.documents import DocumentStore
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.usage import AnsweredUsage
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.runstate import RunState

from .test_model_usage_journal import START, USAGE
from .test_tool_journal import CONTEXT, RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import CONTINUATION, record

OTHER = "20261018T030000000000Z-other"


def prepare(projects: ProjectStore) -> tuple[DelegationRecords, Delegation, Delegation]:
    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    works.finish(record(RUN, stopped="turn_limit"))
    request = works.request("p", "s")
    RunState(projects.control).acquire("p", "s", CONTINUATION, "fixture")
    _, epoch = works.claim("p", "s", request, lease_run=CONTINUATION)
    works.admit("p", "s", CONTINUATION, work=RUN, predecessor=RUN, epoch=epoch)
    works.admit("p", "s", OTHER)
    records = DelegationRecords(projects.control)
    made = []
    for index, parent in enumerate((RUN, OTHER)):
        delegation = Delegation(
            delegation_id=f"dlg-{index:024x}",
            parent=parent,
            run=f"20261018T03000{index + 1}000000Z-child-{index}",
            kind="check",
            brief={"task": "Check."},
            started_at=datetime.now(UTC),
        )
        records.start("p", "s", delegation)
        made.append(records.end("p", "s", delegation.delegation_id, Ending("returned", "answered")))
    return records, made[0], made[1]


@pytest.mark.parametrize("consumption", ["receipt", "prior_receipt", "acknowledgement"])
def test_handback_is_scoped_to_parent_work_and_received_once(
    projects: ProjectStore, consumption: str
) -> None:
    records, own, sibling = prepare(projects)
    # A normal later root can acknowledge PD-35 outcomes; that cannot consume them
    # on behalf of the explicitly resumed responsibility.
    if consumption != "acknowledgement":
        records.acknowledge("p", "s", [own.delegation_id], OTHER)
    Archive(projects.control).append(
        "p",
        "s",
        OTHER,
        Entry("assistant", "assistant", "Other root read it"),
        model_usage=AnsweredUsage(ordinal=1, observed_at=START, usage=USAGE),
        consumed={"delegations": {own.delegation_id}},
    )
    assert records.handbacks("p", "s") == (
        [own, sibling] if consumption == "acknowledgement" else [sibling]
    )
    assert records.handbacks("p", "s", work=RUN)[0].delegation_id == own.delegation_id
    assert records.handbacks("p", "s", work="missing") == []
    if consumption in ("receipt", "prior_receipt"):
        Archive(projects.control).append(
            "p",
            "s",
            RUN if consumption == "prior_receipt" else CONTINUATION,
            Entry("assistant", "assistant", "Parent incorporated it"),
            model_usage=AnsweredUsage(ordinal=1, observed_at=START, usage=USAGE),
            consumed={"delegations": {own.delegation_id}},
        )
    else:
        records.acknowledge(
            "p", "s", [own.delegation_id, sibling.delegation_id], CONTINUATION, work=RUN
        )
    projects.control.close()
    assert records.handbacks("p", "s", work=RUN) == []
    assert records.handbacks("p", "s", work=OTHER)[0].delegation_id == sibling.delegation_id


async def test_continuation_hook_offers_only_prior_attempt_children(projects: ProjectStore) -> None:
    records, own, _ = prepare(projects)

    async def unused(child):
        raise AssertionError("no child may be started by a handback read")

    hook = Delegations(
        records,
        Ledger(DocumentStore(projects.control.path)),
        CONTEXT,
        parent=CONTINUATION,
        work=RUN,
        runner=unused,
        max_turns=10,
        offer=False,
    )
    notices = await hook.notices("start", Transcript(messages=[]))
    assert len(notices) == 1
    assert notices[0].ids == (own.delegation_id,)
