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
    r"files/([a-z0-9]+(?:-[a-z0-9]+)*)/(?:[0-9a-f]{64}|-)/packages/(?:[A-Za-z0-9._+~!-]+/)*"
    r"([A-Za-z0-9._+~!-]+)"
)
_PYPI_SDIST_SUFFIXES: Final = (".tar.gz", ".zip", ".tar.bz2", ".tgz")
_CONDA_CHANNEL_INDEX_RE: Final = re.compile(rf"({_CHANNEL})/channeldata\.json")
_CONDA_INDEX_RE: Final = re.compile(rf"({_CHANNEL})/{_SUBDIR}/([a-z0-9_.]+)")
# conda-forge serves shards beside repodata (the shard index's base URL is
# empty); a "shards/" directory is accepted as well.
_CONDA_SHARD_RE: Final = re.compile(
    rf"({_CHANNEL})/{_SUBDIR}/(?:shards/)?[0-9a-f]{{64}}\.msgpack\.zst"
)
_CONDA_PACKAGE_RE: Final = re.compile(
    rf"({_CHANNEL})/{_SUBDIR}/([a-z0-9_][a-z0-9_.+-]*)-[^-/]+-[^-/]+\.(?:conda|tar\.bz2)"
)
_CARGO_INDEX_RE: Final = re.compile(
    r"index/(?:1|2|3/[a-z0-9_-]|[a-z0-9_-]{2}/[a-z0-9_-]{2})/([a-z0-9_-]{1,64})"
)
_CARGO_DOWNLOAD_RE: Final = re.compile(
    r"dl/([A-Za-z][A-Za-z0-9_-]{0,63})/[0-9A-Za-z.+-]+/[0-9a-f]{64}"
)
_GO_SUMDB_RE: Final = re.compile(r"sumdb/sum\.golang\.org/(?:supported|latest|tile/.+)")
_GO_LOOKUP_RE: Final = re.compile(
    r"sumdb/sum\.golang\.org/lookup/([a-z0-9.!~_-]+(?:/[a-z0-9.!~_-]+)*)@v[0-9a-z.!+-]+"
)
_GO_MODULE_RE: Final = re.compile(
    # Versions are case-encoded like module paths ("!r!c1" for "RC1").
    r"([a-z0-9.!~_-]+(?:/[a-z0-9.!~_-]+)*)/(?:@latest|@v/list|@v/v[0-9a-z.!+-]+\.(info|mod|zip))"
)
# npm names use the characters encodeURIComponent leaves alone.
_NPM_CHAR: Final = r"[A-Za-z0-9._~!*'()-]"
_NPM_NAME: Final = (
    rf"(?:@{_NPM_CHAR}+(?:/|%2[Ff])[A-Za-z0-9_~!*'()-]{_NPM_CHAR}*"
    rf"|[A-Za-z0-9~!*'()]{_NPM_CHAR}*)"
)
_NPM_TARBALL: Final = rf"{_NPM_CHAR}+[-@][0-9][A-Za-z0-9._~+-]*\.tgz"
_NPM_FILE_RE: Final = re.compile(rf"files/(?:[0-9a-f]{{128}}|-)/({_NPM_NAME})/-/({_NPM_TARBALL})")
# Lockfiles name the registry's own tarball path, which npm, pnpm and yarn
# rebase onto the configured registry; the client checks the lockfile's
# integrity, so these carry no digest.
_NPM_LOCKFILE_RE: Final = re.compile(rf"({_NPM_NAME})/-/({_NPM_TARBALL})")
_NPM_INDEX_RE: Final = re.compile(_NPM_NAME)
# opam fetches archives from the repository's content-addressed cache by the
# first checksum an opam file lists; the directory is the digest's first two digits.
_OPAM_ARCHIVE_RE: Final = re.compile(r"cache/(md5|sha256|sha512)/([0-9a-f]{2})/([0-9a-f]+)")
_OPAM_DIGEST_LENGTHS: Final = {"md5": 32, "sha256": 64, "sha512": 128}
# Hackage: hackage-security's signed metadata and index, and source distributions
# named <name>-<version> (Cabal names: words with a letter; versions: numbers).
_HACKAGE_INDEX_FILES: Final = frozenset(
    {"root.json", "timestamp.json", "snapshot.json", "mirrors.json", "01-index.tar.gz"}
)
_HACKAGE_NAME: Final = r"[0-9]*[A-Za-z][A-Za-z0-9]*(?:-[0-9]*[A-Za-z][A-Za-z0-9]*)*"
# Numbers within Cabal's Int, at most 32 of them, as the cache's adapter accepts them.
_HACKAGE_VERSION: Final = r"(?:0|[1-9][0-9]{0,17})(?:\.(?:0|[1-9][0-9]{0,17})){0,31}"
# Cabal's form, package/<id>.tar.gz, or Hackage's, package/<id>/<id>.tar.gz.
_HACKAGE_SDIST_RE: Final = re.compile(
    rf"package/(?P<id>(?P<name>{_HACKAGE_NAME})-{_HACKAGE_VERSION})(?:/(?P=id))?\.tar\.gz"
)


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
    if (match := _PYPI_FILE_RE.fullmatch(rest)) and _pypi_file_belongs(match[1], match[2]):
        # A PEP 658 metadata file describes the package; it installs nothing.
        kind = "index" if rest.endswith(".metadata") else "artifact"
        return PackageOperation("pypi", match[1], kind=kind)
    return None


def _pypi_normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pypi_file_belongs(project: str, filename: str) -> bool:
    """Whether a distribution (or its metadata file) is one of ``project``'s files.

    A wheel names its distribution before the first hyphen; an sdist is
    ``<name>-<version>``, so the project must be followed by a version.
    """
    name = filename.removesuffix(".metadata")
    if name.endswith(".whl"):
        return _pypi_normalize(name.split("-", 1)[0]) == project
    for suffix in _PYPI_SDIST_SUFFIXES:
        if name.endswith(suffix):
            stem = _pypi_normalize(name.removesuffix(suffix))
            version = stem.removeprefix(f"{project}-")
            return version != stem and version[:1].isdigit()
    return False


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
    if match := _GO_LOOKUP_RE.fullmatch(rest):
        # A checksum lookup names a module; the database fetches it from its origin.
        module = _unescape_module(match[1])
        return None if module is None else PackageOperation("go", module, kind="index")
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
    for pattern in (_NPM_FILE_RE, _NPM_LOCKFILE_RE):
        if match := pattern.fullmatch(rest):
            name = _ENCODED_SLASH_RE.sub("/", match[1])
            return PackageOperation("npm", name) if _npm_file_belongs(name, match[2]) else None
    if _NPM_INDEX_RE.fullmatch(rest):
        return PackageOperation("npm", _ENCODED_SLASH_RE.sub("/", rest), kind="index")
    return None


def _npm_file_belongs(name: str, filename: str) -> bool:
    """Whether a tarball is one of ``name``'s: ``<unscoped name>-<version>.tgz``.

    Old packages also use ``@`` as the separator.
    """
    unscoped = name.rsplit("/", 1)[-1]
    version = filename.removesuffix(".tgz")[len(unscoped) + 1 :]
    return (
        filename[: len(unscoped)] == unscoped
        and filename[len(unscoped)] in "-@"
        and (version[:1].isdigit())
    )


def _opam(rest: str) -> PackageOperation | None:
    if rest == "index.tar.gz":
        return PackageOperation("opam", None, kind="index")
    match = _OPAM_ARCHIVE_RE.fullmatch(rest)
    if match and len(match[3]) == _OPAM_DIGEST_LENGTHS[match[1]] and match[3][:2] == match[2]:
        # Content-addressed: the path names a digest, not a package.
        return PackageOperation("opam", None, kind="artifact")
    return None


def _hackage(rest: str) -> PackageOperation | None:
    if rest in _HACKAGE_INDEX_FILES:
        return PackageOperation("hackage", None, kind="index")
    if match := _HACKAGE_SDIST_RE.fullmatch(rest):
        return PackageOperation("hackage", match["name"])
    return None


_CLASSIFIERS: Final[dict[str, Callable[[str], PackageOperation | None]]] = {
    "pypi": _pypi,
    "conda": _conda,
    "cargo": _cargo,
    "go": _go,
    "npm": _npm,
    "opam": _opam,
    "hackage": _hackage,
}
