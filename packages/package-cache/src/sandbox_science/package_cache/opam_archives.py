# SPDX-License-Identifier: MIT
"""Which opam packages an archive belongs to, from the opam index the cache holds.

opam fetches archives from the repository's archive cache by digest, so a
request for one names no package (docs/PACKAGE-CACHE.md, "URL layout").
``OpamArchives`` keeps a table from each checksum that the stored
``index.tar.gz`` lists to the packages whose opam files list it. The cache
answers ``GET /-/opam/archives/<algorithm>/<hex>`` from it, so that the policy
middleware can name the packages when it records an escalation.

The table is built in a worker thread (under a second for opam.ocaml.org's
index) when the cache starts and whenever it stores a new index, and is served
from memory. Until the first build has finished there is no table; while a
later one runs, the previous table answers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from .adapter import Digest
from .adapters.opam import archive_packages
from .errors import StoreError
from .store import Entry, Store

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Table:
    index: str
    """The SHA-256 of the index blob the table was built from."""
    packages: Mapping[str, tuple[str, ...]]


class OpamArchives:
    """A table from archive digests to opam packages, built from the index in a store.

    ``key`` is the store key of the opam index. Use it from the event loop only.
    """

    def __init__(self, store: Store, key: str) -> None:
        self._store = store
        self._key = key
        self._table: _Table | None = None
        self._unreadable: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._again = False

    @property
    def key(self) -> str:
        """The store key of the opam index the table is built from."""
        return self._key

    def packages(self, digest: Digest) -> tuple[str, ...] | None:
        """The ids of the packages whose opam files list ``digest``, sorted.

        Returns None while no table has been built, and starts a build.
        """
        if self._table is None:
            self.refresh()
            return None
        return self._table.packages.get(f"{digest.algorithm}:{digest.hex}", ())

    def refresh(self) -> None:
        """Rebuild the table in the background, if the stored index has changed."""
        if self._task is not None:
            self._again = True  # the running build may have read the index before it changed
            return
        self._task = asyncio.create_task(self._build())

    async def close(self) -> None:
        """Cancel a build in progress and wait for it to end."""
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _build(self) -> None:
        try:
            self._again = True
            while self._again:
                self._again = False
                await self._build_once()
        except StoreError as exc:
            _log.warning("cannot read the opam index: %s", exc)
        except Exception:
            _log.exception("cannot list the archives of the opam index")
        finally:
            self._task = None

    async def _build_once(self) -> None:
        entry = await asyncio.to_thread(self._store.get, self._key)
        if entry is None or entry.status != 200:
            return
        if entry.sha256 == self._unreadable or (
            self._table is not None and self._table.index == entry.sha256
        ):
            return
        try:
            packages = await asyncio.to_thread(_read, self._store, entry)
        except ValueError as exc:
            self._unreadable = entry.sha256  # not again for every lookup
            _log.warning("cannot read the opam index %s: %s", entry.sha256, exc)
            return
        if packages is None:
            return  # evicted meanwhile
        self._table = _Table(entry.sha256, packages)
        _log.info("listed %d opam archive digests from index %s", len(packages), entry.sha256)


def _read(store: Store, entry: Entry) -> dict[str, tuple[str, ...]] | None:
    """List the archives of a stored index, in one worker thread that owns the open blob."""
    file = store.open_blob(entry)
    if file is None:
        return None
    with file:
        return archive_packages(file)
