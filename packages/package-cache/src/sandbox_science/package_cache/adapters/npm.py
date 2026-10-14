# SPDX-License-Identifier: MIT
"""The npm registry adapter (docs/PACKAGE-CACHE.md, "URL layout").

Three request shapes below ``/npm/`` are served; everything else (search, audit, logins,
dist-tag and publish endpoints, the registry root) is refused:

* ``<name>``, ``@<scope>%2F<name>`` (or ``%2f``) and ``@<scope>/<name>``: the package's
  metadata (its "packument"), fetched from ``<registry>/<name>`` with the scope's slash
  written ``%2f``, as npm writes it.
* ``files/<sha512>/<name>/-/<file>``: a tarball, fetched from ``<registry>/<name>/-/<file>``.
  ``<sha512>`` is the lowercase hex SHA-512 the cache verifies, copied from the version's
  ``dist.integrity`` when the metadata was rewritten, or ``-`` when it had none. A scoped
  ``<name>`` keeps its literal slash here, as in the registry's own tarball URLs.
* ``<name>/-/<file>``, ``@<scope>/<name>/-/<file>`` and ``@<scope>%2F<name>/-/<file>`` (or
  ``%2f``): a tarball as a lockfile names it, fetched from ``<registry>/<name>/-/<file>``
  with the scope's slash literal. npm rewrites a lockfile's tarball URLs on the default
  registry to this path below the configured one, pnpm builds it from the name and
  version, and yarn 2 and later build it with the scope's slash written ``%2f``. It
  carries no digest, so the cache verifies nothing: the client checks the tarball against
  the lockfile's integrity. It is the ``files/-/<name>/-/<file>`` route spelled
  differently, and shares that route's cache entry, never one verified against a digest.

Names follow npm's rules for existing packages (``validate-npm-package-name``): legacy
names may contain capital letters and ``~'!()*``. They are never folded: the registry is
case-sensitive, and ``JSONStream`` and ``jsonstream`` are different packages.

A tarball ``<file>``, on either tarball route, must be a name npm gave that package's
tarballs: the unscoped name, ``-`` and a SemVer version, then ``.tgz``. Two legacy forms
that the registry still serves are accepted: a prerelease that starts with a letter may
lack its hyphen, as early npm wrote it (``express-1.0.0rc2.tgz`` holds version
``1.0.0-rc2``), and ``@`` may stand instead of ``-`` (``thirty-two@0.0.1.tgz``). The
policy decides by ``<name>``, so a tarball of another package is refused.

Metadata has two variants, chosen from the client's ``Accept`` header: the abbreviated
install document (``application/vnd.npm.install-v1+json``), which npm, pnpm and yarn 1 ask
for, and the full document (``application/json``), which the registry sends when a
client asks for it (``npm view``) or names no type (yarn 2 and later send no ``Accept``). The
rewrite parses the document, points every version's ``dist.tarball`` at the cache's
``files`` route and re-serializes it, leaving everything else as it was.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from typing import Final, cast
from urllib.parse import unquote, urlsplit

# The ignore is needed until package_cache ships a py.typed marker; strict pyright
# otherwise treats the contract module as an untyped library.
from sandbox_science.package_cache.adapter import (
    Digest,
    Fetch,
    Kind,
    Rewrite,
    Route,
)

__all__ = [
    "ABBREVIATED_METADATA",
    "FULL_METADATA",
    "MAX_NAME_LENGTH",
    "MAX_VERSION_LENGTH",
    "TARBALL_CONTENT_TYPE",
    "NpmAdapter",
    "NpmMetadataError",
]

ABBREVIATED_METADATA: Final = "application/vnd.npm.install-v1+json"
"""The abbreviated install metadata ("corgi") that npm, pnpm and yarn 1 ask for."""

FULL_METADATA: Final = "application/json"
"""The full package document, with times, readme and every manifest field."""

TARBALL_CONTENT_TYPE: Final = "application/octet-stream"
"""The content type served with tarballs, as the registry serves them."""

MAX_NAME_LENGTH: Final = 214
"""npm's limit on the length of a package name, scope included."""

MAX_VERSION_LENGTH: Final = 256
"""node-semver's limit on the length of a version."""

# Stored metadata is the rewritten document, and revalidating it keeps that document as
# long as the registry answers 304. Bump this whenever the rewrite's output changes, so
# that metadata rewritten by an older version of this module is fetched again.
_REWRITE_REVISION: Final = 1

# encodeURIComponent leaves exactly these characters alone; npm names contain no others.
_URL_SAFE: Final = r"[A-Za-z0-9._~!*'()-]"
# validate-npm-package-name: no leading ".", "_" or "-" (the last keeps names out of the
# registry's "/-/" namespace); in a scoped name, only the package part may not start
# with ".".
_UNSCOPED_NAME: Final = re.compile(r"[A-Za-z0-9~!*'()][A-Za-z0-9._~!*'()-]*")
_SCOPED_NAME: Final = re.compile(rf"@{_URL_SAFE}+/[A-Za-z0-9_~!*'()-]{_URL_SAFE}*")
_EXCLUDED_NAMES: Final = frozenset({"node_modules", "favicon.ico"})

_METADATA_PATH: Final = re.compile(
    rf"(?:@(?P<scope>{_URL_SAFE}+)(?:/|%2[Ff]))?(?P<package>{_URL_SAFE}+)"
)
_TARBALL_PATH: Final = re.compile(
    r"files/(?P<digest>[0-9a-f]{128}|-)/"
    rf"(?P<name>(?:@{_URL_SAFE}+/)?{_URL_SAFE}+)/-/(?P<file>[^/]+)"
)
# The registry's own tarball path, as lockfiles lead clients to it. No path matches both
# this and _TARBALL_PATH: a name has a slash only after an "@scope", so a match here that
# does not start with "@" has two slashes, and a files/ path has at least four.
_LOCKFILE_TARBALL_PATH: Final = re.compile(
    rf"(?P<name>(?:@{_URL_SAFE}+(?:/|%2[Ff]))?{_URL_SAFE}+)/-/(?P<file>[^/]+)"
)
_ENCODED_SLASH: Final = re.compile(r"%2[Ff]")

# SemVer 2.0, except that a prerelease starting with a letter may lack its hyphen
# (1.0.0rc2), the one relaxation the registry's tarball names need. Requiring the letter
# keeps the reading unique: node-semver's loose mode also takes 4.17.21.1, as 4.17.2-1.1.
_NUMERIC: Final = r"(?:0|[1-9][0-9]*)"
_IDENTIFIER: Final = rf"(?:{_NUMERIC}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_TARBALL_VERSION: Final = re.compile(
    rf"{_NUMERIC}\.{_NUMERIC}\.{_NUMERIC}"
    rf"(?:-{_IDENTIFIER}(?:\.{_IDENTIFIER})*|[A-Za-z][0-9A-Za-z-]*(?:\.{_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_TARBALL_SEPARATORS: Final = ("-", "@")

# Accept (RFC 9110, sections 5.6 and 12.5.1). Parameter values must be tokens: no client
# of the registry sends quoted strings, and a header that does selects nothing.
_OWS: Final = " \t"
_TCHARS: Final = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_MEDIA_RANGE: Final = re.compile(rf"(?P<type>{_TCHARS})/(?P<subtype>{_TCHARS})")
_PARAMETER: Final = re.compile(rf"(?P<name>{_TCHARS})=(?P<value>{_TCHARS})")
_QVALUE: Final = re.compile(r"0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?")

# Subresource Integrity, as npm's ssri reads it: whitespace-separated "<algorithm>-<base64>"
# tokens with optional "?<options>". A SHA-512 digest is 64 bytes, 88 base64 characters.
_SRI_WHITESPACE: Final = re.compile(r"[\t\n\f\r ]+")
_SHA512_BASE64: Final = re.compile(r"[A-Za-z0-9+/]{86}==")
_SRI_OPTIONS: Final = re.compile(r"[\x21-\x7e]*")


class NpmMetadataError(ValueError):
    """Package metadata the cache must not serve: unparsable, or with a link it cannot vouch for."""


class NpmAdapter:
    """Serve one npm registry's package metadata and tarballs (docs/PACKAGE-CACHE.md).

    ``registry`` is the registry's base URL (``https://registry.npmjs.org``); it must
    serve each package's tarballs at ``<registry>/<name>/-/<file>``, as the public
    registry does. ``public_url`` is the cache's base URL as sandboxes reach it
    (``http://host.openshell.internal:8090``); rewritten tarball links start with it.
    A trailing slash on either is ignored.
    """

    prefix = "npm"

    def __init__(self, *, registry: str, public_url: str) -> None:
        self._registry = _base_url(registry, "registry", schemes=("https",))
        self._public_url = _base_url(public_url, "public_url", schemes=("http", "https"))

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        """Map a request below ``/npm/`` to a metadata or tarball fetch, or None (404).

        ``path`` is still percent-encoded and ``headers`` have lowercased names. Only the
        ``Accept`` header is read; nothing the client sends is forwarded upstream.
        """
        if match := _TARBALL_PATH.fullmatch(path):
            digest = match.group("digest")
            return self._tarball(
                match.group("name"), match.group("file"), None if digest == "-" else digest
            )
        if match := _LOCKFILE_TARBALL_PATH.fullmatch(path):
            # The registry's tarball URLs keep the scope's slash literal.
            name = _ENCODED_SLASH.sub("/", match.group("name"))
            return self._tarball(name, match.group("file"), None)
        if path.startswith("files/"):
            return None  # the cache's own namespace: never a package's metadata
        return self._metadata(path, headers.get("accept"))

    def _metadata(self, path: str, accept: str | None) -> Route:
        match = _METADATA_PATH.fullmatch(path)
        if match is None:
            return None
        scope = match.group("scope")
        package = match.group("package")
        name = package if scope is None else f"@{scope}/{package}"
        if not _is_valid_name(name):
            return None
        variant = _negotiate(accept)
        if variant is None:
            return None
        url = f"{self._registry}/{name.replace('/', '%2f')}"
        label = "abbreviated" if variant == ABBREVIATED_METADATA else "full"
        # The stored body is the rewritten document, so the key names everything the
        # rewrite depends on, not only the upstream resource and variant.
        key = f"npm metadata {label} {url} rewritten for {self._public_url} r{_REWRITE_REVISION}"
        return Fetch(
            key=key,
            url=url,
            kind=Kind.METADATA,
            headers={"Accept": variant},
            rewrite=self._rewriter(name, variant),
            content_type=variant,
        )

    def _tarball(self, name: str, file: str, digest: str | None) -> Route:
        """Fetch ``name``'s tarball ``file``, verified against the SHA-512 ``digest`` if any."""
        if not _is_valid_name(name) or _tarball_version(name, file) is None:
            return None
        url = f"{self._registry}/{name}/-/{file}"
        return Fetch(
            # The digest is part of the key: a body verified against one digest is never
            # served for a path that names another digest, or none, and requests naming
            # different digests never share a fetch. Lockfile paths name none.
            key=f"npm tarball {digest or '-'} {url}",
            url=url,
            kind=Kind.ARTIFACT,
            immutable=True,
            digest=None if digest is None else Digest("sha512", digest),
            content_type=TARBALL_CONTENT_TYPE,
        )

    def _rewriter(self, name: str, variant: str) -> Rewrite:
        def rewrite(body: bytes, content_type: str) -> bytes:
            return self._rewrite(name, variant, body, content_type)

        return rewrite

    def _rewrite(self, name: str, variant: str, body: bytes, content_type: str) -> bytes:
        received = content_type.partition(";")[0].strip(_OWS).lower()
        if received not in (ABBREVIATED_METADATA, FULL_METADATA):
            raise NpmMetadataError(f"{name}: the registry sent {_show(content_type)}, not metadata")
        # A registry that ignores the abbreviated Accept header sends the full document,
        # which holds everything the abbreviated one does. The reverse would lose fields.
        if variant == FULL_METADATA and received != FULL_METADATA:
            raise NpmMetadataError(f"{name}: the registry sent abbreviated metadata for full")
        document = _object(_parse_json(body), f"{name}: metadata")
        if document.get("name") != name:
            raise NpmMetadataError(f"{name}: the metadata is for {_show(document.get('name'))}")
        if "versions" in document:
            versions = _object(document["versions"], f"{name}: versions")
            for version, manifest in versions.items():
                context = f"{name}@{_show(version)}"
                dist = _object(_object(manifest, context).get("dist"), f"{context}: dist")
                dist["tarball"] = self._cache_tarball_url(name, dist, context)
        return _dump_json(document)

    def _cache_tarball_url(self, name: str, dist: dict[str, object], context: str) -> str:
        """The cache's link for ``dist.tarball``, which must be one of ``name``'s tarballs."""
        tarball = dist.get("tarball")
        if not isinstance(tarball, str):
            raise NpmMetadataError(f"{context}: dist.tarball is not a string")
        directory = f"{self._registry}/{name}/-/"
        if not tarball.startswith(directory):
            raise NpmMetadataError(f"{context}: tarball {_show(tarball)} is not on the registry")
        file = tarball[len(directory) :]
        if _tarball_version(name, file) is None:
            raise NpmMetadataError(f"{context}: {_show(file)} is not a tarball name of {name}")
        digest = _sha512_hex(dist.get("integrity"), context)
        return f"{self._public_url}/{self.prefix}/files/{digest or '-'}/{name}/-/{file}"


def _base_url(value: str, setting: str, *, schemes: tuple[str, ...]) -> str:
    """``value`` normalized as the settings normalize URLs, if paths can be appended to it.

    The settings have validated it already; this guards what the adapter relies on: a
    host, no credentials, query, fragment or dot segments, and no whitespace (cache keys
    separate their fields with spaces).
    """
    parts = urlsplit(value)
    try:
        port_ok = parts.port != 0
    except ValueError:
        port_ok = False
    if (
        not value.isascii()
        or any(not char.isprintable() or char.isspace() for char in value)
        or "?" in value
        or "#" in value
        or parts.scheme not in schemes
        or not parts.hostname
        or "@" in parts.netloc
        or not port_ok
        or any(unquote(segment) in (".", "..") for segment in parts.path.split("/"))
    ):
        raise ValueError(
            f"npm {setting} must be an absolute {' or '.join(schemes)} URL without"
            f" credentials, query, fragment or dot segments: {value!r}"
        )
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def _is_valid_name(name: str) -> bool:
    """Whether npm accepts ``name`` for an existing package (validate-npm-package-name)."""
    if len(name) > MAX_NAME_LENGTH or name.lower() in _EXCLUDED_NAMES:
        return False
    return _UNSCOPED_NAME.fullmatch(name) is not None or _SCOPED_NAME.fullmatch(name) is not None


def _tarball_version(name: str, file: str) -> str | None:
    """The version in ``file`` if it is a tarball name npm gave ``name``, else None."""
    if not file.endswith(".tgz"):
        return None
    stem = file.removesuffix(".tgz")
    unscoped = name.rpartition("/")[2]
    for separator in _TARBALL_SEPARATORS:
        start = unscoped + separator
        if stem.startswith(start):
            version = stem[len(start) :]
            if len(version) <= MAX_VERSION_LENGTH and _TARBALL_VERSION.fullmatch(version):
                return version
    return None


def _negotiate(accept: str | None) -> str | None:
    """Choose the metadata variant for an ``Accept`` header, or None when it allows neither.

    The registry sends the abbreviated document only to clients that name its media type,
    and the full one otherwise, also when there is no ``Accept`` header; a wildcard
    therefore stands for the full document. The variant with the higher quality wins,
    and a tie goes to the abbreviated document. A malformed header selects nothing.
    """
    if accept is None or not accept.strip(_OWS):
        return FULL_METADATA
    ranges = _parse_accept(accept)
    if ranges is None:
        return None
    abbreviated = _quality(ranges, ABBREVIATED_METADATA, named_only=True)
    full = _quality(ranges, FULL_METADATA, named_only=False)
    if abbreviated > 0 and abbreviated >= full:
        return ABBREVIATED_METADATA
    if full > 0:
        return FULL_METADATA
    return None


def _parse_accept(accept: str) -> list[tuple[str, str, int]] | None:
    """Media ranges as (type, subtype, quality in thousandths), or None if malformed."""
    ranges: list[tuple[str, str, int]] = []
    for element in accept.split(","):
        if not element.strip(_OWS):
            continue  # lists may contain empty elements (RFC 9110, section 5.6.1)
        media_range, *parameters = element.split(";")
        match = _MEDIA_RANGE.fullmatch(media_range.strip(_OWS))
        if match is None:
            return None
        main = match.group("type").lower()
        sub = match.group("subtype").lower()
        if main == "*" and sub != "*":
            return None
        quality: int | None = None
        for parameter in parameters:
            if not parameter.strip(_OWS):
                continue  # "type/subtype;" is allowed (RFC 9110, section 5.6.6)
            parsed = _PARAMETER.fullmatch(parameter.strip(_OWS))
            if parsed is None:
                return None
            if parsed.group("name").lower() != "q":
                continue
            value = parsed.group("value")
            if quality is not None or not _QVALUE.fullmatch(value):
                return None
            whole, _, fraction = value.partition(".")
            quality = int(whole) * 1000 + int(fraction.ljust(3, "0"))
        ranges.append((main, sub, 1000 if quality is None else quality))
    return ranges


def _quality(ranges: list[tuple[str, str, int]], media_type: str, *, named_only: bool) -> int:
    """The quality the most specific matching range gives ``media_type`` (0 if none)."""
    main, _, sub = media_type.partition("/")
    best: tuple[int, int] | None = None  # (specificity, quality)
    for range_main, range_sub, quality in ranges:
        if (range_main, range_sub) == (main, sub):
            specificity = 2
        elif named_only:
            continue
        elif (range_main, range_sub) == (main, "*"):
            specificity = 1
        elif (range_main, range_sub) == ("*", "*"):
            specificity = 0
        else:
            continue
        if best is None or (specificity, quality) > best:
            best = (specificity, quality)
    return 0 if best is None else best[1]


def _sha512_hex(integrity: object, context: str) -> str | None:
    """The SHA-512 in an SRI string as lowercase hex, or None when it has none.

    Absent or null integrity counts as none, as it does for npm, which then checks the
    SHA-1 ``shasum`` itself. A malformed SHA-512 token, or two different ones, is an
    error: the cache could not verify the tarball the way the client will.
    """
    if integrity is None:
        return None
    if not isinstance(integrity, str):
        raise NpmMetadataError(f"{context}: dist.integrity is not a string")
    digests: set[str] = set()
    for token in _SRI_WHITESPACE.split(integrity):
        algorithm, separator, value = token.partition("-")
        if algorithm != "sha512" or not separator:
            continue
        encoded, _, options = value.partition("?")
        if not _SHA512_BASE64.fullmatch(encoded) or not _SRI_OPTIONS.fullmatch(options):
            raise NpmMetadataError(f"{context}: malformed sha512 integrity {_show(token)}")
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise NpmMetadataError(f"{context}: non-canonical sha512 integrity {_show(token)}")
        digests.add(raw.hex())
    if len(digests) > 1:
        raise NpmMetadataError(f"{context}: conflicting sha512 integrity values")
    return digests.pop() if digests else None


def _parse_json(body: bytes) -> object:
    try:
        document: object = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except NpmMetadataError:
        raise
    except RecursionError as error:
        raise NpmMetadataError("metadata is nested too deeply") from error
    except ValueError as error:  # UnicodeDecodeError, JSONDecodeError, too many digits
        raise NpmMetadataError(f"metadata is not UTF-8 JSON: {error}") from error
    return document


def _reject_constant(constant: str) -> object:
    raise NpmMetadataError(f"metadata contains {constant}, which JSON does not allow")


def _dump_json(document: dict[str, object]) -> bytes:
    # ASCII escapes keep any string JSON can carry, lone surrogates included.
    try:
        text = json.dumps(document, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except RecursionError as error:
        raise NpmMetadataError("metadata is nested too deeply") from error
    except ValueError as error:  # numbers that do not fit a double, such as 1e999
        raise NpmMetadataError(f"metadata cannot be serialized: {error}") from error
    return text.encode("ascii")


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise NpmMetadataError(f"{what} is not a JSON object")
    return cast("dict[str, object]", value)


def _show(value: object) -> str:
    """A short, escaped rendering of untrusted data for error messages."""
    text = repr(value[:100] if isinstance(value, str) else value)
    return text if len(text) <= 120 else text[:117] + "..."
