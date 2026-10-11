# SPDX-License-Identifier: MIT
"""opam packages from an opam HTTP repository, such as opam.ocaml.org.

opam uses the cache as its repository, ``<cache>/opam`` (docs/PACKAGE-CACHE.md).
opam 2 fetches two kinds of file from an HTTP repository. Paths below ``/opam/``:

``index.tar.gz``
    The whole repository: its ``repo`` and ``version`` files and an ``opam``
    file for every package version under ``packages/``. ``opam update``
    downloads it in full and reads the rest from it, so the cache serves no
    other repository file. Index data, stored byte for byte (``Fetch.exact``)
    and revalidated after the TTL.
``cache/<algorithm>/<first two hex digits>/<hex digest>``
    A source archive, or an extra source such as a patch, from the repository's
    content-addressed archive cache. The ``repo`` file of opam.ocaml.org says
    ``archive-mirrors: "cache"``, so opam looks there, relative to the
    repository URL, before it tries the archive's own URL (often on GitHub).
    opam names the archive by the first checksum its opam file lists: ``md5``,
    ``sha256`` or ``sha512``, as lowercase hex. An immutable artifact, verified
    against that digest. The path names no package, so the policy decides it by
    ecosystem alone; ``archive_packages`` finds the packages in the index.

Everything else is refused, including opam 1's files (``urls.txt``,
``archives/``) and the repository's ``repo``, ``version`` and package files,
which opam reads from ``index.tar.gz``.
"""

from __future__ import annotations

import re
import tarfile
import zlib
from collections.abc import Mapping
from typing import IO, Final, Literal, cast

# The ignore is needed until package_cache ships a py.typed marker; strict pyright
# otherwise treats the contract module as an untyped library.
from sandbox_science.package_cache.adapter import (  # pyright: ignore[reportMissingTypeStubs]
    GZIP_MAGIC,
    Digest,
    Fetch,
    Kind,
    Route,
    check_base_url,
)

Algorithm = Literal["md5", "sha256", "sha512"]

INDEX: Final = "index.tar.gz"
"""The repository index, the only index file opam 2 fetches from an HTTP repository."""

INDEX_KEY: Final = f"opam/{INDEX}"
"""The adapter's cache key for the repository index."""

DIGEST_LENGTHS: Final[Mapping[Algorithm, int]] = {"md5": 32, "sha256": 64, "sha512": 128}
"""The checksums opam names archives by, and their length in hex digits."""

MAX_INDEX_BYTES: Final = 512 * 1024**2
"""The most bytes ``archive_packages`` reads from an unpacked index (opam.ocaml.org's: 54 MB)."""

MAX_OPAM_FILE_BYTES: Final = 1024**2
"""Larger opam files are skipped (opam-repository's largest is 24 KB)."""

_ARCHIVE_RE: Final = re.compile(
    r"cache/(?P<algorithm>md5|sha256|sha512)/(?P<prefix>[0-9a-f]{2})/(?P<hex>[0-9a-f]+)",
    re.ASCII,
)
# packages/<name>/<name>.<version>/opam, with opam's characters for names and versions.
_OPAM_FILE_RE: Final = re.compile(
    r"packages/(?P<name>[0-9_+-]*[A-Za-z][A-Za-z0-9_+-]*)"
    r"/(?P<id>(?P=name)\.[A-Za-z0-9_+.~-]+)/opam",
    re.ASCII,
)
_CHECKSUM_RE: Final = re.compile(rb'"(md5|sha256|sha512)=([0-9a-f]{32,128})"')
_TAR_HEADER: Final = 512


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


def archive_packages(
    index: IO[bytes], *, max_bytes: int = MAX_INDEX_BYTES
) -> dict[str, tuple[str, ...]]:
    """Map each archive checksum the opam files in ``index`` list to the packages listing it.

    ``index`` is a repository's ``index.tar.gz``. Keys are ``<algorithm>:<hex>``
    and values the sorted ids (``<name>.<version>``) of the packages whose opam
    file lists that checksum: an archive cache path names the first checksum of
    a ``url`` or ``extra-source`` section, and packages share archives (the
    packages of one project, a compiler's variants). Raises ValueError when
    ``index`` is not a gzip-compressed tar file, or unpacks to more than
    ``max_bytes``.
    """
    found: dict[str, set[str]] = {}
    total = 0
    try:
        with tarfile.open(fileobj=index, mode="r|gz") as tar:
            for member in tar:
                # Skipping a member still unpacks it, so every byte counts.
                total += _TAR_HEADER + member.size
                if total > max_bytes:
                    raise ValueError(f"the index unpacks to more than {max_bytes} bytes")
                match = _OPAM_FILE_RE.fullmatch(member.name)
                if match is None or not member.isfile() or member.size > MAX_OPAM_FILE_BYTES:
                    continue
                file = tar.extractfile(member)
                if file is None:
                    continue
                for algorithm, hex_digest in _CHECKSUM_RE.findall(file.read()):
                    name = cast("Algorithm", algorithm.decode())  # the pattern allows only these
                    if len(hex_digest) == DIGEST_LENGTHS[name]:
                        found.setdefault(f"{name}:{hex_digest.decode()}", set()).add(match["id"])
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise ValueError(f"not a gzip-compressed tar file: {exc}") from exc
    return {digest: tuple(sorted(ids)) for digest, ids in found.items()}


class OpamAdapter:
    """An opam HTTP repository below ``/opam/`` (see the module docstring).

    ``repository`` is the repository's base URL (``https://opam.ocaml.org``),
    without a trailing slash.
    """

    prefix = "opam"

    def __init__(self, *, repository: str) -> None:
        self._repository = check_base_url(repository, "the opam repository")

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        """Map a path below ``/opam/`` to the cache's answer (see the module docstring)."""
        if path == INDEX:
            return Fetch(
                key=INDEX_KEY,
                url=f"{self._repository}/{INDEX}",
                kind=Kind.METADATA,
                # Stored exactly as published: decoding would unpack an archive that
                # a server labelled "Content-Encoding: gzip", so such answers are refused.
                exact=True,
                magic=GZIP_MAGIC,
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
