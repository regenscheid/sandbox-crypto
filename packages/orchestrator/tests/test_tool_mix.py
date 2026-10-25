# SPDX-License-Identifier: MIT
"""How a run computed, counted by code from its calls (tools spec, Kernels 13), and what
a cell whose kernel lost its state is told about the lost generation (Kernels 7, as
amended for A10). No worker runs: a fake answers executions and listings."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.contracts import (
    CellRequest,
    Execution,
    ExecutionState,
    KernelInfo,
    KernelObservation,
    ListFilesRequest,
    Result,
)
from sandbox_science.contracts.worker import FileEntry, FileListing
from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.context.history import Archive, Entry, RunArchive
from sandbox_science.orchestrator.context.history.operations import ToolJournal, ToolRecord
from sandbox_science.orchestrator.continuity.kernels import KernelJournal, LostKernels
from sandbox_science.orchestrator.continuity.records import WorkRecords
from sandbox_science.orchestrator.kernel_check import KernelCheck
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn
from sandbox_science.orchestrator.model import ToolCall as BareCall
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord
from sandbox_science.orchestrator.runstate import RunState
from sandbox_science.orchestrator.runtree import RunTrees
from sandbox_science.orchestrator.tool_mix import (
    ToolMix,
    scan,
    scan_events,
    shell_computation,
    unkept_runs,
)
from sandbox_science.orchestrator.tools import LOST_STATE, RESTART_KERNEL_DESCRIPTION, Toolbox
from sandbox_science.orchestrator.worker_client import WorkerClient

from .scripted import ScriptedModel, ToolCall
from .test_tool_journal import CONTEXT, RUN

# The classifier reads a shell call's script text alone (Kernels 13).
COMPUTATION = [
    ("python3 -c 'print(1)'", "python"),
    ('python -c "import sys; print(sys.version)"', "python"),
    ("python3 -uc 'print(1)'", "python"),
    ("python3 analyse.py", "python"),
    ("cd work && python3 analyse.py --n 5 > out.txt 2>&1", "python"),
    ("/usr/bin/python3.12 -W ignore run.py", "python"),
    ("sage -c 'print(2^3)'", "sage"),
    ("sage attack.sage", "sage"),
    ("sage -python helper.py", "python"),
    ("ipython -c 'print(1)'", "python"),
    ("pypy3 bench.py", "python"),
    ("python3 -m mypkg.cli --fast", "python"),
    ("python3 <<'EOF'\nimport numpy\nprint('it's unbalanced)\nEOF", "python"),
    ("cat <<EOF | python3\nprint(1)\nEOF\necho done", "python"),
    ("python3 - <<EOF\nprint(1)\nEOF", "python"),
    ("echo 'print(1)' | python3", "python"),
    ("python3 <<< 'print(1)'", "python"),
    ("python3 < script.py", "python"),
    ("set -euo pipefail\nuv run --with numpy python -c 'import numpy'", "python"),
    ("timeout 600 python3 long.py", "python"),
    ("env OMP_NUM_THREADS=4 python3 -u x.py", "python"),
    ("OMP_NUM_THREADS=4 nohup python3 x.py &", "python"),
    ("bash -c \"python3 -c 'print(1)'\"", "python"),
    ("x=$(python3 -c 'print(1)')", "python"),
    ("for n in 1 2; do python3 run_$n.py; done", "python"),
    ("echo a#b; python3 x.py", "python"),
]
NOT_COMPUTATION = [
    "python3 --version",
    "python -V",
    "python3 -h",
    "sage --version",
    "pip install numpy",
    "python3 -m pip install numpy",
    "python -m venv .venv",
    "python -m pytest -q tests",
    "pytest tests",
    "python3 -m build",
    "python3 -m ensurepip",
    "python3 setup.py build",
    "sage -t foo.sage",
    "sage -pip install x",
    "./run.sh",  # only the script text is read: what run.sh runs is not seen
    "bash run.sh",
    "make && ./a.out",
    "python3",
    "grep python3 notes.txt",
    "echo python3 x.py",
    "# python3 x.py\nls -l",
    "ls; cat results.json",
]


@pytest.mark.parametrize(("script", "interpreter"), COMPUTATION)
def test_a_shell_call_that_ran_an_interpreter_on_code_is_found(
    script: str, interpreter: str
) -> None:
    assert shell_computation(script) == interpreter


@pytest.mark.parametrize("script", NOT_COMPUTATION)
def test_commands_versions_installs_tests_and_builds_are_not_computation(script: str) -> None:
    assert shell_computation(script) is None


def test_the_counts_take_calls_that_ran_and_leave_out_refused_ones() -> None:
    mix = ToolMix()
    ran = {"execution": {"result": {}}}
    mix.count("python", "{}", ran)
    mix.count("sage", "{}", {"execution": {"result": {"state_lost": True}}})
    mix.count("shell", json.dumps({"script": "python3 x.py"}), ran)
    mix.count("shell", json.dumps({"script": "ls"}), ran)
    mix.count("shell", json.dumps({"script": "python3 y.py"}), {"error": "invalid_request"})
    mix.count("start_job", "{}", {"job": {}})
    mix.count("restart_kernel", "{}", {"kernel": {}})
    mix.count("read_file", "{}", {"read": {}})
    assert mix.summary() == {
        "classifier": "shell-computation-1",
        "python": 1,
        "sage": 1,
        "shell": 2,
        "shell_computation": 1,
        "start_job": 1,
        "restart_kernel": 1,
        "state_lost": 1,
    }
    assert mix.line() == (
        "2 cells (python 1, sage 1) · 2 shell calls, 1 ran Python · 1 job · "
        "1 kernel restart · kernel state lost once"
    )


def test_restart_kernel_says_when_to_use_it_and_that_state_is_lost() -> None:
    box = Toolbox(WorkerClient("http://unused.invalid", "fixture"), CONTEXT)
    (spec,) = [spec for spec in box.specs if spec.name == "restart_kernel"]
    assert spec.description == RESTART_KERNEL_DESCRIPTION
    for words in ("loses all its state", "package change", "corrupt", "free memory", "rebind"):
        assert words in spec.description


class Worker:
    """Answers each execution at once. A cell whose code starts ``# crash`` finds its
    kernel restarted (a new generation, its state lost); a cell's ``# writes P`` lines
    are the files it wrote; ``files`` are the workspace's files now."""

    def __init__(self) -> None:
        self.kernel = KernelInfo(kernel_id="kernel-1", language="python", generation=1)
        self.count = 0
        self.files: set[str] = set()
        self.listed: list[str] = []

    async def run(self, request: Any, on_submitted: Any = None, **_: Any) -> Execution:
        now = datetime.now(UTC)
        if isinstance(request, CellRequest):
            lost = request.code.startswith("# crash")
            if lost:
                self.kernel = self.kernel.model_copy(
                    update={"generation": self.kernel.generation + 1}
                )
                self.count = 0
            self.count += 1
            written = tuple(
                line.removeprefix("# writes ")
                for line in request.code.splitlines()
                if line.startswith("# writes ")
            )
            self.files.update(written)
            result = Result(
                status="ok",
                execution_count=self.count,
                kernel=self.kernel,
                state_lost=lost,
                written_files=written,
            )
            kind = "cell"
        else:
            if request.script.startswith("rm "):
                # Gone, though the worker's comparison did not report it deleted.
                self.files.discard(request.script.removeprefix("rm "))
            result = Result(exit_code=0)
            kind = "shell"
        execution = Execution(
            execution_id=f"exec-{request.operation_id[-16:]}",
            attempt_id="attempt",
            operation_id=request.operation_id,
            kind=kind,
            context=CONTEXT,
            state=ExecutionState.SUCCEEDED,
            submitted_at=now,
            result=result,
        )
        if on_submitted is not None:
            on_submitted(execution, True)
        return execution

    async def restart_kernel(self, context: Any, language: str, run: str | None) -> KernelInfo:
        self.kernel = self.kernel.model_copy(update={"generation": self.kernel.generation + 1})
        self.count = 0
        return self.kernel

    async def observe_kernels(self, context: Any) -> KernelObservation:
        return KernelObservation(
            environment_id=CONTEXT.environment_id,
            session_id=CONTEXT.session_id,
            observed_at=datetime.now(UTC),
            kernels=(self.kernel,),
        )

    async def list_files(self, request: ListFilesRequest) -> FileListing:
        self.listed.append(request.path)
        prefix = "" if request.path == "." else request.path + "/"
        names = [
            path.removeprefix(prefix)
            for path in sorted(self.files)
            if path.startswith(prefix) and "/" not in path.removeprefix(prefix)
        ]
        return FileListing(
            path=request.path,
            entries=tuple(
                FileEntry(name=name, type="file", size=1, modified_at=datetime.now(UTC))
                for name in names
            ),
        )


@pytest.fixture
def projects(tmp_path: Path) -> Iterator[ProjectStore]:
    store = ProjectStore(tmp_path)
    store.create("p", "minimal", "sha256:" + "a" * 64)
    store.open_session("p", "s", dedicated=False)
    store.open_session("p", "t", dedicated=False)
    yield store
    store.close()


def call(index: int, name: str, purpose: str | None = None, **arguments: Any) -> BareCall:
    if purpose is None:
        return ToolCall(f"c{index}", name, json.dumps(arguments))
    return BareCall(f"c{index}", name, json.dumps({"purpose_note": purpose, **arguments}))


async def scripted(
    projects: ProjectStore, worker: Worker, *calls: BareCall, jobs: bool = False
) -> tuple[Toolbox, Any]:
    """One run of ``calls``, a turn each, through the real loop, toolbox and archive,
    with the kernel journal and the lost-state records bound as the harness binds them."""
    box = Toolbox(cast(WorkerClient, worker), CONTEXT, languages=("python", "sage"))
    if jobs:
        box.jobs = cast(Any, object())
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    journal = KernelJournal(projects.control, owner, None)
    check = KernelCheck(cast(Any, worker), CONTEXT)
    check.record, check.record_tool = journal.record, journal.record_tool
    box.lost_kernels = LostKernels(projects.control, "p", "s", None)
    model = ScriptedModel(
        [*(ModelTurn(None, tool_calls=(each,)) for each in calls), ModelTurn("Done")]
    )
    transcript = await run_task(
        model,
        box,
        "Compute",
        run_id="run-1",
        hooks=(check,),
        log=RunArchive(Archive(projects.control), CONTEXT, run=RUN, parent=None),
    )
    return box, transcript


def results(projects: ProjectStore) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for message in Archive(projects.control).messages("p", "s", run=RUN):
        if message.entry.kind == "tool_result":
            content = message.entry.content or ""
            found.append({"message": message.message_id, **json.loads(content)})
    return found


async def test_a_run_keeps_its_tool_mix_and_says_once_that_a_kernel_keeps_state(
    projects: ProjectStore,
) -> None:
    worker = Worker()
    box, transcript = await scripted(
        projects,
        worker,
        call(1, "python", code="x = 1"),
        call(2, "shell", script="ls -l"),
        call(3, "shell", script="python3 -c 'print(2)'"),
        call(4, "sage", code="2^3"),
        call(5, "shell", script="python3 analyse.py"),
        call(6, "restart_kernel", language="python"),
        call(7, "python", code="# crash\ny = 2"),
        jobs=True,
    )
    assert transcript.summaries["tool_mix"] == {
        "classifier": "shell-computation-1",
        "python": 2,
        "sage": 1,
        "shell": 3,
        "shell_computation": 2,
        "start_job": 0,
        "restart_kernel": 1,
        "state_lost": 1,
    }
    shown = results(projects)
    notes = [result.get("note") for result in shown]
    # The first shell call that ran Python, and no other call, carries the note.
    assert notes[2] == (
        "Platform note: this ran Python through the shell: a python cell keeps variables, "
        "imports and loaded data between calls, and start_job runs a long computation in "
        "the background."
    )
    assert [index for index, note in enumerate(notes) if note] == [2]
    assert box.tool_mix is not None
    assert box.tool_mix.nudged


async def test_the_note_offers_only_the_tools_the_run_has(projects: ProjectStore) -> None:
    worker = Worker()
    await scripted(projects, worker, call(1, "shell", script="sage -c 'print(1)'"))
    (shown,) = results(projects)
    assert shown["note"] == (
        "Platform note: this ran Sage through the shell: a sage cell keeps variables, "
        "imports and loaded data between calls."
    )


async def test_a_run_without_a_kernel_gets_no_note() -> None:
    box = Toolbox(cast(WorkerClient, Worker()), CONTEXT, languages=())
    outcome = await box.call(call(1, "shell", script="python3 x.py"), "op-1")
    assert "note" not in json.loads(outcome.text)
    # A call refused before it ran (here, for want of a purpose) is not counted.
    await box.call(BareCall("c2", "shell", json.dumps({"script": "python3 y.py"})), "op-2")
    assert box.tool_mix is not None
    assert box.tool_mix.shell_computation == 1


async def test_a_lost_kernel_lists_its_cells_and_the_files_they_saved(
    projects: ProjectStore,
) -> None:
    worker = Worker()
    await scripted(
        projects,
        worker,
        call(1, "python", "Loading the SNOVA parameter sets", code="import json\n# writes p.json"),
        call(2, "python", "Building the field tables", code="# writes out/t.pkl\nt = 1"),
        call(3, "python", "Writing a scratch file", code="# writes gone.txt"),
        call(4, "shell", script="rm gone.txt"),
        call(5, "python", "Rebuilding after the crash", code="# crash\nimport json"),
    )
    shown = results(projects)
    lost = shown[4]["lost_state"]
    assert shown[4]["state_lost"].startswith("the kernel restarted")
    assert lost["note"] == LOST_STATE
    assert (
        "Load saved files before recomputing; recompute the rest from these cells" in (lost["note"])
    )
    assert lost["cells"] == [
        {"message": shown[0]["message"], "purpose": "Loading the SNOVA parameter sets"},
        {"message": shown[1]["message"], "purpose": "Building the field tables"},
        {"message": shown[2]["message"], "purpose": "Writing a scratch file"},
    ]
    # The files the cells wrote that still exist, newest last.
    assert lost["saved_files"] == ["p.json", "out/t.pkl"]
    assert "earlier_cells" not in lost
    assert not any("lost_state" in result for result in shown[:4])


async def test_cells_without_a_purpose_show_their_tool_and_first_line(
    projects: ProjectStore,
) -> None:
    """A call archived before purposes existed (B9) has none: its tool and its code's
    first line stand in, and a cell the archive lacks is said to be unarchived."""
    owner = WorkRecords(projects.control).admit("p", "s", RUN)
    journal = KernelJournal(projects.control, owner, None)
    kernel = KernelInfo(kernel_id="kernel-1", language="python", generation=1)
    archive, tools = Archive(projects.control), ToolJournal(projects.control)
    observed = KernelObservation(
        environment_id=CONTEXT.environment_id,
        session_id="s",
        observed_at=datetime.now(UTC),
        kernels=(kernel,),
    )
    for count in range(1, 24):
        operation = f"legacy-{count}"
        arguments = json.dumps({"code": f"\n  value_{count} = {count}  \nmore()"})
        execution = Execution(
            execution_id=f"exec-{count}",
            attempt_id="attempt",
            operation_id=operation,
            kind="cell",
            context=CONTEXT,
            state=ExecutionState.SUCCEEDED,
            submitted_at=datetime.now(UTC),
            result=Result(
                status="ok",
                execution_count=count,
                kernel=kernel,
                written_files=("a.npy",) if count == 22 else ("w.tmp",) if count == 20 else (),
                deleted_files=("a.npy",) if count == 23 else (),
            ),
        )
        record: dict[str, Any] = {
            "tool": "python",
            "arguments": arguments,
            "execution": execution.model_dump(mode="json"),
        }
        if count == 20:
            # The recorder's revisions, when it kept them, say what was written.
            record["recorded_files"] = {"files": [{"path": "r.bin", "deleted": False}]}
        if count != 21:  # the archive lacks one
            tools.start(CONTEXT, RUN, operation, BareCall(f"c{count}", "python", arguments))
            archive.append(
                "p",
                "s",
                RUN,
                Entry("tool", "tool_result", "{}", source="python", call_id=f"c{count}"),
                tool_record=ToolRecord(json.loads(json.dumps(record)), operation),
            )
        await journal.record_tool(json.loads(json.dumps(record)), observed)
    lost = await LostKernels(projects.control, "p", "s", None).lost(
        "python", kernel.model_copy(update={"generation": 2})
    )
    assert lost is not None
    assert lost.earlier == 3
    assert len(lost.cells) == 20
    assert lost.cells[0].first_line == "value_4 = 4"
    assert lost.cells[0].purpose is None
    assert lost.cells[0].message is not None
    assert (lost.cells[-3].message, lost.cells[-3].first_line) == (None, None)
    assert lost.written == ("r.bin",)  # a.npy was written by one cell, deleted by the next
    # The current generation is not the lost one; another language has none.
    assert await LostKernels(projects.control, "p", "s", None).lost("python", kernel) is None
    assert await LostKernels(projects.control, "p", "s", None).lost("sage", None) is None
    assert await LostKernels(projects.control, "p", "s", "child").lost("python", None) is None


def _record(session: str, run_id: str, started: datetime, tools: list[dict[str, Any]]) -> RunRecord:
    return RunRecord.model_validate(
        {
            "run_id": run_id,
            "context": CONTEXT.model_copy(update={"session_id": session}).model_dump(mode="json"),
            "task": "Work.",
            "started_at": started,
            "ended_at": started,
            "stopped": "answered",
            "final_text": "Done.",
            "messages": [],
            "tools": tools,
        }
    )


def _shell(script: str) -> dict[str, Any]:
    return {"tool": "shell", "arguments": json.dumps({"script": script}), "execution": {}}


def _cell(tool: str, *, lost: bool = False) -> dict[str, Any]:
    return {
        "tool": tool,
        "arguments": json.dumps({"code": "1"}),
        "execution": {"result": {"state_lost": lost}},
    }


def two_sessions(projects: ProjectStore) -> None:
    """Runs recorded before the summary existed: only their tool calls say how they
    computed. One preview's full script is in the tool journal alone."""
    started = datetime(2026, 10, 18, tzinfo=UTC)
    long = "python3 -c '" + "x" * 600 + "'"
    ToolJournal(projects.control).start(
        CONTEXT, RUN, "op-long", BareCall("c1", "shell", json.dumps({"script": long}))
    )
    message = Archive(projects.control).append(
        "p",
        "s",
        RUN,
        Entry("tool", "tool_result", "{}", source="shell", call_id="c1"),
        tool_record=ToolRecord(_shell(long), "op-long"),
    )
    preview = {
        "tool": "shell",
        "arguments": json.dumps({"script": long})[:512],
        "_record_storage": {
            "format": "tool-record-v1",
            "project": "p",
            "message": message.message_id,
        },
    }
    projects.save_run(
        _record(
            "s",
            "first",
            started,
            [
                _cell("python"),
                _cell("python", lost=True),
                _shell("python3 a.py"),
                _shell("pip install x"),
                preview,
                {"tool": "shell", "arguments": "{}", "error": "invalid_request"},
                {"tool": "start_job", "arguments": "{}", "job": {}},
            ],
        )
    )
    projects.save_run(_record("s", "second", started + timedelta(hours=1), [_cell("sage")]))
    projects.save_run(
        _record(
            "t", "third", started, [_shell("ls"), {"tool": "restart_kernel", "arguments": "{}"}]
        )
    )


def test_a_recorded_run_is_measured_from_its_tool_records(projects: ProjectStore) -> None:
    two_sessions(projects)
    (first, second) = projects.runs("p", "s")
    mix = scan(projects.control, "p", "s", first.name)
    assert (mix.python, mix.shell, mix.shell_computation, mix.state_lost, mix.start_job) == (
        2,
        3,
        2,
        1,
        1,
    )
    assert scan(projects.control, "p", "s", second.name).sage == 1


def run_cli(*arguments: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli.main(["tool-mix", *arguments])
    return code, capsys.readouterr().out


def test_the_command_counts_each_session_and_the_total(
    projects: ProjectStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    two_sessions(projects)
    projects.close()
    code, text = run_cli("--state-dir", str(tmp_path), capsys=capsys)
    assert code == 0
    assert text.splitlines() == [
        "p/s: 2 runs · 3 cells (python 2, sage 1) · 3 shell calls, 2 ran Python · 1 job · "
        "kernel state lost once",
        "p/t: 1 run · 0 cells · 1 shell call · 1 kernel restart",
        "total: 3 runs · 3 cells (python 2, sage 1) · 4 shell calls, 2 ran Python · 1 job · "
        "1 kernel restart · kernel state lost once",
    ]
    code, text = run_cli(
        "--state-dir", str(tmp_path), "--project", "p", "--session", "t", "--json", capsys=capsys
    )
    assert code == 0
    found = json.loads(text)
    assert found["classifier"] == "shell-computation-1"
    assert found["sessions"] == [
        {
            "project": "p",
            "session": "t",
            "runs": 1,
            "runs_from_events": 0,
            "classifier": "shell-computation-1",
            "python": 0,
            "sage": 0,
            "shell": 1,
            "shell_computation": 0,
            "start_job": 0,
            "restart_kernel": 1,
            "state_lost": 0,
        }
    ]
    assert found["total"]["runs"] == 1
    assert found["total"]["shell"] == 1
    code, _ = run_cli(
        "--state-dir", str(tmp_path), "--project", "p", "--session", "x", capsys=capsys
    )
    assert code == 1


def test_the_command_only_reads(
    projects: ProjectStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    two_sessions(projects)
    projects.close()
    store = tmp_path / "control.sqlite3"
    before = store.read_bytes()
    assert run_cli("--state-dir", str(tmp_path), "--json", capsys=capsys)[0] == 0
    assert store.read_bytes() == before


def test_the_run_tree_projects_a_records_tool_mix(projects: ProjectStore) -> None:
    started = datetime(2026, 10, 25, tzinfo=UTC)
    mix = ToolMix(python=3)
    mix.shell = 1
    summary = mix.summary()
    record = _record("s", "kept", started, []).model_copy(
        update={"summaries": {"tool_mix": summary}}
    )
    projects.save_run(record)
    projects.save_run(_record("s", "older", started + timedelta(minutes=1), []))
    runs = {run.run_id: run for run in RunTrees(projects).session("p", "s").runs}
    assert runs["kept"].tool_mix == summary
    assert runs["older"].tool_mix is None


LOST_RESULT = json.dumps(
    {"status": "ok", "state_lost": "the kernel restarted before this cell; earlier state is gone"}
)


def cut_short_run(projects: ProjectStore, run: str) -> None:
    """A run the loss of its agent VM cut short: it wrote events and no run record. Its
    last shell call never returned, so it has no ``tool`` event."""
    state = RunState(projects)

    def assistant(*calls: tuple[str, str, dict[str, Any]]) -> None:
        state.append(
            "p",
            "s",
            run,
            "assistant",
            {
                "turn": 1,
                "text": None,
                "tool_calls": [
                    {"id": call_id, "name": name, "arguments": json.dumps(arguments)}
                    for call_id, name, arguments in calls
                ],
                "usage": None,
            },
        )

    def returned(call_id: str, state_word: str | None, error: str | None, text: str = "{}") -> None:
        state.append(
            "p",
            "s",
            run,
            "tool",
            {
                "turn": 1,
                "call_id": call_id,
                "name": None,
                "result": text,
                "outcome": {"state": state_word, "error": error, "exit_code": None},
            },
        )

    state.append("p", "s", run, "started", {"role": "main", "run_id": run})
    assistant(
        ("a", "python", {"code": "x = 1"}),
        ("b", "shell", {"script": "python3 attack.py"}),
        ("c", "shell", {"script": "python3 refused.py"}),
        ("d", "start_job", {"script": "long.sh"}),
        ("e", "sage", {"code": "x"}),
        ("f", "shell", {"script": "sleep 600"}),
    )
    returned("a", "completed", None)
    returned("b", "completed", None)
    returned("c", None, "invalid_request")
    returned("d", None, None)
    returned("e", "completed", None, LOST_RESULT)


def test_a_run_that_kept_no_record_is_counted_from_its_events(projects: ProjectStore) -> None:
    started = datetime(2026, 10, 25, tzinfo=UTC)
    projects.save_run(_record("s", "kept", started, [_cell("python"), _shell("ls")]))
    cut_short_run(projects, "cut")
    assert unkept_runs(projects.control, "p", "s") == ["cut"]
    assert unkept_runs(projects.control, "p", "t") == []
    mix = scan_events(projects.control, "p", "s", "cut")
    # Calls that ran: a cell, two more, a job, a lost-state sage cell, and one shell call
    # that computed; the refused call and the one that never returned are not counted.
    assert mix.summary() == {
        "classifier": "shell-computation-1",
        "python": 1,
        "sage": 1,
        "shell": 1,
        "shell_computation": 1,
        "start_job": 1,
        "restart_kernel": 0,
        "state_lost": 1,
    }


def test_a_call_reconciled_after_the_run_was_cut_short_counts_once(
    projects: ProjectStore,
) -> None:
    """The holding shell call ("f") never returned; the resumed session reconciled it into
    the archive under the cut run, which wrote no event for it and kept no record."""
    cut_short_run(projects, "cut")
    before = scan_events(projects.control, "p", "s", "cut")
    assert before.shell == 1
    script = json.dumps({"script": "python3 held.py"})
    ToolJournal(projects.control).start(CONTEXT, "cut", "op-f", BareCall("f", "shell", script))
    reconciled = {"tool": "shell", "arguments": script, "execution": {}, "reconciled": True}
    entry = Entry("tool", "tool_result", "{}", source="shell", call_id="f")
    Archive(projects.control).append(
        "p", "s", "cut", entry, tool_record=ToolRecord(reconciled, "op-f")
    )
    # A call whose event also returned is counted by the event alone.
    ToolJournal(projects.control).start(CONTEXT, "cut", "op-a", BareCall("a", "python", "{}"))
    twice = {"tool": "python", "arguments": "{}", "execution": {}, "reconciled": True}
    Archive(projects.control).append(
        "p",
        "s",
        "cut",
        Entry("tool", "tool_result", "{}", source="python", call_id="a"),
        tool_record=ToolRecord(twice, "op-a"),
    )
    after = scan_events(projects.control, "p", "s", "cut")
    assert (after.shell, after.shell_computation, after.python) == (2, 2, 1)
    assert after.summary() == {**before.summary(), "shell": 2, "shell_computation": 2}


def test_the_command_counts_runs_without_a_record_from_their_events_and_says_so(
    projects: ProjectStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started = datetime(2026, 10, 25, tzinfo=UTC)
    projects.save_run(_record("s", "kept", started, [_cell("python"), _shell("ls")]))
    cut_short_run(projects, "cut")
    projects.close()
    code, text = run_cli(
        "--state-dir", str(tmp_path), "--project", "p", "--session", "s", capsys=capsys
    )
    assert code == 0
    assert text.splitlines() == [
        "p/s: 2 runs · 1 run from events · 3 cells (python 2, sage 1) · "
        "2 shell calls, 1 ran Python · 1 job · kernel state lost once",
        "total: 2 runs · 1 run from events · 3 cells (python 2, sage 1) · "
        "2 shell calls, 1 ran Python · 1 job · kernel state lost once",
    ]
