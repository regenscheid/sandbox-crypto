# SPDX-License-Identifier: MIT
"""Command line: run the cache, and inspect or prune its store.

```
sandbox-science-package-cache serve --settings cache.toml [--log-level INFO]
sandbox-science-package-cache stats --settings cache.toml
sandbox-science-package-cache prune --settings cache.toml
sandbox-science-package-cache forget --settings cache.toml KEY_OR_PATH
```

``stats``, ``prune`` and ``forget`` print JSON and work on the store directly,
so they may run while the cache serves. ``prune`` purges expired negative
entries and evicts down to the size cap now, as the cache does after storing a
file, and deletes blobs no entry refers to that are over an hour old (younger
ones may be about to get their entry). ``forget`` drops one entry, such as a
bad copy of a file the cache cannot verify, so that the next request fetches
it again: give its cache key (``hackage:hackage/package/split-0.2.5.tar.gz``)
or the path a client requested (``/hackage/package/split-0.2.5.tar.gz``, as the
request log shows it), which the adapters turn into the key.

``serve`` warns when the open file limit is below ``wanted_open_files``: each
request being served may hold several descriptors, and idle connections one
each (docs/PACKAGE-CACHE.md recommends ``LimitNOFILE=65536``).
"""

from __future__ import annotations

import argparse
import json
import logging
import resource
import sys
from collections.abc import Sequence
from pathlib import Path

from aiohttp import web

from sandbox_science.contracts.logs import configure_logging

from .adapter import Fetch
from .app import KEEPALIVE_TIMEOUT, create_app, negative_expiry, store_key
from .errors import PackageCacheError, StoreError
from .settings import Settings, load_settings
from .store import ORPHAN_GRACE, Store
from .wiring import build_adapters

_log = logging.getLogger("sandbox_science.package_cache")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def wanted_open_files(settings: Settings) -> int:
    """File descriptors the cache may need under full load, idle connections aside.

    Each request being served holds its connection, a stored or downloading
    file and possibly an upstream connection; the rest covers the upstream
    pool, the index and the listener.
    """
    return 4 * settings.max_requests + 1024


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``sandbox-science-package-cache``."""
    parser = argparse.ArgumentParser(prog="sandbox-science-package-cache")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the package cache")
    serve.add_argument("--settings", type=Path, required=True)
    serve.add_argument("--log-level", default="INFO", type=str.upper, choices=_LOG_LEVELS)
    stats = commands.add_parser("stats", help="show how much the store holds")
    stats.add_argument("--settings", type=Path, required=True)
    prune = commands.add_parser("prune", help="evict down to the size cap now")
    prune.add_argument("--settings", type=Path, required=True)
    forget = commands.add_parser("forget", help="drop one entry, to fetch it again")
    forget.add_argument("--settings", type=Path, required=True)
    forget.add_argument(
        "target", metavar="KEY_OR_PATH", help="a cache key, or a request path starting with /"
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.settings)
        match args.command:
            case "serve":
                configure_logging("cache", level=args.log_level)
                return _serve(settings)
            case "stats":
                return _stats(settings)
            case "prune":
                return _prune(settings)
            case _:
                return _forget(settings, args.target)
    except (PackageCacheError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _serve(settings: Settings) -> int:
    try:
        adapters = build_adapters(settings)
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not adapters:
        _log.warning("no ecosystem is enabled: every package path answers 404")
    _check_open_files(settings)
    app = create_app(settings, adapters)
    _log.info(
        "serving %s on %s for %s",
        ", ".join(sorted(adapters)) or "no ecosystems",
        settings.listen,
        settings.public_url,
    )
    web.run_app(
        app,
        host=settings.listen_host,
        port=settings.listen_port,
        access_log=None,
        print=None,
        shutdown_timeout=10.0,
        keepalive_timeout=KEEPALIVE_TIMEOUT,
    )
    return 0


def _check_open_files(settings: Settings) -> None:
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = wanted_open_files(settings)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        _log.warning(
            "the open file limit is %d, but max_requests = %d may need %d or more; "
            "raise it (LimitNOFILE=65536 in a systemd unit)",
            soft,
            settings.max_requests,
            wanted,
        )


def _open(settings: Settings) -> Store:
    if not settings.cache_dir.is_dir():
        raise StoreError(f"there is no package cache in {settings.cache_dir}")
    return Store(
        settings.cache_dir, max_size=settings.max_size, negative_expiry=negative_expiry(settings)
    )


def _stats(settings: Settings) -> int:
    with _open(settings) as store:
        stats = store.stats()
    report = {"entries": stats.entries, "bytes": stats.bytes, "max_size": settings.max_size}
    print(json.dumps(report))
    return 0


def _prune(settings: Settings) -> int:
    with _open(settings) as store:
        evicted = store.evict()
        orphans = store.remove_orphans(min_age=ORPHAN_GRACE)
        stats = store.stats()
    report = {
        "evicted_entries": evicted.entries,
        "deleted_blobs": evicted.blobs + orphans.blobs,
        "freed_bytes": evicted.bytes + orphans.bytes,
        "entries": stats.entries,
        "bytes": stats.bytes,
        "max_size": settings.max_size,
    }
    print(json.dumps(report))
    return 0


def _forget(settings: Settings, target: str) -> int:
    key = _key_of_path(settings, target) if target.startswith("/") else target
    if key is None:
        print(f"error: the cache stores nothing for {target}", file=sys.stderr)
        return 1
    with _open(settings) as store:
        removed = store.forget(key)
    if not removed.entries:
        print(f"error: no entry has the key {key!r}", file=sys.stderr)
        return 1
    report = {"key": key, "deleted_blobs": removed.blobs, "freed_bytes": removed.bytes}
    print(json.dumps(report))
    return 0


def _key_of_path(settings: Settings, path: str) -> str | None:
    """The store key that the enabled adapters give a request path (without its query)."""
    prefix, _, tail = path.partition("?")[0].removeprefix("/").partition("/")
    adapter = build_adapters(settings).get(prefix)
    route = adapter.route(tail, {}) if adapter is not None else None
    return store_key(prefix, route.key) if isinstance(route, Fetch) else None


if __name__ == "__main__":
    sys.exit(main())
