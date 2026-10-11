# SPDX-License-Identifier: MIT
"""The command line's policy store commands."""

from pathlib import Path

import pytest

from sandbox_science.policy_middleware.cli import main
from sandbox_science.policy_middleware.store import PolicyStore

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY = REPO_ROOT / "examples" / "policy.yaml"


def test_bind_stores_the_policy_and_binds_the_sandbox(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert (
        main(["bind", "--state-dir", str(state), "--sandbox", "sbx-1", "--policy", str(POLICY)])
        == 0
    )
    digest = PolicyStore(state).binding("sbx-1")
    assert digest is not None
    assert digest in capsys.readouterr().out
    assert main(["unbind", "--state-dir", str(state), "--sandbox", "sbx-1"]) == 0
    assert PolicyStore(state).binding("sbx-1") is None
    assert main(["unbind", "--state-dir", str(state), "--sandbox", "sbx-1"]) == 0
    assert "was not bound" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["--sandbox", "../escape", "--policy", str(POLICY)], id="bad-sandbox"),
        pytest.param(["--sandbox", "sbx-1", "--policy", "/nonexistent.yaml"], id="no-file"),
    ],
)
def test_bind_reports_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    assert main(["bind", "--state-dir", str(tmp_path), *arguments]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_bind_rejects_an_invalid_policy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: ProjectPolicy\n")
    assert main(["bind", "--state-dir", str(tmp_path), "--sandbox", "s", "--policy", str(bad)]) == 1
    assert "invalid" in capsys.readouterr().err


def test_serve_reports_bad_settings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["serve", "--settings", str(tmp_path / "missing.toml")]) == 1
    assert "cannot read settings" in capsys.readouterr().err
