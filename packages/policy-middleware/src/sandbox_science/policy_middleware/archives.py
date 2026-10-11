# SPDX-License-Identifier: MIT
"""Name the opam packages an archive belongs to, by asking the package cache.

opam fetches archives by digest, so a request for one names no package. The
package cache keeps a table from digests to packages, built from the opam index
it holds (``GET /-/opam/archives/<algorithm>/<hex>``, docs/PACKAGE-CACHE.md).
The middleware asks it when it records an escalation or alert for an opam
archive, so that whoever answers the escalation sees which package it is.

The answer only informs: it never changes a decision. A lookup that fails or
takes longer than ``LOOKUP_TIMEOUT`` is left out of the record.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import urllib.request
from http.client import HTTPMessage
from typing import IO, Final, Protocol, cast

_log = logging.getLogger(__name__)

LOOKUP_TIMEOUT: Final = 0.05
"""Seconds a lookup may wait for each step (connecting, each read). OpenShell gives the
whole evaluation 500 ms, and appending the record may take up to ``events.STORE_TIMEOUT``;
the cache answers from memory in a few milliseconds."""

MAX_PACKAGES: Final = 100
"""Packages kept in a record; opam-repository's most shared archive belongs to 108."""

_MAX_BODY: Final = 64 * 1024
_PACKAGE_ID_RE: Final = re.compile(r"[0-9_+-]*[A-Za-z][A-Za-z0-9_+-]*\.[A-Za-z0-9_+.~-]+", re.ASCII)


class ArchiveLookup(Protocol):
    """Finds the opam packages whose opam files list an archive digest."""

    def packages(self, digest: str) -> tuple[str, ...] | None:
        """The package ids for ``<algorithm>:<hex>``, or None if they cannot be found now."""
        ...


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


class CacheArchiveLookup:
    """Ask the package cache at ``base_url`` (as this host reaches it, without a path)."""

    def __init__(self, base_url: str, *, timeout: float = LOOKUP_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # Never through a proxy from the environment, and nowhere but the cache.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())

    def packages(self, digest: str) -> tuple[str, ...] | None:
        """The package ids the cache lists for ``digest`` (``<algorithm>:<hex>``), sorted."""
        algorithm, _, hex_digest = digest.partition(":")
        url = f"{self._base_url}/-/opam/archives/{algorithm}/{hex_digest}"
        try:
            with self._opener.open(url, timeout=self._timeout) as response:
                body = response.read(_MAX_BODY + 1)
        # URLError and timeouts are OSErrors; a malformed answer can raise HTTPException.
        except (OSError, ValueError, http.client.HTTPException) as exc:
            _log.info("cannot ask the package cache about opam archive %s: %s", digest, exc)
            return None
        packages = _parse(body, digest)
        if packages is None:
            _log.warning("the package cache answered about opam archive %s unexpectedly", digest)
        return packages


def _parse(body: bytes, digest: str) -> tuple[str, ...] | None:
    """The package ids in the cache's answer for ``digest``, or None if it is malformed."""
    if len(body) > _MAX_BODY:
        return None
    try:
        answer = json.loads(body)
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    fields = cast("dict[str, object]", answer)
    packages = fields.get("packages")
    if fields.get("digest") != digest or not isinstance(packages, list):
        return None
    names = cast("list[object]", packages)
    if not all(isinstance(name, str) and _PACKAGE_ID_RE.fullmatch(name) for name in names):
        return None
    return tuple(sorted(cast("list[str]", names)))
