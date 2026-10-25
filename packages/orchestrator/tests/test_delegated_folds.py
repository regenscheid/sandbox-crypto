# SPDX-License-Identifier: MIT
"""Children fold only their own archived evidence, using the existing engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.context.agent import AgentContext
from sandbox_science.orchestrator.context.folding import FoldPoint
from sandbox_science.orchestrator.context.history import Archive, Entry, geometry, session_view
from sandbox_science.orchestrator.context.history.delegated import delegated_folder
from sandbox_science.orchestrator.context.history.geometry import DEFAULT_SHARES
from sandbox_science.orchestrator.context.history.view import spanned
from sandbox_science.orchestrator.loop import Transcript
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.projects import ProjectStore

from .loop_harness import PROJECT, SESSION, Harness, Reply
from .loop_harness import gateway as gateway
from .loop_harness import harness as harness
from .loop_harness import worker as worker
from .test_delegation import CHECK, RESULT, children, wait
from .test_delegation import delegating as delegating
from .test_delegation_tasks import DONE, TASK
from .test_history_folds import CONTEXT


@pytest.mark.parametrize("narrative", [False, True])
@pytest.mark.parametrize("constraints", [None, "Use exact rational arithmetic."])
async def test_scoped_folds_survive_reopen_without_other_agents_context(
    tmp_path: Path, constraints: str | None, narrative: bool
) -> None:
    projects = ProjectStore(tmp_path)
    projects.create(PROJECT, "minimal", "sha256:" + "c" * 64)
    projects.open_session(PROJECT, SESSION, dedicated=False)
    archive = Archive(projects.control)
    brief = "Tabulate squares of the integers 1 through 10. Preserve exact values."
    forbidden = ("PARENT REASONING", "SIBLING CONCLUSION")

    class Narrator:
        calls = 0

        async def complete(self, messages, tools):
            sent = json.dumps(messages)
            assert all(text not in sent for text in forbidden)
            self.calls += 1
            return ModelTurn(
                json.dumps({"narrative": "Exact values remain in the archive.", "keys": []}),
                input_tokens=100,
                output_tokens=10,
            )

    narrator = Narrator()
    made = await delegated_folder(
        archive,
        CONTEXT,
        run="child",
        parent="root",
        brief=brief,
        constraints=constraints,
        ceiling=160_000,
        chunk=32_000,
        shares=DEFAULT_SHARES,
        model=narrator if narrative else None,
    )
    transcript = Transcript(
        context=AgentContext(
            messages=[{"role": "system", "content": "s" * 400}, *made.view.messages]
        )
    )
    first = None
    for batch in range(3):
        for n in range(200):
            archive.append(PROJECT, SESSION, "root", Entry("user", "user", forbidden[0]))
            archive.append(
                PROJECT, SESSION, "sibling", Entry("user", "task", forbidden[1]), parent="root"
            )
            row = archive.append(
                PROJECT,
                SESSION,
                "child",
                Entry("user", "user", f"Square fixture {batch}/{n}: " + "0123456789" * 400),
                parent="root",
            )
            first = first or row
            transcript.context.messages.append(row.chat())
        assert await made.before_call(FoldPoint(transcript.context, transcript.turns))
        assert made.geometry is not None
        assert made.tokens(FoldPoint(transcript.context, transcript.turns)) <= made.geometry.wall
        sent = json.dumps(transcript.context.sent)
        assert brief in sent
        assert all(text not in sent for text in forbidden)
        if constraints:
            assert constraints in sent
    assert len(made.folds) > 2
    if narrative:
        assert narrator.calls > 0
        assert any(fold["state"] == "applied" for fold in made.folds)
    own = archive.messages(PROJECT, SESSION, run="child")
    span = spanned(archive, CONTEXT, own[0].position, own[-1].position, run="child")
    assert span
    assert {row.run for row in span} == {"child"}
    projects.close()

    # Reconstruct from the persisted archive, without the original Folder/Transcript.
    projects = ProjectStore(tmp_path)
    try:
        archive = Archive(projects.control)
        rebuilt = await delegated_folder(
            archive,
            CONTEXT,
            run="child",
            parent="root",
            brief=brief,
            constraints=constraints,
            ceiling=160_000,
            chunk=32_000,
            shares=DEFAULT_SHARES,
        )
        resumed = Transcript(
            context=AgentContext(
                messages=[{"role": "system", "content": "s" * 400}, *rebuilt.view.messages]
            )
        )
        assert await rebuilt.before_call(FoldPoint(resumed.context, resumed.turns))
        assert brief in json.dumps(resumed.context.sent)
        assert all(text not in json.dumps(resumed.context.sent) for text in forbidden)
        assert first is not None
        assert archive.message(PROJECT, first.message_id).entry.content == first.entry.content
        main = session_view(archive, CONTEXT, geometry=geometry(160_000, 0, 32_000))
        assert main.records == ()  # child's folds were archived with its parent identity
        assert "Delegated work brief" not in json.dumps(main.messages)
    finally:
        projects.close()


@pytest.mark.parametrize(("task", "result"), [(TASK, DONE), (CHECK, RESULT)])
async def test_production_harness_folds_each_delegated_role_using_its_model_limit(
    delegating: Harness, task: tuple[str, dict[str, Any]], result: object
) -> None:
    delegating.gateway.limits = {
        "main": {"max_input_tokens": 600_000},
        "delegated": {"max_input_tokens": 120_000},
    }
    root = await delegating.run(
        "root",
        [Reply(calls=[task]), wait(), Reply("Done.")],
        delegated=[Reply(json.dumps(result))],
        options=["--delegated-model", "delegated", "--fold-chunk", "10000"],
    )
    (child,) = children(delegating, root)
    summary = child.summaries["folds"]
    assert isinstance(summary, dict)
    shape = summary["geometry"]
    assert isinstance(shape, dict)
    assert shape["ceiling"] == 120_000
    assert summary["calls"] == []
    assert child.limits is not None
    assert child.limits["max_input_chars"] is None


async def test_production_child_narrates_on_its_own_alias_and_grant(delegating: Harness) -> None:
    from .test_delegation import configure
    from .test_history_folds import folds

    configure(delegating, depth=1, task_tokens=2_000_000)
    delegating.gateway.per_token = 4.0
    delegating.gateway.enrich = [
        Reply(json.dumps({"narrative": "Exact child evidence remains available.", "keys": []}))
    ] * 8
    # Distinct answers: the same text again and again would be a loop check-in (D102).
    notes = [
        Reply(f"Child-only evidence {n}. " + "observation " * 9000, calls=[("plan_read", {})])
        for n in range(6)
    ]
    root = await delegating.run(
        "root",
        [Reply(calls=[TASK]), wait(), Reply("Done.")],
        delegated=[*notes, Reply(json.dumps(DONE))],
        task="PARENT PRIVATE REASONING",
        options=[
            # The history folds tests' ceiling and chunk, with room for the tools' growth: the
            # system message and tools may take a fifth of the ceiling (CM-27).
            "--context-ceiling",
            "114000",
            "--fold-chunk",
            "10000",
            "--delegated-model",
            "delegated",
            "--reasoning-effort",
            "fold=high",
            "--reasoning-effort",
            "overview=low",
        ],
    )
    (child,) = children(delegating, root)
    summary = folds(child)
    assert summary["calls_total"] > 0
    assert any(fold["state"] == "applied" for fold in summary["folds"])
    assert child.model_profiles["fold"].alias == "delegated"
    for sent in delegating.gateway.requests["enrich"]:
        assert "PARENT PRIVATE REASONING" not in json.dumps(sent)
        assert "Child-only evidence" in json.dumps(sent)
