# SPDX-License-Identifier: MIT
"""The frontier's categories (docs/specs/ledger.md, LG-18, LG-19): versioned project
records the user or the main agent defines, and the results that name one."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.documents import USER, DocumentError
from sandbox_science.orchestrator.ledger import Ledger

from .test_ledger import DELEGATED, MAIN, change, events
from .test_ledger import ledger as ledger

KEY_RECOVERY: dict[str, Any] = {
    "category": "key-recovery",
    "target": "The test scheme at its first parameter set",
    "kind": "Key recovery cost",
    "measures": [
        {"name": "time", "unit": "log2 operations", "direction": "lower"},
        {"name": "memory", "unit": "log2 bits", "direction": "lower"},
    ],
    "conditions": ["model", "success"],
    "reason": "The attacks the project compares.",
    "operation_id": "category",
}


def result(**fields: Any) -> dict[str, Any]:
    return {
        "type": "result",
        "origin": "project",
        "title": "R",
        "statement": "Time $2^{105}$.",
        "fields": {"category": "key-recovery", **fields},
    }


CLASSICAL = {"model": "classical", "success": "at least 1/2"}


def test_the_user_or_the_main_agent_defines_and_revises_a_category(ledger: Ledger) -> None:
    defined = ledger.define_category("p", KEY_RECOVERY, MAIN)
    assert (defined.revision, defined.head, defined.author["role"]) == (1, 1, "main")
    assert [m.name for m in defined.measures] == ["time", "memory"]
    assert ledger.define_category("p", KEY_RECOVERY, MAIN).replayed
    with pytest.raises(DocumentError, match="exists, at revision 1") as exists:
        ledger.define_category("p", {**KEY_RECOVERY, "operation_id": "again"}, USER)
    assert exists.value.kind == "conflict"
    revised = {**KEY_RECOVERY, "expected": 1, "conditions": ["model"], "operation_id": "rev"}
    assert ledger.define_category("p", revised, USER).revision == 2
    with pytest.raises(DocumentError) as stale:
        ledger.define_category("p", {**revised, "operation_id": "stale"}, USER)
    assert (stale.value.kind, stale.value.details["revision"]) == ("conflict", 2)
    # Every revision is kept.
    assert ledger.category("p", "key-recovery", 1).conditions == ["model", "success"]
    current = ledger.category("p", "key-recovery")
    assert (current.revision, current.head, current.conditions) == (2, 2, ["model"])
    with pytest.raises(DocumentError) as missing:
        ledger.category("p", "bounds")
    assert missing.value.kind == "not_found"
    kinds = [kind for kind, _ in category_events(ledger)]
    assert kinds == ["ledger.category_defined", "ledger.category_defined"]


def category_events(ledger: Ledger) -> list[tuple[str, dict[str, Any]]]:
    from sandbox_science.orchestrator.control import EventLog

    log = EventLog(ledger.control, "frontier_category", "test")
    with ledger.control.read() as tx:
        return [(e.kind, e.payload) for e in log.page(tx.sql, "p", 0, 200)]


def test_a_category_names_each_measure_and_condition_once_and_is_not_a_delegates(
    ledger: Ledger,
) -> None:
    for bad, message in (
        ({"measures": []}, "at least 1"),
        ({"measures": [*KEY_RECOVERY["measures"], KEY_RECOVERY["measures"][0]]}, "once"),
        ({"conditions": ["model", "model"]}, "once"),
        ({"measures": [{"name": "time", "unit": "s", "direction": "faster"}]}, "direction"),
    ):
        with pytest.raises(DocumentError, match=message):
            ledger.define_category("p", {**KEY_RECOVERY, **bad}, MAIN)
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        ledger.define_category("p", KEY_RECOVERY, DELEGATED)
    # An operation ID names one change in the project, a category's or an entry's.
    change(ledger, {"type": "task", "title": "T", "statement": "s"}, op="category")
    with pytest.raises(DocumentError, match="names another ledger change"):
        ledger.define_category("p", KEY_RECOVERY, MAIN)


def test_an_agents_category_text_holds_no_character_the_user_may_not_see(
    ledger: Ledger,
) -> None:
    """Threat model A3 (lane O's #317 for entries): the target, kind, reason and units an
    agent writes are refused when they hold a hidden character; the user's are kept."""
    hidden = {**KEY_RECOVERY, "target": "The scheme \u202eemehcs"}
    with pytest.raises(DocumentError, match="the category's target holds U\\+202E") as refused:
        ledger.define_category("p", hidden, MAIN)
    assert refused.value.kind == "invalid_request"
    unit = [{"name": "time", "unit": "log2\u200b operations", "direction": "lower"}]
    with pytest.raises(DocumentError, match="measure time's unit holds U\\+200B"):
        ledger.define_category("p", {**KEY_RECOVERY, "measures": unit}, MAIN)
    assert ledger.define_category("p", hidden, USER).target == "The scheme \u202eemehcs"


def test_categories_list_in_the_order_they_were_defined(ledger: Ledger) -> None:
    for n, name in enumerate(("key-recovery", "bounds", "distinguishers")):
        ledger.define_category(
            "p", {**KEY_RECOVERY, "category": name, "operation_id": f"c{n}"}, MAIN
        )
    first = ledger.categories("p", limit=2)
    assert ([c.category for c in first.categories], first.next_after) == (
        ["key-recovery", "bounds"],
        "bounds",
    )
    rest = ledger.categories("p", after=first.next_after)
    assert ([c.category for c in rest.categories], rest.next_after) == (["distinguishers"], None)


def test_a_result_in_a_category_gives_each_condition_and_only_its_measures(
    ledger: Ledger,
) -> None:
    """LG-19."""
    with pytest.raises(DocumentError, match="no frontier category key-recovery"):
        change(ledger, result(conditions=CLASSICAL), op="early")
    ledger.define_category("p", KEY_RECOVERY, MAIN)
    for fields, message in (
        ({"conditions": {"model": "classical"}}, "no value for the condition keys success"),
        ({"conditions": {**CLASSICAL, "data": "2^40"}}, "condition keys it does not have: data"),
        ({"conditions": CLASSICAL, "measures": {"queries": 40}}, "measures it does not have"),
    ):
        with pytest.raises(DocumentError, match=message):
            change(ledger, result(**fields), op="bad")
    made = change(
        ledger, result(conditions=CLASSICAL, measures={"time": 105, "memory": "2^{80}"}), op="r"
    )
    assert made["entries"][0]["handle"] == "R-1"
    # A missing measure is allowed: the result is listed as not compared (LG-21).
    change(ledger, result(conditions=CLASSICAL, measures={"time": 110}), op="r2")
    # A later revision of the category does not hold up changes that leave the fields.
    revised = {**KEY_RECOVERY, "expected": 1, "conditions": ["model", "success", "data"],
               "operation_id": "rev"}  # fmt: skip
    ledger.define_category("p", revised, USER)
    change(ledger, {"entry": "R-1", "expected": 1, "title": "Renamed"}, op="t")
    with pytest.raises(DocumentError, match="no value for the condition keys data"):
        change(ledger, {"entry": "R-1", "expected": 2, "fields": result(conditions=CLASSICAL)[
            "fields"]}, op="f")  # fmt: skip
    assert [kind for kind, _ in events(ledger)].count("ledger.entry_created") == 2


# The review of the frontier stack.


def test_a_category_as_long_as_the_model_takes_is_kept(ledger: Ledger) -> None:
    """L1: a target or units of characters UTF-8 writes in several bytes, within the
    model's lengths, were refused by the store's CHECKs as an internal error."""
    wide = {
        **KEY_RECOVERY,
        "target": "\U0001d53d" * 4096,
        "kind": "\u00e9" * 4096,
        "measures": [
            {"name": f"m{n}-" + "x" * 120, "unit": "\x01" * 512, "direction": "lower"}
            for n in range(16)
        ],
        "reason": "\U0001d53d" * 4096,
    }
    defined = ledger.define_category("p", wide, USER)
    assert (len(defined.target), defined.measures[15].unit) == (4096, "\x01" * 512)
    assert ledger.category("p", "key-recovery") == defined
    too_long = {**wide, "target": "x" * 4097, "operation_id": "long"}
    with pytest.raises(DocumentError) as refused:
        ledger.define_category("p", too_long, USER)
    assert refused.value.kind == "invalid_request"


def test_condition_keys_are_compared_folded_and_values_hold_no_hidden_text(
    ledger: Ledger,
) -> None:
    """M4: a key written with capitals is the category's key, once; an agent's condition
    value with a hidden character is refused (#317's check of every field)."""
    with pytest.raises(DocumentError, match="each measure and condition key once"):
        ledger.define_category("p", {**KEY_RECOVERY, "conditions": ["model", "Model"]}, USER)
    ledger.define_category("p", KEY_RECOVERY, MAIN)
    capitals = {"Model": "classical", "success": "at least 1/2"}
    change(ledger, result(conditions=capitals), op="capitals")
    with pytest.raises(DocumentError, match="the condition keys model more than once"):
        change(ledger, result(conditions={**CLASSICAL, "MODEL": "quantum"}), op="twice")
    hidden = {"model": "classical\u200b", "success": "at least 1/2"}
    with pytest.raises(DocumentError, match=r"fields\.conditions\.model holds U\+200B"):
        change(ledger, result(conditions=hidden), op="hidden")


def test_a_version_25_store_takes_categories_after_the_migration(
    ledger: Ledger, tmp_path: Path
) -> None:
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path

    from .control_versions import older_store

    change(ledger, {"type": "task", "title": "T", "statement": "s"})
    ledger.documents.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(store_path(tmp_path), store_path(old), migrations.BASELINE + 23)
    raw = sqlite3.connect(store_path(old))
    try:
        tables = {n for (n,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = raw.execute("PRAGMA user_version").fetchone()[0]
    finally:
        raw.close()
    assert (version, "ledger_flags" in tables, "frontier_categories" in tables) == (25, True, False)
    migrated = DocumentStore(store_path(old))
    migrated.initialize()
    try:
        moved = Ledger(migrated)
        assert moved.categories("p").categories == []
        assert moved.define_category("p", KEY_RECOVERY, MAIN).revision == 1
    finally:
        migrated.close()
