# SPDX-License-Identifier: MIT
"""PyPI through the simple repository API (PEP 503, 691 and their successors).

pip and uv use the cache as their index, ``<cache>/pypi/simple/``
(docs/PACKAGE-CACHE.md). Paths below ``/pypi/``:

``simple/<project>/``
    One project's page: index data, revalidated after the TTL. The name is
    normalized (PEP 503), so ``simple/Typing_Extensions/`` serves
    ``typing-extensions``. The format is negotiated from the client's ``Accept``
    header (see :func:`negotiate`) and requested upstream in that format. The
    page is parsed and serialized again, never edited as text. Every file link
    then points back at the cache, and only the fields defined up to API version
    1.4 are kept. A link to anywhere but the files host fails the whole page.
    Files the cache would refuse to serve (see below) are left out of it.
``files/<project>/<sha256 or ->/<path>``
    A distribution file at ``<files>/<path>``, where ``<path>`` has the layout of
    files.pythonhosted.org, ``packages/<2 hex>/<2 hex>/<60 hex>/<file name>``. It
    is an immutable artifact, verified against the SHA-256 the index published
    (``-`` when it published none). The digest is part of the cache key, so
    requests naming different digests, or none, never share a fetch or a stored
    file. The policy decides by ``<project>``, so the file must be a wheel or
    sdist of that project (:func:`is_distribution_of`).
``files/<project>/<sha256 or ->/<path>.metadata``
    The distribution's core metadata (PEP 658): immutable index data. The digest
    in the path is the distribution's, so the client checks the metadata against
    the hash the page gives for it. The cache verifies nothing here, so every
    digest in the path shares the one stored copy of the upstream file.

Everything else is refused, including the project list ``simple/``.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final, Literal, cast
from urllib.parse import urljoin, urlsplit

# The ignore is needed until package_cache ships a py.typed marker; strict pyright
# otherwise treats the contract module as an untyped library.
from sandbox_science.package_cache.adapter import (
    Digest,
    Fetch,
    Kind,
    Route,
    Static,
)

JSON_TYPE: Final = "application/vnd.pypi.simple.v1+json"
HTML_TYPE: Final = "application/vnd.pypi.simple.v1+html"
TEXT_HTML_TYPE: Final = "text/html"
"""PEP 691's legacy alias for :data:`HTML_TYPE`."""

MAX_FILENAME_LENGTH: Final = 255
"""Longer file names are refused; files are created on disk before upload."""

_REWRITE_VERSION: Final = 1
"""Part of every page key. Bump it when rewritten pages change, so stored ones are dropped."""

_API_MINOR: Final = 4
"""The rewrite keeps the fields of API versions up to 1.4 (PEP 792)."""

_PROJECT_NAME_RE: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", re.ASCII)
_SEPARATORS_RE: Final = re.compile(r"[-_.]+")
_PAGE_RE: Final = re.compile(rf"(?P<name>{_PROJECT_NAME_RE.pattern})/", re.ASCII)
_FILE_NAME: Final = r"[A-Za-z0-9][A-Za-z0-9._+!-]*"
_FILE_NAME_RE: Final = re.compile(_FILE_NAME, re.ASCII)
_UPSTREAM_PATH_RE: Final = re.compile(
    r"packages/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{60}/(?P<filename>[^/]+)", re.ASCII
)
_FILES_RE: Final = re.compile(
    r"(?P<project>[a-z0-9]+(?:-[a-z0-9]+)*)/(?P<digest>-|[0-9a-f]{64})/"
    rf"(?P<path>packages/[0-9a-f]{{2}}/[0-9a-f]{{2}}/[0-9a-f]{{60}}/(?P<filename>{_FILE_NAME}))",
    re.ASCII,
)
_SDIST_SUFFIXES: Final = (".tar.gz", ".zip", ".tar.bz2", ".tgz")
_METADATA_SUFFIX: Final = ".metadata"

# PEP 440, appendix B, without the named groups: the version part of an sdist name.
_VERSION_RE: Final = re.compile(
    r"""
    v?
    (?:[0-9]+!)?                                                # epoch
    [0-9]+(?:\.[0-9]+)*                                         # release
    (?:[-_.]?(?:alpha|a|beta|b|preview|pre|c|rc)[-_.]?[0-9]*)?  # pre-release
    (?:-[0-9]+|[-_.]?(?:post|rev|r)[-_.]?[0-9]*)?               # post-release
    (?:[-_.]?dev[-_.]?[0-9]*)?                                  # development release
    (?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?                         # local version
    """,
    re.VERBOSE | re.IGNORECASE | re.ASCII,
)

_HOST: Final = r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])"
_BASE_URL_RE: Final = re.compile(
    rf"(?P<scheme>https?)://{_HOST}(?::[0-9]{{1,5}})?(?:/[A-Za-z0-9._~-]+)*", re.ASCII
)
"""A base URL to append paths to: no user info, query, fragment or trailing slash."""

_HEX_RE: Final = re.compile(r"[0-9a-fA-F]+", re.ASCII)
_SHA256_RE: Final = re.compile(r"[0-9a-fA-F]{64}", re.ASCII)
_HASH_NAME_RE: Final = re.compile(r"[A-Za-z0-9_]+", re.ASCII)
_FRAGMENT_RE: Final = re.compile(r"(?P<name>[a-z0-9_]+)=(?P<value>[0-9a-fA-F]+)", re.ASCII)
_HASH_ATTRIBUTE_RE: Final = re.compile(r"[a-z0-9_]+=[0-9a-fA-F]+", re.ASCII)
_API_VERSION_RE: Final = re.compile(r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)", re.ASCII)
_STATUS_RE: Final = re.compile(r"[a-z][a-z0-9-]*", re.ASCII)
_UPLOAD_TIME_RE: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", re.ASCII
)
_HTML_SPACE: Final = " \t\n\f\r"

# RFC 9110: media ranges and quality values in Accept.
_TCHARS: Final = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_MEDIA_RANGE_RE: Final = re.compile(rf"(?P<type>{_TCHARS})/(?P<subtype>{_TCHARS})", re.ASCII)
_QVALUE_RE: Final = re.compile(r"0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?", re.ASCII)

_NOT_ACCEPTABLE: Final = Static(
    body=(
        f"406 Not Acceptable: this index serves {JSON_TYPE}, {HTML_TYPE} and {TEXT_HTML_TYPE}\n"
    ).encode("ascii"),
    content_type="text/plain; charset=utf-8",
    status=406,
)


def normalize_name(name: str) -> str:
    """Normalize a project name as PEP 503 does: lower case, runs of ``-_.`` become ``-``."""
    return _SEPARATORS_RE.sub("-", name).lower()


def is_distribution_of(filename: str, project: str) -> bool:
    """Whether ``filename`` is a wheel or sdist of the project with normalized name ``project``.

    A wheel names its distribution before the first ``-`` (wheel names escape
    ``-`` as ``_``). An sdist is ``<name>-<version>`` plus ``.tar.gz``, ``.zip``,
    ``.tar.bz2`` or ``.tgz``. Older sdists may keep ``-`` in the name, so some
    split of the stem must give a name that normalizes to ``project`` and a PEP
    440 version. Other formats (eggs, Windows installers, RPMs) are refused:
    pip and uv do not install them from an index.
    """
    if len(filename) > MAX_FILENAME_LENGTH or _FILE_NAME_RE.fullmatch(filename) is None:
        return False
    if filename.endswith(".whl"):
        parts = filename.removesuffix(".whl").split("-")
        return len(parts) in (5, 6) and all(parts) and normalize_name(parts[0]) == project
    for suffix in _SDIST_SUFFIXES:
        if filename.endswith(suffix):
            stem = filename.removesuffix(suffix)
            return any(
                normalize_name(stem[:index]) == project
                and _VERSION_RE.fullmatch(stem[index + 1 :]) is not None
                for index, char in enumerate(stem)
                if char == "-"
            )
    return False


@dataclass(frozen=True, slots=True)
class _Offer:
    """A format the index can answer in, and how much the cache prefers it on a tie."""

    content_type: str
    names: frozenset[str]
    """The media types that name this format (``latest`` stands for v1)."""
    named_rank: int
    """Preference among formats the client named: JSON first, ``text/html`` last."""
    wildcard_rank: int
    """Preference among formats only a wildcard matched: ``text/html`` first, as on PyPI."""


_OFFERS: Final = (
    _Offer(JSON_TYPE, frozenset({JSON_TYPE, "application/vnd.pypi.simple.latest+json"}), 3, 1),
    _Offer(HTML_TYPE, frozenset({HTML_TYPE, "application/vnd.pypi.simple.latest+html"}), 2, 2),
    _Offer(TEXT_HTML_TYPE, frozenset({TEXT_HTML_TYPE}), 1, 3),
)


def _parse_accept(value: str) -> list[tuple[str, str, int]]:
    """Return the media ranges of an Accept header as (type, subtype, quality in thousandths).

    Malformed ranges are skipped. Parameters other than the weight are ignored.
    """
    ranges: list[tuple[str, str, int]] = []
    for element in value.split(","):
        media_range, *parameters = element.split(";")
        match = _MEDIA_RANGE_RE.fullmatch(media_range.strip(" \t"))
        if match is None:
            continue
        type_, subtype = match["type"].lower(), match["subtype"].lower()
        if type_ == "*" and subtype != "*":
            continue
        quality: int | None = 1000
        for parameter in parameters:
            name, separator, raw = parameter.partition("=")
            if name.strip(" \t").lower() != "q":
                continue
            raw = raw.strip(" \t")
            if not separator or _QVALUE_RE.fullmatch(raw) is None:
                quality = None
            else:
                whole, _, fraction = raw.partition(".")
                quality = int(whole) * 1000 + int((fraction + "000")[:3])
            break  # anything after the weight is an extension, which has no meaning here
        if quality is not None:
            ranges.append((type_, subtype, quality))
    return ranges


def _quality(offer: _Offer, ranges: list[tuple[str, str, int]]) -> tuple[int, bool]:
    """Return the offer's quality from its most specific matching range, and whether it is named."""
    specificity, quality = -1, 0
    for type_, subtype, range_quality in ranges:
        if f"{type_}/{subtype}" in offer.names:
            level = 2
        elif type_ != "*" and subtype == "*" and offer.content_type.startswith(f"{type_}/"):
            level = 1
        elif type_ == "*" and subtype == "*":
            level = 0
        else:
            continue
        if level > specificity:
            specificity, quality = level, range_quality
        elif level == specificity:
            quality = max(quality, range_quality)
    return quality, specificity == 2


def negotiate(accept: str | None) -> str | None:
    """Choose the content type of a project page, or None when the client accepts none.

    Quality values decide (RFC 9110; the most specific matching range sets a
    format's quality). On a tie, a format the client names beats one that only a
    wildcard matched. Among named formats JSON is preferred, then
    ``application/vnd.pypi.simple.v1+html``, then ``text/html``, as PEP 691
    recommends. Among wildcard matches ``text/html`` is preferred, as PyPI does,
    because clients that only parse HTML send ``*/*``. A missing or empty header
    counts as ``*/*`` (PEP 691), and so does one with no well-formed range.
    """
    ranges = _parse_accept(accept) if accept else []
    if not ranges:
        ranges = [("*", "*", 1000)]
    best: _Offer | None = None
    best_score = (0, 0)
    for offer in _OFFERS:
        quality, named = _quality(offer, ranges)
        if quality == 0:
            continue
        # Named formats rank above every wildcard match of the same quality.
        score = (quality, (3 + offer.named_rank) if named else offer.wildcard_rank)
        if score > best_score:
            best, best_score = offer, score
    return None if best is None else best.content_type


@dataclass(frozen=True, slots=True)
class _FileTarget:
    """A validated ``files/`` request path."""

    project: str
    digest: str
    path: str
    metadata: bool


def _parse_files_path(relative: str) -> _FileTarget | None:
    """Validate the part of a ``files/`` path after ``files/``; None when it is refused."""
    match = _FILES_RE.fullmatch(relative)
    if match is None:
        return None
    project, filename = match["project"], match["filename"]
    metadata = filename.endswith(_METADATA_SUFFIX)
    distribution = filename.removesuffix(_METADATA_SUFFIX) if metadata else filename
    if not is_distribution_of(distribution, project):
        return None
    return _FileTarget(project, match["digest"], match["path"], metadata)


def _base_url(value: str, *, setting: str, schemes: frozenset[str]) -> str:
    match = _BASE_URL_RE.fullmatch(value)
    if match is None or match["scheme"] not in schemes:
        allowed = " or ".join(sorted(schemes))
        raise ValueError(
            f"{setting} must be an absolute {allowed} URL without user info, query, "
            f"fragment or trailing slash, not {value!r}"
        )
    return value


# Parsing upstream pages. Every helper raises ValueError, which the core reports as 502.


def _sha256(value: str, what: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{what} is not a SHA-256 hex digest: {value!r}")
    return value.lower()


def _string(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    return value


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object")
    return cast("dict[str, object]", value)


def _array(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a JSON array")
    return cast("list[object]", value)


def _hashes(value: object, what: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, item in _object(value, what).items():
        digest = _string(item, f"{what}[{name!r}]")
        if _HASH_NAME_RE.fullmatch(name) is None or _HEX_RE.fullmatch(digest) is None:
            raise ValueError(f"{what} must map hash names to hex digests")
        hashes[name] = digest
    return hashes


def _optional_string(value: object, what: str) -> str | None:
    return None if value is None else _string(value, what)


def _boolean(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{what} must be true or false")
    return value


def _metadata_flag(value: object, what: str) -> bool | dict[str, str]:
    return value if isinstance(value, bool) else _hashes(value, what)


def _yanked(value: object, what: str) -> bool | str:
    return value if isinstance(value, bool) else _string(value, what)


def _size(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what} must be a byte count")
    return value


def _upload_time(value: object, what: str) -> str:
    text = _string(value, what)
    if _UPLOAD_TIME_RE.fullmatch(text) is None:
        raise ValueError(f"{what} must be a UTC time such as 2024-01-31T12:00:00.123456Z")
    return text


def _https_url(value: str, what: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not value.isascii()
        or any(char.isspace() or not char.isprintable() for char in value)
    ):
        raise ValueError(f"{what} must be an absolute https URL")
    return value


def _provenance(value: object, what: str) -> str | None:
    return None if value is None else _https_url(_string(value, what), what)


_JSON_FILE_FIELDS: Final[tuple[tuple[str, Callable[[object, str], object]], ...]] = (
    ("requires-python", _optional_string),
    ("core-metadata", _metadata_flag),
    ("dist-info-metadata", _metadata_flag),
    ("data-dist-info-metadata", _metadata_flag),
    ("gpg-sig", _boolean),
    ("yanked", _yanked),
    ("size", _size),
    ("upload-time", _upload_time),
    ("provenance", _provenance),
)
"""The file fields a JSON page keeps, after filename, url and hashes (PEPs 592, 658, 700,
714, 740). PyPI spells PEP 658's key ``data-dist-info-metadata`` in JSON (PEP 714)."""


def _api_version(value: str) -> str:
    """Check a page's API version (PEP 629) and cap it at the newest one the rewrite keeps."""
    match = _API_VERSION_RE.fullmatch(value)
    if match is None or int(match["major"]) != 1:
        raise ValueError(f"unsupported simple API version {value!r}")
    return f"1.{min(int(match['minor']), _API_MINOR)}"


def _project_status(value: object) -> dict[str, str]:
    """PEP 792's project status object, with the keys it defines."""
    status = _object(value, "project-status")
    result: dict[str, str] = {}
    if "status" in status:
        result["status"] = _status_marker(_string(status["status"], "project-status.status"))
    if "reason" in status:
        result["reason"] = _string(status["reason"], "project-status.reason")
    return result


def _status_marker(value: str) -> str:
    if _STATUS_RE.fullmatch(value) is None:
        raise ValueError(f"invalid project status {value!r}")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def _load_json(body: bytes) -> object:
    try:
        loaded: object = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise ValueError("JSON nests too deeply") from exc
    return loaded


def _check_content_type(content_type: str, family: Literal["json", "html"]) -> None:
    """Make sure upstream answered in the format the page key stands for, in UTF-8."""
    media_type, _, parameters = content_type.partition(";")
    expected = {JSON_TYPE} if family == "json" else {HTML_TYPE, TEXT_HTML_TYPE}
    if media_type.strip(" \t").lower() not in expected:
        raise ValueError(f"expected {' or '.join(sorted(expected))}, got {content_type!r}")
    for parameter in parameters.split(";"):
        name, _, value = parameter.partition("=")
        if name.strip(" \t").lower() == "charset" and value.strip(' \t"').lower() not in (
            "utf-8",
            "us-ascii",
        ):
            raise ValueError(f"unsupported charset in {content_type!r}")


_HTML_METAS: Final = (
    "pypi:repository-version",
    "pypi:project-status",
    "pypi:project-status-reason",
)
_HTML_FILE_ATTRIBUTES: Final = (
    "data-requires-python",
    "data-core-metadata",
    "data-dist-info-metadata",
    "data-gpg-sig",
    "data-yanked",
    "data-provenance",
)
"""The anchor attributes an HTML page keeps besides ``href`` (PEPs 503, 592, 658, 714, 740)."""


def _html_attribute(name: str, value: str) -> str:
    match name:
        case "data-core-metadata" | "data-dist-info-metadata":
            if value != "true" and _HASH_ATTRIBUTE_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be true or <hash name>=<hex digest>, not {value!r}")
        case "data-gpg-sig":
            if value not in ("true", "false"):
                raise ValueError(f"data-gpg-sig must be true or false, not {value!r}")
        case "data-provenance":
            _https_url(value, name)
        case _:
            pass  # data-requires-python and data-yanked hold any text
    return value


class _PageParser(HTMLParser):
    """Collect what a simple API page says: its API metas, the first ``<base href>`` and anchors."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base: str | None = None
        self.metas: dict[str, str] = {}
        self.anchors: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record ``a``, ``base`` and ``meta`` elements; ignore everything else."""
        if tag not in ("a", "base", "meta"):
            return
        attributes: dict[str, str] = {}
        for name, value in attrs:
            if name in attributes:
                raise ValueError(f"<{tag}> repeats the attribute {name!r}")
            # A bare attribute (<a data-yanked>) has the empty string as its value.
            attributes[name] = "" if value is None else value
        if tag == "a":
            if "href" in attributes:
                self.anchors.append(attributes)
        elif tag == "base":
            # As browsers and pip do, only the first base element with an href counts.
            if self.base is None and "href" in attributes:
                self.base = attributes["href"]
        else:
            name = attributes.get("name")
            if name in _HTML_METAS:
                if name in self.metas:
                    raise ValueError(f"the page repeats the meta {name!r}")
                self.metas[name] = attributes.get("content", "")


def _attribute(value: str) -> str:
    return html.escape(value, quote=True)


@dataclass(frozen=True, slots=True)
class _Link:
    url: str
    filename: str


@dataclass(frozen=True, slots=True)
class _PageRewriter:
    """Rewrite one project's page so that every file link points at the cache."""

    project: str
    page_url: str
    family: Literal["json", "html"]
    files_host: str
    files_port: int
    files_prefix: str
    """The path of the files base URL, with a trailing slash."""
    cache_files: str
    """``<public_url>/pypi/files``."""

    def __call__(self, body: bytes, content_type: str) -> bytes:
        """Return the rewritten page; raise ValueError when it cannot be rewritten safely."""
        _check_content_type(content_type, self.family)
        if self.family == "json":
            return self._json(body)
        return self._html(body)

    def _link(self, raw: str, base: str, sha256: str | None) -> _Link | None:
        """Map a file URL from the page to the cache, or None to leave the file out.

        Raise ValueError when the URL is malformed or not on the files host.
        """
        value = raw.strip(_HTML_SPACE)
        if (
            not value
            or not value.isascii()
            or any(char.isspace() or not char.isprintable() for char in value)
        ):
            raise ValueError(f"malformed file URL {raw!r}")
        try:
            url, _, fragment = urljoin(base, value).partition("#")
            parts = urlsplit(url)
            port = parts.port or 443
        except ValueError as exc:  # an invalid port or IPv6 literal
            raise ValueError(f"malformed file URL {raw!r}: {exc}") from exc
        if (
            # urljoin drops an empty query, and a reference may inherit the base's query.
            "?" in value
            or "?" in url
            or parts.scheme != "https"
            or "@" in parts.netloc
            or parts.hostname != self.files_host
            or port != self.files_port
            or not parts.path.startswith(self.files_prefix)
        ):
            raise ValueError(f"file URL {url!r} is not on the files host")
        path = parts.path.removeprefix(self.files_prefix)
        upstream = _UPSTREAM_PATH_RE.fullmatch(path)
        if upstream is None:
            raise ValueError(f"file URL {url!r} does not have the files host's layout")
        if fragment:
            match = _FRAGMENT_RE.fullmatch(fragment)
            if match is None:
                raise ValueError(f"unexpected fragment in file URL {raw!r}")
            if match["name"] == "sha256":
                in_fragment = _sha256(match["value"], "the URL fragment")
                if sha256 is not None and sha256 != in_fragment:
                    raise ValueError(f"the hashes and URL of {url!r} disagree")
                sha256 = in_fragment
        digest = sha256 or "-"
        relative = f"{self.project}/{digest}/{path}"
        # The cache must serve every link it writes, so the route's own check decides.
        if _parse_files_path(relative) is None:
            return None
        link = f"{self.cache_files}/{relative}"
        return _Link(f"{link}#{fragment}" if fragment else link, upstream["filename"])

    def _json(self, body: bytes) -> bytes:
        page = _object(_load_json(body), "the page")
        meta = _object(page.get("meta"), "meta")
        version = _api_version(_string(meta.get("api-version"), "meta.api-version"))
        if normalize_name(_string(page.get("name"), "name")) != self.project:
            raise ValueError(f"the page is not about {self.project!r}")
        result: dict[str, object] = {"meta": {"api-version": version}, "name": self.project}
        if "project-status" in page:
            result["project-status"] = _project_status(page["project-status"])
        if "versions" in page:
            versions = _array(page["versions"], "versions")
            result["versions"] = [_string(item, "versions[]") for item in versions]
        files: list[dict[str, object]] = []
        for entry in _array(page.get("files"), "files"):
            rewritten = self._json_file(_object(entry, "files[]"))
            if rewritten is not None:
                files.append(rewritten)
        result["files"] = files
        return json.dumps(result, separators=(",", ":")).encode("ascii")

    def _json_file(self, entry: dict[str, object]) -> dict[str, object] | None:
        filename = _string(entry.get("filename"), "filename")
        hashes = _hashes(entry.get("hashes"), "hashes")
        sha256 = _sha256(hashes["sha256"], "hashes.sha256") if "sha256" in hashes else None
        link = self._link(_string(entry.get("url"), "url"), self.page_url, sha256)
        if link is None or link.filename != filename:
            return None
        result: dict[str, object] = {"filename": filename, "url": link.url, "hashes": hashes}
        for key, check in _JSON_FILE_FIELDS:
            if key in entry:
                result[key] = check(entry[key], key)
        return result

    def _html(self, body: bytes) -> bytes:
        parser = _PageParser()
        parser.feed(body.decode("utf-8"))
        parser.close()
        base = self.page_url
        if parser.base is not None:
            base = urljoin(self.page_url, parser.base.strip(_HTML_SPACE))
        head = ["<!DOCTYPE html>", "<html>", "  <head>"]
        if "pypi:repository-version" in parser.metas:
            version = _api_version(parser.metas["pypi:repository-version"])
            head.append(f'    <meta name="pypi:repository-version" content="{version}">')
        if "pypi:project-status" in parser.metas:
            status = _status_marker(parser.metas["pypi:project-status"])
            head.append(f'    <meta name="pypi:project-status" content="{status}">')
        if "pypi:project-status-reason" in parser.metas:
            reason = _attribute(parser.metas["pypi:project-status-reason"])
            head.append(f'    <meta name="pypi:project-status-reason" content="{reason}">')
        title = f"Links for {self.project}"
        lines = [
            *head,
            f"    <title>{title}</title>",
            "  </head>",
            "  <body>",
            f"    <h1>{title}</h1>",
        ]
        for anchor in parser.anchors:
            link = self._link(anchor["href"], base, None)
            if link is None:
                continue
            attributes = [f'href="{_attribute(link.url)}"']
            for name in _HTML_FILE_ATTRIBUTES:
                if name in anchor:
                    attributes.append(f'{name}="{_attribute(_html_attribute(name, anchor[name]))}"')
            lines.append(f"    <a {' '.join(attributes)}>{_attribute(link.filename)}</a><br>")
        lines += ["  </body>", "</html>", ""]
        # Character references keep the page ASCII, whatever charset a client assumes.
        return "\n".join(lines).encode("ascii", "xmlcharrefreplace")


class PypiAdapter:
    """PyPI below ``/pypi/``, as a simple repository API index (see the module docstring).

    ``index`` is the upstream simple index (``https://pypi.org/simple``), ``files``
    the host its file links point at (``https://files.pythonhosted.org``), and
    ``public_url`` the cache's base URL as sandboxes reach it
    (``http://host.openshell.internal:8090``), for rewritten file links. None of
    them may end with a slash.
    """

    prefix = "pypi"

    def __init__(self, *, index: str, files: str, public_url: str) -> None:
        https = frozenset({"https"})
        self._index = _base_url(index, setting="pypi index", schemes=https)
        self._files = _base_url(files, setting="pypi files", schemes=https)
        self._public_url = _base_url(
            public_url, setting="public_url", schemes=frozenset({"http", "https"})
        )
        files_parts = urlsplit(self._files)
        self._files_host = files_parts.hostname or ""
        self._files_port = files_parts.port or 443
        self._files_prefix = f"{files_parts.path}/"

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        """Map a path below ``/pypi/`` to the cache's answer (see the module docstring)."""
        if path.startswith("simple/"):
            return self._page(path.removeprefix("simple/"), headers.get("accept"))
        if path.startswith("files/"):
            return self._file(path.removeprefix("files/"))
        return None

    def _page(self, relative: str, accept: str | None) -> Route:
        match = _PAGE_RE.fullmatch(relative)
        if match is None:
            return None
        project = normalize_name(match["name"])
        content_type = negotiate(accept)
        if content_type is None:
            return _NOT_ACCEPTABLE
        url = f"{self._index}/{project}/"
        family: Literal["json", "html"] = "json" if content_type == JSON_TYPE else "html"
        rewrite = _PageRewriter(
            project=project,
            page_url=url,
            family=family,
            files_host=self._files_host,
            files_port=self._files_port,
            files_prefix=self._files_prefix,
            cache_files=f"{self._public_url}/{self.prefix}/files",
        )
        # The stored body is the rewritten page, so the key names everything it depends on.
        key = (
            f"{url} accept={content_type} files={self._files} "
            f"public={self._public_url} rewrite={_REWRITE_VERSION}"
        )
        return Fetch(
            key=key,
            url=url,
            kind=Kind.METADATA,
            headers={"Accept": content_type},
            rewrite=rewrite,
            content_type=content_type,
        )

    def _file(self, relative: str) -> Route:
        target = _parse_files_path(relative)
        if target is None:
            return None
        url = f"{self._files}/{target.path}"
        if target.metadata:
            # No digest of its own: the one in the path is the distribution's, and the
            # cache verifies nothing against it, so the upstream URL alone is the key.
            return Fetch(key=url, url=url, kind=Kind.METADATA, immutable=True)
        digest = None if target.digest == "-" else Digest("sha256", target.digest)
        return Fetch(
            # The digest is part of the key, as for npm and Cargo: a body verified against
            # one digest is never served for a path that names another digest, or none,
            # and a request naming a wrong digest fails alone instead of sharing its fetch
            # (and its 502) with a concurrent request naming the right one.
            key=f"{url} sha256={target.digest}",
            url=url,
            kind=Kind.ARTIFACT,
            immutable=True,
            digest=digest,
        )
