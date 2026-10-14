# SPDX-License-Identifier: MIT
"""The command line: binding projects, making tokens, refusing bad settings."""

import re
from pathlib import Path

import pytest

from sandbox_science.mcp_proxy.callers import token_digest
from sandbox_science.mcp_proxy.cli import main
from sandbox_science.mcp_proxy.store import PolicyStore

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_POLICY = REPO_ROOT / "examples" / "policy.yaml"


def test_bind_stores_the_policy_and_binds_the_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert (
        main(
            [
                "bind",
                "--state-dir",
                str(state),
                "--project",
                "crypto",
                "--policy",
                str(EXAMPLE_POLICY),
            ]
        )
        == 0
    )
    digest = PolicyStore(state).binding("crypto")
    assert digest is not None
    assert f"bound crypto to permissive-default {digest}" in capsys.readouterr().out
    assert main(["unbind", "--state-dir", str(state), "--project", "crypto"]) == 0
    assert PolicyStore(state).binding("crypto") is None
    assert main(["unbind", "--state-dir", str(state), "--project", "crypto"]) == 0
    assert "was not bound" in capsys.readouterr().out


def test_bind_refuses_an_invalid_policy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = tmp_path / "bad.yaml"
    policy.write_text("kind: ProjectPolicy\n")
    assert (
        main(["bind", "--state-dir", str(tmp_path), "--project", "crypto", "--policy", str(policy)])
        == 1
    )
    assert "error:" in capsys.readouterr().err


def test_new_token_prints_a_token_and_its_hash(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new-token"]) == 0
    output = capsys.readouterr().out
    token = re.search(r"^token = (\S+)$", output, re.MULTILINE)
    digest = re.search(r"^token_sha256 = ([0-9a-f]{64})$", output, re.MULTILINE)
    assert token is not None
    assert digest is not None
    assert len(token[1]) >= 43
    assert token_digest(token[1]) == digest[1]


def test_serve_refuses_bad_settings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = tmp_path / "proxy.toml"
    settings.write_text('state_dir = "state"\nceiling = "ceiling.yaml"\n')
    assert main(["serve", "--settings", str(settings)]) == 1
    assert "callers" in capsys.readouterr().err
    settings.write_text(
        'state_dir = "state"\nceiling = "missing.yaml"\n\n[[callers]]\nname = "o"\n'
        f'token_sha256 = "{"a" * 64}"\nkind = "orchestrator"\nproject = "crypto"\n'
    )
    assert main(["serve", "--settings", str(settings)]) == 1
    assert "missing.yaml" in capsys.readouterr().err


def test_compile_policy_writes_the_proxy_sandboxs_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ceiling = tmp_path / "pool-ceiling.yaml"
    ceiling.write_text("apiVersion: sandbox-science/v0alpha1\nkind: PoolCeiling\n")
    targets = REPO_ROOT / "examples" / "openshell" / "targets.yaml"
    base = ["--policy", str(EXAMPLE_POLICY), "--ceiling", str(ceiling), "--targets", str(targets)]
    held, admitting = tmp_path / "held.yaml", tmp_path / "admitting.yaml"
    assert main(["compile-policy", *base, "--out", str(held)]) == 0
    printed = capsys.readouterr().out
    assert f"wrote {held}" in printed
    assert "always denied at this fence" in printed
    assert main(["compile-policy", *base, "--out", str(admitting), "--admit-escalated"]) == 0
    printed = capsys.readouterr().out
    assert "escalates by name are admitted at this fence" in printed
    # The example escalates firecrawl's crawl by name: only the second admits it. Its
    # escalated patterns would need a deny entry for portal_*, which the exact-allowed
    # portal_list_servers matches, so they stay held at the fence, and the compiler says so.
    assert "firecrawl-mcp_firecrawl_crawl" not in held.read_text()
    assert "firecrawl-mcp_firecrawl_crawl" in admitting.read_text()
    assert "would deny crypto_research_mcp-portal_list_servers" in printed
    assert "feedback" not in admitting.read_text()
    assert (
        held.read_text()
        == (REPO_ROOT / "examples" / "openshell" / "mcp-proxy.yaml").read_text().split("\n", 2)[2]
    )


def test_compile_policy_refuses_a_bad_ceiling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ceiling = tmp_path / "bad.yaml"
    ceiling.write_text("kind: Nothing\n")
    targets = REPO_ROOT / "examples" / "openshell" / "targets.yaml"
    argv = [
        "compile-policy", "--policy", str(EXAMPLE_POLICY), "--ceiling", str(ceiling),
        "--targets", str(targets), "--out", str(tmp_path / "out.yaml"),
    ]  # fmt: skip
    assert main(argv) == 1
    assert capsys.readouterr().err.startswith("error:")
    assert not (tmp_path / "out.yaml").exists()
