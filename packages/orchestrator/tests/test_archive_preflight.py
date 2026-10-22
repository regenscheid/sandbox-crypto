# SPDX-License-Identifier: MIT
"""Metadata preflight retains neither payloads nor closed Blob bookkeeping."""

from __future__ import annotations

import gc
import tracemalloc

import pytest

from sandbox_science.orchestrator.context.history import Archive, Entry
from sandbox_science.orchestrator.context.history import archive as module
from sandbox_science.orchestrator.context.history.limits import WorkingMemoryLimitError
from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.control import store as stores
from sandbox_science.orchestrator.projects import ProjectStore

from .test_tool_journal import projects as projects


def test_repeated_preflight_does_not_accumulate_connection_blob_weakrefs(
    projects: ProjectStore,
) -> None:
    archive = Archive(projects.control)
    row = archive.append(
        "p",
        "s",
        "r",
        Entry("assistant", "assistant", "small", native=b"native", native_format="fixture"),
    )
    archive.message("p", row.message_id, max_bytes=1024)
    gc.collect()
    tracemalloc.start()
    try:
        initial, _ = tracemalloc.get_traced_memory()
        for _ in range(5000):
            assert archive.message("p", row.message_id, max_bytes=1024).entry.content == "small"
        gc.collect()
        current, _ = tracemalloc.get_traced_memory()
        assert current - initial < 512 * 1024
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_preflight_and_legacy_connections_close_on_success_and_refusal(
    projects: ProjectStore, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    if not legacy and not module._OCTET_LENGTH:
        pytest.skip("octet_length requires SQLite3.43; product uses legacy reader here")
    archive = Archive(projects.control)
    content = "\u03b1\x00😀" * 1000
    native = b"native" * 1000
    row = archive.append(
        "p",
        "s",
        "r",
        Entry("assistant", "assistant", content, native=native, native_format="fixture"),
    )
    expected = archive.payload_bytes("p", row.message_id)
    assert expected >= len(content.encode()) + len(native)
    temporary: list[ControlStore] = []

    class Reader(ControlStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            assert self.readonly
            temporary.append(self)

    monkeypatch.setattr(module, "_OCTET_LENGTH", not legacy)
    monkeypatch.setattr(stores, "ControlStore", Reader)
    assert archive.payload_bytes("p", row.message_id) == expected
    assert archive.message("p", row.message_id, max_bytes=expected).entry.native == native
    with pytest.raises(WorkingMemoryLimitError):
        archive.message("p", row.message_id, max_bytes=expected - 1)
    assert len(temporary) == (3 if legacy else 0)
    assert all(reader._db is None for reader in temporary)
    assert archive.message("p", row.message_id).entry.content == content
