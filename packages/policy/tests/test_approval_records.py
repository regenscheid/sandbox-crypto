# SPDX-License-Identifier: MIT
"""Typed permission request records: what parses, and what is refused as malformed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sandbox_science.policy.approvals import PermissionRequest, RequestKey, files_digest
from sandbox_science.policy.approvals.records import AnswerPayload, RequestPayload

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
PARI = "/conda/conda-forge/linux-64/pari-2.15.4-h0_0.conda"
GMP = "/conda/conda-forge/linux-64/gmp-6.3.0-h0_1.conda"
HTTP = RequestKey.http("sb", DIGEST, "GET", "example.org", 443, "/a")
PACKAGES = RequestKey.packages("sb", DIGEST, "prv-1", [PARI, GMP])
TERMS = {"timeout_seconds": 600, "on_timeout": "deny"}


def packages_request(**change: object) -> dict[str, object]:
    return {
        "target": PACKAGES.payload(),
        "escalation": TERMS,
        "files": [{"path": GMP, "size": 1}, {"path": PARI}],
        "valid_until": (NOW + timedelta(hours=1)).isoformat(),
        "asked": "pari",
        "purpose": "number theory",
        **change,
    }


def test_a_request_reads_its_key_and_terms_and_ignores_other_facts() -> None:
    payload = RequestPayload.model_validate(
        {"target": HTTP.payload(), "escalation": TERMS, "request": {"method": "GET"}, "rule": "r"}
    )
    request = PermissionRequest.from_record(7, NOW, payload)
    assert (request.seq, request.key, request.timeout, request.on_timeout) == (
        7,
        HTTP,
        timedelta(seconds=600),
        "deny",
    )
    assert request.files == ()
    assert request.valid_until is None
    assert not request.expired(NOW + timedelta(seconds=599))
    assert request.expired(NOW + timedelta(seconds=600))
    # The first format: a key without ``kind`` is an http key.
    legacy = {k: v for k, v in HTTP.payload().items() if k != "kind"}
    assert RequestPayload.model_validate({"target": legacy, "escalation": TERMS}).target == HTTP


def test_a_packages_request_carries_its_exact_file_set_deadline_and_context() -> None:
    raw = packages_request(asked="pari\x00 \n gmp", purpose=["not", "text"])
    request = PermissionRequest.from_record(3, NOW, RequestPayload.model_validate(raw))
    assert request.files == tuple(sorted((PARI, GMP)))
    assert request.valid_until == NOW + timedelta(hours=1)
    assert (request.asked, request.purpose) == ("pari gmp", None)


@pytest.mark.parametrize(
    "change",
    [
        {"files": None},
        {"files": []},
        {"files": [{"path": PARI}]},  # not the set the key names
        {"files": [{"path": PARI}, {"path": GMP}, {"path": "/other.conda"}]},
        {"files": [{"path": "relative/pari.conda"}, {"path": GMP}]},
        {"files": [PARI, GMP]},
        {"valid_until": None},
        {"valid_until": "2026-10-14T13:00:00"},  # no timezone
        {"escalation": {"timeout_seconds": True, "on_timeout": "deny"}},
        {"escalation": {"timeout_seconds": 600, "on_timeout": "allow"}},
        {"target": {**PACKAGES.payload(), "kind": "other"}},
    ],
)
def test_a_malformed_request_does_not_parse(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RequestPayload.model_validate(packages_request(**change))


def test_too_many_files_do_not_parse() -> None:
    paths = [f"/conda/f-{n}.conda" for n in range(2001)]
    key = RequestKey.packages("sb", DIGEST, "prv-1", paths)
    raw = packages_request(target=key.payload(), files=[{"path": path} for path in paths])
    with pytest.raises(ValidationError):
        RequestPayload.model_validate(raw)
    assert files_digest(paths[:2000]) != key.fields()["files"]


def test_an_answer_reads_its_scope_expiry_and_withdrawal() -> None:
    answer = AnswerPayload.model_validate(
        {
            "request_seq": 3,
            "scope": "files",
            "target": PACKAGES.payload(),
            "expires": None,
            "withdrawn": True,
            "reason": "ended",
        }
    )
    assert (answer.request_seq, answer.target, answer.expires, answer.withdrawn) == (
        3,
        PACKAGES,
        None,
        True,
    )
    # Only a literal true withdraws.
    assert not AnswerPayload.model_validate(
        {"request_seq": 3, "scope": "files", "target": PACKAGES.payload(), "withdrawn": "yes"}
    ).withdrawn


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "host"},  # not a packages scope
        {"request_seq": True},
        {"request_seq": "3"},
        {"expires": "soon"},
        {"expires": "2026-10-14T13:00:00"},
        {"target": {"kind": "packages"}},
    ],
)
def test_a_malformed_answer_does_not_parse(change: dict[str, object]) -> None:
    raw: dict[str, object] = {"request_seq": 3, "scope": "files", "target": PACKAGES.payload()}
    with pytest.raises(ValidationError):
        AnswerPayload.model_validate({**raw, **change})
