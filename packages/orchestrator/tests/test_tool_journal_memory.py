# SPDX-License-Identifier: MIT
"""RC-F10 retrieval gate: hash a 10x larger record with bounded reader memory.

Each reader is a fresh process so fixture construction cannot hide an allocation in
an already-grown Python heap or SQLite cache. No model, worker or network is used.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.operations import ToolRecord
from sandbox_science.orchestrator.projects import ProjectStore

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux RSS gate"),
]

READER = """
import hashlib, json, sys, tracemalloc
from pathlib import Path
from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.history import Archive

store = ControlStore(Path(sys.argv[1]), readonly=True)
store.initialize()
archive = Archive(store)
def peak_rss():
    # getrusage's peak can include the fixture parent's pre-exec high-water mark.
    # VmHWM belongs to this exec's address space, so fixture construction cannot mask it.
    return next(int(line.split()[1]) * 1024
                for line in Path('/proc/self/status').read_text().splitlines()
                if line.startswith('VmHWM:'))

initial_rss = peak_rss()
tracemalloc.start()
offset, pages, total, digest = 0, 0, 0, hashlib.sha256()
try:
    mode = sys.argv[3] if len(sys.argv) > 3 else 'chunks'
    if mode == 'whole':
        # Positive control: the older full-read path must exceed the same budgets.
        whole = archive.tool_record('p', sys.argv[2], max_bytes=32 * 1024 * 1024)
    elif mode == 'index':
        while True:
            page = archive.tool_records('p', 's', 'run', after=offset, limit=1)
            total += sum(record.record_bytes for record in page.records)
            pages += 1
            if not page.has_more:
                break
            offset = page.next_after
    else:
        while True:
            page = archive.tool_record_chunk('p', sys.argv[2], offset=offset)
            total = page.total
            digest.update(page.data)
            pages += 1
            if page.next_offset is None:
                break
            offset = page.next_offset
    _, python_peak = tracemalloc.get_traced_memory()
    rss_growth = peak_rss() - initial_rss
finally:
    tracemalloc.stop()
    store.close()
print(json.dumps(dict(bytes=total, pages=pages, sha256=digest.hexdigest(),
                     python_peak=python_peak, rss_growth=rss_growth)))
"""


def test_reading_ten_times_more_evidence_does_not_materialize_the_record(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    fixtures = []
    try:
        store.create("p", "minimal", "sha256:" + "a" * 64)
        store.open_session("p", "s", dedicated=False)
        for scale in (1, 10):
            record = {"output": "x" * (2 * 1024 * 1024 * scale), "exact": "00123456789"}
            raw = json.dumps(record, ensure_ascii=False, allow_nan=False).encode()
            saved = Archive(store.control).append(
                "p",
                "s",
                "run",
                Entry("tool", "tool_result", "sample"),
                tool_record=ToolRecord(record),
            )
            fixtures.append((saved.message_id, len(raw), hashlib.sha256(raw).hexdigest()))
    finally:
        store.close()
    measurements = []
    for message_id, size, digest in fixtures:
        result = subprocess.run(
            [sys.executable, "-c", READER, str(store.control.path), message_id],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        measured = json.loads(result.stdout)
        assert (measured["bytes"], measured["sha256"]) == (size, digest)
        assert measured["pages"] > 1
        assert measured["python_peak"] < 1024 * 1024, measured
        assert measured["rss_growth"] < 8 * 1024 * 1024, measured
        measurements.append(measured)
    control = subprocess.run(
        [sys.executable, "-c", READER, str(store.control.path), fixtures[-1][0], "whole"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    materialized = json.loads(control.stdout)
    assert materialized["python_peak"] > 1024 * 1024, materialized
    assert materialized["rss_growth"] > 8 * 1024 * 1024, materialized
    listing = subprocess.run(
        [sys.executable, "-c", READER, str(store.control.path), fixtures[-1][0], "index"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    metadata = json.loads(listing.stdout)
    assert metadata["bytes"] == sum(size for _, size, _ in fixtures)
    assert metadata["pages"] == len(fixtures)
    assert metadata["python_peak"] < 1024 * 1024, metadata
    assert metadata["rss_growth"] < 8 * 1024 * 1024, metadata
    print(
        json.dumps({"pages": measurements, "full_read_control": materialized, "metadata": metadata})
    )
