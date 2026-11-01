# SPDX-License-Identifier: MIT
"""The bundle's opt-in Loki alert rules (D103): absent by default, one ConfigMap of valid
rule YAML with every first alert when ``[alerts]`` is there."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from sandbox_science.launcher.kubernetes_bundle import ALERT_RULES_FILE, ALERTS, render
from sandbox_science.launcher.kubernetes_config import load

from .test_kubernetes_bundle import configured, values

# A shape check of the rendered LogQL, not a parser: each rule is one ``sum by (...)`` of a
# ``count_over_time`` over a stream selector, line filters, ``| json`` with named fields and
# label filters, compared with a number, or two of those joined by ``unless``. It catches a
# broken selector, a stray quote or bracket, or a stage out of place.
_MATCHER = r'[a-z_]+(?:=|!=|=~|!~)"[^"\\]*"'
_SELECTOR = rf"\{{{_MATCHER}(?:, {_MATCHER})*\}}"
_STAGE = rf'(?:\|= `[^`]+`|\| json [a-z_]+="[a-z_]+"(?:, [a-z_]+="[a-z_]+")*|\| {_MATCHER})'
_RANGE = rf"count_over_time\({_SELECTOR}(?: {_STAGE})* ?\[\d+[mh]\]\)"
_SUM = rf"sum by \([a-z_]+(?:, [a-z_]+)*\) \({_RANGE}\) > \d+"
_RULE = re.compile(rf"{_SUM}|\({_SUM}\) unless \({_SUM}\)")


def logql_shape(expr: str) -> bool:
    return _RULE.fullmatch(expr) is not None


def test_the_shape_check_refuses_broken_rules() -> None:
    good = 'sum by (service) (count_over_time({namespace="n", level="error"}[5m])) > 1'
    assert logql_shape(good)
    for broken in (
        good.replace("}[5m]", "[5m]"),
        good.replace('level="error"', "level=error"),
        good.replace("[5m]", "[5]"),
        good.replace("> 1", ""),
        good.replace("[5m]", " | json kind=kind [5m]"),
    ):
        assert not logql_shape(broken), broken


def _alerts(changes: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = values()
    data["alerts"] = changes or {}
    items: list[dict[str, Any]] = json.loads(json.dumps(render(configured(data))))["items"]
    return [item for item in items if item["metadata"]["name"] == "research-alert-rules"]


def _rules(config_map: dict[str, Any]) -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(config_map["data"][ALERT_RULES_FILE])
    (group,) = document["groups"]
    assert group["name"] == "sandbox-science-research"
    return {rule["alert"]: rule for rule in group["rules"]}


def test_no_alert_rules_without_the_section() -> None:
    items = json.loads(json.dumps(render(configured())))["items"]
    assert not [item for item in items if "alert-rules" in item["metadata"]["name"]]


def test_the_section_renders_one_config_map_of_every_rule() -> None:
    (config_map,) = _alerts()
    assert config_map["kind"] == "ConfigMap"
    metadata = config_map["metadata"]
    assert metadata["namespace"] == "research-test"
    assert metadata["labels"]["loki_rule"] == "true"
    assert metadata["labels"]["app.kubernetes.io/instance"] == "research"
    assert metadata["annotations"] == {"k8s-sidecar-target-directory": "/rules/fake"}
    rules = _rules(config_map)
    assert tuple(rules) == ALERTS
    for rule in rules.values():
        expr = rule["expr"]
        # Only this instance's lines, in the shape every rule has.
        assert '{namespace="research-test", instance="research"' in expr
        assert logql_shape(expr), expr
        assert rule["labels"]["severity"] in ("warning", "critical")
        assert rule["labels"]["instance"] == "research"
        assert rule["annotations"]["summary"]
        assert rule["annotations"]["description"]


def test_each_rule_reads_the_event_its_service_logs() -> None:
    rules = _rules(_alerts()[0])
    errors = rules["SandboxScienceServiceErrors"]["expr"]
    assert 'level=~"error|critical"' in errors
    assert errors.endswith("[5m])) > 10")
    manager = rules["SandboxScienceManagerUnreachable"]
    assert manager["for"] == "10m"
    assert 'service="runtime-manager"' in manager["expr"]
    assert '`"event":"runtime_manager_note"`' in manager["expr"]
    assert 'kind=~"gateway_unreachable|control_unreachable"' in manager["expr"]
    creation = rules["SandboxScienceCreationUnknown"]["expr"]
    assert '`"event":"environment_creation_unknown"`' in creation
    assert "sum by (service, project, environment)" in creation
    # Longer than the worker's creation_recheck between passes (30 s by default).
    assert "[15m]" in creation
    changed = rules["SandboxScienceMcpCanaryChanged"]["expr"]
    assert '`"event":"mcp_canary_changed"`' in changed
    assert 'status!="listed"' in changed
    # Holds while every canary in the window failed: no listed result among them.
    failing = rules["SandboxScienceMcpCanaryFailing"]["expr"]
    assert '`"event":"mcp_canary"`' in failing
    assert ") unless (" in failing
    assert 'status!="listed" [20m]' in failing
    assert 'status="listed" [20m]' in failing
    hold = rules["SandboxSciencePolicyHold"]["expr"]
    assert 'service="middleware"' in hold
    assert '`"event":"detect_finding"`' in hold
    assert 'outcome=~"hold|hold_call"' in hold
    # A held call's record names no project, so the rule does not group by one.
    assert "sum by (service, rule, outcome)" in hold
    assert "project" not in hold


def test_the_event_names_are_the_ones_the_services_log() -> None:
    from sandbox_science.launcher.runtime_manager.manager import NOTE_EVENT
    from sandbox_science.orchestrator.mcp_canaries import CHANGED, EVENT
    from sandbox_science.worker.environments import CREATION_UNKNOWN

    rules = _rules(_alerts()[0])
    assert f'"event":"{NOTE_EVENT}"' in rules["SandboxScienceManagerUnreachable"]["expr"]
    assert f'"event":"{CREATION_UNKNOWN}"' in rules["SandboxScienceCreationUnknown"]["expr"]
    assert f'"event":"{CHANGED}"' in rules["SandboxScienceMcpCanaryChanged"]["expr"]
    assert f'"event":"{EVENT}"' in rules["SandboxScienceMcpCanaryFailing"]["expr"]


def test_network_drops_stay_a_commented_rule() -> None:
    text = _alerts()[0]["data"][ALERT_RULES_FILE]
    assert "# Network-policy drops: not rendered" in text
    assert "SandboxScienceNetworkDenials" not in _rules(_alerts()[0])


def test_the_section_settings() -> None:
    (config_map,) = _alerts(
        {
            "namespace": "monitoring",
            "tenant": "research",
            "configmap_labels": {"loki-rules": "sandbox"},
            "error_threshold": 3,
            "canary_window_minutes": 45,
        }
    )
    failing = _rules(config_map)["SandboxScienceMcpCanaryFailing"]["expr"]
    assert failing.count("[45m]") == 2
    assert config_map["metadata"]["namespace"] == "monitoring"
    assert config_map["metadata"]["labels"]["loki-rules"] == "sandbox"
    assert "loki_rule" not in config_map["metadata"]["labels"]
    assert config_map["metadata"]["annotations"]["k8s-sidecar-target-directory"] == (
        "/rules/research"
    )
    # The selector still names the instance's own namespace, where its Pods log.
    errors = _rules(config_map)["SandboxScienceServiceErrors"]["expr"]
    assert 'namespace="research-test"' in errors
    assert errors.endswith("> 3")


@pytest.mark.parametrize(
    "changes",
    [
        {"error_threshold": 0},
        {"canary_window_minutes": 1},
        {"configmap_labels": {}},
        {"configmap_labels": {"bad label!": "x"}},
        {"namespace": "Not_A_Name"},
        {"tenant": "../etc"},
        {"rules": []},
    ],
)
def test_bad_alert_settings_are_refused(changes: dict[str, Any]) -> None:
    data = values()
    data["alerts"] = changes
    with pytest.raises(ValidationError):
        configured(data)


def test_the_examples_alerts_render_uncommented(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[3] / "examples/kubernetes-bundle.toml"
    text = example.read_text()
    for role in ("app", "worker"):  # the template's images are placeholders, as it pins none
        text = text.replace(
            f'# {role} = "YOUR_REGISTRY/trusted-{role}@sha256:YOUR_VERIFIED_DIGEST"',
            f'{role} = "ghcr.io/test/{role}@sha256:{hashlib.sha256(role.encode()).hexdigest()}"',
        )
    lines = text.splitlines()
    start = lines.index("# [alerts]")
    end = next(i for i in range(start, len(lines)) if not lines[i].startswith("# "))
    block = [line.removeprefix("# ") for line in lines[start:end]]
    path = tmp_path / "bundle.toml"
    path.write_text("\n".join([*lines[:start], *block, *lines[end:]]) + "\n")
    config = load(path)
    assert config.alerts is not None
    assert config.alerts.namespace == "monitoring"
    items = json.loads(json.dumps(render(config)))["items"]
    (config_map,) = [item for item in items if item["metadata"]["name"].endswith("-alert-rules")]
    assert config_map["metadata"]["namespace"] == "monitoring"
    assert tuple(_rules_of(config_map)) == ALERTS


def _rules_of(config_map: dict[str, Any]) -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(config_map["data"][ALERT_RULES_FILE])
    return {rule["alert"]: rule for rule in document["groups"][0]["rules"]}
