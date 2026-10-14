# SPDX-License-Identifier: MIT
"""Rust crates from crates.io over Cargo's sparse registry protocol.

Cargo reaches the cache through source replacement, as the sparse registry
``sparse+<cache>/cargo/index/`` (docs/PACKAGE-CACHE.md). Paths below ``/cargo/``:

``index/config.json``
    Built here. ``dl`` points back at the cache and puts the crate's checksum in
    the path. There is no ``api`` key, so publishing, yanking, owners and logins
    have nothing to talk to.
``index/<prefix>/<crate>``
    A crate's index file: index data, revalidated after the TTL. Cargo requests
    it by the lowercased crate name, in the layout of
    https://doc.rust-lang.org/cargo/reference/registry-index.html (``1/<name>``,
    ``2/<name>``, ``3/<first character>/<name>``, ``<first two>/<next two>/<name>``).
``dl/<crate>/<version>/<sha256>``
    A ``.crate`` file: an immutable artifact, verified against the index checksum
    that Cargo copied into the path. The crate name keeps the case the index
    gives it (``Inflector``), as crates.io stores the file under that name.

Crate names must follow crates.io's rules: ASCII letters, digits, ``-`` and
``_``, starting with a letter, at most 64 characters. Versions must be SemVer
2.0.0 as Cargo's ``semver`` crate parses them. Anything else is refused.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final

# The ignore is needed until package_cache ships a py.typed marker; strict pyright
# otherwise treats the contract module as an untyped library.
from sandbox_science.package_cache.adapter import (
    Digest,
    Fetch,
    Kind,
    Route,
    Static,
)

MAX_CRATE_NAME_LENGTH: Final = 64
"""crates.io's limit on the length of a crate name."""

_CRATE_NAME_RE: Final = re.compile(rf"[A-Za-z][A-Za-z0-9_-]{{0,{MAX_CRATE_NAME_LENGTH - 1}}}")
# Cargo lowercases the crate name before it builds an index path.
_INDEX_NAME_RE: Final = re.compile(rf"[a-z][a-z0-9_-]{{0,{MAX_CRATE_NAME_LENGTH - 1}}}")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")

# SemVer 2.0.0 as the semver crate (used by Cargo and crates.io) parses it: numeric
# parts without leading zeros, dot-separated pre-release identifiers whose numeric
# ones have no leading zeros, and dot-separated build metadata identifiers.
_NUMERIC: Final = r"(?:0|[1-9][0-9]*)"
_PRE_RELEASE_IDENTIFIER: Final = rf"(?:{_NUMERIC}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_BUILD_IDENTIFIER: Final = r"[0-9A-Za-z-]+"
_SEMVER_RE: Final = re.compile(
    rf"(?P<major>{_NUMERIC})\.(?P<minor>{_NUMERIC})\.(?P<patch>{_NUMERIC})"
    rf"(?:-{_PRE_RELEASE_IDENTIFIER}(?:\.{_PRE_RELEASE_IDENTIFIER})*)?"
    rf"(?:\+{_BUILD_IDENTIFIER}(?:\.{_BUILD_IDENTIFIER})*)?"
)
_U64_LIMIT: Final = 1 << 64
"""The semver crate stores major, minor and patch as u64 and rejects larger numbers."""

_HOST: Final = r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])"
_BASE_URL_RE: Final = re.compile(
    rf"(?P<scheme>https?)://{_HOST}(?::[0-9]{{1,5}})?(?:/[A-Za-z0-9._~-]+)*"
)
"""A base URL to append paths to: no user info, query, fragment or trailing slash.

Braces are excluded on purpose: Cargo substitutes ``{crate}`` and the other
markers anywhere in the ``dl`` template, including the part from ``public_url``.
"""

_INDEX: Final = "index/"
_DOWNLOAD: Final = "dl/"
_CONFIG: Final = "index/config.json"


def crate_index_path(name: str) -> str:
    """Return the index path of a crate whose name is already lowercase.

    This is Cargo's ``make_dep_path``: ``1/a``, ``2/cc``, ``3/s/syn``,
    ``se/rd/serde``.
    """
    match len(name):
        case 0:
            raise ValueError("a crate name cannot be empty")
        case 1:
            return f"1/{name}"
        case 2:
            return f"2/{name}"
        case 3:
            return f"3/{name[0]}/{name}"
        case _:
            return f"{name[0:2]}/{name[2:4]}/{name}"


def is_crate_name(name: str) -> bool:
    """Whether ``name`` follows crates.io's crate name rules (any letter case)."""
    return _CRATE_NAME_RE.fullmatch(name) is not None


def is_semver(version: str) -> bool:
    """Whether ``version`` is a SemVer 2.0.0 version Cargo can parse, such as ``0.11.0+wasi``."""
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        return False
    return all(int(match.group(part)) < _U64_LIMIT for part in ("major", "minor", "patch"))


def _base_url(value: str, *, setting: str, schemes: frozenset[str]) -> str:
    match = _BASE_URL_RE.fullmatch(value)
    if match is None or match.group("scheme") not in schemes:
        allowed = " or ".join(sorted(schemes))
        raise ValueError(
            f"{setting} must be an absolute {allowed} URL without user info, query, "
            f"fragment or trailing slash, not {value!r}"
        )
    return value


class CargoAdapter:
    """crates.io below ``/cargo/``, as a Cargo sparse registry.

    ``index`` is the sparse index (``https://index.crates.io``), ``download`` the
    base of the crate files (``https://static.crates.io/crates``), and
    ``public_url`` the cache's base URL as sandboxes reach it
    (``http://host.openshell.internal:8090``), for the ``dl`` link in
    ``config.json``. None of them may end with a slash.
    """

    prefix = "cargo"

    def __init__(self, *, index: str, download: str, public_url: str) -> None:
        https = frozenset({"https"})
        self._index = _base_url(index, setting="cargo index", schemes=https)
        self._download = _base_url(download, setting="cargo download", schemes=https)
        public = _base_url(public_url, setting="public_url", schemes=frozenset({"http", "https"}))
        dl = f"{public}/{self.prefix}/dl/{{crate}}/{{version}}/{{sha256-checksum}}"
        self._config = Static(
            body=json.dumps({"dl": dl}).encode("utf-8"), content_type="application/json"
        )

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        """Map a path below ``/cargo/`` to the cache's answer (see the module docstring)."""
        if path == _CONFIG:
            return self._config
        if path.startswith(_INDEX):
            return self._index_file(path.removeprefix(_INDEX))
        if path.startswith(_DOWNLOAD):
            return self._crate_file(path.removeprefix(_DOWNLOAD))
        return None

    def _index_file(self, relative: str) -> Route:
        name = relative.rpartition("/")[2]
        # Rebuilding the path from the name checks the directory levels and their
        # letters exactly, and refuses anything Cargo would not request.
        if _INDEX_NAME_RE.fullmatch(name) is None or relative != crate_index_path(name):
            return None
        return Fetch(
            key=f"{self.prefix}/{_INDEX}{relative}",
            url=f"{self._index}/{relative}",
            kind=Kind.METADATA,
        )

    def _crate_file(self, relative: str) -> Route:
        parts = relative.split("/")
        if len(parts) != 3:
            return None
        crate, version, checksum = parts
        if not is_crate_name(crate) or not is_semver(version):
            return None
        if _SHA256_RE.fullmatch(checksum) is None:
            return None
        # The form crates.io documents for downloads straight from its CDN. Cargo itself,
        # given crates.io's marker-less dl, asks for <dl>/<crate>/<version>/download,
        # which the CDN serves with the same bytes.
        return Fetch(
            key=f"{self.prefix}/{_DOWNLOAD}{crate}/{version}/{checksum}",
            url=f"{self._download}/{crate}/{crate}-{version}.crate",
            kind=Kind.ARTIFACT,
            immutable=True,
            digest=Digest("sha256", checksum),
        )
