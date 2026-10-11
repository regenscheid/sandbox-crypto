# SPDX-License-Identifier: MIT
"""The contract between the cache core and its ecosystem adapters.

An adapter turns a request path below its prefix (``/pypi/``, ``/conda/``, ...)
into an upstream fetch, a response it builds itself, or ``None`` for 404. It is
a pure function of the path, the request headers and its settings: the core
does all I/O, so adapters are tested without a network (docs/PACKAGE-CACHE.md).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Literal, Protocol

_HEX_LENGTHS: Final = {"sha256": 64, "sha512": 128}
_HEX_RE: Final = re.compile(r"[0-9a-f]+")


class Kind(StrEnum):
    """Index data, which may change, or a package file, which never does."""

    METADATA = "metadata"
    ARTIFACT = "artifact"


@dataclass(frozen=True, slots=True)
class Digest:
    """The digest an upstream body must have, as lowercase hex."""

    algorithm: Literal["sha256", "sha512"]
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
    upstreams. ``immutable`` bodies are kept until evicted and never
    revalidated. ``digest``, when given, is verified before anything is stored
    or served. ``rewrite`` applies to index data only.
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

    def __post_init__(self) -> None:
        if not self.url.startswith("https://"):
            raise ValueError("upstream URLs must use https")
        if self.rewrite is not None and self.kind is not Kind.METADATA:
            raise ValueError("only index data may be rewritten")


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
