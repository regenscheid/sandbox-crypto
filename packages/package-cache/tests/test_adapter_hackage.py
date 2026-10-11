# SPDX-License-Identifier: MIT
"""Tests for the Hackage adapter (docs/PACKAGE-CACHE.md, "URL layout").

``fixtures/hackage`` holds files captured from hackage.haskell.org
(``manifest.json`` says where each came from): hackage-security's signed
metadata (``root.json``, ``timestamp.json``, ``snapshot.json``,
``mirrors.json``) and the ``package.json`` targets of package versions whose
names have digits (``4Blocks``, ``3d-graphics-examples``, ``x86-64bit``),
mixed case (``mit-3qvpPyAi6mH``) or many version components (``cctools-workqueue``),
and of Cryptol. The tests derive request paths from them the way cabal does
(hackage-security's ``hackageRepoLayout``). The negative tests break one rule at
a time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from sandbox_science.package_cache.adapter import GZIP_MAGIC, Adapter, Fetch, Kind
from sandbox_science.package_cache.adapters.hackage import (
    SDIST_REVALIDATION,
    HackageAdapter,
    is_package_name,
    is_version,
    parse_package_id,
    parse_sdist_path,
)
from sandbox_science.policy import (
    Action,
    ClassificationError,
    HttpRequest,
    PoolCeiling,
    classify_package_request,
    evaluate_request,
    parse_policy,
)

FIXTURES = Path(__file__).parent / "fixtures" / "hackage"
REPOSITORY = "https://hackage.haskell.org"
ADAPTER = HackageAdapter(repository=REPOSITORY)
REPO = "<repo>/"


def fetch(path: str) -> Fetch:
    answer = ADAPTER.route(path, {})
    assert isinstance(answer, Fetch), path
    return answer


def manifest() -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return data["fixtures"]


def signed(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["signed"]


def targets() -> list[str]:
    """Every ``<repo>/package/<id>.tar.gz`` target in the captured package.json files."""
    found: list[str] = []
    for entry in manifest():
        if entry["file"].endswith(".package.json"):
            found += list(signed(entry["file"])["targets"])
    return found


def test_satisfies_the_adapter_contract() -> None:
    adapter: Adapter = ADAPTER
    assert adapter.prefix == "hackage"


# --- signed metadata and the index -------------------------------------------------


@pytest.mark.parametrize("name", ["root.json", "timestamp.json", "snapshot.json", "mirrors.json"])
def test_signed_metadata_is_revalidated_on_every_request(name: str) -> None:
    answer = fetch(name)
    assert answer.url == f"{REPOSITORY}/{name}"
    assert answer.key == f"hackage/{name}"
    assert answer.kind is Kind.METADATA
    assert not answer.immutable
    assert answer.max_age == 0
    assert answer.digest is None
    assert answer.rewrite is None
    assert answer.headers == {}
    # JSON may come compressed: decoding gives back the signed bytes.
    assert not answer.exact


def test_the_index_is_revalidated_on_every_request_and_stored_as_published() -> None:
    answer = fetch("01-index.tar.gz")
    assert answer.url == f"{REPOSITORY}/01-index.tar.gz"
    assert answer.key == "hackage/01-index.tar.gz"
    assert answer.kind is Kind.METADATA
    assert answer.max_age == 0
    assert answer.rewrite is None
    # Byte for byte, as the snapshot names it: a content coding is refused, not decoded.
    assert answer.exact
    assert answer.magic == GZIP_MAGIC
    assert answer.headers == {}


def test_the_signed_metadata_names_only_files_the_cache_serves() -> None:
    # The timestamp names the snapshot; the snapshot names the root, the mirror list
    # and the index in both forms. hackage-security fetches and updates only the
    # compressed index, so the uncompressed one (about 1 GiB) is refused.
    named = [*signed("timestamp.json")["meta"], *signed("snapshot.json")["meta"]]
    assert sorted(named) == [
        "<repo>/01-index.tar",
        "<repo>/01-index.tar.gz",
        "<repo>/mirrors.json",
        "<repo>/root.json",
        "<repo>/snapshot.json",
    ]
    for target in named:
        path = target.removeprefix(REPO)
        if path == "01-index.tar":
            assert ADAPTER.route(path, {}) is None
        else:
            assert fetch(path).kind is Kind.METADATA


# --- source distributions ----------------------------------------------------------


def test_every_captured_target_downloads_by_its_package_id() -> None:
    found = targets()
    assert len(found) == 9
    for target in found:
        path = target.removeprefix(REPO)
        package_id = path.removeprefix("package/").removesuffix(".tar.gz")
        upstream = f"{REPOSITORY}/package/{package_id}/{package_id}.tar.gz"
        # cabal's form, and the one Hackage redirects it to: one cache entry for both.
        for request in (path, f"package/{package_id}/{package_id}.tar.gz"):
            answer = fetch(request)
            assert answer.url == upstream
            assert answer.key == f"hackage/{path}"
            assert answer.kind is Kind.ARTIFACT
            assert answer.immutable
            # cabal checks the file against the signed index; the cache checks only
            # that it is a gzip file, and asks Hackage again now and then.
            assert answer.digest is None
            assert answer.magic == GZIP_MAGIC
            assert answer.revalidate_after == SDIST_REVALIDATION == 86400
            assert answer.rewrite is None


@pytest.mark.parametrize(
    ("package_id", "name", "version"),
    [
        ("split-0.2.5", "split", "0.2.5"),
        ("base64-bytestring-1.2.1.0", "base64-bytestring", "1.2.1.0"),
        ("4Blocks-0.2", "4Blocks", "0.2"),
        ("2captcha-0.1.0.0", "2captcha", "0.1.0.0"),
        ("3d-graphics-examples-0.0.0.2", "3d-graphics-examples", "0.0.0.2"),
        ("x86-64bit-0.4.6.2", "x86-64bit", "0.4.6.2"),
        ("cctools-workqueue-3.6.1.0.1.0.0.1", "cctools-workqueue", "3.6.1.0.1.0.0.1"),
        ("mit-3qvpPyAi6mH-10", "mit-3qvpPyAi6mH", "10"),
        ("cryptol-3.6.0", "cryptol", "3.6.0"),
        ("a-0", "a", "0"),
        ("tamarin-prover-1.10.0", "tamarin-prover", "1.10.0"),
    ],
)
def test_package_ids_split_at_the_last_hyphen(package_id: str, name: str, version: str) -> None:
    assert parse_package_id(package_id) == (name, version)
    assert parse_sdist_path(f"package/{package_id}.tar.gz") == package_id
    assert parse_sdist_path(f"package/{package_id}/{package_id}.tar.gz") == package_id


def test_request_headers_do_not_change_the_answer() -> None:
    headers = {"range": "bytes=0-10", "cache-control": "max-age=0", "if-none-match": '"x"'}
    for path in ("timestamp.json", "01-index.tar.gz", "package/split-0.2.5.tar.gz"):
        assert ADAPTER.route(path, headers) == ADAPTER.route(path, {})


# --- refused paths -----------------------------------------------------------------

BAD_NAMES = [
    "",
    "-split",
    "split-",
    "split--x",
    "1",
    "x-1",  # a word of digits only would read as a version
    "split_x",
    "split.x",
    "split+x",
    "spl\u00eft",
    "\uff53plit",  # fullwidth s
    "a" * 215,
]

BAD_VERSIONS = [
    "",
    "1.",
    ".1",
    "1..2",
    "01",
    "1.02",
    "1.0-2",
    "1.0a",
    "v1.0",
    "1.0 ",
    "\u0661.0",  # Arabic-Indic digit one
    "1" * 19,
    ".".join("1" * 33),
]

REFUSED = [
    "",
    "/",
    "timestamp.json/",
    "/timestamp.json",
    "TIMESTAMP.JSON",
    "timestamp.json.sig",
    "timestamp",
    "1.root.json",
    "targets.json",
    # Index forms no client fetches, and the unsigned legacy index.
    "01-index.tar",
    "01-index.tar.gz/",
    "01-index.tgz",
    "00-index.tar.gz",
    "00-index.tar",
    "01-index.tar.gz.sig",
    # Hackage's web pages, API and other package files.
    "packages/",
    "packages/archive/split/0.2.5/split-0.2.5.tar.gz",
    "package/split",
    "package/split-0.2.5",
    "package/split-0.2.5/",
    "package/split-0.2.5/split.cabal",
    "package/split-0.2.5/revision/1.cabal",
    "package/split-0.2.5/split-0.2.5.cabal",
    "package/split-0.2.5/docs/index.html",
    "package/split-0.2.5/split-0.2.5-docs.tar.gz",
    "package/split/split-0.2.5.tar.gz",
    "api/v1/packages",
    "upload",
    # Source distributions: a file of another package or version under this id.
    "package/split-0.2.5/split-0.2.6.tar.gz",
    "package/split-0.2.5/evil-0.2.5.tar.gz",
    "package/split-0.2.5/split-0.2.5/split-0.2.5.tar.gz",
    "package/split-0.2.5.tar.gz/split-0.2.5.tar.gz",
    "package//split-0.2.5.tar.gz",
    "package/split-0.2.5//split-0.2.5.tar.gz",
    # Other archive formats and case.
    "package/split-0.2.5.tgz",
    "package/split-0.2.5.tar.bz2",
    "package/split-0.2.5.TAR.GZ",
    "PACKAGE/split-0.2.5.tar.gz",
    # Dot segments, encodings and query-like characters.
    "package/../timestamp.json",
    "package/./split-0.2.5.tar.gz",
    "package/split-0.2.5.tar.gz?x",
    "package/split-0.2.5.tar.gz#x",
    "package/split-0.2.5.tar.gz;x",
    "package/split%2D0.2.5.tar.gz",
    "package%2Fsplit-0.2.5.tar.gz",
    "package/split-0.2.5.tar.gz\n",
    "package\\split-0.2.5.tar.gz",
    *(f"package/{name}-1.0.tar.gz" for name in BAD_NAMES),
    *(f"package/split-{version}.tar.gz" for version in BAD_VERSIONS),
]


@pytest.mark.parametrize("path", REFUSED)
def test_refused(path: str) -> None:
    assert ADAPTER.route(path, {}) is None


@pytest.mark.parametrize("name", BAD_NAMES)
def test_bad_names(name: str) -> None:
    assert not is_package_name(name)


@pytest.mark.parametrize("version", BAD_VERSIONS)
def test_bad_versions(version: str) -> None:
    assert not is_version(version)


@pytest.mark.parametrize(
    "repository",
    [
        "http://hackage.haskell.org",
        "https://hackage.haskell.org/",
        "hackage.haskell.org",
        "https://hackage.haskell.org?x=1",
        "https://hackage.haskell.org#x",
        "https://user@hackage.haskell.org",
        "",
    ],
)
def test_bad_settings_are_refused(repository: str) -> None:
    with pytest.raises(ValueError, match="must be an absolute https URL"):
        HackageAdapter(repository=repository)


# --- agreement with the policy -----------------------------------------------------


def test_policy_classifies_what_the_adapter_serves() -> None:
    # The policy middleware decides each request with classify_package_request before
    # it reaches the cache: everything the adapter serves must classify the same way.
    for name in ("root.json", "timestamp.json", "snapshot.json", "mirrors.json", "01-index.tar.gz"):
        index = classify_package_request("GET", f"/hackage/{name}")
        assert (index.ecosystem, index.name, index.kind) == ("hackage", None, "index")
    for target in targets():
        path = target.removeprefix(REPO)
        package_id = path.removeprefix("package/").removesuffix(".tar.gz")
        parsed = parse_package_id(package_id)
        assert parsed is not None
        for request in (path, f"package/{package_id}/{package_id}.tar.gz"):
            assert ADAPTER.route(request, {}) is not None
            sdist = classify_package_request("GET", f"/hackage/{request}")
            assert (sdist.ecosystem, sdist.name, sdist.kind) == ("hackage", parsed[0], "artifact")


@pytest.mark.parametrize("path", [path for path in REFUSED if path and "\n" not in path])
def test_policy_refuses_what_the_adapter_refuses(path: str) -> None:
    with pytest.raises(ClassificationError):
        classify_package_request("GET", f"/hackage/{path}")


def test_every_signed_mirror_is_on_the_floor() -> None:
    # cabal falls back to the mirrors in mirrors.json when the cache fails; reaching
    # them directly would fetch packages around the cache and the packages section.
    # The floor's list is fixed while the signed list can change: refreshing this
    # fixture checks every mirror it names, over both schemes, whatever the rules allow.
    policy = parse_policy(
        "apiVersion: sandbox-science/v0alpha1\nkind: ProjectPolicy\n"
        "metadata: {name: reads}\ndefaults: {action: allow}\n"
        "network:\n  rules:\n"
        '    - {name: reads, operation: http, match: {hosts: ["*"]}, action: allow}\n'
        '    - {name: fetch, operation: git, match: {hosts: ["*"]}, action: allow}\n'
    )
    mirrors = [urlsplit(mirror["urlbase"]) for mirror in signed("mirrors.json")["mirrors"]]
    assert [mirror.hostname for mirror in mirrors] == [
        "hackage.fpcomplete.com",
        "objects-us-east-1.dream.io",
    ]
    for mirror in mirrors:
        assert mirror.hostname is not None
        bases = [f"{scheme}://{mirror.hostname}{mirror.path}" for scheme in ("http", "https")]
        bucket = mirror.path.strip("/")
        if bucket:
            # A bucket on a shared object store answers under its own host name too.
            bases += [f"{scheme}://{bucket}.{mirror.hostname}/" for scheme in ("http", "https")]
        for base in bases:
            for name in ("", "timestamp.json", "01-index.tar.gz", "package/split-0.2.5.tar.gz"):
                request = HttpRequest(method="GET", url=f"{base}{name}")
                decision = evaluate_request(request, policy, ceiling=PoolCeiling.unrestricted())
                assert (decision.action, decision.layer) == (Action.DENY, "floor"), base + name
            clone = HttpRequest(method="GET", url=f"{base}repo/info/refs?service=git-upload-pack")
            decision = evaluate_request(clone, policy, ceiling=PoolCeiling.unrestricted())
            assert (decision.action, decision.layer) == (Action.DENY, "floor"), base
