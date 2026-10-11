# SPDX-License-Identifier: MIT
"""Classify requests to the package cache (docs/PACKAGE-CACHE.md, "URL layout").

The policy middleware calls ``classify_package_request`` for every request a
sandbox sends to the package cache. The cache's adapters accept the same paths;
anything this module does not recognize is refused, so search, audit, publish
and login endpoints are denied by policy as well as by the cache.

Paths arrive in OpenShell's canonical form: percent-decoded and re-encoded with
upper-case escapes for bytes outside the URL-safe set. The layout uses only
URL-safe characters, except the ``%2F`` in npm's scoped names, so every other
escape is refused.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

from .errors import ClassificationError
from .operations import PackageOperation

_SAFE_PATH_RE: Final = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@/%-]*")
_ENCODED_SLASH_RE: Final = re.compile(r"%2[Ff]")
_CHANNEL: Final = r"[a-z0-9][a-z0-9._-]*"
_SUBDIR: Final = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_CONDA_INDEX_FILES: Final = frozenset(
    {
        "repodata.json",
        "repodata.json.zst",
        "repodata.json.bz2",
        "current_repodata.json",
        "current_repodata.json.zst",
        "current_repodata.json.bz2",
        "repodata_shards.msgpack.zst",
    }
)

_PYPI_SIMPLE_RE: Final = re.compile(r"simple/([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)/")
_PYPI_FILE_RE: Final = re.compile(
    r"files/([a-z0-9]+(?:-[a-z0-9]+)*)/(?:[0-9a-f]{64}|-)/packages/[A-Za-z0-9._/+~-]+"
)
_CONDA_CHANNEL_INDEX_RE: Final = re.compile(rf"({_CHANNEL})/channeldata\.json")
_CONDA_INDEX_RE: Final = re.compile(rf"({_CHANNEL})/{_SUBDIR}/([a-z0-9_.]+)")
_CONDA_SHARD_RE: Final = re.compile(rf"({_CHANNEL})/{_SUBDIR}/shards/[0-9a-f]{{64}}\.msgpack\.zst")
_CONDA_PACKAGE_RE: Final = re.compile(
    rf"({_CHANNEL})/{_SUBDIR}/([a-z0-9_][a-z0-9_.+-]*)-[^-/]+-[^-/]+\.(?:conda|tar\.bz2)"
)
_CARGO_INDEX_RE: Final = re.compile(
    r"index/(?:1|2|3/[a-z0-9_-]|[a-z0-9_-]{2}/[a-z0-9_-]{2})/([a-z0-9_-]{1,64})"
)
_CARGO_DOWNLOAD_RE: Final = re.compile(
    r"dl/([A-Za-z][A-Za-z0-9_-]{0,63})/[0-9A-Za-z.+-]+/[0-9a-f]{64}"
)
_GO_SUMDB_RE: Final = re.compile(r"sumdb/sum\.golang\.org/(?:supported|latest|lookup/.+|tile/.+)")
_GO_MODULE_RE: Final = re.compile(
    # Versions are case-encoded like module paths ("!r!c1" for "RC1").
    r"([a-z0-9.!~_-]+(?:/[a-z0-9.!~_-]+)*)/(?:@latest|@v/list|@v/v[0-9a-z.!+-]+\.(info|mod|zip))"
)
_NPM_FILE_RE: Final = re.compile(
    r"files/(?:[0-9a-f]{128}|-)/((?:@[^/]+/)?[^/@][^/]*)/-/[A-Za-z0-9._~-]+\.tgz"
)
_NPM_SCOPED_RE: Final = re.compile(r"(@[^/%]+)(?:%2[Ff]|/)([^/%]+)")
_NPM_UNSCOPED_RE: Final = re.compile(r"[^/@%._-][^/%]*")


def classify_package_request(method: str, path: str) -> PackageOperation:
    """Classify a request to the package cache; raise ClassificationError for anything else."""
    if method not in ("GET", "HEAD"):
        raise ClassificationError(f"the package cache accepts GET and HEAD, not {method!r}")
    if not _SAFE_PATH_RE.fullmatch(path):
        raise ClassificationError(f"package cache path {path!r} has unexpected characters")
    ecosystem, _, rest = path[1:].partition("/")
    segments = rest.split("/")
    # Only PyPI's per-project index ends in "/", leaving one empty last segment.
    inner = segments[:-1] if ecosystem == "pypi" and rest.endswith("/") else segments
    if any(segment in ("", ".", "..") for segment in inner):
        raise ClassificationError(f"package cache path {path!r} has empty or dot segments")
    escapes = path.count("%")
    if escapes and not (ecosystem == "npm" and escapes == len(_ENCODED_SLASH_RE.findall(path))):
        raise ClassificationError(f"package cache path {path!r} has percent-escapes")
    classify = _CLASSIFIERS.get(ecosystem)
    operation = classify(rest) if classify is not None else None
    if operation is None:
        raise ClassificationError(f"{path!r} is not a package cache path")
    return operation


def _pypi(rest: str) -> PackageOperation | None:
    if match := _PYPI_SIMPLE_RE.fullmatch(rest):
        return PackageOperation("pypi", match[1], kind="index")
    if match := _PYPI_FILE_RE.fullmatch(rest):
        # A PEP 658 metadata file describes the package; it installs nothing.
        kind = "index" if rest.endswith(".metadata") else "artifact"
        return PackageOperation("pypi", match[1], kind=kind)
    return None


def _conda(rest: str) -> PackageOperation | None:
    if match := _CONDA_CHANNEL_INDEX_RE.fullmatch(rest):
        return PackageOperation("conda", None, channel=match[1], kind="index")
    if match := _CONDA_SHARD_RE.fullmatch(rest):
        return PackageOperation("conda", None, channel=match[1], kind="index")
    if match := _CONDA_PACKAGE_RE.fullmatch(rest):
        return PackageOperation("conda", match[2], channel=match[1])
    if (match := _CONDA_INDEX_RE.fullmatch(rest)) and match[2] in _CONDA_INDEX_FILES:
        return PackageOperation("conda", None, channel=match[1], kind="index")
    return None


def _cargo(rest: str) -> PackageOperation | None:
    if rest == "index/config.json":
        return PackageOperation("cargo", None, kind="index")
    if match := _CARGO_INDEX_RE.fullmatch(rest):
        return PackageOperation("cargo", match[1], kind="index")
    if match := _CARGO_DOWNLOAD_RE.fullmatch(rest):
        return PackageOperation("cargo", match[1])
    return None


def _go(rest: str) -> PackageOperation | None:
    if _GO_SUMDB_RE.fullmatch(rest):
        return PackageOperation("go", None, kind="index")
    if match := _GO_MODULE_RE.fullmatch(rest):
        module = _unescape_module(match[1])
        if module is None:
            return None
        return PackageOperation("go", module, kind="artifact" if match[2] == "zip" else "index")
    return None


def _unescape_module(escaped: str) -> str | None:
    """Undo the proxy's case encoding ("!x" is "X"); None if it is malformed."""
    if re.search(r"![^a-z]|!$", escaped):
        return None
    return re.sub(r"!([a-z])", lambda match: match[1].upper(), escaped)


def _npm(rest: str) -> PackageOperation | None:
    if match := _NPM_FILE_RE.fullmatch(rest):
        return PackageOperation("npm", match[1])
    if match := _NPM_SCOPED_RE.fullmatch(rest):
        return PackageOperation("npm", f"{match[1]}/{match[2]}", kind="index")
    if _NPM_UNSCOPED_RE.fullmatch(rest):
        return PackageOperation("npm", rest, kind="index")
    return None


_CLASSIFIERS: Final[dict[str, Callable[[str], PackageOperation | None]]] = {
    "pypi": _pypi,
    "conda": _conda,
    "cargo": _cargo,
    "go": _go,
    "npm": _npm,
}
