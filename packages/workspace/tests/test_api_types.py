# SPDX-License-Identifier: MIT
"""The browser's API types are generated from the response models and kept current."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "generate_api_types.py"


def test_generated_types_are_current() -> None:
    module = runpy.run_path(str(SCRIPT))
    generate = cast(Callable[[], str], module["generate"])
    out = cast(Path, module["OUT"])
    assert out.read_text(encoding="utf-8") == generate(), (
        "web/workspace/src/lib/api-types.ts is stale: run scripts/generate_api_types.py"
    )
