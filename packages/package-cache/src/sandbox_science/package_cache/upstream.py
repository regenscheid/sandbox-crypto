# SPDX-License-Identifier: MIT
"""Fetching from the upstream registries.

``UpstreamClient.fetch`` performs one upstream GET for a ``Fetch``:

- the URL must lie below one of the configured upstream base URLs; upstream
  may redirect (at most five times) to other ``https://`` URLs, on the host of
  a configured upstream or on a public host only (``check_redirect``);
- only the adapter's ``Fetch.headers`` are sent, plus a User-Agent,
  ``Accept-Encoding: identity`` for artifacts and the stored validators when
  revalidating; client headers never reach upstream;
- the body streams to a temp file in the store while SHA-256 (the blob name)
  and the expected digest's algorithm are computed, and the download is
  abandoned as soon as ``Content-Length`` or the bytes received exceed the
  limit; artifacts are stored exactly as sent, while index data may arrive
  compressed and is decoded (and measured after decoding);
- every request takes a token from one ``TokenBucket`` shared by all clients.

So that no kind of request can starve the others, capacity is bounded and
waiting for it is too. A fetch that cannot get capacity in time fails with
BusyError (503):

- at most ``max_artifact_downloads`` artifacts download at once; a fetch waits
  a few seconds for a slot;
- the session's connection pool holds ``INDEX_CONNECTIONS`` more connections
  than that (``pool_size``), so index data always finds one; getting a
  connection, from the pool or by connecting, takes at most ``CONNECT_TIMEOUT``;
- downloads in progress reserve their size in a ``TempSpace`` (the
  ``Content-Length``, or what has arrived so far), and a download that would
  overflow it fails at once;
- an index fetch must finish within ``METADATA_DEADLINE`` (a plain
  UpstreamError, 502, since upstream is too slow).

``DigestMemory`` remembers the actual digests of recent downloads of
immutable files by URL. A fetch whose expected digest contradicts them fails
with DigestMismatchError without downloading the file again, and downloads of
one URL that are checked against digests run one at a time, so that
concurrent requests naming different digests download it once.

``SingleFlight`` lets concurrent requests for the same key share one fetch.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import os
import ssl
import tempfile
import time
from collections import OrderedDict
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Mapping,
)
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final
from urllib.parse import unquote

import aiohttp
from multidict import CIMultiDict
from yarl import URL

from sandbox_science.policy.hosts import floor_reason, is_public, normalize_host

from .adapter import Digest, Fetch, Kind
from .errors import (
    BusyError,
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
CONNECT_TIMEOUT: Final = 10.0
"""Seconds to get an upstream connection: a free one from the pool, or a new one."""
DEFAULT_TIMEOUT: Final = aiohttp.ClientTimeout(
    total=None, connect=CONNECT_TIMEOUT, sock_connect=CONNECT_TIMEOUT, sock_read=60
)
"""No overall limit, since artifacts can be large (index data has ``METADATA_DEADLINE``),
but a bounded wait for a connection and no stalls longer than a minute."""
METADATA_DEADLINE: Final = 120.0
"""Seconds an index fetch may take in all, redirects and the body included."""
DEFAULT_MAX_WAIT: Final = 10.0
"""Seconds a fetch may wait for the rate limit before it fails with 503."""
DEFAULT_SLOT_WAIT: Final = 5.0
"""Seconds a fetch may wait for a download slot before it fails with 503."""
DEFAULT_ARTIFACT_DOWNLOADS: Final = 16
"""Artifact downloads that may run at once (the ``max_artifact_downloads`` setting)."""
INDEX_CONNECTIONS: Final = 32
"""Upstream connections that artifact downloads can never take, kept for index data."""
DIGEST_MEMORY_SIZE: Final = 10_000
"""URLs whose actual digests ``DigestMemory`` keeps."""
DIGEST_MEMORY_TTL: Final = 3600.0
"""Seconds ``DigestMemory`` trusts a download's digests, so that a file upstream really
changed (or a corrupt copy a CDN served once) is downloaded again eventually."""

_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_NEGATIVE_STATUSES: Final = frozenset({404, 410})
_CHUNK: Final = 256 * 1024
_FLUSH: Final = 1024 * 1024
_RESERVE_STEP: Final = 4 * 1024 * 1024
_MAX_HEADER_VALUE: Final = 1024
_FALLBACK_CONTENT_TYPE: Final = "application/octet-stream"
_PATH_CHARACTERS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@%/"
)

_log = logging.getLogger(__name__)


def pool_size(max_artifact_downloads: int) -> int:
    """The upstream connection pool for a cache that runs ``max_artifact_downloads`` at once.

    Artifact downloads never hold more connections than they have slots, so
    ``INDEX_CONNECTIONS`` always remain for index data.
    """
    return max_artifact_downloads + INDEX_CONNECTIONS


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


class TempSpace:
    """The bytes that downloads in progress may hold in the store's temp directory.

    Each download takes a ``Reservation`` before it writes and grows it as the
    body arrives; the reservation is released once the temp file has been
    stored or deleted. Use it from the event loop only.
    """

    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("the temp space limit must be positive")
        self._limit = limit
        self._used = 0

    @property
    def limit(self) -> int:
        """The most bytes downloads in progress may hold."""
        return self._limit

    @property
    def used(self) -> int:
        """The bytes reserved now."""
        return self._used

    def reserve(self, size: int) -> Reservation:
        """Reserve ``size`` bytes; raise BusyError if they do not fit."""
        reservation = Reservation(self)
        reservation.grow(size)
        return reservation

    def claim(self, size: int) -> None:
        """Count ``size`` more bytes as used, or raise BusyError if they do not fit.

        ``Reservation`` calls this; other code takes a reservation instead.
        """
        if size < 0:
            raise ValueError("cannot claim a negative size")
        if self._used + size > self._limit:
            raise BusyError(
                f"downloads in progress hold {self._used} of {self._limit} temp bytes, "
                f"so {size} more do not fit"
            )
        self._used += size

    def free(self, size: int) -> None:
        """Count ``size`` bytes as no longer used (``Reservation.release`` calls this)."""
        self._used = max(self._used - size, 0)


class Reservation:
    """The bytes one download holds in a ``TempSpace``."""

    def __init__(self, space: TempSpace) -> None:
        self._space = space
        self._size = 0
        self._released = False

    @property
    def size(self) -> int:
        """The bytes reserved."""
        return self._size

    def grow(self, size: int) -> None:
        """Reserve ``size`` more bytes; raise BusyError if they do not fit."""
        if self._released:
            raise RuntimeError("the reservation was released")
        self._space.claim(size)
        self._size += size

    def release(self) -> None:
        """Give the bytes back; later calls do nothing."""
        if not self._released:
            self._released = True
            self._space.free(self._size)


class DigestMemory:
    """The actual digests of recent downloads, by upstream URL.

    An LRU of at most ``size`` URLs, each mapping algorithm names to hex
    digests. An entry is forgotten ``ttl`` seconds after the download that
    recorded it.
    """

    def __init__(
        self,
        *,
        size: int = DIGEST_MEMORY_SIZE,
        ttl: float = DIGEST_MEMORY_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if size < 1 or ttl <= 0:
            raise ValueError("size must be at least 1 and ttl positive")
        self._size = size
        self._ttl = ttl
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def remember(self, url: str, digests: Mapping[str, str]) -> None:
        """Record the digests (algorithm to hex) that a complete download of ``url`` had."""
        self._entries[url] = (self._clock() + self._ttl, dict(digests))
        self._entries.move_to_end(url)
        while len(self._entries) > self._size:
            self._entries.popitem(last=False)

    def actual(self, url: str, algorithm: str) -> str | None:
        """The ``algorithm`` digest ``url`` had when last downloaded, if it is remembered."""
        entry = self._entries.get(url)
        if entry is None:
            return None
        expires, digests = entry
        if self._clock() >= expires:
            del self._entries[url]
            return None
        self._entries.move_to_end(url)
        return digests.get(algorithm)


@dataclass(frozen=True, slots=True)
class Validators:
    """A stored response's validators, for a conditional request."""

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class Downloaded:
    """A complete upstream body, in a temp file of the store.

    The caller owns the temp file and its reservation: ``close`` it once the
    body has been stored (``Store.put`` moves the file) or is no longer needed.
    """

    status: int
    """200, or 404 or 410 (whose body is cut at ``NEGATIVE_BODY_LIMIT``)."""
    blob: TempBlob
    content_type: str
    reservation: Reservation
    """The temp space the body holds."""
    etag: str | None = None
    last_modified: str | None = None
    digest: str | None = None
    """The expected digest the body was verified against, as ``<algorithm>:<hex>``."""

    def close(self) -> None:
        """Delete the temp file if it is still there, and release its temp space."""
        try:
            self.blob.path.unlink(missing_ok=True)
        finally:
            self.reservation.release()


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
    limit: int = pool_size(DEFAULT_ARTIFACT_DOWNLOADS),
) -> aiohttp.ClientSession:
    """Create the session for upstream requests; call it with the event loop running.

    It ignores proxy environment variables and never stores cookies. Its pool
    holds ``limit`` connections (see ``pool_size``). Pass ``ssl_context`` to
    trust other certificate authorities (tests), or a whole ``connector``
    (which brings its own limit); the session owns and closes either.
    """
    if connector is not None and ssl_context is not None:
        raise ValueError("pass ssl_context or connector, not both")
    if limit < 1:
        raise ValueError("the connection pool needs at least one connection")
    if connector is None:
        connector = aiohttp.TCPConnector(
            ssl=ssl_context if ssl_context is not None else True, ttl_dns_cache=300, limit=limit
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


def check_redirect(url: URL, location: str | None, *, allowed: Collection[str]) -> URL:
    """Return where a redirect from ``url`` to ``location`` leads, if the cache may follow it.

    The target must pass ``check_url`` and be on the host (and port) of one of
    the ``allowed`` base URLs, or on a public host: not an IP address that is
    not public, not a name that is not (``localhost``, single-label names,
    special-use suffixes such as ``.internal``), and nothing on the policy's
    hard floor. Resolved addresses are not checked. Raises RedirectError.
    """
    if not location:
        raise RedirectError(f"{url} redirected without a Location")
    try:
        target = url.join(URL(location))
    except ValueError as exc:
        raise RedirectError(f"{url} redirected to an invalid URL {location!r}") from exc
    try:
        target = check_url(target.with_fragment(None))
    except UpstreamURLError as exc:
        raise RedirectError(f"{url} redirected to {location!r}, which is refused") from exc
    if _origin(target) in {_origin(URL(base, encoded=True)) for base in allowed}:
        return target
    reason = _not_public(target.raw_host or "")
    if reason is not None:
        raise RedirectError(f"{url} redirected to {target.raw_authority}, refused: {reason}")
    return target


def _origin(url: URL) -> tuple[str, int | None]:
    return (url.raw_host or "").lower(), url.port


def _not_public(host: str) -> str | None:
    """Why a redirect may not lead to ``host``, or None if it is a public host."""
    try:
        normalized = normalize_host(host)
    except ValueError as exc:
        return str(exc)
    reason = floor_reason(normalized)
    if reason is not None:
        return reason
    if not is_public(normalized):
        return f"{normalized} is not a public host"
    return None


@dataclass(slots=True)
class _Users:
    """A lock and how many fetches hold it or wait for it."""

    lock: asyncio.Lock
    count: int = 0


class UpstreamClient:
    """Fetch upstream resources for the cache."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        temp_dir: Path,
        limiter: TokenBucket,
        allowed: Collection[str],
        temp_space: TempSpace,
        max_artifact_downloads: int = DEFAULT_ARTIFACT_DOWNLOADS,
        max_wait: float = DEFAULT_SLOT_WAIT,
        metadata_deadline: float = METADATA_DEADLINE,
        digests: DigestMemory | None = None,
        max_redirects: int = MAX_REDIRECTS,
    ) -> None:
        if max_artifact_downloads < 1 or max_wait < 0 or metadata_deadline <= 0:
            raise ValueError(
                "max_artifact_downloads must be at least 1, max_wait not negative "
                "and metadata_deadline positive"
            )
        self._session = session
        self._temp_dir = temp_dir
        self._limiter = limiter
        self._allowed = tuple(allowed)
        self._space = temp_space
        self._max_downloads = max_artifact_downloads
        self._downloads = asyncio.Semaphore(max_artifact_downloads)
        self._max_wait = max_wait
        self._metadata_deadline = metadata_deadline
        self._digests = digests if digests is not None else DigestMemory()
        self._checked_downloads: dict[str, _Users] = {}
        self._max_redirects = max_redirects

    async def fetch(
        self, fetch: Fetch, *, max_size: int, validators: Validators | None = None
    ) -> UpstreamResult:
        """GET ``fetch.url``; with ``validators``, conditionally.

        Returns the body (200, 404 or 410) in a temp file of the store, which
        the caller must ``close``, or NotModified for a 304 to a conditional
        request. Raises an UpstreamError for anything else (BusyError when
        the cache lacks capacity now), and StoreError if the body cannot be
        written; nothing is left in the temp directory then.
        """
        url = check_url(fetch.url, allowed=self._allowed)
        # Immutable files checked against a digest: what a download of the URL
        # turned out to be settles every later request for it, whatever its key.
        remember = fetch.digest is not None and (fetch.kind is Kind.ARTIFACT or fetch.immutable)
        if remember:
            self._check_remembered(fetch)
        async with contextlib.AsyncExitStack() as stack:
            if remember:
                await stack.enter_async_context(self._one_checked_download(fetch.url))
                self._check_remembered(fetch)  # a download that just finished may settle it
            if fetch.kind is Kind.ARTIFACT:
                await stack.enter_async_context(self._download_slot())
                return await self._get(url, fetch, max_size, validators, remember=remember)
            try:
                async with asyncio.timeout(self._metadata_deadline):
                    return await self._get(url, fetch, max_size, validators, remember=remember)
            except TimeoutError as exc:
                raise UpstreamError(
                    f"{fetch.url} took longer than {self._metadata_deadline:g}s"
                ) from exc

    def _check_remembered(self, fetch: Fetch) -> None:
        digest = fetch.digest
        if digest is None:
            return
        actual = self._digests.actual(fetch.url, digest.algorithm)
        if actual is not None and actual != digest.hex:
            raise DigestMismatchError(
                f"{fetch.url} had {digest.algorithm} {actual} when it was last downloaded, "
                f"but the index published {digest.hex}; not downloading it again"
            )

    @contextlib.asynccontextmanager
    async def _one_checked_download(self, url: str) -> AsyncGenerator[None, None]:
        """Hold the right to download ``url`` for a digest check; others wait their turn."""
        users = self._checked_downloads.get(url)
        if users is None:
            users = self._checked_downloads[url] = _Users(asyncio.Lock())
        users.count += 1
        try:
            try:
                async with asyncio.timeout(self._max_wait):
                    await users.lock.acquire()
            except TimeoutError:
                raise BusyError(
                    f"{url} has been downloading for another digest for {self._max_wait:g}s"
                ) from None
            try:
                yield
            finally:
                users.lock.release()
        finally:
            users.count -= 1
            if users.count == 0:
                del self._checked_downloads[url]

    @contextlib.asynccontextmanager
    async def _download_slot(self) -> AsyncGenerator[None, None]:
        """Hold one of the ``max_artifact_downloads`` slots, waiting at most ``max_wait``."""
        try:
            async with asyncio.timeout(self._max_wait):
                await self._downloads.acquire()
        except TimeoutError:
            raise BusyError(
                f"all {self._max_downloads} download slots stayed busy for {self._max_wait:g}s"
            ) from None
        try:
            yield
        finally:
            self._downloads.release()

    async def _get(
        self,
        url: URL,
        fetch: Fetch,
        max_size: int,
        validators: Validators | None,
        *,
        remember: bool,
    ) -> UpstreamResult:
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
                        target = check_redirect(
                            url, response.headers.get("Location"), allowed=self._allowed
                        )
                        _log.info(
                            "%s redirected to host %s",
                            url.with_query(None),
                            target.raw_authority,
                        )
                        url = target
                        continue
                    if status == 304 and validators is not None:
                        return NotModified(
                            etag=_header(response, "ETag"),
                            last_modified=_header(response, "Last-Modified"),
                        )
                    if status == 200:
                        return await self._receive(
                            response,
                            max_size,
                            fetch.digest,
                            remember=fetch.url if remember else None,
                        )
                    if status in _NEGATIVE_STATUSES:
                        return await self._receive(response, NEGATIVE_BODY_LIMIT, None, cut=True)
                    raise UpstreamStatusError(status, str(url))
            except aiohttp.ConnectionTimeoutError as exc:
                # Every pooled connection busy, or upstream slow to accept one.
                raise BusyError(
                    f"no connection to {url.raw_authority} within the connect timeout: {exc!r}"
                ) from exc
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
        remember: str | None = None,
    ) -> Downloaded:
        """Stream a body to a temp file; with ``cut``, keep only its first ``limit`` bytes.

        With ``remember``, record the body's digests under that URL in the digest
        memory, whether or not it matches ``digest``.
        """
        length = response.content_length
        if not cut and length is not None and length > limit:
            raise TooLargeError(f"{response.url} is {length} bytes, over the {limit}-byte limit")
        reservation = self._space.reserve(min(length, limit) if length is not None else 0)
        try:
            blob, actual = await self._stream(response, limit, digest, reservation, cut=cut)
            if remember is not None:
                digests = {"sha256": blob.sha256}
                if digest is not None:
                    digests[digest.algorithm] = actual
                self._digests.remember(remember, digests)
            verified = None
            if digest is not None:
                if actual != digest.hex:
                    blob.path.unlink(missing_ok=True)
                    raise DigestMismatchError(
                        f"{response.url} has {digest.algorithm} {actual}, "
                        f"but the index published {digest.hex}"
                    )
                verified = f"{digest.algorithm}:{digest.hex}"
        except BaseException:
            reservation.release()
            raise
        return Downloaded(
            status=response.status,
            blob=blob,
            content_type=_content_type(response),
            reservation=reservation,
            etag=_header(response, "ETag"),
            last_modified=_header(response, "Last-Modified"),
            digest=verified,
        )

    async def _stream(
        self,
        response: aiohttp.ClientResponse,
        limit: int,
        digest: Digest | None,
        reservation: Reservation,
        *,
        cut: bool,
    ) -> tuple[TempBlob, str]:
        """Write the body to a temp file, growing ``reservation`` to cover what arrives."""
        sink = await _run_io(_Sink.create, self._temp_dir, digest)
        try:
            received = 0
            pending: list[bytes] = []
            pending_size = 0
            async for chunk in response.content.iter_chunked(_CHUNK):
                received += len(chunk)
                over = received > limit
                if over:
                    if not cut:
                        raise TooLargeError(f"{response.url} is over the {limit}-byte limit")
                    chunk = chunk[: len(chunk) - (received - limit)]
                    received = limit
                if received > reservation.size:
                    # Without a Content-Length, or decoded beyond it: reserve ahead into
                    # free space, but what has arrived must fit.
                    needed = received - reservation.size
                    ahead = min(
                        _RESERVE_STEP,
                        limit - reservation.size,
                        self._space.limit - self._space.used,
                    )
                    reservation.grow(max(needed, ahead))
                pending.append(chunk)
                pending_size += len(chunk)
                if over:
                    break
                if pending_size >= _FLUSH:
                    await _run_io(sink.write, b"".join(pending))
                    pending.clear()
                    pending_size = 0
            if pending:
                await _run_io(sink.write, b"".join(pending))
            return await _run_io(sink.finish)
        except BaseException:
            sink.discard()
            raise


class _Sink:
    """Write a body to a temp file while hashing it, from one worker thread at a time."""

    def __init__(self, file: BinaryIO, path: Path, digest: Digest | None) -> None:
        self._file = file
        self._path = path
        self._sha256 = hashlib.sha256()
        # An MD5 check only confirms that an opam archive is the one its path names;
        # opam checks the stronger checksums an opam file lists itself. Declaring it
        # so keeps it available where FIPS mode forbids MD5 for security.
        self._other = (
            hashlib.new(digest.algorithm, usedforsecurity=digest.algorithm != "md5")
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
