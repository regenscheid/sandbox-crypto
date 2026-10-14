# SPDX-License-Identifier: MIT
"""The workspace reaches the control store only through the orchestrator's public
repository methods: no transaction, SQL or lock of its own (B-17, E-12)."""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "sandbox_science" / "workspace"
FORBIDDEN = re.compile(
    r"\.control\b|\btx\.sql\b|\.execute\(|\.locked\(|sqlite_master|session_plan_|review_findings"
)


def test_the_workspace_uses_no_control_store_internals() -> None:
    found = [
        f"{path.name}:{number}: {line.strip()}"
        for path in sorted(SOURCE.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN.search(line)
    ]
    assert found == []
