# SPDX-License-Identifier: MIT
"""A stand-in for Authentik's proxy provider: its signing key, its JWKS and the tokens
its outpost sends in ``X-authentik-jwt`` (forward_auth.py, D104)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from sandbox_science.workspace.forward_auth import ForwardAuthSettings, KeySet, Verifier

ISSUER = "https://auth.example.org/application/o/atlas/"
AUDIENCE = "test-client-id-atlas"
JWKS_URL = "http://authentik-server.authentik.svc/application/o/atlas/jwks/"
KID = "test-kid-provider"
SECRET = "test-secret-authentik-client-secret-0123456789abcdefghijklmnopqrstuvwxyz"
USERS = "atlas-users"
ADMINS = "atlas-admins"


class Authentik:
    """The provider: an RSA signing key published in a JWKS that ``fetch`` answers."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = KID
        self.fetches = 0
        self.down = False

    def jwks(self) -> dict[str, Any]:
        public = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    async def fetch(self, url: str) -> bytes:
        assert url == JWKS_URL
        self.fetches += 1
        if self.down:
            raise OSError("connection refused")
        return json.dumps(self.jwks()).encode()

    def token(
        self,
        user: str = "alice",
        groups: object = (USERS,),
        *,
        key: Any = None,
        kid: str | None = KID,
        algorithm: str = "RS256",
        **claims: Any,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": f"sub-{user}",
            "iat": now,
            "exp": now + 600,
            "preferred_username": user,
            "groups": list(groups) if isinstance(groups, tuple) else groups,
            **claims,
        }
        headers = {"kid": kid} if kid is not None else {}
        signer = (SECRET if algorithm == "HS256" else self.key) if key is None else key
        return jwt.encode(payload, signer, algorithm=algorithm, headers=headers)

    def verifier(
        self, admin_group: str | None = None, *, secret_file: Path | None = None, **keys: Any
    ) -> Verifier:
        """RS256 with the JWKS by default; HS256 with ``secret_file``, which holds
        ``SECRET`` unless the test wrote another."""
        common: dict[str, Any] = {"issuer": ISSUER, "audience": AUDIENCE, "user_group": USERS}
        if secret_file is not None:
            settings = ForwardAuthSettings(
                algorithm="HS256", secret_file=secret_file, admin_group=admin_group, **common
            )
            return Verifier(settings)
        settings = ForwardAuthSettings(
            algorithm="RS256", jwks_url=JWKS_URL, admin_group=admin_group, **common
        )
        return Verifier(settings, keys=KeySet(JWKS_URL, fetch=self.fetch, **keys))


def secret_file(directory: Path, value: str = SECRET, mode: int = 0o600) -> Path:
    """The provider's client secret as the workspace's Secret projects it."""
    path = directory / "authentik-client-secret"
    path.write_text(value)
    path.chmod(mode)
    return path


def as_user(token: str) -> dict[str, str]:
    """The headers of a request Authentik admitted: its token, as the outpost sends it."""
    return {"X-authentik-jwt": token}
