# SPDX-License-Identifier: MIT
"""Verification of OpenShell extension tokens."""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from sandbox_science.policy_middleware.auth import (
    Caller,
    TokenAuthenticator,
    UnverifiedCallers,
    default_audience,
    load_public_key,
    require_sandbox,
)
from sandbox_science.policy_middleware.errors import AuthError, SettingsError

AUDIENCE = default_audience("sandbox-science-policy")
ISSUER = "openshell-gateway:test-gateway"
KEY = ed25519.Ed25519PrivateKey.generate()


def authenticator(**overrides: object) -> TokenAuthenticator:
    options: dict[str, object] = {
        "public_key": KEY.public_key(),
        "gateway_id": "test-gateway",
        "audience": AUDIENCE,
    }
    options.update(overrides)
    return TokenAuthenticator(**options)  # pyright: ignore[reportArgumentType]


def token(
    *,
    key: object = KEY,
    algorithm: str = "EdDSA",
    typ: str = "openshell-ext+jwt",
    drop: tuple[str, ...] = (),
    **claims: object,
) -> str:
    now = int(time.time())
    body: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "jti": "test-jti",
        "caller_kind": "supervisor",
        "sandbox_id": "sbx-0001",
    }
    body.update(claims)
    for name in drop:
        body.pop(name)
    return jwt.encode(body, key, algorithm=algorithm, headers={"typ": typ})  # pyright: ignore[reportArgumentType]


def bearer(value: str) -> list[str]:
    return [f"Bearer {value}"]


def test_a_supervisor_token_names_its_sandbox() -> None:
    caller = authenticator().authenticate(bearer(token()))
    assert caller == Caller(kind="supervisor", sandbox_id="sbx-0001")


def test_a_gateway_token() -> None:
    caller = authenticator().authenticate(
        bearer(token(caller_kind="gateway", drop=("sandbox_id",)))
    )
    assert caller == Caller(kind="gateway")


def test_the_scheme_is_case_insensitive() -> None:
    assert authenticator().authenticate([f"bearer {token()}"]).kind == "supervisor"


def test_a_single_element_audience_list_is_accepted() -> None:
    assert authenticator().authenticate(bearer(token(aud=[AUDIENCE]))).kind == "supervisor"


def test_expiry_allows_the_configured_leeway() -> None:
    expired = token(exp=int(time.time()) - 10)
    assert authenticator().authenticate(bearer(expired)).kind == "supervisor"
    with pytest.raises(AuthError):
        authenticator(leeway=timedelta(0)).authenticate(bearer(expired))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(lambda: token(typ="JWT"), id="generic-typ"),
        pytest.param(lambda: token(key=ed25519.Ed25519PrivateKey.generate()), id="other-key"),
        pytest.param(lambda: token(exp=int(time.time()) - 3600), id="expired"),
        pytest.param(lambda: token(drop=("exp",)), id="no-expiry"),
        pytest.param(lambda: token(iss="https://gateway.example.org"), id="issuer-is-a-url"),
        pytest.param(lambda: token(iss="openshell-gateway:other"), id="other-gateway"),
        pytest.param(lambda: token(drop=("iss",)), id="no-issuer"),
        pytest.param(
            lambda: token(aud="urn:openshell:extension:middleware:other"), id="other-audience"
        ),
        pytest.param(lambda: token(aud=[AUDIENCE, "urn:other"]), id="extra-audience"),
        pytest.param(lambda: token(drop=("aud",)), id="no-audience"),
        pytest.param(lambda: token(drop=("caller_kind",)), id="no-caller-kind"),
        pytest.param(lambda: token(caller_kind="admin"), id="unknown-caller-kind"),
        pytest.param(lambda: token(drop=("sandbox_id",)), id="supervisor-without-sandbox"),
        pytest.param(lambda: token(sandbox_id=""), id="empty-sandbox"),
        pytest.param(lambda: token(sandbox_id=7), id="numeric-sandbox"),
        pytest.param(lambda: "not-a-token", id="garbage"),
        pytest.param(lambda: "a" * 9000, id="oversized"),
    ],
)
def test_invalid_tokens_are_rejected(value: Callable[[], str]) -> None:
    """Each token is made as its test runs, so that it is refused for its own fault and
    not because it expired while a long run reached it."""
    with pytest.raises(AuthError):
        authenticator().authenticate(bearer(value()))


def forge(header: dict[str, object], claims: dict[str, object], signature: bytes) -> str:
    """Build a token by hand, as an attacker would."""

    def part(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing_input = f"{part(json.dumps(header).encode())}.{part(json.dumps(claims).encode())}"
    return f"{signing_input}.{part(signature)}"


def forged_claims() -> dict[str, object]:
    return {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300, "caller_kind": "gateway"}


def test_algorithm_confusion_is_rejected() -> None:
    # HS256 keyed with the public key, which an attacker knows.
    public_pem = KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header: dict[str, object] = {"alg": "HS256", "typ": "openshell-ext+jwt"}
    claims = forged_claims()  # once: the signature covers these very claims
    unsigned = forge(header, claims, b"").rsplit(".", 1)[0]
    signature = hmac.new(public_pem, unsigned.encode(), hashlib.sha256).digest()
    with pytest.raises(AuthError):
        authenticator().authenticate(bearer(forge(header, claims, signature)))


def test_unsigned_tokens_are_rejected() -> None:
    for alg in ("none", "None", "EdDSA"):
        unsigned = forge({"alg": alg, "typ": "openshell-ext+jwt"}, forged_claims(), b"")
        with pytest.raises(AuthError):
            authenticator().authenticate(bearer(unsigned))


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(list, id="missing"),
        pytest.param(lambda: [f"Bearer {token()}", f"Bearer {token()}"], id="repeated"),
        pytest.param(lambda: [f"Basic {token()}"], id="basic"),
        pytest.param(lambda: ["Bearer "], id="empty"),
        pytest.param(lambda: [token()], id="no-scheme"),
    ],
)
def test_malformed_authorization_is_rejected(authorization: Callable[[], list[str]]) -> None:
    with pytest.raises(AuthError):
        authenticator().authenticate(authorization())


def test_evaluations_must_come_from_the_requests_sandbox() -> None:
    require_sandbox(Caller(kind="supervisor", sandbox_id="sbx-1"), "sbx-1")
    require_sandbox(Caller(kind="unverified"), "sbx-1")
    with pytest.raises(AuthError):
        require_sandbox(Caller(kind="supervisor", sandbox_id="sbx-2"), "sbx-1")
    with pytest.raises(AuthError):
        require_sandbox(Caller(kind="gateway"), "sbx-1")


def test_unverified_callers_accept_anything() -> None:
    assert UnverifiedCallers().authenticate([]) == Caller(kind="unverified")


def test_load_public_key(tmp_path: Path) -> None:
    path = tmp_path / "public.pem"
    path.write_bytes(
        KEY.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    assert load_public_key(path).public_bytes_raw() == KEY.public_key().public_bytes_raw()


def test_load_public_key_rejects_other_key_types(tmp_path: Path) -> None:
    path = tmp_path / "ec.pem"
    path.write_bytes(
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    with pytest.raises(SettingsError, match="not an Ed25519"):
        load_public_key(path)
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a key")
    with pytest.raises(SettingsError):
        load_public_key(garbage)
    with pytest.raises(SettingsError):
        load_public_key(tmp_path / "missing.pem")
