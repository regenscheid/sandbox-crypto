# SPDX-License-Identifier: MIT
"""The geometry of a run's view, in tokens (docs/specs/context-management.md, CM-27).

The ceiling C is the role's ceiling, capped by the model's input limit. Folds start at
the trigger, 0.75 C, and past the hard wall, 0.9 C, the run folds at once, skeleton-only.
A fold takes about a chunk K of the oldest raw messages, never leaving fewer than the raw
floor. On a small ceiling the scale λ shrinks the state block's and the fold region's
budgets and the floor together, so that a fold at the trigger still takes half a chunk
when both regions are full: the steady state. A run whose system message and tools pass
0.2 C, or whose λ would fall below 0.4, does not start.

The shares are settable (``Shares``), as the labs set small ones to force overviews and
the user's words into pointers; λ then counts the shares as set.
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
class Shares:
    """The view's shares of C, before λ scales them: the user's words', the digest's
    (together the state block) and the fold region's."""

    words: float = 0.10
    digest: float = 0.05
    region: float = 0.15

    def __post_init__(self) -> None:
        for name in ("words", "digest", "region"):
            if not 0.0 < getattr(self, name) <= 0.5:
                raise ValueError(f"the {name} share must be above 0 and at most 0.5")


DEFAULT_SHARES: Final = Shares()


@dataclass(frozen=True)
class Geometry:
    """A run's geometry, in tokens: ``ceiling`` C, ``prompt`` P (the system message and
    tools), the chunk K and the scale λ, and the shares it scales."""

    ceiling: int
    prompt: int
    chunk: int
    scale: float
    shares: Shares = DEFAULT_SHARES

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
        return self.words + self.digest

    @property
    def words(self) -> int:
        """The user's words' share of the state block: two thirds of it by default."""
        return int(self.shares.words * self.ceiling * self.scale)

    @property
    def digest(self) -> int:
        """The research-state digest's share of the state block: the third left."""
        return int(self.shares.digest * self.ceiling * self.scale)

    @property
    def region(self) -> int:
        """The fold region's budget."""
        return int(self.shares.region * self.ceiling * self.scale)

    @property
    def region_cap(self) -> int:
        """How far current decision lines may grow the fold region past its budget: while
        the system message and tools, the state block, the region, the raw floor and
        half a chunk stay under the trigger, so a useful fold still fits (CM-21b)."""
        room = self.trigger - self.prompt - self.state_block - self.floor - self.chunk // 2
        return max(self.region, room)

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

    def summary(self) -> dict[str, object]:
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
            "region_cap": self.region_cap,
            **(
                {}
                if self.shares == DEFAULT_SHARES
                else {"shares": [self.shares.words, self.shares.digest, self.shares.region]}
            ),
        }


def geometry(
    ceiling: int, prompt: int, chunk: int | None = None, shares: Shares = DEFAULT_SHARES
) -> Geometry:
    """The geometry at ``ceiling`` with ``prompt`` tokens of system message and tools;
    ``chunk`` is the role's chunk size, if it sets one, and ``shares`` the view's."""
    size = chunk if chunk is not None else max(MIN_CHUNK, int(0.2 * ceiling))
    scaled = shares.words + shares.digest + shares.region  # 0.3 by default
    scale = min(1.0, (TRIGGER * ceiling - prompt - size / 2) / (scaled * ceiling + 0.7 * size))
    return Geometry(ceiling, prompt, size, max(0.0, scale), shares)


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
