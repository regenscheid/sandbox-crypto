# SPDX-License-Identifier: MIT
"""Fetching from the upstream registries.

``UpstreamClient.fetch`` performs one upstream GET for a ``Fetch``:

- the URL must lie below one of the configured upstream base URLs; upstream
  may redirect (at most five times) to other ``https://`` URLs only;
- only the adapter's ``Fetch.headers`` are sent, plus a User-Agent,
  ``Accept-Encoding: identity`` for artifacts and the stored validators when
  revalidating; client headers never reach upstream;
- the body streams to a temp file in the store while SHA-256 (the blob name)
  and the expected digest's algorithm are computed, and the download is
  abandoned as soon as ``Content-Length`` or the bytes received exceed the
  limit; artifacts are stored exactly as sent, while index data may arrive
  compressed and is decoded (and measured after decoding);
- every request takes a token from one ``TokenBucket`` shared by all clients.

``SingleFlight`` lets concurrent requests for the same key share one fetch.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import ssl
import tempfile
import time
from collections.abc import Awaitable, Callable, Collection, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final
from urllib.parse import unquote

import aiohttp
from multidict import CIMultiDict
from yarl import URL

from .adapter import Digest, Fetch, Kind
from .errors import (
    DigestMismatchError,
    RateLimitedError,
    RedirectError,
    StoreError,
    TooLargeError,
    UpstreamError,
    UpstreamStatusError,
    UpstreamURLError,
)
from .store import TempBlob

USER_AGENT: Final = "sandbox-science-package-cache/0.1"
MAX_REDIRECTS: Final = 5
NEGATIVE_BODY_LIMIT: Final = 64 * 1024
"""Bytes of a 404 or 410 body that are kept; the rest is dropped."""
DEFAULT_TIMEOUT: Final = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
"""No overall limit, since artifacts can be large, but no stalls longer than a minute."""
DEFAULT_MAX_WAIT: Final = 10.0
"""Seconds a fetch may wait for the rate limit before it fails with 503."""

_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_NEGATIVE_STATUSES: Final = frozenset({404, 410})
_CHUNK: Final = 256 * 1024
_FLUSH: Final = 1024 * 1024
_MAX_HEADER_VALUE: Final = 1024
_FALLBACK_CONTENT_TYPE: Final = "application/octet-stream"
_PATH_CHARACTERS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@%/"
)


class TokenBucket:
    """A token bucket: ``rate`` tokens per second, holding at most ``burst``.

    ``acquire`` reserves a token and sleeps until it is due. When the wait
    would exceed ``max_wait`` it raises RateLimitedError instead of queueing
    without bound.
    """

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        max_wait: float = DEFAULT_MAX_WAIT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0 or burst < 1 or max_wait < 0:
            raise ValueError("rate must be positive, burst at least 1 and max_wait not negative")
        self._rate = rate
        self._burst = float(burst)
        self._max_wait = max_wait
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._updated = clock()

    async def acquire(self) -> None:
        """Take one token, waiting for it if need be."""
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + max(now - self._updated, 0.0) * self._rate)
        self._updated = now
        self._tokens -= 1
        if self._tokens >= 0:
            return
        wait = -self._tokens / self._rate
        if wait > self._max_wait:
            self._tokens += 1
            raise RateLimitedError(f"the upstream rate limit would delay this fetch by {wait:.1f}s")
        try:
            await self._sleep(wait)
        except asyncio.CancelledError:
            self._tokens += 1
            raise


class SingleFlight[T]:
    """Run at most one operation per key; concurrent callers share its result.

    The operation runs in its own task, so a caller that is cancelled (a
    client that disconnects) does not cancel it for the others, and its
    result still lands in the cache.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[T]] = {}

    def __len__(self) -> int:
        return len(self._tasks)

    async def run(self, key: str, operation: Callable[[], Coroutine[object, object, T]]) -> T:
        """Return the result of the running operation for ``key``, starting it if none is."""
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(operation())
            self._tasks[key] = task
            task.add_done_callback(functools.partial(self._finished, key))
        return await asyncio.shield(task)

    def _finished(self, key: str, task: asyncio.Task[T]) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if not task.cancelled():
            # Mark the exception retrieved; callers still waiting receive it.
            task.exception()

    async def close(self) -> None:
        """Cancel the running operations and wait for them to finish."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(frozen=True, slots=True)
class Validators:
    """A stored response's validators, for a conditional request."""

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class Downloaded:
    """A complete upstream body, in a temp file of the store."""

    status: int
    """200, or 404 or 410 (whose body is cut at ``NEGATIVE_BODY_LIMIT``)."""
    blob: TempBlob
    content_type: str
    etag: str | None = None
    last_modified: str | None = None
    digest: str | None = None
    """The expected digest the body was verified against, as ``<algorithm>:<hex>``."""


@dataclass(frozen=True, slots=True)
class NotModified:
    """Upstream confirmed the stored body (304), with its current validators if it sent any."""

    etag: str | None = None
    last_modified: str | None = None


UpstreamResult = Downloaded | NotModified


def create_session(
    *,
    ssl_context: ssl.SSLContext | None = None,
    connector: aiohttp.BaseConnector | None = None,
    timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
) -> aiohttp.ClientSession:
    """Create the session for upstream requests; call it with the event loop running.

    It ignores proxy environment variables and never stores cookies. Pass
    ``ssl_context`` to trust other certificate authorities (tests), or a whole
    ``connector``; the session owns and closes either.
    """
    if connector is not None and ssl_context is not None:
        raise ValueError("pass ssl_context or connector, not both")
    if connector is None:
        connector = aiohttp.TCPConnector(
            ssl=ssl_context if ssl_context is not None else True, ttl_dns_cache=300
        )
    return aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        auto_decompress=True,
    )


def check_url(url: str | URL, *, allowed: Collection[str] | None = None) -> URL:
    """Return ``url`` (left exactly as encoded) if the cache may request it.

    It must be an ``https://`` URL with a host, no credentials or fragment,
    only URL characters and no ``.`` or ``..`` path segments, encoded or not.
    With ``allowed``, it must also lie below one of those base URLs.
    """
    text = str(url)
    if not text.isascii() or any(not char.isprintable() or char.isspace() for char in text):
        raise UpstreamURLError(f"refusing upstream URL {text!r}: not printable ASCII")
    try:
        parsed = url if isinstance(url, URL) else URL(text, encoded=True)
    except ValueError as exc:
        raise UpstreamURLError(f"refusing upstream URL {text!r}: {exc}") from exc
    if parsed.scheme != "https" or not parsed.raw_host:
        raise UpstreamURLError(f"refusing upstream URL {text!r}: not an absolute https URL")
    if parsed.raw_user is not None or parsed.raw_password is not None or parsed.raw_fragment:
        raise UpstreamURLError(f"refusing upstream URL {text!r}: credentials or fragment")
    raw_path = parsed.raw_path
    if not set(raw_path) <= _PATH_CHARACTERS or any(
        part in (".", "..")
        for segment in raw_path.split("/")
        for part in unquote(segment).replace("\\", "/").split("/")
    ):
        raise UpstreamURLError(f"refusing upstream URL {text!r}: unsafe path")
    if allowed is not None:
        origin = f"https://{parsed.raw_authority.lower()}{raw_path}"
        if not any(origin.startswith(base + "/") for base in allowed):
            raise UpstreamURLError(
                f"refusing upstream URL {text!r}: not below a configured upstream"
            )
    return parsed


class UpstreamClient:
    """Fetch upstream resources for the cache."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        temp_dir: Path,
        limiter: TokenBucket,
        allowed: Collection[str],
        max_redirects: int = MAX_REDIRECTS,
    ) -> None:
        self._session = session
        self._temp_dir = temp_dir
        self._limiter = limiter
        self._allowed = tuple(allowed)
        self._max_redirects = max_redirects

    async def fetch(
        self, fetch: Fetch, *, max_size: int, validators: Validators | None = None
    ) -> UpstreamResult:
        """GET ``fetch.url``; with ``validators``, conditionally.

        Returns the body (200, 404 or 410) in a temp file of the store, or
        NotModified for a 304 to a conditional request. Raises an
        UpstreamError for anything else, and StoreError if the body cannot be
        written; nothing is left in the temp directory then.
        """
        url = check_url(fetch.url, allowed=self._allowed)
        headers = _request_headers(fetch, validators)
        # Artifacts are stored exactly as sent, even when a server labels a
        # .tar.gz "Content-Encoding: gzip"; index data is decoded.
        decode = fetch.kind is not Kind.ARTIFACT
        for _ in range(self._max_redirects + 1):
            await self._limiter.acquire()
            try:
                async with self._session.get(
                    url, headers=headers, allow_redirects=False, auto_decompress=decode
                ) as response:
                    status = response.status
                    if status in _REDIRECT_STATUSES:
                        url = _redirect_target(url, response.headers.get("Location"))
                        continue
                    if status == 304 and validators is not None:
                        return NotModified(
                            etag=_header(response, "ETag"),
                            last_modified=_header(response, "Last-Modified"),
                        )
                    if status == 200:
                        return await self._receive(response, max_size, fetch.digest)
                    if status in _NEGATIVE_STATUSES:
                        return await self._receive(response, NEGATIVE_BODY_LIMIT, None, cut=True)
                    raise UpstreamStatusError(status, str(url))
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise UpstreamError(f"cannot fetch {url}: {exc!r}") from exc
        raise RedirectError(f"more than {self._max_redirects} redirects from {fetch.url}")

    async def _receive(
        self,
        response: aiohttp.ClientResponse,
        limit: int,
        digest: Digest | None,
        *,
        cut: bool = False,
    ) -> Downloaded:
        """Stream a body to a temp file; with ``cut``, keep only its first ``limit`` bytes."""
        length = response.content_length
        if not cut and length is not None and length > limit:
            raise TooLargeError(f"{response.url} is {length} bytes, over the {limit}-byte limit")
        sink = await _run_io(_Sink.create, self._temp_dir, digest)
        try:
            received = 0
            pending: list[bytes] = []
            pending_size = 0
            async for chunk in response.content.iter_chunked(_CHUNK):
                received += len(chunk)
                if received > limit:
                    if not cut:
                        raise TooLargeError(f"{response.url} is over the {limit}-byte limit")
                    pending.append(chunk[: len(chunk) - (received - limit)])
                    break
                pending.append(chunk)
                pending_size += len(chunk)
                if pending_size >= _FLUSH:
                    await _run_io(sink.write, b"".join(pending))
                    pending.clear()
                    pending_size = 0
            if pending:
                await _run_io(sink.write, b"".join(pending))
            blob, actual = await _run_io(sink.finish)
        except BaseException:
            sink.discard()
            raise
        verified = None
        if digest is not None:
            if actual != digest.hex:
                blob.path.unlink(missing_ok=True)
                raise DigestMismatchError(
                    f"{response.url} has {digest.algorithm} {actual}, "
                    f"but the index published {digest.hex}"
                )
            verified = f"{digest.algorithm}:{digest.hex}"
        return Downloaded(
            status=response.status,
            blob=blob,
            content_type=_content_type(response),
            etag=_header(response, "ETag"),
            last_modified=_header(response, "Last-Modified"),
            digest=verified,
        )


class _Sink:
    """Write a body to a temp file while hashing it, from one worker thread at a time."""

    def __init__(self, file: BinaryIO, path: Path, digest: Digest | None) -> None:
        self._file = file
        self._path = path
        self._sha256 = hashlib.sha256()
        self._other = (
            hashlib.new(digest.algorithm)
            if digest is not None and digest.algorithm != "sha256"
            else None
        )
        self._size = 0

    @classmethod
    def create(cls, directory: Path, digest: Digest | None) -> _Sink:
        fd, name = tempfile.mkstemp(dir=directory, prefix="download-")
        return cls(os.fdopen(fd, "wb"), Path(name), digest)

    def write(self, data: bytes) -> None:
        self._sha256.update(data)
        if self._other is not None:
            self._other.update(data)
        self._file.write(data)
        self._size += len(data)

    def finish(self) -> tuple[TempBlob, str]:
        """Flush, fsync and close; return the blob and the hex digest of the expected algorithm."""
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        sha256 = self._sha256.hexdigest()
        other = self._other.hexdigest() if self._other is not None else sha256
        return TempBlob(self._path, sha256, self._size), other

    def discard(self) -> None:
        """Close and delete the temp file (on the event loop: both are quick)."""
        try:
            self._file.close()
        finally:
            self._path.unlink(missing_ok=True)


async def _run_io[**P, R](function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run blocking file work in a thread; report failures as StoreError."""
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except OSError as exc:
        raise StoreError(f"cannot write a download: {exc}") from exc


def _request_headers(fetch: Fetch, validators: Validators | None) -> CIMultiDict[str]:
    headers = CIMultiDict(fetch.headers)
    if fetch.kind is Kind.ARTIFACT:
        # Store the file exactly as published; index data may come compressed.
        headers["Accept-Encoding"] = "identity"
    if validators is not None:
        if validators.etag is not None:
            headers["If-None-Match"] = validators.etag
        if validators.last_modified is not None:
            headers["If-Modified-Since"] = validators.last_modified
    return headers


def _redirect_target(url: URL, location: str | None) -> URL:
    if not location:
        raise RedirectError(f"{url} redirected without a Location")
    try:
        target = url.join(URL(location))
    except ValueError as exc:
        raise RedirectError(f"{url} redirected to an invalid URL {location!r}") from exc
    try:
        return check_url(target.with_fragment(None))
    except UpstreamURLError as exc:
        raise RedirectError(f"{url} redirected to {location!r}, which is refused") from exc


def _header(response: aiohttp.ClientResponse, name: str) -> str | None:
    """A validator header, if present and sane enough to store and send back."""
    value = response.headers.get(name)
    if (
        value is None
        or len(value) > _MAX_HEADER_VALUE
        or not value.isascii()
        or not value.isprintable()
    ):
        return None
    return value


def _content_type(response: aiohttp.ClientResponse) -> str:
    value = response.headers.get("Content-Type")
    if (
        not value
        or len(value) > _MAX_HEADER_VALUE
        or not value.isascii()
        or not value.isprintable()
    ):
        return _FALLBACK_CONTENT_TYPE
    return value
