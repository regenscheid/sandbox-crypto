# SPDX-License-Identifier: MIT
"""User source reconciliation, retained patch review, promotion and exact-ID reverts."""

from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_science.orchestrator.documents import USER, Author, DocumentError, DocumentStore

AGENT = Author(kind="agent", session_id="a", run_id="run-a", role="main")


@pytest.fixture
def store(tmp_path: Path):
    found = DocumentStore(tmp_path / "control.sqlite3")
    yield found
    found.close()


def create(store: DocumentStore, text: str = "# Heading\n\nFirst $x$ and $y$.\n\nLast.\n") -> str:
    return store.create("p", name="notes", title="Notes", text=text, author=USER).document_id


def edit(store: DocumentStore, doc: str, base: int, text: str, op: str = "user-edit"):
    return store.edit_source(
        "p", doc, base=base, text=text, author=USER, reason="User edit", operation_id=op
    )


def test_source_rebases_only_changed_blocks(store: DocumentStore):
    doc = create(store)
    base = store.mapped("p", doc)
    last = base.block_ids[-1]
    store.patch(
        "p",
        doc,
        base=1,
        operations=[{"op": "replace", "block": last, "markdown": "Agent last."}],
        author=AGENT,
        reason="Agent last",
        operation_id="agent",
    )
    result = edit(store, doc, 1, "# Heading\n\nFirst $x$ and $z$.\n\nLast.\n")
    assert result.rebased
    assert result.outcome == "applied"
    mapped = store.mapped("p", doc)
    assert "Agent last." in mapped.parsed.text
    assert "$z$" in mapped.parsed.text
    assert mapped.block_ids == base.block_ids
    assert mapped.span_ids[0] == base.span_ids[0]
    assert store.revision("p", doc).info.author == USER
    assert edit(store, doc, 1, "# Heading\n\nFirst $x$ and $z$.\n\nLast.\n").replayed


def test_conflicting_user_text_is_retained_with_current_author(store: DocumentStore):
    doc = create(store)
    block = store.mapped("p", doc).block_ids[1]
    store.patch(
        "p",
        doc,
        base=1,
        operations=[{"op": "replace", "block": block, "markdown": "Agent equation $a$."}],
        author=AGENT,
        reason="Agent change",
        operation_id="agent",
    )
    result = edit(store, doc, 1, "# Heading\n\nUser equation $b$.\n\nLast.\n")
    assert result.outcome == "conflict"
    assert result.revision == 2
    assert result.conflicts[0]["now"] == "Agent equation $a$."
    assert (
        cast(list[dict[str, Any]], result.conflicts)[0]["changed_by"][0]["author"]["kind"]
        == "agent"
    )
    patch = store.patches("p", doc)[-1]
    assert "User equation" in str(patch.operations)


def test_source_move_insert_delete_and_math_ids(store: DocumentStore):
    doc = create(
        store, "# Heading\n\nFirst $x$ and $y$.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nLast.\n"
    )
    old = store.mapped("p", doc)
    result = edit(
        store,
        doc,
        1,
        "# Heading\n\nInserted.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nFirst $x$ and $z$.\n",
    )
    assert result.outcome == "applied"
    new = store.mapped("p", doc)
    assert old.block_ids[2] in new.block_ids
    assert old.block_ids[3] not in new.block_ids
    assert old.span_ids[0] == new.span_ids[0]
    assert new.block_ids[-1] == old.block_ids[1]


def rejected(store: DocumentStore, doc: str):
    block = store.mapped("p", doc).block_ids[1]
    store.patch(
        "p",
        doc,
        base=1,
        operations=[{"op": "replace", "block": block, "markdown": "New current."}],
        author=USER,
        reason="current",
        operation_id="current",
    )
    result = store.patch(
        "p",
        doc,
        base=1,
        operations=[{"op": "replace", "block": block, "markdown": "Retained proposal."}],
        author=AGENT,
        reason="proposal",
        operation_id="proposal",
    )
    assert result.patch_id is not None
    return result.patch_id


def test_retained_apply_requires_review_head_and_preserves_other_changes(store: DocumentStore):
    doc = create(store)
    patch = rejected(store, doc)
    preview = store.preview_patch("p", doc, patch)
    assert preview["base"] == 2
    assert preview["allowed"]
    assert "Retained proposal." in preview["proposed"]
    last = store.mapped("p", doc).block_ids[-1]
    store.patch(
        "p",
        doc,
        base=2,
        operations=[{"op": "replace", "block": last, "markdown": "Unmentioned newer text."}],
        author=USER,
        reason="later",
        operation_id="later",
    )
    with pytest.raises(DocumentError, match="review it first"):
        store.apply_patch(
            "p", doc, patch, base=2, author=USER, reason="apply", operation_id="apply-old"
        )
    result = store.apply_patch(
        "p", doc, patch, base=3, author=USER, reason="Reviewed proposal", operation_id="apply"
    )
    assert result.outcome == "accepted"
    assert "Unmentioned newer text." in store.revision("p", doc).text
    rev = store.revision("p", doc)
    assert rev.info.author == AGENT
    assert rev.info.accepted_by == USER
    assert store.apply_patch(
        "p", doc, patch, base=3, author=USER, reason="Reviewed proposal", operation_id="apply"
    ).replayed


def test_retained_unclosed_fence_cannot_swallow_unnamed_blocks(store: DocumentStore):
    doc = create(store)
    block = store.mapped("p", doc).block_ids[1]
    bad = store.patch(
        "p",
        doc,
        base=1,
        operations=[{"op": "replace", "block": block, "markdown": "```\nmalicious"}],
        author=AGENT,
        reason="bad",
        operation_id="bad",
    )
    assert bad.outcome == "refused"
    assert bad.patch_id is not None
    assert not store.preview_patch("p", doc, bad.patch_id)["allowed"]
    result = store.apply_patch(
        "p", doc, bad.patch_id, base=1, author=USER, reason="try", operation_id="try"
    )
    assert result.outcome == "refused"
    assert store.document("p", doc).head == 1


def test_revert_restores_original_ids_without_reusing_future_counters(store: DocumentStore):
    doc = create(store)
    original = store.mapped("p", doc)
    edit(store, doc, 1, "# Heading\n\nEntirely different $q$.\n\nExtra.\n\nAnother.\n")
    store.revert("p", doc, base=2, target=1, author=USER, reason="restore", operation_id="restore")
    assert store.mapped("p", doc).block_ids == original.block_ids
    assert store.mapped("p", doc).span_ids == original.span_ids
    assert store.revision("p", doc).info.operations == [{"op": "revert", "revision": 1}]
    with pytest.raises(DocumentError):
        store.revert("p", doc, base=1, target=2, author=USER, reason="stale", operation_id="stale")


def test_promote_retains_history_and_rejects_collision_or_agent(store: DocumentStore):
    doc = store.create(
        "p", name="book", title="Book", text="Book.", author=AGENT, session_id="a"
    ).document_id
    create(store)
    with pytest.raises(DocumentError):
        store.promote(
            "p", doc, base=1, name="notes", author=USER, reason="move", operation_id="collision"
        )
    with pytest.raises(DocumentError):
        store.promote(
            "p", doc, base=1, name="book", author=AGENT, reason="move", operation_id="agent"
        )
    result = store.promote(
        "p", doc, base=1, name="book", author=USER, reason="move", operation_id="move"
    )
    assert result.document_id == doc
    assert store.document("p", "book").scope == "project"
    assert len(store.revisions("p", doc)) == 1
    assert store.events("p")[-1].kind == "document.promoted"
    with pytest.raises(DocumentError):
        store.promote(
            "other", doc, base=1, name="book", author=USER, reason="wrong", operation_id="wrong"
        )


def test_exact_source_spacing_and_normalization_are_preserved(store: DocumentStore):
    doc = create(store)
    source = "\n# Heading\n\n\nFirst \\(x\\) and $z$.\n\n\nLast.\n\n"
    result = edit(store, doc, 1, source)
    assert result.outcome == "applied"
    assert store.revision("p", doc).text == source.replace("\\(x\\)", "$x$")
    assert result.normalized["converted"]
    with pytest.raises(DocumentError):
        store.edit_source(
            "p",
            doc,
            base=2,
            text="Agent forges user edit.",
            author=AGENT,
            reason="forged",
            operation_id="forged",
        )


def test_preview_of_patch_with_deleted_named_block_stays_reviewable(store: DocumentStore):
    doc = create(store)
    patch = rejected(store, doc)
    block = store.mapped("p", doc).block_ids[1]
    store.patch(
        "p",
        doc,
        base=2,
        operations=[{"op": "delete", "block": block}],
        author=USER,
        reason="removed",
        operation_id="removed",
    )
    preview = store.preview_patch("p", doc, patch)
    assert not preview["allowed"]
    assert preview["proposed"] is None
    assert "Retained proposal" in str(preview["patch"])
    assert preview["message"]


def test_revert_same_text_still_restores_target_identity(store: DocumentStore):
    doc = create(store, "First.\n\nLast.\n")
    original = store.mapped("p", doc)
    store.patch(
        "p",
        doc,
        base=1,
        operations=[{"op": "delete", "block": original.block_ids[0]}],
        author=USER,
        reason="remove",
        operation_id="remove",
    )
    store.patch(
        "p",
        doc,
        base=2,
        operations=[{"op": "insert", "at": "start", "markdown": "First."}],
        author=USER,
        reason="recreate",
        operation_id="recreate",
    )
    assert store.revision("p", doc).text == "First.\n\nLast.\n"
    assert store.mapped("p", doc).block_ids != original.block_ids
    result = store.revert(
        "p", doc, base=3, target=1, author=USER, reason="restore IDs", operation_id="restore-ids"
    )
    assert result.outcome == "applied"
    assert result.revision == 4
    assert store.mapped("p", doc).block_ids == original.block_ids


def test_editing_preparation_runs_outside_writer_transaction(store: DocumentStore, monkeypatch):
    import sandbox_science.orchestrator.documents.store as module

    doc = create(store)
    patch = rejected(store, doc)
    store._cache.clear()
    from sandbox_science.orchestrator.documents.blocks import parse as original

    def checked(text):
        assert not store.control.in_transaction
        return original(text)

    monkeypatch.setattr(module, "parse", checked)
    store.apply_patch(
        "p", doc, patch, base=2, author=USER, reason="reviewed", operation_id="reviewed"
    )
    store._cache.clear()
    store.revert("p", doc, base=3, target=1, author=USER, reason="restore", operation_id="restore")
    store._cache.clear()
    edit(store, doc, 4, "# Heading\n\nUser $x$.\n\nLast.\n")
