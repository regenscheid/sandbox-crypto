# SPDX-License-Identifier: MIT
"""Schema validation of policy files (fail closed on anything unexpected)."""

from __future__ import annotations

from datetime import timedelta
from textwrap import dedent, indent

import pytest
from pydantic import ValidationError

from sandbox_science.policy import Action, PolicyLoadError, parse_policy
from sandbox_science.policy.model import ProviderRef

HEADER = dedent(
    """\
    apiVersion: sandbox-science/v0alpha1
    kind: ProjectPolicy
    metadata: {name: test}
    """
)


def policy_text(body: str) -> str:
    return HEADER + dedent(body)


def rules_text(*rules: str) -> str:
    items = "".join(indent(dedent(rule), "  ") for rule in rules)
    return HEADER + "network:\n  rules:\n" + items


def server_text(server: str) -> str:
    return HEADER + "mcp:\n  servers:\n    srv:\n" + indent(dedent(server), "      ")


def test_opam_and_hackage_take_an_action() -> None:
    text = policy_text("packages: {opam: {action: deny}, hackage: {action: escalate}}\n")
    packages = parse_policy(text).policy.packages
    assert packages.opam is not None
    assert packages.hackage is not None
    assert (packages.opam.action, packages.hackage.action) == (Action.DENY, Action.ESCALATE)
    defaults = parse_policy(policy_text("packages: {opam: {}, hackage: {}}\n")).policy.packages
    assert defaults.opam is not None
    assert defaults.hackage is not None
    assert (defaults.opam.action, defaults.hackage.action) == (Action.ALLOW, Action.ALLOW)


def test_defaults() -> None:
    policy = parse_policy(HEADER).policy
    assert policy.defaults.action is Action.ESCALATE
    assert policy.escalation.timeout == timedelta(minutes=30)
    assert policy.mcp.proxy.worker_access is False
    assert policy.packages.conda is None
    assert policy.network.rules == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("kind: ProjectPolicy\nmetadata: {name: test}\n", "apiVersion"),
        (HEADER.replace("v0alpha1", "v1"), "apiVersion"),
        (HEADER.replace("ProjectPolicy", "Policy"), "kind"),
        (HEADER + "unknown: 1\n", "unknown"),
        (HEADER.replace("name: test", "name: Not_A_Name"), "metadata.name"),
        (policy_text("defaults: {action: permit}\n"), "defaults.action"),
        (policy_text("defaults: {action: allow, extra: 1}\n"), "extra"),
        (policy_text("escalation: {on_timeout: allow}\n"), "policy-wide on_timeout"),
        (policy_text("escalation: {timeout: 0s}\n"), "duration"),
        (policy_text("escalation: {notify: []}\n"), "notify"),
        (policy_text("packages: {system_packages: allow}\n"), "system_packages"),
        (policy_text("packages: {conda: {channels: []}}\n"), "channels"),
        (policy_text("packages: {opam: {channels: [default]}}\n"), "channels"),
        (policy_text("packages: {hackage: {action: permit}}\n"), "hackage.action"),
        (policy_text("packages: {maven: {action: allow}}\n"), "maven"),
        (
            policy_text("packages: {conda: {channels: ['https://repo.example/x']}}\n"),
            "channels",
        ),
        (policy_text("resources: {cpu: 0}\n"), "cpu"),
        (policy_text("resources: {memory: 64GB}\n"), "size"),
        (policy_text("data: {default_class: secret}\n"), "default_class"),
        (policy_text("monitors: [{name: m, when: {}}]\n"), "at least one condition"),
    ],
)
def test_rejects_invalid_policies(text: str, message: str) -> None:
    with pytest.raises(PolicyLoadError, match=message):
        parse_policy(text)


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (
            "- {name: r, operation: any, match: {hosts: ['*']}, action: allow}\n",
            "does not match any of the expected tags",
        ),
        ("- {name: r, operation: http, match: {hosts: []}, action: allow}\n", "hosts"),
        ("- {name: r, operation: http, match: {hosts: ['ex ample']}, action: allow}\n", "host"),
        ("- {name: r, operation: http, match: {hosts: ['*.1.2.3.4']}, action: allow}\n", "host"),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], methods: [get]}, action: allow}\n",
            "methods",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], methods: [CONNECT]},"
            " action: allow}\n",
            "methods",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], ports: [0]}, action: allow}\n",
            "ports",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], paths: [v1]}, action: allow}\n",
            "start with",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], paths: ['/']}, action: allow}\n",
            "omit paths",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], paths: ['/a/../b']},"
            " action: allow}\n",
            "segments",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], paths: ['/a%2Fb']},"
            " action: allow}\n",
            "decoded",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], git_service: upload-pack}, "
            "action: allow}\n",
            "git_service",
        ),
        (
            "- {name: r, operation: git, match: {hosts: ['*'], methods: [GET]}, action: allow}\n",
            "methods",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], methods: [GET]}, action: allow,"
            " escalation: {timeout: 5m}}\n",
            "need action: escalate",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*'], methods: [GET, POST]}, "
            "action: escalate, escalation: {on_timeout: allow}}\n",
            "read-only",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*']}, action: escalate, "
            "escalation: {on_timeout: allow}}\n",
            "read-only",
        ),
        (
            "- {name: r, operation: git, match: {hosts: ['*']}, action: escalate, "
            "escalation: {on_timeout: allow}}\n",
            "read-only",
        ),
        (
            "- {name: r, operation: http, match: {hosts: ['*']}, limits: {max_url_bytes: 0}, "
            "action: allow}\n",
            "max_url_bytes",
        ),
    ],
)
def test_rejects_invalid_rules(rule: str, message: str) -> None:
    with pytest.raises(PolicyLoadError, match=message):
        parse_policy(rules_text(rule))


def test_rule_names_are_unique_across_rules_and_monitors() -> None:
    text = (
        rules_text("- {name: same, operation: http, match: {hosts: ['*']}, action: allow}\n")
        + "monitors: [{name: same, when: {egress_bytes_per_hour_over: 1Gi}}]\n"
    )
    with pytest.raises(PolicyLoadError, match="unique: same"):
        parse_policy(text)


def test_read_only_rules_may_time_out_to_allow() -> None:
    policy = parse_policy(
        rules_text(
            "- {name: slow-read, operation: http, match: {hosts: ['*'], methods: [GET, HEAD]},"
            " action: escalate, escalation: {timeout: 5m, on_timeout: allow}}\n",
            "- {name: slow-fetch, operation: git, match: {hosts: ['*'], git_service: upload-pack},"
            " action: escalate, escalation: {on_timeout: allow}}\n",
        )
    ).policy
    assert [rule.read_only for rule in policy.network.rules] == [True, True]


@pytest.mark.parametrize(
    ("server", "message"),
    [
        (
            "url: http://mcp.example.org/x\nrevision: '1'\nauth: {mode: none}\n",
            "https",
        ),
        (
            "url: https://user:pw@mcp.example.org/x\nrevision: '1'\nauth: {mode: none}\n",
            "credentials",
        ),
        ("url: https://mcp.example.org/x\nauth: {mode: none}\n", "revision"),
        (
            "url: https://mcp.example.org/x\nrevision: '1'\nauth: {mode: magic}\n",
            "does not match any of the expected tags",
        ),
        (
            "url: https://mcp.example.org/x\nrevision: '1'\n"
            "auth: {mode: oauth, registration: dcr, provider: p, client_id: abc}\n",
            "dynamic client registration",
        ),
        (
            "url: https://mcp.example.org/x\nrevision: '1'\n"
            "auth: {mode: oauth, registration: preregistered, provider: p}\n",
            "client_id",
        ),
        (
            "url: https://mcp.example.org/x\nrevision: '1'\n"
            "auth: {mode: oauth, registration: dcr, provider: p, scopes: ['a b']}\n",
            "scopes",
        ),
        (
            "url: https://mcp.example.org/x\nrevision: '1'\n"
            "auth: {mode: headers, headers: {X-Api-Key: test-secret-123}}\n",
            "literal secrets",
        ),
        (
            "url: https://mcp.example.org/x\nrevision: '1'\n"
            "auth: {mode: headers, headers: {Authorization: 'Bearer test-secret-123'}}\n",
            "literal secrets",
        ),
        (
            "url: https://mcp.example.org/x\nrevision: '1'\n"
            "auth: {mode: headers, headers: {X-Account: '${provider:p}'}}\n",
            "malformed placeholder",
        ),
        (
            "url: https://mcp.example.org/x\nrevision: '1'\nauth: {mode: none}\n"
            "tools: {allow: ['*search*'], deny: ['*search*']}\n",
            "both allow and deny",
        ),
        (
            "url: https://mcp.example.org/x\nrevision: '1'\nauth: {mode: none}\n"
            "tools: {allow: ['search?']}\n",
            "invalid tool pattern",
        ),
    ],
)
def test_rejects_invalid_mcp_servers(server: str, message: str) -> None:
    with pytest.raises(PolicyLoadError, match=message):
        parse_policy(server_text(server))


@pytest.mark.parametrize(
    "headers",
    [
        "{X-Api-Key: '${provider:srv-key.API_KEY}', X-Account: research}",
        "{Authorization: 'Bearer ${provider:srv-key.TOKEN}'}",
        "{Authorization: 'Basic ${provider:srv-key.BASIC}'}",
    ],
)
def test_accepts_placeholder_headers(headers: str) -> None:
    parse_policy(
        server_text(
            "url: https://mcp.example.org/x\nrevision: '1'\n"
            f"auth: {{mode: headers, headers: {headers}}}\n"
        )
    )


def test_worker_tools_must_be_inherit_or_patterns() -> None:
    with pytest.raises(PolicyLoadError, match="worker_tools"):
        parse_policy(policy_text("mcp: {proxy: {worker_tools: everything}}\n"))
    policy = parse_policy(policy_text("mcp: {proxy: {worker_tools: ['*search*']}}\n")).policy
    assert policy.mcp.proxy.worker_tools == ("*search*",)


def test_provider_names_are_unique() -> None:
    text = policy_text(
        "credentials: {providers: [{name: gh, profile: github, hosts: [api.github.com]}, "
        "{name: gh, profile: github, hosts: [github.com]}]}\n"
    )
    with pytest.raises(PolicyLoadError, match="provider names must be unique"):
        parse_policy(text)


def provider(hosts: str) -> str:
    return policy_text(
        f"credentials: {{providers: [{{name: gh, profile: token, hosts: {hosts}}}]}}\n"
    )


def test_providers_name_the_hosts_their_credential_may_reach() -> None:
    policy = parse_policy(provider("['API.GitHub.com.', github.com]")).policy
    assert policy.credentials.providers[0].hosts == ("api.github.com", "github.com")
    assert policy.credentials.hosts == frozenset({"api.github.com", "github.com"})
    assert parse_policy(HEADER).policy.credentials.hosts == frozenset()


@pytest.mark.parametrize(
    ("hosts", "message"),
    [
        ("[]", "at least 1 item"),
        ("['*.github.com']", "name each host exactly"),
        ("['*']", "name each host exactly"),
        ("[140.82.112.3]", "name the host, not an address"),
        ("['[2606:4700::1111]']", "name the host, not an address"),
        ("[github.com, GitHub.com]", "lists a host twice"),
        ("[42]", "must be strings"),
    ],
)
def test_provider_hosts_are_exact_names(hosts: str, message: str) -> None:
    with pytest.raises(PolicyLoadError, match=message):
        parse_policy(provider(hosts))


def test_provider_hosts_must_be_ascii() -> None:
    # Policy files are ASCII already; the model also refuses other sources.
    with pytest.raises(ValidationError, match="ASCII"):
        ProviderRef.model_validate({"name": "gh", "profile": "token", "hosts": ["bücher.example"]})


def test_providers_need_hosts() -> None:
    with pytest.raises(PolicyLoadError, match="hosts"):
        parse_policy(policy_text("credentials: {providers: [{name: gh, profile: token}]}\n"))


def test_a_host_is_bound_to_one_provider() -> None:
    text = policy_text(
        "credentials: {providers: [{name: a, profile: token, hosts: [api.github.com]}, "
        "{name: b, profile: token, hosts: [github.com, api.github.com]}]}\n"
    )
    with pytest.raises(PolicyLoadError, match=r"more than one provider: api\.github\.com"):
        parse_policy(text)


def test_models_are_frozen() -> None:
    policy = parse_policy(HEADER).policy
    with pytest.raises(ValueError, match="frozen"):
        policy.defaults.action = Action.ALLOW  # type: ignore[misc]
