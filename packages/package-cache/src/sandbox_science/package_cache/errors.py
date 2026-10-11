# SPDX-License-Identifier: MIT
"""Exceptions raised by the package cache."""

from __future__ import annotations

from typing import ClassVar


class PackageCacheError(Exception):
    """Base class for package cache errors."""


class SettingsError(PackageCacheError):
    """The settings file is missing, malformed or inconsistent."""


class StoreError(PackageCacheError):
    """The on-disk store could not be opened, read or written."""


class UpstreamError(PackageCacheError):
    """An upstream registry did not provide a usable response.

    ``http_status`` and ``reason`` are what the cache answers when it has no
    stale copy to serve instead; the message, with details, goes to the log.
    """

    http_status: ClassVar[int] = 502
    reason: ClassVar[str] = "upstream error"


class UpstreamURLError(UpstreamError):
    """An upstream URL is not one the cache may fetch."""

    reason: ClassVar[str] = "upstream URL refused"


class UpstreamStatusError(UpstreamError):
    """Upstream answered with a status the cache does not pass through."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"upstream answered {status} for {url}")
        self.status = status


class RedirectError(UpstreamError):
    """Upstream redirected too often, or to something other than an https URL."""

    reason: ClassVar[str] = "upstream redirect refused"


class TooLargeError(UpstreamError):
    """An upstream body is larger than the configured limit."""

    reason: ClassVar[str] = "upstream file too large"


class DigestMismatchError(UpstreamError):
    """An upstream body does not have the digest the index published for it."""

    reason: ClassVar[str] = "digest mismatch"


class RewriteError(UpstreamError):
    """The adapter could not rewrite an upstream index body."""

    reason: ClassVar[str] = "upstream index cannot be rewritten"


class RateLimitedError(UpstreamError):
    """The shared upstream rate limit would delay a fetch for too long."""

    http_status: ClassVar[int] = 503
    reason: ClassVar[str] = "upstream rate limit reached, retry later"
