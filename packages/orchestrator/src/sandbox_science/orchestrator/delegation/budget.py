# SPDX-License-Identifier: MIT
"""A run tree's share of the project's model budget (plans-and-delegation spec, PD-38;
run-service spec, RS-6a and RS-6c).

The run service gives each root run a share of the project's budget, half by default.
With delegation, the share bounds the whole tree, not each run:

- the root's own calls and its children's spend it;
- a child's tokens are reserved from what the root has left when it starts, and are at
  most that, less what the root keeps for its own calls: ``FLOOR``, or twice its last
  call when that cost more; a ``delegate`` that cannot reserve ``MIN_CHILD`` is
  refused;
- while a child runs, it counts as its reservation, or as what it used once that is
  more; once it ended, as what it used, so the rest returns to the root;
- every model call in flight counts too, as an estimate of what it sends and an
  allowance for its answer, so that the tree's runs calling at once cannot all pass a
  check that each passes alone. A run's check registers its call's estimate in the same
  step as it passes, with nothing else running between the two; a child's call counts
  only for what goes past its reservation, which holds it already.

The tree lives in the root's process (PD-32) and is kept in memory; the project's
budget itself is read from the control store.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

FLOOR: Final = 100_000
"""What a root keeps of its share for its own calls when it delegates."""
MIN_CHILD: Final = 50_000
"""The fewest tokens worth starting a child with: a few calls."""
ROOT: Final = ""
"""The root's key among the calls in flight."""


@dataclass
class _Share:
    reserved: int
    used: int = 0
    running: bool = True

    @property
    def counted(self) -> int:
        return max(self.reserved, self.used) if self.running else self.used


class TreeBudget:
    """What a root run's tree may spend, and what it spent; ``allowance`` reads the
    root's share of the project's budget."""

    def __init__(self, allowance: Callable[[], Awaitable[int]]) -> None:
        self.allowance = allowance
        self.root = 0
        """What the root's own calls used."""
        self.root_last = 0
        """What the root's last call cost, which sets what it keeps for itself."""
        self.children: dict[str, _Share] = {}
        self.flying: dict[str, int] = {}
        """Each run's call in flight, by its member (``ROOT`` for the root), as an estimate."""

    def spent(self) -> int:
        """What the tree has spent or holds: the root's calls and each child's share."""
        return self.root + sum(share.counted for share in self.children.values())

    def in_flight(self, *, besides: str | None = None, root: bool = False) -> int:
        """The estimates of the tree's calls in flight, but those of the child ``besides``
        or, with ``root``, the root's."""
        left_out = ROOT if root else besides
        return sum(tokens for member, tokens in self.flying.items() if member != left_out)

    def committed(self) -> int:
        """What the tree has spent, holds or has in flight: the root's calls and its call
        in flight, and each child's share, or its use with its call in flight once that
        is more."""
        found = self.root + self.flying.get(ROOT, 0)
        for member, share in self.children.items():
            flying = self.flying.get(member, 0)
            found += max(share.reserved, share.used + flying) if share.running else share.used
        return found

    async def reserve(self, member: str, wanted: int) -> int | None:
        """Reserve up to ``wanted`` tokens for a new child, keeping for the root
        ``FLOOR``, or twice its last call; ``None`` when fewer than ``MIN_CHILD`` are
        left. Nothing else runs between the reading and the reservation."""
        allowance = await self.allowance()
        left = allowance - self.committed() - max(FLOOR, 2 * self.root_last)
        tokens = min(wanted, left)
        if tokens < MIN_CHILD:
            return None
        self.children[member] = _Share(tokens)
        return tokens

    def used(self, member: str | None, tokens: int, last: int = 0) -> None:
        """What a run of the tree has used so far: the root (``None``) or a child; for
        the root, ``last`` is what its last call cost."""
        if member is None:
            self.root = tokens
            if last:
                self.root_last = last
        elif (share := self.children.get(member)) is not None:
            share.used = tokens

    def ended(self, member: str, tokens: int | None = None) -> None:
        """A child ended: it counts as what it used from now on."""
        share = self.children.get(member)
        if share is not None:
            share.running = False
            if tokens is not None:
                share.used = tokens
        self.flying.pop(member, None)

    def calling(self, member: str | None, estimate: int) -> None:
        self.flying[member or ROOT] = estimate

    def called(self, member: str | None) -> None:
        self.flying.pop(member or ROOT, None)
