# SPDX-License-Identifier: MIT
"""Go modules over the GOPROXY protocol, with the checksum database proxied too.

The ``go`` command uses the cache as its module proxy (``GOPROXY=<cache>/go``).
Because the cache answers ``sumdb/sum.golang.org/supported``, the ``go`` command
also reaches the checksum database through it (docs/PACKAGE-CACHE.md,
https://go.dev/ref/mod#goproxy-protocol and #checksum-database). Paths below
``/go/``:

``<module>/@v/list``, ``<module>/@latest``
    A module's versions: index data, revalidated after the TTL.
``<module>/@v/<version>.info``, ``<module>/@v/<version>.mod``
    Index data for one version: immutable.
``<module>/@v/<version>.zip``
    The module itself: an immutable artifact. The ``go`` command verifies it
    against the checksum database, so it carries no digest here.
``sumdb/sum.golang.org/supported``
    Built here: 200 with an empty body.
``sumdb/sum.golang.org/latest``
    The signed tree head: index data, revalidated after the TTL.
``sumdb/sum.golang.org/lookup/<module>@<version>``
    A checksum record: immutable.
``sumdb/sum.golang.org/tile/8/<level or data>/<index>[.p/<width>]``
    Log tiles: full tiles are immutable, partial tiles are revalidated.

Module paths and versions arrive case-encoded: ``!`` followed by a lowercase
letter stands for the uppercase letter, and uppercase letters themselves never
appear. The ``go`` command percent-encodes that ``!`` as ``%21`` (``url.PathEscape``),
so ``%21`` is the one percent-encoding accepted. Module paths are decoded and
checked as ``golang.org/x/mod/module`` does (``UnescapePath`` and ``CheckPath``).
Versions must be canonical, as the ``go`` command sends them for ``.info``,
``.mod``, ``.zip`` and lookups, so branch or commit queries (``@v/master.info``)
are refused. The escaped path goes upstream unchanged, with ``%21`` written as ``!``.
Only the ``sum.golang.org`` database is served.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from typing import Final

# The ignore is needed until package_cache ships a py.typed marker; strict pyright
# otherwise treats the contract module as an untyped library.
from sandbox_science.package_cache.adapter import (
    Fetch,
    Kind,
    Route,
    Static,
)

SUMDB_NAME: Final = "sum.golang.org"
"""The only checksum database the cache serves."""

TILE_HEIGHT: Final = 8
"""The tile height the ``go`` command reads sum.golang.org with (the sumdb client's default)."""

# Everything a valid path can contain once %21 is decoded: case-encoded module paths
# and versions are lowercase, and only versions carry "+" ("+incompatible").
_PATH_CHARS_RE: Final = re.compile(r"[a-z0-9._~!@+/-]+")

# golang.org/x/mod/module: modPathOK for every element, firstPathOK for the first.
_ELEMENT_CHARS: Final = frozenset(string.ascii_letters + string.digits + "-._~")
_FIRST_ELEMENT_CHARS: Final = frozenset(string.ascii_lowercase + string.digits + "-.")
_BAD_WINDOWS_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
_DIGITS_RE: Final = re.compile(r"[0-9]+")

# module.CanonicalVersion: "v" and three numbers, an optional pre-release (numeric
# identifiers without leading zeros), no build metadata except "+incompatible".
_NUMERIC: Final = r"(?:0|[1-9][0-9]*)"
_PRE_RELEASE_IDENTIFIER: Final = rf"(?:{_NUMERIC}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_CANONICAL_VERSION_RE: Final = re.compile(
    rf"v{_NUMERIC}\.{_NUMERIC}\.{_NUMERIC}"
    rf"(?:-{_PRE_RELEASE_IDENTIFIER}(?:\.{_PRE_RELEASE_IDENTIFIER})*)?"
    r"(?:\+incompatible)?"
)

# golang.org/x/mod/sumdb/tlog ParseTilePath, for height 8. The index is written in
# groups of three digits, all but the last prefixed with "x" ("x123/456"). A level
# above 7 cannot address any record of a log with fewer than 2^63 entries.
_TILE_RE: Final = re.compile(
    r"tile/8/(?P<level>data|[0-7])/(?P<index>(?:x[0-9]{3}/){0,6}[0-9]{3})"
    r"(?:\.p/(?P<width>[1-9][0-9]{0,2}))?"
)
_TILE_INDEX_LIMIT: Final = 1 << 63

_PROXY_FILES: Final = ((".info", Kind.METADATA), (".mod", Kind.METADATA), (".zip", Kind.ARTIFACT))
_SUMDB: Final = f"sumdb/{SUMDB_NAME}/"
_LOOKUP: Final = "lookup/"

_HOST: Final = r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])"
_BASE_URL_RE: Final = re.compile(rf"https://{_HOST}(?::[0-9]{{1,5}})?(?:/[A-Za-z0-9._~-]+)*")
"""An https base URL to append paths to: no user info, query, fragment or trailing slash."""


def unescape(escaped: str) -> str | None:
    """Undo the module proxy's case encoding (``module.unescapeString``).

    ``!x`` becomes ``X``. Returns None for an uppercase letter, a ``!`` that is not
    followed by a lowercase letter, or a non-ASCII character.
    """
    decoded: list[str] = []
    bang = False
    for char in escaped:
        if not char.isascii():
            return None
        if bang:
            bang = False
            if not "a" <= char <= "z":
                return None
            decoded.append(char.upper())
        elif char == "!":
            bang = True
        elif "A" <= char <= "Z":
            return None
        else:
            decoded.append(char)
    if bang:
        return None
    return "".join(decoded)


def _check_element(element: str) -> bool:
    """``module.checkElem`` for a module path element."""
    if not element or element.count(".") == len(element):
        return False
    if element[0] == "." or element[-1] == ".":
        return False
    if not set(element) <= _ELEMENT_CHARS:
        return False
    short = element.partition(".")[0]
    if short.lower() in _BAD_WINDOWS_NAMES:
        return False
    # Windows short names such as EXAMPL~1.
    tilde = short.rfind("~")
    return not (tilde >= 0 and _DIGITS_RE.fullmatch(short[tilde + 1 :]))


def _is_digit(char: str) -> bool:
    return "0" <= char <= "9"


def _split_path_version_ok(path: str) -> bool:
    """The ``ok`` result of ``module.SplitPathVersion``: a well-formed major version suffix."""
    if path.startswith("gopkg.in/"):
        return _gopkg_in_ok(path)
    i = len(path)
    dot = False
    while i > 0 and (_is_digit(path[i - 1]) or path[i - 1] == "."):
        dot = dot or path[i - 1] == "."
        i -= 1
    if i <= 1 or i == len(path) or path[i - 1] != "v" or path[i - 2] != "/":
        return True
    major = path[i - 2 :]
    return not (dot or len(major) <= 2 or major[2] == "0" or major == "/v1")


def _gopkg_in_ok(path: str) -> bool:
    """The ``ok`` result of ``module.splitGopkgIn``: gopkg.in paths end in ``.vN``."""
    i = len(path)
    if path.endswith("-unstable"):
        i -= len("-unstable")
    while i > 0 and _is_digit(path[i - 1]):
        i -= 1
    if i <= 1 or path[i - 1] != "v" or path[i - 2] != ".":
        return False
    major = path[i - 2 :]
    return not (len(major) <= 2 or (major[2] == "0" and major != ".v0"))


def check_module_path(path: str) -> bool:
    """Whether ``path`` is a valid module path (``module.CheckPath``).

    Elements use ASCII letters, digits and ``-._~``, never start or end with a
    dot, and are not Windows reserved or short names. The first element, by
    convention a domain name, is lowercase letters, digits, dots and dashes, has
    a dot and does not start with a dash. A ``/vN`` suffix has no leading zero,
    is not ``/v1`` and has no dots; gopkg.in paths end in ``.vN`` instead.
    """
    if not path or path[0] == "-" or "//" in path or path[-1] == "/":
        return False
    elements = path.split("/")
    if not all(_check_element(element) for element in elements):
        return False
    first = elements[0]
    if "." not in first or not set(first) <= _FIRST_ELEMENT_CHARS:
        return False
    return _split_path_version_ok(path)


def decode_module_path(escaped: str) -> str | None:
    """Return the module path for a case-encoded one, or None if either is invalid.

    ``github.com/!burnt!sushi/toml`` decodes to ``github.com/BurntSushi/toml``.
    """
    path = unescape(escaped)
    if path is None or not check_module_path(path):
        return None
    return path


def decode_version(escaped: str) -> str | None:
    """Return the canonical version for a case-encoded one, or None.

    Canonical is ``module.CanonicalVersion``'s form: ``v1.2.3``, ``v1.2.3-pre``,
    pseudo-versions, and any of these with ``+incompatible``.
    """
    version = unescape(escaped)
    if version is None or _CANONICAL_VERSION_RE.fullmatch(version) is None:
        return None
    return version


def _tile_is_partial(path: str) -> bool | None:
    """Whether a checksum database tile path names a partial tile; None if it is invalid."""
    match = _TILE_RE.fullmatch(path)
    if match is None:
        return None
    groups = match.group("index").split("/")
    # tlog writes the index without leading zero groups ("x000/123" is "123").
    if len(groups) > 1 and groups[0] == "x000":
        return None
    index = 0
    for group in groups:
        index = index * 1000 + int(group.removeprefix("x"))
    if index >= _TILE_INDEX_LIMIT:
        return None
    width: str | None = match.group("width")
    if width is not None and int(width) >= 1 << TILE_HEIGHT:
        return None
    return width is not None


def _base_url(value: str, *, setting: str) -> str:
    if _BASE_URL_RE.fullmatch(value) is None:
        raise ValueError(
            f"{setting} must be an absolute https URL without user info, query, "
            f"fragment or trailing slash, not {value!r}"
        )
    return value


class GoAdapter:
    """Go modules below ``/go/``: a module proxy and a checksum database proxy.

    ``proxy`` is the upstream module proxy (``https://proxy.golang.org``) and
    ``sumdb`` the checksum database (``https://sum.golang.org``). Neither may end
    with a slash.
    """

    prefix = "go"

    def __init__(self, *, proxy: str, sumdb: str) -> None:
        self._proxy = _base_url(proxy, setting="go proxy")
        self._sumdb = _base_url(sumdb, setting="go sumdb")
        self._supported = Static(body=b"", content_type="text/plain; charset=utf-8")

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        """Map a path below ``/go/`` to the cache's answer (see the module docstring)."""
        path = path.replace("%21", "!")
        if _PATH_CHARS_RE.fullmatch(path) is None:
            return None
        if path.startswith("sumdb/"):
            return self._checksum_database(path)
        # Module paths cannot contain "@", so the first "/@" ends the module path.
        escaped_module, separator, endpoint = path.partition("/@")
        if not separator or decode_module_path(escaped_module) is None:
            return None
        if endpoint in ("v/list", "latest"):
            return self._proxy_fetch(path, Kind.METADATA, immutable=False)
        if not endpoint.startswith("v/"):
            return None
        filename = endpoint.removeprefix("v/")
        for suffix, kind in _PROXY_FILES:
            if filename.endswith(suffix):
                if decode_version(filename.removesuffix(suffix)) is None:
                    return None
                return self._proxy_fetch(path, kind, immutable=True)
        return None

    def _checksum_database(self, path: str) -> Route:
        if not path.startswith(_SUMDB):
            return None
        relative = path.removeprefix(_SUMDB)
        if relative == "supported":
            return self._supported
        if relative == "latest":
            return self._sumdb_fetch(relative, immutable=False)
        if relative.startswith(_LOOKUP):
            escaped_module, at, escaped_version = relative.removeprefix(_LOOKUP).partition("@")
            if not at or decode_module_path(escaped_module) is None:
                return None
            if decode_version(escaped_version) is None:
                return None
            return self._sumdb_fetch(relative, immutable=True)
        partial = _tile_is_partial(relative)
        if partial is None:
            return None
        return self._sumdb_fetch(relative, immutable=not partial)

    def _proxy_fetch(self, path: str, kind: Kind, *, immutable: bool) -> Fetch:
        return Fetch(
            key=f"{self.prefix}/{path}",
            url=f"{self._proxy}/{path}",
            kind=kind,
            immutable=immutable,
        )

    def _sumdb_fetch(self, relative: str, *, immutable: bool) -> Fetch:
        return Fetch(
            key=f"{self.prefix}/{_SUMDB}{relative}",
            url=f"{self._sumdb}/{relative}",
            kind=Kind.METADATA,
            immutable=immutable,
        )
