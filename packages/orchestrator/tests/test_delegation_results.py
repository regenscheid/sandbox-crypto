# SPDX-License-Identifier: MIT
"""A delegated task's result files (research artifacts, RA-38; plans-and-delegation
spec, PD-17): the files the child declares as its results, checked against the
revisions its calls made, written or published, and pinned in its outcome, where the
parent can cite them; a file its calls did not make is refused, with one correction."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.delegation.outcome import SUBMIT, Outcome, parse_task
from sandbox_science.orchestrator.documents import USER
from sandbox_science.orchestrator.file_settings import DEFAULT_IGNORE, ProjectFiles
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.loop import Transcript

from .loop_harness import AREA, PROJECT, Harness, Reply, call
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import children, delegation, results, started, submitted, wait
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE
from .test_delegation_tracks import an_issue, worked

TASK = call("delegate", task="Tabulate a^16 mod 17 into table.txt.")
WRITE = call("write_file", path=f"{AREA}/table.txt", content="1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1\n")
RESULT = {**DONE, "result_files": ["table.txt"]}
WRITTEN = {**DONE, "result_files": [f"{AREA}/table.txt"]}
"""``RESULT`` as a delegated run gives it, naming the file in its write area (DA-19)."""


def listed(files: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    assert files is not None
    found = files[key]
    assert isinstance(found, list)
    return [item for item in found if isinstance(item, dict)]


def answer(record: Any, n: int) -> dict[str, Any]:
    found = json.loads(results(record)[n])
    return json.loads(found["tool_result"]) if "tool_result" in found else found


async def test_a_tasks_result_files_are_pinned_in_its_outcome(delegating: Harness) -> None:
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[Reply(calls=[WRITE]), *submitted(WRITTEN, "Done.")],
    )
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    (pinned,) = listed(found.files, "results")
    (recorded,) = listed(found.files, "files")
    assert pinned == {k: v for k, v in recorded.items() if k not in ("deleted", "reproduced")}
    assert (pinned["path"], pinned["revision"], pinned["content_retained"]) == (
        f"work/{delegation_id}/table.txt",
        1,
        True,
    )
    waited = answer(root, 1)["delegations"][0]
    # Its one recorded file is its result file: nothing else is counted or listed.
    assert list(waited)[-2:] == ["result", "result_files"]
    assert waited["result_files"] == [pinned]
    (child,) = children(delegating, root)
    outcome = child.summaries["outcome"]
    assert isinstance(outcome, dict)
    assert outcome["result_files"] == [pinned]


async def test_a_result_file_named_relative_to_the_write_area_is_pinned_at_once(
    delegating: Harness,
) -> None:
    """DA-19: the file named as the child's processes' working directory names it, without
    the write area's prefix, is recorded at the first submission, and the outcome, the
    stored result and the parent's view are those the full path gives."""

    async def ran(result: dict[str, Any]) -> tuple[str, Any, Any]:
        root = await delegating.run(
            "root",
            [Reply(calls=[TASK]), wait(), Reply("Done.")],
            delegated=[Reply(calls=[WRITE]), *submitted(result, "Done.")],
        )
        (delegation_id,) = started(root)
        (child,) = children(delegating, root)
        outcome = child.summaries["outcome"]
        assert isinstance(outcome, dict)
        assert (outcome["submissions"], outcome["errors"]) == (1, [])
        found = delegation(delegating, delegation_id)
        (view,) = [
            ended
            for ended in answer(root, 1)["delegations"]
            if ended["delegation_id"] == delegation_id
        ]
        return delegation_id, found, view

    relative_id, relative, relative_view = await ran(RESULT)
    full_id, full, full_view = await ran(WRITTEN)

    def same(value: Any, delegation_id: str) -> str:
        return json.dumps(value, sort_keys=True).replace(delegation_id, "<id>")

    assert relative.result is not None
    assert relative.result["result_files"] == [f"work/{relative_id}/table.txt"]
    assert same(relative.result, relative_id) == same(full.result, full_id)
    (pinned,) = listed(relative.files, "results")
    assert pinned["path"] == f"work/{relative_id}/table.txt"
    assert relative_view["result_files"] == [pinned]
    assert same(relative_view["result"], relative_id) == same(full_view["result"], full_id)


async def test_a_file_the_child_did_not_make_is_refused_once(delegating: Harness) -> None:
    """Its first submission names a file its calls did not write; the answer to it lists
    only the file they wrote."""
    wrong = {**DONE, "result_files": [f"{AREA}/table.txt", "missing.txt"]}
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[WRITE]),
            Reply(calls=[call(SUBMIT, **wrong)]),
            *submitted(WRITTEN, "Done."),
        ],
    )
    (child,) = children(delegating, root)
    outcome = child.summaries["outcome"]
    assert isinstance(outcome, dict)
    errors = outcome["errors"]
    assert isinstance(errors, list)
    (error,) = errors
    assert "wrote or published no file missing.txt" in str(error)
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    assert [r["path"] for r in listed(found.files, "results")] == [
        f"work/{delegation_id}/table.txt"
    ]


async def test_a_deleted_file_is_no_result() -> None:
    """A file a later call deleted has a tombstone as its latest revision: no result."""
    outcome = Outcome(task=True)
    run = Transcript(context=AgentContext(messages=[]))
    entry = {"path": "table.txt", "artifact_id": "artifact-1", "revision": 1, "sha256": "ab" * 32}
    for deleted in (False, True):
        files = [
            {**entry, "revision": 1 + deleted, "deleted": deleted, "content_retained": not deleted}
        ]
        await outcome.observe_tool(run, {"recorded_files": {"files": files}})
    text, _ = await outcome.submit(json.dumps(RESULT))
    assert "error" in json.loads(text)
    assert outcome.result is None
    assert "wrote or published no file table.txt" in outcome.errors[0]


@pytest.mark.parametrize("several", [False, True])
async def test_a_published_file_is_a_result_in_a_results_only_project(
    delegating: Harness, several: bool
) -> None:
    """RA-39: a project that records results only records none of a call's files; the
    child publishes its result, which is pinned from its publication, whether the call
    named it as its ``path`` or among its ``paths`` (W2)."""
    documents = delegating.documents()
    try:
        change = {"expected": 0, "recording": "results", "ignore": list(DEFAULT_IGNORE)}
        ProjectFiles(documents).change(PROJECT, {**change, "operation_id": "o1"}, USER)
    finally:
        documents.close()
    table = f"{AREA}/table.txt"
    published = (
        call("publish", paths=[f"{AREA}/absent.txt", table])
        if several
        else call("publish", path=table)
    )
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[
            Reply(calls=[WRITE]),
            Reply(calls=[published]),
            *submitted(WRITTEN, "Done."),
        ],
    )
    (delegation_id,) = started(root)
    found = delegation(delegating, delegation_id)
    (pinned,) = listed(found.files, "results")
    assert (pinned["path"], pinned["revision"]) == (f"work/{delegation_id}/table.txt", 1)
    assert found.files is not None
    assert found.files["total"] == 0


async def test_an_issues_outcome_cites_the_result_files(delegating: Harness) -> None:
    issue = an_issue(delegating)
    task = call("delegate", task="Tabulate it.", issue=issue)
    root = await delegating.run(
        "root",
        [Reply(calls=[task]), wait(), Reply("Done.")],
        delegated=[Reply(calls=[WRITE]), *submitted(WRITTEN, "Done.")],
    )
    (delegation_id,) = started(root)
    (outcome,) = worked(delegating, issue)
    cited = [r for r in outcome.references if "artifact" in r]
    assert [(r["revision"], r["note"]) for r in cited] == [(1, f"work/{delegation_id}/table.txt")]
    documents = delegating.documents()
    try:
        assert Ledger(documents).entry(PROJECT, issue).status == "open"
    finally:
        documents.close()


@pytest.mark.parametrize(
    ("files", "error"),
    [
        (["a.txt", "a.txt"], "listed once"),
        ([f"f{n}.txt" for n in range(51)], "at most 50"),
        (["a‮.txt"], "result_files.0"),
    ],
)
def test_result_files_are_checked(files: list[str], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_task(json.dumps({**DONE, "result_files": files}))
