# SPDX-License-Identifier: MIT
"""Fresh-process export memory, including a large legacy final record and native state."""

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
import json, sys, tracemalloc
from pathlib import Path
from datetime import UTC, datetime
from sandbox_science.contracts import ExecutionContext
from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history.operations import ToolRecord, ToolJournal
from sandbox_science.orchestrator.context.history.export import EvidenceExport
from sandbox_science.orchestrator.context.history.verify_export import verify
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.projects import ProjectStore, RunRecord, run_name
mode, root, size = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
store = ProjectStore(root)
start = datetime(2026,10,16,tzinfo=UTC)
run = run_name(start, 'fixture')
context = ExecutionContext(project_id='p',session_id='s',environment_id='env')
if mode == 'seed':
    store.create('p','minimal','sha256:'+'a'*64)
    store.open_session('p','s',dedicated=False)
    text = 'x' * size
    Archive(store.control).append('p','s',run,
        Entry('assistant','assistant',text,native=text.encode(),native_format='fixture'))
    ToolJournal(store.control).start(context,run,'op',ToolCall('c','arithmetic',text))
    Archive(store.control).append('p','s',run,Entry('tool','tool_result','sample'),
                                 tool_record=ToolRecord({'output':text},'op'))
    store.save_run(RunRecord(run_id='fixture',context=context,task='Fixture',
        started_at=start,ended_at=start,stopped='answered',
        messages=[{'role':'user','content':text}],tools=[]))
else:
    def rss():
        lines = Path('/proc/self/status').read_text().splitlines()
        return next(int(s.split()[1])*1024 for s in lines
                    if s.startswith('VmHWM:'))
    baseline = rss()
    tracemalloc.start()
    if mode == 'export':
        count, largest = 0, 0
        with (root / 'evidence.ndjson').open('wb') as output:
            for line in EvidenceExport(store.control,'p','s',run).lines():
                output.write(line)
                count += len(line)
                largest = max(largest,len(line))
        with (root / 'evidence.ndjson').open('rb') as source:
            assert verify(source).run == run
        assert count > size * 5
        assert largest < 90000
    else:
        found = store.run('p','s',run,max_bytes=None)  # intentional unbounded positive control
        assert len(found.messages[0]['content']) == size
    _, peak = tracemalloc.get_traced_memory()
    print(json.dumps(dict(mode=mode,size=size,python_peak=peak,rss_growth=rss()-baseline)))
store.close()
"""


def test_export_ten_times_larger_fields_has_bounded_memory(tmp_path: Path) -> None:
    measurements = []
    for mib in (2, 20):
        root = tmp_path / str(mib)
        for mode in ("seed", "export", "inline"):
            result = subprocess.run(
                [sys.executable, "-c", RUNNER, mode, str(root), str(mib * 1024**2)],
                capture_output=True,
                text=True,
                check=True,
                timeout=90,
            )
            if mode == "seed":
                continue
            measured = json.loads(result.stdout)
            measurements.append(measured)
            if mode == "export":
                assert measured["python_peak"] < 2 * 1024**2, measured
                assert measured["rss_growth"] < 12 * 1024**2, measured
            elif mib == 20:
                assert measured["python_peak"] > 20 * 1024**2, measured
                assert measured["rss_growth"] > 20 * 1024**2, measured
    print(json.dumps(measurements))
