# SPDX-License-Identifier: MIT
"""Tests for the opam adapter (docs/PACKAGE-CACHE.md, "URL layout").

``fixtures/opam`` holds files captured from opam.ocaml.org (``manifest.json`` says
where each came from): the repository's ``repo`` file, whose ``archive-mirrors``
sends opam to the archive cache, and the opam files of packages whose first
checksum is MD5 (``ocamlfind``), SHA-256 (the compiler, Jasmin) or SHA-512
(``zed``), and of one with extra sources (``ocaml-config``). The tests derive
archive cache paths from them the way opam does (``OpamHash.to_path``) and check
that each routes to the right upstream file and digest. The negative tests break
one rule at a time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import pytest

from sandbox_science.package_cache.adapter import Adapter, Digest, Fetch, Kind
from sandbox_science.package_cache.adapters.opam import (
    DIGEST_LENGTHS,
    OpamAdapter,
    parse_archive_path,
)
from sandbox_science.policy import ClassificationError, classify_package_request

FIXTURES = Path(__file__).parent / "fixtures" / "opam"
REPOSITORY = "https://opam.ocaml.org"
ADAPTER = OpamAdapter(repository=REPOSITORY)
MD5 = "0123456789abcdef" * 2
SHA256 = "0123456789abcdef" * 4
SHA512 = "0123456789abcdef" * 8

Algorithm = Literal["md5", "sha256", "sha512"]


def fetch(path: str) -> Fetch:
    answer = ADAPTER.route(path, {})
    assert isinstance(answer, Fetch), path
    return answer


def manifest() -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return data["fixtures"]


def opam_files() -> list[dict[str, Any]]:
    return [entry for entry in manifest() if entry["file"].endswith(".opam")]


def sources(text: str) -> list[list[tuple[Algorithm, str]]]:
    """The checksum lists of an opam file's url and extra-source sections, in order.

    Written for the captured files, independently of the adapter: a section's
    ``checksum`` is one string or a list of them, each ``<algorithm>=<hex>``.
    """
    lists: list[list[tuple[Algorithm, str]]] = []
    for section in re.finditer(r'^(?:url|extra-source "[^"]+") \{\n(.*?)^\}', text, re.M | re.S):
        checksum = re.search(r"checksum:\s*(\[.*?\]|\"[^\"]*\")", section.group(1), re.S)
        assert checksum is not None, section.group(0)
        pairs = re.findall(r'"(md5|sha256|sha512)=([0-9a-f]+)"', checksum.group(1))
        lists.append([(algorithm, value) for algorithm, value in pairs])
    return lists


def cache_path(algorithm: str, hex_digest: str) -> str:
    """opam's ``OpamHash.to_path`` below the ``cache`` archive mirror."""
    return f"cache/{algorithm}/{hex_digest[:2]}/{hex_digest}"


def test_satisfies_the_adapter_contract() -> None:
    adapter: Adapter = ADAPTER
    assert adapter.prefix == "opam"


# --- the repository index ----------------------------------------------------------


def test_the_index_is_index_data_stored_as_published() -> None:
    answer = fetch("index.tar.gz")
    assert answer.url == f"{REPOSITORY}/index.tar.gz"
    assert answer.key == "opam/index.tar.gz"
    assert answer.kind is Kind.METADATA
    assert not answer.immutable
    assert answer.digest is None
    assert answer.rewrite is None
    assert answer.max_age is None
    assert answer.headers == {"Accept-Encoding": "identity"}


def test_the_repo_file_sends_opam_to_the_archive_cache() -> None:
    # opam resolves a relative archive mirror against the repository URL, so with the
    # cache as repository it asks <cache>/opam/cache/<algorithm>/<xx>/<digest>.
    repo = (FIXTURES / "repo").read_text(encoding="utf-8")
    assert re.search(r'^archive-mirrors: "cache"$', repo, re.M)
    assert 'opam-version: "2.0"' in repo
    # The redirects it lists apply only to opam 1, which the cache does not serve.
    for constraint in re.findall(r"\{(opam-version [^}]*)\}", repo.split("archive-mirrors")[0]):
        assert constraint.startswith(('opam-version < "1.', 'opam-version < "2.0~"'))


# --- archives ----------------------------------------------------------------------


def test_every_captured_source_routes_to_a_verified_archive() -> None:
    count = 0
    for entry in opam_files():
        text = (FIXTURES / entry["file"]).read_text(encoding="utf-8")
        for checksums in sources(text):
            assert checksums, entry["file"]
            # opam asks for the first checksum listed; any of them may come first
            # in another file, so every one must route.
            for algorithm, hex_digest in checksums:
                path = cache_path(algorithm, hex_digest)
                answer = fetch(path)
                assert answer.url == f"{REPOSITORY}/{path}"
                assert answer.key == f"opam/cache/{algorithm}/{hex_digest}"
                assert answer.kind is Kind.ARTIFACT
                assert answer.immutable
                assert answer.digest == Digest(algorithm, hex_digest)
                assert answer.rewrite is None
                count += 1
    assert count == 11


@pytest.mark.parametrize(
    ("file", "algorithm"),
    [
        ("ocamlfind.1.9.8.opam", "md5"),
        ("ocaml-base-compiler.5.2.1.opam", "sha256"),
        ("zed.3.2.3.opam", "sha512"),
        ("jasmin.2026.09.0.opam", "sha256"),
    ],
)
def test_opam_names_archives_by_their_first_checksum(file: str, algorithm: str) -> None:
    # About a quarter of opam-repository lists MD5 first, so opam asks the cache for
    # MD5 paths; it then checks every listed checksum itself, a stronger one included.
    (first, *_), *_ = sources((FIXTURES / file).read_text(encoding="utf-8"))
    assert first[0] == algorithm
    assert fetch(cache_path(*first)).digest == Digest(*first)


def test_extra_sources_come_through_the_archive_cache_too() -> None:
    # ocaml-config has no source archive, only two extra files (opam fetches them with
    # the same archive mirrors); the compiler has both.
    for file, count in (("ocaml-config.3.opam", 2), ("ocaml-base-compiler.5.2.1.opam", 1)):
        text = (FIXTURES / file).read_text(encoding="utf-8")
        assert text.count('\nextra-source "') == count
        for (algorithm, hex_digest), *_ in sources(text):
            assert fetch(cache_path(algorithm, hex_digest)).kind is Kind.ARTIFACT


@pytest.mark.parametrize(
    ("algorithm", "hex_digest"), [("md5", MD5), ("sha256", SHA256), ("sha512", SHA512)]
)
def test_archive_paths(algorithm: Algorithm, hex_digest: str) -> None:
    path = cache_path(algorithm, hex_digest)
    assert parse_archive_path(path) == Digest(algorithm, hex_digest)
    assert fetch(path).digest == Digest(algorithm, hex_digest)
    assert len(hex_digest) == DIGEST_LENGTHS[algorithm]


def test_one_file_named_by_two_digests_gets_two_entries() -> None:
    # The keys differ, so a request naming a wrong digest never shares a fetch with
    # one naming the right one; the store keeps the body once, by its SHA-256.
    md5 = fetch(cache_path("md5", MD5))
    sha256 = fetch(cache_path("sha256", SHA256))
    assert md5.key != sha256.key


def test_request_headers_do_not_change_the_answer() -> None:
    headers = {"accept-encoding": "gzip", "range": "bytes=0-10", "cache-control": "max-age=0"}
    for path in ("index.tar.gz", cache_path("sha256", SHA256)):
        assert ADAPTER.route(path, headers) == ADAPTER.route(path, {})


# --- refused paths -----------------------------------------------------------------

REFUSED = [
    "",
    "/",
    "index.tar.gz/",
    "/index.tar.gz",
    "index.tar",
    "index.tgz",
    "INDEX.TAR.GZ",
    "index.tar.gz?x=1",
    "index.tar.gz.sig",
    # Repository files opam reads from index.tar.gz, and opam 1's layout.
    "repo",
    "version",
    "urls.txt",
    "packages/ocamlfind/ocamlfind.1.9.8/opam",
    "packages/ocamlfind/ocamlfind.1.9.8/files/fix.patch",
    "archives/ocamlfind.1.9.8+opam.tar.gz",
    "1.2.2/index.tar.gz",
    # The archive cache: wrong shape.
    "cache",
    "cache/",
    "cache/sha256",
    "cache/sha256/",
    f"cache/sha256/{SHA256}",
    f"cache/sha256/0/{SHA256}",
    f"cache/sha256/012/{SHA256}",
    f"cache/sha256/ab/{SHA256}",  # the directory is not the digest's first two digits
    f"cache/sha256/10/{SHA256}",
    f"cache/sha256/01/{SHA256}/",
    f"cache/sha256/01/{SHA256}/extra",
    f"cache/sha256/01/01/{SHA256}",
    f"/cache/sha256/01/{SHA256}",
    f"CACHE/sha256/01/{SHA256}",
    f"cache/SHA256/01/{SHA256}",
    # Digests: wrong length for the algorithm, case, characters or algorithm.
    f"cache/sha256/01/{SHA256[:-1]}",
    f"cache/sha256/01/{SHA256}0",
    f"cache/sha256/01/{SHA512}",
    f"cache/md5/01/{SHA256}",
    f"cache/sha512/01/{SHA256}",
    f"cache/md5/01/{MD5[:-1]}",
    f"cache/sha256/01/{SHA256.upper()}",
    f"cache/sha256/01/{SHA256[:-1]}g",
    f"cache/sha1/01/{'0123456789' * 4}",
    f"cache/sha384/01/{'0123456789abcdef' * 6}",
    f"cache/blake2b/01/{SHA512}",
    # Dot segments, encodings and query-like characters.
    f"cache/sha256/../sha256/01/{SHA256}",
    f"cache/sha256/./01/{SHA256}",
    f"cache/sha256%2F01%2F{SHA256}",
    f"cache/sha256/01/{SHA256[:-1]}%30",
    f"cache/sha256/01/{SHA256}?x",
    f"cache/sha256/01/{SHA256}#x",
    f"cache/sha256/01/{SHA256};x",
    f"cache/sha256/01/{SHA256}\n",
    f"cache\\sha256\\01\\{SHA256}",
]


@pytest.mark.parametrize("path", REFUSED)
def test_refused(path: str) -> None:
    assert ADAPTER.route(path, {}) is None


@pytest.mark.parametrize(
    "repository",
    [
        "http://opam.ocaml.org",
        "https://opam.ocaml.org/",
        "opam.ocaml.org",
        "https://opam.ocaml.org?x=1",
        "https://opam.ocaml.org#x",
        "https://user@opam.ocaml.org",
        "https://opam.ocaml.org/a b",
        "",
    ],
)
def test_bad_settings_are_refused(repository: str) -> None:
    with pytest.raises(ValueError, match="must be an absolute https URL"):
        OpamAdapter(repository=repository)


def test_a_repository_below_a_path_keeps_it() -> None:
    adapter = OpamAdapter(repository="https://mirror.example/opam")
    answer = adapter.route(cache_path("md5", MD5), {})
    assert isinstance(answer, Fetch)
    assert answer.url == f"https://mirror.example/opam/{cache_path('md5', MD5)}"


# --- agreement with the policy classifier ------------------------------------------


def test_policy_classifies_what_the_adapter_serves() -> None:
    # The policy middleware decides each request with classify_package_request before
    # it reaches the cache: everything the adapter serves must classify the same way.
    index = classify_package_request("GET", "/opam/index.tar.gz")
    assert (index.ecosystem, index.name, index.kind) == ("opam", None, "index")
    for entry in opam_files():
        text = (FIXTURES / entry["file"]).read_text(encoding="utf-8")
        for checksums in sources(text):
            for algorithm, hex_digest in checksums:
                path = cache_path(algorithm, hex_digest)
                assert ADAPTER.route(path, {}) is not None
                archive = classify_package_request("GET", f"/opam/{path}")
                # Content-addressed: the path names no package.
                assert (archive.ecosystem, archive.name, archive.kind) == ("opam", None, "artifact")


@pytest.mark.parametrize("path", [path for path in REFUSED if path and "\n" not in path])
def test_policy_refuses_what_the_adapter_refuses(path: str) -> None:
    with pytest.raises(ClassificationError):
        classify_package_request("GET", f"/opam/{path}")
