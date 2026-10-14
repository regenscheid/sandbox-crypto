# SPDX-License-Identifier: MIT
"""File-set grants: which package files a ``packages`` request's approval lets a sandbox
download (Conda provisioning, CP-8).

The worker's forecast and withdrawal and the middleware's enforcement must decide a grant
identically, so they share these rules over the ledger's requests and answers.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime

from .records import Answer, PermissionRequest


def granted(
    requests: Mapping[int, PermissionRequest],
    answers: Sequence[Answer],
    withdrawn: Collection[int],
    sandbox_id: str,
    policy_digest: str,
    path: str,
    now: datetime,
) -> Answer | None:
    """The approval that lets ``sandbox_id`` download the package file at ``path`` under
    ``policy_digest``, if any.

    A ``packages`` request's newest answer decides it, as for other requests: an approval
    grants the files the request lists, until it expires; a later denial withdraws them. A
    request the worker withdrew, as its operation ended, grants nothing, whatever answers
    it later, and neither does one past its own deadline. Among the requests that list the
    file, the newest live approval is returned.
    """
    newest: dict[int, Answer] = {}
    for answer in answers:
        request = requests.get(answer.request_seq)
        if (
            answer.key.kind != "packages"
            or request is None
            or request.key != answer.key
            or answer.request_seq in withdrawn
            or request.valid_until is None
            or now >= request.valid_until
            or path not in request.files
        ):
            continue
        fields = answer.key.fields()
        if fields["sandbox_id"] != sandbox_id or fields["policy_digest"] != policy_digest:
            continue
        current = newest.get(answer.request_seq)
        if current is None or answer.seq > current.seq:
            newest[answer.request_seq] = answer
    live = [
        answer
        for answer in newest.values()
        if answer.kind == "approval" and (answer.expires is None or now < answer.expires)
    ]
    return max(live, key=lambda answer: answer.seq) if live else None


def open_requests(
    requests: Mapping[int, PermissionRequest], withdrawn: Collection[int], operations: Iterable[str]
) -> list[int]:
    """The ``packages`` requests of these provisioning operations not yet withdrawn."""
    wanted = set(operations)
    return [
        seq
        for seq, request in sorted(requests.items())
        if request.key.kind == "packages"
        and request.key.fields()["operation"] in wanted
        and seq not in withdrawn
    ]
