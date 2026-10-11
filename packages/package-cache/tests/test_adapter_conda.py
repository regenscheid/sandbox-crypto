# SPDX-License-Identifier: MIT
"""Tests for the conda adapter (docs/PACKAGE-CACHE.md).

fixtures/conda records conda-forge on conda.anaconda.org as observed on 2026-10-10
(fixtures/conda/manifest.json): which channel paths exist upstream and how the
adapter must answer them, where repodata shards really live, and a sample of real
package file names that covers every shape among the 1.5 million in repodata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.package_cache.adapter import Adapter, Fetch, Kind
from sandbox_science.package_cache.adapters.conda import (
    CHANNEL_DATA,
    INDEX_FILES,
    CondaAdapter,
    parse_package_filename,
)

FIXTURES = Path(__file__).parent / "fixtures" / "conda"
LAYOUT: dict[str, Any] = json.loads((FIXTURES / "conda-forge-layout.json").read_text())
FILENAMES = (FIXTURES / "filenames.txt").read_text().splitlines()

BASE = "https://conda.anaconda.org"
CHANNEL = "conda-forge"
SUBDIRS = ["linux-64", "linux-aarch64", "noarch"]
SHARD = "eb66076ab6428bdb9093fd1524266c6f6da0933bcabc1719b668eb5aa254a848"


def make_adapter(**settings: Any) -> CondaAdapter:
    return CondaAdapter(**({"base": BASE, "channels": [CHANNEL], "subdirs": SUBDIRS} | settings))


def route(path: str, adapter: CondaAdapter | None = None) -> Fetch | None:
    answer = (adapter or make_adapter()).route(path, {"accept": "*/*"})
    assert answer is None or isinstance(answer, Fetch)
    return answer


def expect_fetch(path: str, upstream_path: str | None = None) -> Fetch:
    """Route ``path`` below the channel and check the parts every answer shares."""
    fetch = route(f"{CHANNEL}/{path}")
    assert fetch is not None, path
    url = f"{BASE}/{CHANNEL}/{upstream_path or path}"
    assert fetch.url == url
    assert fetch.key == url
    assert fetch.digest is None
    assert fetch.rewrite is None
    assert fetch.content_type is None
    assert dict(fetch.headers) == {}
    return fetch


# --- construction -----------------------------------------------------------------------


def test_is_an_adapter() -> None:
    adapter: Adapter = make_adapter()
    assert adapter.prefix == "conda"


@pytest.mark.parametrize(
    "settings",
    [
        {"base": "http://conda.anaconda.org"},
        {"base": "https://conda.anaconda.org/"},
        {"base": "https://conda.anaconda.org?x=1"},
        {"base": "https://user@conda.anaconda.org"},
        {"base": "conda.anaconda.org"},
        {"base": ""},
        {"channels": []},
        {"channels": [""]},
        {"channels": [".."]},
        {"channels": ["../conda-forge"]},
        {"channels": ["conda-forge/label/main"]},
        {"channels": ["conda forge"]},
        {"channels": ["-conda-forge"]},
        {"channels": ["c" * 101]},
        {"subdirs": []},
        {"subdirs": [""]},
        {"subdirs": ["Linux-64"]},
        {"subdirs": ["linux_64"]},
        {"subdirs": ["noarch/"]},
        {"subdirs": [".."]},
        {"subdirs": ["linux--64"]},
    ],
)
def test_rejects_invalid_settings(settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="conda"):
        make_adapter(**settings)


def test_rejects_a_string_where_names_belong() -> None:
    with pytest.raises(TypeError):
        make_adapter(channels="conda-forge")
    with pytest.raises(TypeError):
        make_adapter(subdirs="noarch")


# --- what conda-forge serves ------------------------------------------------------------------


@pytest.mark.parametrize("path", LAYOUT["routed"])
def test_routes_what_conda_forge_serves(path: str) -> None:
    fetch = expect_fetch(path)
    name = path.rpartition("/")[2]
    if name == CHANNEL_DATA or name in INDEX_FILES:
        assert fetch.kind is Kind.METADATA
        assert not fetch.immutable
    elif name.endswith(".msgpack.zst"):
        assert fetch.kind is Kind.METADATA
        assert fetch.immutable
    else:
        assert fetch.kind is Kind.ARTIFACT
        assert fetch.immutable


@pytest.mark.parametrize(("path", "canonical"), sorted(LAYOUT["routed_percent_encoded"].items()))
def test_percent_encoded_epochs_and_local_versions(path: str, canonical: str) -> None:
    fetch = expect_fetch(path, canonical)
    assert fetch.kind is Kind.ARTIFACT
    assert fetch == expect_fetch(canonical)


@pytest.mark.parametrize("path", LAYOUT["routed_absent_upstream"])
def test_routes_the_documented_shards_directory(path: str) -> None:
    fetch = expect_fetch(path)
    assert fetch.kind is Kind.METADATA
    assert fetch.immutable


@pytest.mark.parametrize(
    "path", LAYOUT["refused_present_upstream"] + LAYOUT["refused_absent_upstream"]
)
def test_refuses_other_channel_files(path: str) -> None:
    assert route(f"{CHANNEL}/{path}") is None


@pytest.mark.parametrize("path", FILENAMES)
def test_routes_real_package_files(path: str) -> None:
    fetch = expect_fetch(path)
    assert fetch.kind is Kind.ARTIFACT
    assert fetch.immutable
    filename = path.rpartition("/")[2]
    stem = filename.removesuffix(".conda").removesuffix(".tar.bz2")
    assert parse_package_filename(filename) == tuple(stem.rsplit("-", 2))


@pytest.mark.parametrize(
    ("filename", "parts"),
    [
        ("_libgcc_mutex-0.1-conda_forge.tar.bz2", ("_libgcc_mutex", "0.1", "conda_forge")),
        ("python-3.12.7-hc5c86c4_0_cpython.conda", ("python", "3.12.7", "hc5c86c4_0_cpython")),
        (
            "ca-certificates-2025.1.31-hbd8a1cb_1.conda",
            ("ca-certificates", "2025.1.31", "hbd8a1cb_1"),
        ),
        (
            "aiida-core.atomic_tools-2.0.1-hd8ed1ab_5.tar.bz2",
            ("aiida-core.atomic_tools", "2.0.1", "hd8ed1ab_5"),
        ),
        (
            "sam-2-1.0.0.20240814-cuda120py310hd17e05d_301.conda",
            ("sam-2", "1.0.0.20240814", "cuda120py310hd17e05d_301"),
        ),
        (
            "dagster-airbyte-1!0.26.10-pyh29332c3_0.conda",
            ("dagster-airbyte", "1!0.26.10", "pyh29332c3_0"),
        ),
        (
            "dagster-airbyte-1%210.26.10-pyh29332c3_0.conda",
            ("dagster-airbyte", "1!0.26.10", "pyh29332c3_0"),
        ),
        (
            "py-sirius-ms-2.1%2Bsirius6.0.3-pyhd8ed1ab_0.conda",
            ("py-sirius-ms", "2.1+sirius6.0.3", "pyhd8ed1ab_0"),
        ),
        (
            "py-sirius-ms-2.1%2bsirius6.0.3-pyhd8ed1ab_0.conda",
            ("py-sirius-ms", "2.1+sirius6.0.3", "pyhd8ed1ab_0"),
        ),
        ("esmp-ESMF_6_3_0rp1_ESMP_01-py27_0.tar.bz2", ("esmp", "ESMF_6_3_0rp1_ESMP_01", "py27_0")),
        (
            "ambertools-23.6-cuda_None_mpich_py310h313cf14_6.conda",
            ("ambertools", "23.6", "cuda_None_mpich_py310h313cf14_6"),
        ),
        ("x-1-0.conda", ("x", "1", "0")),
        ("foo-1.0-0-1.conda", ("foo-1.0", "0", "1")),
    ],
)
def test_package_names(filename: str, parts: tuple[str, str, str]) -> None:
    assert parse_package_filename(filename) == parts
    canonical = filename.replace("%21", "!").replace("%2B", "+").replace("%2b", "+")
    assert expect_fetch(f"noarch/{filename}", f"noarch/{canonical}").kind is Kind.ARTIFACT


@pytest.mark.parametrize(
    "filename",
    [
        "foo-1.0.conda",
        "foo.conda",
        "foo-1.0-0.zip",
        "foo-1.0-0.tar.gz",
        "foo-1.0-0.whl",
        "foo-1.0-0.conda.part",
        "foo-1.0-0.tar.bz2.sig",
        "foo-1.0-0.CONDA",
        "foo-1.0-0.tar.bz2/",
        "Foo-1.0-0.conda",
        "foo--1.0-0.conda",
        "-foo-1.0-0.conda",
        ".foo-1.0-0.conda",
        "foo.-1.0-0.conda",
        "foo-1.0-.conda",
        "foo--0.conda",
        "foo-.1-0.conda",
        "foo-1.0-.0.conda",
        "foo-1.0*-0.conda",
        "foo-1.0-0;.conda",
        "f\N{LATIN SMALL LETTER O WITH DIAERESIS}o-1.0-0.conda",
        # Build strings with "* *" are in conda-forge's repodata, but are not file names
        # the cache will pass on.
        "code-aster-17.3.16-py310*%20*_cpython_nompi_h76c0163_100.conda",
        "code-aster-17.3.16-py310* *_cpython_nompi_h76c0163_100.conda",
        # Only %21 and %2B are decoded; anything else stays an error.
        "foo-1.0-0%2Econda",
        "foo%2D1.0-0.conda",
        "foo-1.0-h%2F..%2F-0.conda",
        "foo-1.0-0.conda%00",
        "foo-1.0%252B1-0.conda",
        "%21foo-1.0-0.conda",
        "foo-1.0-0.conda?x=1",
        "foo-1.0-0.conda#x",
        "a" * 250 + "-1.0-0.conda",
    ],
)
def test_refuses_malformed_package_files(filename: str) -> None:
    assert parse_package_filename(filename) is None
    assert route(f"{CHANNEL}/noarch/{filename}") is None


@pytest.mark.parametrize(
    "path",
    [
        # Channels and subdirs that are not configured, in any spelling.
        "bioconda/noarch/repodata.json",
        "pytorch/linux-64/repodata.json",
        "Conda-Forge/noarch/repodata.json",
        "conda-forge2/noarch/repodata.json",
        "conda-forge./noarch/repodata.json",
        "conda-forge%2Fnoarch/repodata.json",
        "conda-forge/osx-arm64/repodata.json",
        "conda-forge/win-64/requests-2.32.3-pyhd8ed1ab_1.conda",
        "conda-forge/Noarch/repodata.json",
        "conda-forge/linux-ppc64le/repodata.json",
        "conda-forge/label/main/noarch/repodata.json",
        "conda-forge/label/broken/noarch/repodata.json",
        # Traversal and empty segments.
        "..",
        "../conda-forge/noarch/repodata.json",
        "conda-forge/../noarch/repodata.json",
        "conda-forge/noarch/../linux-64/repodata.json",
        "conda-forge/./noarch/repodata.json",
        "conda-forge/noarch/./repodata.json",
        "conda-forge/%2E%2E/noarch/repodata.json",
        "conda-forge/noarch%2Frepodata.json",
        "conda-forge/noarch/..%2Frepodata.json",
        "conda-forge//repodata.json",
        "conda-forge/noarch//repodata.json",
        "/conda-forge/noarch/repodata.json",
        "conda-forge/noarch/repodata.json/",
        "conda-forge/noarch/sub/repodata.json",
        # Paths that are not files.
        "",
        "conda-forge",
        "conda-forge/",
        "conda-forge/noarch",
        "conda-forge/noarch/",
        "conda-forge/noarch/shards",
        "conda-forge/noarch/shards/",
        # Files in the wrong place, or that the cache does not serve.
        "conda-forge/repodata.json",
        "conda-forge/noarch/channeldata.json",
        "conda-forge/channeldata.json/",
        "conda-forge/CHANNELDATA.json",
        "conda-forge/noarch/repodata.JSON",
        "conda-forge/noarch/current_repodata.json.xz",
        "conda-forge/noarch/repodata_shards.msgpack",
        "conda-forge/noarch/repodata_shards.msgpack.zst.bak",
        "conda-forge/noarch/patch_instructions.json",
        "conda-forge/noarch/index.html",
        "conda-forge/noarch/.cache",
        # Shards must be named by a lowercase SHA-256.
        f"conda-forge/noarch/{SHARD.upper()}.msgpack.zst",
        f"conda-forge/noarch/{SHARD[:-1]}.msgpack.zst",
        f"conda-forge/noarch/{SHARD}0.msgpack.zst",
        f"conda-forge/noarch/{SHARD}.msgpack",
        f"conda-forge/noarch/{SHARD}.msgpack.zstd",
        f"conda-forge/noarch/shards/shards/{SHARD}.msgpack.zst",
        f"conda-forge/noarch/shards/{SHARD}.msgpack.zst/",
        f"conda-forge/shards/{SHARD}.msgpack.zst",
        "conda-forge/noarch/shards/requests-2.32.3-pyhd8ed1ab_1.conda",
        "conda-forge/noarch/shards/repodata.json",
    ],
)
def test_refuses_other_paths(path: str) -> None:
    assert route(path) is None


def test_serves_only_the_configured_channels_and_subdirs() -> None:
    adapter = make_adapter(channels=["conda-forge", "bioconda"], subdirs=["noarch"])
    fetch = route("bioconda/noarch/repodata.json", adapter)
    assert fetch is not None
    assert fetch.url == f"{BASE}/bioconda/noarch/repodata.json"
    assert route("bioconda/channeldata.json", adapter) is not None
    assert route("conda-forge/linux-64/repodata.json", adapter) is None
    assert route("conda-forge/linux-64/_libgcc_mutex-0.1-conda_forge.tar.bz2", adapter) is None


def test_base_url_with_a_path() -> None:
    adapter = make_adapter(base="https://mirror.example:8443/conda")
    fetch = route(f"{CHANNEL}/noarch/repodata.json.zst", adapter)
    assert fetch is not None
    assert fetch.url == "https://mirror.example:8443/conda/conda-forge/noarch/repodata.json.zst"


def test_headers_do_not_change_the_answer() -> None:
    adapter = make_adapter()
    path = f"{CHANNEL}/noarch/repodata.json"
    assert adapter.route(path, {}) == adapter.route(path, {"accept": "application/json"})
