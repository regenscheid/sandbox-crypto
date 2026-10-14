# SPDX-License-Identifier: MIT
"""The execution manager's grace periods, deadlines and polling intervals.

The defaults are the production values. Tests pass shorter ones instead of
patching module constants. ``seconds`` checks such a parameter wherever the worker
takes one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final


def seconds(name: str, value: float) -> float:
    """``value`` if it is a positive, finite number of seconds; ValueError otherwise.

    A zero or negative interval would make a polling loop spin, and a NaN never ends one.
    """
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds")
    return value


@dataclass(frozen=True)
class ManagerTimeouts:
    """Seconds the execution manager waits; see each field."""

    watchdog_grace: float = 30.0
    """Past an execution's own limit before the worker cancels it."""
    lost_grace: float = 60.0
    """After that cancel, before an execution that never reported a result is lost."""
    cancel_grace: float = 30.0
    """For an executor to confirm a cancel before its execution is lost."""
    retain_live: float = 600.0
    """An ended execution stays in memory this long; later reads come from the store."""
    kernel_observation: float = 2.0
    """The whole budget of a passive kernel observation, the transport write lock included."""
    lockfile: float = 120.0
    """For the executor to send an environment's lockfile."""
    shared: float = 120.0
    """For syncing the project's shared files before or after an execution (D79)."""
    poll_interval: float = 2.0
    """Between replays of a daemon's running executions, which catch an event a push
    left for a replay; the daemon pushes the others as they come."""
    min_poll_interval: float = 0.1
    """The least time between those replays, when one is wanted at once; and the time
    between them for a daemon that does not push events, one older than this worker."""
    poll_retry_max: float = 10.0
    """The longest wait between replays while they keep failing."""
    recovery_retry: float = 0.2
    """The first wait before retrying a daemon recovery; it doubles each time."""
    recovery_retry_max: float = 30.0
    """The longest wait between daemon recovery retries."""

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            seconds(name, value)


MANAGER_TIMEOUTS: Final = ManagerTimeouts()
"""The production values."""
