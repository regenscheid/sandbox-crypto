# SPDX-License-Identifier: MIT
from __future__ import annotations

import hashlib
from pathlib import Path
from textwrap import dedent

import pytest

from sandbox_science.policy import (
    PolicyLoadError,
    load_ceiling,
    load_policy,
    parse_ceiling,
    parse_policy,
)
from sandbox_science.policy.loader import MAX_FILE_BYTES

MINIMAL = dedent(
    """\
    apiVersion: sandbox-science/v0alpha1
    kind: ProjectPolicy
    metadata: {name: test}
    """
)


def test_minimal_policy_and_digest() -> None:
    loaded = parse_policy(MINIMAL, source="inline")
    assert loaded.policy.metadata.name == "test"
    assert loaded.source == "inline"
    assert loaded.digest == "sha256:" + hashlib.sha256(MINIMAL.encode()).hexdigest()
    assert parse_policy(MINIMAL.encode()).digest == loaded.digest
    assert parse_policy(MINIMAL + "# comment\n").digest != loaded.digest


def test_load_policy_from_file(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(MINIMAL)
    loaded = load_policy(path)
    assert loaded.source == str(path)
    assert loaded.digest == parse_policy(MINIMAL).digest


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PolicyLoadError, match="cannot read"):
        load_policy(tmp_path / "absent.yaml")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (MINIMAL + "kind: ProjectPolicy\n", "duplicate key 'kind'"),
        (
            MINIMAL + "defaults: {action: allow, action: deny}\n",
            "duplicate key 'action'",
        ),
        (MINIMAL + "x: &a [1]\ny: *a\n", "aliases are not allowed"),
        (MINIMAL + "defaults:\n  <<: {action: allow}\n", "merge keys are not allowed"),
        (MINIMAL + "---\n" + MINIMAL, "not valid YAML"),
        ("- just\n- a list\n", "mapping at the top level"),
        ("", "mapping at the top level"),
        (MINIMAL + "defaults: {action: [unclosed\n", "not valid YAML"),
        (MINIMAL + "network: !!python/object:os.system {}\n", "not valid YAML"),
        (MINIMAL + "? [a, b]\n: c\n", "mapping keys must be scalars"),
    ],
)
def test_rejected_yaml(text: str, message: str) -> None:
    with pytest.raises(PolicyLoadError, match=message):
        parse_policy(text)


def test_rejects_invalid_utf8() -> None:
    with pytest.raises(PolicyLoadError, match="UTF-8"):
        parse_policy(MINIMAL.encode() + b"# \xff\n")


def test_rejects_oversized_file() -> None:
    padding = "#" * (MAX_FILE_BYTES + 1)
    with pytest.raises(PolicyLoadError, match="larger than"):
        parse_policy(MINIMAL + padding)


def test_rejects_deep_nesting() -> None:
    depth = 100_000
    with pytest.raises(PolicyLoadError, match=r"nested too deeply|not valid YAML"):
        parse_policy(MINIMAL + "x: " + "[" * depth + "]" * depth + "\n")


def test_validation_errors_name_the_location() -> None:
    text = MINIMAL + "defaults: {action: maybe}\n"
    with pytest.raises(PolicyLoadError, match=r"defaults\.action"):
        parse_policy(text, source="p.yaml")


def test_ceiling(tmp_path: Path) -> None:
    text = dedent(
        """\
        apiVersion: sandbox-science/v0alpha1
        kind: PoolCeiling
        resources: {memory: 32Gi}
        network: {deny_hosts: ["*.example.com"]}
        mcp_servers: [crypto-research-mcp]
        """
    )
    ceiling = parse_ceiling(text)
    assert ceiling.resources.memory == 32 * 1024**3
    assert ceiling.mcp_servers == ("crypto-research-mcp",)
    path = tmp_path / "ceiling.yaml"
    path.write_text(text)
    assert load_ceiling(path) == ceiling


@pytest.mark.parametrize(
    "text",
    [
        "kind: ProjectPolicy\n",
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\nextra: 1\n",
        "apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n"
        "network: {allow_hosts: [bad host]}\n",
    ],
)
def test_ceiling_rejects(text: str) -> None:
    with pytest.raises(PolicyLoadError):
        parse_ceiling(text)
