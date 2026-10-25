# SPDX-License-Identifier: MIT
"""Tool calls as the tests script them: each names a purpose unless the test names its
own (docs/specs/tools.md, Purpose 1), so a test of something else need not write one.

``ToolCall`` and ``ScriptedModel`` stand in for the model module's: a call whose
arguments are a JSON object without ``purpose_note`` gets ``PURPOSE``. A test of the
purpose itself (``test_purpose.py``) uses the model module's own.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from sandbox_science.orchestrator import model

PURPOSE = "Testing the harness with a scripted call"


def with_purpose(arguments: str) -> str:
    """``arguments`` with ``PURPOSE``, when they are a JSON object that names none."""
    try:
        found: object = json.loads(arguments or "{}")
    except (ValueError, RecursionError):
        return arguments
    if not isinstance(found, dict) or "purpose_note" in found:
        return arguments
    return json.dumps({"purpose_note": PURPOSE, **cast(dict[str, Any], found)})


if TYPE_CHECKING:
    # To the type checker it is the model module's, which is what it makes.
    ToolCall = model.ToolCall
else:

    class ToolCall:
        """Makes a ``model.ToolCall`` with a purpose."""

        def __new__(cls, id: str, name: str, arguments: str) -> model.ToolCall:
            return model.ToolCall(id, name, with_purpose(arguments))


class ScriptedModel(model.ScriptedModel):
    """``model.ScriptedModel``, whose scripted calls name a purpose."""

    def __init__(self, turns: Sequence[model.ModelTurn]) -> None:
        super().__init__(
            [
                replace(
                    turn,
                    tool_calls=tuple(
                        model.ToolCall(c.id, c.name, with_purpose(c.arguments))
                        for c in turn.tool_calls
                    ),
                )
                for turn in turns
            ]
        )

    @classmethod
    def from_json(cls, data: object) -> ScriptedModel:
        # The model module's makes ``cls(turns)``: this class, whose calls name purposes.
        return cast(ScriptedModel, super().from_json(data))


def script(turns: Sequence[Any]) -> list[Any]:
    """A script for ``--script`` (``model.ScriptedModel.from_json``), its calls naming a
    purpose."""
    found: list[Any] = []
    for turn in turns:
        if isinstance(turn, dict) and cast(dict[str, Any], turn).get("tool_calls"):
            turn = cast(dict[str, Any], turn)
            calls = [
                {**c, "arguments": {"purpose_note": PURPOSE, **c.get("arguments", {})}}
                if "purpose_note" not in c.get("arguments", {})
                else c
                for c in cast(list[dict[str, Any]], turn["tool_calls"])
            ]
            turn = {**turn, "tool_calls": calls}
        found.append(turn)
    return found
