# SPDX-License-Identifier: MIT
"""The contract between the cache core and its ecosystem adapters.

An adapter turns a request path below its prefix (``/pypi/``, ``/conda/``, ...)
into an upstream fetch, a response it builds itself, or ``None`` for 404. It is
a pure function of the path, the request headers and its settings: the core
does all I/O, so adapters are tested without a network (docs/PACKAGE-CACHE.md).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Literal, Protocol

_HEX_LENGTHS: Final = {"md5": 32, "sha256": 64, "sha512": 128}
_HEX_RE: Final = re.compile(r"[0-9a-f]+")
_HOST: Final = r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])"
_BASE_URL_RE: Final = re.compile(
    rf"https://{_HOST}(?::[0-9]{{1,5}})?(?:/[A-Za-z0-9._~-]+)*", re.ASCII
)

GZIP_MAGIC: Final = b"\x1f\x8b"
"""The first bytes of every gzip file (RFC 1952)."""

MAX_MAGIC: Final = 16
"""The longest ``Fetch.magic`` the cache checks."""


def check_base_url(url: str, what: str) -> str:
    """Return ``url`` if it is an https base URL an adapter can append paths to.

    No user info, query, fragment, percent-escapes or trailing slash. The
    settings model checks repository URLs with this too, so that a setting it
    accepts never stops an adapter from starting. Raises ValueError.
    """
    if _BASE_URL_RE.fullmatch(url) is None:
        raise ValueError(
            f"{what} must be an absolute https URL without user info, query, fragment, "
            f"percent-escapes or trailing slash, not {url!r}"
        )
    return url


class Kind(StrEnum):
    """Index data, which may change, or a package file, which never does."""

    METADATA = "metadata"
    ARTIFACT = "artifact"


@dataclass(frozen=True, slots=True)
class Digest:
    """The digest an upstream body must have, as lowercase hex.

    MD5 is accepted because opam addresses archives by the first checksum an opam
    file lists, which is MD5 for about a quarter of opam-repository; opam then
    checks every listed checksum itself, a stronger one included.
    """

    algorithm: Literal["md5", "sha256", "sha512"]
    hex: str

    def __post_init__(self) -> None:
        if len(self.hex) != _HEX_LENGTHS[self.algorithm] or not _HEX_RE.fullmatch(self.hex):
            raise ValueError(f"invalid {self.algorithm} digest {self.hex!r}")


def _no_headers() -> dict[str, str]:
    return {}


Rewrite = Callable[[bytes, str], bytes]
"""Rewrite an index body (decoded bytes, upstream content type) before it is stored.

It raises ValueError when the body cannot be parsed; the core then answers 502
and stores nothing.
"""


@dataclass(frozen=True, slots=True)
class Fetch:
    """Serve an upstream resource through the cache.

    ``key`` identifies the resource and response variant in the store, so two
    requests with the same key share one stored body: include anything that
    changes the upstream response (for example the negotiated content type).
    ``url`` is an absolute ``https://`` URL on one of the adapter's configured
    upstreams. ``immutable`` bodies, and artifacts, are kept until evicted and
    not revalidated, unless ``revalidate_after`` says otherwise. ``digest``,
    when given, is verified before anything is stored or served. ``rewrite``
    applies to index data only.
    """

    key: str
    url: str
    kind: Kind
    immutable: bool = False
    headers: Mapping[str, str] = field(default_factory=lambda: _no_headers())
    digest: Digest | None = None
    rewrite: Rewrite | None = None
    content_type: str | None = None
    """Override the content type served to clients (default: upstream's)."""
    max_age: float | None = None
    """Seconds a stored copy of mutable index data is served without asking upstream.

    At most the cache's ``metadata_ttl``, which applies when this is None. With 0
    the cache revalidates the copy on every request (a conditional request,
    answered 304 while it is unchanged), for files that clients check against
    each other, such as Hackage's signed metadata.
    """
    exact: bool = False
    """Store index data byte for byte as published, such as a compressed index archive.

    The cache asks for it with ``Accept-Encoding: identity``, never decodes it,
    and refuses an answer that names any content coding (502): a server that
    applied one changed the bytes, and one that mislabelled the file cannot be
    told apart from it. Artifacts are always stored as sent.
    """
    magic: bytes = b""
    """Bytes the body must start with, such as ``GZIP_MAGIC``; checked before it is stored."""
    revalidate_after: float | None = None
    """Seconds after which the cache revalidates a stored artifact that has no digest.

    The cache sends a conditional request with the stored validators: a 304
    confirms the copy, a 200 replaces it, and when upstream fails or no longer
    has the file, the stored copy is served. For artifacts the cache cannot
    verify, so that one bad download does not stay until it is evicted.
    """

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError("upstream URLs must use https")
        if self.rewrite is not None and self.kind is not Kind.METADATA:
            raise ValueError("only index data may be rewritten")
        if self.max_age is not None:
            if self.kind is not Kind.METADATA or self.immutable:
                raise ValueError("max_age applies to mutable index data only")
            if not math.isfinite(self.max_age) or self.max_age < 0:
                raise ValueError("max_age must be a finite number of seconds, at least 0")
        if self.exact and (self.kind is not Kind.METADATA or self.rewrite is not None):
            raise ValueError("exact applies to index data that is not rewritten")
        if len(self.magic) > MAX_MAGIC:
            raise ValueError(f"magic may be at most {MAX_MAGIC} bytes")
        if self.revalidate_after is not None:
            if self.kind is not Kind.ARTIFACT or self.digest is not None:
                raise ValueError("revalidate_after applies to artifacts without a digest")
            if not math.isfinite(self.revalidate_after) or self.revalidate_after <= 0:
                raise ValueError("revalidate_after must be a finite, positive number of seconds")


@dataclass(frozen=True, slots=True)
class Static:
    """A response the adapter builds itself, such as Cargo's ``config.json``."""

    body: bytes
    content_type: str
    status: int = 200


Route = Fetch | Static | None
"""What an adapter answers: fetch upstream, serve this, or 404."""


class Adapter(Protocol):
    """One package ecosystem, served below ``/<prefix>/``."""

    @property
    def prefix(self) -> str:
        """The first path segment this adapter serves, such as ``pypi``."""
        ...

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        """Map a request to an answer.

        ``path`` is the rest of the request path after ``/<prefix>/``, exactly
        as received (still percent-encoded, no query string). ``headers`` are
        the client's request headers with lowercased names. Validate strictly:
        anything unexpected returns None.
        """
        ...
