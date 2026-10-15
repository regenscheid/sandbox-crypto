# SPDX-License-Identifier: MIT
"""Fixtures for every orchestrator test."""

from __future__ import annotations

import pytest

from sandbox_science.orchestrator.skills import builtin


@pytest.fixture(autouse=True)
def no_shipped_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs get no built-in skills unless a test ships its own: the repository's would add
    a section and a tool to every run (docs/specs/skills.md, SK-40). The shipped pack's
    own tests, and the installed wheel's, do not rely on this."""
    monkeypatch.setattr(builtin, "ROOT", None)
