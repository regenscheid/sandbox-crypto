# SPDX-License-Identifier: MIT
"""Command line: run the cache, and inspect or prune its store.

```
sandbox-science-package-cache serve --settings cache.toml [--log-level INFO]
sandbox-science-package-cache stats --settings cache.toml
sandbox-science-package-cache prune --settings cache.toml
```

``stats`` and ``prune`` print JSON and work on the store directly, so they may
run while the cache serves. ``prune`` evicts down to the size cap now, as the
cache does after storing a file, and deletes blobs no entry refers to that are
over an hour old (younger ones may be about to get their entry).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from aiohttp import web

from .app import create_app
from .errors import PackageCacheError, StoreError
from .settings import Settings, load_settings
from .store import ORPHAN_GRACE, Store
from .wiring import build_adapters

_log = logging.getLogger("sandbox_science.package_cache")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


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
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.settings)
        match args.command:
            case "serve":
                logging.basicConfig(
                    level=args.log_level,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                )
                return _serve(settings)
            case "stats":
                return _stats(settings)
            case _:
                return _prune(settings)
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
    )
    return 0


def _open(settings: Settings) -> Store:
    if not settings.cache_dir.is_dir():
        raise StoreError(f"there is no package cache in {settings.cache_dir}")
    return Store(settings.cache_dir, max_size=settings.max_size)


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


if __name__ == "__main__":
    sys.exit(main())
