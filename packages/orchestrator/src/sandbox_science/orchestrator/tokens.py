# SPDX-License-Identifier: MIT
"""Counting a view's tokens (docs/specs/context-management.md, CM-28).

A call's total input is every token its input occupies, cached or not, normalized from
each API's usage: Claude's Messages API reports the cache's tokens apart from
``input_tokens``, OpenAI's Responses API counts them inside it, and the chat-completions
client takes ``prompt_tokens`` as the gateway reports it. Between rebuilds a view's size
is the last call's total input and output, plus what was added since, at the characters
per token of recent calls on that model (4 until there are any). After a rebuild, a
run's start among them, it is a fresh estimate of the whole view, until a call reports.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from sandbox_science.orchestrator.projects import ModelUsage

Form = Literal["chat_completions", "anthropic_messages", "openai_responses"]
DEFAULT_CHARS: Final = 4.0
"""Characters per token before any call on the model has reported its tokens."""
CALIBRATED: Final = 20
"""The recent calls whose reports calibrate the characters per token."""
_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "chat_completions": ("prompt_tokens",),
    "anthropic_messages": (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ),
    "openai_responses": ("input_tokens",),
}


def total_input(usage: Mapping[str, object], form: Form) -> int | None:
    """A call's total input tokens from its API's usage object; ``None`` when a count
    the form needs is missing. A cache count Claude leaves out is none."""
    counts = [usage.get(key, 0 if key.startswith("cache_") else None) for key in _KEYS[form]]
    found = [count for count in counts if type(count) is int and count >= 0]
    return sum(found) if len(found) == len(counts) else None


def chars_per_token(calls: Iterable[ModelUsage]) -> float:
    """The characters per token of the last ``CALIBRATED`` calls that reported both."""
    known = [
        call
        for call in calls
        if call.characters and call.input_tokens is not None and call.output_tokens is not None
    ][-CALIBRATED:]
    tokens = sum((call.input_tokens or 0) + (call.output_tokens or 0) for call in known)
    chars = sum(call.characters or 0 for call in known)
    return chars / tokens if tokens and chars else DEFAULT_CHARS


def estimate(chars: int, per_token: float) -> int:
    """A text's tokens from its characters."""
    return math.ceil(chars / per_token) if per_token > 0 else chars


def view_tokens(size: int, calls: Sequence[ModelUsage], per_token: float) -> int:
    """A view's tokens before its next call, from ``size``, its characters now, and
    ``calls``, those made since its last rebuild: a fresh estimate until a call reports;
    then that call's input and output tokens and what was added since."""
    last = calls[-1] if calls else None
    if last is None or None in (last.input_tokens, last.output_tokens, last.characters):
        return estimate(size, per_token)
    added = max(0, size - (last.characters or 0))
    return (last.input_tokens or 0) + (last.output_tokens or 0) + estimate(added, per_token)
