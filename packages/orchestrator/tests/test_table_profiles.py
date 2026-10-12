# SPDX-License-Identifier: MIT
"""The table correction versions new parses without rewriting immutable history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.documents import USER, DocumentError, DocumentStore, table_rule
from sandbox_science.orchestrator.documents import store as store_module
from sandbox_science.orchestrator.documents.blocks import parse
from sandbox_science.orchestrator.documents.engine import attach, block_map
from sandbox_science.orchestrator.documents.identity import Allocator, compare, fresh
from sandbox_science.orchestrator.documents.parser import (
    LEGACY_PROFILE,
    PROFILE,
    parser_versions,
    tokens,
)


@pytest.mark.parametrize("edge", ["\ufeff", "\x1c", "\x1d", "\x1e", "\x1f", "\x85"])
def test_legacy_unicode_revision_read_edit_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edge: str
) -> None:
    path = tmp_path / "control.sqlite3"
    text = f"Before $p$.\n\n{edge}| $h$ | right |{edge}\n| --- | --- |\n| $b$ | value |\n"
    old = DocumentStore(path)
    # Make a real pre-upgrade revision through the store, not a fabricated map.
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "parse", lambda source: parse(source, profile=LEGACY_PROFILE))
        patch.setattr(
            store_module,
            "parser_versions",
            lambda: {**parser_versions(), "profile": LEGACY_PROFILE},
        )
        old.create("p1", name="legacy", title="Legacy", text=text, author=USER)
    previous = old.revision("p1", "legacy", 1)
    before = old.mapped("p1", "legacy", 1)
    old.close()

    current = DocumentStore(path)
    try:
        restored = current.mapped("p1", "legacy", 1)
        assert restored == before
        assert restored.parsed.profile == LEGACY_PROFILE
        assert current.revision("p1", "legacy", 1) == previous
        assert before.parsed.blocks[1].kind == ("paragraph" if edge == "\ufeff" else "table")

        result = current.edit_source(
            "p1",
            "legacy",
            base=1,
            text=text + "\nAfter $a$.\n",
            author=USER,
            reason="append after upgrade",
            operation_id="test-profile-edit",
        )
        assert result.outcome == "applied"
        edited = current.mapped("p1", "legacy", 2)
        assert edited.parsed.profile == PROFILE
        assert current.revision("p1", "legacy", 2).info.parser["profile"] == PROFILE
        # Unaffected blocks and formulas keep their real identities. The table/paragraph
        # disagreement receives new identities instead of pretending that an old map changed.
        assert edited.block_ids[0] == before.block_ids[0]
        assert edited.span_ids[0] == before.span_ids[0]
        assert edited.parsed.blocks[1].kind == ("table" if edge == "\ufeff" else "paragraph")
        assert edited.block_ids[1] != before.block_ids[1]
        assert current.revision("p1", "legacy", 1) == previous

        current.revert(
            "p1",
            "legacy",
            base=2,
            target=1,
            author=USER,
            reason="restore exact legacy identities",
            operation_id="test-profile-restore",
        )
        assert current.mapped("p1", "legacy", 3) == before
        revision = current.revision("p1", "legacy", 3)
        assert revision.info.parser["profile"] == revision.block_map["profile"] == LEGACY_PROFILE
        assert current.revision("p1", "legacy", 1) == previous
    finally:
        current.close()


@pytest.mark.parametrize("documents", [True, False])
def test_saved_maps_choose_their_exact_profile_and_mode(documents: bool) -> None:
    source = "---\ntext: ordinary\n---\n\n\ufeff| $h$ |\n| --- |\n"
    legacy = fresh(parse(source, documents=documents, profile=LEGACY_PROFILE), Allocator(0, 0))
    stored = block_map(legacy, compare(None, legacy))
    # This is the common reader seam for document, comment and response math.
    loaded = attach(parse(source, documents=documents), stored)
    assert loaded == legacy
    assert loaded.parsed.documents == documents
    assert loaded.parsed.profile == LEGACY_PROFILE
    assert (loaded.parsed.front_matter is not None) == documents
    assert json.loads(json.dumps(stored)) == stored


@pytest.mark.parametrize("profile", [None, {}, "ss-markdown-0", "ss-markdown-3"])
def test_unknown_profile_stays_unreadable(profile: Any) -> None:
    parsed = fresh(parse("Text $x$."), Allocator(0, 0))
    stored = block_map(parsed, compare(None, parsed))
    stored["profile"] = profile
    with pytest.raises(DocumentError, match="unsupported profile"):
        attach(parsed.parsed, stored)


def test_ordinary_text_ids_do_not_change_just_because_the_profile_changes() -> None:
    source = "# Header\n\nA $formula$.\n\n| $h$ |\n| --- |\n| $b$ |\n"
    legacy = fresh(parse(source, profile=LEGACY_PROFILE), Allocator(0, 0))
    current = fresh(parse(source), Allocator(0, 0))
    assert legacy.block_ids == current.block_ids
    assert legacy.span_ids == current.span_ids
    assert legacy.parsed.blocks == current.parsed.blocks
    assert legacy.parsed.spans == current.parsed.spans


def test_the_port_retains_the_upstream_empty_cell_expansion_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(table_rule, "MAX_AUTOCOMPLETED_CELLS", 4)
    source = "| one | two |\n| --- | --- |\n" + "| row |\n" * 6
    table = next(token for token in tokens(source) if token.type == "table_open")
    assert table.map == [0, 6]  # four rows, each adding one empty second cell
    assert [block.kind for block in parse(source).blocks] == ["table", "paragraph"]
