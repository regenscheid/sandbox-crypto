# SPDX-License-Identifier: MIT
"""Effect classes: resolution precedence, shipped defaults, browser automation, lockdown and
the checked-in classifications against their pinned listings (D95, D96, D97)."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.policy import (
    Action,
    Decision,
    HttpRequest,
    LoadedPolicy,
    PolicyLoadError,
    PoolCeiling,
    evaluate_mcp_message,
    evaluate_request,
    host_reach,
    load_policy,
    parse_policy,
)
from sandbox_science.policy.classification import (
    catalog,
    catalog_names,
    load_classification,
)
from sandbox_science.policy.effects import SHIPPED, EffectClass, MaxGrant
from sandbox_science.policy.mcp_wire import tool_digest

OPEN = PoolCeiling.unrestricted()
ROOT = Path(__file__).resolve().parents[3]
PINS = Path(__file__).parent / "fixtures" / "mcp-tools" / "crypto-research-mcp"
SERVER = "crypto-research-mcp"

HEADER = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata: {name: classes}
"""


def policy(server: str = "", mcp: str = "", top: str = "") -> LoadedPolicy:
    """A policy with crypto-research-mcp under its checked-in classification."""
    return parse_policy(
        HEADER
        + top
        + "mcp:\n"
        + mcp
        + "  servers:\n    crypto-research-mcp:\n"
        + "      url: https://mcp.example.org/crypto\n      revision: '2025-11-25'\n"
        + "      auth: {mode: none}\n      classification: crypto-research-mcp\n"
        + server
    )


def call(
    loaded: LoadedPolicy,
    tool: str,
    arguments: dict[str, Any] | None = None,
    hints: dict[str, EffectClass] | None = None,
) -> Decision:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    return evaluate_mcp_message(SERVER, message, loaded, ceiling=OPEN, hints=hints)


SCRAPE = "firecrawl-mcp_firecrawl_scrape"
BROWSING = {"url": "https://example.org/", "actions": [{"type": "click", "selector": "#go"}]}


# --- Precedence ---------------------------------------------------------------------------------


def test_the_classification_file_gives_the_base_class() -> None:
    decision = call(policy(), SCRAPE, {"url": "https://example.org/"})
    assert (decision.action, decision.effect) == (Action.ALLOW, "read")
    assert "classification" in decision.reason


def test_an_argument_rule_wins_over_the_base_class() -> None:
    decision = call(policy(), SCRAPE, BROWSING)
    assert (decision.action, decision.effect) == (Action.DENY, "browse")
    assert "argument rule actions set" in decision.reason


def test_the_policy_wins_over_the_classification_file() -> None:
    loaded = policy(server=f"      classify: {{{SCRAPE}: write}}\n")
    decision = call(loaded, SCRAPE, {"url": "https://example.org/"})
    assert (decision.action, decision.effect) == (Action.ESCALATE, "write")
    assert "policy" in decision.reason


def test_argument_rules_still_apply_over_the_policy_class() -> None:
    loaded = policy(server=f"      classify: {{{SCRAPE}: write}}\n")
    assert call(loaded, SCRAPE, BROWSING).effect == "browse"


def test_the_policy_and_the_file_win_over_hints() -> None:
    hints = {SCRAPE: EffectClass.READ, "firecrawl-mcp_firecrawl_feedback": EffectClass.READ}
    assert call(policy(), "firecrawl-mcp_firecrawl_feedback", hints=hints).effect == "write"
    loaded = policy(server=f"      classify: {{{SCRAPE}: admin}}\n")
    assert call(loaded, SCRAPE, hints=hints).effect == "admin"


@pytest.mark.parametrize(
    ("hint", "effect", "action"),
    [
        (EffectClass.READ, "read", Action.ALLOW),
        (EffectClass.UNCLASSIFIED, "unclassified", Action.ESCALATE),
        (None, "unclassified", Action.ESCALATE),
    ],
)
def test_an_unclassified_tool_takes_its_hint(
    hint: EffectClass | None, effect: str, action: Action
) -> None:
    tool = "e-print-mcp_new_tool"
    hints = {tool: hint} if hint is not None else {}
    decision = call(policy(), tool, hints=hints)
    assert (decision.effect, decision.action) == (effect, action)


def test_the_most_restrictive_of_several_argument_rules_wins() -> None:
    crawl = "firecrawl-mcp_firecrawl_crawl"
    arguments = {
        "url": "https://example.org/",
        "webhook": "https://hooks.example/x",
        "scrapeOptions": {"profile": {"name": "p", "saveChanges": True}},
    }
    # communicate (session) is narrower than write (project), both escalate.
    assert call(policy(), crawl, arguments).effect == "communicate"
    arguments["scrapeOptions"] = {"actions": [{"type": "press"}]}
    assert call(policy(), crawl, arguments).effect == "browse"


@pytest.mark.parametrize("value", [None, False, [], {}, "", "no-not-a-bool-but-a-string"])
def test_a_rule_needs_its_argument_set_or_equal(value: object) -> None:
    arguments = {"url": "https://example.org/", "actions": value}
    if isinstance(value, str) and value:
        assert call(policy(), SCRAPE, arguments).effect == "browse"  # any non-empty value
    else:
        assert call(policy(), SCRAPE, arguments).effect == "read"
    save = {"url": "https://example.org/", "profile": {"name": "p", "saveChanges": 1}}
    assert call(policy(), SCRAPE, save).effect == "read"  # 1 is not true


def test_a_tool_rule_holds_unless_an_argument_rule_makes_the_call_stricter() -> None:
    loaded = policy(server=f"      tools: {{allow: [{SCRAPE}]}}\n")
    plain = call(loaded, SCRAPE, {"url": "https://example.org/"})
    assert plain.action is Action.ALLOW
    assert plain.rule is not None
    assert "tools.allow" in plain.rule
    assert call(loaded, SCRAPE, BROWSING).action is Action.DENY
    denied = policy(server='      tools: {deny: ["firecrawl-mcp_*"]}\n')
    assert call(denied, SCRAPE, {"url": "https://example.org/"}).action is Action.DENY


# --- Defaults and ceilings ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "effect", "action", "grant"),
    [
        ("e-print-mcp_search_eprint", "read", Action.ALLOW, MaxGrant.NONE),
        ("firecrawl-mcp_firecrawl_feedback", "write", Action.ESCALATE, MaxGrant.PROJECT),
        (
            "firecrawl-mcp_firecrawl_monitor_create",
            "communicate",
            Action.ESCALATE,
            MaxGrant.SESSION,
        ),
        ("firecrawl-mcp_firecrawl_interact", "browse", Action.DENY, MaxGrant.NONE),
        ("e-print-mcp_refresh_eprint", "admin", Action.ESCALATE, MaxGrant.ONCE),
        ("web-fetch-mcp_made_up", "unclassified", Action.ESCALATE, MaxGrant.ONCE),
    ],
)
def test_each_shipped_default(tool: str, effect: str, action: Action, grant: MaxGrant) -> None:
    decision = call(policy(), tool)
    assert (decision.effect, decision.action, decision.max_grant) == (effect, action, grant.value)
    assert SHIPPED[EffectClass(effect)].action is action


def test_class_actions_per_server_override_the_cross_server_ones() -> None:
    loaded = policy(
        mcp="  effects: {write: {action: deny}, communicate: {action: allow}}\n",
        server="      effects: {write: {action: escalate, max_grant: once}}\n",
    )
    feedback = call(loaded, "firecrawl-mcp_firecrawl_feedback")
    assert (feedback.action, feedback.max_grant) == (Action.ESCALATE, "once")
    assert call(loaded, "firecrawl-mcp_firecrawl_monitor_create").action is Action.ALLOW


@pytest.mark.parametrize(
    "effects",
    ["{read: {action: allow, max_grant: project}}", "{write: {action: deny, max_grant: once}}"],
)
def test_a_grant_ceiling_needs_an_escalation(effects: str) -> None:
    with pytest.raises(PolicyLoadError, match="max_grant applies only to action: escalate"):
        policy(mcp=f"  effects: {effects}\n")


def test_a_deny_default_denies_unclassified_tools() -> None:
    loaded = policy(top="defaults: {action: deny}\n")
    decision = call(loaded, "web-fetch-mcp_made_up")
    assert (decision.action, decision.max_grant) == (Action.DENY, "none")


def test_an_unknown_classification_is_refused() -> None:
    with pytest.raises(PolicyLoadError, match="no checked-in classification"):
        parse_policy(
            HEADER
            + "mcp:\n  servers:\n    s:\n      url: https://mcp.example.org/x\n"
            + "      revision: '1'\n      auth: {mode: none}\n      classification: nothing-here\n"
        )


# --- Browser automation -------------------------------------------------------------------------


def test_browser_automation_is_off_by_default_then_escalates_once_enabled() -> None:
    off = call(policy(), "firecrawl-mcp_firecrawl_agent", {"prompt": "find"})
    assert (off.effect, off.action) == ("browse", Action.DENY)
    enabled = policy(mcp="  browser_automation: enabled\n")
    on = call(enabled, "firecrawl-mcp_firecrawl_agent", {"prompt": "find"})
    assert (on.effect, on.action, on.max_grant) == ("browse", Action.ESCALATE, "once")
    assert call(enabled, SCRAPE, BROWSING).action is Action.ESCALATE


def test_browse_settings_need_browser_automation() -> None:
    with pytest.raises(PolicyLoadError, match="browser_automation: enabled"):
        policy(mcp="  effects: {browse: {action: allow}}\n")
    loaded = policy(mcp="  browser_automation: enabled\n  effects: {browse: {action: allow}}\n")
    assert call(loaded, "firecrawl-mcp_firecrawl_interact").action is Action.ALLOW


# --- Lockdown -----------------------------------------------------------------------------------


def test_locked_mcp_refuses_a_tool_the_policy_does_not_name() -> None:
    loaded = policy(top="lockdown: {mcp: locked}\n")
    added = call(loaded, "e-print-mcp_new_tool", hints={"e-print-mcp_new_tool": EffectClass.READ})
    assert (added.action, added.rule) == (Action.DENY, "lockdown.mcp")
    assert call(loaded, "e-print-mcp_search_eprint").action is Action.ALLOW
    named = policy(
        top="lockdown: {mcp: locked}\n", server="      classify: {e-print-mcp_new_tool: read}\n"
    )
    assert call(named, "e-print-mcp_new_tool").action is Action.ALLOW


def test_declared_mcp_lets_a_new_tool_take_its_class() -> None:
    hints = {"e-print-mcp_new_tool": EffectClass.READ}
    assert call(policy(), "e-print-mcp_new_tool", hints=hints).action is Action.ALLOW


ALLOWLIST = """\
lockdown:
  network: allowlist
  hosts: [eprint.iacr.org, "*.arxiv.org", 1.1.1.0/24]
  unlisted: {unlisted}
network:
  rules:
    - {{name: reads, operation: http, match: {{hosts: ["*"], methods: [GET, HEAD]}}, action: allow}}
"""


def allowlisted(unlisted: str = "deny") -> LoadedPolicy:
    return parse_policy(HEADER + ALLOWLIST.format(unlisted=unlisted))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://eprint.iacr.org/2024/1.pdf", Action.ALLOW),
        ("https://export.arxiv.org/abs/1", Action.ALLOW),
        ("https://1.1.1.7/x", Action.ALLOW),
        ("https://example.org/", Action.DENY),
        ("https://arxiv.org/", Action.DENY),  # *.arxiv.org lists the names under it only
        ("https://8.8.8.8/x", Action.DENY),
    ],
)
def test_an_allowlist_denies_unlisted_hosts(url: str, expected: Action) -> None:
    decision = evaluate_request(HttpRequest("GET", url), allowlisted(), ceiling=OPEN)
    assert decision.action is expected, decision.reason
    if expected is Action.DENY:
        assert decision.rule == "lockdown.network"


def test_an_allowlist_can_escalate_unlisted_hosts() -> None:
    decision = evaluate_request(
        HttpRequest("GET", "https://example.org/"), allowlisted("escalate"), ceiling=OPEN
    )
    assert (decision.action, decision.rule) == (Action.ESCALATE, "lockdown.network")
    assert decision.escalation is not None
    assert decision.escalation.on_timeout is Action.DENY


def test_an_allowlist_never_widens_the_rules() -> None:
    loaded = allowlisted()
    post = evaluate_request(
        HttpRequest("POST", "https://eprint.iacr.org/submit"), loaded, ceiling=OPEN
    )
    assert post.action is Action.ESCALATE  # listed, but no rule allows a POST
    assert host_reach("example.org", 443, loaded, ceiling=OPEN).action is Action.DENY


@pytest.mark.parametrize(
    "text",
    [
        "lockdown: {hosts: [example.org]}\n",
        "lockdown: {unlisted: escalate}\n",
        "lockdown: {network: allowlist, hosts: ['*']}\n",
        "lockdown: {network: allowlist, hosts: [192.0.2.1/24]}\n",
    ],
)
def test_malformed_lockdowns_are_refused(text: str) -> None:
    with pytest.raises(PolicyLoadError):
        parse_policy(HEADER + text)


# --- The checked-in classifications and their pins ---------------------------------------------


def pinned_listings() -> dict[str, list[dict[str, Any]]]:
    """Each pinned listing's tools, by the file's group."""
    return {
        path.stem: json.loads(path.read_text())["tools"] for path in sorted(PINS.glob("*.json"))
    }


GROUPS = {
    "attack-catalog-mcp",
    "e-print-mcp",
    "firecrawl-mcp",
    "nist-csrc-mcp",
    "nist-mcp",
    "paper-search-mcp",
    "patent-search-mcp",
    "portal",
    "web-fetch-mcp",
}


def test_every_server_behind_the_portal_has_a_classification() -> None:
    found = catalog(SERVER)
    assert {file.prefix.removesuffix("_") for file in found.files} == GROUPS
    assert catalog_names() >= {SERVER}


def test_every_pinned_tool_is_classified_with_its_digest() -> None:
    """The drift test: a server's tool list (names, descriptions and schemas) as pinned must
    be what its classification pins; a change fails here with the difference."""
    found = catalog(SERVER)
    for group, tools in pinned_listings().items():
        listed = {tool["name"]: tool_digest(tool) for tool in tools}
        pinned = {
            name: entry.sha256
            for name, entry in found.tools.items()
            if name.startswith(f"{group}_") and entry.sha256 is not None
        }
        if listed != pinned:
            lines = [f"{name} {digest}" for name, digest in sorted(listed.items())]
            want = [f"{name} {digest}" for name, digest in sorted(pinned.items())]
            diff = "\n".join(
                difflib.unified_diff(want, lines, "classification", "listing", lineterm="")
            )
            pytest.fail(f"{group}'s listing drifted from its classification:\n{diff}")


def test_only_tools_missing_from_the_pinned_listings_go_unpinned() -> None:
    unpinned = sorted(name for name, entry in catalog(SERVER).tools.items() if entry.sha256 is None)
    assert unpinned == [
        "firecrawl-mcp_firecrawl_monitor_delete",  # in lab 39's listing, not the 2026-10-24 one
        *sorted(f"nist-mcp_{name}" for name in (
            "csrc_currency", "csrc_file", "csrc_fulltext", "csrc_stats", "get_csrc",
            "refresh_csrc", "search_csrc",
        )),  # the 2026-10-10 notes; nist-mcp was not listed on 2026-10-15 or 2026-10-24
    ]  # fmt: skip


def _schema_paths(schema: Any, prefix: str = "") -> set[str]:
    """Every argument path a JSON schema's object properties name, through objects and
    the items of arrays (an array's own path names it, as rules do)."""
    paths: set[str] = set()
    if not isinstance(schema, dict):
        return paths
    for option in schema.get("anyOf", []):
        paths |= _schema_paths(option, prefix)
    for name, sub in (schema.get("properties") or {}).items():
        path = f"{prefix}{name}"
        paths.add(path)
        paths |= _schema_paths(sub, f"{path}.")
    return paths


def test_rules_and_significant_arguments_name_arguments_of_the_pinned_schemas() -> None:
    schemas = {
        tool["name"]: tool["inputSchema"] for tools in pinned_listings().values() for tool in tools
    }
    for name, entry in catalog(SERVER).tools.items():
        if name not in schemas:
            assert not entry.rules, name  # unpinned: no schema to check a rule against
            continue
        paths = _schema_paths(schemas[name])
        named = [rule.argument for rule in entry.rules] + list(entry.significant.values())
        assert set(named) <= paths, (name, sorted(set(named) - paths))
        for rule in entry.rules:
            if rule.equals is not None:
                leaf = schemas[name]
                for part in rule.argument.split("."):
                    leaf = leaf["properties"][part]
                assert leaf["type"] == type(rule.equals).__name__.replace("bool", "boolean")


def test_h1_firecrawl_tools_have_their_argument_rules() -> None:
    tools = catalog(SERVER).tools

    def rules(tool: str) -> set[tuple[str, str]]:
        return {(rule.argument, rule.effect.value) for rule in tools[f"firecrawl-mcp_{tool}"].rules}

    assert rules("firecrawl_scrape") == {("actions", "browse"), ("profile.saveChanges", "write")}
    assert {("webhook", "communicate"), ("scrapeOptions.actions", "browse")} <= rules(
        "firecrawl_crawl"
    )
    assert tools["firecrawl-mcp_firecrawl_agent"].effect is EffectClass.BROWSE
    assert tools["firecrawl-mcp_firecrawl_feedback"].effect is EffectClass.WRITE
    assert tools["e-print-mcp_refresh_eprint"].effect is EffectClass.ADMIN
    assert tools["firecrawl-mcp_firecrawl_monitor_create"].effect is EffectClass.COMMUNICATE


def test_a_classification_directory_is_read_strictly(tmp_path: Path) -> None:
    good = (
        "apiVersion: sandbox-science/v0alpha1\nkind: ToolClassification\nserver: s\n"
        'prefix: a_\ntools:\n  a_x: {class: read, reason: "Reads."}\n'
    )
    (tmp_path / "a.yaml").write_text(good)
    assert set(load_classification(tmp_path, "s").tools) == {"a_x"}
    with pytest.raises(PolicyLoadError, match="not 'other'"):
        load_classification(tmp_path, "other")
    (tmp_path / "b.yaml").write_text(good.replace("prefix: a_", "prefix: ''"))
    with pytest.raises(PolicyLoadError, match="another file has"):
        load_classification(tmp_path, "s")
    (tmp_path / "b.yaml").write_text(good.replace("a_x", "b_x"))
    with pytest.raises(PolicyLoadError, match="without the prefix"):
        load_classification(tmp_path, "s")
    (tmp_path / "b.yaml").write_text(
        good.replace("prefix: a_", "prefix: c_").replace(
            'a_x: {class: read, reason: "Reads."}',
            'c_x: {class: read, reason: "R.", rules: [{argument: u, class: write}]}',
        )
    )
    with pytest.raises(PolicyLoadError, match="exactly one of present"):
        load_classification(tmp_path, "s")


def test_the_example_policy_uses_the_classification() -> None:
    loaded = load_policy(ROOT / "examples" / "policy.yaml")
    assert loaded.policy.mcp.servers[SERVER].classification == SERVER
    assert loaded.policy.mcp.browser_automation == "disabled"


MATRIX = json.loads((Path(__file__).parent / "fixtures" / "mcp-class-matrix.json").read_text())


@pytest.mark.parametrize("entry", MATRIX["calls"], ids=lambda entry: entry["tool"])
def test_the_shared_matrix_under_the_example_policy(entry: dict[str, Any]) -> None:
    """The library's half of the agreement check; the proxy and the middleware run the same
    matrix through their own paths."""
    loaded = load_policy(ROOT / "examples" / "policy.yaml")
    decision = call(loaded, entry["tool"], entry["arguments"])
    assert (decision.effect, decision.action.value) == (entry["effect"], entry["action"])
