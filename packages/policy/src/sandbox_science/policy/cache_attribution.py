# SPDX-License-Identifier: MIT
"""Authenticated attribution of requests to the package cache (docs/PACKAGE-CACHE.md,
"Logging and limits").

The policy middleware names the sandbox of each request it allows to the cache
(``SANDBOX_HEADER``). With a key the two services share, it also writes
``ATTRIBUTION_HEADER``: an HMAC-SHA256 under that key of the sandbox, the method, the
request target and the time, which the cache checks, in constant time, before it trusts
the sandbox's name or serves the request. A process that reaches the cache's port without
passing through the middleware cannot make one. A captured one is good only for the same
request from the same sandbox within ``ATTRIBUTION_WINDOW``: replayed, it fetches what the
middleware already allowed that sandbox, and charges that sandbox's budget.

The middleware sees OpenShell's canonical form of a path and the cache what OpenShell
forwards, so both sides sign and check ``canonical_target``'s form of it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import stat
from pathlib import Path
from typing import Final

from .errors import PolicyError

ATTRIBUTION_HEADER: Final = "X-Sandbox-Science-Attribution"
"""Authenticates ``SANDBOX_HEADER``: ``v1:<unix seconds>:<HMAC-SHA256, hex>``. The
middleware writes it beside that header when it has the key, replacing whatever the
sandbox sent."""

ATTRIBUTION_WINDOW: Final = 300
"""Seconds an attribution is good for on either side of the cache's clock: the clock skew
allowed between the two services, and how long a captured one can be replayed."""

MIN_KEY_BYTES: Final = 32
"""The shortest key accepted, after surrounding whitespace is dropped."""

MAX_KEY_FILE: Final = 4096
"""The largest key file read."""

_VERSION: Final = "v1"
_CONTEXT: Final = b"sandbox-science-cache-attribution"
_VALUE_RE: Final = re.compile(r"v1:([0-9]{1,12}):([0-9a-f]{64})")
_ESCAPE_RE: Final = re.compile(r"%([0-9A-Fa-f]{2})")
_URL_SAFE: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@"
)
"""The bytes OpenShell leaves unescaped in a path, ``/`` and ``%`` aside
(``package_requests``)."""


class AttributionKeyError(PolicyError):
    """The shared attribution key could not be read, or is not one."""


def read_key(path: Path) -> bytes:
    """The shared key, from a private regular file: never through a symlink, nor a file
    group or others may read, as the worker's token file. Surrounding whitespace is
    dropped."""
    try:
        found = path.lstat()
        if not stat.S_ISREG(found.st_mode) or found.st_mode & 0o077:
            raise AttributionKeyError(f"the attribution key {path} must be a private regular file")
        if found.st_size > MAX_KEY_FILE:
            raise AttributionKeyError(f"the attribution key {path} is over {MAX_KEY_FILE} bytes")
        key = path.read_bytes().strip()
    except OSError as exc:
        raise AttributionKeyError(f"cannot read the attribution key {path}: {exc}") from exc
    if len(key) < MIN_KEY_BYTES:
        raise AttributionKeyError(
            f"the attribution key in {path} is shorter than {MIN_KEY_BYTES} bytes"
        )
    return key


def canonical_target(target: str) -> str:
    """``target`` (a path, and its query if any) with escapes of URL-safe bytes decoded
    and the others' hex digits in upper case, as OpenShell reports a path."""

    def escape(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _URL_SAFE else "%" + match.group(1).upper()

    return _ESCAPE_RE.sub(escape, target)


def _digest(key: bytes, sandbox: str, method: str, target: str, at: int) -> str:
    fields = (_VERSION, sandbox, method.upper(), canonical_target(target), str(at))
    message = b"\0".join(
        (_CONTEXT, *(field.encode("utf-8", "surrogateescape") for field in fields))
    )
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def sign(key: bytes, sandbox: str, method: str, target: str, now: float) -> str:
    """The ``ATTRIBUTION_HEADER`` value for one request of ``sandbox``'s at ``now``."""
    at = int(now)
    return f"{_VERSION}:{at}:{_digest(key, sandbox, method, target, at)}"


def verify(key: bytes, value: str, sandbox: str, method: str, target: str, now: float) -> bool:
    """Whether ``value`` attributes this request to ``sandbox``, made within
    ``ATTRIBUTION_WINDOW`` of ``now``."""
    match = _VALUE_RE.fullmatch(value)
    if match is None:
        return False
    at = int(match.group(1))
    if abs(now - at) > ATTRIBUTION_WINDOW:
        return False
    expected = _digest(key, sandbox, method, target, at)
    return hmac.compare_digest(expected, match.group(2))
