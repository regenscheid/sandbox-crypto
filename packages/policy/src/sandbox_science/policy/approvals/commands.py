# SPDX-License-Identifier: MIT
"""What the ``pending``, ``approve`` and ``deny`` commands of the policy middleware and the
MCP proxy share: each reads its own event store and appends to it as the operator."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Final

from .keys import DEFAULT_SCOPES, check_scope
from .ledger import APPROVAL, DENIAL, Approvals, RecordLike
from .records import answer_payload
from .render import pending_lines, request_lines

MAX_APPROVAL_HOURS: Final = 24 * 30

Append = Callable[[str, dict[str, object]], int]
"""Appends a record of a kind and payload as the operator; returns its sequence number."""


def prepare_answer(
    approvals: Approvals,
    seq: int,
    *,
    approve: bool,
    scope: str | None,
    hours: float | None,
    now: datetime,
) -> tuple[str, dict[str, object]]:
    """The kind and payload of an approval or denial of request ``seq``; ValueError if invalid.

    The scope defaults to the request itself; an approval lasts ``hours``.
    """
    request = approvals.request(seq)
    if request is None:
        raise ValueError(f"no permission request {seq}")
    if approvals.withdrawn(seq):
        raise ValueError(
            f"permission request {seq} was withdrawn: its provisioning operation has ended, "
            "and its files can no longer be granted"
        )
    scope = scope or DEFAULT_SCOPES[request.key.kind]
    problem = check_scope(request.key.kind, scope)
    if problem is not None:
        raise ValueError(problem)
    expires = None
    if approve and hours is not None:
        if not 0 < hours <= MAX_APPROVAL_HOURS:
            raise ValueError(
                f"an approval lasts more than 0 and at most {MAX_APPROVAL_HOURS} hours"
            )
        expires = now + timedelta(hours=hours)
    return (APPROVAL if approve else DENIAL), answer_payload(request, scope, expires)


def pending(records: Iterable[RecordLike], seq: int | None = None) -> int:
    """Print the requests nobody answered, or request ``seq`` in full; the exit status."""
    approvals = Approvals()
    approvals.load(records)
    if seq is not None:
        shown = request_lines(approvals, seq)
        if shown is None:
            print(f"error: no permission request {seq}", file=sys.stderr)
            return 1
        print("\n".join(shown))
        return 0
    lines = pending_lines(approvals)
    print("\n".join(lines) if lines else "no permission requests are waiting")
    return 0


def answer(
    records: Iterable[RecordLike],
    append: Append,
    seq: int,
    *,
    approve: bool,
    scope: str | None,
    hours: float | None,
    now: datetime,
) -> int:
    """Append an approval or a denial of permission request ``seq``; the exit status."""
    approvals = Approvals()
    approvals.load(records)
    try:
        kind, payload = prepare_answer(
            approvals, seq, approve=approve, scope=scope, hours=hours, now=now
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    record = append(kind, payload)
    print(f"{'approved' if approve else 'denied'} request {seq} as record {record}")
    files = approvals.files(seq)
    if files and approve:
        print(f"its {len(files)} package files may download; pending --request {seq} lists them")
    return 0
