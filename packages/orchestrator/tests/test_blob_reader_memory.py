# SPDX-License-Identifier: MIT
"""Incremental evidence consumers retain no history of closed Blob handles."""

from __future__ import annotations

import gc
import hashlib
import json
import tracemalloc

import pytest

from sandbox_science.orchestrator.control import ControlError, ControlStore, migrations
from sandbox_science.orchestrator.control import store as stores
from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history import archive as archives
from sandbox_science.orchestrator.history.collections import Collection
from sandbox_science.orchestrator.history.export import EvidenceExport
from sandbox_science.orchestrator.history.operations import ToolRecord
from sandbox_science.orchestrator.projects import ProjectError, ProjectStore

from .test_tool_journal import RUN
from .test_tool_journal import projects as projects
from .test_work_ownership import record


def test_aggregate_repeated_blob_reads_export_pages_and_closure(
    projects: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = Archive(projects.control)
    text = "needle\x00\u03b1😀" * 9000
    original = {"tool": "arithmetic", "stdout": text}
    message = archive.append(
        "p", "s", RUN, Entry("tool", "tool_result", "needle"), tool_record=ToolRecord(original)
    )
    values = Collection[str](projects.control, ("p", "s", RUN), "fixture")
    values.append(text)
    projects.save_run(record(RUN))
    expected_value = json.dumps(text, ensure_ascii=False).encode()
    expected_record = json.dumps(original, ensure_ascii=False).encode()
    assert len(expected_value) > 2 * 65536
    assert (
        b"".join(
            values.chunk(0, offset=offset).data for offset in range(0, len(expected_value), 65536)
        )
        == expected_value
    )
    assert (
        b"".join(
            archive.tool_record_chunk("p", message.message_id, offset=offset).data
            for offset in range(0, len(expected_record), 65536)
        )
        == expected_record
    )
    active = opened = 0

    class Reader(ControlStore):
        def __init__(self, *args, **kwargs):
            nonlocal active, opened
            super().__init__(*args, **kwargs)
            assert self.readonly
            active += 1
            opened += 1
            self.counted = True

        def close(self):
            nonlocal active
            super().close()
            if self.counted:
                active -= 1
                self.counted = False

    monkeypatch.setattr(stores, "ControlStore", Reader)

    def read(index: int = 0) -> None:
        offset = (index % 3) * 65536
        before = opened
        assert projects.run("p", "s", RUN).final_text == "Evidence retained"
        assert values.chunk(0, offset=offset).data == expected_value[offset : offset + 65536]
        assert (
            archive.tool_record_chunk("p", message.message_id, offset=offset).data
            == (expected_record[offset : offset + 65536])
        )
        assert archive.search("p", "needle").hits
        assert active == 0
        assert opened - before == 3 + int(not archives._OCTET_LENGTH)

    read()
    gc.collect()
    tracemalloc.start()
    try:
        initial, _ = tracemalloc.get_traced_memory()
        for index in range(1000):
            read(index)
            if index % 100 == 0:
                digest, count = hashlib.sha256(), 0
                for raw in EvidenceExport(projects.control, "p", "s", RUN).lines():
                    assert active == 0  # including every consumer pause/cancellation boundary
                    line = json.loads(raw)
                    if line["kind"] == "complete":
                        assert line["sha256"] == digest.hexdigest()
                        assert line["lines"] == count
                    else:
                        digest.update(raw)
                        count += 1
        gc.collect()
        current, _ = tracemalloc.get_traced_memory()
        assert current - initial < 512 * 1024
    finally:
        tracemalloc.stop()
    assert active == 0
    for call, error in (
        (lambda: values.chunk(0, offset=10**6), ControlError),
        (lambda: archive.tool_record_chunk("other", message.message_id), ControlError),
        (lambda: projects.run("p", "s", RUN, max_bytes=1), ProjectError),
    ):
        with pytest.raises(error):
            call()
        assert active == 0
    monkeypatch.setattr(archives, "_OCTET_LENGTH", False)
    before = opened
    assert archive.search("p", "needle").hits
    assert opened == before + 1
    assert active == 0
    abandoned = EvidenceExport(projects.control, "p", "s", RUN).lines()
    for _ in range(8):
        next(abandoned)
        assert active == 0
    del abandoned
    gc.collect()
    assert active == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_isolated_first_read_preserves_lazy_writer_migration(
    projects: ProjectStore, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    projects.save_run(record(RUN))
    archive = Archive(projects.control)
    message = archive.append("p", "s", RUN, Entry("assistant", "assistant", "needle"))
    path = projects.root.parent
    projects.close()

    def extra(schema: migrations.Schema) -> None:
        schema.execute("CREATE TABLE isolated_reader_migration (value TEXT)")

    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, extra))
    monkeypatch.setattr(migrations, "VERSION", migrations.VERSION + 1)
    if legacy:
        monkeypatch.setattr(archives, "_OCTET_LENGTH", False)
    readonly = ProjectStore(path, readonly=True)
    try:
        with pytest.raises(ControlError, match="open it with the harness"):
            readonly.run("p", "s", RUN)
    finally:
        readonly.close()
    writer = ProjectStore(path)
    try:
        if legacy:
            assert Archive(writer.control).message("p", message.message_id, max_bytes=1024)
        else:
            assert writer.run("p", "s", RUN).final_text == "Evidence retained"
        with writer.control.read() as tx:
            assert tx.sql.execute("PRAGMA user_version").fetchone() == (migrations.VERSION,)
            assert tx.sql.execute("SELECT * FROM isolated_reader_migration").fetchall() == []
    finally:
        writer.close()
