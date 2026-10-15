# SPDX-License-Identifier: MIT
"""The ledger views' fixture (statuses, link kinds, field names) is current with the
ledger's models; the views' tests compare their tables with it."""

from __future__ import annotations

import json
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

from sandbox_science.orchestrator.ledger.models import STATUSES

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "generate_ledger_fixture.py"


def test_the_fixture_is_current() -> None:
    module = runpy.run_path(str(SCRIPT))
    generate = cast(Callable[[], str], module["generate"])
    out = cast(Path, module["OUT"])
    assert out.read_text(encoding="utf-8") == generate(), (
        "web/workspace/src/lib/ledger.fixtures.json is stale: "
        "run scripts/generate_ledger_fixture.py"
    )


def test_the_fixture_holds_every_type_and_field() -> None:
    module = runpy.run_path(str(SCRIPT))
    data = json.loads(cast(Callable[[], str], module["generate"])())
    assert data["statuses"]["issue"][0] == "open"
    assert set(data["statuses"]) == set(STATUSES)
    assert {"kind", "measures", "identifiers", "pdf"} <= set(data["fields"])
    assert "concerns" in data["link_kinds"]
