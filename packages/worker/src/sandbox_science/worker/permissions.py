# SPDX-License-Identifier: MIT
"""Provisioning operations' permission requests, in the policy middleware's event store.

When the policy escalates any file of a resolved install, the operation asks once for the
exact set (Conda provisioning, CP-8; open question 3): an ``escalation`` record whose
``target`` is a ``packages`` key (``sandbox_science.policy.approvals``) and whose
``files`` list each file by its path at the package cache. An operator answers it like
any permission request (D76): ``sandbox-science-policy-middleware approve --request N``.
The middleware then lets exactly those files download to the environment's sandbox under
the same policy revision, and the operation installs. A denial, a timeout or a
cancellation ends the operation, and the worker records a denial for a request nobody
answered, so that it leaves the operator's list and a late approval cannot open it.

The store is the middleware's (``events.db`` in its state directory, which the worker's
``policy_store`` names). Writes and reads are blocking: callers run them in a thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from sandbox_science.event_store import EventStore, EventStoreError, StoreReader
from sandbox_science.policy.approvals import (
    ESCALATION,
    Answer,
    Approvals,
    PendingRequest,
    RequestKey,
    prepare_answer,
)

_log = logging.getLogger(__name__)

SOURCE: Final = "worker"
"""The source of the records the worker writes."""
STORE_TIMEOUT: Final = 5.0


class PermissionStoreError(Exception):
    """A permission request could not be recorded or read."""


class Permissions:
    """The worker's permission requests and their answers, in one event store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._approvals = Approvals()
        self._writer: EventStore | None = None
        self._lock = threading.Lock()

    def _store(self) -> EventStore:
        with self._lock:
            if self._writer is None:
                self._writer = EventStore(self.path, timeout=STORE_TIMEOUT)
            return self._writer

    def request(self, key: RequestKey, payload: Mapping[str, object]) -> int:
        """Record a permission request and return its sequence number. Blocking."""
        try:
            record = self._store().append(ESCALATION, SOURCE, {**payload, "target": key.payload()})
        except EventStoreError as exc:
            raise PermissionStoreError(
                f"the permission request could not be recorded: {exc}"
            ) from exc
        self.refresh()
        return record.seq

    def refresh(self) -> None:
        """Read the records added since the last look. Blocking."""
        if not self.path.is_file():
            return
        try:
            with StoreReader(self.path, timeout=STORE_TIMEOUT) as reader:
                self._approvals.load(list(reader.records(after=self._approvals.last_seq)))
        except EventStoreError as exc:
            raise PermissionStoreError(f"the event store cannot be read: {exc}") from exc

    def pending(self, seq: int) -> PendingRequest | None:
        """The recorded request ``seq``, as the store holds it."""
        return self._approvals.request(seq)

    def answer(self, seq: int) -> Answer | None:
        """The newest answer to request ``seq``."""
        return self._approvals.answer_to(seq)

    def withdraw(self, seq: int, reason: str, now: datetime) -> None:
        """Record a denial of request ``seq``, unless it is answered already. Blocking."""
        self.refresh()
        if self._approvals.answer_to(seq) is not None:
            return
        try:
            kind, payload = prepare_answer(
                self._approvals, seq, approve=False, scope=None, hours=None, now=now
            )
            self._store().append(kind, SOURCE, {**payload, "reason": reason[:500]})
        except (ValueError, EventStoreError) as exc:
            raise PermissionStoreError(f"request {seq} could not be withdrawn: {exc}") from exc
        self.refresh()

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
