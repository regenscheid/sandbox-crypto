# SPDX-License-Identifier: MIT
"""Starting inputs survive absent final records; failed persistence prevents dispatch."""

from __future__ import annotations

import io
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.history.export import EvidenceExport
from sandbox_science.orchestrator.history.verify_export import verify
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.run_inputs import (
    INPUT_BYTES,
    RecordInputs,
    RunInputs,
    StartingInputs,
)
from sandbox_science.orchestrator.tools import ToolOutcome

from .control_versions import older_store
from .test_evidence_export import decode
from .test_tool_journal import CALL, CONTEXT, RUN, toolbox
from .test_tool_journal import projects as projects


@pytest.mark.parametrize("delegated", [False, True])
async def test_starting_inputs_commit_before_model_and_tool_calls_without_final_save(
    projects: ProjectStore, tmp_path: Path, delegated: bool
) -> None:
    inputs = RunInputs(projects.control)
    calls = []
    role = "delegated" if delegated else "main"
    request = {
        "project": "p",
        "session": "s",
        "task": "Compute",
        "alias": "fixture",
        "parent": "20261016T020000000000Z-parent" if delegated else None,
        "delegation": "dlg-1" if delegated else None,
    }

    class Model(ScriptedModel):
        async def complete(self, messages, tools):
            assert inputs.get("p", "s", RUN).request == request
            calls.append("model")
            return await super().complete(messages, tools)

    async def arithmetic(_):
        assert inputs.get("p", "s", RUN).pins.role == role
        calls.append("tool")
        return ToolOutcome("4", {"result": 4})

    hook = RecordInputs(inputs, CONTEXT, RUN, request, {"ceiling": 600000})
    result = await run_task(
        Model([ModelTurn(None, tool_calls=(CALL,)), ModelTurn("Done")]),
        toolbox(arithmetic),
        "Compute",
        run_id="run-1",
        hooks=(hook,),
        role=role,
    )
    assert calls == ["model", "tool", "model"]
    assert result.stopped == "answered"
    original = inputs.get("p", "s", RUN)
    assert original.pins == result.prompt_provenance
    assert projects.runs("p", "s") == []
    projects.close()
    reopened = ProjectStore(tmp_path)
    try:
        assert RunInputs(reopened.control).get("p", "s", RUN) == original
        encoded = list(EvidenceExport(reopened.control, "p", "s", RUN).lines())
        assert verify(io.BytesIO(b"".join(encoded))).format == "sandbox-run-evidence-v4"
        _, rows = decode(encoded)
        assert rows == [
            (
                "run_inputs",
                {
                    "project": "p",
                    "session": "s",
                    "run": RUN,
                    "snapshot": original.model_dump_json(),
                },
            )
        ]
        for project, session, run in (
            ("other", "s", RUN),
            ("p", "other", RUN),
            ("p", "s", RUN + "x"),
        ):
            with pytest.raises(ControlError, match="no saved starting inputs"):
                RunInputs(reopened.control).get(project, session, run)
        with pytest.raises(ControlError, match="already exist"):
            RunInputs(reopened.control).save(original)
        for sql in ("DELETE FROM run_inputs", "UPDATE run_inputs SET snapshot=snapshot"):
            with (
                pytest.raises(sqlite3.IntegrityError, match="written once"),
                reopened.control.write() as tx,
            ):
                tx.sql.execute(sql)
        with pytest.raises(ControlError, match="1 MiB"):
            RunInputs(reopened.control).save(
                original.model_copy(
                    update={
                        "run": RUN + "new",
                        "request": {**request, "task": "x" * INPUT_BYTES},
                    }
                )
            )
        fields = original.model_dump(mode="json")
        fields["request"]["project"] = "other"
        with pytest.raises(ValidationError, match="own project"):
            StartingInputs.model_validate(fields)
        fields["request"]["project"] = "p"
        fields["request"]["history"] = []
        with pytest.raises(ValidationError, match="process-only"):
            StartingInputs.model_validate(fields)
        # The mandatory inputs prove the parent existed even if its best-effort
        # started event failed. Its child's partial record must still be retainable.
        when = datetime(2026, 10, 16, 4, tzinfo=UTC)
        child = RunRecord(
            run_id="child",
            context=CONTEXT,
            role="delegated",
            parent=RUN,
            delegation="dlg-child",
            task="Part",
            started_at=when,
            ended_at=when,
            stopped="interrupted",
            messages=[],
            tools=[],
        )
        entry = reopened.save_run(child)
        assert reopened.run("p", "s", entry.name) == child
    finally:
        reopened.close()


async def test_failed_starting_input_write_prevents_every_model_and_tool_call(
    projects: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(self, inputs):
        raise sqlite3.OperationalError("fixture disk I/O error")

    monkeypatch.setattr(RunInputs, "save", unavailable)

    class Never(ScriptedModel):
        async def complete(self, messages, tools):
            pytest.fail("model dispatch after failed persistence")

    async def never(_):
        pytest.fail("tool dispatch after failed persistence")

    transcript = await run_task(
        Never([]),
        toolbox(never),
        "Compute",
        run_id="run-1",
        hooks=(
            RecordInputs(
                RunInputs(projects.control),
                CONTEXT,
                RUN,
                {
                    "project": "p",
                    "session": "s",
                    "task": "Compute",
                },
                {},
            ),
        ),
    )
    assert transcript.stopped == "error"
    assert transcript.turns == 0
    with pytest.raises(ControlError, match="no saved starting inputs"):
        RunInputs(projects.control).get("p", "s", RUN)


def test_migration_does_not_invent_starting_inputs(projects: ProjectStore, tmp_path: Path) -> None:
    target = tmp_path / "old.sqlite3"
    older_store(projects.control.path, target, 58)
    store = ControlStore(target)
    try:
        store.initialize()
        with pytest.raises(ControlError, match="no saved starting inputs"):
            RunInputs(store).get("p", "s", RUN)
    finally:
        store.close()


async def test_failed_preflight_retry_still_compares_nearest_same_work_pins(
    projects: ProjectStore,
) -> None:
    from sandbox_science.orchestrator.continuity.records import WorkRecords

    store = RunInputs(projects.control)
    WorkRecords(projects.control).admit("p", "s", RUN)
    request: dict[str, JsonValue] = {"project": "p", "session": "s", "task": "Compute"}

    async def never(_):
        pytest.fail("unexpected tool dispatch")

    original = await run_task(
        ScriptedModel([ModelTurn("Original")]),
        toolbox(never),
        "Compute",
        run_id="original",
        system="Pinned system",
        hooks=(RecordInputs(store, CONTEXT, RUN, request, {}),),
    )
    assert original.stopped == "answered"
    failed = "20261017T030000000000Z-failed-preflight"
    retry = "20261017T030001000000Z-retry-preflight"
    with projects.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO research_attempts VALUES (?,?,?,?,?,1,NULL)", ("p", "s", failed, RUN, RUN)
        )
        tx.sql.execute(
            "INSERT INTO research_attempts VALUES (?,?,?,?,?,1,NULL)",
            ("p", "s", retry, RUN, failed),
        )

    class Never(ScriptedModel):
        async def complete(self, messages, tools):
            pytest.fail("changed pins reached model dispatch after failed predecessor")

    for name, predecessor in ((failed, RUN), (retry, failed)):
        result = await run_task(
            Never([]),
            toolbox(never),
            "Compute",
            run_id=name,
            system="Changed system",
            hooks=(RecordInputs(store, CONTEXT, name, request, {}, predecessor=predecessor),),
        )
        assert result.failure == "harness"
        with pytest.raises(ControlError, match="no saved starting inputs"):
            store.get("p", "s", name)
    ancestor = store.previous("p", "s", retry)
    assert ancestor is not None
    assert ancestor.run == RUN
    with pytest.raises(ControlError, match="no saved ancestor"):
        store.previous("other", "s", retry)
    continued = "20261017T030002000000Z-compatible"
    result = await run_task(
        ScriptedModel([ModelTurn("Continued")]),
        toolbox(never),
        "Compute",
        run_id=continued,
        system="Pinned system",
        hooks=(RecordInputs(store, CONTEXT, continued, request, {}, predecessor=retry),),
    )
    assert result.stopped == "answered"
    assert store.get("p", "s", continued).pins == store.get("p", "s", RUN).pins


@pytest.mark.parametrize("evidence", ["intent", "archive", "event", "record"])
def test_missing_pin_ancestor_allows_only_provably_never_started_work(
    projects: ProjectStore,
    evidence: str,
) -> None:
    from sandbox_science.orchestrator.continuity.models import ModelIntents
    from sandbox_science.orchestrator.continuity.records import WorkRecords
    from sandbox_science.orchestrator.history import Archive, Entry
    from sandbox_science.orchestrator.runstate import RunState

    from .test_work_ownership import record

    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    store = RunInputs(projects.control)
    assert store.previous("p", "s", RUN) is None
    if evidence == "intent":
        ModelIntents(projects.control).begin(owner, kind="main", alias="fixture")
    elif evidence == "archive":
        Archive(projects.control).append(
            "p", "s", RUN, Entry(role="user", kind="task", content="Old")
        )
    elif evidence == "event":
        RunState(projects.control).append("p", "s", RUN, "started", {})
    else:
        projects.save_run(record(RUN))
    with pytest.raises(ControlError, match="no saved ancestor"):
        store.previous("p", "s", RUN)


@pytest.mark.parametrize("evidence", ["assistant", "tool", "model", "intent"])
def test_never_started_proof_does_not_ignore_active_dispatch_evidence(
    projects: ProjectStore,
    evidence: str,
) -> None:
    from sandbox_science.orchestrator.continuity.models import ModelIntents
    from sandbox_science.orchestrator.continuity.records import WorkRecords
    from sandbox_science.orchestrator.history import Archive, Entry
    from sandbox_science.orchestrator.runstate import RunState

    works = WorkRecords(projects.control)
    works.admit("p", "s", RUN)
    active = "20261017T040000000000Z-new-attempt"
    with projects.control.write() as tx:
        tx.sql.execute(
            "INSERT INTO research_attempts VALUES (?,?,?,?,?,1,NULL)", ("p", "s", active, RUN, RUN)
        )
        tx.sql.execute(
            "UPDATE research_work SET current=? WHERE project='p' AND session='s' AND work=?",
            (active, RUN),
        )
    archive = Archive(projects.control)
    archive.append("p", "s", active, Entry(role="user", kind="task", content="Initial task"))
    events = RunState(projects.control)
    events.append("p", "s", active, "started", {})
    store = RunInputs(projects.control)
    assert store.previous("p", "s", RUN, active_run=active) is None
    with pytest.raises(ControlError, match="no saved ancestor"):
        store.previous("p", "s", RUN, active_run="wrong-attempt")
    if evidence == "assistant":
        archive.append(
            "p", "s", active, Entry(role="assistant", kind="assistant", content="Answer")
        )
    elif evidence in ("tool", "model"):
        events.append("p", "s", active, evidence, {})
    else:
        owner = works.owner("p", "s", active)
        assert owner is not None
        ModelIntents(projects.control).begin(owner, kind="main", alias="fixture")
    with pytest.raises(ControlError, match="no saved ancestor"):
        store.previous("p", "s", RUN, active_run=active)
