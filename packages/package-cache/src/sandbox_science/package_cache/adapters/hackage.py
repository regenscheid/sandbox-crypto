# SPDX-License-Identifier: MIT
"""Haskell packages from Hackage, a hackage-security (TUF) repository.

cabal uses the cache as a secure repository whose URL is ``<cache>/hackage/``
(docs/PACKAGE-CACHE.md). cabal verifies everything it downloads against
Hackage's signed metadata itself (https://github.com/haskell/hackage-security),
so the cache serves every file unchanged. Paths below ``/hackage/``:

``root.json``, ``timestamp.json``, ``snapshot.json``, ``mirrors.json``
    The signed metadata. ``timestamp.json`` names the snapshot by hash and
    length, and the snapshot names the other files and the index the same way,
    so a client must get them all as they were at one moment. Each is index
    data that the cache revalidates on every request (``max_age`` 0: a
    conditional request, answered 304 while the file is unchanged). With any
    lifetime, the cache could pair a fresh file with an older one that it names
    differently, and cabal never asks caches to revalidate when it retries (its
    request headers lose ``Cache-Control``).
``01-index.tar.gz``
    The package index, about 140 MB, revalidated on every request like the
    metadata that vouches for it, and stored byte for byte (``Fetch.exact``).
    cabal updates its copy with one range request for the end of the file,
    which the cache answers from the stored copy (206).
``package/<name>-<version>.tar.gz`` and ``package/<name>-<version>/<name>-<version>.tar.gz``
    A source distribution: the first form is what cabal requests, the second is
    where Hackage serves it (it redirects the first). An artifact, fetched from
    the second form; both share one cache entry. cabal checks it against the
    length and SHA-256 that the signed index lists for it. The cache checks
    only that it is a gzip file, and revalidates it after
    ``SDIST_REVALIDATION`` with Hackage's ETag, so that a bad download does not
    stay until it is evicted (docs/PACKAGE-CACHE.md, "Verification").

Names and versions follow Cabal and Hackage: a name is ASCII words of letters
and digits joined by single hyphens, each word holding at least one letter, and
a version is numbers without leading zeros joined by dots. Everything else is
refused, including the uncompressed ``01-index.tar`` (about 1 GiB, which no
client fetches: hackage-security updates the compressed index), the unsigned
legacy ``00-index.tar.gz``, and Hackage's web pages and API.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

# The ignore is needed until package_cache ships a py.typed marker; strict pyright
# otherwise treats the contract module as an untyped library.
from sandbox_science.package_cache.adapter import (  # pyright: ignore[reportMissingTypeStubs]
    GZIP_MAGIC,
    Fetch,
    Kind,
    Route,
    check_base_url,
)

METADATA_FILES: Final = frozenset({"root.json", "timestamp.json", "snapshot.json", "mirrors.json"})
"""hackage-security's signed metadata."""

INDEX: Final = "01-index.tar.gz"
"""The package index that hackage-security downloads and updates."""

SDIST_REVALIDATION: Final = 86400.0
"""Seconds after which a stored source distribution is revalidated (a conditional request)."""

MAX_NAME_LENGTH: Final = 214
"""Longer package names are refused (Hackage's longest is 58 characters)."""

_WORD_RE: Final = re.compile(r"[A-Za-z0-9]+", re.ASCII)
_LETTER_RE: Final = re.compile(r"[A-Za-z]", re.ASCII)
# Up to 18 digits keeps every number within the Int that Cabal stores it in.
_VERSION_RE: Final = re.compile(
    r"(?:0|[1-9][0-9]{0,17})(?:\.(?:0|[1-9][0-9]{0,17})){0,31}", re.ASCII
)
_SDIST_SUFFIX: Final = ".tar.gz"
_PACKAGE: Final = "package/"


def is_package_name(name: str) -> bool:
    """Whether ``name`` is a Hackage package name, such as ``base64-bytestring``."""
    if not 0 < len(name) <= MAX_NAME_LENGTH:
        return False
    return all(
        _WORD_RE.fullmatch(word) is not None and _LETTER_RE.search(word) is not None
        for word in name.split("-")
    )


def is_version(version: str) -> bool:
    """Whether ``version`` is a Cabal version as Hackage publishes it, such as ``0.2.5``."""
    return _VERSION_RE.fullmatch(version) is not None


def parse_package_id(package_id: str) -> tuple[str, str] | None:
    """Split ``<name>-<version>`` into name and version, or return None.

    The split is unique: a version has no hyphen, and every word of a name has
    a letter, so the version is what follows the last hyphen.
    """
    name, hyphen, version = package_id.rpartition("-")
    if not hyphen or not is_package_name(name) or not is_version(version):
        return None
    return name, version


def parse_sdist_path(path: str) -> str | None:
    """The package id a source distribution path names, or None if it is not one.

    ``package/<id>.tar.gz`` and ``package/<id>/<id>.tar.gz`` (the same id twice).
    """
    if not path.startswith(_PACKAGE) or not path.endswith(_SDIST_SUFFIX):
        return None
    parts = path.removeprefix(_PACKAGE).removesuffix(_SDIST_SUFFIX).split("/")
    match parts:
        case [package_id]:
            pass
        case [package_id, again] if again == package_id:
            pass
        case _:
            return None
    return package_id if parse_package_id(package_id) is not None else None


class HackageAdapter:
    """A hackage-security repository below ``/hackage/`` (see the module docstring).

    ``repository`` is the repository's base URL (``https://hackage.haskell.org``),
    without a trailing slash.
    """

    prefix = "hackage"

    def __init__(self, *, repository: str) -> None:
        self._repository = check_base_url(repository, "the Hackage repository")

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        """Map a path below ``/hackage/`` to the cache's answer (see the module docstring)."""
        if path in METADATA_FILES:
            return self._index(path)
        if path == INDEX:
            # Stored exactly as published, as the signed snapshot describes it.
            return self._index(path, exact=True)
        package_id = parse_sdist_path(path)
        if package_id is None:
            return None
        return Fetch(
            key=f"{self.prefix}/{_PACKAGE}{package_id}{_SDIST_SUFFIX}",
            url=f"{self._repository}/{_PACKAGE}{package_id}/{package_id}{_SDIST_SUFFIX}",
            kind=Kind.ARTIFACT,
            immutable=True,
            # Nothing else is checked: cabal verifies it against the signed index.
            magic=GZIP_MAGIC,
            revalidate_after=SDIST_REVALIDATION,
        )

    def _index(self, path: str, *, exact: bool = False) -> Fetch:
        return Fetch(
            key=f"{self.prefix}/{path}",
            url=f"{self._repository}/{path}",
            kind=Kind.METADATA,
            exact=exact,
            magic=GZIP_MAGIC if exact else b"",
            max_age=0,
        )
