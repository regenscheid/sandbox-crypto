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
    # The verification's confusables: an agent writes condition values in printable ASCII,
    # so a Cyrillic a, a Unicode dash or Arabic-Indic digits make no group of their own.
    for n, value in enumerate(("cl\u0430ssical", "classical\u2010quantum", "\u0661/2")):
        with pytest.raises(DocumentError, match="outside printable ASCII"):
            change(ledger, result(conditions={**CLASSICAL, "success": value}), op=f"c{n}")
    change(ledger, result(conditions={**CLASSICAL, "model": "cl\u0430ssical"}), op="u", author=USER)


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


# The best known results (LG-20, LG-21, LG-23): LG-F2 as far as this step goes.

QUANTUM = {"model": "quantum", "success": "at least 1/2"}


def published(title: str, conditions: dict[str, str], **measures: Any) -> dict[str, Any]:
    cite = [{"role": "supports", "type": "source_claim", "entry": "REF-1"}]
    return {**result(conditions=conditions, measures=measures), "origin": "published",
            "title": title, "evidence": cite}  # fmt: skip


def best(view: Any, conditions: dict[str, str]) -> Any:
    (group,) = [g for g in view.groups if g.conditions == conditions]
    return group


def test_the_best_known_results_under_each_set_of_conditions(ledger: Ledger) -> None:
    from .test_ledger import NEW
    from .test_ledger_decisions import decide

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)  # R-1
    change(ledger, published("P2", CLASSICAL, time=110, memory=80), op="p2", author=USER)  # R-2
    view = ledger.frontier("p", "key-recovery")
    group = best(view, CLASSICAL)
    assert [r.handle for r in group.best] == ["R-1", "R-2"]  # conflicting measures
    (first,) = [r for r in group.best if r.handle == "R-1"]
    assert (first.status, first.origin, first.sources, first.evidence_types) == (
        "recorded",
        "published",
        ["REF-1"],
        ["source_claim"],
    )
    change(ledger, published("P3", CLASSICAL, time=125, memory=65), op="p3", author=USER)  # R-3
    change(ledger, published("Q1", QUANTUM, time=90, memory=90), op="q1", author=USER)  # R-4
    project = result(conditions=CLASSICAL, measures={"time": 105, "memory": 80})
    change(ledger, {**project, "title": "R1"}, op="r1")  # R-5, proposed
    change(
        ledger, published("P4", CLASSICAL, time="$2^{0.29n}$", memory=70), op="p4", author=USER
    )  # R-6
    view = ledger.frontier("p", "key-recovery")
    group = best(view, CLASSICAL)
    assert [r.handle for r in group.best] == ["R-1", "R-2"]
    assert [(r.handle, r.dominated_by) for r in group.dominated] == [("R-3", ["R-1"])]
    assert [r.handle for r in group.not_compared] == ["R-6"]
    assert [(r.handle, r.status, r.would_dominate) for r in group.candidates] == [
        ("R-5", "proposed", ["R-2"])
    ]
    assert [r.handle for r in best(view, QUANTUM).best] == ["R-4"]
    # Once established, R1 is best known, and P2 is dominated, still recorded.
    change(ledger, decide("R-5"), {"entry": "R-5", "expected": 1, "status": "established"},
           op="est", author=USER)  # fmt: skip
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [r.handle for r in group.best] == ["R-1", "R-5"]
    assert [(r.handle, r.dominated_by, r.status) for r in group.dominated] == [
        ("R-2", ["R-5"], "recorded"),
        ("R-3", ["R-1"], "recorded"),
    ]
    assert group.candidates == []
    # A disputed result stays best known, marked; a refuted one leaves.
    change(ledger, {"entry": "R-1", "expected": 1, "status": "disputed"}, op="d")
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [(r.handle, r.disputed) for r in group.best] == [("R-1", True), ("R-5", False)]
    change(ledger, decide("R-1"), {"entry": "R-1", "expected": 2, "status": "refuted"},
           op="ref-1", author=USER)  # fmt: skip
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    # P3 was dominated only by P1, so it is best known again beside R1.
    assert [r.handle for r in group.best] == ["R-3", "R-5"]
    assert [(r.handle, r.dominated_by) for r in group.dominated] == [("R-2", ["R-5"])]
    assert ledger.frontier("p", "key-recovery").truncated is False


def test_a_result_disputed_before_it_was_eligible_is_a_candidate(ledger: Ledger) -> None:
    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, result(conditions=CLASSICAL, measures={"time": 100, "memory": 50}), op="r")
    change(ledger, {"entry": "R-1", "expected": 1, "status": "disputed"}, op="d")
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert (group.best, [(r.handle, r.status) for r in group.candidates]) == (
        [],
        [("R-1", "disputed")],
    )


def test_results_the_category_gained_a_condition_key_after_are_unplaced(ledger: Ledger) -> None:
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)
    revised = {**KEY_RECOVERY, "expected": 1, "conditions": ["model", "success", "data"],
               "operation_id": "rev"}  # fmt: skip
    ledger.define_category("p", revised, MAIN)
    view = ledger.frontier("p", "key-recovery")
    assert (view.groups, [r.handle for r in view.unplaced]) == ([], ["R-1"])
    with pytest.raises(DocumentError) as missing:
        ledger.frontier("p", "bounds")
    assert missing.value.kind == "not_found"


# The review of the frontier stack: H1, H2, H3, M4, and the main agent's revisions.


def test_a_disputed_result_shows_the_revision_that_was_eligible(ledger: Ledger) -> None:
    """H1: the main agent disputed P1 and rewrote its measures in the same change, and the
    frontier showed the new ones as best known; a result refuted, then disputed, came
    back."""
    from .test_ledger import NEW
    from .test_ledger_decisions import decide

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)
    change(ledger, published("P2", CLASSICAL, time=110, memory=80), op="p2", author=USER)
    faster = result(conditions=CLASSICAL, measures={"time": 90, "memory": 50})["fields"]
    change(ledger, {"entry": "R-1", "expected": 1, "status": "disputed", "fields": faster}, op="d")
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [(r.handle, r.revision, r.status, r.disputed, r.measures) for r in group.best] == [
        ("R-1", 1, "disputed", True, {"time": 120, "memory": 60}),
        ("R-2", 1, "recorded", False, {"time": 110, "memory": 80}),
    ]
    change(ledger, decide("R-2"), {"entry": "R-2", "expected": 1, "status": "refuted"},
           op="refute", author=USER)  # fmt: skip
    change(ledger, decide("R-2"), {"entry": "R-2", "expected": 2, "status": "disputed"},
           op="back", author=USER)  # fmt: skip
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert ([r.handle for r in group.best], group.candidates) == (["R-1"], [])


def test_every_eligible_result_is_read_and_candidates_are_capped_apart(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2: as many proposed or withdrawn results as the cap pushed a recorded one out of
    the frontier's read."""
    from sandbox_science.orchestrator.ledger import frontier

    from .test_ledger import NEW

    monkeypatch.setattr(frontier, "MAX_CANDIDATES", 2)
    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    made = [
        result(conditions=CLASSICAL, measures={"time": 130 + n, "memory": 70}) for n in range(4)
    ]
    change(ledger, *made, op="made")  # R-1 to R-4
    change(ledger, {"entry": "R-4", "expected": 1, "status": "withdrawn"}, op="w")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)
    view = ledger.frontier("p", "key-recovery")
    group = best(view, CLASSICAL)
    assert [r.handle for r in group.best] == ["R-5"]
    # The newest come first, and the rest a read further on (the verification).
    assert ([r.handle for r in group.candidates], view.truncated, view.next_candidates_before) == (
        ["R-2", "R-3"],
        True,
        2,
    )
    older = ledger.frontier("p", "key-recovery", candidates_before=2)
    assert ([r.handle for r in best(older, CLASSICAL).candidates], older.truncated) == (
        ["R-1"],
        False,
    )


def test_a_published_result_an_agent_records_is_unverified_until_the_user_confirms_it(
    ledger: Ledger,
) -> None:
    """H3: the main agent recorded a published result with measures its source need not
    give, and it was best known at once."""
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1")  # the agent's
    change(ledger, published("P2", CLASSICAL, time=125, memory=65), op="p2", author=USER)
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [(r.handle, r.author) for r in group.best] == [
        ("R-2", USER.model_dump(exclude_none=True))
    ]
    assert [(r.handle, r.unverified, r.author["role"], r.would_dominate)
            for r in group.candidates] == [("R-1", True, "main", ["R-2"])]  # fmt: skip
    # The user's check of the revision confirms it, and a title is not what was checked.
    ledger.check("p", "R-1", 1, USER, operation_id="checked")
    change(ledger, {"entry": "R-1", "expected": 2, "title": "P1, renamed"}, op="title")
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert ([r.handle for r in group.best], [r.handle for r in group.dominated]) == (
        ["R-1"],
        ["R-2"],
    )
    # Measures the main agent gives after are not what the user confirmed.
    faster = published("P1", CLASSICAL, time=100, memory=60)["fields"]
    change(ledger, {"entry": "R-1", "expected": 3, "fields": faster}, op="faster")
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [r.handle for r in group.best] == ["R-2"]
    assert [(r.handle, r.unverified) for r in group.candidates] == [("R-1", True)]


def test_only_the_users_recording_writing_or_check_confirms_an_agents_result(
    ledger: Ledger,
) -> None:
    """The verification's defects 1 and 2: the user checked R-1 at 100 and corrected it to
    125, the agent put back 100, and R-1 was verified again; and the user's edit of an
    agent's result's title verified it."""
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=100, memory=60), op="p1")  # the agent's

    def shown() -> tuple[list[tuple[str, Any]], list[tuple[str, Any]]]:
        group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
        return (
            [(r.handle, r.measures["time"]) for r in group.best],
            [(r.handle, r.measures["time"]) for r in group.candidates],
        )

    def measures(time: int) -> dict[str, Any]:
        return published("P1", CLASSICAL, time=time, memory=60)["fields"]

    change(ledger, {"entry": "R-1", "expected": 1, "title": "P1."}, op="title", author=USER)
    assert shown() == ([], [("R-1", 100)])
    ledger.check("p", "R-1", 2, USER, operation_id="checked")
    assert shown() == ([("R-1", 100)], [])
    change(ledger, {"entry": "R-1", "expected": 3, "fields": measures(125)}, op="fix", author=USER)
    assert shown() == ([("R-1", 125)], [])
    change(ledger, {"entry": "R-1", "expected": 4, "fields": measures(100)}, op="back")
    assert shown() == ([], [("R-1", 100)])


def test_the_frontier_warns_of_groups_that_only_look_different(ledger: Ledger) -> None:
    """M4's residue: the user's spellings stay apart, and the frontier says which look
    alike: a Cyrillic a, and a division slash."""
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)
    alike = {"model": "cl\u0430ssical", "success": "at least 1\u22152"}
    change(ledger, published("P2", alike, time=130, memory=70), op="p2", author=USER)
    view = ledger.frontier("p", "key-recovery")
    assert (len(view.groups), view.lookalike) == (2, [[CLASSICAL, alike]])


def test_condition_values_group_as_they_read(ledger: Ledger) -> None:
    """M4: a capital, a no-break space or doubled spaces put a result in a group of its
    own, where it was best known though another dominated it."""
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)
    for n, written in enumerate(("Classical", "classical\u00a0", "  CLASSICAL ")):
        conditions = {"Model": written, "success": "at least  1/2"}
        change(ledger, published(f"P{n + 2}", conditions, time=130 + n, memory=70),
               op=f"p{n + 2}", author=USER)  # fmt: skip
    (group,) = ledger.frontier("p", "key-recovery").groups
    assert group.conditions == CLASSICAL
    assert ([r.handle for r in group.best], [r.handle for r in group.dominated]) == (
        ["R-1"],
        ["R-2", "R-3", "R-4"],
    )


def test_the_frontier_shows_what_the_main_agent_changed_in_its_category(
    ledger: Ledger,
) -> None:
    """The review's hardening: the main agent may revise a category, and the frontier
    says that it did, and what changed, until the user revises it."""
    ledger.define_category("p", KEY_RECOVERY, MAIN)
    revised = ledger.frontier("p", "key-recovery").agent_revised
    assert revised is not None
    assert (revised.revision, revised.since, revised.changed) == (1, None, [])
    ledger.define_category("p", {**KEY_RECOVERY, "expected": 1, "operation_id": "u"}, USER)
    assert ledger.frontier("p", "key-recovery").agent_revised is None
    measures = [
        {**KEY_RECOVERY["measures"][0], "direction": "higher"},
        {"name": "data", "unit": "log2 queries", "direction": "lower"},
    ]
    again = {**KEY_RECOVERY, "expected": 2, "measures": measures, "conditions": ["model"],
             "operation_id": "a"}  # fmt: skip
    ledger.define_category("p", again, MAIN)
    revised = ledger.frontier("p", "key-recovery").agent_revised
    assert revised is not None
    assert (revised.revision, revised.since, revised.author["role"]) == (3, 2, "main")
    assert revised.changed == [
        "measure time's direction: higher is better",
        "measure data added, lower is better",
        "measure memory removed",
        "condition key success removed",
    ]


# Dominates links (LG-22).


def dominating(*targets: str, expected: int, reason: str | None = "P1 is faster.") -> Any:
    links: list[dict[str, Any]] = []
    for target in targets:
        link: dict[str, Any] = {"kind": "dominates", "entry": target}
        if reason is not None:
            link["note"] = reason
        links.append(link)
    return {"entry": "R-1", "expected": expected, "links": links}


def test_a_dominates_link_decides_where_the_rule_cannot_compare(ledger: Ledger) -> None:
    """LG-F2: the user records that P1 dominates P4, whose time is an expression, with a
    reason, and P4 leaves the results not compared."""
    from .test_ledger import NEW
    from .test_ledger_decisions import decide

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)  # R-1
    change(ledger, published("P2", CLASSICAL, time=110, memory=80), op="p2", author=USER)  # R-2
    change(
        ledger, published("P4", CLASSICAL, time="$2^{0.29n}$", memory=70), op="p4", author=USER
    )  # R-3
    change(ledger, published("Q1", QUANTUM, time=90, memory=90), op="q1", author=USER)  # R-4
    project = result(conditions=CLASSICAL, measures={"time": "$2^{100}$", "memory": 50})
    change(ledger, {**project, "title": "R1"}, op="r1")  # R-5, proposed
    self_link = {"entry": "R-1", "expected": 1, "links": [
        {"kind": "dominates", "entry": "R-1", "note": "r"}]}  # fmt: skip
    for refused, message in (
        (dominating("R-3", expected=1, reason=None), "states its reason"),
        (self_link, "does not link to itself"),
        (dominating("R-5", expected=1), "R-5 is not eligible"),
        (dominating("R-2", expected=1), "compares R-1 and R-2 itself"),
    ):
        with pytest.raises(DocumentError, match=message):
            change(ledger, refused, op="refused", author=USER)
    with pytest.raises(DocumentError, match="not_permitted_for_role"):
        change(ledger, dominating("R-3", expected=1), op="delegated", author=DELEGATED)
    bounds = {**KEY_RECOVERY, "category": "bounds", "operation_id": "bounds"}
    ledger.define_category("p", bounds, MAIN)
    bound = published("B1", CLASSICAL, time=1, memory=1)
    bound["fields"] = {"category": "bounds", "conditions": CLASSICAL, "measures": {"time": 1}}
    change(ledger, bound, op="b1", author=USER)  # R-6
    with pytest.raises(DocumentError, match="not results of one frontier category"):
        change(ledger, dominating("R-6", expected=1), op="across", author=USER)
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [r.handle for r in group.not_compared] == ["R-3"]
    change(ledger, dominating("R-3", expected=1), op="p1-p4", author=USER)
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert (group.not_compared, [(r.handle, r.dominated_by) for r in group.dominated]) == (
        [],
        [("R-3", ["R-1"])],
    )
    # A link across conditions takes a best known result from its own group.
    change(ledger, dominating("R-3", "R-4", expected=2), op="p1-q1", author=USER)
    view = ledger.frontier("p", "key-recovery")
    quantum = best(view, QUANTUM)
    assert (quantum.best, [(r.handle, r.dominated_by) for r in quantum.dominated]) == (
        [],
        [("R-4", ["R-1"])],
    )
    # Only an eligible result's links count: once P1 is refuted, P4 is not compared again.
    change(ledger, decide("R-1"), {"entry": "R-1", "expected": 3, "status": "refuted"},
           op="refute", author=USER)  # fmt: skip
    view = ledger.frontier("p", "key-recovery")
    assert [r.handle for r in best(view, CLASSICAL).not_compared] == ["R-3"]
    assert [r.handle for r in best(view, QUANTUM).best] == ["R-4"]


def test_the_main_agent_records_a_dominates_link_too(ledger: Ledger) -> None:
    """LG-22: the user or the main agent; the main agent's reason is its text, checked."""
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)
    change(ledger, published("P4", CLASSICAL, time="$2^{0.29n}$", memory=70), op="p4", author=USER)
    change(ledger, dominating("R-2", expected=1), op="main")
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [(r.handle, r.dominated_by) for r in group.dominated] == [("R-2", ["R-1"])]


# The review of the frontier stack: M1 to M4 for dominates links.


def linked(ledger: Ledger) -> None:
    """The user's P1 (R-1); P3 (R-2), which P1 dominates by the rule; and P4 (R-3), whose
    time is an expression."""
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    for op, title, time, memory in (("p1", "P1", 120, 60), ("p3", "P3", 125, 65),
                                    ("p4", "P4", "$2^{0.29n}$", 70)):  # fmt: skip
        change(ledger, published(title, CLASSICAL, time=time, memory=memory), op=op, author=USER)


def test_a_link_the_rule_now_decides_counts_for_nothing(ledger: Ledger) -> None:
    """M1: P1 dominates P4 by a link; once P4's time is a number the rule compares them,
    and P4, faster, stayed dominated."""
    linked(ledger)
    change(ledger, dominating("R-3", expected=1), op="link", author=USER)
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [r.handle for r in group.dominated] == ["R-2", "R-3"]
    faster = published("P4", CLASSICAL, time=100, memory=70)["fields"]
    change(ledger, {"entry": "R-3", "expected": 1, "fields": faster}, op="number", author=USER)
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [r.handle for r in group.best] == ["R-1", "R-3"]
    assert [(r.handle, r.dominated_by) for r in group.dominated] == [("R-2", ["R-1"])]


def test_no_dominates_link_closes_a_cycle(ledger: Ledger) -> None:
    """M2: P4 dominated P1 by a link, P1 dominated P3 by the rule, and P3 dominated P4 by a
    link, so none was best known."""
    linked(ledger)
    p3_p4 = {"entry": "R-2", "links": [{"kind": "dominates", "entry": "R-3", "note": "Bounds."}]}
    change(ledger, {**p3_p4, "expected": 1}, op="p3-p4", author=USER)
    back = {"entry": "R-3", "expected": 1,
            "links": [{"kind": "dominates", "entry": "R-1", "note": "P4 is faster."}]}  # fmt: skip
    with pytest.raises(DocumentError, match="R-3 dominating R-1 closes a cycle: R-1 already"):
        change(ledger, back, op="cycle", author=USER)
    # Two links that close one in the same change.
    change(ledger, {"entry": "R-2", "expected": 2, "links": []}, op="unlink", author=USER)
    with pytest.raises(DocumentError, match="closes a cycle"):
        change(ledger, back, {**p3_p4, "expected": 3}, op="both", author=USER)
    change(ledger, back, op="alone", author=USER)
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [(r.handle, r.dominated_by) for r in group.dominated] == [
        ("R-2", ["R-1"]),
        ("R-1", ["R-3"]),
    ]


def test_each_links_reason_and_author_show_and_the_user_drops_one(ledger: Ledger) -> None:
    """M3: a link took a result from the best known without saying why or who, and only a
    revision of its holder's links removed it."""
    linked(ledger)
    change(ledger, dominating("R-3", expected=1, reason="Faster for n > 64."), op="link")
    change(ledger, {"entry": "R-1", "expected": 2, "title": "P1, renamed"}, op="t", author=USER)
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    (p4,) = [r for r in group.dominated if r.handle == "R-3"]
    assert [(k.handle, k.reason, k.author["role"], k.revision) for k in p4.linked_by] == [
        ("R-1", "Faster for n > 64.", "main", 2)
    ]
    with pytest.raises(DocumentError, match="only the user drops a dominates link"):
        ledger.drop_dominates("p", "R-1", "R-3", 3, MAIN, reason="No.", operation_id="main")
    with pytest.raises(DocumentError, match="R-1 holds no dominates link to R-2") as missing:
        ledger.drop_dominates("p", "R-1", "R-2", 3, USER, reason="No.", operation_id="none")
    assert missing.value.kind == "not_found"
    with pytest.raises(DocumentError) as stale:
        ledger.drop_dominates("p", "R-1", "R-3", 2, USER, reason="No.", operation_id="old")
    assert stale.value.kind == "conflict"
    why = "The bound needs n > 128."
    ledger.drop_dominates("p", "R-1", "R-3", 3, USER, reason=why, operation_id="drop")
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [r.handle for r in group.not_compared] == ["R-3"]
    latest = ledger.history("p", "R-1").revisions[0]
    assert (latest.revision, latest.reason, latest.author["kind"]) == (4, why, "user")
    assert ledger.entry("p", "R-1").links == []


def link(holder: str, target: str, expected: int, note: str = "By the paper.") -> Any:
    return {"entry": holder, "expected": expected,
            "links": [{"kind": "dominates", "entry": target, "note": note}]}  # fmt: skip


def test_a_cycle_is_read_as_the_frontier_shows_results(ledger: Ledger) -> None:
    """The verification's defect 3: the main agent disputes P1, rewriting its measures
    worse as it does; the frontier still shows P1 as recorded, ahead of P3 by the rule,
    but the cycle check read the rewrite, so P4 over P1 and P3 over P4 closed a cycle
    in the frontier, where nothing in the group was best known."""
    linked(ledger)
    worse = published("P1", CLASSICAL, time=130, memory=70)["fields"]
    change(ledger, {"entry": "R-1", "expected": 1, "status": "disputed", "fields": worse}, op="d")
    change(ledger, link("R-3", "R-1", 1), op="x-y", author=USER)
    with pytest.raises(DocumentError, match="R-2 dominating R-3 closes a cycle"):
        change(ledger, link("R-2", "R-3", 1), op="z-x", author=USER)
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [(r.handle, r.dominated_by) for r in group.dominated] == [
        ("R-2", ["R-1"]),
        ("R-1", ["R-3"]),
    ]


def test_a_cycle_a_later_change_makes_voids_its_links_and_says_why(ledger: Ledger) -> None:
    """The verification's defect 3, at read: with higher measures better, P3 is ahead of
    P1, and P4 over P1 and P3 over P4 make no cycle; once the user makes lower better
    again, P1 is ahead of P3 and the two links close one, so they count for nothing and
    the frontier says so, rather than leave no result best known."""
    linked(ledger)
    higher = [{**m, "direction": "higher"} for m in KEY_RECOVERY["measures"]]
    ledger.define_category(
        "p", {**KEY_RECOVERY, "expected": 1, "measures": higher, "operation_id": "up"}, USER
    )
    change(ledger, link("R-3", "R-1", 1), op="x-y", author=USER)
    change(ledger, link("R-2", "R-3", 1), op="z-x", author=USER)
    ledger.define_category("p", {**KEY_RECOVERY, "expected": 2, "operation_id": "down"}, USER)
    view = ledger.frontier("p", "key-recovery")
    group = best(view, CLASSICAL)
    assert ([r.handle for r in group.best], [r.handle for r in group.not_compared]) == (
        ["R-1"],
        ["R-3"],
    )
    assert [(k.handle, k.target, k.void) for k in view.void_links] == [
        ("R-2", "R-3", frontier_module().CYCLE),
        ("R-3", "R-1", frontier_module().CYCLE),
    ]


def frontier_module() -> Any:
    from sandbox_science.orchestrator.ledger import frontier

    return frontier


def test_a_link_dropped_by_the_user_confirms_nothing(ledger: Ledger) -> None:
    """The verification's defect 2: the user's dropping of a link an agent's published
    result held verified the result."""
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P4", CLASSICAL, time="$2^n$", memory=70), op="p4", author=USER)
    p1 = {**published("P1", CLASSICAL, time=120, memory=60),
          "links": [{"kind": "dominates", "entry": "R-1", "note": "Faster."}]}  # fmt: skip
    change(ledger, p1, op="p1")  # R-2, the agent's
    ledger.drop_dominates("p", "R-2", "R-1", 1, USER, reason="Not shown.", operation_id="d")
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [(r.handle, r.unverified) for r in group.candidates] == [("R-2", True)]


def test_an_agents_dominates_reason_holds_no_hidden_text(ledger: Ledger) -> None:
    """M4: #317's check of an agent's link notes holds for a dominates link's reason."""
    linked(ledger)
    with pytest.raises(DocumentError, match=r"link 1's note holds U\+202E"):
        change(ledger, dominating("R-3", expected=1, reason="P1 \u202eis faster"), op="hidden")


# The history (LG-24): LG-F2's "P2 stays recorded, and the history shows it best known
# until R1's decision".


def test_each_change_to_the_best_known_is_recorded_with_its_cause(ledger: Ledger) -> None:
    from datetime import datetime

    from .test_ledger import NEW
    from .test_ledger_decisions import decide

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)  # R-1
    change(ledger, published("P2", CLASSICAL, time=110, memory=80), op="p2", author=USER)  # R-2
    change(ledger, published("P3", CLASSICAL, time=125, memory=65), op="p3", author=USER)  # R-3
    change(ledger, published("Q1", QUANTUM, time=90, memory=90), op="q1", author=USER)  # R-4
    project = result(conditions=CLASSICAL, measures={"time": 105, "memory": 80})
    change(ledger, {**project, "title": "R1"}, op="r1")  # R-5, proposed: nothing changes
    history = ledger.frontier_history("p", "key-recovery")
    assert [(c.cause["operation_id"], c.added, c.removed) for c in history.changes] == [
        ("q1", ["R-4"], []),
        ("p2", ["R-2"], []),
        ("p1", ["R-1"], []),
    ]
    before = history.changes[1].created  # P1 and P2 best known
    change(ledger, decide("R-5"), {"entry": "R-5", "expected": 1, "status": "established"},
           op="est", author=USER)  # fmt: skip
    (latest,) = ledger.frontier_history("p", "key-recovery", limit=1).changes
    assert (latest.conditions, latest.added, latest.removed) == (CLASSICAL, ["R-5"], ["R-2"])
    assert latest.cause == {
        "author": "user:user",
        "entries": ["R-5"],
        "operation_id": "est",
        "reason": "Recorded.",
    }
    assert [m.handle for m in latest.best] == ["R-1", "R-5"]
    # P2 stays recorded, and the frontier as it was shows it best known.
    assert ledger.entry("p", "R-2").status == "recorded"
    then = ledger.frontier_past("p", "key-recovery", datetime.fromisoformat(before))
    (classical,) = [g for g in then.groups if g.conditions == CLASSICAL]
    assert [m.handle for m in classical.best] == ["R-1", "R-2"]
    page = ledger.frontier_history("p", "key-recovery", limit=2)
    assert page.next_before == page.changes[-1].seq
    rest = ledger.frontier_history("p", "key-recovery", before=page.next_before)
    assert len(page.changes) + len(rest.changes) == 4
    # A category revised to a new condition key leaves no result placed: the history
    # says each group's best known left, caused by the category's revision.
    revised = {**KEY_RECOVERY, "expected": 1, "conditions": ["model", "success", "data"],
               "operation_id": "rev"}  # fmt: skip
    ledger.define_category("p", revised, MAIN)
    left = [c for c in ledger.frontier_history("p", "key-recovery").changes
            if c.cause.get("category_revision") == 2]  # fmt: skip
    assert sorted(tuple(c.removed) for c in left) == [("R-1", "R-5"), ("R-4",)]
    kinds = [kind for kind, _ in category_events(ledger)]
    assert kinds.count("ledger.frontier_changed") == 5  # one for each change, not each group


def test_a_version_26_store_takes_frontier_changes_after_the_migration(
    ledger: Ledger, tmp_path: Path
) -> None:
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path

    from .control_versions import older_store

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    ledger.documents.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(store_path(tmp_path), store_path(old), migrations.BASELINE + 24)
    raw = sqlite3.connect(store_path(old))
    try:
        tables = {n for (n,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = raw.execute("PRAGMA user_version").fetchone()[0]
    finally:
        raw.close()
    assert (version, "frontier_categories" in tables, "frontier_changes" in tables) == (
        26,
        True,
        False,
    )
    migrated = DocumentStore(store_path(old))
    migrated.initialize()
    try:
        moved = Ledger(migrated)
        assert moved.frontier_history("p", "key-recovery").changes == []
        moved.change(
            "p",
            {"operation_id": "r", "reason": "r", "changes": [result(
                conditions=CLASSICAL, measures={"time": 1, "memory": 1}) | {
                "origin": "project"}]},
            USER,
        )  # fmt: skip
    finally:
        migrated.close()


def test_a_category_older_than_its_history_gets_a_baseline_first(
    ledger: Ledger, tmp_path: Path
) -> None:
    """L2: P1 was best known before version 27; the first change after it recorded P1 as
    added by that change, which did not touch it."""
    from sandbox_science.orchestrator.control import migrations
    from sandbox_science.orchestrator.documents import DocumentStore, store_path

    from .control_versions import older_store
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    change(ledger, published("P1", CLASSICAL, time=120, memory=60), op="p1", author=USER)
    ledger.documents.close()
    old = tmp_path / "old"
    old.mkdir()
    older_store(store_path(tmp_path), store_path(old), migrations.BASELINE + 24)
    migrated = DocumentStore(store_path(old))
    migrated.initialize()
    try:
        moved = Ledger(migrated)
        p2 = published("P2", CLASSICAL, time=110, memory=80)
        moved.change("p", {"operation_id": "p2", "reason": "P2.", "changes": [p2]}, USER)
        changes = moved.frontier_history("p", "key-recovery").changes
        assert [
            (c.cause.get("operation_id"), c.cause.get("baseline"), c.added) for c in changes
        ] == [
            ("p2", None, ["R-2"]),
            (None, "frontier history begins", ["R-1"]),
        ]
    finally:
        migrated.close()


def test_the_history_keeps_long_conditions_and_says_why(ledger: Ledger) -> None:
    """L1, M3, M4: conditions over the history's 8192 bytes aborted a valid change; a
    group's spelling, from another result, made it a new group; and a cause did not say
    why or which links."""
    from .test_ledger import NEW
    from .test_ledger_decisions import decide

    keys = [f"k{n}" for n in range(16)]
    ledger.define_category("p", {**KEY_RECOVERY, "conditions": keys}, USER)
    change(ledger, NEW["reference"], op="ref")
    wide = dict.fromkeys(keys, "\x01" * 256)
    change(ledger, published("W", wide, time=1, memory=1), op="wide", author=USER)  # R-1
    (first,) = ledger.frontier_history("p", "key-recovery").changes
    assert (first.added, len(first.conditions["#"])) == (["R-1"], 64)
    # One group, whichever result spells it.
    narrow = dict.fromkeys(keys, "a")
    change(ledger, published("P1", narrow, time=120, memory=60), op="p1", author=USER)  # R-2
    change(ledger, published("P2", dict.fromkeys(keys, "A"), time=125, memory=65),
           op="p2", author=USER)  # fmt: skip
    change(ledger, decide("R-2"), {"entry": "R-2", "expected": 1, "status": "refuted"},
           op="refute", author=USER)  # fmt: skip
    latest = ledger.frontier_history("p", "key-recovery").changes[0]
    assert (latest.added, latest.removed, latest.cause["reason"]) == (["R-3"], ["R-2"], "Recorded.")
    # P2 dominates W, under other conditions, by a link, and the user drops it.
    link = [{"kind": "dominates", "entry": "R-1", "note": "P2 is faster for every n."}]
    change(ledger, {"entry": "R-3", "expected": 1, "links": link}, op="link", author=USER)
    latest = ledger.frontier_history("p", "key-recovery").changes[0]
    assert (latest.removed, latest.cause["dominates"]) == (
        ["R-1"],
        ["R-3 dominates R-1: P2 is faster for every n."],
    )
    ledger.drop_dominates("p", "R-3", "R-1", 2, USER, reason="Not for n < 8.", operation_id="d")
    latest = ledger.frontier_history("p", "key-recovery").changes[0]
    assert (latest.added, latest.cause["reason"], latest.cause["dominates"]) == (
        ["R-1"],
        "Not for n < 8.",
        ["R-3 no longer dominates R-1"],
    )


def test_a_dominated_result_names_the_best_known_that_dominate_it(ledger: Ledger) -> None:
    """The write lock's comparisons: P3 is dominated by P1 and by P2, which P1 dominates
    too; it names P1, the best known."""
    from .test_ledger import NEW

    ledger.define_category("p", KEY_RECOVERY, MAIN)
    change(ledger, NEW["reference"], op="ref")
    for n, (time, memory) in enumerate(((120, 70), (110, 60), (100, 50))):
        change(ledger, published(f"P{3 - n}", CLASSICAL, time=time, memory=memory),
               op=f"p{n}", author=USER)  # fmt: skip
    group = best(ledger.frontier("p", "key-recovery"), CLASSICAL)
    assert [r.handle for r in group.best] == ["R-3"]
    assert [(r.handle, r.dominated_by) for r in group.dominated] == [
        ("R-1", ["R-3"]),
        ("R-2", ["R-3"]),
    ]
