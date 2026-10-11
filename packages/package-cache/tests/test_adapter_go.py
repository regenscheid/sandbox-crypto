# SPDX-License-Identifier: MIT
"""Tests for the Go adapter (docs/PACKAGE-CACHE.md, "URL layout").

``fixtures/go`` holds responses captured from proxy.golang.org and sum.golang.org
(``manifest.json`` says where each came from): version lists, ``.info``, ``.mod``
and ``@latest`` answers for modules with upper-case letters in their paths
(``github.com/BurntSushi/toml``, ``github.com/Azure/go-autorest`` with
``+incompatible`` versions), the toolchain module ``golang.org/toolchain`` that
GOTOOLCHAIN downloads use, a gopkg.in module and a pseudo-version, plus checksum
database lookups and the latest tree head. The tests derive request paths from
them the way the ``go`` command does. The negative tests break one rule at a time.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.package_cache.adapter import Adapter, Fetch, Kind, Static
from sandbox_science.package_cache.adapters.go import (
    GoAdapter,
    check_module_path,
    decode_module_path,
    decode_version,
    unescape,
)
from sandbox_science.policy import classify_package_request

FIXTURES = Path(__file__).parent / "fixtures" / "go"
PROXY = "https://proxy.golang.org"
SUMDB = "https://sum.golang.org"
ADAPTER = GoAdapter(proxy=PROXY, sumdb=SUMDB)


def escape(text: str) -> str:
    """The proxy's case encoding (module.EscapePath), written independently of the adapter."""
    return "".join(f"!{char.lower()}" if "A" <= char <= "Z" else char for char in text)


def tile(level: str, index: int, width: int | None = None) -> str:
    """A checksum database tile path, as golang.org/x/mod/sumdb/tlog writes it."""
    text = f"{index % 1000:03d}"
    while index >= 1000:
        index //= 1000
        text = f"x{index % 1000:03d}/{text}"
    suffix = f".p/{width}" if width is not None else ""
    return f"sumdb/sum.golang.org/tile/8/{level}/{text}{suffix}"


def fetch(path: str) -> Fetch:
    answer = ADAPTER.route(path, {})
    assert isinstance(answer, Fetch), path
    return answer


def manifest() -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return data["fixtures"]


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def listed_versions() -> Iterator[tuple[str, str]]:
    """(module, version) for every version in the captured @v/list answers."""
    for entry in manifest():
        if entry["path"].endswith("/@v/list"):
            for version in fixture_text(entry["file"]).splitlines():
                yield entry["module"], version


def test_satisfies_the_adapter_contract() -> None:
    adapter: Adapter = ADAPTER
    assert adapter.prefix == "go"


# --- module proxy -----------------------------------------------------------------

MODULES = [
    ("golang.org/x/net", "golang.org/x/net"),
    ("github.com/!burnt!sushi/toml", "github.com/BurntSushi/toml"),
    ("github.com/!azure/go-autorest", "github.com/Azure/go-autorest"),
    (
        "github.com/!azure/azure-sdk-for-go/sdk/azcore",
        "github.com/Azure/azure-sdk-for-go/sdk/azcore",
    ),
    ("golang.org/toolchain", "golang.org/toolchain"),
    ("gopkg.in/yaml.v3", "gopkg.in/yaml.v3"),
    ("gopkg.in/check.v1", "gopkg.in/check.v1"),
    ("gopkg.in/yaml.v0", "gopkg.in/yaml.v0"),
    ("gopkg.in/src-d/go-git.v4", "gopkg.in/src-d/go-git.v4"),
    ("gopkg.in/yaml.v2-unstable", "gopkg.in/yaml.v2-unstable"),
    ("example.com/mod/v2", "example.com/mod/v2"),
    ("example.com/mod/v10", "example.com/mod/v10"),
    ("example.com/v1/sub", "example.com/v1/sub"),
    ("example.com/a_b~c/d-e.f", "example.com/a_b~c/d-e.f"),
    ("example.com/~user", "example.com/~user"),
    ("example.com/x~1y", "example.com/x~1y"),
    ("example.com/com10", "example.com/com10"),
    ("example.com/a..b", "example.com/a..b"),
    ("k8s.io/api", "k8s.io/api"),
    ("xn--bcher-kva.example/m", "xn--bcher-kva.example/m"),
    ("123.example/m", "123.example/m"),
    # The go command looks for the module providing a package by asking for each
    # prefix of its path, down to the first element.
    ("golang.org", "golang.org"),
]

VERSIONS = [
    ("v0.0.0", "v0.0.0"),
    ("v1.4.0", "v1.4.0"),
    ("v14.2.0+incompatible", "v14.2.0+incompatible"),
    ("v0.0.1-go1.21.0.linux-amd64", "v0.0.1-go1.21.0.linux-amd64"),
    ("v0.0.1-go1.9rc2.windows-amd64", "v0.0.1-go1.9rc2.windows-amd64"),
    ("v0.0.0-20191109021931-daa7c04131f5", "v0.0.0-20191109021931-daa7c04131f5"),
    ("v1.2.4-0.20191109021931-daa7c04131f5", "v1.2.4-0.20191109021931-daa7c04131f5"),
    ("v1.2.3-pre.0.20191109021931-daa7c04131f5", "v1.2.3-pre.0.20191109021931-daa7c04131f5"),
    (
        "v2.0.1-20200722182040-012345abcdef+incompatible",
        "v2.0.1-20200722182040-012345abcdef+incompatible",
    ),
    ("v1.0.0-!r!c1", "v1.0.0-RC1"),
    ("v1.0.0-rc.1", "v1.0.0-rc.1"),
    ("v1.0.0-0a.-", "v1.0.0-0a.-"),
    ("v123456789012345678901234567890.0.0", "v123456789012345678901234567890.0.0"),
]


@pytest.mark.parametrize(("escaped", "module"), MODULES)
def test_version_lists(escaped: str, module: str) -> None:
    assert decode_module_path(escaped) == module
    for endpoint in ("@v/list", "@latest"):
        path = f"{escaped}/{endpoint}"
        answer = fetch(path)
        assert answer.url == f"{PROXY}/{path}"
        assert answer.key == f"go/{path}"
        assert answer.kind is Kind.METADATA
        assert not answer.immutable
        assert answer.digest is None
        assert answer.rewrite is None


@pytest.mark.parametrize(("escaped", "module"), MODULES)
@pytest.mark.parametrize(("escaped_version", "version"), VERSIONS)
def test_version_files(escaped: str, module: str, escaped_version: str, version: str) -> None:
    assert decode_version(escaped_version) == version
    for suffix, kind in (
        (".info", Kind.METADATA),
        (".mod", Kind.METADATA),
        (".zip", Kind.ARTIFACT),
    ):
        path = f"{escaped}/@v/{escaped_version}{suffix}"
        answer = fetch(path)
        assert answer.url == f"{PROXY}/{path}"
        assert answer.key == f"go/{path}"
        assert answer.kind is kind
        assert answer.immutable
        # The go command checks modules against the checksum database itself.
        assert answer.digest is None
        assert answer.rewrite is None


@pytest.mark.parametrize(
    ("sent", "forwarded"),
    [
        # cmd/go escapes paths with url.PathEscape, which writes "!" as %21.
        ("github.com/%21burnt%21sushi/toml/@v/list", "github.com/!burnt!sushi/toml/@v/list"),
        ("github.com/%21burnt%21sushi/toml/@latest", "github.com/!burnt!sushi/toml/@latest"),
        (
            "github.com/%21burnt%21sushi/toml/@v/v1.4.0.zip",
            "github.com/!burnt!sushi/toml/@v/v1.4.0.zip",
        ),
        ("github.com/!burnt%21sushi/toml/@v/list", "github.com/!burnt!sushi/toml/@v/list"),
        ("example.com/m/@v/v1.0.0-%21r%21c1.info", "example.com/m/@v/v1.0.0-!r!c1.info"),
        (
            "sumdb/sum.golang.org/lookup/github.com/%21azure/go-autorest@v14.2.0+incompatible",
            "sumdb/sum.golang.org/lookup/github.com/!azure/go-autorest@v14.2.0+incompatible",
        ),
    ],
)
def test_percent_encoded_bang(sent: str, forwarded: str) -> None:
    answer = fetch(sent)
    assert answer == fetch(forwarded)
    assert answer.key == f"go/{forwarded}"
    assert answer.url.endswith(forwarded.removeprefix("sumdb/sum.golang.org"))


# --- checksum database ------------------------------------------------------------


def test_sumdb_supported_is_answered_by_the_cache() -> None:
    answer = ADAPTER.route("sumdb/sum.golang.org/supported", {})
    assert isinstance(answer, Static)
    assert answer.status == 200
    assert answer.body == b""


def test_sumdb_latest() -> None:
    answer = fetch("sumdb/sum.golang.org/latest")
    assert answer.url == f"{SUMDB}/latest"
    assert answer.key == "go/sumdb/sum.golang.org/latest"
    assert answer.kind is Kind.METADATA
    assert not answer.immutable


@pytest.mark.parametrize(("escaped", "module"), MODULES)
@pytest.mark.parametrize(("escaped_version", "version"), VERSIONS)
def test_sumdb_lookups(escaped: str, module: str, escaped_version: str, version: str) -> None:
    relative = f"lookup/{escaped}@{escaped_version}"
    answer = fetch(f"sumdb/sum.golang.org/{relative}")
    assert answer.url == f"{SUMDB}/{relative}"
    assert answer.key == f"go/sumdb/sum.golang.org/{relative}"
    assert answer.kind is Kind.METADATA
    assert answer.immutable


@pytest.mark.parametrize(
    ("path", "immutable"),
    [
        (tile("0", 0), True),
        (tile("0", 999), True),
        (tile("0", 1000), True),
        (tile("0", 102314), True),
        (tile("0", 1_000_000), True),
        (tile("1", 399), True),
        (tile("2", 1), True),
        (tile("7", 0), True),
        (tile("data", 102314), True),
        (tile("0", 2**63 - 1), True),
        (tile("3", 0, 4), False),
        (tile("0", 265895, 124), False),
        (tile("0", 0, 1), False),
        (tile("0", 0, 255), False),
        (tile("data", 265895, 124), False),
    ],
)
def test_sumdb_tiles(path: str, immutable: bool) -> None:
    answer = fetch(path)
    relative = path.removeprefix("sumdb/sum.golang.org/")
    assert answer.url == f"{SUMDB}/{relative}"
    assert answer.key == f"go/{path}"
    assert answer.kind is Kind.METADATA
    assert answer.immutable is immutable


def test_tile_paths_written_by_tlog() -> None:
    assert tile("0", 102314) == "sumdb/sum.golang.org/tile/8/0/x102/314"
    assert tile("1", 1038, 167) == "sumdb/sum.golang.org/tile/8/1/x001/038.p/167"
    assert tile("0", 123456789) == "sumdb/sum.golang.org/tile/8/0/x123/x456/789"
    assert fetch(tile("0", 1_000_000)).url.endswith("/tile/8/0/x001/x000/000")


# --- captured responses -----------------------------------------------------------


def test_captured_responses_route_to_their_source() -> None:
    entries = manifest()
    assert len(entries) == 17
    for entry in entries:
        path: str = entry["path"]
        answer = fetch(path)
        assert answer.url == entry["url"]
        if path.startswith("sumdb/"):
            assert answer.immutable is ("/lookup/" in path)
            continue
        escaped_module = path.partition("/@")[0]
        assert decode_module_path(escaped_module) == entry["module"]
        assert escape(entry["module"]) == escaped_module
        assert answer.immutable is ("/@v/v" in path)


def test_every_listed_version_is_served() -> None:
    count = 0
    for module, version in listed_versions():
        escaped = f"{escape(module)}/@v/{escape(version)}"
        assert decode_version(escape(version)) == version
        assert fetch(f"{escaped}.info").kind is Kind.METADATA
        assert fetch(f"{escaped}.mod").kind is Kind.METADATA
        assert fetch(f"{escaped}.zip").kind is Kind.ARTIFACT
        assert fetch(f"sumdb/sum.golang.org/lookup/{escape(module)}@{escape(version)}").immutable
        count += 1
    # BurntSushi/toml, Azure/go-autorest, golang.org/toolchain (linux-amd64), yaml.v3.
    assert count == 16 + 138 + 366 + 2


def test_info_answers_name_canonical_versions() -> None:
    for entry in manifest():
        if entry["path"].endswith((".info", "/@latest")):
            version = json.loads(fixture_text(entry["file"]))["Version"]
            assert fetch(f"{escape(entry['module'])}/@v/{escape(version)}.zip").immutable


def test_lookups_of_captured_records() -> None:
    for entry in manifest():
        if "/lookup/" not in entry["path"]:
            continue
        # A record: its number, then the go.sum lines for the module and its go.mod.
        lines = fixture_text(entry["file"]).splitlines()
        modules = {tuple(line.split()[:2]) for line in lines[1:3]}
        assert modules == {
            (entry["module"], entry["version"]),
            (entry["module"], f"{entry['version']}/go.mod"),
        }
        path = f"sumdb/sum.golang.org/lookup/{escape(entry['module'])}@{escape(entry['version'])}"
        assert path == entry["path"]
        assert fetch(path).url == entry["url"]


def test_tiles_at_the_edge_of_the_captured_tree() -> None:
    size = int(fixture_text("sumdb-latest.txt").splitlines()[1])
    assert size > 256**3
    for level in range(3):
        # The rightmost tile at each level is partial; the one before it is full.
        index, width = size // 256 ** (level + 1), (size // 256**level) % 256
        assert not fetch(tile(str(level), index, width)).immutable
        assert fetch(tile(str(level), index - 1)).immutable
    assert not fetch(tile("data", size // 256, size % 256)).immutable


# --- decoding ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("escaped", "decoded"),
    [
        ("github.com/!burnt!sushi/toml", "github.com/BurntSushi/toml"),
        ("v1.0.0-!r!c1", "v1.0.0-RC1"),
        ("plain", "plain"),
        ("", ""),
        ("!a!b!c", "ABC"),
    ],
)
def test_unescape(escaped: str, decoded: str) -> None:
    assert unescape(escaped) == decoded


@pytest.mark.parametrize(
    "escaped", ["A", "github.com/BurntSushi", "!", "a!", "!!a", "!1", "!-", "!A", "é"]
)
def test_unescape_refuses(escaped: str) -> None:
    assert unescape(escaped) is None


INVALID_MODULE_PATHS = [
    "",
    "/",
    "golang.org/",
    "/golang.org/x",
    "golang/x/net",  # the first element needs a dot
    "localhost/x",
    "-golang.org/x",
    "golang_org.example/x",
    "Golang.org/x",
    "golang.org:443/x",
    ".golang.org/x",
    "golang.org./x",
    "golang.org/../x",
    "golang.org/./x",
    "golang.org/x/..",
    "golang.org//x",
    "golang.org/x/.net",
    "golang.org/x/net.",
    "golang.org/x/...",
    "golang.org/x/n+t",
    "golang.org/x/n@t",
    "golang.org/x/n et",
    "golang.org/x/nét",
    "golang.org/x/con",
    "golang.org/x/CON",
    "golang.org/x/aux.go",
    "golang.org/x/com1",
    "golang.org/x/lpt9.txt",
    "golang.org/x/exampl~1",
    "golang.org/x/exampl~12.com",
    "golang.org/x/net/v1",
    "golang.org/x/net/v0",
    "golang.org/x/net/v02",
    "golang.org/x/net/v2.0",
    "gopkg.in/yaml",
    "gopkg.in/yaml.v",
    "gopkg.in/yaml.v03",
    "gopkg.in/yaml/v3",
]


@pytest.mark.parametrize("path", INVALID_MODULE_PATHS)
def test_invalid_module_paths(path: str) -> None:
    assert not check_module_path(path)


# --- refused paths ----------------------------------------------------------------

REFUSED = [
    "",
    "/",
    "@v/list",
    "@latest",
    "/golang.org/x/net/@v/list",
    "golang.org/x/net",
    "golang.org/x/net/",
    "golang.org/x/net/@v",
    "golang.org/x/net/@v/",
    "golang.org/x/net/@",
    "golang.org/x/net/@v/list/",
    "golang.org/x/net/@v/list.txt",
    "golang.org/x/net/@latest/",
    "golang.org/x/net/@latest/extra",
    "golang.org/x/net/@v/v1.0.0.info/extra",
    "golang.org/x/net/@vv/list",
    "golang.org/x/net/@V/list",
    "golang.org/x/net/@LATEST",
    "golang.org/x/net/@v/@v/list",
    "golang.org/x/net/@v/v1.0.0",
    "golang.org/x/net/@v/v1.0.0.ziphash",
    "golang.org/x/net/@v/v1.0.0.ZIP",
    "golang.org/x/net/@v/v1.0.0.tar.gz",
    "golang.org/x/net/@v/v1.0.0.info.mod",
    "golang.org/x/net/@v/.info",
    "golang.org/x/net/@v/v1.0.0/.info",
    "golang.org/x/net/@v/x/v1.0.0.info",
    # Upper case where the go command case-encodes.
    "github.com/BurntSushi/toml/@v/list",
    "Golang.org/x/net/@v/list",
    "golang.org/x/net/@v/v1.0.0-RC1.info",
    "golang.org/x/net/@v/V1.0.0.info",
    # Malformed case encoding.
    "github.com/!/toml/@v/list",
    "github.com/toml!/@v/list",
    "github.com/!!toml/@v/list",
    "github.com/!1toml/@v/list",
    "github.com/!-toml/@v/list",
    "!github.com/x/@v/list",
    "golang.org/x/net/@v/v1.0.0-!r!c1!.info",
    "golang.org/x/net/@v/v1.0.0+!incompatible.zip",
    # Percent-encodings other than %21, including encoded slashes and dots.
    "golang.org%2Fx%2Fnet/@v/list",
    "golang.org/x/net%2F@v/list",
    "golang.org/x/net/%40v/list",
    "golang.org/x/net/@v%2Flist",
    "golang.org/x/net/@v/v1.0.0%2Bincompatible.zip",
    "github.com/%2521burnt/toml/@v/list",
    "github.com/%41zure/x/@v/list",
    "github.com/%61zure/x/@v/list",
    "golang.org/x/net/@v/list%00",
    "%2e%2e/golang.org/x/net/@v/list",
    "golang.org/%2e%2e/net/@v/list",
    "github.com/%21/toml/@v/list",
    "github.com/%2/toml/@v/list",
    # Query and fragment characters, spaces, backslashes, controls, non-ASCII.
    "golang.org/x/net/@v/list?x=1",
    "golang.org/x/net/@v/list#x",
    "golang.org/x/net/@latest;x",
    "golang.org/x/n et/@v/list",
    "golang.org\\x\\net/@v/list",
    "golang.org/x/nét/@v/list",
    "golang.org/x/net/@v/list\n",
    "golang.org/x/net/@v/list\x00",
    # Invalid versions: the go command only sends canonical ones here.
    "golang.org/x/net/@v/v1.info",
    "golang.org/x/net/@v/v1.0.info",
    "golang.org/x/net/@v/1.0.0.info",
    "golang.org/x/net/@v/v01.0.0.info",
    "golang.org/x/net/@v/v1.0.00.mod",
    "golang.org/x/net/@v/v1.0.0-01.mod",
    "golang.org/x/net/@v/v1.0.0-.mod",
    "golang.org/x/net/@v/v1.0.0-a..b.mod",
    "golang.org/x/net/@v/v1.0.0+meta.zip",
    "golang.org/x/net/@v/v1.0.0+incompatible+x.zip",
    "golang.org/x/net/@v/v1.0.0+incompatible.x.zip",
    "golang.org/x/net/@v/v1.0.0-rc_1.zip",
    "golang.org/x/net/@v/master.info",
    "golang.org/x/net/@v/daa7c041.info",
    "golang.org/x/net/@v/latest.info",
    "golang.org/x/net/@v/v1.0.0~1.info",
    # Checksum database: other names, malformed lookups and tiles.
    "sumdb",
    "sumdb/",
    "sumdb/sum.golang.org",
    "sumdb/sum.golang.org/",
    "sumdb/sum.golang.org/supported/",
    "sumdb/sum.golang.org/latest/",
    "sumdb/sum.golang.org/latest.txt",
    "sumdb/sum.golang.org/lookup",
    "sumdb/sum.golang.org/lookup/",
    "sumdb/sum.golang.org/lookup/golang.org/x/net",
    "sumdb/sum.golang.org/lookup/golang.org/x/net@",
    "sumdb/sum.golang.org/lookup/@v0.1.0",
    "sumdb/sum.golang.org/lookup/golang.org/x/net@v0.1",
    "sumdb/sum.golang.org/lookup/golang.org/x/net@v0.1.0/go.mod",
    "sumdb/sum.golang.org/lookup/golang.org/x/net@v0.1.0+meta",
    "sumdb/sum.golang.org/lookup/golang.org/x/net@master",
    "sumdb/sum.golang.org/lookup/golang.org/x/net@v0.1.0@v0.2.0",
    "sumdb/sum.golang.org/lookup/github.com/BurntSushi/toml@v1.4.0",
    "sumdb/sum.golang.org/lookup/golang/x/net@v0.1.0",
    "sumdb/sum.golang.org/lookup/golang.org/../net@v0.1.0",
    "sumdb/sum.golang.org/lookup/golang.org/x/net@v0.1.0%2Bincompatible",
    "sumdb/sum.golang.org/lookup/../latest",
    "sumdb/sum.golang.org/tile",
    "sumdb/sum.golang.org/tile/",
    "sumdb/sum.golang.org/tile/8",
    "sumdb/sum.golang.org/tile/8/0",
    "sumdb/sum.golang.org/tile/8/0/",
    "sumdb/sum.golang.org/tile/8/0/1",
    "sumdb/sum.golang.org/tile/8/0/01",
    "sumdb/sum.golang.org/tile/8/0/0001",
    "sumdb/sum.golang.org/tile/8/0/000/",
    "sumdb/sum.golang.org/tile/8/0/x000/001",
    "sumdb/sum.golang.org/tile/8/0/x1/001",
    "sumdb/sum.golang.org/tile/8/0/x0001/001",
    "sumdb/sum.golang.org/tile/8/0/001/002",
    "sumdb/sum.golang.org/tile/8/0/x001/x002",
    "sumdb/sum.golang.org/tile/8/0/x001.p/5/000",
    "sumdb/sum.golang.org/tile/8/0/x009/x223/x372/x036/x854/x775/808",
    "sumdb/sum.golang.org/tile/8/0/x001/x000/x000/x000/x000/x000/x000/000",
    "sumdb/sum.golang.org/tile/8/00/000",
    "sumdb/sum.golang.org/tile/8/8/000",
    "sumdb/sum.golang.org/tile/8/10/000",
    "sumdb/sum.golang.org/tile/8/-1/000",
    "sumdb/sum.golang.org/tile/8/DATA/000",
    "sumdb/sum.golang.org/tile/8/datum/000",
    "sumdb/sum.golang.org/tile/9/0/000",
    "sumdb/sum.golang.org/tile/1/0/000",
    "sumdb/sum.golang.org/tile/08/0/000",
    "sumdb/sum.golang.org/tile/8/0/000.p",
    "sumdb/sum.golang.org/tile/8/0/000.p/",
    "sumdb/sum.golang.org/tile/8/0/000.p/0",
    "sumdb/sum.golang.org/tile/8/0/000.p/256",
    "sumdb/sum.golang.org/tile/8/0/000.p/012",
    "sumdb/sum.golang.org/tile/8/0/000.p/1/",
    "sumdb/sum.golang.org/tile/8/0/000.p/1.p/2",
    "sumdb/sum.golang.org/tile/8/0/000?x",
    "sumdb/sum.golang.org/tile/8/0/../000",
    "sumdb/sum.golang.org/tile/8/0/000.P/1",
    "sumdb/sum.golang.google.cn/supported",
    "sumdb/sum.golang.google.cn/latest",
    "sumdb/sum.example.com/supported",
    "sumdb/sum.golang.org.example/supported",
    "sumdb/sum%2Egolang.org/supported",
    "sumdb/SUM.GOLANG.ORG/supported",
    "sumdb/sum.golang.orgx/latest",
    "sumdb/sum/golang.org/latest",
    "sumdb/sum.golang.org%2Flatest",
    *(f"{path}/@v/list" for path in INVALID_MODULE_PATHS if path),
    *(f"sumdb/sum.golang.org/lookup/{path}@v1.0.0" for path in INVALID_MODULE_PATHS if path),
]


@pytest.mark.parametrize("path", REFUSED)
def test_refused(path: str) -> None:
    assert ADAPTER.route(path, {}) is None


def test_request_headers_do_not_change_the_answer() -> None:
    headers = {"if-none-match": '"abc"', "accept": "application/json"}
    for path in (
        "golang.org/x/net/@v/list",
        "golang.org/x/net/@v/v0.1.0.zip",
        "sumdb/sum.golang.org/supported",
        "sumdb/sum.golang.org/latest",
    ):
        assert ADAPTER.route(path, headers) == ADAPTER.route(path, {})


@pytest.mark.parametrize(
    "settings",
    [
        {"proxy": "http://proxy.golang.org"},
        {"proxy": "https://proxy.golang.org/"},
        {"proxy": "proxy.golang.org"},
        {"proxy": "https://proxy.golang.org?x=1"},
        {"proxy": "https://user@proxy.golang.org"},
        {"proxy": "https://proxy.golang.org,direct"},
        {"proxy": ""},
        {"sumdb": "http://sum.golang.org"},
        {"sumdb": "https://sum.golang.org/"},
        {"sumdb": "sum.golang.org"},
        {"sumdb": "https://sum.golang.org#x"},
    ],
)
def test_bad_settings_are_refused(settings: dict[str, str]) -> None:
    arguments = {"proxy": PROXY, "sumdb": SUMDB} | settings
    with pytest.raises(ValueError, match="must be an absolute https URL"):
        GoAdapter(**arguments)


def test_upstreams_with_a_path() -> None:
    adapter = GoAdapter(
        proxy="https://goproxy.example/go", sumdb="https://goproxy.example/sumdb/sum.golang.org"
    )
    answer = adapter.route("golang.org/x/net/@v/list", {})
    assert isinstance(answer, Fetch)
    assert answer.url == "https://goproxy.example/go/golang.org/x/net/@v/list"
    answer = adapter.route("sumdb/sum.golang.org/latest", {})
    assert isinstance(answer, Fetch)
    assert answer.url == "https://goproxy.example/sumdb/sum.golang.org/latest"


# --- agreement with the policy classifier -----------------------------------------


def served_paths() -> Iterator[str]:
    """Paths the adapter serves, in the canonical form OpenShell hands the middleware."""
    for entry in manifest():
        yield entry["path"]
    for module, version in listed_versions():
        for suffix in (".info", ".mod", ".zip"):
            yield f"{escape(module)}/@v/{escape(version)}{suffix}"
    for escaped, _ in MODULES:
        yield f"{escaped}/@v/list"
        for escaped_version, _ in VERSIONS:
            yield f"{escaped}/@v/{escaped_version}.zip"
    yield "sumdb/sum.golang.org/supported"
    yield tile("0", 102314)
    yield tile("data", 265895, 124)


def test_policy_classifies_what_the_adapter_serves() -> None:
    # The policy middleware decides each request with classify_package_request before
    # it reaches the cache: everything the adapter serves must classify the same way.
    for path in served_paths():
        answer = ADAPTER.route(path, {})
        assert answer is not None, path
        operation = classify_package_request("GET", f"/go/{path}")
        assert operation.ecosystem == "go"
        is_artifact = isinstance(answer, Fetch) and answer.kind is Kind.ARTIFACT
        assert operation.kind == ("artifact" if is_artifact else "index")
        if not path.startswith("sumdb/"):
            assert operation.name == decode_module_path(path.partition("/@")[0]), path
