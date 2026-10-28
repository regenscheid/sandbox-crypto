# SPDX-License-Identifier: MIT
"""Login through Authentik's forward auth (D104): only a token the provider's key signed,
for the configured issuer, audience and user group, opens a session; the plain headers
never do, and the login link keeps working as the operator's own login."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import jwt
import pytest
from aiohttp.test_utils import TestClient
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from sandbox_science.workspace import forward_auth
from sandbox_science.workspace.app import SESSION_COOKIE
from sandbox_science.workspace.forward_auth import (
    ForwardAuthSettings,
    KeySet,
    TokenRefusedError,
    parse_keys,
)

from .authentik_support import ADMINS, AUDIENCE, ISSUER, JWKS_URL, USERS, Authentik, as_user
from .route_support import log_in, origin, workspace


ClientFactory = Callable[..., Awaitable[TestClient[Any, Any]]]
PROJECTS = "/api/v0/projects"


@pytest.fixture
def authentik() -> Authentik:
    return Authentik()


async def forwarded(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    authentik: Authentik,
    admin_group: str | None = None,
) -> TestClient[Any, Any]:
    return await workspace(
        aiohttp_client,
        state_dir,
        static_dir,
        login=False,
        forward_auth=authentik.verifier(admin_group),
    )


def cookie(client: TestClient[Any, Any]) -> str:
    return f"{SESSION_COOKIE}-{client.port}"


async def me(client: TestClient[Any, Any], headers: dict[str, str] | None = None) -> Any:
    response = await client.get("/api/v0/me", headers=headers or {})
    assert response.status == 200, await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    return await response.json()


@pytest.mark.asyncio
async def test_a_valid_token_opens_a_session_named_by_its_user(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, authentik: Authentik
) -> None:
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik)
    token = authentik.token("alice")
    response = await client.get(PROJECTS, headers=as_user(token))
    assert response.status == 200, await response.text()
    session = response.cookies[cookie(client)]
    assert session["httponly"] is True
    assert session["samesite"] == "Strict"
    assert session["path"] == "/"
    assert token not in session.value
    # Without an admin group every admitted user is an admin, as every login was.
    assert await me(client, as_user(token)) == {
        "user": "alice",
        "login": "forward",
        "admin": True,
        "admin_group": None,
    }
    # The session stands for that token: the same token opens no other.
    again = await client.get(PROJECTS, headers=as_user(token))
    assert again.status == 200
    assert cookie(client) not in again.cookies
    # Without the token, the cookie alone is the session (a port-forward, say).
    assert (await client.get(PROJECTS)).status == 200
    assert authentik.fetches == 1


@pytest.mark.asyncio
async def test_the_admin_group_decides_admin(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, authentik: Authentik
) -> None:
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik, ADMINS)
    member = await me(client, as_user(authentik.token("bob")))
    assert (member["user"], member["admin"], member["admin_group"]) == ("bob", False, ADMINS)
    admin = await me(client, as_user(authentik.token("carol", (USERS, ADMINS))))
    assert (admin["user"], admin["admin"]) == ("carol", True)


@pytest.mark.asyncio
async def test_another_token_replaces_the_session(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, authentik: Authentik
) -> None:
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik, ADMINS)
    first = await client.get(PROJECTS, headers=as_user(authentik.token("bob")))
    old = first.cookies[cookie(client)].value
    # Another login in the same browser: the session is the new token's, not bob's.
    carol = authentik.token("carol", (USERS, ADMINS))
    assert (await me(client, as_user(carol)))["user"] == "carol"
    assert (await client.get(PROJECTS, cookies={cookie(client): old})).status == 401
    # A new token that is refused ends the session it would have replaced.
    refused = await client.get(PROJECTS, headers=as_user(authentik.token("dave", ())))
    assert refused.status == 401
    assert (await client.get(PROJECTS)).status == 401


@pytest.mark.asyncio
async def test_a_session_ends_when_its_token_expires(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, authentik: Authentik
) -> None:
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik)
    now = int(time.time())
    token = authentik.token(exp=now + 2)
    assert (await client.get(PROJECTS, headers=as_user(token))).status == 200
    await asyncio.sleep(2.1)
    assert (await client.get(PROJECTS)).status == 401


def _other_rsa() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda a: a.token(key=_other_rsa()), "signature"),
        (lambda a: a.token(iss="https://auth.example.org/application/o/other/"), "issuer"),
        (lambda a: a.token(aud="another-client"), "audience"),
        (lambda a: a.token(exp=int(time.time()) - 120), "expired"),
        (lambda a: a.token(groups=("someone-else",)), "not in the group"),
        (lambda a: a.token(groups=()), "not in the group"),
        (lambda a: a.token(kid="rotated-away"), "signing key is not one"),
        (lambda a: a.token(kid=None), "names no signing key"),
        (
            lambda a: a.token(key="test-secret-client-secret-0123456789abcdef", algorithm="HS256"),
            "HS256",
        ),
        (lambda a: a.token(key=None, algorithm="none"), "public-key signature"),
        (lambda a: a.token(preferred_username=""), "username"),
        (lambda a: a.token(preferred_username="eve\nadmin"), "username"),
        (lambda a: a.token(groups="atlas-users"), "groups"),
        (lambda a: "not.a.jwt", "not a JWT"),
        (lambda a: "x" * (forward_auth.MAX_TOKEN + 1), "too long"),
    ],
    ids=[
        "bad-signature",
        "wrong-issuer",
        "wrong-audience",
        "expired",
        "missing-group",
        "no-groups",
        "unknown-kid",
        "no-kid",
        "hs256",
        "alg-none",
        "empty-user",
        "control-char-user",
        "groups-not-a-list",
        "garbage",
        "huge",
    ],
)
@pytest.mark.asyncio
async def test_a_token_that_is_not_believed_opens_nothing(
    aiohttp_client: ClientFactory,
    state_dir: Path,
    static_dir: Path,
    authentik: Authentik,
    make: Callable[[Authentik], str],
    reason: str,
) -> None:
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik)
    token = _none_token() if reason == "public-key signature" else make(authentik)
    response = await client.get(PROJECTS, headers=as_user(token))
    assert response.status == 401
    assert not response.cookies
    body = await response.json()
    assert body["error"]["kind"] == "policy_denied"
    assert reason in body["error"]["message"]
    assert token not in body["error"]["message"]


def _none_token() -> str:
    return jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "preferred_username": "mallory", "groups": [USERS]},
        None,
        algorithm="none",
        headers={"kid": "test-kid-provider"},
    )


@pytest.mark.asyncio
async def test_unreachable_keys_refuse(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, authentik: Authentik
) -> None:
    authentik.down = True
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik)
    response = await client.get(PROJECTS, headers=as_user(authentik.token()))
    assert response.status == 401
    assert "could not be fetched" in (await response.json())["error"]["message"]
    assert not response.cookies


@pytest.mark.asyncio
async def test_the_plain_authentik_headers_open_nothing(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, authentik: Authentik
) -> None:
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik)
    forged = {
        "X-authentik-username": "alice",
        "X-authentik-groups": f"{USERS}|{ADMINS}",
        "X-authentik-uid": "sub-alice",
        "X-authentik-meta-jwks": "https://attacker.invalid/jwks/",
    }
    response = await client.get(PROJECTS, headers=forged)
    assert response.status == 401
    assert not response.cookies
    assert authentik.fetches == 0


@pytest.mark.asyncio
async def test_two_tokens_in_one_request_open_nothing(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, authentik: Authentik
) -> None:
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik)
    token = authentik.token()
    response = await client.get(
        PROJECTS, headers=[("X-authentik-jwt", token), ("X-authentik-jwt", token)]
    )
    assert response.status == 401


@pytest.mark.asyncio
async def test_the_login_link_still_works_and_is_the_operators(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, authentik: Authentik
) -> None:
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik, ADMINS)
    assert (await client.get(PROJECTS)).status == 401
    await log_in(client)
    assert await me(client) == {
        "user": "user",
        "login": "token",
        "admin": True,
        "admin_group": ADMINS,
    }
    # An Authentik token in the same browser leaves the operator's session as it is.
    response = await client.get("/api/v0/me", headers=as_user(authentik.token("bob")))
    assert (await response.json())["login"] == "token"
    assert cookie(client) not in response.cookies


@pytest.mark.asyncio
async def test_logging_out_ends_an_authentik_session(
    aiohttp_client: ClientFactory, state_dir: Path, static_dir: Path, authentik: Authentik
) -> None:
    client = await forwarded(aiohttp_client, state_dir, static_dir, authentik)
    token = authentik.token()
    assert (await client.get(PROJECTS, headers=as_user(token))).status == 200
    response = await client.post("/api/v0/logout", headers=origin(client))
    assert response.status == 204
    assert (await client.get(PROJECTS)).status == 401
    # Logging out with the token on the request opens nothing that outlives the answer.
    response = await client.post("/api/v0/logout", headers={**origin(client), **as_user(token)})
    assert response.status == 204
    assert cookie(client) not in response.cookies or not response.cookies[cookie(client)].value
    # Changes still need the workspace's own Origin, whatever the token.
    refused = await client.post("/api/v0/logout", headers=as_user(token))
    assert refused.status == 403


@pytest.mark.asyncio
async def test_keys_refresh_within_bounds() -> None:
    authentik = Authentik()
    now = [1000.0]
    keys = KeySet(JWKS_URL, fetch=authentik.fetch, clock=lambda: now[0])
    assert (await keys.key(authentik.kid)).algorithm == "RS256"
    assert authentik.fetches == 1
    # An unknown kid asks for the keys again, at most once per retry interval.
    for _ in range(5):
        with pytest.raises(TokenRefusedError, match="not one of the provider's"):
            await keys.key("unknown")
    assert authentik.fetches == 1
    now[0] += forward_auth.RETRY_SECONDS
    for _ in range(5):
        with pytest.raises(TokenRefusedError):
            await keys.key("unknown")
    assert authentik.fetches == 2
    # Fresh keys outlive a failed fetch; stale ones do not.
    authentik.down = True
    now[0] += forward_auth.RETRY_SECONDS
    with pytest.raises(TokenRefusedError, match="not one of the provider's"):
        await keys.key("unknown")
    await keys.key(authentik.kid)
    now[0] += forward_auth.REFRESH_SECONDS
    with pytest.raises(TokenRefusedError, match="could not be fetched"):
        await keys.key(authentik.kid)
    # Back up: the next fetch, after the retry interval, brings them back.
    authentik.down = False
    with pytest.raises(TokenRefusedError, match="could not be fetched"):
        await keys.key(authentik.kid)  # within the retry interval of the failed fetch
    now[0] += forward_auth.RETRY_SECONDS
    await keys.key(authentik.kid)


def test_the_jwks_keeps_only_signing_keys() -> None:
    authentik = Authentik()
    rsa_key = authentik.jwks()["keys"][0]
    ec_key = jwt.algorithms.ECAlgorithm.to_jwk(
        ec.generate_private_key(ec.SECP256R1()).public_key(), as_dict=True
    )
    body = {
        "keys": [
            rsa_key,
            {**ec_key, "kid": "ec", "alg": "ES256"},
            {**rsa_key, "kid": "enc", "use": "enc"},
            {**rsa_key, "kid": "hmac", "alg": "HS256"},
            {"kty": "oct", "kid": "secret", "k": "c2VjcmV0"},
            {**rsa_key, "kid": ""},
            "not a key",
        ]
    }
    assert sorted(parse_keys(json.dumps(body).encode())) == ["ec", authentik.kid]
    for bad in (b"[]", b"{}", b"not json", b'{"keys": {}}'):
        with pytest.raises(ValueError, match="JWKS"):
            parse_keys(bad)


def test_settings_are_checked() -> None:
    good = {"jwks_url": JWKS_URL, "issuer": ISSUER, "audience": AUDIENCE, "user_group": USERS}
    ForwardAuthSettings(**good)
    for bad in (
        {"jwks_url": "file:///etc/keys"},
        {"jwks_url": "https://user:pass@auth.example.org/jwks/"},
        {"issuer": ""},
        {"audience": ""},
        {"user_group": ""},
        {"admin_group": " padded "},
        {"header": "X-Bad Header"},
    ):
        with pytest.raises(ValueError):
            ForwardAuthSettings(**{**good, **bad})
