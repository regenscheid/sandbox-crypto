# SPDX-License-Identifier: MIT
"""Permission requests for an exact set of package files (Conda provisioning, CP-8).

A provisioning operation asks once for the files a resolved install would download; an
approval grants those files, to that sandbox under that policy revision, and nothing
else. The policy middleware honors it (test_approvals.py of the middleware).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from sandbox_science.policy.approvals import (
    APPROVAL,
    DENIAL,
    ESCALATION,
    VALID_UNTIL,
    WITHDRAWN,
    Approvals,
    RequestKey,
    answer_payload,
    describe,
    files_digest,
    pending_lines,
    prepare_answer,
    request_lines,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
PARI = "/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda"
GMP = "/conda/conda-forge/linux-64/gmp-6.3.0-h0_1.conda"
OTHER = "/conda/conda-forge/linux-64/flint-3.1.0-h0_0.conda"


@dataclass(frozen=True)
class Record:
    seq: int
    time: str
    kind: str
    payload: dict[str, object]

    def data(self) -> dict[str, object]:
        return json.loads(json.dumps(self.payload))


def escalation(seq: int, key: RequestKey, paths: list[str], **extra: object) -> Record:
    payload: dict[str, object] = {
        "target": key.payload(),
        "escalation": {"timeout_seconds": 1800, "on_timeout": "deny"},
        VALID_UNTIL: (NOW + timedelta(hours=12)).isoformat(),
        "files": [{"path": path, "name": path.rsplit("/", 1)[-1]} for path in paths],
        **extra,
    }
    return Record(seq, NOW.isoformat(), ESCALATION, payload)


def answer(
    seq: int, request: int, kind: str, approvals: Approvals, hours: float | None = 8
) -> Record:
    pending = approvals.request(request)
    assert pending is not None
    expires = NOW + timedelta(hours=hours) if hours is not None and kind == APPROVAL else None
    return Record(seq, NOW.isoformat(), kind, answer_payload(pending, "files", expires))


def test_a_packages_key_names_its_files_by_their_digest() -> None:
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", [GMP, PARI, PARI])
    fields = key.fields()
    assert fields["files"] == files_digest([PARI, GMP])
    assert (fields["sandbox_id"], fields["operation"]) == ("sbx-1", "prv-1")
    assert RequestKey.from_payload(key.payload()) == key
    # Each operation asks anew, so its answers decide nothing else.
    assert RequestKey.packages("sbx-1", DIGEST, "prv-2", [PARI, GMP]) != key
    assert "install package files for prv-1" in describe(key)


def test_an_approval_grants_exactly_the_files_listed() -> None:
    approvals = Approvals()
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", [PARI, GMP])
    approvals.load([escalation(1, key, [PARI, GMP])])
    assert approvals.files(1) == tuple(sorted([PARI, GMP]))
    assert approvals.granted("sbx-1", DIGEST, PARI, NOW) is None  # not before an answer
    approvals.load([answer(2, 1, APPROVAL, approvals)])
    grant = approvals.granted("sbx-1", DIGEST, PARI, NOW)
    assert grant is not None
    assert (grant.seq, grant.request_seq) == (2, 1)
    assert approvals.granted("sbx-1", DIGEST, GMP, NOW) is not None
    # Nothing else: another file, sandbox or revision, or after the approval expires.
    assert approvals.granted("sbx-1", DIGEST, OTHER, NOW) is None
    assert approvals.granted("sbx-2", DIGEST, PARI, NOW) is None
    assert approvals.granted("sbx-1", "sha256:" + "b" * 64, PARI, NOW) is None
    assert approvals.granted("sbx-1", DIGEST, PARI, NOW + timedelta(hours=9)) is None
    assert approvals.answer_to(1) == grant


def test_the_newest_answer_decides_and_a_denial_withdraws_the_files() -> None:
    approvals = Approvals()
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", [PARI])
    approvals.load([escalation(1, key, [PARI])])
    approvals.load([answer(2, 1, APPROVAL, approvals)])
    approvals.load([answer(3, 1, DENIAL, approvals)])
    assert approvals.granted("sbx-1", DIGEST, PARI, NOW) is None
    denial = approvals.answer_to(1)
    assert denial is not None
    assert denial.kind == "denial"
    # Another operation's approval of the same file still grants it.
    second = RequestKey.packages("sbx-1", DIGEST, "prv-2", [PARI])
    approvals.load([escalation(4, second, [PARI])])
    approvals.load([answer(5, 4, APPROVAL, approvals)])
    grant = approvals.granted("sbx-1", DIGEST, PARI, NOW)
    assert grant is not None
    assert grant.request_seq == 4


def test_a_request_whose_files_are_not_its_keys_is_no_request() -> None:
    approvals = Approvals()
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", [PARI])
    approvals.load([escalation(1, key, [PARI, OTHER])])  # a file slipped into the list
    approvals.load([escalation(2, key, [])])
    approvals.load([escalation(3, key, ["relative/path.conda"])])
    assert approvals.unanswered() == []
    assert approvals.files(1) == ()


def test_an_answer_must_name_its_requests_key() -> None:
    approvals = Approvals()
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", [PARI])
    approvals.load([escalation(1, key, [PARI])])
    forged = RequestKey.packages("sbx-1", DIGEST, "prv-1", [PARI, OTHER])
    payload = {"request_seq": 1, "scope": "files", "target": forged.payload(), "expires": None}
    approvals.load([Record(2, NOW.isoformat(), APPROVAL, payload)])
    assert approvals.granted("sbx-1", DIGEST, PARI, NOW) is None
    assert approvals.answer_to(1) is None


def test_the_operator_answers_with_the_files_scope_by_default() -> None:
    approvals = Approvals()
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", [PARI, GMP])
    approvals.load([escalation(1, key, [PARI, GMP])])
    kind, payload = prepare_answer(approvals, 1, approve=True, scope=None, hours=2, now=NOW)
    assert (kind, payload["scope"]) == (APPROVAL, "files")
    lines = pending_lines(approvals)
    assert lines[0].startswith("1  ")
    assert "install package files for prv-1" in lines[0]
    assert [line.strip() for line in lines[1:]] == sorted([PARI, GMP])


def withdrawal(seq: int, request: int, approvals: Approvals) -> Record:
    """The worker's denial that closes a request as its operation ends."""
    pending = approvals.request(request)
    assert pending is not None
    payload = {**answer_payload(pending, "files", None), WITHDRAWN: True}
    return Record(seq, NOW.isoformat(), DENIAL, payload)


def test_a_withdrawn_request_grants_nothing_ever_again() -> None:
    approvals = Approvals()
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", [PARI])
    approvals.load([escalation(1, key, [PARI]), escalation(2, key, [PARI])])
    assert approvals.packages_requests(["prv-1"]) == [1, 2]
    assert approvals.packages_requests(["prv-9"]) == []
    approvals.load([answer(3, 1, APPROVAL, approvals)])
    approvals.load([withdrawal(4, 1, approvals)])
    assert approvals.withdrawn(1)
    assert approvals.packages_requests(["prv-1"]) == [2]
    assert approvals.granted("sbx-1", DIGEST, PARI, NOW) is None
    # A later approval, which the command line refuses, would open nothing either.
    approvals.load([answer(5, 1, APPROVAL, approvals)])
    assert approvals.granted("sbx-1", DIGEST, PARI, NOW) is None
    with pytest.raises(ValueError, match="withdrawn"):
        prepare_answer(approvals, 1, approve=True, scope=None, hours=2, now=NOW)
    # Only the worker's mark closes a request: an operator's denial can be answered again.
    approvals.load([answer(6, 2, DENIAL, approvals)])
    assert not approvals.withdrawn(2)
    approvals.load([answer(7, 2, APPROVAL, approvals)])
    assert approvals.granted("sbx-1", DIGEST, PARI, NOW) is not None


def test_a_request_is_shown_in_full_on_demand() -> None:
    approvals = Approvals()
    paths = [f"/conda/conda-forge/linux-64/p{index}-1.0-h0_0.conda" for index in range(25)]
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", paths)
    approvals.load([escalation(1, key, paths)])
    lines = pending_lines(approvals)
    assert lines[-1].strip() == "… 5 more: pending --request 1 lists them all"
    shown = request_lines(approvals, 1)
    assert shown is not None
    assert shown[1].strip().startswith("waiting")
    assert shown[2].strip().startswith("grants nothing after ")
    assert shown[3].strip() == "25 package files:"
    assert [line.strip() for line in shown[4:]] == sorted(paths)
    approvals.load([answer(2, 1, APPROVAL, approvals)])
    shown = request_lines(approvals, 1)
    assert shown is not None
    assert shown[1].strip().startswith("approved by record 2, until ")
    approvals.load([withdrawal(3, 1, approvals)])
    shown = request_lines(approvals, 1)
    assert shown is not None
    assert shown[1].strip() == "withdrawn: its operation has ended"
    assert request_lines(approvals, 9) is None


def test_a_file_set_grants_nothing_past_its_own_deadline() -> None:
    approvals = Approvals()
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", [PARI])
    until = NOW + timedelta(hours=1)
    approvals.load([escalation(1, key, [PARI], valid_until=until.isoformat())])
    approvals.load([answer(2, 1, APPROVAL, approvals, hours=8)])
    assert approvals.granted("sbx-1", DIGEST, PARI, NOW) is not None
    # The approval lasts eight hours; the request, one: a withdrawal that never came is moot.
    assert approvals.granted("sbx-1", DIGEST, PARI, until) is None
    # A file set without a deadline is no request.
    bare = escalation(3, key, [PARI])
    del bare.payload[VALID_UNTIL]
    approvals.load([bare])
    assert approvals.request(3) is None


def test_a_request_shows_what_was_asked_and_why_in_safe_text() -> None:
    approvals = Approvals()
    key = RequestKey.packages("sbx-1", DIGEST, "prv-1", [PARI])
    purpose = "factor\x1b[2Jthe\nlattice " + "x" * 400
    approvals.load([escalation(1, key, [PARI], asked="Conda: pari>=2.15", purpose=purpose)])
    shown = request_lines(approvals, 1)
    assert shown is not None
    assert "asked for: Conda: pari>=2.15" in shown[2]
    assert "\x1b" not in "".join(shown)
    assert shown[3].strip().startswith("purpose (the agent's words): factor [2Jthe lattice x")
    assert len(shown[3].strip()) < 340
    assert shown[4].strip().startswith("grants nothing after ")
