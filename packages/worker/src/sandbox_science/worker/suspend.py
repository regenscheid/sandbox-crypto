# SPDX-License-Identifier: MIT
"""Idle suspension (W3): stop the sandboxes of environments nobody is using.

A project's environments persist, but need not keep running (docs/PROJECTS-AND-
SESSIONS.md, "Provisioning and persistence"). An environment is idle when the worker
has run nothing in it, and been asked nothing of it, for ``suspend_after`` seconds,
and its kernels have used almost no CPU meanwhile, so that a computation left running
in a kernel thread keeps it up. The suspender then stops its executor and its sandbox
(``Environments.suspend``). OpenShell keeps a stopped sandbox's writable disk, so
files and installed tools survive; kernels do not, and the next cell reports that its
earlier state is gone. The next use starts the sandbox again.

The executor reports the CPU time, and research code may take the executor over. It
can keep its own environment running, or let it be suspended, and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Final

from .environments import Environments
from .errors import WorkerError
from .manager import ExecutionManager

_log = logging.getLogger(__name__)

BUSY_SHARE: Final = 0.02
"""CPU seconds per second, summed over an environment's kernels, that count as use."""


class Suspender:
    """Stops idle environments' sandboxes, checking every ``interval`` seconds."""

    def __init__(
        self,
        environments: Environments,
        manager: ExecutionManager,
        after: float,
        *,
        interval: float | None = None,
    ) -> None:
        self.environments = environments
        self.manager = manager
        self.after = after
        self.interval = interval if interval is not None else max(5.0, min(60.0, after / 4))
        self.started = time.monotonic()
        self._samples: dict[str, tuple[float, float]] = {}
        """Environment to the last (time, CPU seconds) its executor reported."""
        self._busy: dict[str, float] = {}
        """Environment to when its kernels were last seen using CPU."""

    async def run(self) -> None:
        """Check every ready environment until cancelled."""
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.check()
            except Exception:
                # Never let one bad round end suspension for good.
                _log.exception("idle suspension check failed")

    async def check(self) -> list[str]:
        """Suspend the environments idle for ``after`` seconds; return their IDs."""
        suspended: list[str] = []
        for info in self.environments.records.ready():
            environment_id = info.environment_id
            try:
                if not await self._idle(environment_id) or not await self.environments.suspend(
                    environment_id, time.monotonic() - self.after
                ):
                    continue
            except WorkerError as error:
                # Its executor is gone, but it stays ready; the next use starts another.
                _log.warning("could not suspend environment %s: %s", environment_id, error.message)
                continue
            except Exception:
                # Logged in full; the other environments still get their turn.
                _log.exception("could not suspend environment %s", environment_id)
                continue
            suspended.append(environment_id)
            self._samples.pop(environment_id, None)
            self._busy.pop(environment_id, None)
        return suspended

    async def _idle(self, environment_id: str) -> bool:
        now = time.monotonic()
        activity = await self.manager.activity(environment_id)
        if activity is None:
            return False  # its executor could not say what it is doing
        if activity.running:
            self._busy[environment_id] = now
            return False
        if activity.cpu_seconds is None:
            self._samples.pop(environment_id, None)
        else:
            previous = self._samples.get(environment_id)
            self._samples[environment_id] = (now, activity.cpu_seconds)
            if previous is None:
                return False  # the next sample tells
            elapsed = max(now - previous[0], 1e-6)
            if activity.cpu_seconds - previous[1] > BUSY_SHARE * elapsed:
                self._busy[environment_id] = now
                return False
        last = max(self.started, activity.last_used or 0.0, self._busy.get(environment_id, 0.0))
        return now - last >= self.after
