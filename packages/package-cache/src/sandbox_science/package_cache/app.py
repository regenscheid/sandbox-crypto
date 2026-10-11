# SPDX-License-Identifier: MIT
"""The package cache's web application (docs/PACKAGE-CACHE.md).

Routes, all on one listener:

- ``GET|HEAD /-/health`` answers ``{"status": "ok"}``;
- ``GET|HEAD /-/stats`` answers the store's entries and bytes, and the hit,
  miss, revalidation, stale and error counts since the cache started;
- ``GET|HEAD /<prefix>/<path>`` is answered through the adapter for ``<prefix>``.

Any other method is answered 405. A path must be a plain URL path: one with
characters outside the URL syntax, malformed or non-UTF-8 percent escapes,
control characters, backslashes, or ``.`` or ``..`` segments (encoded or not)
is answered 400 before any adapter sees it. An unknown prefix, or an adapter
answering None, is a 404; a ``Static`` answer is served as given.

For a ``Fetch``, the stored copy is served while it is fresh: artifacts and
immutable index files until they are evicted, other index data for
``metadata_ttl``, negative (404/410) entries for a minute. Otherwise the cache
fetches it, or revalidates it with its stored validators; concurrent requests
for one key share one fetch. If the fetch fails, a stale copy at most
``stale_if_error`` old is served; without one the answer is 502 (503 when the
upstream rate limit is exhausted). Upstream 404 and 410 pass through, and are
cached briefly for index data. HEAD is answered like GET without the body,
and populates the cache the same way.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final
from urllib.parse import unquote

import aiohttp
from aiohttp import web

from .adapter import Adapter, Fetch, Kind, Rewrite, Static
from .errors import PackageCacheError, RewriteError, StoreError, UpstreamError
from .settings import Settings
from .store import Entry, Store, TempBlob
from .upstream import (
    DEFAULT_MAX_WAIT,
    Downloaded,
    NotModified,
    SingleFlight,
    TokenBucket,
    UpstreamClient,
    Validators,
    create_session,
)

NEGATIVE_TTL: Final = 60.0
"""Seconds an upstream 404 or 410 for index data is served from the store."""

_MAX_KEY_LENGTH: Final = 4096
_PREFIX_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_PATH_RE: Final = re.compile(r"/(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/]|%[0-9A-Fa-f]{2})*")
_COPY_CHUNK: Final = 256 * 1024

_log = logging.getLogger(__name__)
_requests = logging.getLogger("sandbox_science.package_cache.requests")


class CacheResult(StrEnum):
    """How the cache answered a package request."""

    HIT = "hit"
    MISS = "miss"
    REVALIDATED = "revalidated"
    STALE = "stale"
    ERROR = "error"


@dataclass(slots=True)
class Counters:
    """Package request results since the cache started."""

    hits: int = 0
    misses: int = 0
    revalidated: int = 0
    stale: int = 0
    errors: int = 0

    def count(self, result: CacheResult) -> None:
        """Count one request's result."""
        match result:
            case CacheResult.HIT:
                self.hits += 1
            case CacheResult.MISS:
                self.misses += 1
            case CacheResult.REVALIDATED:
                self.revalidated += 1
            case CacheResult.STALE:
                self.stale += 1
            case CacheResult.ERROR:
                self.errors += 1


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What a shared fetch produced: a stored entry to serve, or a response to send."""

    result: CacheResult
    entry: Entry | None = None
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    upstream_ms: float | None = None


@dataclass(slots=True)
class _RequestLog:
    """The fields of a request's log line, filled in as it is answered."""

    ecosystem: str = "-"
    result: CacheResult | None = None
    status: int | None = None
    bytes: int = 0
    upstream_ms: float | None = None
    quiet: bool = False


class PackageCache:
    """Answer requests from the store, fetching from upstream when needed."""

    def __init__(
        self,
        settings: Settings,
        adapters: Mapping[str, Adapter],
        store: Store,
        upstream: UpstreamClient,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._adapters = dict(adapters)
        self._store = store
        self._upstream = upstream
        self._clock = clock
        self._ttl = settings.metadata_ttl.total_seconds()
        self._negative_ttl = min(NEGATIVE_TTL, self._ttl)
        self._stale_if_error = settings.stale_if_error.total_seconds()
        self._max_artifact_size = settings.max_artifact_size
        self._max_metadata_size = settings.max_metadata_size
        self._flights: SingleFlight[_Outcome] = SingleFlight()
        self._eviction: asyncio.Task[None] | None = None
        self.counters = Counters()

    @property
    def store(self) -> Store:
        """The store the cache serves from."""
        return self._store

    def start(self) -> None:
        """Evict in the background if the store starts out over its cap."""
        self._evict_if_needed()

    async def close(self) -> None:
        """Cancel fetches in progress and wait for a running eviction."""
        await self._flights.close()
        if self._eviction is not None:
            await self._eviction

    async def stats(self) -> dict[str, int]:
        """The store's size and the request counters, as served at ``/-/stats``."""
        stats = await asyncio.to_thread(self._store.stats)
        counters = self.counters
        return {
            "entries": stats.entries,
            "bytes": stats.bytes,
            "hits": counters.hits,
            "misses": counters.misses,
            "revalidated": counters.revalidated,
            "stale": counters.stale,
            "errors": counters.errors,
        }

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Answer one request and log it."""
        log = _RequestLog()
        try:
            response = await self._dispatch(request, log)
            if log.status is None:
                log.status = response.status
            return response
        finally:
            _log_request(request, log)

    async def _dispatch(self, request: web.Request, log: _RequestLog) -> web.StreamResponse:
        if request.method not in ("GET", "HEAD"):
            return _text(405, "method not allowed", headers={"Allow": "GET, HEAD"})
        path = request.raw_path.partition("?")[0]
        if path in ("/-/health", "/-/stats"):
            log.quiet = True
            if path == "/-/health":
                return web.json_response({"status": "ok"})
            try:
                return web.json_response(await self.stats())
            except StoreError as exc:
                _log.error("%s", exc)
                return _text(500, "cache storage error")
        if not _safe_path(path):
            return _text(400, "bad request path")
        prefix, _, tail = path[1:].partition("/")
        adapter = self._adapters.get(prefix)
        if adapter is None:
            return _text(404, "not found")
        log.ecosystem = prefix
        try:
            route = adapter.route(tail, _client_headers(request))
        except Exception:
            _log.exception("the %s adapter failed on %s", prefix, _printable(path))
            self._record(log, CacheResult.ERROR)
            return _text(500, "internal error")
        if route is None:
            return _text(404, "not found")
        if isinstance(route, Static):
            if request.method == "GET":
                log.bytes = len(route.body)
            return web.Response(
                body=route.body, status=route.status, headers={"Content-Type": route.content_type}
            )
        return await self._serve(request, prefix, route, log)

    async def _serve(
        self, request: web.Request, prefix: str, fetch: Fetch, log: _RequestLog
    ) -> web.StreamResponse:
        try:
            key = _store_key(prefix, fetch.key)
            found = await asyncio.to_thread(self._store.lookup, key)
        except (ValueError, StoreError) as exc:
            _log.error("cannot look up %s: %s", _printable(fetch.key), exc)
            self._record(log, CacheResult.ERROR)
            return _text(500, "cache storage error")
        stale: Entry | None = None
        if found is not None:
            entry, file = found
            if self._fresh(entry, fetch) and _satisfies(entry, fetch):
                return await self._send(request, entry, file, fetch, log, CacheResult.HIT)
            file.close()
            stale = entry
        outcome = await self._flights.run(key, lambda: self._refresh(key, fetch, stale))
        log.upstream_ms = outcome.upstream_ms
        if outcome.entry is None:
            self._record(log, outcome.result)
            headers = {"Retry-After": "1"} if outcome.status == 503 else None
            return _response(outcome.status, outcome.body, outcome.content_type, headers)
        if not _satisfies(outcome.entry, fetch):
            # A request sharing a fetch that verified another digest for this key.
            _log.warning("%s was verified against another digest", key)
            self._record(log, CacheResult.ERROR)
            return _text(502, "digest mismatch")
        try:
            file = await asyncio.to_thread(self._store.open_blob, outcome.entry)
        except StoreError as exc:
            _log.error("%s", exc)
            self._record(log, CacheResult.ERROR)
            return _text(500, "cache storage error")
        if file is None:
            # Evicted between storing and serving: only with a cap far too small.
            self._record(log, CacheResult.ERROR)
            return _text(503, "evicted while fetching, retry later", headers={"Retry-After": "1"})
        return await self._send(request, outcome.entry, file, fetch, log, outcome.result)

    async def _send(
        self,
        request: web.Request,
        entry: Entry,
        file: BinaryIO,
        fetch: Fetch,
        log: _RequestLog,
        result: CacheResult,
    ) -> web.StreamResponse:
        """Serve a stored entry from its open blob, which this closes."""
        self._record(log, result)
        try:
            response = web.StreamResponse(status=entry.status)
            content_type = entry.content_type
            if fetch.content_type is not None and entry.status == 200:
                content_type = fetch.content_type
            response.headers["Content-Type"] = content_type
            response.content_length = entry.size
            await response.prepare(request)
            log.status = response.status
            if request.method == "GET" and entry.size > 0:
                try:
                    await _send_file(request, response, file, entry.size)
                except ConnectionError as exc:
                    # The client went away mid-body (an interrupted install): not an error.
                    _log.debug("client went away while receiving %s: %s", entry.key, exc)
                    response.force_close()
                    return response
                log.bytes = entry.size
            await response.write_eof()
            return response
        finally:
            file.close()

    async def _refresh(self, key: str, fetch: Fetch, stale: Entry | None) -> _Outcome:
        """Fetch or revalidate ``key``; the operation that concurrent requests share."""
        started = time.monotonic()
        try:
            outcome = await self._fetch(key, fetch, stale)
        except (UpstreamError, StoreError) as exc:
            outcome = self._fallback(key, stale, exc)
        except Exception:
            # A bug: log it once here rather than in every request sharing the fetch.
            _log.exception("fetching %s for %s failed", fetch.url, _printable(key))
            outcome = _Outcome(CacheResult.ERROR, status=500, body=b"internal error\n")
        return replace(outcome, upstream_ms=(time.monotonic() - started) * 1000)

    async def _fetch(self, key: str, fetch: Fetch, stale: Entry | None) -> _Outcome:
        validators = None
        if (
            stale is not None
            and stale.status == 200
            and (stale.etag is not None or stale.last_modified is not None)
            and _satisfies(stale, fetch)
        ):
            validators = Validators(stale.etag, stale.last_modified)
        limit = self._max_artifact_size if fetch.kind is Kind.ARTIFACT else self._max_metadata_size
        result = await self._upstream.fetch(fetch, max_size=limit, validators=validators)
        if isinstance(result, NotModified):
            if stale is None:
                raise UpstreamError(f"{fetch.url} answered 304 to an unconditional request")
            entry = await asyncio.to_thread(
                self._store.refresh,
                stale,
                etag=result.etag or stale.etag,
                last_modified=result.last_modified or stale.last_modified,
            )
            if entry is None:
                # The entry changed or was evicted meanwhile: fetch the body in full.
                return await self._fetch(key, fetch, None)
            return _Outcome(CacheResult.REVALIDATED, entry=entry)
        try:
            return await self._keep(key, fetch, result)
        finally:
            result.blob.path.unlink(missing_ok=True)  # a no-op once stored

    async def _keep(self, key: str, fetch: Fetch, download: Downloaded) -> _Outcome:
        """Store a downloaded body (rewritten if need be), or pass an error body on."""
        if download.status != 200:
            if fetch.kind is not Kind.METADATA:
                body = await asyncio.to_thread(_read, download.blob.path)
                return _Outcome(
                    CacheResult.MISS,
                    status=download.status,
                    body=body,
                    content_type=download.content_type,
                )
            entry = await asyncio.to_thread(
                self._store.put,
                key,
                download.blob,
                kind=fetch.kind,
                content_type=download.content_type,
                status=download.status,
            )
            self._evict_if_needed()
            return _Outcome(CacheResult.MISS, entry=entry)
        blob = download.blob
        if fetch.rewrite is not None:
            blob = await self._rewrite(fetch.rewrite, fetch.url, download)
        try:
            entry = await asyncio.to_thread(
                self._store.put,
                key,
                blob,
                kind=fetch.kind,
                content_type=download.content_type,
                immutable=fetch.immutable or fetch.kind is Kind.ARTIFACT,
                etag=download.etag,
                last_modified=download.last_modified,
                digest=download.digest,
            )
        finally:
            blob.path.unlink(missing_ok=True)  # a no-op once stored
        self._evict_if_needed()
        return _Outcome(CacheResult.MISS, entry=entry)

    async def _rewrite(self, rewrite: Rewrite, url: str, download: Downloaded) -> TempBlob:
        raw = await asyncio.to_thread(_read, download.blob.path)
        try:
            body = await asyncio.to_thread(rewrite, raw, download.content_type)
        except ValueError as exc:
            raise RewriteError(f"cannot rewrite the index from {url}: {exc}") from exc
        return await asyncio.to_thread(self._store.write_temp, body)

    def _fallback(self, key: str, stale: Entry | None, error: PackageCacheError) -> _Outcome:
        if stale is not None and 0 <= self._clock() - stale.fetched <= self._stale_if_error:
            _log.warning("serving a stale copy of %s: %s", _printable(key), error)
            return _Outcome(CacheResult.STALE, entry=stale)
        _log.warning("cannot serve %s: %s", _printable(key), error)
        if isinstance(error, UpstreamError):
            return _Outcome(
                CacheResult.ERROR, status=error.http_status, body=f"{error.reason}\n".encode()
            )
        return _Outcome(CacheResult.ERROR, status=500, body=b"cache storage error\n")

    def _fresh(self, entry: Entry, fetch: Fetch) -> bool:
        age = self._clock() - entry.fetched
        if age < 0:
            return False  # fetched "in the future": the clock went back, so check again
        if entry.status != 200:
            return age < self._negative_ttl
        if fetch.kind is Kind.ARTIFACT or fetch.immutable:
            return True
        return age < self._ttl

    def _record(self, log: _RequestLog, result: CacheResult) -> None:
        log.result = result
        self.counters.count(result)

    def _evict_if_needed(self) -> None:
        if self._eviction is None and self._store.over_capacity():
            self._eviction = asyncio.create_task(self._evict())

    async def _evict(self) -> None:
        try:
            removed = await asyncio.to_thread(self._store.evict)
            if removed.entries or removed.blobs:
                _log.info(
                    "evicted %d entries and %d blobs (%d bytes)",
                    removed.entries,
                    removed.blobs,
                    removed.bytes,
                )
        except StoreError as exc:
            _log.error("eviction failed: %s", exc)
        finally:
            self._eviction = None


CACHE: Final = web.AppKey("cache", PackageCache)
"""The running cache, available once the application has started."""


def create_app(
    settings: Settings,
    adapters: Mapping[str, Adapter],
    *,
    ssl_context: ssl.SSLContext | None = None,
    connector: aiohttp.BaseConnector | None = None,
    clock: Callable[[], float] = time.time,
    rate_limit_wait: float = DEFAULT_MAX_WAIT,
) -> web.Application:
    """Build the cache's web application.

    ``adapters`` maps each adapter's prefix to the adapter. When the
    application starts it opens and recovers the store (taking the cache
    directory's lock) and creates the upstream session; it closes both when it
    stops. ``ssl_context`` or ``connector`` replace the default TLS
    verification for upstream requests (tests use a local TLS upstream with
    its own certificate authority). ``clock`` gives the Unix time used for
    freshness, and ``rate_limit_wait`` is how long a fetch may wait for an
    upstream token before it fails with 503.
    """
    checked = _check_adapters(adapters)
    if ssl_context is not None and connector is not None:
        raise ValueError("pass ssl_context or connector, not both")
    app = web.Application()

    async def lifecycle(app: web.Application) -> AsyncIterator[None]:
        store = await asyncio.to_thread(_open_store, settings, clock)
        try:
            session = create_session(ssl_context=ssl_context, connector=connector)
            try:
                limiter = TokenBucket(
                    settings.upstream_rate, settings.upstream_burst, max_wait=rate_limit_wait
                )
                upstream = UpstreamClient(
                    session, temp_dir=store.temp_dir, limiter=limiter, allowed=settings.upstreams()
                )
                cache = PackageCache(settings, checked, store, upstream, clock=clock)
                app[CACHE] = cache
                cache.start()
                try:
                    yield
                finally:
                    await cache.close()
            finally:
                await session.close()
        finally:
            await asyncio.to_thread(store.close)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_route("*", r"/{path:[\s\S]*}", _handle)
    return app


async def _handle(request: web.Request) -> web.StreamResponse:
    return await request.app[CACHE].handle(request)


def _open_store(settings: Settings, clock: Callable[[], float]) -> Store:
    store = Store(settings.cache_dir, max_size=settings.max_size, clock=clock)
    try:
        removed = store.recover()
    except BaseException:
        store.close()
        raise
    if removed.blobs:
        _log.info("removed %d unreferenced blobs (%d bytes)", removed.blobs, removed.bytes)
    return store


def _check_adapters(adapters: Mapping[str, Adapter]) -> dict[str, Adapter]:
    checked: dict[str, Adapter] = {}
    for name, adapter in adapters.items():
        if not _PREFIX_RE.fullmatch(name):
            raise ValueError(f"{name!r} is not a valid adapter prefix")
        if adapter.prefix != name:
            raise ValueError(f"the adapter registered as {name!r} has prefix {adapter.prefix!r}")
        checked[name] = adapter
    return checked


def _safe_path(path: str) -> bool:
    """Whether a raw request path (without its query) may reach an adapter."""
    if not _PATH_RE.fullmatch(path):
        return False
    for segment in path.split("/"):
        try:
            decoded = unquote(segment, errors="strict")
        except UnicodeDecodeError:
            return False
        if any(char == "\\" or ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in decoded):
            return False
        if any(part in (".", "..") for part in decoded.split("/")):
            return False
    return True


def _store_key(prefix: str, key: str) -> str:
    """Namespace an adapter's key with its prefix, so adapters cannot collide."""
    if not key or len(key) > _MAX_KEY_LENGTH or "\x00" in key:
        raise ValueError(f"invalid cache key {key[:80]!r}")
    return f"{prefix}:{key}"


def _satisfies(entry: Entry, fetch: Fetch) -> bool:
    """Whether serving ``entry`` meets ``fetch``'s digest: it was verified against it."""
    if fetch.digest is None or entry.status != 200:
        return True
    return entry.digest == f"{fetch.digest.algorithm}:{fetch.digest.hex}"


def _client_headers(request: web.Request) -> dict[str, str]:
    """The client's headers with lowercased names; repeated headers are joined with commas."""
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lowered = name.lower()
        headers[lowered] = f"{headers[lowered]}, {value}" if lowered in headers else value
    return headers


async def _send_file(
    request: web.Request, response: web.StreamResponse, file: BinaryIO, size: int
) -> None:
    transport = request.transport
    if transport is None or transport.is_closing():
        raise ConnectionResetError("the client went away")
    try:
        await asyncio.get_running_loop().sendfile(transport, file, 0, size)
    except NotImplementedError:
        # Event loops without sendfile (uvloop): copy in chunks instead.
        offset = 0
        while offset < size:
            chunk = await asyncio.to_thread(
                os.pread, file.fileno(), min(_COPY_CHUNK, size - offset), offset
            )
            if not chunk:
                raise StoreError(f"blob {file.name!r} ended at {offset} of {size} bytes") from None
            await response.write(chunk)
            offset += len(chunk)


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StoreError(f"cannot read {path}: {exc}") from exc


def _text(status: int, message: str, *, headers: Mapping[str, str] | None = None) -> web.Response:
    return web.Response(status=status, text=f"{message}\n", headers=headers)


def _response(
    status: int, body: bytes, content_type: str, headers: Mapping[str, str] | None
) -> web.Response:
    response = web.Response(status=status, body=body, headers=headers)
    response.headers["Content-Type"] = content_type
    return response


def _printable(text: str) -> str:
    """``text`` with anything but printable ASCII escaped, for log lines."""
    return "".join(char if " " < char < "\x7f" else f"\\x{ord(char):02x}" for char in text[:2048])


def _log_request(request: web.Request, log: _RequestLog) -> None:
    _requests.log(
        logging.DEBUG if log.quiet else logging.INFO,
        "ecosystem=%s method=%s path=%s result=%s status=%s bytes=%d upstream_ms=%s",
        log.ecosystem,
        _printable(request.method),
        _printable(request.raw_path),
        log.result or "-",
        log.status if log.status is not None else "-",
        log.bytes,
        "-" if log.upstream_ms is None else f"{log.upstream_ms:.0f}",
    )
