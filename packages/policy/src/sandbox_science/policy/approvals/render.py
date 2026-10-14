# SPDX-License-Identifier: MIT
"""Permission requests shown to an operator, one line each or one request in full."""

from __future__ import annotations

from typing import Final

from .keys import RequestKey
from .ledger import APPROVAL, Approvals

LISTED_FILES: Final = 20
"""Files of a ``packages`` request that ``pending`` lists before it counts the rest;
``pending --request`` lists them all."""


def describe(key: RequestKey) -> str:
    """A permission request's key on one line, for an operator."""
    fields = key.fields()
    if key.kind == "http":
        where = f"{fields['host']}:{fields['port']}{fields['path']}"
        return f"{fields['sandbox_id']}  {fields['method']} {where}"
    if key.kind == "packages":
        return f"{fields['sandbox_id']}  install package files for {fields['operation']}"
    return f"{fields['caller']}  {fields['server']} {fields['method']} {fields['tool']}"


def pending_lines(approvals: Approvals) -> list[str]:
    """One line per request nobody answered, for ``pending`` commands, and below a
    ``packages`` request the files it would let the sandbox download."""
    lines: list[str] = []
    for request in approvals.unanswered():
        lines.append(
            f"{request.seq}  {request.recorded:%Y-%m-%d %H:%M:%S}  {describe(request.key)}  "
            f"(after {request.timeout.total_seconds() / 60:.0f} min: {request.on_timeout})"
        )
        if request.key.kind == "packages":
            files = request.files
            lines += [f"      {path}" for path in files[:LISTED_FILES]]
            if len(files) > LISTED_FILES:
                lines.append(
                    f"      … {len(files) - LISTED_FILES} more: pending --request {request.seq} "
                    "lists them all"
                )
    return lines


def request_lines(approvals: Approvals, seq: int) -> list[str] | None:
    """Request ``seq`` in full, for ``pending --request``: what it asks, how it stands,
    and every file of a ``packages`` request. None if there is no such request."""
    request = approvals.request(seq)
    if request is None:
        return None
    answer = approvals.answer_to(seq)
    if approvals.withdrawn(seq):
        state = "withdrawn: its operation has ended"
    elif answer is None:
        state = f"waiting (after {request.timeout.total_seconds() / 60:.0f} min: "
        state += f"{request.on_timeout})"
    else:
        state = f"{'approved' if answer.kind == APPROVAL else 'denied'} by record {answer.seq}"
        if answer.expires is not None:
            state += f", until {answer.expires:%Y-%m-%d %H:%M:%S}"
    lines = [f"{request.seq}  {request.recorded:%Y-%m-%d %H:%M:%S}  {describe(request.key)}"]
    lines.append(f"      {state}")
    if request.asked is not None:
        lines.append(f"      asked for: {request.asked}")
    if request.purpose is not None:
        lines.append(f"      purpose (the agent's words): {request.purpose}")
    if request.valid_until is not None:
        lines.append(f"      grants nothing after {request.valid_until:%Y-%m-%d %H:%M:%S}")
    if request.files:
        lines.append(f"      {len(request.files)} package files:")
        lines += [f"      {path}" for path in request.files]
    return lines
