# SPDX-License-Identifier: MIT
"""The worker's command line sets up the shared logging before it serves."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_science.worker import cli


def test_serve_sets_up_the_shared_logging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up: list[tuple[str, object]] = []
    monkeypatch.setattr(
        cli, "configure_logging", lambda service, **kw: set_up.append((service, kw))
    )
    argv = ["serve", "--settings", str(tmp_path / "missing.toml"), "--log-level", "debug"]
    assert cli.main(argv) == 1  # the settings are refused, after the logging is up
    assert set_up == [("worker", {"level": "debug"})]
    assert "error:" in capsys.readouterr().err
