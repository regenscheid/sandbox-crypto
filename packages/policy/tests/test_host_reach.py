# SPDX-License-Identifier: MIT
"""host_reach: the least restrictive decision any request to a host and port can get (D69)."""

import textwrap

import pytest

from sandbox_science.policy import (
    Action,
    ClassificationError,
    HardFloor,
    LoadedPolicy,
    PoolCeiling,
    host_reach,
    parse_policy,
)

HEADER = """\
apiVersion: sandbox-science/v0alpha1
kind: ProjectPolicy
metadata:
  name: test
"""

UNRESTRICTED = PoolCeiling.unrestricted()


def load(body: str = "") -> LoadedPolicy:
    return parse_policy(HEADER + textwrap.dedent(body))


def reach(
    policy: LoadedPolicy,
    host: str,
    port: int = 443,
    *,
    ceiling: PoolCeiling = UNRESTRICTED,
    floor: HardFloor | None = None,
) -> Action:
    decision = host_reach(host, port, policy, ceiling=ceiling, floor=floor or HardFloor())
    return decision.action


READ_ANYWHERE = """\
defaults: {action: deny}
network:
  rules:
    - name: read
      operation: http
      match: {hosts: ["*"], methods: [GET, HEAD]}
      action: allow
"""


def test_the_default_action_decides_hosts_no_rule_names() -> None:
    assert reach(load(), "papers.example.org") is Action.ESCALATE
    assert reach(load("defaults: {action: deny}\n"), "papers.example.org") is Action.DENY
    assert reach(load("defaults: {action: alert}\n"), "papers.example.org") is Action.ALERT


def test_a_wildcard_read_rule_reaches_every_public_host_on_its_ports() -> None:
    policy = load(READ_ANYWHERE)
    assert reach(policy, "papers.example.org") is Action.ALLOW
    assert reach(policy, "papers.example.org", 80) is Action.ALLOW
    # Without ports a rule matches only the scheme's default port.
    assert reach(policy, "papers.example.org", 8443) is Action.DENY


def test_rule_ports_are_reached() -> None:
    policy = load(
        """\
        defaults: {action: deny}
        network:
          rules:
            - name: alt-port
              operation: http
              match: {hosts: ["*.example.org"], ports: [8443]}
              action: allow
        """
    )
    assert reach(policy, "data.example.org", 8443) is Action.ALLOW
    assert reach(policy, "data.example.org", 443) is Action.DENY
    assert reach(policy, "data.example.net", 8443) is Action.DENY


def test_an_exact_deny_shadows_a_wildcard_allow() -> None:
    policy = load(
        READ_ANYWHERE
        + """\
    - name: blocked
      operation: http
      match: {hosts: [blocked.example.org]}
      action: deny
"""
    )
    assert reach(policy, "blocked.example.org") is Action.DENY
    assert reach(policy, "other.example.org") is Action.ALLOW


def test_a_deny_that_covers_part_of_a_host_leaves_the_rest_reachable() -> None:
    policy = load(
        READ_ANYWHERE
        + """\
    - name: private-path
      operation: http
      match: {hosts: [docs.example.org], paths: [/private]}
      action: deny
    - name: no-posts
      operation: http
      match: {hosts: [api.example.org], methods: [POST, GET, HEAD]}
      action: deny
"""
    )
    assert reach(policy, "docs.example.org") is Action.ALLOW
    # GET and HEAD are denied there; no other method is allowed anywhere.
    assert reach(policy, "api.example.org") is Action.DENY


def test_a_method_no_rule_names_falls_to_the_default() -> None:
    policy = load(
        """\
        defaults: {action: allow}
        network:
          rules:
            - name: every-named-method
              operation: http
              match:
                hosts: [api.example.org]
                methods: [GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS, TRACE]
              action: deny
        """
    )
    assert reach(policy, "api.example.org") is Action.ALLOW


def test_a_path_rule_is_reached_through_its_path() -> None:
    policy = load(
        """\
        defaults: {action: deny}
        network:
          rules:
            - name: papers-only
              operation: http
              match: {hosts: ["*"], paths: [/papers], methods: [GET]}
              action: allow
        """
    )
    assert reach(policy, "archive.example.org") is Action.ALLOW


def test_the_least_restrictive_outcome_wins() -> None:
    policy = load(
        """\
        defaults: {action: deny}
        network:
          rules:
            - name: writes
              operation: http
              match: {hosts: ["*"], methods: [POST]}
              action: escalate
            - name: reads
              operation: http
              match: {hosts: ["*"], methods: [GET]}
              action: alert
        """
    )
    assert reach(policy, "archive.example.org") is Action.ALERT


def test_git_rules_reach_hosts_too() -> None:
    policy = load(
        """\
        defaults: {action: deny}
        network:
          rules:
            - name: push
              operation: git
              match: {hosts: ["*"], git_service: receive-pack}
              action: escalate
        """
    )
    assert reach(policy, "git.example.org") is Action.ESCALATE


def test_the_floor_and_the_ceiling_deny_whatever_the_rules_say() -> None:
    policy = load(READ_ANYWHERE)
    assert reach(policy, "metadata.google.internal") is Action.DENY
    admin = HardFloor(admin_hosts=frozenset({"admin.example.org"}))
    assert reach(policy, "admin.example.org", floor=admin) is Action.DENY
    ceiling = PoolCeiling.model_validate(
        {"apiVersion": "sandbox-science/v0alpha1", "network": {"deny_hosts": ["*.example.net"]}}
    )
    assert reach(policy, "x.example.net", ceiling=ceiling) is Action.DENY
    assert reach(policy, "x.example.org", ceiling=ceiling) is Action.ALLOW


def test_reserved_hosts_are_not_reached() -> None:
    policy = load(
        READ_ANYWHERE
        + """\
mcp:
  servers:
    research:
      url: https://mcp.example.org/mcp
      revision: "2025-11-25"
      auth: {mode: none}
      tools: {allow: [search]}
"""
    )
    assert reach(policy, "pypi.org") is Action.DENY
    assert reach(policy, "mcp.example.org") is Action.DENY


def test_non_public_names_need_an_exact_rule() -> None:
    assert reach(load(READ_ANYWHERE), "printer.local") is Action.DENY
    policy = load(
        """\
        defaults: {action: deny}
        network:
          rules:
            - name: printer
              operation: http
              match: {hosts: [printer.local], methods: [GET]}
              action: allow
        """
    )
    assert reach(policy, "printer.local") is Action.ALLOW


def test_the_decision_explains_a_refusal() -> None:
    policy = load(READ_ANYWHERE)
    decision = host_reach("pypi.org", 443, policy, ceiling=UNRESTRICTED)
    assert decision.layer == "floor"
    assert "package cache" in decision.reason


@pytest.mark.parametrize(("host", "port"), [("bad host", 443), ("ok.example.org", 0)])
def test_invalid_hosts_and_ports_are_refused(host: str, port: int) -> None:
    with pytest.raises(ClassificationError):
        host_reach(host, port, load(), ceiling=UNRESTRICTED)


def test_a_rule_path_no_request_can_have_is_skipped() -> None:
    # The model accepts "::$" in a path, but the classifier refuses any request with
    # it, so such a rule matches nothing and must not break the search (review M1).
    policy = load(
        READ_ANYWHERE
        + """\
    - name: streams
      operation: http
      match: {hosts: [files.example.org], paths: ["/files::$DATA"]}
      action: deny
"""
    )
    assert reach(policy, "files.example.org") is Action.ALLOW
