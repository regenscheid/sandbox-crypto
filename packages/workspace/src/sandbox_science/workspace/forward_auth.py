# SPDX-License-Identifier: MIT
"""Login through Authentik's forward auth (D104): the signed token its proxy outpost sends.

Behind Traefik with Authentik's forward auth, every request that reaches the workspace
was admitted by Authentik, and its proxy outpost adds headers that say who the user is.
``X-authentik-username`` and ``X-authentik-groups`` are plain text that anything able to
reach the workspace's port could send, so they are never read. The outpost also sends
``X-authentik-jwt``: the OAuth access token Authentik issued to the outpost for this
user, a JWT that the provider's signing key signs. ``Verifier`` believes a token only
when all of these hold:

- it is at most ``MAX_TOKEN`` characters and its header names a ``kid`` and an
  asymmetric algorithm (``ALGORITHMS``). ``HS256``, which Authentik uses when the
  provider has no signing key, is a MAC under the provider's client secret, which the
  workspace never holds, and it is refused;
- the key with that ``kid`` is in the provider's JWKS, fetched from the configured URL
  (``KeySet``), and the key's own ``alg``, if it names one, is the header's;
- the signature verifies, ``iss`` is exactly the configured issuer, ``aud`` names the
  configured audience (the provider's client ID), and ``exp`` has not passed, with
  ``LEEWAY_SECONDS`` of leeway (also for ``iat`` and ``nbf``);
- ``preferred_username`` is a non-empty name without control characters, and ``groups``
  is a list of names that contains the configured user group.

The admin group, when configured, decides ``admin``; without one every admitted user is
an admin, as every logged-in user was before (app.py). Anything else is a refusal, with a
reason the guard logs and shows; never a fallback to the plain headers.

The JWKS is fetched without redirects, proxies or cookies, at most ``MAX_JWKS_BYTES``,
within ``FETCH_SECONDS``. Keys are used for ``REFRESH_SECONDS`` after a fetch; a token
whose ``kid`` is unknown, or keys older than that, ask for a new fetch, at most once per
``RETRY_SECONDS`` however many requests ask. A failed fetch keeps keys that are still
fresh, and otherwise leaves none: a token is then refused, never accepted unchecked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import urlsplit

import aiohttp
import jwt
from jwt import PyJWK

_log = logging.getLogger(__name__)

HEADER: Final = "X-authentik-jwt"
"""The header Authentik's proxy outpost puts the token in."""
ALGORITHMS: Final = frozenset(
    ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
)
"""Signature algorithms a token may name: public-key ones only, never a MAC."""
MAX_TOKEN: Final = 8000
"""Below aiohttp's limit on a header line, which answers 400 before the guard."""
MAX_JWKS_BYTES: Final = 64 * 1024
MAX_KEYS: Final = 32
MAX_USER: Final = 150
"""Authentik's own limit on a username."""
MAX_GROUPS: Final = 512
LEEWAY_SECONDS: Final = 30.0
REFRESH_SECONDS: Final = 900.0
RETRY_SECONDS: Final = 10.0
FETCH_SECONDS: Final = 5.0

Fetch = Callable[[str], Awaitable[bytes]]


class TokenRefusedError(Exception):
    """A token that is not believed; the message says why, without the token."""


@dataclass(frozen=True)
class ForwardAuthSettings:
    """``serve``'s ``--forward-auth-*`` options (cli.py)."""

    jwks_url: str
    issuer: str
    audience: str
    user_group: str
    admin_group: str | None = None
    header: str = HEADER

    def __post_init__(self) -> None:
        parts = urlsplit(self.jwks_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the JWKS URL must be an http or https URL")
        if parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError("the JWKS URL carries no credentials or fragment")
        if not self.issuer or not self.audience:
            raise ValueError("forward auth needs the issuer and the audience")
        for group in (self.user_group, self.admin_group):
            if group is not None and not _name(group):
                raise ValueError("a group is a non-empty name without control characters")
        if not self.header or not all(
            char.isascii() and (char.isalnum() or char == "-") for char in self.header
        ):
            raise ValueError("the header name is letters, digits and hyphens")


@dataclass(frozen=True)
class SigningKey:
    """A key from the JWKS, with the ``alg`` the JWKS gave it, if any."""

    key: PyJWK
    algorithm: str | None


@dataclass(frozen=True)
class Claimed:
    """Who a believed token names."""

    user: str
    admin: bool
    expires: float
    """When the token expires, in seconds since the epoch."""


def _name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_USER
        and value == value.strip()
        and not any(unicodedata.category(char).startswith("C") for char in value)
    )


async def fetch_url(url: str) -> bytes:
    """GET ``url`` once: no redirect, no proxy from the environment, a bounded answer."""
    timeout = aiohttp.ClientTimeout(total=FETCH_SECONDS)
    async with (
        aiohttp.ClientSession(timeout=timeout, trust_env=False) as session,
        session.get(url, allow_redirects=False, headers={"Accept": "application/json"}) as answer,
    ):
        if answer.status != 200:
            raise OSError(f"the JWKS URL answered {answer.status}")
        body = await answer.content.read(MAX_JWKS_BYTES + 1)
        if len(body) > MAX_JWKS_BYTES:
            raise OSError("the JWKS is larger than the workspace reads")
        return body


def parse_keys(body: bytes) -> dict[str, SigningKey]:
    """A JWKS's signing keys by ``kid``: RSA and EC keys only; others are skipped."""
    try:
        found = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("the JWKS is not JSON") from error
    listed = cast(dict[str, object], found).get("keys") if isinstance(found, dict) else None
    if not isinstance(listed, list):
        raise ValueError("the JWKS has no list of keys")
    keys: dict[str, SigningKey] = {}
    for item in cast(list[object], listed)[:MAX_KEYS]:
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, object], item)
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid or kid in keys:
            continue
        if entry.get("kty") not in ("RSA", "EC") or entry.get("use", "sig") != "sig":
            continue
        named = entry.get("alg")
        if named is not None and named not in ALGORITHMS:
            continue
        try:
            keys[kid] = SigningKey(PyJWK(entry), named if isinstance(named, str) else None)
        except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError):
            continue
    return keys


class KeySet:
    """The provider's public keys, from its JWKS URL, cached with a bounded refresh."""

    def __init__(
        self, url: str, *, fetch: Fetch = fetch_url, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.url = url
        self._fetch = fetch
        self._clock = clock
        self._keys: dict[str, SigningKey] = {}
        self._fetched: float | None = None
        self._tried: float | None = None
        self._lock = asyncio.Lock()

    def _fresh(self, now: float) -> bool:
        return self._fetched is not None and now - self._fetched < REFRESH_SECONDS

    async def key(self, kid: str) -> SigningKey:
        """The key named ``kid``; TokenRefusedError when the keys are unknown or stale."""
        async with self._lock:
            now = self._clock()
            fresh = self._fresh(now)
            wanted = not fresh or kid not in self._keys
            if wanted and (self._tried is None or now - self._tried >= RETRY_SECONDS):
                self._tried = now
                try:
                    keys = parse_keys(await self._fetch(self.url))
                except (OSError, TimeoutError, ValueError, aiohttp.ClientError) as error:
                    _log.warning("Authentik's JWKS could not be fetched: %s", error)
                    if not fresh:
                        self._keys, self._fetched = {}, None
                else:
                    self._keys, self._fetched = keys, now
            if not self._fresh(self._clock()):
                raise TokenRefusedError("Authentik's signing keys could not be fetched")
            found = self._keys.get(kid)
            if found is None:
                raise TokenRefusedError("the token's signing key is not one of the provider's")
            return found


class Verifier:
    """Believes Authentik's forward-auth token only as the module's docstring says."""

    def __init__(self, settings: ForwardAuthSettings, *, keys: KeySet | None = None) -> None:
        self.settings = settings
        self.keys = keys if keys is not None else KeySet(settings.jwks_url)

    @property
    def header(self) -> str:
        return self.settings.header

    async def verify(self, token: str) -> Claimed:
        settings = self.settings
        if not token or len(token) > MAX_TOKEN:
            raise TokenRefusedError("the token is empty or too long")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenRefusedError("the token is not a JWT") from None
        algorithm, kid = header.get("alg"), header.get("kid")
        if algorithm == "HS256":
            raise TokenRefusedError(
                "the token is signed with the provider's client secret (HS256): "
                "the Authentik provider needs a signing key"
            )
        if not isinstance(algorithm, str) or algorithm not in ALGORITHMS:
            raise TokenRefusedError("the token's algorithm is not a public-key signature")
        if not isinstance(kid, str) or not kid:
            raise TokenRefusedError("the token names no signing key")
        found = await self.keys.key(kid)
        if found.algorithm is not None and found.algorithm != algorithm:
            raise TokenRefusedError("the token's algorithm is not its key's")
        try:
            claims = jwt.decode(
                token,
                key=found.key.key,
                algorithms=[algorithm],
                audience=settings.audience,
                issuer=settings.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenRefusedError("the token has expired") from None
        except jwt.InvalidIssuerError:
            raise TokenRefusedError("the token's issuer is not the configured one") from None
        except jwt.InvalidAudienceError:
            raise TokenRefusedError("the token is not for the configured audience") from None
        except (jwt.PyJWTError, ValueError, TypeError):
            raise TokenRefusedError("the token's signature or claims are not valid") from None
        return self._claimed(cast(Mapping[str, object], claims))

    def _claimed(self, claims: Mapping[str, object]) -> Claimed:
        settings = self.settings
        user = claims.get("preferred_username")
        if not _name(user):
            raise TokenRefusedError("the token names no usable username")
        groups = claims.get("groups")
        if not isinstance(groups, list):
            raise TokenRefusedError("the token lists no groups")
        names = cast(list[object], groups)
        if len(names) > MAX_GROUPS or not all(isinstance(name, str) for name in names):
            raise TokenRefusedError("the token's groups are not a list of names")
        if settings.user_group not in names:
            raise TokenRefusedError(f"the user is not in the group {settings.user_group}")
        expires = claims.get("exp")
        if not isinstance(expires, int | float) or isinstance(expires, bool):
            raise TokenRefusedError("the token's expiry is not a time")
        admin = settings.admin_group is None or settings.admin_group in names
        return Claimed(user=cast(str, user), admin=admin, expires=float(expires))
