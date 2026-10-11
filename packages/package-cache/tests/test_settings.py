# SPDX-License-Identifier: MIT
"""The settings file."""

import re
from datetime import timedelta
from pathlib import Path

import pytest

from sandbox_science.package_cache.adapters.hackage import HackageAdapter
from sandbox_science.package_cache.adapters.opam import OpamAdapter
from sandbox_science.package_cache.errors import SettingsError
from sandbox_science.package_cache.settings import (
    CargoSettings,
    CondaSettings,
    GoSettings,
    HackageSettings,
    NpmSettings,
    OpamSettings,
    PypiSettings,
    load_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "docs" / "PACKAGE-CACHE.md"
MIB = 1024**2
GIB = 1024**3
BASE = 'public_url = "http://cache.test:8090"\ncache_dir = "cache"\n'


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cache.toml"
    path.write_text(text)
    return path


def documented_example() -> str:
    """The TOML block in the "Settings" section of docs/PACKAGE-CACHE.md."""
    section = DOC.read_text().split("\n## Settings\n", 1)[1]
    match = re.search(r"```toml\n(.*?)```", section, re.DOTALL)
    assert match is not None
    return match.group(1)


def test_the_documented_example_is_valid(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, documented_example()))
    assert settings.listen == "127.0.0.1:8090"
    assert (settings.listen_host, settings.listen_port) == ("127.0.0.1", 8090)
    assert settings.public_url == "http://host.openshell.internal:8090"
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.max_size == 100 * GIB
    assert settings.metadata_ttl == timedelta(minutes=5)
    assert settings.stale_if_error == timedelta(days=1)
    assert settings.max_artifact_size == 4 * GIB
    assert settings.max_metadata_size == GIB
    assert settings.max_rewrite_size == settings.rewrite_limit == 128 * MIB
    assert settings.max_temp_size == settings.temp_limit == 16 * GIB
    assert settings.max_artifact_downloads == 16
    assert settings.max_requests == 1024
    assert settings.upstream_rate == 20
    assert settings.upstream_burst == 200
    assert settings.pypi == PypiSettings(
        index="https://pypi.org/simple", files="https://files.pythonhosted.org"
    )
    assert settings.conda == CondaSettings(
        base="https://conda.anaconda.org",
        channels=("conda-forge",),
        subdirs=("linux-64", "linux-aarch64", "noarch"),
    )
    assert settings.cargo == CargoSettings(
        index="https://index.crates.io", download="https://static.crates.io/crates"
    )
    assert settings.go == GoSettings(
        proxy="https://proxy.golang.org", sumdb="https://sum.golang.org"
    )
    assert settings.npm == NpmSettings(registry="https://registry.npmjs.org")
    assert settings.opam == OpamSettings(repository="https://opam.ocaml.org")
    assert settings.hackage == HackageSettings(repository="https://hackage.haskell.org")
    assert settings.upstreams() == (
        "https://pypi.org/simple",
        "https://files.pythonhosted.org",
        "https://conda.anaconda.org",
        "https://index.crates.io",
        "https://static.crates.io/crates",
        "https://proxy.golang.org",
        "https://sum.golang.org",
        "https://registry.npmjs.org",
        "https://opam.ocaml.org",
        "https://hackage.haskell.org",
    )


def test_absent_tables_disable_their_ecosystem(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, BASE))
    tables = (settings.pypi, settings.conda, settings.cargo, settings.go, settings.npm)
    assert tables == (None, None, None, None, None)
    assert (settings.opam, settings.hackage) == (None, None)
    assert settings.upstreams() == ()
    assert settings.listen == "127.0.0.1:8090"
    assert settings.max_size == 100 * GIB
    assert settings.metadata_ttl == timedelta(minutes=5)
    assert (settings.max_rewrite_size, settings.max_temp_size) == (128 * MIB, 16 * GIB)
    assert (settings.max_artifact_downloads, settings.max_requests) == (16, 1024)


def test_default_limits_shrink_to_fit_smaller_ones(tmp_path: Path) -> None:
    text = BASE + 'max_size = "8Gi"\nmax_artifact_size = "1Gi"\nmax_metadata_size = "64Mi"\n'
    settings = load_settings(write(tmp_path, text))
    assert settings.max_rewrite_size == 128 * MIB
    assert settings.rewrite_limit == 64 * MIB
    assert settings.max_temp_size == 16 * GIB
    assert settings.temp_limit == 8 * GIB


def test_explicit_limits_are_kept(tmp_path: Path) -> None:
    text = BASE + (
        'max_rewrite_size = "1Gi"\nmax_temp_size = "4Gi"\n'
        "max_artifact_downloads = 4\nmax_requests = 100\n"
    )
    settings = load_settings(write(tmp_path, text))
    assert (settings.rewrite_limit, settings.temp_limit) == (GIB, 4 * GIB)
    assert (settings.max_artifact_downloads, settings.max_requests) == (4, 100)


def test_an_empty_table_enables_its_ecosystem_with_the_defaults(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, BASE + "[npm]\n[go]\n[hackage]\n[opam]\n"))
    assert settings.npm == NpmSettings()
    assert settings.go == GoSettings()
    assert settings.opam == OpamSettings()
    assert settings.hackage == HackageSettings()
    assert settings.upstreams() == (
        "https://proxy.golang.org",
        "https://sum.golang.org",
        "https://registry.npmjs.org",
        "https://opam.ocaml.org",
        "https://hackage.haskell.org",
    )


def test_urls_are_normalized(tmp_path: Path) -> None:
    text = (
        'public_url = "https://Cache.Test:8443/"\ncache_dir = "cache"\n'
        '[pypi]\nindex = "https://PyPI.org/simple/"\nfiles = "https://files.example/"\n'
    )
    settings = load_settings(write(tmp_path, text))
    assert settings.public_url == "https://cache.test:8443"
    assert settings.pypi is not None
    assert settings.pypi.index == "https://pypi.org/simple"
    assert settings.pypi.files == "https://files.example"


def test_an_absolute_cache_dir_is_kept(tmp_path: Path) -> None:
    text = 'public_url = "http://cache.test"\ncache_dir = "/var/cache/packages"\n'
    assert load_settings(write(tmp_path, text)).cache_dir == Path("/var/cache/packages")


@pytest.mark.parametrize(
    ("listen", "host", "port"),
    [
        ("127.0.0.1:8090", "127.0.0.1", 8090),
        ("[::1]:9000", "::1", 9000),
        ("cache.internal:80", "cache.internal", 80),
        ("0.0.0.0:65535", "0.0.0.0", 65535),  # noqa: S104 - a listen address under test
    ],
)
def test_listen_addresses(tmp_path: Path, listen: str, host: str, port: int) -> None:
    settings = load_settings(write(tmp_path, BASE + f'listen = "{listen}"\n'))
    assert (settings.listen_host, settings.listen_port) == (host, port)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param('cache_dir = "c"\n', "public_url", id="no-public-url"),
        pytest.param('public_url = "http://c.test"\n', "cache_dir", id="no-cache-dir"),
        pytest.param('public_url = "http://c.test"\ncache_dir = ""\n', "cache_dir", id="empty-dir"),
        pytest.param(
            'public_url = "cache.test:8090"\ncache_dir = "c"\n', "absolute", id="relative-url"
        ),
        pytest.param('public_url = "ftp://c.test"\ncache_dir = "c"\n', "absolute", id="ftp"),
        pytest.param('public_url = "http://c.test/?a=1"\ncache_dir = "c"\n', "query", id="query"),
        pytest.param(
            'public_url = "http://c.test/#x"\ncache_dir = "c"\n', "fragment", id="fragment"
        ),
        pytest.param(
            'public_url = "http://user:pw@c.test"\ncache_dir = "c"\n', "credentials", id="userinfo"
        ),
        pytest.param('public_url = "http://"\ncache_dir = "c"\n', "host", id="no-host"),
        pytest.param('public_url = "http://c.test:99999"\ncache_dir = "c"\n', "port", id="port"),
        pytest.param('public_url = "http://c.test:0"\ncache_dir = "c"\n', "port", id="port-0"),
        pytest.param('public_url = "http://c test"\ncache_dir = "c"\n', "spaces", id="space"),
        pytest.param('public_url = "http://c.tést"\ncache_dir = "c"\n', "ASCII", id="non-ascii"),
        pytest.param(BASE + '[pypi]\nindex = "http://pypi.org/simple"\n', "https", id="http-index"),
        pytest.param(BASE + '[npm]\nregistry = "https://r.test/a/../b"\n', r"\.\.", id="dots"),
        pytest.param(
            BASE + '[npm]\nregistry = "https://r.test/%2e%2e/b"\n', r"\.\.", id="dots-enc"
        ),
        pytest.param(BASE + "[conda]\nchannels = []\n", "channels", id="no-channels"),
        pytest.param(BASE + '[conda]\nchannels = ["conda forge"]\n', "channels", id="channel"),
        pytest.param(BASE + '[conda]\nchannels = ["a", "a"]\n', "unique", id="duplicate"),
        pytest.param(BASE + '[conda]\nsubdirs = ["Linux-64"]\n', "subdirs", id="subdir"),
        pytest.param(BASE + '[conda]\nchannels = "conda-forge"\n', "channels", id="not-a-list"),
        pytest.param(BASE + "unknown = 1\n", "unknown", id="unknown-key"),
        pytest.param(BASE + '[pypi]\nmirror = "https://m.test"\n', "mirror", id="unknown-in-table"),
        pytest.param(BASE + 'pypi = "yes"\n', "pypi", id="not-a-table"),
        pytest.param(BASE + "[maven]\n", "maven", id="unknown-ecosystem"),
        pytest.param(
            BASE + '[opam]\nrepository = "http://opam.ocaml.org"\n', "https", id="http-opam"
        ),
        pytest.param(
            BASE + '[hackage]\nrepository = "https://h.test/?x"\n', "query", id="hackage-query"
        ),
        pytest.param(BASE + '[hackage]\nindex = "https://h.test"\n', "index", id="hackage-key"),
        pytest.param(
            BASE + '[opam]\nrepository = "https://opam.ocaml.org/a%2Fb"\n',
            "percent-escapes",
            id="opam-escape",
        ),
        pytest.param(
            BASE + '[hackage]\nrepository = "https://h.test/a b"\n', "spaces", id="hackage-space"
        ),
        pytest.param(
            BASE + '[hackage]\nrepository = "https://h.test/~x/p@q"\n',
            "percent-escapes",
            id="hackage-at",
        ),
        pytest.param(BASE + 'max_size = "100Q"\n', "invalid size", id="size-unit"),
        pytest.param(BASE + "max_size = 0\n", "max_size", id="size-zero"),
        pytest.param(BASE + "max_size = 1.5\n", "max_size", id="size-float"),
        pytest.param(BASE + 'metadata_ttl = "5x"\n', "invalid duration", id="duration"),
        pytest.param(BASE + "metadata_ttl = 300\n", "duration", id="duration-number"),
        pytest.param(BASE + 'stale_if_error = "0s"\n', "positive", id="duration-zero"),
        pytest.param(BASE + "upstream_rate = 0\n", "upstream_rate", id="rate-zero"),
        pytest.param(BASE + 'upstream_rate = "20"\n', "upstream_rate", id="rate-text"),
        pytest.param(BASE + "upstream_rate = inf\n", "upstream_rate", id="rate-inf"),
        pytest.param(BASE + "upstream_burst = 0\n", "upstream_burst", id="burst-zero"),
        pytest.param(BASE + "upstream_burst = 1.5\n", "upstream_burst", id="burst-float"),
        pytest.param(BASE + "upstream_burst = true\n", "upstream_burst", id="burst-bool"),
        pytest.param(
            BASE + 'max_size = "1Gi"\n', "max_artifact_size must not be larger", id="artifact-cap"
        ),
        pytest.param(
            BASE + 'max_size = "8Gi"\nmax_metadata_size = "9Gi"\n',
            "max_metadata_size must not be larger",
            id="metadata-cap",
        ),
        pytest.param(
            BASE + 'max_metadata_size = "64Mi"\nmax_rewrite_size = "128Mi"\n',
            "max_rewrite_size must not be larger than max_metadata_size",
            id="rewrite-cap",
        ),
        pytest.param(
            BASE + 'max_temp_size = "2Gi"\n',
            "max_artifact_size must not be larger than max_temp_size",
            id="temp-below-artifact",
        ),
        pytest.param(
            BASE + 'max_temp_size = "1Gi"\nmax_artifact_size = "1Gi"\nmax_metadata_size = "2Gi"\n',
            "max_metadata_size must not be larger than max_temp_size",
            id="temp-below-metadata",
        ),
        pytest.param(BASE + 'max_temp_size = "0"\n', "max_temp_size", id="temp-zero"),
        pytest.param(BASE + 'max_rewrite_size = "1Q"\n', "invalid size", id="rewrite-unit"),
        pytest.param(
            BASE + "max_artifact_downloads = 0\n", "max_artifact_downloads", id="downloads-zero"
        ),
        pytest.param(
            BASE + 'max_artifact_downloads = "16"\n', "max_artifact_downloads", id="downloads-text"
        ),
        pytest.param(BASE + "max_requests = 0\n", "max_requests", id="requests-zero"),
        pytest.param(BASE + "max_requests = 2.5\n", "max_requests", id="requests-float"),
        pytest.param(BASE + "max_requests = true\n", "max_requests", id="requests-bool"),
        pytest.param(BASE + 'listen = "8090"\n', "listen", id="listen-port-only"),
        pytest.param(BASE + 'listen = "::1:8090"\n', "listen", id="listen-ipv6-brackets"),
        pytest.param(BASE + 'listen = "[::x]:8090"\n', "listen", id="listen-ipv6"),
        pytest.param(BASE + 'listen = "host:0"\n', "listen", id="listen-port-zero"),
        pytest.param(BASE + 'listen = "bad host:80"\n', "listen", id="listen-host"),
        pytest.param(BASE + "listen = 8090\n", "listen", id="listen-number"),
        pytest.param("public_url = [", "cannot read", id="not-toml"),
    ],
)
def test_invalid_settings_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        load_settings(write(tmp_path, text))


@pytest.mark.parametrize(
    "repository",
    [
        "https://opam.ocaml.org",
        "https://opam.ocaml.org/",
        "https://OPAM.ocaml.org:8443/mirror/v2",
        "https://[2001:db8::1]:8443",
        "https://opam.ocaml.org/a%2Fb",
        "https://opam.ocaml.org/a%41",
        "https://opam.ocaml.org/a@b",
        "https://opam.ocaml.org/a:b",
        "https://opam.ocaml.org/a,b",
        "https://opam.ocaml.org/a+b",
        "https://opam.ocaml.org/.",
        "https://user@opam.ocaml.org",
        "http://opam.ocaml.org",
        "https://opam.ocaml.org?x",
        "https://opam_ocaml.org",
    ],
)
def test_repositories_the_settings_accept_are_ones_the_adapters_accept(
    tmp_path: Path, repository: str
) -> None:
    # A setting accepted here but refused by the adapter would stop the service at
    # startup instead of being reported as invalid settings.
    for table, adapter in (("opam", OpamAdapter), ("hackage", HackageAdapter)):
        path = write(tmp_path, BASE + f'[{table}]\nrepository = "{repository}"\n')
        try:
            settings = load_settings(path)
        except SettingsError:
            continue
        configured = settings.opam if table == "opam" else settings.hackage
        assert configured is not None
        adapter(repository=configured.repository)


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="cannot read"):
        load_settings(tmp_path / "missing.toml")
