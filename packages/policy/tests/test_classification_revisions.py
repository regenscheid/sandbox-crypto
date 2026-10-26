# SPDX-License-Identifier: MIT
"""Project classification revisions (D96, CL-3): loading one from its bytes, the policy
store's revisions, bindings and drafts, the resolution order, the widening rule, the
catalog-name validator, and the drift ledger's acceptances by project."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.policy import (
    Action,
    Decision,
    LoadedPolicy,
    PolicyLoadError,
    PoolCeiling,
    evaluate_mcp_message,
    parse_policy,
)
from sandbox_science.policy.approvals.ledger import RecordLike
from sandbox_science.policy.classification import (
    catalog,
    changed_tools,
    revision_classification,
    server_classification,
    widening_reasons,
    widenings,
)
from sandbox_science.policy.detection import DriftLedger, pin_digest
from sandbox_science.policy.effects import EffectClass
from sandbox_science.policy.openshell import AgentMcpTarget, compile_agent_mcp
from sandbox_science.policy.store import Acceptance, PolicyStore, StoreError

OPEN = PoolCeiling.unrestricted()
SERVER = "crypto-research-mcp"
SCRAPE = "firecrawl-mcp_firecrawl_scrape"
FEEDBACK = "firecrawl-mcp_firecrawl_feedback"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
HEADER = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata: {name: revisions}
"""


def policy(server: str = "      classification: crypto-research-mcp\n") -> LoadedPolicy:
    return parse_policy(
        HEADER
        + "mcp:\n  servers:\n    crypto-research-mcp:\n"
        + "      url: https://mcp.example.org/crypto\n      revision: '2025-11-25'\n"
        + "      auth: {mode: none}\n"
        + server
    )


def revision(tools: str, server: str = SERVER, prefix: str = "") -> bytes:
    return (
        "apiVersion: sandbox-science/v0alpha1\nkind: ToolClassification\n"
        f"server: {server}\nprefix: {prefix!r}\npinned: '2026-10-25'\ntools:\n{tools}"
    ).encode()


def entry(name: str, effect: str, extra: str = "") -> str:
    return f'  {name}:\n    class: {effect}\n    reason: "Test."\n{extra}'


def call(
    loaded: LoadedPolicy, tool: str, arguments: dict[str, Any] | None = None, **options: Any
) -> Decision:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    return evaluate_mcp_message(SERVER, message, loaded, ceiling=OPEN, **options)


def accept(store: PolicyStore, data: bytes, project: str = "p1") -> str:
    found = store.accept_classification(
        project, SERVER, data, Acceptance("user", NOW, "reviewed", None, None, {})
    )
    assert found.revision is not None
    return found.revision


# --- Loading a revision from its bytes ----------------------------------------------------------


def test_a_revision_loads_from_its_bytes_in_the_strict_dialect() -> None:
    data = revision(entry(SCRAPE, "write"))
    found = revision_classification(data, SERVER)
    assert found.revision is not None
    assert found.digest == "sha256:" + found.revision
    assert found.tools[SCRAPE].effect is EffectClass.WRITE
    assert found.pinned == "2026-10-25"
    assert revision_classification(data, SERVER) is found  # cached by its content


def test_a_revision_of_another_server_or_in_a_loose_dialect_is_refused() -> None:
    with pytest.raises(PolicyLoadError, match="classifies 'other', not"):
        revision_classification(revision(entry(SCRAPE, "read"), server="other"), SERVER)
    with pytest.raises(PolicyLoadError):
        revision_classification(revision(entry(SCRAPE, "!!str read")), SERVER)
    with pytest.raises(PolicyLoadError):
        revision_classification(revision(entry(SCRAPE, "sometimes")), SERVER)


# --- The policy store ---------------------------------------------------------------------------


def test_an_accepted_revision_is_stored_bound_and_recorded(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    assert store.classification("p1", SERVER) is None
    data = revision(entry(SCRAPE, "write"))
    hexdigest = accept(store, data)
    root = tmp_path / "classifications" / "p1" / SERVER
    assert (root / f"{hexdigest}.yaml").read_bytes() == data
    assert store.classification_binding("p1", SERVER) == hexdigest
    found = store.classification("p1", SERVER)
    assert found is not None
    assert found.tools[SCRAPE].effect is EffectClass.WRITE
    record = store.acceptance("p1", SERVER, hexdigest)
    assert record is not None
    assert (record.accepted_by, record.reason) == ("user", "reviewed")
    assert store.classification("p2", SERVER) is None  # another project keeps the catalog's
    # A newer revision is seen at once; restoring the older one, or none, too.
    newer = accept(store, revision(entry(SCRAPE, "admin")))
    in_force = store.classification("p1", SERVER)
    assert in_force is not None
    assert in_force.tools[SCRAPE].effect is EffectClass.ADMIN
    store.restore_classification("p1", SERVER, hexdigest)
    assert store.classification_binding("p1", SERVER) == hexdigest
    store.restore_classification("p1", SERVER, None)
    assert store.classification("p1", SERVER) is None
    assert newer != hexdigest


def test_a_tampered_or_invalid_revision_is_refused(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    with pytest.raises(StoreError, match="invalid"):
        store.accept_classification(
            "p1", SERVER, revision(entry(SCRAPE, "nope")), Acceptance("user", NOW, "r")
        )
    hexdigest = accept(store, revision(entry(SCRAPE, "write")))
    path = tmp_path / "classifications" / "p1" / SERVER / f"{hexdigest}.yaml"
    path.write_bytes(revision(entry(SCRAPE, "read")))
    with pytest.raises(StoreError, match="does not hold"):
        store.classification("p1", SERVER)
    (path.parent / "current.json").write_text('{"revision": "../x"}')
    with pytest.raises(StoreError):
        store.classification("p1", SERVER)
    for project, server in (("../p", SERVER), ("p1", "Bad/Server")):
        with pytest.raises(StoreError):
            store.classification(project, server)


def test_drafts_are_stored_by_digest_and_listed_newest_first(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    first = store.add_classification_draft(
        "p1", SERVER, revision(entry(SCRAPE, "read")), created_at=NOW, model="main"
    )
    later = datetime(2026, 10, 25, 13, 0, tzinfo=UTC)
    second = store.add_classification_draft(
        "p1", SERVER, revision(entry(SCRAPE, "write")), created_at=later, model=None
    )
    assert [d.draft for d in store.classification_drafts("p1", SERVER)] == [
        second.draft,
        first.draft,
    ]
    found = store.classification_draft("p1", SERVER, first.draft)
    assert found is not None
    assert (found.model, found.created_at) == ("main", NOW)
    assert store.classification_drafts("p2", SERVER) == []
    assert store.classification_draft("p1", SERVER, "0" * 64) is None
    with pytest.raises(StoreError, match="not a classification"):
        store.add_classification_draft("p1", SERVER, b"tools: {}\n", created_at=NOW, model="main")
    # A draft never puts anything in force.
    assert store.classification("p1", SERVER) is None


# --- Resolution order ---------------------------------------------------------------------------


def test_a_projects_revision_replaces_the_catalog_for_its_calls() -> None:
    loaded = policy()
    assert call(loaded, SCRAPE).action is Action.ALLOW  # the catalog: a read
    project = revision_classification(revision(entry(SCRAPE, "write")), SERVER)
    decided = call(loaded, SCRAPE, classification=project)
    assert (decided.action, decided.effect) == (Action.ESCALATE, "write")
    # The revision stands for the whole server: a tool it leaves out takes its hints.
    left_out = call(loaded, FEEDBACK, classification=project)
    assert left_out.effect == "unclassified"
    hinted = call(loaded, FEEDBACK, classification=project, hints={FEEDBACK: EffectClass.READ})
    assert hinted.effect == "read"


def test_the_policys_own_classify_entries_win_over_a_revision() -> None:
    loaded = policy("      classification: crypto-research-mcp\n      classify:\n"
                    f"        {SCRAPE}: read\n")  # fmt: skip
    project = revision_classification(revision(entry(SCRAPE, "admin")), SERVER)
    assert call(loaded, SCRAPE, classification=project).effect == "read"


def test_a_revision_applies_only_where_the_policy_names_a_classification() -> None:
    loaded = policy(server="")
    entry_ = loaded.policy.mcp.servers[SERVER]
    project = revision_classification(revision(entry(SCRAPE, "read")), SERVER)
    assert server_classification(entry_, project) is None
    assert call(loaded, SCRAPE, classification=project).effect == "unclassified"


def test_the_catalog_name_is_still_required_to_name_a_checked_in_classification() -> None:
    with pytest.raises(PolicyLoadError, match="no checked-in classification"):
        policy("      classification: not-in-the-catalog\n")
    assert server_classification(policy().policy.mcp.servers[SERVER]) is catalog(SERVER)


def test_the_fence_admits_tools_by_the_projects_revision() -> None:
    loaded = policy()
    target = AgentMcpTarget(
        binaries=("/usr/bin/python3",),
        middleware="sandbox-science-policy",
        project="p1",
        caller="agent",
    )
    project = revision_classification(revision(entry("brand_new_tool", "read")), SERVER)
    plain = compile_agent_mcp(loaded, target, ceiling=OPEN)
    revised = compile_agent_mcp(loaded, target, ceiling=OPEN, classifications={SERVER: project})
    assert "brand_new_tool" not in plain.policy.to_yaml()
    assert "brand_new_tool" in revised.policy.to_yaml()


# --- Widenings ----------------------------------------------------------------------------------


def test_a_widening_is_a_call_that_would_pass_where_it_asks_or_is_denied_now() -> None:
    loaded = policy()
    current = catalog(SERVER)
    scrape = current.tools[SCRAPE]
    rules = "".join(
        f"      - {{argument: {rule.argument}, "
        + ("present: true" if rule.present else f"equals: {str(rule.equals).lower()}")
        + f", class: {rule.effect}}}\n"
        for rule in scrape.rules
    )
    kept = entry(SCRAPE, "read", "    rules:\n" + rules)
    narrower = revision_classification(revision(kept + entry(FEEDBACK, "admin")), SERVER)
    # Feedback was a write that asks; admin asks too. Every other tool is left out, and the
    # left-out writes and admin tools could become reads by their hints: widenings.
    found = widenings(loaded.policy, SERVER, current, narrower)
    assert SCRAPE not in found
    assert FEEDBACK not in found
    assert "firecrawl-mcp_firecrawl_monitor_create" in found
    assert "attack-catalog-mcp_search_attacks" not in found  # a read stays a read
    # Dropping scrape's browse rule lets a call with page actions pass: a widening.
    no_rules = revision_classification(revision(entry(SCRAPE, "read")), SERVER)
    assert SCRAPE in widenings(loaded.policy, SERVER, current, no_rules)
    # A write made a read widens; a read made a write does not.
    loosened = revision_classification(revision(entry(FEEDBACK, "read")), SERVER)
    assert FEEDBACK in widenings(loaded.policy, SERVER, current, loosened)
    tightened = revision_classification(revision(entry(SCRAPE, "write")), SERVER)
    assert SCRAPE not in widenings(loaded.policy, SERVER, current, tightened)
    # Under a policy that allows writes, a write made a read widens nothing.
    lenient = parse_policy(
        HEADER + "mcp:\n  effects:\n    write: {action: allow}\n  servers:\n"
        "    crypto-research-mcp:\n      url: https://mcp.example.org/crypto\n"
        "      revision: '2025-11-25'\n      auth: {mode: none}\n"
        "      classification: crypto-research-mcp\n"
    )
    assert FEEDBACK not in widenings(lenient.policy, SERVER, current, loosened)


def test_the_diff_names_added_removed_and_changed_tools() -> None:
    before = revision_classification(revision(entry("a", "read") + entry("b", "read")), SERVER)
    after = revision_classification(revision(entry("b", "write") + entry("c", "read")), SERVER)
    assert changed_tools(before, after) == {"added": ["c"], "removed": ["a"], "changed": ["b"]}


# --- The drift ledger's acceptances -------------------------------------------------------------


class Rec:
    def __init__(self, seq: int, kind: str, payload: Mapping[str, object]) -> None:
        self.seq, self.kind, self.payload = seq, kind, payload
        self.time = "2026-10-25T12:00:00Z"

    def data(self) -> dict[str, object]:
        return dict(self.payload)


def test_an_acceptance_naming_a_project_ends_the_hold_for_it_alone() -> None:
    pin = pin_digest({"search": "a" * 64})
    ledger = DriftLedger()
    drift: dict[str, object] = {"server": "papers", "pin": pin, "listing": "L1"}
    records: list[RecordLike] = [Rec(1, "mcp-tools-drift", drift)]
    ledger.load(records)
    assert ledger.drifted("papers", pin, "p1") == ledger.drifted("papers", pin, "p2") == 1
    accepted = {"server": "papers", "project": "p1", "pin": pin, "listing": "L1"}
    ledger.load([Rec(2, "mcp-tools-accepted", accepted)])
    assert ledger.drifted("papers", pin, "p1") is None
    assert ledger.drifted("papers", pin, "p2") == 1
    ledger.load([Rec(3, "mcp-classification-accepted", {"server": "papers", "project": "p2"})])
    assert ledger.drifted("papers", pin, "p2") is None
    # The same listing seen again is no new drift; another listing holds both again.
    assert ledger.known("papers", pin, "L1")
    ledger.load([Rec(4, "mcp-tools-drift", {**drift, "listing": "L2"})])
    assert ledger.drifted("papers", pin, "p1") == 4
    assert ledger.drifted("papers", pin, "p2") == 4
    # The operator's acceptance, which names no project, ends it for every project.
    ledger.load([Rec(5, "mcp-tools-accepted", {"server": "papers"})])
    assert ledger.drifted("papers", pin, "p1") is None
    assert ledger.latest("papers", pin) is None


def test_drifts_from_different_pins_hold_their_own_projects() -> None:
    pin_a, pin_b = pin_digest({"search": "a" * 64}), pin_digest({"search": "b" * 64})
    ledger = DriftLedger()
    ledger.load([Rec(1, "mcp-tools-drift", {"server": "papers", "pin": pin_a, "listing": "X"})])
    assert ledger.drifted("papers", pin_a, "A") == 1
    # Project B, under a revision of its own (pin B), records its own drift: A stays held.
    ledger.load([Rec(2, "mcp-tools-drift", {"server": "papers", "pin": pin_b, "listing": "X"})])
    assert ledger.drifted("papers", pin_a, "A") == 1
    assert ledger.drifted("papers", pin_b, "B") == 2
    # A's acceptance ends A's hold only, and leaves B's drift and B's hold alone.
    accepted = {"server": "papers", "project": "A", "pin": pin_a, "listing": "X"}
    ledger.load([Rec(3, "mcp-tools-accepted", accepted)])
    assert ledger.drifted("papers", pin_a, "A") is None
    assert ledger.drifted("papers", pin_b, "B") == 2
    assert ledger.drifted("papers", pin_a, "C") == 1  # another project under pin A


def test_an_acceptance_of_an_older_listing_does_not_end_a_newer_drift() -> None:
    pin = pin_digest({"search": "a" * 64})
    ledger = DriftLedger()
    ledger.load([Rec(1, "mcp-tools-drift", {"server": "papers", "pin": pin, "listing": "L2"})])
    stale = {"server": "papers", "project": "p1", "pin": pin, "listing": "L1"}
    ledger.load([Rec(2, "mcp-tools-accepted", stale)])
    assert ledger.drifted("papers", pin, "p1") == 1


def test_a_later_record_of_an_accepted_listing_never_ends_a_hold() -> None:
    # Review of 2026-10-26: listing A accepted, the server changes to B (held), and a record
    # of A again (a runtime's replay) must not end B's hold. Only an acceptance ends one.
    pin = pin_digest({"search": "a" * 64})
    drift: dict[str, object] = {"server": "papers", "pin": pin}
    ledger = DriftLedger()
    ledger.load([Rec(1, "mcp-tools-drift", {**drift, "listing": "A"})])
    accepted = {"server": "papers", "project": "p1", "pin": pin, "listing": "A"}
    ledger.load([Rec(2, "mcp-tools-accepted", accepted)])
    assert ledger.drifted("papers", pin, "p1") is None
    ledger.load([Rec(3, "mcp-tools-drift", {**drift, "listing": "B"})])
    assert ledger.drifted("papers", pin, "p1") == 3
    ledger.load([Rec(4, "mcp-tools-drift", {**drift, "listing": "A"})])
    assert ledger.drifted("papers", pin, "p1") == 3
    # A record for another project under the same pin does not end p1's hold either.
    ledger.load([Rec(5, "mcp-tools-drift", {**drift, "listing": "A", "project": "p2"})])
    assert ledger.drifted("papers", pin, "p1") == 3
    # Both unaccepted drifts are kept: accepting B leaves C, recorded between, holding.
    ledger.load([Rec(6, "mcp-tools-drift", {**drift, "listing": "C"})])
    ledger.load([Rec(7, "mcp-tools-accepted", {**accepted, "listing": "B"})])
    assert ledger.drifted("papers", pin, "p1") == 6
    assert [seen.listing for seen in ledger.unaccepted("papers", pin, "p1")] == ["C"]
    both = {**accepted, "listing": "C", "listings": ["C", "B"]}
    ledger.load([Rec(8, "mcp-tools-accepted", both)])
    assert ledger.drifted("papers", pin, "p1") is None
    assert ledger.drifted("papers", pin, "p2") == 6  # p2 accepted nothing


def test_widenings_take_an_unmentioned_tool_at_its_decision_time_class() -> None:
    loaded = policy()
    current = catalog(SERVER)
    new = "firecrawl-mcp_brand_new_delete_everything"
    proposed = revision_classification(revision(entry(new, "read")), SERVER)
    # No hint: unclassified, which asks; read passes, so it widens.
    assert call(loaded, new).action is Action.ESCALATE
    assert call(loaded, new, classification=proposed).action is Action.ALLOW
    reasons = widening_reasons(loaded.policy, SERVER, current, proposed)
    assert reasons[new] == ["read passes without asking, where unclassified is escalate"]
    # A read-only hint made it a read already: classifying it read widens nothing.
    hinted = widening_reasons(loaded.policy, SERVER, current, proposed, {new: EffectClass.READ})
    assert new not in hinted


def test_widenings_name_a_wider_ceiling_and_a_class_grants_would_cover() -> None:
    loaded = policy()
    current = revision_classification(revision(entry(FEEDBACK, "admin")), SERVER)
    proposed = revision_classification(revision(entry(FEEDBACK, "write")), SERVER)
    reasons = widening_reasons(loaded.policy, SERVER, current, proposed)
    assert reasons[FEEDBACK] == [
        "grant ceiling once -> project (admin -> write)",
        f"any grant of write on {SERVER} (up to project) would cover it",
    ]
    # Back to admin narrows: nothing is listed.
    assert FEEDBACK not in widening_reasons(loaded.policy, SERVER, proposed, current)


def test_an_unreadable_draft_does_not_hide_the_others(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    good = store.add_classification_draft(
        "p1", SERVER, revision(entry(SCRAPE, "read")), created_at=NOW, model="main"
    )
    bad = store.add_classification_draft(
        "p1", SERVER, revision(entry(SCRAPE, "write")), created_at=NOW, model="main"
    )
    (tmp_path / "classification-drafts" / "p1" / SERVER / f"{bad.draft}.json").unlink()
    assert [d.draft for d in store.classification_drafts("p1", SERVER)] == [good.draft]
