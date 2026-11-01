# SPDX-License-Identifier: MIT
"""Fresh-process memory gate through dispatch, archival and final save, using benign text."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux RSS gate"),
]

RUNNER = """
import asyncio, json, sys, tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.harness import Finished, save
from sandbox_science.orchestrator.context.history import Archive, RunArchive
from sandbox_science.orchestrator.loop import run_task
from sandbox_science.orchestrator.model import ModelTurn, ToolCall, ToolSpec
from sandbox_science.orchestrator.projects import ProjectStore, run_name
from sandbox_science.orchestrator.tools import Toolbox, ToolEntry, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

mode, count, root = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
context = ExecutionContext(project_id='p', session_id='s', environment_id='env')
start = datetime(2026, 10, 16, tzinfo=UTC)
name = run_name(start, 'test')
store = ProjectStore(root)
store.create('p', 'minimal', 'sha256:' + 'a' * 64)
store.open_session('p', 's', dedicated=False)

class Model:
    def __init__(self):
        self.turn = 0
    async def complete(self, messages, tools):
        self.turn += 1
        if self.turn > count:
            return ModelTurn('Done')
        return ModelTurn(None, tool_calls=(
            ToolCall(str(self.turn), 'arithmetic', json.dumps(
                {'step': self.turn, 'purpose_note': 'Computing one more step'})),))

async def arithmetic(request):
    return ToolOutcome('4 (sample)', {'execution': {'state': 'succeeded',
        'result': {'exit_code': 0, 'stdout': {'text': 'x' * (1024 * 1024)}}}})

def peak_rss():
    return next(int(line.split()[1]) * 1024
                for line in Path('/proc/self/status').read_text().splitlines()
                if line.startswith('VmHWM:'))

async def main():
    box = Toolbox(WorkerClient('http://unused.invalid', 'test-secret-unused'), context)
    box.entries = {'arithmetic': ToolEntry(
        ToolSpec('arithmetic', 'Fixture', {'type': 'object'}), 'read', arithmetic)}
    log = (RunArchive(Archive(store.control), context, run=name, parent=None)
           if mode == 'archive' else None)
    initial_rss = peak_rss()
    tracemalloc.start()
    transcript = await run_task(Model(), box, 'Compute', run_id='test', hooks=(),
                                max_turns=count + 1, log=log)
    assert transcript.stopped == 'answered'
    assert len(transcript.tool_records) == count
    finished = Finished('test', context, 'Compute', start, transcript)
    errors = []
    output = root / 'run.json'
    assert await save(finished.fields(), 0, (), projects=store,
                      error=errors.append, transcript=output) == 0, errors
    _, python_peak = tracemalloc.get_traced_memory()
    rss_growth = peak_rss() - initial_rss
    tracemalloc.stop()
    print(json.dumps(dict(count=count, mode=mode, python_peak=python_peak,
                         rss_growth=rss_growth, saved_bytes=output.stat().st_size)))

try:
    asyncio.run(main())
finally:
    store.close()
"""


def test_ten_times_more_large_results_stay_bounded_through_final_save(tmp_path: Path) -> None:
    measurements = []
    for mode, count in (("archive", 4), ("archive", 40), ("inline", 40)):
        root = tmp_path / f"{mode}-{count}"
        result = subprocess.run(
            [sys.executable, "-c", RUNNER, mode, str(count), str(root)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        measured = json.loads(result.stdout)
        measurements.append(measured)
        if mode == "archive":
            assert measured["python_peak"] < 12 * 1024**2, measured
            assert measured["rss_growth"] < 32 * 1024**2, measured
            assert measured["saved_bytes"] < 256 * 1024, measured
        else:
            # Positive control: preserving all full outputs must trip the same gates.
            assert measured["python_peak"] > 32 * 1024**2, measured
            assert measured["rss_growth"] > 32 * 1024**2, measured
            assert measured["saved_bytes"] > 40 * 1024**2, measured
    print(json.dumps(measurements))
