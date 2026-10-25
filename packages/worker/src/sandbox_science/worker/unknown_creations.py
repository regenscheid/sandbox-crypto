# SPDX-License-Identifier: MIT
"""Creations whose outcome is unknown, reconciled on confirmed absence (backlog B3).

When ``openshell sandbox create`` fails after the gateway may have received it, and no
sandbox identity has appeared, the worker cannot tell whether a sandbox exists. On
Kubernetes this is common: OpenShell's driver rolls a sandbox back itself when its
bootstrap misses the 5-minute grace, so the sandbox never appears. Absence is
*confirmed*, and the environment may be marked deleted (``creation_rolled_back``), only
when all of these hold:

- ``creation_grace`` has passed since the create command ended (success, failure or
  timeout); while the command runs, the environment's lock keeps every read away;
- the gateway has no sandbox by the environment's original name, and its complete
  listing, read page by page, shows none by that name and none under another name that
  carries the worker's ``ENVIRONMENT_LABEL`` with this environment's ID. A listing that
  misses any sandbox of the worker's other environments comes from another gateway or
  workspace, or one still starting, and confirms nothing;
- that absence was observed on at least two reads at least ``creation_recheck`` apart,
  by the wall clock and, within one process, by the monotonic clock too, each recorded
  on the environment.

Any read that shows the sandbox gives the environment its identity instead, and the
ordinary deletion by identity follows; nothing here deletes a sandbox. The running
worker checks every such environment each ``creation_recheck`` (``CreationReconciler``);
``sandbox-science-worker environments reconcile`` does the same with the worker stopped,
leaving the deletion hooks to the next running worker.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Sequence
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
ENVIRONMENT_LABEL: Final = "sandbox-science.io/environment"
"""The label ``Environments.create`` gives a sandbox: the environment's ID."""
_LABEL_VALUE: Final = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
MAX_LIST_PAGES: Final = 32
"""Pages of a sandbox listing read at most; a longer listing confirms nothing."""
LIST_PAGE_SIZE: Final = 100
"""Sandboxes per page, OpenShell's default."""


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


def page(document: object) -> tuple[list[dict[str, object]], str] | None:
    """One page of ``openshell sandbox list -o json``: its entries and the token of the
    next page (empty at the end); None for an answer the worker cannot read.

    OpenShell answers ``{"sandboxes": [...], "next_page_token": "..."}``."""
    if not isinstance(document, dict):
        return None
    mapping = cast(dict[str, object], document)
    entries, token = mapping.get("sandboxes"), mapping.get("next_page_token") or ""
    if not isinstance(entries, list) or not isinstance(token, str):
        return None
    items = cast(list[object], entries)
    if not all(isinstance(item, dict) for item in items):
        return None
    return [cast(dict[str, object], item) for item in items], token


def label_value(environment_id: str) -> str | None:
    """The value of ``ENVIRONMENT_LABEL`` for an environment, or None when its ID is no
    valid Kubernetes label value (over 63 characters, or not ending alphanumerically)."""
    return environment_id if _LABEL_VALUE.fullmatch(environment_id) else None


def _labels(entry: dict[str, object]) -> dict[str, object]:
    """A listed sandbox's labels, at the top level or under ``metadata``."""
    labels = entry.get("labels")
    metadata = entry.get("metadata")
    if not isinstance(labels, dict) and isinstance(metadata, dict):
        labels = cast(dict[str, object], metadata).get("labels")
    return cast(dict[str, object], labels) if isinstance(labels, dict) else {}


def examine(
    entries: Sequence[dict[str, object]],
    complete: bool,
    info: EnvironmentInfo,
    known: Sequence[EnvironmentInfo],
    *,
    labelled: bool = False,
) -> Listing:
    """Read a sandbox listing for traces of ``info``'s sandbox (B3).

    A listing counts as evidence of absence only when it ended (``complete``) and shows
    every sandbox of ``known``, the worker's other environments with an identity, by
    name or ID: a listing from another gateway or workspace, or from a gateway that is
    still rebuilding its index, misses them. A sandbox by the original name gives its
    identity. A sandbox under another name that carries ``ENVIRONMENT_LABEL`` with this
    environment's ID, and that no other record of the worker names, is a blocker when
    the pool labels its sandboxes (``labelled``): the label is the worker's own, so only
    that key counts, never a value alone. Otherwise the name alone decides."""
    names = tuple(str(entry.get("name")) for entry in entries)
    if not complete:
        return Listing(
            blocker=f"the gateway's sandbox listing has more than {MAX_LIST_PAGES} pages",
            names=names,
        )
    listed_names = set(names)
    listed_ids = {entry.get("id") for entry in entries}
    missing = [
        other.sandbox
        for other in known
        if other.sandbox not in listed_names and other.sandbox_id not in listed_ids
    ]
    if missing:
        shown = ", ".join(sorted(missing)[:5]) + (", …" if len(missing) > 5 else "")
        return Listing(
            blocker=f"listing incomplete: the worker's sandboxes {shown} are not listed, so the"
            " listing may come from another gateway or workspace, or one still starting",
            names=names,
        )
    owned = {other.sandbox for other in known}
    wanted = label_value(info.environment_id) if labelled else None
    for entry in entries:
        name = entry.get("name")
        if name == info.sandbox:
            identity = entry.get("id")
            if isinstance(identity, str) and identity:
                return Listing(sandbox_id=identity, names=names)
            return Listing(
                blocker=f"the gateway lists sandbox {info.sandbox} without an identity",
                names=names,
            )
        if (
            wanted is not None
            and name not in owned
            and _labels(entry).get(ENVIRONMENT_LABEL) == wanted
        ):
            return Listing(
                blocker=f"sandbox {str(name)[:64]!r} carries {ENVIRONMENT_LABEL}={wanted};"
                " inspect it with `openshell sandbox get`",
                names=names,
            )
    return Listing(names=names)


def observation(at: datetime, names: Iterable[str]) -> Observation:
    listed = sorted(names)
    return Observation(at, len(listed), tuple(listed[:MAX_LISTED_NAMES]))


def confirms(
    observations: tuple[Observation, ...],
    now: datetime,
    recheck: float,
    local: tuple[datetime, float] | None,
    monotonic: float,
) -> bool:
    """Whether recorded absences, the newest just made, confirm absence: two reads at
    least ``recheck`` apart by the wall clock. ``local`` is this process's first absence
    of the sandbox (wall time, monotonic time); when the first recorded read is that one,
    the gap must hold by the monotonic clock too, so a step of the wall clock cannot
    shorten it."""
    if len(observations) < 2:
        return False
    first = observations[0].observed_at
    if (now - first).total_seconds() < recheck:
        return False
    return local is None or local[0] != first or monotonic - local[1] >= recheck


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
