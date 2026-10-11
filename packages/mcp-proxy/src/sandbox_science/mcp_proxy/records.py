# SPDX-License-Identifier: MIT
"""Records the proxy keeps in the event store (docs/EVENT-STORE.md).

Three kinds, all with the source ``mcp-proxy``:

- ``escalation``: a call held for approval, which becomes a pending
  permission request. One that cannot be stored is refused with
  ``audit_unavailable`` instead of ``approval_required``: there is no
  request to approve, and the caller must not wait for one;
- ``alert``: a call that proceeds with a finding. It proceeds only once the
  record is stored; otherwise it is refused (``audit_unavailable``);
- ``mcp-call``: one record for every authenticated request, whatever its
  outcome: method, server, tool, decision and sizes, never argument values
  or results. Like the middleware's monitor findings, it is written on a
  best-effort basis and does not hold up the call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Protocol

from sandbox_science.event_store import EventStore

SOURCE: Final = "mcp-proxy"
"""The source of every record the proxy writes."""

STORE_TIMEOUT: Final = 1.0
"""Seconds an append may wait for the store's locks before it fails."""


class EventSink(Protocol):
    """Somewhere to append records."""

    async def record(self, kind: str, payload: Mapping[str, object]) -> None:
        """Append one record; raise if it could not be stored."""
        ...


class StoreSink:
    """Append records to the event store; each call returns once its record is on disk."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    async def record(self, kind: str, payload: Mapping[str, object]) -> None:
        """Append one record off the event loop; raise EventStoreError if it was not stored."""
        await self._store.append_async(kind, SOURCE, payload)
