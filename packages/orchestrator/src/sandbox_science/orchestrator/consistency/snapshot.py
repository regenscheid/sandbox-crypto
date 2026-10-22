# SPDX-License-Identifier: MIT
"""Review evidence admission and immutable snapshot plans, without store access."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Final

from .models import ReviewType
from .values import Json, digest, dump, stored_json

MAX_ITEM_BYTES: Final = 256 * 1024
MAX_SNAPSHOT_BYTES: Final = 8 * 1024 * 1024
MAX_SNAPSHOT_ITEMS: Final = 256
ITEM_OVERHEAD: Final = 256
PRIOR_ITEMS: Final = 8
PRIOR_BYTES: Final = 128 * 1024
MAX_SNAPSHOT_FINDINGS: Final = 100


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One exact item a review snapshot pins."""

    kind: str
    locator: str
    payload: str
    sha256: str
    complete: int

    @property
    def size(self) -> int:
        return len(self.locator.encode()) + len(self.payload.encode()) + ITEM_OVERHEAD


@dataclass(frozen=True, slots=True)
class EvidenceGrant:
    """An earlier evidence record, serialized so its nested values cannot change."""

    evidence: str
    record: str

    size: int = field(init=False)
    complete: bool = field(init=False)

    def __post_init__(self) -> None:
        # Admission asks for this metadata more than once; parse a large immutable
        # grant only once, without retaining a mutable decoded payload in the plan.
        record = stored_json(self.record)
        object.__setattr__(
            self,
            "size",
            len(dump(record["locator"]).encode())
            + len(dump(record["payload"]).encode())
            + ITEM_OVERHEAD,
        )
        object.__setattr__(self, "complete", bool(record["complete"]))

    @classmethod
    def from_record(cls, evidence: str, record: Json) -> EvidenceGrant:
        return cls(evidence, dump(record))


@dataclass(frozen=True, slots=True)
class ResponsePin:
    finding: str
    record: str


@dataclass(frozen=True, slots=True)
class FindingGroup:
    """An open finding's changed evidence, finding item, grants and response pin."""

    finding: str
    revision: int
    items: tuple[EvidenceItem, ...]
    grants: tuple[EvidenceGrant, ...]
    body_pin: str | None


@dataclass(frozen=True, slots=True)
class SnapshotWindow:
    watermark: int
    retry: int | None
    end: int
    failures: int


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    bytes: int = MAX_SNAPSHOT_BYTES
    items: int = MAX_SNAPSHOT_ITEMS
    item_bytes: int = MAX_ITEM_BYTES


@dataclass(frozen=True, slots=True)
class SnapshotInputs:
    kind: ReviewType
    window: SnapshotWindow
    targets: tuple[tuple[str, int], ...]
    coverage_complete: bool
    finding_count: int
    max_feed_bytes: int
    max_feed_items: int
    shown: tuple[EvidenceItem, ...] = ()
    limits: SnapshotLimits = SnapshotLimits()


@dataclass(frozen=True, slots=True)
class SnapshotPlan:
    kind: ReviewType
    window: SnapshotWindow
    selected_end: int
    items: tuple[EvidenceItem, ...]
    grants: tuple[EvidenceGrant, ...]
    targets: tuple[tuple[str, int], ...]
    response_pins: tuple[ResponsePin, ...]
    complete: bool
    span_complete: bool
    confirmation_key: str | None


@dataclass
class _Budget:
    size: int = 0
    items: int = 0

    @staticmethod
    def cost(group: FindingGroup) -> int:
        return sum(item.size for item in group.items) + sum(grant.size for grant in group.grants)

    def fits(self, group: FindingGroup, limits: SnapshotLimits, share: int = 1) -> bool:
        return (
            all(len(item.payload.encode()) <= limits.item_bytes for item in group.items)
            and self.size + self.cost(group) <= limits.bytes // share
            and self.items + len(group.items) + len(group.grants) <= limits.items // share
        )

    def add(self, group: FindingGroup) -> None:
        self.size += self.cost(group)
        self.items += len(group.items) + len(group.grants)


def confirmation_key(targets: Iterable[tuple[str, int]], pins: Iterable[ResponsePin]) -> str:
    """The targets and exact response bodies/macro contexts a confirmation checks."""
    copied = [(pin.finding, stored_json(pin.record)) for pin in pins]
    return digest(
        dump(
            [
                tuple(targets),
                sorted(
                    (finding, pin["revision"], pin["sha256"], digest(dump(pin["macro_context"])))
                    for finding, pin in copied
                ),
            ]
        )
    )


class SnapshotPlanner:
    """Admit materialized evidence in two passes and produce an immutable plan.

    The caller supplies findings in priority order to ``reserve``, selects the feed
    using ``feed_room`` and ``feed_ceiling``, then supplies omitted findings again to
    ``admit``. Keeping materialization outside this policy preserves bounded reads and
    the second pass's fresh response pins without granting access to the repository.
    """

    def __init__(self, inputs: SnapshotInputs) -> None:
        self.inputs = inputs
        self._groups: dict[int, FindingGroup] = {}
        self._grants: dict[str, EvidenceGrant] = {}
        self._reserved = _Budget(sum(item.size for item in inputs.shown), len(inputs.shown))
        self._budget = _Budget()
        self._feed: tuple[EvidenceItem, ...] = ()
        self._end = inputs.window.watermark

    @property
    def granted(self) -> frozenset[str]:
        """Evidence already admitted, which need not be read again."""
        return frozenset(self._grants)

    def admitted(self, index: int) -> bool:
        return index in self._groups

    def _admit(self, index: int, group: FindingGroup, budget: _Budget, share: int) -> None:
        group = replace(
            group,
            grants=tuple(grant for grant in group.grants if grant.evidence not in self._grants),
        )
        if budget.fits(group, self.inputs.limits, share):
            self._groups[index] = group
            self._grants.update((grant.evidence, grant) for grant in group.grants)
            budget.add(group)

    def reserve(self, index: int, group: FindingGroup) -> None:
        """Give findings their initial share, ahead of new feed evidence."""
        self._admit(index, group, self._reserved, 1 if self.inputs.kind == "confirmation" else 2)

    @property
    def feed_room(self) -> tuple[int, int]:
        limits = self.inputs.limits
        shrink = 4 ** min(self.inputs.window.failures, 2)
        return (
            min(limits.bytes - self._reserved.size, max(1, self.inputs.max_feed_bytes // shrink)),
            min(limits.items - self._reserved.items, max(1, self.inputs.max_feed_items // shrink)),
        )

    @property
    def feed_ceiling(self) -> int:
        return self.inputs.limits.items - self._reserved.items

    def select_feed(self, end: int, items: Iterable[EvidenceItem]) -> None:
        self._end, self._feed = end, tuple(items)
        self._budget = _Budget(
            sum(item.size for item in self._feed) + self._reserved.size,
            len(self._feed) + self._reserved.items,
        )

    def admit(self, index: int, group: FindingGroup) -> None:
        """Give an omitted finding room left after the feed, within the full budget."""
        self._admit(index, group, self._budget, 1)

    @property
    def targets(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (group.finding, group.revision)
            for _, group in sorted(self._groups.items())
            if (group.finding, group.revision) in self.inputs.targets
        )

    def finish(self, fix_actions: EvidenceItem | None = None) -> SnapshotPlan:
        groups = [group for _, group in sorted(self._groups.items())]
        items = (
            self._feed
            + self.inputs.shown
            + tuple(item for group in groups for item in group.items)
            + (() if fix_actions is None else (fix_actions,))
        )
        pins = tuple(
            ResponsePin(group.finding, group.body_pin)
            for group in groups
            if group.body_pin is not None
        )
        grants = tuple(self._grants.values())
        confirmation = self.inputs.kind == "confirmation"
        span_complete = (
            self.inputs.coverage_complete
            and (not confirmation or set(self.targets) == set(self.inputs.targets))
            and len(groups) == self.inputs.finding_count
            and all(item.complete for item in items)
            and all(grant.complete for grant in grants)
        )
        return SnapshotPlan(
            kind=self.inputs.kind,
            window=self.inputs.window,
            selected_end=self._end,
            items=items,
            grants=grants,
            targets=self.targets,
            response_pins=pins,
            complete=span_complete and (confirmation or self._end == self.inputs.window.end),
            span_complete=span_complete,
            confirmation_key=confirmation_key(self.targets, pins) if confirmation else None,
        )
