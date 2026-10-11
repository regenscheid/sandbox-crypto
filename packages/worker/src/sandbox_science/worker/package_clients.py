# SPDX-License-Identifier: MIT
"""The package clients' configuration for a sandbox (docs/PACKAGE-CACHE.md, "Client configuration").

``render`` takes the package cache's URL as sandboxes reach it (the cache's
``public_url``) and returns the files and environment variables that point pip,
uv, conda (with mamba and micromamba), Cargo, Go and npm at the cache, and
nowhere else: no extra index, no ``defaults`` channel, no ``direct`` fallback
for Go. Paths follow the cache's URL layout. The OpenShell backend sends the
result to the executor when it starts it; the executor writes the files and
adds the variables to every shell command and kernel (docs/WORKER.md).

Each file goes in the executor's configuration directory, and a variable points
its client at it. Cargo's is the exception: Cargo reads configuration only from
``$CARGO_HOME`` and from ``.cargo`` directories above the working directory, and
setting ``CARGO_HOME`` would also move its caches and installed programs, so its
file goes in the home directory.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

Base = Literal["config", "home"]
"""Where a file goes: the executor's configuration directory, or the sandbox user's home."""

HEADER: Final = (
    "# Written by the Sandbox Science worker: packages come only through the package cache.\n"
    "# Once changed, this file is left as it is.\n"
)

_URL_RE: Final = re.compile(
    r"(?P<scheme>https?)://"
    r"(?P<host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])"
    r"(?::(?P<port>[0-9]{1,5}))?"
    r"(?P<path>(?:/[A-Za-z0-9._~-]+)*)/?",
    re.ASCII,
)
"""A base URL to append paths to. Its characters need no quoting in any of the files."""


class CacheUrlError(ValueError):
    """The package cache URL cannot be written into the clients' configuration."""


@dataclass(frozen=True, slots=True)
class ConfigFile:
    """One client's configuration file."""

    base: Base
    path: str
    """Relative to ``base``."""
    content: str
    variable: str | None = None
    """For a file in the configuration directory, the variable that points its client at it."""


@dataclass(frozen=True, slots=True)
class ClientConfiguration:
    """Files to write, and variables to set in every shell command and kernel."""

    files: tuple[ConfigFile, ...] = ()
    env: Mapping[str, str] = field(default_factory=lambda: dict[str, str]())


@dataclass(frozen=True, slots=True)
class _Cache:
    url: str
    """Normalized, without a trailing slash."""
    netloc: str
    """``host`` or ``host:port``, as clients name the host they trust."""
    host: str
    port: int
    """As given, or the scheme's default."""
    plain_http: bool


def check_cache_url(value: str) -> str:
    """Return the cache URL without a trailing slash and with a lowercase host.

    The URL must be ``http`` or ``https``, name a host and optionally a port and
    a path, and have no user info, query or fragment. Raises CacheUrlError.
    """
    return _cache(value).url


def cache_endpoint(cache_url: str) -> tuple[str, int]:
    """The host and port the clients connect to, as an OpenShell targets file names them."""
    cache = _cache(cache_url)
    return cache.host, cache.port


def render(cache_url: str) -> ClientConfiguration:
    """The configuration that points every supported client at the cache only."""
    cache = _cache(cache_url)
    files: list[ConfigFile] = []
    env: dict[str, str] = {}
    for client in _CLIENTS:
        part = client(cache)
        files += part.files
        env.update(part.env)
    return ClientConfiguration(files=tuple(files), env=env)


def _cache(value: str) -> _Cache:
    match = _URL_RE.fullmatch(value)
    if match is None:
        raise CacheUrlError(
            f"the package cache URL must be http(s)://host[:port][/path] with letters, digits "
            f"and '.', '_', '~' or '-' in the path, and no user info, query or fragment, "
            f"not {value!r}"
        )
    host, port, path = match["host"].lower(), match["port"], match["path"]
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError as exc:
            raise CacheUrlError(f"invalid IPv6 address in {value!r}") from exc
    if port is not None and not 0 < int(port) < 65536:
        raise CacheUrlError(f"invalid port in {value!r}")
    if any(segment in (".", "..") for segment in path.split("/")):
        raise CacheUrlError(f"the package cache URL must not have . or .. segments: {value!r}")
    netloc = f"{host}:{port}" if port is not None else host
    plain_http = match["scheme"] == "http"
    return _Cache(
        url=f"{match['scheme']}://{netloc}{path}",
        netloc=netloc,
        host=host,
        port=int(port) if port is not None else 80 if plain_http else 443,
        plain_http=plain_http,
    )


def _pip(cache: _Cache) -> ClientConfiguration:
    content = f"{HEADER}[global]\nindex-url = {cache.url}/pypi/simple/\n"
    if cache.plain_http:
        # pip ignores an index on a plain-HTTP host unless the host is trusted.
        content += f"trusted-host = {cache.netloc}\n"
    return ClientConfiguration(
        files=(ConfigFile("config", "pip.conf", content, "PIP_CONFIG_FILE"),)
    )


def _uv(cache: _Cache) -> ClientConfiguration:
    # uv reads neither pip.conf nor PIP_* variables. A uv.toml named by
    # UV_CONFIG_FILE would replace the project's own uv settings, so a variable
    # sets the index instead.
    return ClientConfiguration(env={"UV_DEFAULT_INDEX": f"{cache.url}/pypi/simple/"})


def _conda(cache: _Cache) -> ClientConfiguration:
    # Every channel name, conda-forge's included, resolves below channel_alias.
    # "defaults" would name repo.anaconda.com, so it names nothing here.
    content = (
        f"{HEADER}"
        "# conda also reads its other .condarc files; mamba and micromamba read only\n"
        "# this one while CONDARC names it. `conda config` writes ~/.condarc.\n"
        f'channel_alias: "{cache.url}/conda"\n'
        "channels:\n"
        "  - conda-forge\n"
        "default_channels: []\n"
    )
    return ClientConfiguration(files=(ConfigFile("config", "condarc", content, "CONDARC"),))


def _cargo(cache: _Cache) -> ClientConfiguration:
    content = (
        f"{HEADER}"
        "[source.crates-io]\n"
        'replace-with = "package-cache"\n'
        "\n"
        "[source.package-cache]\n"
        f'registry = "sparse+{cache.url}/cargo/index/"\n'
    )
    if cache.plain_http:
        content += (
            "\n"
            "# Over plain HTTP, multiplexing makes Cargo ask to upgrade to HTTP/2 (h2c),\n"
            "# which OpenShell refuses on inspected endpoints.\n"
            "[http]\n"
            "multiplexing = false\n"
        )
    return ClientConfiguration(files=(ConfigFile("home", ".cargo/config.toml", content),))


def _go(cache: _Cache) -> ClientConfiguration:
    # The cache answers sumdb/sum.golang.org/supported, so the go command checks
    # every module against the checksum database through the proxy. Toolchains
    # come from conda-forge: the go command never downloads another one.
    content = f"{HEADER}GOPROXY={cache.url}/go\nGOSUMDB=sum.golang.org\nGOTOOLCHAIN=local\n"
    return ClientConfiguration(files=(ConfigFile("config", "go.env", content, "GOENV"),))


def _npm(cache: _Cache) -> ClientConfiguration:
    # The cache refuses npm's audit (a POST), and funding notices and update
    # checks are noise in a sandbox.
    content = f"{HEADER}registry={cache.url}/npm/\naudit=false\nfund=false\nupdate-notifier=false\n"
    return ClientConfiguration(
        files=(ConfigFile("config", "npmrc", content, "NPM_CONFIG_USERCONFIG"),)
    )


_CLIENTS: Final[tuple[Callable[[_Cache], ClientConfiguration], ...]] = (
    _pip,
    _uv,
    _conda,
    _cargo,
    _go,
    _npm,
)
"""One function per client.

opam and cabal (Hackage) join once the cache serves them (docs/PACKAGE-CACHE.md):
cabal reads the file that CABAL_CONFIG names, while opam keeps its repository
in its root, where ``opam repository set-url default <cache>/opam`` changes it.
"""
