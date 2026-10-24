# SPDX-License-Identifier: MIT
"""Provisioning operations' permission requests, in the policy middleware's event store.

When the policy escalates any file of a resolved install, the operation asks once for the
exact set (Conda provisioning, CP-8; open question 3): an ``escalation`` record whose
``target`` is a ``packages`` key (``sandbox_science.policy.approvals``) and whose
``files`` list each file by its path at the package cache. An operator answers it like
any permission request (D76): ``sandbox-science-policy-middleware approve --request N``.
The middleware then lets exactly those files download to the environment's sandbox under
the same policy revision, and the operation installs. However the operation ends
(installed, denied, timed out, cancelled, failed or cut short by a restart), the worker
then withdraws its request: a denial marked ``withdrawn``, after which nothing grants its
files again, a late approval included, and the command line refuses to answer it. The
denial is built from the request's key, which the worker holds, so a store it cannot read
does not stop it; a withdrawal that fails is tried again, and the request's own deadline
(``valid_until``) ends the grant in any case.

The store is the middleware's (``events.db`` in its state directory, which the worker's
``policy_store`` names). Writes and reads are blocking: callers run them in a thread.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from sandbox_science.event_store import EventStore, EventStoreError, StoreReader
from sandbox_science.policy.approvals import (
    DENIAL,
    ESCALATION,
    KINDS,
    Answer,
    Approvals,
    PermissionRequest,
    RequestKey,
    prepare_answer,
    withdrawal_payload,
)

from .provision_events import checked_payload

_log = logging.getLogger(__name__)

SOURCE: Final = "worker"
"""The source of the records the worker writes."""
ANSWERS: Final = "workspace"
"""The source of the user's answers the workspace relays (D97)."""
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
        self._keys: dict[int, RequestKey] = {}
        """The requests this worker recorded since it started, by sequence number."""

    def _store(self) -> EventStore:
        with self._lock:
            if self._writer is None:
                self._writer = EventStore(self.path, timeout=STORE_TIMEOUT)
            return self._writer

    def request(self, key: RequestKey, payload: Mapping[str, object]) -> int:
        """Record a permission request and return its sequence number. Blocking."""
        seq = self.request_record(key, payload)
        try:
            self.refresh()
        except PermissionStoreError as exc:
            _log.warning("permission request %d: %s", seq, exc)
        return seq

    def request_record(self, key: RequestKey, payload: Mapping[str, object]) -> int:
        """One bounded durable append; admission and this commit run on the serving loop."""
        try:
            record = self._store().append(ESCALATION, SOURCE, {**payload, "target": key.payload()})
        except EventStoreError as exc:
            raise PermissionStoreError(
                f"the permission request could not be recorded: {exc}"
            ) from exc
        with self._lock:
            self._keys[record.seq] = key
        return record.seq

    def observation(self, kind: str, payload: Mapping[str, object]) -> int:
        """Append only bounded provision.* observations, never a permission answer."""
        copied = checked_payload(kind, payload)
        try:
            return self._store().append(kind, SOURCE, copied).seq
        except EventStoreError:
            raise PermissionStoreError("provision observation append outcome unknown") from None

    def refresh(self) -> None:
        """Read the records added since the last look. Blocking."""
        if not self.path.is_file():
            return
        try:
            with StoreReader(self.path, timeout=STORE_TIMEOUT) as reader:
                after = self._approvals.last_seq
                self._approvals.load(list(reader.records(after=after, kinds=KINDS)))
        except EventStoreError as exc:
            raise PermissionStoreError(f"the event store cannot be read: {exc}") from exc

    def pending(self, seq: int) -> PermissionRequest | None:
        """The recorded request ``seq``, as the store holds it."""
        return self._approvals.request(seq)

    def answer(self, seq: int) -> Answer | None:
        """The newest answer to request ``seq``."""
        return self._approvals.answer_to(seq)

    def unanswered(self) -> list[PermissionRequest]:
        """The requests nobody answered and the worker did not withdraw, oldest first."""
        return [r for r in self._approvals.unanswered() if not self._approvals.withdrawn(r.seq)]

    def withdrawn(self, seq: int) -> bool:
        return self._approvals.withdrawn(seq)

    def escalations(self, seqs: Iterable[int]) -> dict[int, dict[str, object]]:
        """The payloads of the escalation records ``seqs``, for what their cards show.
        Blocking."""
        wanted = set(seqs)
        if not wanted or not self.path.is_file():
            return {}
        found: dict[int, dict[str, object]] = {}
        try:
            with StoreReader(self.path, timeout=STORE_TIMEOUT) as reader:
                for record in reader.records(after=min(wanted) - 1, kinds=(ESCALATION,)):
                    if record.seq in wanted:
                        with contextlib.suppress(ValueError):
                            found[record.seq] = record.data()
                    if record.seq >= max(wanted):
                        break
        except EventStoreError as exc:
            raise PermissionStoreError(f"the event store cannot be read: {exc}") from exc
        return found

    def answer_request(
        self, seq: int, *, approve: bool, scope: str, hours: float | None, now: datetime
    ) -> tuple[int, str]:
        """Record the user's answer to request ``seq``, which the workspace relays, as the
        source ``workspace``: the record's number and kind. ValueError if it cannot be
        answered so. Blocking."""
        self.refresh()
        kind, payload = prepare_answer(
            self._approvals, seq, approve=approve, scope=scope, hours=hours, now=now
        )
        try:
            record = self._store().append(kind, ANSWERS, payload)
        except EventStoreError as exc:
            raise PermissionStoreError(f"the answer could not be recorded: {exc}") from exc
        try:
            self.refresh()
        except PermissionStoreError as exc:
            _log.warning("request %d was answered, but the store cannot be read: %s", seq, exc)
        return record.seq, kind

    def withdraw(self, seq: int, reason: str, now: datetime) -> None:
        """Close request ``seq`` for good, answered or not, as its operation ends: a denial
        marked ``withdrawn``. Blocking.

        The denial is built from the request's key, as this worker recorded it or the store
        holds it, so that a failed read does not stop it; a second one is harmless.
        """
        del now  # a withdrawal has no expiry
        try:
            self.refresh()
        except PermissionStoreError as exc:
            _log.warning("withdrawing request %d without reading the store: %s", seq, exc)
        if self._approvals.withdrawn(seq):
            return
        with self._lock:
            key = self._keys.get(seq)
        if key is None:
            request = self._approvals.request(seq)
            if request is None:
                raise PermissionStoreError(f"request {seq} is not known: it cannot be withdrawn")
            key = request.key
        try:
            self._store().append(DENIAL, SOURCE, withdrawal_payload(seq, key, reason))
        except EventStoreError as exc:
            raise PermissionStoreError(f"request {seq} could not be withdrawn: {exc}") from exc
        try:
            self.refresh()
        except PermissionStoreError as exc:
            _log.warning("request %d was withdrawn, but the store cannot be read: %s", seq, exc)

    def withdraw_operations(self, operations: Iterable[str], reason: str, now: datetime) -> None:
        """Close every request these operations made that is still open, those a crash or a
        stop left before the operation noted them included. Blocking."""
        self.refresh()
        for seq in self._approvals.packages_requests(operations):
            self.withdraw(seq, reason, now)

    def close_operation(self, operation: str, seq: int | None, reason: str, now: datetime) -> None:
        """Close the requests of an operation that ends: the one it noted, from its key even
        if the store cannot be read, and any other the store holds for it. Blocking."""
        failed: PermissionStoreError | None = None
        if seq is not None:
            try:
                self.withdraw(seq, reason, now)
            except PermissionStoreError as exc:
                failed = exc
        try:
            self.withdraw_operations([operation], reason, now)
        except PermissionStoreError as exc:
            failed = failed or exc
        if failed is not None:
            raise failed

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
