# SPDX-License-Identifier: MIT
"""The browser's API types are generated from the response models and kept current."""

from __future__ import annotations

import json
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, Field, create_model

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


def test_two_models_of_one_name_are_refused() -> None:
    documents = create_model("Documents", revision=(create_model("Revision", x=(int, ...)), ...))
    ledger = create_model("Ledger", revision=(create_model("Revision", x=(str, ...)), ...))
    with pytest.raises(SystemExit, match="two response models share a name"):
        _generate((documents, ledger))


def test_response_defaults_stay_required() -> None:
    class Defaults(BaseModel):
        absent: str | None = None
        count: int = 0

    text, _ = _generate((Defaults,))
    assert "  absent: string | null;" in text
    assert "  count: number;" in text


def test_request_defaults_aliases_and_wire_omissions() -> None:
    module = runpy.run_path(str(SCRIPT))
    generate = cast(Callable[[], str], module["generate"])
    text = generate()
    # Requests use validation aliases and required lists; responses retain real omissions.
    insert = text.split("export interface DocumentRequestInsert {", 1)[1].split("}", 1)[0]
    assert 'op: "insert";' in insert
    assert "before?: string | null;" in insert
    replace = text.split("export interface DocumentRequestReplaceText {", 1)[1].split("}", 1)[0]
    assert "with: string;" in replace
    assert "with_" not in replace
    anchor = text.split("export interface CommentRequestAnchorInput {", 1)[1].split("}", 1)[0]
    assert "revision: number;" in anchor
    assert "math?: string | null;" in anchor
    change = text.split("export interface CommentChange {", 1)[1].split("}", 1)[0]
    assert "request_id: string | null;" in change
    assert "replayed?: boolean;" in change
    revision = text.split("export interface DocumentRevision {", 1)[1].split("}", 1)[0]
    assert "macro_context: DocumentMacroContext;" in revision
    assert "pins: Record<string, JsonValue>[];" in revision


def test_conflicting_wire_and_model_names_are_refused() -> None:
    from pydantic import TypeAdapter
    from typing_extensions import TypedDict

    class Wire(TypedDict):
        other: str

    model = create_model("Wire", value=(int, ...))
    module = runpy.run_path(str(SCRIPT))
    generate = cast(Callable[[], str], module["generate"])
    generate.__globals__["MODELS"] = (model,)
    generate.__globals__["WIRE_TYPES"] = {"Wire": TypeAdapter(Wire)}
    generate.__globals__["REQUESTS"] = {}
    with pytest.raises(SystemExit, match="different shapes: Wire"):
        generate()
