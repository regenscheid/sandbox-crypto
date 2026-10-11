# SPDX-License-Identifier: MIT
"""conda packages from channels on anaconda.org, such as conda-forge.

conda, mamba and pixi reach the cache through ``channel_alias: <cache>/conda``
(docs/PACKAGE-CACHE.md), so the channel ``conda-forge`` is
``<cache>/conda/conda-forge``. Paths below ``/conda/``, for the configured
channels and subdirs only:

``<channel>/channeldata.json``
    The channel's summary: index data, revalidated after the TTL.
``<channel>/<subdir>/<index file>``
    ``repodata.json`` and ``current_repodata.json``, each also compressed as
    ``.zst`` and ``.bz2``, and the sharded repodata index
    ``repodata_shards.msgpack.zst`` (CEP 16): index data, revalidated after the
    TTL. These are the variants conda-forge publishes; it has no ``.gz``,
    ``.jlap`` or ``.zck`` files.
``<channel>/<subdir>/<sha256>.msgpack.zst``
    One package name's repodata shard, named by the SHA-256 of the compressed
    file as served: immutable index data, which the cache verifies against that
    name. conda-forge's shard index leaves ``shards_base_url`` empty, so clients
    fetch shards from the subdir itself. ``<channel>/<subdir>/shards/<sha256>.msgpack.zst``,
    the layout docs/PACKAGE-CACHE.md describes, is accepted as well.
``<channel>/<subdir>/<name>-<version>-<build>.conda`` or ``.tar.bz2``
    A package file: an immutable artifact. The cache does not verify it; the
    client checks it against the SHA-256 in repodata. Some clients
    percent-encode the ``!`` of an epoch or the ``+`` of a local version, so
    ``%21`` and ``%2B`` stand for them; no other escape is accepted.

Nothing is rewritten, because conda-forge's index files name packages and
shards relative to the subdir. Everything else is refused, including other
files the channel publishes (``repodata_from_packages.json``,
``run_exports.json``) and labels (``<channel>/label/<label>/...``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

# The ignore is needed until package_cache ships a py.typed marker; strict pyright
# otherwise treats the contract module as an untyped library.
from sandbox_science.package_cache.adapter import (  # pyright: ignore[reportMissingTypeStubs]
    Digest,
    Fetch,
    Kind,
    Route,
)

MAX_FILENAME_LENGTH: Final = 255
"""Longer package file names are refused (conda-forge's longest is 92 characters)."""

CHANNEL_DATA: Final = "channeldata.json"
INDEX_FILES: Final = frozenset(
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
"""The per-subdir index files the cache serves."""

_CHANNEL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", re.ASCII)
_SUBDIR_RE: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
_SHARD_RE: Final = re.compile(r"(?P<sha256>[0-9a-f]{64})\.msgpack\.zst", re.ASCII)
# Names are lower case and may start with "_" (_libgcc_mutex); versions and build
# strings cannot contain "-", which is how the three parts are told apart.
_PACKAGE_RE: Final = re.compile(
    r"(?P<name>[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?)"
    r"-(?P<version>[A-Za-z0-9_][A-Za-z0-9_.+!]*)"
    r"-(?P<build>[A-Za-z0-9_][A-Za-z0-9_.+]*)"
    r"\.(?:conda|tar\.bz2)",
    re.ASCII,
)
_ESCAPES: Final = (("%21", "!"), ("%2B", "+"), ("%2b", "+"))

_HOST: Final = r"(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])"
_BASE_URL_RE: Final = re.compile(
    rf"https://{_HOST}(?::[0-9]{{1,5}})?(?:/[A-Za-z0-9._~-]+)*", re.ASCII
)
"""An https base URL to append paths to: no user info, query, fragment or trailing slash."""


def _unescape(filename: str) -> str:
    # Replacements never produce "%", so they cannot form a new escape; any "%"
    # left over fails the file name pattern.
    for escape, char in _ESCAPES:
        filename = filename.replace(escape, char)
    return filename


def parse_package_filename(filename: str) -> tuple[str, str, str] | None:
    """Split a package file name, as requested, into name, version and build string.

    ``%21`` and ``%2B`` are read as ``!`` and ``+``. Returns None for anything
    that is not ``<name>-<version>-<build>.conda`` or ``.tar.bz2``.
    """
    if len(filename) > MAX_FILENAME_LENGTH:
        return None
    match = _PACKAGE_RE.fullmatch(_unescape(filename))
    if match is None:
        return None
    return match["name"], match["version"], match["build"]


class CondaAdapter:
    """conda channels below ``/conda/`` (see the module docstring).

    ``base`` is the channel host (``https://conda.anaconda.org``), without a
    trailing slash. Only ``channels`` (``["conda-forge"]``) and, within them,
    ``subdirs`` (``["linux-64", "linux-aarch64", "noarch"]``) are served.
    """

    prefix = "conda"

    def __init__(self, *, base: str, channels: Sequence[str], subdirs: Sequence[str]) -> None:
        if _BASE_URL_RE.fullmatch(base) is None:
            raise ValueError(
                "conda base must be an absolute https URL without user info, query, "
                f"fragment or trailing slash, not {base!r}"
            )
        if isinstance(channels, str) or isinstance(subdirs, str):
            raise TypeError("channels and subdirs must be sequences of names, not a string")
        if not channels or not subdirs:
            raise ValueError("conda needs at least one channel and one subdir")
        for channel in channels:
            if _CHANNEL_RE.fullmatch(channel) is None:
                raise ValueError(f"invalid conda channel name {channel!r}")
        for subdir in subdirs:
            if _SUBDIR_RE.fullmatch(subdir) is None:
                raise ValueError(f"invalid conda subdir {subdir!r}")
        self._base = base
        self._channels = frozenset(channels)
        self._subdirs = frozenset(subdirs)

    def route(self, path: str, headers: Mapping[str, str]) -> Route:
        """Map a path below ``/conda/`` to the cache's answer (see the module docstring)."""
        channel, *rest = path.split("/")
        if channel not in self._channels:
            return None
        match rest:
            case [name]:
                return self._index(path, immutable=False) if name == CHANNEL_DATA else None
            case [subdir, *_] if subdir not in self._subdirs:
                return None
            case [_, name] if name in INDEX_FILES:
                return self._index(path, immutable=False)
            case [_, name] | [_, "shards", name] if shard := _SHARD_RE.fullmatch(name):
                # Content-addressed, so it never changes, and the cache checks that it
                # is what its name says. The digest follows from the URL, so the key
                # needs nothing more.
                digest = Digest("sha256", shard.group("sha256"))
                return self._index(path, immutable=True, digest=digest)
            case [subdir, name]:
                return self._package(channel, subdir, name)
            case _:
                return None

    def _index(self, path: str, *, immutable: bool, digest: Digest | None = None) -> Fetch:
        url = f"{self._base}/{path}"
        return Fetch(key=url, url=url, kind=Kind.METADATA, immutable=immutable, digest=digest)

    def _package(self, channel: str, subdir: str, name: str) -> Route:
        if parse_package_filename(name) is None:
            return None
        # Upstream gets one spelling of each file, so both spellings share a stored copy.
        url = f"{self._base}/{channel}/{subdir}/{_unescape(name)}"
        return Fetch(key=url, url=url, kind=Kind.ARTIFACT, immutable=True)
