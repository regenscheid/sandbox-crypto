# SPDX-License-Identifier: MIT
"""Tests for the Cargo adapter (docs/PACKAGE-CACHE.md, "URL layout").

``fixtures/cargo`` holds responses captured from crates.io (``manifest.json`` says
where each came from): its ``config.json`` and the index files of crates with
one-, two-, three- and longer names, an upper-case name (``Inflector``),
pre-releases (``rand_core``) and build metadata (``wasi``). The tests derive
request paths from them the way Cargo does, and check that every published
version routes to the right upstream file and digest. The negative tests break
one rule at a time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.package_cache.adapter import Adapter, Digest, Fetch, Kind, Static
from sandbox_science.package_cache.adapters.cargo import (
    MAX_CRATE_NAME_LENGTH,
    CargoAdapter,
    crate_index_path,
    is_crate_name,
    is_semver,
)
from sandbox_science.policy import classify_package_request

FIXTURES = Path(__file__).parent / "fixtures" / "cargo"
INDEX = "https://index.crates.io"
DOWNLOAD = "https://static.crates.io/crates"
PUBLIC_URL = "http://host.openshell.internal:8090"
SHA = "0123456789abcdef" * 4  # a synthetic checksum
U64_MAX = str(2**64 - 1)
U64_OVERFLOW = str(2**64)


def make_adapter(public_url: str = PUBLIC_URL) -> CargoAdapter:
    return CargoAdapter(index=INDEX, download=DOWNLOAD, public_url=public_url)


ADAPTER = make_adapter()


def fetch(path: str) -> Fetch:
    answer = ADAPTER.route(path, {})
    assert isinstance(answer, Fetch), path
    return answer


def manifest() -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return data["fixtures"]


def index_fixtures() -> list[dict[str, Any]]:
    return [entry for entry in manifest() if "crate" in entry]


def index_lines(entry: dict[str, Any]) -> list[dict[str, Any]]:
    text = (FIXTURES / entry["file"]).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def config_template() -> str:
    answer = ADAPTER.route("index/config.json", {})
    assert isinstance(answer, Static)
    return json.loads(answer.body)["dl"]


def crate_url(template: str, name: str, version: str, checksum: str) -> str:
    """Cargo's crate_url for a template with markers (cargo-util's registry.rs)."""
    return (
        template.replace("{crate}", name)
        .replace("{version}", version)
        .replace("{sha256-checksum}", checksum)
    )


def test_satisfies_the_adapter_contract() -> None:
    adapter: Adapter = make_adapter()
    assert adapter.prefix == "cargo"


# --- config.json ------------------------------------------------------------------


def test_config_json_points_downloads_back_at_the_cache() -> None:
    answer = ADAPTER.route("index/config.json", {})
    assert isinstance(answer, Static)
    assert answer.status == 200
    assert answer.content_type == "application/json"
    assert json.loads(answer.body) == {
        "dl": "http://host.openshell.internal:8090/cargo/dl/{crate}/{version}/{sha256-checksum}"
    }


def test_config_json_drops_what_crates_io_offers_beyond_downloads() -> None:
    # crates.io's own config names its web API (publish, yank, owners, login); the
    # cache's names none, and does not ask Cargo for credentials either.
    crates_io = json.loads((FIXTURES / "config.json").read_text(encoding="utf-8"))
    assert {"dl", "api"} <= crates_io.keys()
    answer = ADAPTER.route("index/config.json", {})
    assert isinstance(answer, Static)
    assert set(json.loads(answer.body)) == {"dl"}


def test_config_json_follows_public_url() -> None:
    adapter = make_adapter("https://cache.example:8443/packages")
    answer = adapter.route("index/config.json", {})
    assert isinstance(answer, Static)
    assert json.loads(answer.body)["dl"] == (
        "https://cache.example:8443/packages/cargo/dl/{crate}/{version}/{sha256-checksum}"
    )


def test_crates_io_dl_has_no_markers() -> None:
    # With no markers Cargo appends /{crate}/{version}/download to crates.io's dl; the
    # cache fetches the .crate path crates.io documents for its CDN instead (same bytes).
    crates_io = json.loads((FIXTURES / "config.json").read_text(encoding="utf-8"))
    assert crates_io["dl"] == DOWNLOAD
    assert "{" not in crates_io["dl"]


# --- index files ------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "1/a",
        "1/z",
        "2/cc",
        "2/md",
        "3/s/syn",
        "3/f/fnv",
        "ca/rg/cargo",
        "cf/g-/cfg-if",
        "se/rd/serde_json",
        "in/fl/inflector",
        "x8/6_/x86_64",
        "a-/b_/a-b_c",
        "ab/cd/abcd",
        f"aa/aa/{'a' * MAX_CRATE_NAME_LENGTH}",
    ],
)
def test_index_files(relative: str) -> None:
    answer = fetch(f"index/{relative}")
    assert answer.url == f"{INDEX}/{relative}"
    assert answer.key == f"cargo/index/{relative}"
    assert answer.kind is Kind.METADATA
    assert not answer.immutable
    assert answer.digest is None
    assert answer.rewrite is None
    assert answer.headers == {}


def test_captured_index_files_route_to_their_source() -> None:
    entries = index_fixtures()
    assert len(entries) == 7
    for entry in entries:
        answer = fetch(entry["path"])
        assert answer.url == entry["url"]
        assert answer.key == f"cargo/{entry['path']}"
        assert answer.kind is Kind.METADATA
        assert not answer.immutable


def test_index_layout_matches_crates_io() -> None:
    # Every crate in a captured file lives at the path Cargo computes from its name.
    for entry in index_fixtures():
        names = {line["name"] for line in index_lines(entry)}
        assert len(names) == 1
        (name,) = names
        assert f"index/{crate_index_path(name.lower())}" == entry["path"]


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("a", "1/a"),
        ("ab", "2/ab"),
        ("abc", "3/a/abc"),
        ("abcd", "ab/cd/abcd"),
        ("abcde", "ab/cd/abcde"),
        ("cargo", "ca/rg/cargo"),
        ("serde_json", "se/rd/serde_json"),
    ],
)
def test_crate_index_path(name: str, path: str) -> None:
    assert crate_index_path(name) == path


def test_crate_index_path_needs_a_name() -> None:
    with pytest.raises(ValueError, match="empty"):
        crate_index_path("")


# --- crate files ------------------------------------------------------------------


def test_every_captured_version_downloads_through_the_dl_template() -> None:
    template = config_template()
    prefix = f"{PUBLIC_URL}/cargo/"
    count = 0
    for entry in index_fixtures():
        for line in index_lines(entry):
            name, version, checksum = line["name"], line["vers"], line["cksum"]
            url = crate_url(template, name, version, checksum)
            assert url.startswith(prefix)
            answer = fetch(url.removeprefix(prefix))
            assert answer.url == f"{DOWNLOAD}/{name}/{name}-{version}.crate"
            assert answer.key == f"cargo/dl/{name}/{version}/{checksum}"
            assert answer.kind is Kind.ARTIFACT
            assert answer.immutable
            assert answer.digest == Digest("sha256", checksum)
            assert answer.rewrite is None
            count += 1
    assert count == 124


@pytest.mark.parametrize(
    ("crate", "version"),
    [
        ("cfg-if", "1.0.0"),
        ("Inflector", "0.11.4"),
        ("wasi", "0.11.0+wasi-snapshot-preview1"),
        ("wasi", "0.12.0+wasi-0.2.0"),
        ("rand_core", "0.9.0-alpha.1"),
        ("rand_core", "0.10.0-rc-1"),
        ("syn", "0.10.0-rc1"),
        ("a", "0.0.0"),
        ("A", "0.0.0"),
        ("x", "1.0.0-0.3.7"),
        ("x", "1.0.0-x.7.z.92"),
        ("x", "1.0.0-x-y-z.--"),
        ("x", "1.0.0-0a"),
        ("x", "1.0.0-alpha+001"),
        ("x", "1.0.0+20130313144700"),
        ("x", "1.0.0-beta+exp.sha.5114f85"),
        ("x", "1.0.0+21AF26D3----117B344092BD"),
        ("x", f"{U64_MAX}.{U64_MAX}.{U64_MAX}"),
        ("a" * MAX_CRATE_NAME_LENGTH, "1.0.0"),
    ],
)
def test_crate_files(crate: str, version: str) -> None:
    answer = fetch(f"dl/{crate}/{version}/{SHA}")
    assert answer.url == f"{DOWNLOAD}/{crate}/{crate}-{version}.crate"
    assert answer.key == f"cargo/dl/{crate}/{version}/{SHA}"
    assert answer.kind is Kind.ARTIFACT
    assert answer.immutable
    assert answer.digest == Digest("sha256", SHA)


def test_request_headers_do_not_change_the_answer() -> None:
    headers = {"if-none-match": '"abc"', "if-modified-since": "Sat, 10 Oct 2026 00:00:00 GMT"}
    for path in ("index/config.json", "index/cf/g-/cfg-if", f"dl/cfg-if/1.0.0/{SHA}"):
        assert ADAPTER.route(path, headers) == ADAPTER.route(path, {})


# --- refused paths ----------------------------------------------------------------

BAD_NAMES = [
    "_foo",
    "-foo",
    "0foo",
    "foo.bar",
    "foo bar",
    "foo+bar",
    "foo!",
    "f\u00f8o",
    "\uff46oo",  # fullwidth f
    "a" * (MAX_CRATE_NAME_LENGTH + 1),
]

BAD_VERSIONS = [
    "",
    "1",
    "1.0",
    "1.0.0.0",
    "v1.0.0",
    "01.0.0",
    "1.00.0",
    "1.0.00",
    "-1.0.0",
    "1.0.0-",
    "1.0.0+",
    "1.0.0-01",
    "1.0.0-alpha.01",
    "1.0.0-a..b",
    "1.0.0-a.",
    "1.0.0-.a",
    "1.0.0+a..b",
    "1.0.0+a.",
    "1.0.0-a_b",
    "1.0.0+a_b",
    "1.0.0+a+b",
    "1.0.0-alpha+",
    "1.0.0 ",
    "1.0.0%2Bbuild",
    "1.0.0-\u00e9",
    "\u0661.0.0",  # Arabic-Indic digit one
    f"{U64_OVERFLOW}.0.0",
    f"1.{U64_OVERFLOW}.0",
    f"1.0.{U64_OVERFLOW}",
]

BAD_CHECKSUMS = [
    "",
    "-",
    SHA[:-1],
    SHA + "0",
    SHA.upper(),
    "g" * 64,
    "a" * 128,
    SHA[:-1] + " ",
]

REFUSED = [
    "",
    "/",
    "index",
    "index/",
    "index/config.json/",
    "index/config.jsonx",
    "index/CONFIG.json",
    "index/Config.json",
    "index//config.json",
    "/index/config.json",
    "index/config.json?x=1",
    "config.json",
    "api/v1/crates",
    "api/v1/crates/new",
    "api/v1/crates?q=serde",
    "api/v1/crates/serde/1.0.0/download",
    "api/v1/crates/serde/1.0.0/yank",
    "api/v1/me",
    "me",
    # Index files: wrong directory levels or letters.
    "index/1/ab",
    "index/2/a",
    "index/2/abc",
    "index/3/abc",
    "index/3/b/abc",
    "index/3/ab/abc",
    "index/1/1/a",
    "index/1/a/",
    "index/cargo",
    "index/ca/cargo",
    "index/c/ar/cargo",
    "index/ca/rgo/cargo",
    "index/ca/rx/cargo",
    "index/cr/ag/cargo",
    "index/ca/rg/cargo/",
    "index/ca/rg/cargo/extra",
    "index/ca/rg/ca/rg/cargo",
    # Cargo requests index files by the lowercased name.
    "index/in/fl/Inflector",
    "index/In/fl/Inflector",
    "index/IN/FL/INFLECTOR",
    "index/1/A",
    # Names crates.io does not allow.
    "index/1/_",
    "index/1/-",
    "index/1/0",
    "index/2/0a",
    "index/_a/bc/_abcd",
    "index/-a/bc/-abcd",
    "index/9a/bc/9abcd",
    "index/fo/o./foo.bar",
    "index/fo/o+/foo+bar",
    f"index/aa/aa/{'a' * (MAX_CRATE_NAME_LENGTH + 1)}",
    "index/3/\u00e9/\u00e9ab",
    # Dot segments, percent-encoding and query-like characters.
    "index/../config.json",
    "index/./config.json",
    "index/ca/rg/../rg/cargo",
    "index/ca/../ca/rg/cargo",
    "index/ca%2Frg%2Fcargo",
    "index/ca/rg/carg%6F",
    "index/ca/rg/cargo%00",
    "index/ca/rg/cargo?x",
    "index/ca/rg/cargo#x",
    "index/ca/rg/cargo;x",
    "index/ca/rg/cargo\n",
    "index/ca/rg/cargo ",
    "index\\ca\\rg\\cargo",
    # Crate files: wrong shape.
    "dl",
    "dl/",
    "dl/cfg-if",
    "dl/cfg-if/",
    "dl/cfg-if/1.0.0",
    "dl/cfg-if/1.0.0/",
    f"dl/cfg-if/1.0.0/{SHA}/",
    f"dl/cfg-if/1.0.0/{SHA}/extra",
    f"dl/cfg-if/1.0.0/{SHA}/download",
    "dl/cfg-if/1.0.0/download",
    "dl/cfg-if/cfg-if-1.0.0.crate",
    f"dl/cfg-if/cfg-if-1.0.0.crate/{SHA}",
    f"dl//1.0.0/{SHA}",
    f"dl/cfg-if//{SHA}",
    f"/dl/cfg-if/1.0.0/{SHA}",
    f"dl/../index/1.0.0/{SHA}",
    f"dl/cfg-if/../1.0.0/{SHA}",
    f"dl/./cfg-if/1.0.0/{SHA}",
    f"dl/cfg-if%2F1.0.0/{SHA}",
    f"dl/cfg-if/1.0.0/{SHA}?x=1",
    f"dl/cfg-if/1.0.0/{SHA}#x",
    f"DL/cfg-if/1.0.0/{SHA}",
    *(f"dl/{name}/1.0.0/{SHA}" for name in BAD_NAMES),
    *(f"dl/cfg-if/{version}/{SHA}" for version in BAD_VERSIONS),
    *(f"dl/cfg-if/1.0.0/{checksum}" for checksum in BAD_CHECKSUMS),
]


@pytest.mark.parametrize("path", REFUSED)
def test_refused(path: str) -> None:
    assert ADAPTER.route(path, {}) is None


@pytest.mark.parametrize("name", BAD_NAMES)
def test_bad_crate_names(name: str) -> None:
    assert not is_crate_name(name)


@pytest.mark.parametrize("version", BAD_VERSIONS)
def test_bad_versions(version: str) -> None:
    assert not is_semver(version)


@pytest.mark.parametrize(
    "settings",
    [
        {"index": "http://index.crates.io"},
        {"index": "https://index.crates.io/"},
        {"index": "index.crates.io"},
        {"index": "https://index.crates.io?x=1"},
        {"index": "https://index.crates.io#x"},
        {"index": "https://user@index.crates.io"},
        {"index": "https://index.crates.io/a b"},
        {"index": ""},
        {"download": "http://static.crates.io/crates"},
        {"download": "https://static.crates.io/crates/"},
        {"download": "https://static.crates.io/crates/{crate}"},
        {"public_url": "http://host.openshell.internal:8090/"},
        {"public_url": "ftp://host.openshell.internal"},
        {"public_url": "http://host.openshell.internal:8090/{crate}"},
        {"public_url": "http://host.openshell.internal:8090?x=1"},
        {"public_url": "http://user:pass@host.openshell.internal:8090"},
        {"public_url": "host.openshell.internal:8090"},
    ],
)
def test_bad_settings_are_refused(settings: dict[str, str]) -> None:
    arguments = {"index": INDEX, "download": DOWNLOAD, "public_url": PUBLIC_URL} | settings
    with pytest.raises(ValueError, match="must be an absolute"):
        CargoAdapter(**arguments)


# --- agreement with the policy classifier -----------------------------------------


def test_policy_classifies_what_the_adapter_serves() -> None:
    # The policy middleware decides each request with classify_package_request before
    # it reaches the cache: everything the adapter serves must classify the same way.
    config = classify_package_request("GET", "/cargo/index/config.json")
    assert (config.ecosystem, config.name, config.kind) == ("cargo", None, "index")
    for entry in index_fixtures():
        # The policy compares crate names as crates.io does: case and -/_ folded.
        expected = entry["crate"].replace("_", "-")
        index = classify_package_request("GET", f"/cargo/{entry['path']}")
        assert (index.ecosystem, index.name, index.kind) == ("cargo", expected, "index")
        for line in index_lines(entry):
            path = f"dl/{line['name']}/{line['vers']}/{line['cksum']}"
            assert ADAPTER.route(path, {}) is not None
            crate = classify_package_request("GET", f"/cargo/{path}")
            assert (crate.ecosystem, crate.name, crate.kind) == ("cargo", expected, "artifact")
