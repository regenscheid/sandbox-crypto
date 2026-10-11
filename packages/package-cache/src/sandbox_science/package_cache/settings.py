# SPDX-License-Identifier: MIT
"""The package cache's settings, read from a TOML file (docs/PACKAGE-CACHE.md).

```toml
listen = "127.0.0.1:8090"
public_url = "http://host.openshell.internal:8090"   # how sandboxes reach the cache
cache_dir = "cache"
max_size = "100Gi"
metadata_ttl = "5m"
stale_if_error = "1d"
max_artifact_size = "4Gi"
max_metadata_size = "1Gi"
max_rewrite_size = "128Mi"   # index data the cache rewrites (PyPI pages, npm metadata)
max_temp_size = "16Gi"       # downloads in progress, in tmp/
max_artifact_downloads = 16  # package files downloading at once
max_requests = 1024          # client requests served at once; more get 503
upstream_rate = 20           # upstream fetches per second
upstream_burst = 200

[pypi]
index = "https://pypi.org/simple"
files = "https://files.pythonhosted.org"

[conda]
base = "https://conda.anaconda.org"
channels = ["conda-forge"]
subdirs = ["linux-64", "linux-aarch64", "noarch"]

[cargo]
index = "https://index.crates.io"
download = "https://static.crates.io/crates"

[go]
proxy = "https://proxy.golang.org"
sumdb = "https://sum.golang.org"

[npm]
registry = "https://registry.npmjs.org"

[opam]
repository = "https://opam.ocaml.org"

[hackage]
repository = "https://hackage.haskell.org"
```

``public_url`` and ``cache_dir`` are required; every other key has the default
shown. An ecosystem whose table is absent is disabled; a table with no keys
enables it with the default upstreams. Relative paths resolve against the
settings file's directory. URLs are normalized without a trailing slash, so
adapters append paths with ``f"{url}/..."``.

The size limits must agree: ``max_artifact_size`` and ``max_metadata_size``
at most ``max_size`` and at most ``max_temp_size``, and ``max_rewrite_size``,
when given, at most ``max_metadata_size``. The cache uses at most ``max_size``
of ``max_temp_size`` (``temp_limit``), and at most ``max_metadata_size`` of
the default ``max_rewrite_size`` (``rewrite_limit``).
"""

from __future__ import annotations

import ipaddress
import re
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Final, Self
from urllib.parse import unquote, urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    ValidationError,
    model_validator,
)

from sandbox_science.policy.units import parse_duration, parse_size

from .errors import SettingsError

_MIB: Final = 1024**2
_GIB: Final = 1024**3

_HOSTNAME_RE: Final = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
_PORT_RE: Final = re.compile(r"[0-9]{1,5}")


def _check_url(value: str, *, schemes: tuple[str, ...]) -> str:
    """Validate an absolute URL and return it without a trailing slash."""
    if not value.isascii() or any(not char.isprintable() or char.isspace() for char in value):
        raise ValueError("a URL must be printable ASCII without spaces")
    if "?" in value or "#" in value:
        raise ValueError("a URL must not have a query or fragment")
    parts = urlsplit(value)
    if parts.scheme not in schemes:
        raise ValueError(f"{value!r} must be an absolute {' or '.join(schemes)} URL")
    if not parts.hostname:
        raise ValueError(f"{value!r} must name a host")
    if "@" in parts.netloc:
        raise ValueError(f"{value!r} must not contain credentials")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{value!r} has an invalid port") from exc
    if port == 0:
        raise ValueError(f"{value!r} has an invalid port")
    if any(unquote(segment) in (".", "..") for segment in parts.path.split("/")):
        raise ValueError(f"{value!r} must not contain . or .. path segments")
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def _https_url(value: str) -> str:
    return _check_url(value, schemes=("https",))


def _public_url(value: str) -> str:
    return _check_url(value, schemes=("http", "https"))


def _split_listen(value: str) -> tuple[str, int]:
    host, separator, port = value.rpartition(":")
    if not separator or not host or not _PORT_RE.fullmatch(port) or not 1 <= int(port) <= 65535:
        raise ValueError(f"listen must be host:port with a port from 1 to 65535, not {value!r}")
    if host.startswith("["):
        if not host.endswith("]"):
            raise ValueError(f"invalid IPv6 address in listen {value!r}")
        host = host[1:-1]
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError(f"invalid IPv6 address in listen {value!r}") from exc
    elif not _HOSTNAME_RE.fullmatch(host):
        raise ValueError(f"invalid host in listen {value!r} (write IPv6 addresses in brackets)")
    return host, int(port)


def _check_listen(value: str) -> str:
    _split_listen(value)
    return value


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError("entries must be unique")
    return values


HttpsUrl = Annotated[str, AfterValidator(_https_url)]
"""An absolute ``https://`` URL without query, fragment or trailing slash."""

PublicUrl = Annotated[str, AfterValidator(_public_url)]
"""An absolute ``http(s)://`` URL without query, fragment or trailing slash."""

Size = Annotated[int, BeforeValidator(parse_size), Field(gt=0)]
"""A positive byte count such as ``100Gi``."""

Duration = Annotated[timedelta, BeforeValidator(parse_duration)]
"""A positive whole-second duration such as ``5m``."""

_Channel = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")]
_Subdir = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PypiSettings(_Model):
    """PyPI: the simple index and the host that serves the files it links to."""

    index: HttpsUrl = "https://pypi.org/simple"
    files: HttpsUrl = "https://files.pythonhosted.org"


class CondaSettings(_Model):
    """conda: the channel host, and the channels and subdirs the cache serves."""

    base: HttpsUrl = "https://conda.anaconda.org"
    channels: Annotated[
        tuple[_Channel, ...], Strict(False), Field(min_length=1), AfterValidator(_unique)
    ] = ("conda-forge",)
    subdirs: Annotated[
        tuple[_Subdir, ...], Strict(False), Field(min_length=1), AfterValidator(_unique)
    ] = ("linux-64", "linux-aarch64", "noarch")


class CargoSettings(_Model):
    """Rust crates: the sparse index and the crate download host."""

    index: HttpsUrl = "https://index.crates.io"
    download: HttpsUrl = "https://static.crates.io/crates"


class GoSettings(_Model):
    """Go modules: the module proxy and the checksum database."""

    proxy: HttpsUrl = "https://proxy.golang.org"
    sumdb: HttpsUrl = "https://sum.golang.org"


class NpmSettings(_Model):
    """npm: the registry, which serves both packuments and tarballs."""

    registry: HttpsUrl = "https://registry.npmjs.org"


class OpamSettings(_Model):
    """opam: an HTTP repository, which serves its index and its archive cache."""

    repository: HttpsUrl = "https://opam.ocaml.org"


class HackageSettings(_Model):
    """Hackage: a hackage-security repository (signed metadata, index and packages)."""

    repository: HttpsUrl = "https://hackage.haskell.org"


class Settings(_Model):
    """Settings for one package cache."""

    listen: Annotated[str, AfterValidator(_check_listen)] = "127.0.0.1:8090"
    public_url: PublicUrl
    """How sandboxes reach the cache; rewritten index links point here."""
    cache_dir: Path
    max_size: Size = 100 * _GIB
    metadata_ttl: Duration = timedelta(minutes=5)
    stale_if_error: Duration = timedelta(days=1)
    max_artifact_size: Size = 4 * _GIB
    max_metadata_size: Size = 1 * _GIB
    max_rewrite_size: Size = 128 * _MIB
    """The largest index body the cache rewrites: rewriting parses the whole body in memory."""
    max_temp_size: Size = 16 * _GIB
    """Bytes that downloads in progress may hold in the temp directory."""
    max_artifact_downloads: Annotated[int, Field(ge=1)] = 16
    """Package files that may download at once; the upstream pool keeps more for index data."""
    max_requests: Annotated[int, Field(ge=1)] = 1024
    """Client requests served at once; more are answered 503."""
    upstream_rate: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 20.0
    """Upstream requests per second, shared by all clients."""
    upstream_burst: Annotated[int, Field(ge=1)] = 200
    pypi: PypiSettings | None = None
    conda: CondaSettings | None = None
    cargo: CargoSettings | None = None
    go: GoSettings | None = None
    npm: NpmSettings | None = None
    opam: OpamSettings | None = None
    hackage: HackageSettings | None = None

    @model_validator(mode="after")
    def _limits(self) -> Self:
        limits = {
            "max_artifact_size": self.max_artifact_size,
            "max_metadata_size": self.max_metadata_size,
        }
        for name, limit in limits.items():
            if limit > self.max_size:
                raise ValueError(f"{name} must not be larger than max_size")
        for name, limit in limits.items():
            if limit > self.max_temp_size:
                # A download of that size could never fit in the temp space.
                raise ValueError(f"{name} must not be larger than max_temp_size")
        if (
            "max_rewrite_size" in self.model_fields_set
            and self.max_rewrite_size > self.max_metadata_size
        ):
            raise ValueError("max_rewrite_size must not be larger than max_metadata_size")
        return self

    @property
    def temp_limit(self) -> int:
        """Bytes downloads in progress may hold: ``max_temp_size``, at most ``max_size``."""
        return min(self.max_temp_size, self.max_size)

    @property
    def rewrite_limit(self) -> int:
        """The largest body rewritten: ``max_rewrite_size``, at most ``max_metadata_size``."""
        return min(self.max_rewrite_size, self.max_metadata_size)

    @property
    def listen_host(self) -> str:
        """The address to listen on, without IPv6 brackets."""
        return _split_listen(self.listen)[0]

    @property
    def listen_port(self) -> int:
        """The port to listen on."""
        return _split_listen(self.listen)[1]

    def upstreams(self) -> tuple[str, ...]:
        """The upstream base URLs of the enabled ecosystems: the only places the cache fetches.

        Redirects that upstreams answer with may lead elsewhere (over https).
        """
        urls: list[str] = []
        if self.pypi is not None:
            urls += [self.pypi.index, self.pypi.files]
        if self.conda is not None:
            urls.append(self.conda.base)
        if self.cargo is not None:
            urls += [self.cargo.index, self.cargo.download]
        if self.go is not None:
            urls += [self.go.proxy, self.go.sumdb]
        if self.npm is not None:
            urls.append(self.npm.registry)
        if self.opam is not None:
            urls.append(self.opam.repository)
        if self.hackage is not None:
            urls.append(self.hackage.repository)
        return tuple(dict.fromkeys(urls))


def load_settings(path: Path) -> Settings:
    """Read and validate a settings file."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"cannot read settings {path}: {exc}") from exc
    cache_dir = data.get("cache_dir")
    if isinstance(cache_dir, str) and cache_dir:
        data["cache_dir"] = path.absolute().parent / cache_dir
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(f"invalid settings {path}: {exc}") from exc
