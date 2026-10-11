# SPDX-License-Identifier: MIT
"""opam packages from an opam HTTP repository, such as opam.ocaml.org.

opam uses the cache as its repository, ``<cache>/opam`` (docs/PACKAGE-CACHE.md).
opam 2 fetches two kinds of file from an HTTP repository. Paths below ``/opam/``:

``index.tar.gz``
    The whole repository: its ``repo`` and ``version`` files and an ``opam``
    file for every package version under ``packages/``. ``opam update``
    downloads it in full and reads the rest from it, so the cache serves no
    other repository file. Index data, revalidated after the TTL.
``cache/<algorithm>/<first two hex digits>/<hex digest>``
    A source archive, or an extra source such as a patch, from the repository's
    content-addressed archive cache. The ``repo`` file of opam.ocaml.org says
    ``archive-mirrors: "cache"``, so opam looks there, relative to the
    repository URL, before it tries the archive's own URL (often on GitHub).
    opam names the archive by the first checksum its opam file lists: ``md5``,
    ``sha256`` or ``sha512``, as lowercase hex. An immutable artifact, verified
    against that digest. The path names no package, so the policy decides it by
    ecosystem alone.

Everything else is refused, including opam 1's files (``urls.txt``,
``archives/``) and the repository's ``repo``, ``version`` and package files,
which opam reads from ``index.tar.gz``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final, Literal, cast

# The ignore is needed until package_cache ships a py.typed marker; strict pyright
# otherwise treats the contract module as an untyped library.
from sandbox_science.package_cache.adapter import (  # pyright: ignore[reportMissingTypeStubs]
    Digest,
    Fetch,
    Kind,
    Route,
)

Algorithm = Literal["md5", "sha256", "sha512"]

INDEX: Final = "index.tar.gz"
"""The repository index, the only index file opam 2 fetches from an HTTP repository."""

DIGEST_LENGTHS: Final[Mapping[Algorithm, int]] = {"md5": 32, "sha256": 64, "sha512": 128}
"""The checksums opam names archives by, and their length in hex digits."""

_ARCHIVE_RE: Final = re.compile(
    r"cache/(?P<algorithm>md5|sha256|sha512)/(?P<prefix>[0-9a-f]{2})/(?P<hex>[0-9a-f]+)",
    re.ASCII,
)

_HOST: Final = r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])"
_BASE_URL_RE: Final = re.compile(
    rf"https://{_HOST}(?::[0-9]{{1,5}})?(?:/[A-Za-z0-9._~-]+)*", re.ASCII
)
"""An https base URL to append paths to: no user info, query, fragment or trailing slash."""


def parse_archive_path(path: str) -> Digest | None:
    """The digest an archive cache path names, or None if it is not one.

    ``cache/sha256/ab/abcd...`` names SHA-256 ``abcd...``; the directory must be
    the digest's first two hex digits, as opam builds it (``OpamHash.to_path``).
    """
    match = _ARCHIVE_RE.fullmatch(path)
    if match is None:
        return None
    algorithm = cast("Algorithm", match["algorithm"])  # the pattern allows only these
    hex_digest = match["hex"]
    if len(hex_digest) != DIGEST_LENGTHS[algorithm] or hex_digest[:2] != match["prefix"]:
        return None
    return Digest(algorithm, hex_digest)


class OpamAdapter:
    """An opam HTTP repository below ``/opam/`` (see the module docstring).

    ``repository`` is the repository's base URL (``https://opam.ocaml.org``),
    without a trailing slash.
    """

    prefix = "opam"

    def __init__(self, *, repository: str) -> None:
        if _BASE_URL_RE.fullmatch(repository) is None:
            raise ValueError(
                "the opam repository must be an absolute https URL without user info, "
                f"query, fragment or trailing slash, not {repository!r}"
            )
        self._repository = repository

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        """Map a path below ``/opam/`` to the cache's answer (see the module docstring)."""
        if path == INDEX:
            return Fetch(
                key=f"{self.prefix}/{INDEX}",
                url=f"{self._repository}/{INDEX}",
                kind=Kind.METADATA,
                # Stored exactly as published: a server that labelled the archive
                # "Content-Encoding: gzip" would otherwise have it unpacked.
                headers={"Accept-Encoding": "identity"},
            )
        digest = parse_archive_path(path)
        if digest is None:
            return None
        return Fetch(
            # The digest is the archive's identity; the algorithm is part of it, since
            # opam names one file by several digests.
            key=f"{self.prefix}/cache/{digest.algorithm}/{digest.hex}",
            url=f"{self._repository}/{path}",
            kind=Kind.ARTIFACT,
            immutable=True,
            digest=digest,
        )
