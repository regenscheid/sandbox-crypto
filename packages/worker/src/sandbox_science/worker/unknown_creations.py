# SPDX-License-Identifier: MIT
"""Creations whose outcome is unknown, reconciled on confirmed absence (backlog B3).

When ``openshell sandbox create`` fails after the gateway may have received it, and no
sandbox identity has appeared, the worker cannot tell whether a sandbox exists. On
Kubernetes this is common: OpenShell's driver rolls a sandbox back itself when its
bootstrap misses the 5-minute grace, so the sandbox never appears. Absence is
*confirmed*, and the environment may be marked deleted (``creation_rolled_back``), only
when all of these hold:

- the record is older than the pool's ``creation_grace``;
- the gateway has no sandbox by the environment's original name, and lists none by that
  name or whose labels or metadata name the environment or that name;
- that absence was observed on at least two reads at least ``creation_recheck`` apart,
  each past the grace and each recorded on the environment.

Any read that shows the sandbox gives the environment its identity instead, and the
ordinary deletion by identity follows; nothing here deletes a sandbox. The running
worker checks every such environment each ``creation_recheck`` (``CreationReconciler``);
``sandbox-science-worker environments reconcile`` does the same with the worker stopped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, cast

from sandbox_science.contracts import SYSTEM_CLOCK, Clock, EnvironmentInfo, ErrorKind

from .errors import WorkerError
from .timeouts import seconds

if TYPE_CHECKING:
    from .environments import Environments

_log = logging.getLogger(__name__)

CREATION_ROLLED_BACK: Final = "creation_rolled_back"
"""The reason recorded for an environment whose sandbox was confirmed never to exist."""
UNKNOWN_OUTCOME: Final = (
    "sandbox creation outcome is unknown; its original name is retained until the gateway"
    " exposes an identity or an operator reconciles it"
)
MAX_LISTED_NAMES: Final = 32
"""Sandbox names an observation keeps of what the gateway listed; the count is kept too."""
MAX_OBSERVATIONS: Final = 16
"""Observations kept per environment and sandbox name; the newest are kept."""
_LABEL_KEYS: Final = ("labels", "metadata", "annotations")


@dataclass(frozen=True, slots=True)
class Observation:
    """One gateway read, past the grace, that found no trace of the sandbox."""

    observed_at: datetime
    listed: int
    """How many sandboxes the gateway listed."""
    names: tuple[str, ...]
    """The first ``MAX_LISTED_NAMES`` of their names, sorted."""


@dataclass(frozen=True, slots=True)
class Listing:
    """What a sandbox listing says about one environment's original sandbox."""

    sandbox_id: str | None = None
    """The identity of a sandbox listed by the original name."""
    blocker: str | None = None
    """Why the listing cannot confirm absence, when it cannot."""
    names: tuple[str, ...] = ()
    """Every listed sandbox's name."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """The verdict of one reconciliation read."""

    sandbox_id: str | None = None
    """The sandbox appeared: its identity."""
    confirmed: bool = False
    """Absence is confirmed: the environment may be marked deleted."""
    message: str = ""
    retry_at: datetime | None = None
    """When only a second read is missing: the earliest time it can confirm absence."""


Action = Literal["rolled_back", "adopted", "inconclusive", "skipped"]


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What a reconciliation did to one environment, for the operator and the logs."""

    environment_id: str
    action: Action
    message: str
    retry_at: datetime | None = None
    """For an inconclusive read that only a later one can settle: when it can."""


def examine(document: object, info: EnvironmentInfo) -> Listing:
    """Read ``openshell sandbox list -o json`` for traces of ``info``'s sandbox.

    OpenShell 0.1.5 answers ``{"sandboxes": [...], "next_page_token": ...}``. A paged
    or unrecognised answer cannot show absence, so it is a blocker, never an absence.
    """
    entries: object
    token: object
    if isinstance(document, list):
        entries, token = cast(list[object], document), None
    elif isinstance(document, dict):
        mapping = cast(dict[str, object], document)
        entries, token = mapping.get("sandboxes"), mapping.get("next_page_token")
    else:
        entries, token = None, None
    if not isinstance(entries, list):
        return Listing(blocker="the gateway's sandbox listing was not understood")
    if token:
        return Listing(blocker="the gateway's sandbox listing has more pages than one read shows")
    names: list[str] = []
    wanted = {info.sandbox, info.environment_id}
    for item in cast(list[object], entries):
        if not isinstance(item, dict):
            return Listing(blocker="the gateway's sandbox listing was not understood")
        entry = cast(dict[str, object], item)
        name = entry.get("name")
        names.append(str(name))
        if name == info.sandbox:
            identity = entry.get("id")
            if isinstance(identity, str) and identity:
                return Listing(sandbox_id=identity, names=tuple(names))
            return Listing(
                blocker=f"the gateway lists sandbox {info.sandbox} without an identity",
                names=tuple(names),
            )
        if any(wanted & set(_strings(entry.get(key))) for key in _LABEL_KEYS):
            return Listing(
                blocker=f"sandbox {str(name)[:64]!r} carries labels or metadata naming this"
                " environment; inspect it with `openshell sandbox get`",
                names=tuple(names),
            )
    return Listing(names=tuple(names))


def _strings(value: object, depth: int = 0) -> Iterable[str]:
    """The strings in a label or metadata value, nested maps and lists included."""
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in cast(dict[object, object], value).values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, list):
        for item in cast(list[object], value):
            yield from _strings(item, depth + 1)


def observation(at: datetime, names: Iterable[str]) -> Observation:
    listed = sorted(names)
    return Observation(at, len(listed), tuple(listed[:MAX_LISTED_NAMES]))


def confirms(observations: tuple[Observation, ...], now: datetime, recheck: float) -> bool:
    """Whether recorded absences, the newest just made, confirm absence: two reads at
    least ``recheck`` apart."""
    return len(observations) >= 2 and (now - observations[0].observed_at).total_seconds() >= recheck


def when(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


class CreationReconciler:
    """The running worker's maintenance pass over creations with an unknown outcome.

    Every ``interval`` seconds it makes one read for each ``creating`` environment
    without an identity whose record is past the grace, skipping any environment
    whose lock is held or awaited (a creation or deletion under way). Each read holds
    only that environment's lock, never two at once, and never sleeps under it.
    """

    def __init__(
        self, environments: Environments, interval: float, *, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self.environments = environments
        self.interval = seconds("interval", interval)
        self.clock = clock

    async def run(self) -> None:
        while True:
            await self.clock.sleep(self.interval)
            try:
                await self.environments.reconcile_creations()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad round must not end reconciliation for good.
                _log.exception("reconciling unknown creations failed")


def refusal(outcome: Outcome) -> WorkerError:
    return WorkerError(ErrorKind.UNAVAILABLE, outcome.message)
