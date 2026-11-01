# SPDX-License-Identifier: MIT
"""The purpose of each tool call (docs/specs/tools.md, "Purpose of each call"; B9).

Every tool a model is offered has a required ``purpose_note``: a short clause in the
agent's words saying what the call is for. The harness adds it to each tool's schema
where the tools are assembled for a model (``offered``), checks it before anything else
reads the call (``split``), and takes it out of the arguments, so that no gate, policy
decision, plan fence, approval, handler, worker request or connector call sees it, and
an operation's identity does not depend on it. It is agent text, data wherever it is
shown (CM-49): the archive keeps it with the call as the model wrote it, and a result's
header and a fold's skeleton name it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, cast

from .documents.agent_text import check_agent_text
from .documents.errors import DocumentError
from .model import ToolCall, ToolSpec

FIELD: Final = "purpose_note"
MIN_CHARS: Final = 8
MAX_CHARS: Final = 120
FORBIDDEN: Final = '[]"'
"""Characters a purpose may not hold: it is quoted in a result's header (CM-4a)."""
SCHEMA: Final = {"type": "string", "minLength": MIN_CHARS, "maxLength": MAX_CHARS}
"""The field as each tool's schema carries it, about 20 tokens a tool (Purpose 5): what
a purpose is, and the characters it may not hold, the prompts say once (``GUIDANCE``)
rather than every schema."""
COLLISION: Final = (
    f"its schema defines a {FIELD} parameter of its own, which the platform adds to every tool"
)
"""Why a connector tool whose schema defines the field is not loaded (Purpose 1)."""


def offered(spec: ToolSpec) -> ToolSpec:
    """``spec`` as a model is offered it: with ``purpose_note`` first among its
    parameters, and required."""
    parameters = dict(spec.parameters)
    found = parameters.get("properties")
    properties = cast(dict[str, Any], found) if isinstance(found, dict) else {}
    parameters["properties"] = {FIELD: dict(SCHEMA), **properties}
    found = parameters.get("required")
    required = (
        [r for r in cast(list[object], found) if r != FIELD] if isinstance(found, list) else []
    )
    parameters["required"] = [FIELD, *required]
    parameters.setdefault("type", "object")
    return replace(spec, parameters=parameters)


def offered_all(specs: Sequence[ToolSpec]) -> tuple[ToolSpec, ...]:
    return tuple(offered(spec) for spec in specs)


def collides(spec: ToolSpec) -> bool:
    """Whether a tool's own schema defines a top-level ``purpose_note``."""
    properties = spec.parameters.get("properties")
    return isinstance(properties, dict) and FIELD in properties


class PurposeError(ValueError):
    """A call refused for its purpose: ``invalid_request``, with ``message``."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Split:
    """A call as the harness passes it on: without its purpose, and the purpose."""

    call: ToolCall
    purpose: str


def check(value: object) -> str:
    """``value`` as a purpose, or ``PurposeError`` naming the field and the limit."""
    if value is None:
        raise PurposeError(
            f"{FIELD}: required, a clause of {MIN_CHARS} to {MAX_CHARS} characters on one "
            "line saying what this call is for"
        )
    if not isinstance(value, str):
        raise PurposeError(f"{FIELD}: must be a string of {MIN_CHARS} to {MAX_CHARS} characters")
    if len(value.splitlines()) > 1 or (value and value.splitlines()[0] != value):
        raise PurposeError(f"{FIELD}: one line, without line breaks")
    if len(value) > MAX_CHARS:
        raise PurposeError(f"{FIELD}: at most {MAX_CHARS} characters (got {len(value)})")
    if len(value) < MIN_CHARS:
        raise PurposeError(f"{FIELD}: at least {MIN_CHARS} characters (got {len(value)})")
    for index, char in enumerate(value):
        if char in FORBIDDEN:
            raise PurposeError(f'{FIELD}: no [, ] or " (got {char} at character {index + 1})')
    try:
        check_agent_text(value, f"{FIELD}:")
    except DocumentError as error:
        raise PurposeError(error.message) from None
    return value


def _object(arguments: str) -> dict[str, Any] | None:
    try:
        found: object = json.loads(arguments or "{}")
    except (ValueError, RecursionError):
        return None
    return cast(dict[str, Any], found) if isinstance(found, dict) else None


def split(call: ToolCall) -> Split:
    """``call`` without its purpose, and the purpose; ``PurposeError`` for a call
    without one, or with one outside its limits. Arguments that are not a JSON object
    are refused here too, since they hold no purpose."""
    try:
        found: object = json.loads(call.arguments or "{}")
    except (ValueError, RecursionError) as exc:
        raise PurposeError(f"arguments are not JSON: {exc}") from None
    if not isinstance(found, dict):
        raise PurposeError("arguments must be a JSON object")
    arguments = cast(dict[str, Any], found)
    purpose = check(arguments.pop(FIELD, None))
    return Split(replace(call, arguments=json.dumps(arguments, ensure_ascii=False)), purpose)


def without(arguments: str) -> str:
    """``arguments`` without a purpose: as written when they hold none, or are not a
    JSON object, so a call from before purposes keeps its identity."""
    found = _object(arguments)
    if found is None or FIELD not in found:
        return arguments
    del found[FIELD]
    return json.dumps(found, ensure_ascii=False)


def stripped(call: ToolCall) -> ToolCall:
    """``call`` without its purpose (``without``)."""
    arguments = without(call.arguments)
    return call if arguments is call.arguments else replace(call, arguments=arguments)


def purpose_of(arguments: str) -> str | None:
    """The call's purpose when it holds a valid one, else ``None``."""
    found = _object(arguments)
    if found is None:
        return None
    try:
        return check(found.get(FIELD))
    except PurposeError:
        return None


GUIDANCE: Final = (
    "Each tool call names its purpose in purpose_note, one line of 8 to 120 characters "
    'without [, ] or ": what this call is for in the work at hand, specific enough that a '
    "reader following the conversation knows why it was made, such as "
    '"Reading section 3 of the SNOVA specification for the field size", not '
    '"Reading a file". The purpose is for that reader, not a request: it changes nothing '
    "about what the call may do."
)
"""What each prompt says of the purpose (Purpose 6): the MAIN, task and check prompts
hold it as their own sentences, and the reviewer's bundle as a section of its own."""
