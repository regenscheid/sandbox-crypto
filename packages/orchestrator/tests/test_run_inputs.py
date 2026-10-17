# SPDX-License-Identifier: MIT
"""Starting inputs survive absent final records; failed persistence prevents dispatch."""

from __future__ import annotations

import io
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

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
        assert verify(io.BytesIO(b"".join(encoded))).format == "sandbox-run-evidence-v2"
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
