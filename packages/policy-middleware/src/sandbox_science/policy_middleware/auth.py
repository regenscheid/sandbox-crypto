# SPDX-License-Identifier: MIT
"""Authenticate OpenShell's calls to the middleware.

When the gateway signs extension tokens (``gateway_jwt``), every call carries
a short-lived EdDSA JWT in the ``authorization`` metadata. The gateway calls
``Describe`` and ``ValidateConfig``; each sandbox's supervisor calls the
evaluation RPCs with a token that names its sandbox, which must match the
sandbox the request claims to come from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final, Literal, Protocol, cast

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .errors import AuthError, SettingsError

TOKEN_TYPE: Final = "openshell-ext+jwt"  # noqa: S105 (a JOSE "typ" value, not a secret)
ALGORITHM: Final = "EdDSA"
_MAX_TOKEN_BYTES: Final = 8192

CallerKind = Literal["gateway", "supervisor", "unverified"]


@dataclass(frozen=True, slots=True)
class Caller:
    """Who made a call. ``unverified`` callers come from an unauthenticated registration."""

    kind: CallerKind
    sandbox_id: str | None = None


class Authenticator(Protocol):
    """Turns a call's ``authorization`` metadata into a caller."""

    def authenticate(self, authorization: Sequence[str]) -> Caller:
        """Return the caller, or raise AuthError."""
        ...


def default_audience(registration: str) -> str:
    """OpenShell's default token audience for a middleware registration."""
    return f"urn:openshell:extension:middleware:{registration}"


def load_public_key(path: Path) -> Ed25519PublicKey:
    """Load the gateway's Ed25519 signing public key (PEM, as in ``jwt/public.pem``)."""
    try:
        key = load_pem_public_key(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise SettingsError(f"cannot load the gateway public key from {path}: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SettingsError(f"{path} is not an Ed25519 public key")
    return key


class TokenAuthenticator:
    """Verify OpenShell extension tokens signed by one gateway."""

    def __init__(
        self,
        *,
        public_key: Ed25519PublicKey,
        gateway_id: str,
        audience: str,
        leeway: timedelta = timedelta(seconds=30),
    ) -> None:
        self._key = public_key
        self._issuer = f"openshell-gateway:{gateway_id}"
        self._audience = audience
        self._leeway = leeway

    def authenticate(self, authorization: Sequence[str]) -> Caller:
        """Verify the bearer token; pin the algorithm, type, issuer, audience and expiry."""
        if len(authorization) != 1:
            raise AuthError("expected exactly one authorization value")
        scheme, _, token = authorization[0].partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > _MAX_TOKEN_BYTES:
            raise AuthError("expected a bearer token")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("typ") != TOKEN_TYPE or header.get("alg") != ALGORITHM:
                raise AuthError(f"token type must be {TOKEN_TYPE} signed with {ALGORITHM}")
            claims = jwt.decode(
                token,
                key=self._key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {exc}") from exc
        return _caller(cast("Mapping[str, object]", claims), self._audience)


class UnverifiedCallers:
    """Accept every call: for a registration with ``allow_insecure_transport`` only.

    OpenShell then sends no token, so the middleware cannot tell its calls
    from anyone else's. Use it for local development on a host that nothing
    untrusted can reach.
    """

    def authenticate(self, authorization: Sequence[str]) -> Caller:
        """Return an unverified caller."""
        del authorization
        return Caller(kind="unverified")


def _caller(claims: Mapping[str, object], audience: str) -> Caller:
    # PyJWT accepts a list audience that merely contains ours; OpenShell sends
    # exactly one.
    if claims.get("aud") not in (audience, [audience]):
        raise AuthError("token audience must be exactly this middleware")
    kind = claims.get("caller_kind")
    if kind == "gateway":
        return Caller(kind="gateway")
    if kind == "supervisor":
        sandbox_id = claims.get("sandbox_id")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise AuthError("a supervisor token must name its sandbox")
        return Caller(kind="supervisor", sandbox_id=sandbox_id)
    raise AuthError(f"unknown caller kind {kind!r}")


def require_sandbox(caller: Caller, sandbox_id: str) -> None:
    """Allow a request evaluation only from the supervisor of the sandbox it names."""
    if caller.kind == "unverified":
        return
    if caller.kind != "supervisor":
        raise AuthError("only sandbox supervisors evaluate requests")
    if caller.sandbox_id != sandbox_id:
        raise AuthError("the token's sandbox does not match the request's sandbox")
