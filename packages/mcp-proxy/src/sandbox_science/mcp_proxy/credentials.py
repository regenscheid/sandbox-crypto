# SPDX-License-Identifier: MIT
"""Credential headers for upstream requests, from OpenShell placeholders only.

In its sandbox the proxy holds placeholders, never credentials: OpenShell
exposes each provider credential as an environment variable whose value is a
placeholder, and substitutes the real value when a request leaves, in headers
only (docs/MCP-CONNECTORS.md; lab 2). So credentials travel only as headers,
and a value that is not a placeholder is refused rather than sent.

The settings' ``[credentials]`` table names the variable for each provider
(``bearer`` and ``oauth`` servers) and for each ``${provider:NAME.KEY}``
reference (``headers`` servers).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from sandbox_science.policy.model import BearerAuth, HeadersAuth, McpAuth, NoAuth, OAuthAuth

from .errors import RefusalError
from .reasons import CREDENTIAL_UNAVAILABLE

PLACEHOLDER_MARK: Final = b"openshell:resolve:"
"""What every OpenShell placeholder starts with."""

_PLACEHOLDER_RE: Final = re.compile(r"openshell:resolve:env:[\x21-\x7e]{1,512}")
_REFERENCE_RE: Final = re.compile(
    r"\$\{provider:([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[A-Z][A-Z0-9_]*)\}"
)
_LITERAL_RE: Final = re.compile(r"[\x20-\x7e]*")
RESERVED_HEADERS: Final = frozenset(
    {
        "accept",
        "accept-encoding",
        "connection",
        "content-encoding",
        "content-length",
        "content-type",
        "host",
        "last-event-id",
        "mcp-protocol-version",
        "mcp-session-id",
        "te",
        "transfer-encoding",
        "upgrade",
        "user-agent",
    }
)
"""Headers the proxy sets itself, which a ``headers`` server entry may not name."""

Headers = tuple[tuple[str, str], ...]


def contains_placeholder(body: bytes) -> bool:
    """Whether a body carries something that reads as an OpenShell placeholder, in any case."""
    return PLACEHOLDER_MARK in body.lower()


def credential_headers(
    auth: McpAuth, variables: Mapping[str, str], environ: Mapping[str, str]
) -> Headers:
    """The headers that carry a server's credentials; RefusalError if one is unavailable."""
    match auth:
        case NoAuth():
            return ()
        case OAuthAuth() | BearerAuth():
            return (("Authorization", f"Bearer {_placeholder(auth.provider, variables, environ)}"),)
        case HeadersAuth():
            headers: list[tuple[str, str]] = []
            for name, value in auth.headers.items():
                if name.lower() in RESERVED_HEADERS:
                    raise RefusalError(CREDENTIAL_UNAVAILABLE, f"header {name} is set by the proxy")
                if contains_placeholder(value.encode()):
                    # Placeholders come from the environment, never from the policy's text.
                    raise RefusalError(
                        CREDENTIAL_UNAVAILABLE, f"header {name} holds a literal placeholder"
                    )
                rendered = _REFERENCE_RE.sub(
                    lambda match: _placeholder(match[1], variables, environ), value
                )
                if not _LITERAL_RE.fullmatch(rendered):
                    raise RefusalError(
                        CREDENTIAL_UNAVAILABLE, f"header {name} is not printable ASCII"
                    )
                headers.append((name, rendered))
            return tuple(headers)


def _placeholder(key: str, variables: Mapping[str, str], environ: Mapping[str, str]) -> str:
    variable = variables.get(key)
    if variable is None:
        raise RefusalError(CREDENTIAL_UNAVAILABLE, f"no [credentials] entry for {key}")
    value = environ.get(variable)
    if value is None:
        raise RefusalError(CREDENTIAL_UNAVAILABLE, f"environment variable {variable} is not set")
    if not _PLACEHOLDER_RE.fullmatch(value):
        # Never send a credential the proxy holds itself (D67: placeholders only).
        raise RefusalError(
            CREDENTIAL_UNAVAILABLE,
            f"environment variable {variable} does not hold an OpenShell placeholder",
        )
    return value
