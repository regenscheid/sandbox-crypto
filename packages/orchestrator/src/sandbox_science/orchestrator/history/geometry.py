# SPDX-License-Identifier: MIT
"""The geometry of a run's view, in tokens (docs/specs/context-management.md, CM-27).

The ceiling C is the role's ceiling, capped by the model's input limit. Folds start at
the trigger, 0.75 C, and past the hard wall, 0.9 C, the run folds at once, skeleton-only.
A fold takes about a chunk K of the oldest raw messages, never leaving fewer than the raw
floor. On a small ceiling the scale λ shrinks the state block's and the fold region's
budgets and the floor together, so that a fold at the trigger still takes half a chunk
when both regions are full: the steady state. A run whose system message and tools pass
0.2 C, or whose λ would fall below 0.4, does not start.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

CEILING: Final = 600_000
"""A role's ceiling, by default."""
TRIGGER: Final = 0.75
WALL: Final = 0.90
MIN_CHUNK: Final = 50_000
MIN_SCALE: Final = 0.4
MAX_PROMPT: Final = 0.2
"""The largest share of C the system message and tools may take."""


@dataclass(frozen=True)
class Geometry:
    """A run's geometry, in tokens: ``ceiling`` C, ``prompt`` P (the system message and
    tools), the chunk K and the scale λ."""

    ceiling: int
    prompt: int
    chunk: int
    scale: float

    @property
    def trigger(self) -> int:
        return int(TRIGGER * self.ceiling)

    @property
    def wall(self) -> int:
        return int(WALL * self.ceiling)

    @property
    def floor(self) -> int:
        """The raw tail's least size, which a fold never takes it below."""
        return int(0.7 * self.chunk * self.scale)

    @property
    def state_block(self) -> int:
        return int(0.15 * self.ceiling * self.scale)

    @property
    def words(self) -> int:
        """The user's words' share of the state block: two thirds of it."""
        return self.state_block * 2 // 3

    @property
    def digest(self) -> int:
        """The research-state digest's share of the state block: the third left."""
        return self.state_block - self.words

    @property
    def region(self) -> int:
        """The fold region's budget."""
        return int(0.15 * self.ceiling * self.scale)

    @property
    def record(self) -> int:
        """A fold record's largest size, without its marker lines."""
        return int(0.1 * self.chunk)

    def refused(self) -> str | None:
        """Why a run with this geometry does not start, or ``None`` (CM-27)."""
        if self.scale < MIN_SCALE:
            return (
                f"the steady state does not fit: with {self.prompt:,} tokens of system "
                f"message and tools under a ceiling of {self.ceiling:,}, the scale would "
                f"be {self.scale:.2f}, below {MIN_SCALE}, and folds could not keep up"
            )
        if self.prompt > MAX_PROMPT * self.ceiling:
            return (
                f"the system message and tools take {self.prompt:,} tokens, more than "
                f"{MAX_PROMPT:.0%} of the ceiling of {self.ceiling:,}"
            )
        return None

    def summary(self) -> dict[str, float | int]:
        """The values a run used, as its record keeps them (CM-47)."""
        return {
            "ceiling": self.ceiling,
            "prompt": self.prompt,
            "trigger": self.trigger,
            "wall": self.wall,
            "chunk": self.chunk,
            "scale": round(self.scale, 3),
            "floor": self.floor,
            "state_block": self.state_block,
            "region": self.region,
        }


def geometry(ceiling: int, prompt: int, chunk: int | None = None) -> Geometry:
    """The geometry at ``ceiling`` with ``prompt`` tokens of system message and tools;
    ``chunk`` is the role's chunk size, if it sets one."""
    size = chunk if chunk is not None else max(MIN_CHUNK, int(0.2 * ceiling))
    scale = min(1.0, (TRIGGER * ceiling - prompt - size / 2) / (0.3 * ceiling + 0.7 * size))
    return Geometry(ceiling, prompt, size, max(0.0, scale))


def ceiling(
    role: int = CEILING,
    *,
    window: int | None = None,
    input_limit: int | None = None,
    max_output: int | None = None,
) -> int:
    """C: the role's ceiling, capped by the model's input limit. ``input_limit`` already
    excludes the output, so nothing is subtracted from it; ``window`` is shared by input
    and output, so the call's ``max_output`` comes off it. Neither known: the role's."""
    if input_limit is not None:
        return min(role, input_limit)
    if window is not None:
        return min(role, window - (max_output or 0))
    return role


def input_limit(info: Mapping[str, object]) -> int | None:
    """The input limit the gateway's model information gives: LiteLLM's
    ``max_input_tokens``, which excludes the output. Its ``max_tokens`` is not read: some
    providers' is the output's limit, not a window."""
    value = info.get("max_input_tokens")
    return value if type(value) is int and value > 0 else None
