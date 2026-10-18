# SPDX-License-Identifier: MIT
"""Full visible archive indexing preserves exact search and reports incomplete coverage."""

import pytest

from sandbox_science.orchestrator.history import Archive, Entry
from sandbox_science.orchestrator.history.inspector_index import CHUNK, InspectorIndex

from .test_tool_journal import RUN
from .test_tool_journal import projects as projects


@pytest.mark.parametrize("needle", ["abc", 'a"b', "\u03b1\u03b2\u03b3", "a b", "123", "a-b"])
def test_index_matches_exact_visible_retrieval_echoes_once(projects, needle):
    entry = Entry("tool", "tool_result", f"start {needle} end {needle}", source="history_read")
    saved = Archive(projects.control).append("p", "s", RUN, entry)
    found = InspectorIndex(projects.control).page("p", "s", RUN, contains=needle)
    assert [item.message for item in found.messages] == [saved.message_id]
    assert found.indexed
    assert not found.index_pending
    assert needle in found.messages[0].snippet
    assert InspectorIndex(projects.control).page("p", "s", "other", contains=needle).messages == ()


def test_backfill_covers_chunk_boundaries_large_tool_arguments_and_old_missing_rows(projects):
    archive = Archive(projects.control)
    witness = "CASE-sensitive-witness"
    content = "x" * (CHUNK - 131) + witness + "y" * 300000
    saved = archive.append("p", "s", RUN, Entry("user", "task", content))
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "sage", "arguments": "z" * 300000 + "tool-original-needle"},
    }
    tool = archive.append(
        "p", "s", RUN, Entry("assistant", "assistant", "small", tool_calls=(call,))
    )
    index = InspectorIndex(projects.control)
    before = index.page("p", "s", RUN, contains=witness)
    assert not before.messages
    assert before.index_pending
    assert index.backfill("p", "s", RUN, chunks=1).pending
    assert index.page("p", "s", RUN, contains=witness).index_pending
    progress = index.backfill("p", "s", RUN)
    assert not progress.pending
    assert [m.message for m in index.page("p", "s", RUN, contains=witness).messages] == [
        saved.message_id
    ]
    assert index.page("p", "s", RUN, contains=witness.lower()).messages == ()
    assert [
        m.message for m in index.page("p", "s", RUN, contains="tool-original-needle").messages
    ] == [tool.message_id]
    assert index.backfill("p", "s", RUN).chunks == 0
    projects.control.close()
    assert not index.page("p", "s", RUN, contains=witness).index_pending


def test_index_filters_and_pagination_do_not_mix_agents_or_sessions(projects):
    archive = Archive(projects.control)
    records = [archive.append("p", "s", RUN, Entry("user", "task", "needle")) for _ in range(3)]
    archive.append("p", "s", "other", Entry("user", "task", "needle"))
    archive.append("p", "s", RUN, Entry("user", "fold", "needle"))
    index = InspectorIndex(projects.control)
    first = index.page("p", "s", RUN, contains="needle", limit=2)
    assert [m.message for m in first.messages] == [r.message_id for r in records[:2]]
    last = index.page("p", "s", RUN, contains="needle", after=first.next_after)
    assert [m.message for m in last.messages] == [records[2].message_id]
    assert last.next_after is None
    assert len(index.page("p", "s", RUN, contains="needle", include_derived=True).messages) == 4
    assert index.page("p", "s", RUN, contains="needle", role="tool").messages == ()


@pytest.mark.parametrize("large", [False, True])
def test_nulls_and_multibyte_boundaries_do_not_hide_later_original_text(projects, large):
    prefix = "\u03b1" * (200000 if large else 20)
    original = prefix + "\x00" + "after-null-witness" + "x" * 20
    saved = Archive(projects.control).append("p", "s", RUN, Entry("user", "task", original))
    index = InspectorIndex(projects.control)
    while index.backfill("p", "s", RUN).pending:
        pass
    page = index.page("p", "s", RUN, contains="after-null-witness")
    assert [row.message for row in page.messages] == [saved.message_id]
    assert "after-null-witness" in page.messages[0].snippet
    assert index.page("p", "s", RUN, contains="\u03b1 after-null").messages == ()


def test_upgrade_leaves_legacy_evidence_intact_and_backfill_completes_it(projects, tmp_path):
    from sandbox_science.orchestrator.control import ControlStore

    from .control_versions import older_store

    saved = Archive(projects.control).append(
        "p",
        "s",
        RUN,
        Entry("tool", "tool_result", "legacy retrieval-echo-witness", source="history_search"),
    )
    path = projects.control.path
    projects.control.close()
    target = tmp_path / "legacy.sqlite3"
    older_store(path, target, 71)
    upgraded = ControlStore(target)
    upgraded.initialize()
    index = InspectorIndex(upgraded)
    assert index.page("p", "s", RUN, contains="retrieval-echo").index_pending
    assert not index.backfill("p", "s", RUN).pending
    assert (
        index.page("p", "s", RUN, contains="retrieval-echo").messages[0].message == saved.message_id
    )
    assert Archive(upgraded).message("p", saved.message_id).entry.content == saved.entry.content
    upgraded.close()
