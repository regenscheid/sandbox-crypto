# SPDX-License-Identifier: MIT
"""The browser's API types are generated from the response models and kept current."""

from __future__ import annotations

import json
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "generate_api_types.py"


def _generate(models: tuple[type[BaseModel], ...]) -> tuple[str, dict[str, Any]]:
    module = runpy.run_path(str(SCRIPT))
    generate = cast(Callable[[], str], module["generate"])
    generate.__globals__["MODELS"] = models
    return generate(), module


def test_generated_types_are_current() -> None:
    module = runpy.run_path(str(SCRIPT))
    generate = cast(Callable[[], str], module["generate"])
    out = cast(Path, module["OUT"])
    assert out.read_text(encoding="utf-8") == generate(), (
        "web/workspace/src/lib/api-types.ts is stale: run scripts/generate_api_types.py"
    )


def test_field_names_not_aliases_as_the_routes_dump() -> None:
    class Aliased(BaseModel):
        field: int = Field(serialization_alias="renamed")

    text, _ = _generate((Aliased,))
    assert set(json.loads(Aliased(field=1).model_dump_json())) == {"field"}
    assert "  field: number;" in text
    assert "renamed" not in text


def test_a_docstring_cannot_end_its_comment() -> None:
    class Commented(BaseModel):
        """Matches a/*/b and */ then more."""

        x: int

    text, _ = _generate((Commented,))
    comment = text.split("/**", 1)[1]
    assert comment.split("*/", 1)[1].lstrip().startswith("export interface Commented")


def test_names_that_are_not_identifiers_are_quoted() -> None:
    module = runpy.run_path(str(SCRIPT))
    render = cast(Callable[[dict[str, Any], str], str], module["_object"])
    text = render({"properties": {"content-type": {"type": "string"}, "ok": {}}}, "")
    assert '"content-type": string;' in text
    assert "  ok: unknown;" in text
