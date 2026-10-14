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
        {"valid_until": 4102444800},  # a timestamp is no ISO text
        {"valid_until": "2099-01-01T00:00:00z"},
        {"escalation": {"timeout_seconds": 10**15, "on_timeout": "deny"}},  # no timedelta
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
        {"expires": 4102444800},
        {"expires": "4102444800"},
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


@pytest.mark.parametrize(
    ("expires", "until"),
    [
        ("20991014T120000Z", datetime(2099, 10, 14, 12, 0, tzinfo=UTC)),  # ISO basic format
        ("2099-10-14T12+00:00", datetime(2099, 10, 14, 12, 0, tzinfo=UTC)),  # hour only
    ],
)
def test_every_iso_time_the_fold_always_read_still_reads(expires: str, until: datetime) -> None:
    # A denial written this way must keep denying: skipping it would let an earlier
    # approval stand.
    raw = {"request_seq": 3, "scope": "request", "target": HTTP.payload(), "expires": expires}
    assert AnswerPayload.model_validate(raw).expires == until


def test_booleans_count_as_the_integers_they_always_did() -> None:
    # A denial numbered ``true`` still denies; nothing it closed opens.
    answer = AnswerPayload.model_validate(
        {"request_seq": True, "scope": "request", "target": HTTP.payload()}
    )
    assert answer.request_seq == 1
    terms = {"timeout_seconds": False, "on_timeout": "deny"}
    request = RequestPayload.model_validate({"target": HTTP.payload(), "escalation": terms})
    assert request.escalation.timeout_seconds == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"files": "not a list"},
        {"files": [{"path": "relative"}]},
        {"valid_until": "soon"},
        {"files": [{"path": PARI}], "valid_until": NOW.isoformat(), "purpose": "trust me"},
    ],
)
def test_other_kinds_ignore_the_file_set_fields(extra: dict[str, object]) -> None:
    payload = RequestPayload.model_validate(
        {"target": HTTP.payload(), "escalation": TERMS, **extra}
    )
    request = PermissionRequest.from_record(1, NOW, payload)
    assert (request.files, request.valid_until, request.asked, request.purpose) == (
        (),
        None,
        None,
        None,
    )


@pytest.mark.parametrize("kind", [[], {}, ["http"], 3])
def test_a_key_whose_kind_is_not_text_is_no_key(kind: object) -> None:
    assert RequestKey.from_payload({**HTTP.payload(), "kind": kind}) is None
