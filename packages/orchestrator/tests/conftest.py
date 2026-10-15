# SPDX-License-Identifier: MIT
"""Fixtures for every orchestrator test."""

from __future__ import annotations

import pytest

from sandbox_science.orchestrator.skills import builtin


@pytest.fixture(autouse=True)
def no_shipped_skills(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs get no built-in skills unless a test ships its own: the repository's would add
    a section and a tool to every run (docs/specs/skills.md, SK-40)."""
    monkeypatch.setattr(builtin, "ROOT", tmp_path_factory.getbasetemp() / "no-built-in-skills")
