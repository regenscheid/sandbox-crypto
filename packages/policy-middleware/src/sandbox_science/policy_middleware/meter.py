# SPDX-License-Identifier: MIT
"""Per-sandbox egress counts for the policy monitors ("Monitors" in docs/POLICY.md)."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Final

_WINDOW_MINUTES: Final = 60
_MAX_SANDBOXES: Final = 10_000


class EgressMeter:
    """Bytes each sandbox sent in the last hour, kept in one-minute buckets.

    Counts are approximate at bucket edges: a request is attributed to the
    whole minute it falls in.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, deque[tuple[int, int]]] = {}
        self._lock = threading.Lock()

    def add(self, sandbox_id: str, size: int) -> int:
        """Count ``size`` bytes for a sandbox; return its total for the last hour."""
        minute = int(self._clock() // 60)
        with self._lock:
            if sandbox_id not in self._buckets and len(self._buckets) >= _MAX_SANDBOXES:
                self._prune(minute)
            buckets = self._buckets.setdefault(sandbox_id, deque())
            while buckets and buckets[0][0] <= minute - _WINDOW_MINUTES:
                buckets.popleft()
            if buckets and buckets[-1][0] == minute:
                buckets[-1] = (minute, buckets[-1][1] + size)
            else:
                buckets.append((minute, size))
            return sum(count for _, count in buckets)

    def _prune(self, minute: int) -> None:
        stale = [
            sandbox
            for sandbox, buckets in self._buckets.items()
            if not buckets or buckets[-1][0] <= minute - _WINDOW_MINUTES
        ]
        for sandbox in stale:
            del self._buckets[sandbox]
