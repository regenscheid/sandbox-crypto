# SPDX-License-Identifier: MIT
"""Exact-file provisioning permission requests, answers and withdrawal."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from sandbox_science.contracts import (
    Clock,
    ErrorInfo,
    ErrorKind,
    ProvisionOperation,
    ProvisionOutcome,
    ProvisionState,
)
from sandbox_science.policy import Action
from sandbox_science.policy.approvals import VALID_UNTIL, RequestKey

from .permissions import Permissions, PermissionStoreError
from .provision_types import (
    GRANT_MARGIN,
    Governing,
    Observe,
    Operation,
    PermissionGrant,
    RecordUpdate,
    Stop,
    asked,
    described,
    install_timeout,
)
from .recovery_fence import check_dispatch
from .resolution import Decided

_log = logging.getLogger(__name__)


class ProvisionPermissions:
    """Own permission interaction without owning queue or operation lifetime."""

    def __init__(
        self,
        permissions: Permissions | None,
        clock: Clock,
        poll: float,
        update: RecordUpdate,
        observe: Observe,
    ) -> None:
        self.permissions = permissions
        self.clock = clock
        self.poll = poll
        self._update = update
        self._observe = observe
        self._owed: set[str] = set()

    async def restart(self, provision_ids: Sequence[str]) -> None:
        """Withdraw requests left open before any restarted operation is classified."""
        if self.permissions is None:
            return
        try:
            await asyncio.to_thread(
                self.permissions.withdraw_operations,
                provision_ids,
                "the worker restarted",
                self.clock.now(),
            )
        except PermissionStoreError as exc:
            _log.warning("could not withdraw the permission requests left open: %s", exc)

    def stopped(self, record: ProvisionOperation) -> None:
        """Close synchronously during shutdown; startup closes an interrupted write."""
        if self.permissions is not None:
            with contextlib.suppress(PermissionStoreError):
                self.permissions.close_operation(
                    record.provision_id,
                    record.permission_request,
                    "the worker stopped",
                    self.clock.now(),
                )

    async def request(
        self, op: Operation, governing: Governing, decided: Decided
    ) -> PermissionGrant:
        """Ask once for the exact resolved file set, and wait for the answer (CP-8)."""
        request = op.record.request
        escalated = [decided.decisions[index] for index in decided.with_action(Action.ESCALATE)]
        if self.permissions is None or governing.sandbox_id is None:
            raise Stop(
                ProvisionOutcome.DENIED,
                "permission_unavailable",
                "permission",
                ErrorInfo(
                    kind=ErrorKind.POLICY_DENIED,
                    message=f"the policy asks for permission to download {len(escalated)} "
                    "package files, and this worker cannot record permission requests",
                ),
            )
        timeout = min(
            (decision.escalation.timeout for decision in escalated if decision.escalation),
            default=governing.policy.policy.escalation.timeout,
        )
        key = RequestKey.packages(
            governing.sandbox_id, governing.policy.digest, op.record.provision_id, decided.paths
        )
        payload: dict[str, object] = {
            "sandbox_id": governing.sandbox_id,
            "project_id": request.context.project_id,
            "environment_id": request.context.environment_id,
            "provision_id": op.record.provision_id,
            "policy": {
                "name": governing.policy.policy.metadata.name,
                "digest": governing.policy.digest,
            },
            "rule": escalated[0].rule,
            "reason": f"install {len(decided.files)} package files",
            # The agent's words, as text the store can hold whatever the client sent.
            "purpose": request.purpose[:500].encode("utf-8", "replace").decode("utf-8"),
            "escalation": {"timeout_seconds": int(timeout.total_seconds()), "on_timeout": "deny"},
            "asked": asked(request),
            VALID_UNTIL: (
                self.clock.now()
                + timeout
                + timedelta(seconds=install_timeout(governing))
                + GRANT_MARGIN
            ).isoformat(timespec="seconds"),
            "files": [
                {"path": path, **described(file)}
                for path, file in zip(decided.paths, decided.files, strict=True)
            ],
        }
        try:
            check_dispatch()
            seq = self.permissions.request_record(key, payload)
        except PermissionStoreError as exc:
            raise Stop(
                ProvisionOutcome.DENIED,
                "permission_unavailable",
                "permission",
                ErrorInfo(kind=ErrorKind.UNAVAILABLE, message=str(exc)),
            ) from None
        try:
            await asyncio.to_thread(self.permissions.refresh)
        except PermissionStoreError as exc:
            _log.warning("permission request %d: %s", seq, exc)
        minutes = max(1, round(timeout.total_seconds() / 60))
        self._update(
            op,
            state=ProvisionState.AWAITING_PERMISSION,
            outcome=ProvisionOutcome.AWAITING_PERMISSION,
            permission_request=seq,
            next_step=f"Permission request {seq} asks to download the {len(decided.files)} "
            "package files listed. On the pool host, the user reviews them with "
            f"`sandbox-science-policy-middleware pending --request {seq}`, then approves the "
            f"request (`approve --request {seq}`) or denies it. The operation installs once "
            f"it is approved, and ends denied if it is refused or nobody answers within "
            f"{minutes} minutes.",
        )
        _log.info("provisioning %s waits for permission request %d", op.record.provision_id, seq)
        deadline = self.clock.now() + timeout
        await self._observe(op.record, "provision.awaiting_permission")
        return await self.await_answer(op, seq, self.permissions, deadline, timeout)

    async def await_answer(
        self,
        op: Operation,
        seq: int,
        permissions: Permissions,
        deadline: datetime,
        timeout: timedelta,
    ) -> PermissionGrant:
        """Wait for the answer, until ``deadline``: its own, so that a store the worker can
        no longer read cannot hold the environment's turn for ever."""
        while True:
            if op.cancelled.is_set():
                await self.withdraw(seq, "the operation was cancelled")
                raise Stop(
                    ProvisionOutcome.FAILED,
                    "cancelled",
                    "permission",
                    ErrorInfo(kind=ErrorKind.CANCELLED, message="cancelled while it waited"),
                    next_step="Nothing was installed; the permission request was withdrawn.",
                )
            try:
                await asyncio.to_thread(permissions.refresh)
            except PermissionStoreError as exc:
                _log.warning("provisioning %s: %s", op.record.provision_id, exc)
            answer = permissions.answer(seq)
            now = self.clock.now()
            if answer is not None and answer.kind == "approval":
                if answer.expires is not None and answer.expires <= now:
                    raise Stop(
                        ProvisionOutcome.DENIED,
                        "permission_timed_out",
                        "permission",
                        ErrorInfo(
                            kind=ErrorKind.POLICY_DENIED,
                            message=f"the approval of permission request {seq} expired before "
                            "the install",
                        ),
                    )
                self._update(op, outcome=None, next_step=None)
                return PermissionGrant(seq)
            if answer is not None:
                raise Stop(
                    ProvisionOutcome.DENIED,
                    "permission_denied",
                    "permission",
                    ErrorInfo(
                        kind=ErrorKind.POLICY_DENIED,
                        message=f"permission request {seq} was denied",
                    ),
                    next_step="The user refused these packages. Do not ask again for them "
                    "unless the user says so.",
                )
            if now >= deadline:
                await self.withdraw(seq, "nobody answered it in time")
                minutes = max(1, round(timeout.total_seconds() / 60))
                raise Stop(
                    ProvisionOutcome.DENIED,
                    "permission_timed_out",
                    "permission",
                    ErrorInfo(
                        kind=ErrorKind.POLICY_DENIED,
                        message=f"nobody answered permission request {seq} within {minutes} "
                        "minutes",
                    ),
                    next_step="Ask the user about it, then request the install again.",
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(op.cancelled.wait(), self.poll)

    async def withdraw(self, seq: int, reason: str) -> None:
        """Close a permission request for good, as its wait ends; its operation's end closes
        it again if this fails."""
        if self.permissions is None:
            return
        try:
            await asyncio.to_thread(self.permissions.withdraw, seq, reason, self.clock.now())
        except PermissionStoreError as exc:
            _log.warning("could not withdraw permission request %d: %s", seq, exc)

    async def close(self, provision_id: str, seq: int | None, reason: str) -> None:
        """Close every permission request of an operation that ends, and those still owed
        by operations before it; one that cannot be closed now stays owed."""
        if self.permissions is None:
            return
        for owed in sorted(self._owed - {provision_id}):
            try:
                await asyncio.to_thread(
                    self.permissions.withdraw_operations,
                    [owed],
                    "its operation ended",
                    self.clock.now(),
                )
                self._owed.discard(owed)
            except PermissionStoreError:
                break  # the store is still out of reach
        if seq is None and provision_id not in self._owed:
            return  # it asked nothing: a request a stop cut short is closed at the next start
        try:
            await asyncio.to_thread(
                self.permissions.close_operation, provision_id, seq, reason, self.clock.now()
            )
            self._owed.discard(provision_id)
        except PermissionStoreError as exc:
            self._owed.add(provision_id)
            _log.warning("could not close the permission requests of %s: %s", provision_id, exc)
