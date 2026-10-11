# SPDX-License-Identifier: MIT
"""Records the middleware keeps for the approval flow and the monitors.

OpenShell logs every middleware decision itself (OCSF), but not the text of
our findings. The middleware therefore records what other components act on:
escalations, which become pending permission requests, alerts and monitor
findings. They go to the event store (docs/EVENT-STORE.md), whose hash chain
makes later changes to them evident.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Protocol

from sandbox_science.event_store import EventStore

SOURCE: Final = "policy-middleware"
"""The source of every record the middleware writes."""

OPERATOR: Final = "operator"
"""The source of approvals and denials written with the command line."""

STORE_TIMEOUT: Final = 0.25
"""Seconds an append may wait for the store's locks before it fails.

OpenShell gives an evaluation 500 ms in the documented registration, so an
append must give up well before that; the request is then denied if it needed
the record (an alert).
"""


class EventSink(Protocol):
    """Somewhere to append middleware records."""

    def record(self, kind: str, payload: Mapping[str, object]) -> int | None:
        """Append one record and return its sequence number if known; raise if not stored."""
        ...


class StoreSink:
    """Append records to the event store; each call returns once its record is on disk."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    def record(self, kind: str, payload: Mapping[str, object]) -> int:
        """Append one record and return its sequence number; raise EventStoreError if not stored."""
        return self._store.append(kind, SOURCE, payload).seq
