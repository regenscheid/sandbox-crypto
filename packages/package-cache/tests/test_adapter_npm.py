# SPDX-License-Identifier: MIT
"""Tests for the npm adapter (docs/PACKAGE-CACHE.md, "URL layout").

The fixtures in ``fixtures/npm`` are real package metadata from registry.npmjs.org, saved
unmodified; ``fixtures/npm/manifest.json`` records each request and why it was chosen.
Rewriting one must point every version's tarball at the cache with the SHA-512 from its
``integrity``, change nothing else, and routing each new link must lead back to the
registry's tarball with that digest.

The negative tests cover names npm refuses, paths outside the layout (the registry's API
among them), Accept headers that allow neither variant, and metadata the rewrite must not
pass on: tarballs on another host or path, unusable integrity, malformed JSON.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.package_cache.adapter import Adapter, Digest, Fetch, Kind
from sandbox_science.package_cache.adapters.npm import (
    ABBREVIATED_METADATA,
    FULL_METADATA,
    MAX_NAME_LENGTH,
    MAX_VERSION_LENGTH,
    TARBALL_CONTENT_TYPE,
    NpmAdapter,
    NpmMetadataError,
)

REGISTRY = "https://registry.npmjs.org"
PUBLIC = "http://host.openshell.internal:8090"
FILES = f"{PUBLIC}/npm/files"
ADAPTER = NpmAdapter(registry=REGISTRY, public_url=PUBLIC)

# What npm (pacote), pnpm, yarn 1 and bun send for install metadata.
INSTALL_ACCEPT = "application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8, */*"
HEX = "ab" * 64

DIGEST = bytes(range(64))
INTEGRITY = "sha512-" + base64.b64encode(DIGEST).decode()
DEMO_TARBALL = f"{REGISTRY}/demo/-/demo-1.0.0.tgz"

FIXTURES = Path(__file__).parent / "fixtures" / "npm"

# sha512sum of @isaacs/string-locale-compare 1.1.0's tarball, downloaded on 2026-10-10.
STRING_LOCALE_COMPARE_1_1_0 = (
    "490ecace187df83f995bd300d33918bf75578480cdc7e2f333a109fbfeb92374"
    "18f9e9d4e88b6d7bb1395d7ec45aba8b5b614de27d3a196cc19cfa02ded87669"
)


def short_id(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else f"{text[:25]}...{text[-30:]}"


def fetch(path: str, accept: str | None = None, adapter: NpmAdapter = ADAPTER) -> Fetch:
    route = adapter.route(path, {} if accept is None else {"accept": accept})
    assert isinstance(route, Fetch)
    return route


def metadata_path(name: str) -> str:
    """The path npm requests for ``name``'s metadata (npm-package-arg's escapedName)."""
    return name.replace("/", "%2f")


def rewrite(
    document: object,
    *,
    name: str = "demo",
    accept: str = INSTALL_ACCEPT,
    content_type: str = ABBREVIATED_METADATA,
    adapter: NpmAdapter = ADAPTER,
) -> Any:
    route = fetch(metadata_path(name), accept, adapter)
    assert route.rewrite is not None
    body = document if isinstance(document, bytes) else json.dumps(document).encode()
    return json.loads(route.rewrite(body, content_type))


def demo(**dist: object) -> dict[str, object]:
    """Metadata for a package ``demo`` with one version, 1.0.0, and the given ``dist``."""
    manifest = {"name": "demo", "version": "1.0.0", "dist": dist}
    return {"name": "demo", "dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": manifest}}


def link(document: object, **options: Any) -> str:
    return rewrite(document, **options)["versions"]["1.0.0"]["dist"]["tarball"]


# --- the adapter -------------------------------------------------------------------------


def test_adapter_satisfies_the_contract() -> None:
    adapter: Adapter = NpmAdapter(registry=REGISTRY, public_url=PUBLIC)
    assert adapter.prefix == "npm"


def test_metadata_errors_are_value_errors() -> None:
    # The core answers 502 and stores nothing when a rewrite raises ValueError.
    assert issubclass(NpmMetadataError, ValueError)


@pytest.mark.parametrize(
    ("registry", "public_url", "metadata_url", "link_start"),
    [
        (REGISTRY, PUBLIC, f"{REGISTRY}/demo", f"{PUBLIC}/npm/files/"),
        (f"{REGISTRY}/", f"{PUBLIC}/", f"{REGISTRY}/demo", f"{PUBLIC}/npm/files/"),
        (
            "HTTPS://Registry.NPMJS.org",
            "http://Cache.Example:8090",
            f"{REGISTRY}/demo",
            "http://cache.example:8090/npm/files/",
        ),
        (
            "https://npm.example.com/repository/npm/",
            "https://cache.example/sandbox",
            "https://npm.example.com/repository/npm/demo",
            "https://cache.example/sandbox/npm/files/",
        ),
        (REGISTRY, "http://[fd00::1]:8090", f"{REGISTRY}/demo", "http://[fd00::1]:8090/npm/files/"),
        (
            REGISTRY,
            "http://package_cache:8090",
            f"{REGISTRY}/demo",
            "http://package_cache:8090/npm/files/",
        ),
    ],
)
def test_settings_are_normalized_like_the_settings_file(
    registry: str, public_url: str, metadata_url: str, link_start: str
) -> None:
    adapter = NpmAdapter(registry=registry, public_url=public_url)
    assert fetch("demo", adapter=adapter).url == metadata_url
    tarball = metadata_url.replace("/demo", "/demo/-/demo-1.0.0.tgz")
    assert link(demo(tarball=tarball), adapter=adapter).startswith(link_start)


@pytest.mark.parametrize(
    ("registry", "public_url"),
    [
        ("http://registry.npmjs.org", PUBLIC),  # upstreams use https
        ("ftp://registry.npmjs.org", PUBLIC),
        ("registry.npmjs.org", PUBLIC),
        ("https://", PUBLIC),
        ("https://:443", PUBLIC),
        ("https://user:password@registry.npmjs.org", PUBLIC),
        ("https://registry.npmjs.org?write=true", PUBLIC),
        ("https://registry.npmjs.org/?", PUBLIC),
        ("https://registry.npmjs.org#top", PUBLIC),
        ("https://registry.npmjs.org/a b", PUBLIC),
        ("https://registry.npmjs.org/a/../b", PUBLIC),
        ("https://registry.npmjs.org/%2e%2e", PUBLIC),
        ("https://registry.npmjs.org:0", PUBLIC),
        ("https://registry.npmjs.org:99999", PUBLIC),
        ("https://registry.npmjs.org:x", PUBLIC),
        ("https://régistry.example", PUBLIC),
        ("https://registry.npmjs.org\n", PUBLIC),
        (REGISTRY, ""),
        (REGISTRY, "ftp://host.openshell.internal:8090"),
        (REGISTRY, "host.openshell.internal:8090"),
        (REGISTRY, "http://host.openshell.internal:8090/?"),
        (REGISTRY, "http://host.openshell.internal:8090/#"),
        (REGISTRY, "http://test-secret@host.openshell.internal:8090"),
    ],
    ids=short_id,
)
def test_invalid_settings_are_refused(registry: str, public_url: str) -> None:
    with pytest.raises(ValueError, match="npm"):
        NpmAdapter(registry=registry, public_url=public_url)


# --- package metadata --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "upstream_name"),
    [
        ("lodash", "lodash"),
        ("files", "files"),  # a package called "files": tarballs live below files/
        ("JSONStream", "JSONStream"),  # legacy capital letters are kept, never folded
        ("Base64", "Base64"),
        ("http", "http"),  # core module names are old package names
        ("underscore.string", "underscore.string"),
        ("a", "a"),
        ("7", "7"),
        ("~legacy!(*)'", "~legacy!(*)'"),  # characters old packages may still have
        ("x" * MAX_NAME_LENGTH, "x" * MAX_NAME_LENGTH),
        ("@types%2Fnode", "@types%2fnode"),  # pnpm (encodeURIComponent)
        ("@types%2fnode", "@types%2fnode"),  # npm, yarn
        ("@types/node", "@types%2fnode"),
        ("@isaacs%2fstring-locale-compare", "@isaacs%2fstring-locale-compare"),
        ("@scope%2f_private", "@scope%2f_private"),  # only "." may not start a scoped name
        ("@scope%2f-dash", "@scope%2f-dash"),
        ("@.scope%2fpkg", "@.scope%2fpkg"),
        ("@Scope%2FName", "@Scope%2fName"),
        ("@s%2f" + "x" * (MAX_NAME_LENGTH - 3), "@s%2f" + "x" * (MAX_NAME_LENGTH - 3)),
    ],
    ids=short_id,
)
def test_metadata_routes(path: str, upstream_name: str) -> None:
    for accept, variant in ((INSTALL_ACCEPT, ABBREVIATED_METADATA), (None, FULL_METADATA)):
        route = fetch(path, accept)
        assert route.url == f"{REGISTRY}/{upstream_name}"
        assert route.kind is Kind.METADATA
        assert not route.immutable
        assert route.digest is None
        assert route.rewrite is not None
        assert route.headers == {"Accept": variant}
        assert route.content_type == variant


def test_metadata_key_format() -> None:
    assert fetch("@types/node", INSTALL_ACCEPT).key == (
        f"npm metadata abbreviated {REGISTRY}/@types%2fnode rewritten for {PUBLIC} r1"
    )
    assert fetch("lodash").key == f"npm metadata full {REGISTRY}/lodash rewritten for {PUBLIC} r1"


def test_scope_encodings_share_one_cache_entry() -> None:
    for accept in (INSTALL_ACCEPT, "application/json"):
        routes = [fetch(path, accept) for path in ("@types%2Fnode", "@types%2fnode", "@types/node")]
        assert len({(route.key, route.url) for route in routes}) == 1


def test_metadata_keys_name_the_variant_and_what_the_rewrite_depends_on() -> None:
    # The cache stores the rewritten document, and a 304 keeps it: a key shared with
    # another variant, public URL or registry would serve links built for that one.
    keys = {
        fetch("lodash", INSTALL_ACCEPT).key,
        fetch("lodash", "application/json").key,
        fetch(
            "lodash", INSTALL_ACCEPT, NpmAdapter(registry=REGISTRY, public_url="https://c.example")
        ).key,
        fetch(
            "lodash", INSTALL_ACCEPT, NpmAdapter(registry="https://m.example", public_url=PUBLIC)
        ).key,
        fetch("Lodash", INSTALL_ACCEPT).key,
    }
    assert len(keys) == 5


def test_only_the_negotiated_accept_header_goes_upstream() -> None:
    route = ADAPTER.route(
        "lodash",
        {
            "accept": INSTALL_ACCEPT,
            "authorization": "Bearer test-secret-token",
            "cookie": "session=test-secret",
            "npm-session": "0123456789abcdef",
            "npm-command": "install",
            "user-agent": "npm/11.0.0 node/v24.0.0",
            "if-none-match": '"etag"',
        },
    )
    assert isinstance(route, Fetch)
    assert route.headers == {"Accept": ABBREVIATED_METADATA}


@pytest.mark.parametrize(
    ("accept", "variant"),
    [
        (None, FULL_METADATA),  # yarn 2+ sends none, and the registry then sends everything
        ("", FULL_METADATA),
        (" \t", FULL_METADATA),
        (INSTALL_ACCEPT, ABBREVIATED_METADATA),
        ("application/json", FULL_METADATA),  # npm (full metadata), yarn 1 (unfiltered)
        ("application/json; q=1.0, */*", FULL_METADATA),  # pnpm (full metadata)
        ("application/vnd.npm.install-v1+json", ABBREVIATED_METADATA),
        ("Application/VND.NPM.Install-V1+JSON", ABBREVIATED_METADATA),
        ("*/*", FULL_METADATA),  # a wildcard never selects the abbreviated document
        ("application/*", FULL_METADATA),
        ("application/json;charset=utf-8", FULL_METADATA),
        ("application/json;Q=0.5", FULL_METADATA),
        ("text/html, application/json;q=0.1", FULL_METADATA),
        ("application/vnd.npm.install-v1+json;q=0.5, application/json", FULL_METADATA),
        ("application/vnd.npm.install-v1+json;q=0.5, */*", FULL_METADATA),
        ("application/vnd.npm.install-v1+json;q=0.8, application/json;q=0.8", ABBREVIATED_METADATA),
        ("application/json;q=0.8, application/vnd.npm.install-v1+json;q=0.9", ABBREVIATED_METADATA),
        ("application/vnd.npm.install-v1+json;q=0, application/json", FULL_METADATA),
        ("application/vnd.npm.install-v1+json;q=0, */*", FULL_METADATA),
        ("application/vnd.npm.install-v1+json;q=0.001", ABBREVIATED_METADATA),
        ("application/vnd.npm.install-v1+json;q=1.000", ABBREVIATED_METADATA),
        (" , application/vnd.npm.install-v1+json ,, ", ABBREVIATED_METADATA),
        ("application/json;", FULL_METADATA),
        ("application/vnd.npm.install-v1+json ; ; q=0.9 ;level=1", ABBREVIATED_METADATA),
    ],
)
def test_accept_selects_the_variant(accept: str | None, variant: str) -> None:
    route = fetch("lodash", accept)
    assert route.headers == {"Accept": variant}
    assert route.content_type == variant


@pytest.mark.parametrize(
    "accept",
    [
        "text/html",
        "application/vnd.npm.install-v1+json;q=0",
        "application/json;q=0",
        "application/json;q=0, */*",  # the named type outranks the wildcard
        "*/*;q=0",
        "application/json;q=0.",
        # malformed headers select nothing
        "application/json;q=2",
        "application/json;q=1.5",
        "application/json;q=0.1234",
        "application/json;q=abc",
        "application/json;q=",
        "application/json;q",
        "application/json;q=0.5;q=0.6",
        "application/json; q = 0.5",
        'application/json;foo="bar"',  # quoted parameter values are not supported
        "application",
        "*",
        "*/json",
        "application/json/extra",
        "application/js on",
        "application/jsön",
        "application/json, text/",
    ],
)
def test_accept_that_allows_neither_variant_is_refused(accept: str) -> None:
    assert ADAPTER.route("lodash", {"accept": accept}) is None


# --- tarballs ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "upstream"),
    [
        (f"files/{HEX}/lodash/-/lodash-4.17.21.tgz", f"{REGISTRY}/lodash/-/lodash-4.17.21.tgz"),
        (
            f"files/{HEX}/@types/node/-/node-20.11.0.tgz",
            f"{REGISTRY}/@types/node/-/node-20.11.0.tgz",
        ),
        (
            f"files/{HEX}/JSONStream/-/JSONStream-1.3.5.tgz",
            f"{REGISTRY}/JSONStream/-/JSONStream-1.3.5.tgz",
        ),
        (f"files/{HEX}/files/-/files-1.0.0.tgz", f"{REGISTRY}/files/-/files-1.0.0.tgz"),
        (
            f"files/{HEX}/demo/-/demo-1.0.0-alpha.1+build.5.tgz",
            f"{REGISTRY}/demo/-/demo-1.0.0-alpha.1+build.5.tgz",
        ),
        (
            f"files/{HEX}/~legacy!(*)'/-/~legacy!(*)'-1.0.0.tgz",
            f"{REGISTRY}/~legacy!(*)'/-/~legacy!(*)'-1.0.0.tgz",
        ),
        (
            f"files/{HEX}/@scope/_private/-/_private-1.0.0.tgz",
            f"{REGISTRY}/@scope/_private/-/_private-1.0.0.tgz",
        ),
        # Legacy names the registry still serves: early npm left out the hyphen before a
        # prerelease (1.0.0rc2 holds version 1.0.0-rc2), and a few tarballs use "@".
        (
            f"files/{HEX}/express/-/express-1.0.0rc2.tgz",
            f"{REGISTRY}/express/-/express-1.0.0rc2.tgz",
        ),
        (f"files/{HEX}/riak-js/-/riak-js-0.2.2f.tgz", f"{REGISTRY}/riak-js/-/riak-js-0.2.2f.tgz"),
        (f"files/{HEX}/demo/-/demo-1.0.0beta-2.tgz", f"{REGISTRY}/demo/-/demo-1.0.0beta-2.tgz"),
        (f"files/{HEX}/demo/-/demo-1.0.0rc.1.tgz", f"{REGISTRY}/demo/-/demo-1.0.0rc.1.tgz"),
        (
            f"files/{HEX}/thirty-two/-/thirty-two@0.0.1.tgz",
            f"{REGISTRY}/thirty-two/-/thirty-two@0.0.1.tgz",
        ),
        (
            f"files/{HEX}/nodemailer/-/nodemailer@0.1.1.tgz",
            f"{REGISTRY}/nodemailer/-/nodemailer@0.1.1.tgz",
        ),
        (
            f"files/{HEX}/{'x' * MAX_NAME_LENGTH}/-/{'x' * MAX_NAME_LENGTH}-1.0.0.tgz",
            f"{REGISTRY}/{'x' * MAX_NAME_LENGTH}/-/{'x' * MAX_NAME_LENGTH}-1.0.0.tgz",
        ),
        (
            f"files/{HEX}/demo/-/demo-1.0.0-{'a' * (MAX_VERSION_LENGTH - 6)}.tgz",
            f"{REGISTRY}/demo/-/demo-1.0.0-{'a' * (MAX_VERSION_LENGTH - 6)}.tgz",
        ),
    ],
    ids=short_id,
)
def test_tarball_routes(path: str, upstream: str) -> None:
    for accept in (None, INSTALL_ACCEPT):
        route = fetch(path, accept)
        assert route.url == upstream
        assert route.key == f"npm tarball {HEX} {upstream}"
        assert route.kind is Kind.ARTIFACT
        assert route.immutable
        assert route.digest == Digest("sha512", HEX)
        assert route.rewrite is None
        assert route.headers == {}
        assert route.content_type == TARBALL_CONTENT_TYPE


def test_tarball_without_a_digest() -> None:
    route = fetch("files/-/http-shutdown/-/http-shutdown-1.0.1.tgz")
    assert route.url == f"{REGISTRY}/http-shutdown/-/http-shutdown-1.0.1.tgz"
    assert route.key == f"npm tarball - {route.url}"
    assert route.kind is Kind.ARTIFACT
    assert route.immutable
    assert route.digest is None


def test_tarball_keys_include_the_digest() -> None:
    # A body verified against one digest must never answer a path naming another, or none.
    keys = {
        fetch(f"files/{digest}/lodash/-/lodash-4.17.21.tgz").key for digest in (HEX, "cd" * 64, "-")
    }
    assert len(keys) == 3


# --- refused paths -----------------------------------------------------------------------

REFUSED_PATHS = [
    # the registry root and its API: search, audit, logins, tokens, keys, dist-tags, orgs
    "",
    "-",
    "-/",
    "-/v1/search",
    "-/v1/search%3Ftext=lodash",
    "-/npm/v1/security/advisories/bulk",
    "-/npm/v1/security/audits/quick",
    "-/npm/v1/security/audits",
    "-/whoami",
    "-/ping",
    "-/user/org.couchdb.user:alice",
    "-/v1/login",
    "-/npm/v1/tokens",
    "-/npm/v1/keys",
    "-/npm/v1/attestations/lodash@4.17.21",
    "-/package/lodash/dist-tags",
    "-/package/lodash/dist-tags/latest",
    "-/package/@types%2fnode/dist-tags",
    "-/package/lodash/access",
    "-/org/acme/user",
    "-/all",
    "-/tarballs/sha512/" + base64.urlsafe_b64encode(DIGEST).decode(),  # pnpm 11 lockfiles
    # other paths below a package: versions, unpublishing, the registry's own tarball path
    "lodash/",
    "lodash/latest",
    "lodash/4.17.21",
    "lodash/-rev/1-0123456789abcdef",
    "lodash/-/lodash-4.17.21.tgz",
    "@types%2fnode/-/node-20.11.0.tgz",
    "@types/node/-/node-20.11.0.tgz",
    "/lodash",
    "lodash//",
    "./lodash",
    # CouchDB's paths and dot segments
    "_all_docs",
    "_changes",
    "_session",
    "_users",
    "_design",
    ".",
    "..",
    ".lodash",
    # names npm refuses
    "-lodash",
    "--",
    "_lodash",
    "node_modules",
    "NODE_MODULES",
    "favicon.ico",
    "Favicon.ICO",
    "x" * (MAX_NAME_LENGTH + 1),
    "@s%2f" + "x" * (MAX_NAME_LENGTH - 2),
    "lo dash",
    "lo%20dash",
    "lo+dash",
    "lo@dash",
    "lo:dash",
    "lo;dash",
    "lo,dash",
    "lo=dash",
    "lo$dash",
    "lo&dash",
    "lo#dash",
    "lo?dash",
    "lo%3Fdash",
    "lo\\dash",
    "lo/dash",
    "lödash",
    "lo%C3%B6dash",
    "lo\x00dash",
    "lo%00dash",
    "lo\ndash",
    "lodash\t",
    "lo%2edash",
    "%6Codash",
    "lodash%2F",
    "lodash%2f",
    # scoped names npm refuses, and encodings clients never send
    "@types",
    "@types/",
    "@types%2f",
    "@types%2F",
    "@/node",
    "@%2fnode",
    "@@types/node",
    "@ty pes/node",
    "@ty@pes%2fnode",
    "%40types%2fnode",
    "%40types/node",
    "@types%252fnode",
    "@types%2F%2Fnode",
    "@types//node",
    "@types/node/",
    "@types/node/extra",
    "@types%2fnode%2fextra",
    "@types/.node",
    "@types%2f.node",
    "@types%5cnode",
    "@types\\node",
    "@types%2fnode%00",
    "@types%2Fnode%20",
    "@types%2fnode/",
    # tarball paths outside the layout
    "files/",
    "files/-",
    "files/-/",
    f"files/{HEX}",
    f"files/{HEX}/",
    f"files/{HEX}/lodash",
    f"files/{HEX}/lodash/-",
    f"files/{HEX}/lodash/-/",
    f"files/{HEX.upper()}/lodash/-/lodash-4.17.21.tgz",
    f"files/{HEX[:-2]}/lodash/-/lodash-4.17.21.tgz",
    f"files/{HEX}ab/lodash/-/lodash-4.17.21.tgz",
    f"files/{'ab' * 32}/lodash/-/lodash-4.17.21.tgz",  # a SHA-256
    "files//lodash/-/lodash-4.17.21.tgz",
    "files/--/lodash/-/lodash-4.17.21.tgz",
    f"files/{HEX}/lodash/lodash-4.17.21.tgz",
    f"files/{HEX}/lodash/x/lodash-4.17.21.tgz",
    f"files/{HEX}/lodash/-/-/lodash-4.17.21.tgz",
    f"files/{HEX}/lodash/-/lodash-4.17.21.tgz/",
    f"files/{HEX}/lodash/-/lodash-4.17.21.tgz/x",
    f"/files/{HEX}/lodash/-/lodash-4.17.21.tgz",
    f"files/{HEX}//lodash/-/lodash-4.17.21.tgz",
    f"files/{HEX}/lodash/-/..%2flodash-4.17.21.tgz",
    # the file must be a tarball name of the named package
    f"files/{HEX}/lodash/-/underscore-1.13.6.tgz",
    f"files/{HEX}/lodash/-/Lodash-4.17.21.tgz",
    f"files/{HEX}/Lodash/-/lodash-4.17.21.tgz",
    f"files/{HEX}/lodash/-/lodash-4.17.21.tar.gz",
    f"files/{HEX}/lodash/-/lodash-4.17.21.TGZ",
    f"files/{HEX}/lodash/-/lodash-4.17.21",
    f"files/{HEX}/lodash/-/lodash-4.17.21.tgz.sig",
    f"files/{HEX}/lodash/-/lodash-4.17.21%2etgz",
    f"files/{HEX}/lodash/-/lodash-4.17.21.tgz%3Fx=1",
    f"files/{HEX}/lodash/-/lodash-latest.tgz",
    f"files/{HEX}/lodash/-/lodash-4.17.tgz",
    f"files/{HEX}/lodash/-/lodash-4.17.21.1.tgz",  # node-semver's loose mode: 4.17.2-1.1
    f"files/{HEX}/lodash/-/lodash-1.2.34.5.tgz",
    f"files/{HEX}/lodash/-/lodash-04.17.21.tgz",  # SemVer numbers have no leading zeros
    f"files/{HEX}/lodash/-/lodash-4.17.21-01.tgz",
    f"files/{HEX}/lodash/-/lodash-4.17.211.tgz.tgz",
    f"files/{HEX}/lodash/-/lodash-v4.17.21.tgz",
    f"files/{HEX}/lodash/-/lodash-=4.17.21.tgz",
    f"files/{HEX}/lodash/-/lodash-4.17.21_1.tgz",
    f"files/{HEX}/lodash/-/lodash-4.17.21+.tgz",
    f"files/{HEX}/lodash/-/lodash-4.17.21%2B1.tgz",
    f"files/{HEX}/lodash/-/lodash--4.17.21.tgz",
    f"files/{HEX}/lodash/-/lodash.tgz",
    f"files/{HEX}/lodash/-/lodash-.tgz",
    f"files/{HEX}/lodash/-/lodash@.tgz",
    f"files/{HEX}/lodash/-/lodash#4.17.21.tgz",
    f"files/{HEX}/lodash/-/lodash-4.17.21-{'a' * MAX_VERSION_LENGTH}.tgz",
    # scoped tarballs: the scope keeps its slash, and the unscoped name names the file
    f"files/{HEX}/@types%2fnode/-/node-20.11.0.tgz",
    f"files/{HEX}/@types%2Fnode/-/node-20.11.0.tgz",
    f"files/{HEX}/@types/node/-/@types/node-20.11.0.tgz",  # Artifactory's layout
    f"files/{HEX}/@types/node/-/@types%2fnode-20.11.0.tgz",
    f"files/{HEX}/@types/node/-/types-node-20.11.0.tgz",
    f"files/{HEX}/@types/-/types-1.0.0.tgz",
    f"files/{HEX}/@types/node/extra/-/node-1.0.0.tgz",
    f"files/{HEX}/@types/.node/-/.node-1.0.0.tgz",
    # names npm refuses, in tarball paths
    f"files/{HEX}/_all_docs/-/_all_docs-1.0.0.tgz",
    f"files/{HEX}/node_modules/-/node_modules-1.0.0.tgz",
    f"files/{HEX}/../-/..-1.0.0.tgz",
    f"files/{HEX}/./-/.-1.0.0.tgz",
    f"files/{HEX}/-/-/--0.0.1.tgz",  # the registry's "-" package sits in its API namespace
    f"files/{HEX}/{'x' * (MAX_NAME_LENGTH + 1)}/-/{'x' * (MAX_NAME_LENGTH + 1)}-1.0.0.tgz",
]


@pytest.mark.parametrize("path", REFUSED_PATHS, ids=short_id)
def test_paths_outside_the_layout_are_refused(path: str) -> None:
    for headers in ({}, {"accept": INSTALL_ACCEPT}, {"accept": "application/json"}):
        assert ADAPTER.route(path, headers) is None


# --- rewriting real metadata ---------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    file: str
    package: str
    accept: str
    content_type: str
    covers: str

    @property
    def body(self) -> bytes:
        return (FIXTURES / self.file).read_bytes()


def load_fixtures() -> list[Fixture]:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    return [Fixture(**entry) for entry in manifest["fixtures"]]


ALL_FIXTURES = load_fixtures()


def rewrite_fixture(fixture: Fixture) -> bytes:
    route = fetch(metadata_path(fixture.package), fixture.accept)
    assert route.content_type == fixture.content_type
    assert route.rewrite is not None
    return route.rewrite(fixture.body, fixture.content_type)


def test_manifest_lists_every_fixture() -> None:
    on_disk = {path.name for path in FIXTURES.glob("*.json")} - {"manifest.json"}
    assert on_disk == {fixture.file for fixture in ALL_FIXTURES}


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda fixture: fixture.file)
def test_rewrite_points_every_tarball_at_the_cache(fixture: Fixture) -> None:
    original = json.loads(fixture.body)
    rewritten = json.loads(rewrite_fixture(fixture))
    versions = original["versions"]
    assert versions
    assert rewritten["versions"].keys() == versions.keys()
    for version, manifest in versions.items():
        dist = manifest["dist"]
        cache_link = rewritten["versions"][version]["dist"]["tarball"]
        assert cache_link.startswith(f"{FILES}/")
        # The link routes back to the registry's tarball, verified with the version's digest.
        back = ADAPTER.route(cache_link.removeprefix(f"{PUBLIC}/npm/"), {})
        assert isinstance(back, Fetch)
        assert back.url == dist["tarball"]
        assert back.kind is Kind.ARTIFACT
        assert back.immutable
        if "integrity" in dist:
            assert back.digest is not None
            # Cross-check: base64 of the digest in the path is the registry's integrity.
            assert (
                dist["integrity"]
                == "sha512-" + base64.b64encode(bytes.fromhex(back.digest.hex)).decode()
            )
        else:
            assert back.digest is None
            assert cache_link.startswith(f"{FILES}/-/")
    # Nothing but the tarball links changed.
    for version, manifest in rewritten["versions"].items():
        manifest["dist"]["tarball"] = versions[version]["dist"]["tarball"]
    assert rewritten == original
    assert list(rewritten) == list(original)


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda fixture: fixture.file)
def test_rewritten_metadata_is_compact_ascii_json(fixture: Fixture) -> None:
    body = rewrite_fixture(fixture)
    assert body.isascii()
    assert body == json.dumps(json.loads(body), separators=(",", ":")).encode()


@pytest.mark.parametrize(
    ("file", "version", "cache_link"),
    [
        (
            "isaacs__string-locale-compare.abbreviated.json",
            "1.1.0",
            f"{FILES}/{STRING_LOCALE_COMPARE_1_1_0}/@isaacs/string-locale-compare/-/"
            "string-locale-compare-1.1.0.tgz",
        ),
        (
            "http-shutdown.full.json",
            "1.0.1",
            f"{FILES}/-/http-shutdown/-/http-shutdown-1.0.1.tgz",
        ),
    ],
)
def test_known_links(file: str, version: str, cache_link: str) -> None:
    fixture = next(fixture for fixture in ALL_FIXTURES if fixture.file == file)
    rewritten = json.loads(rewrite_fixture(fixture))
    assert rewritten["versions"][version]["dist"]["tarball"] == cache_link


@pytest.mark.parametrize(
    ("file", "version", "tail"),
    [
        ("thirty-two.abbreviated.json", "0.0.1", "/thirty-two/-/thirty-two@0.0.1.tgz"),
        ("riak-js.full.json", "0.2.2f", "/riak-js/-/riak-js-0.2.2f.tgz"),
        ("riak-js.abbreviated.json", "0.3.0beta", "/riak-js/-/riak-js-0.3.0beta.tgz"),
        (
            "underscore.string.abbreviated.json",
            "2.2.0-rc",
            "/underscore.string/-/underscore.string-2.2.0rc.tgz",
        ),
        ("Base64.full.json", "1.3.0", "/Base64/-/Base64-1.3.0.tgz"),
    ],
)
def test_legacy_tarball_names_stay_installable(file: str, version: str, tail: str) -> None:
    fixture = next(fixture for fixture in ALL_FIXTURES if fixture.file == file)
    rewritten = json.loads(rewrite_fixture(fixture))
    assert rewritten["versions"][version]["dist"]["tarball"].endswith(tail)


def test_scope_encodings_rewrite_alike() -> None:
    fixture = next(f for f in ALL_FIXTURES if f.file == "isaacs__string-locale-compare.full.json")
    bodies: set[bytes] = set()
    for path in ("@isaacs%2Fstring-locale-compare", "@isaacs/string-locale-compare"):
        route = fetch(path, fixture.accept)
        assert route.rewrite is not None
        bodies.add(route.rewrite(fixture.body, fixture.content_type))
    assert bodies == {rewrite_fixture(fixture)}


def test_rewriting_needs_the_package_the_metadata_is_for() -> None:
    fixture = next(f for f in ALL_FIXTURES if f.file == "left-pad.abbreviated.json")
    route = fetch("right-pad", fixture.accept)
    assert route.rewrite is not None
    with pytest.raises(NpmMetadataError, match="metadata is for 'left-pad'"):
        route.rewrite(fixture.body, fixture.content_type)


def test_rewritten_metadata_cannot_be_rewritten_again() -> None:
    # Its links are on the cache, not the registry.
    fixture = next(f for f in ALL_FIXTURES if f.file == "tiny-tarball.abbreviated.json")
    route = fetch("tiny-tarball", fixture.accept)
    assert route.rewrite is not None
    once = route.rewrite(fixture.body, fixture.content_type)
    with pytest.raises(NpmMetadataError, match="not on the registry"):
        route.rewrite(once, fixture.content_type)


# --- the rewrite: links and integrity ------------------------------------------------------


def test_link_carries_the_sha512_as_hex() -> None:
    assert link(demo(tarball=DEMO_TARBALL, integrity=INTEGRITY)) == (
        f"{FILES}/{DIGEST.hex()}/demo/-/demo-1.0.0.tgz"
    )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.mark.parametrize(
    ("dist", "digest"),
    [
        ({}, "-"),
        ({"integrity": None}, "-"),  # npm then checks the SHA-1 shasum, as without integrity
        ({"integrity": ""}, "-"),
        ({"integrity": " \t\n"}, "-"),
        ({"integrity": "sha1-" + b64(bytes(20))}, "-"),
        ({"integrity": "sha256-" + b64(bytes(32))}, "-"),
        ({"integrity": "sha384-" + b64(bytes(48))}, "-"),
        ({"integrity": "SHA512-" + b64(DIGEST)}, "-"),  # npm ignores unknown algorithm names
        ({"integrity": "md5-x sha512 sha512x-y"}, "-"),
        ({"integrity": INTEGRITY}, DIGEST.hex()),
        ({"integrity": f"sha1-{b64(bytes(20))} {INTEGRITY}"}, DIGEST.hex()),
        ({"integrity": f"{INTEGRITY} sha256-{b64(bytes(32))}"}, DIGEST.hex()),
        ({"integrity": f"{INTEGRITY} {INTEGRITY}"}, DIGEST.hex()),
        ({"integrity": f" \t{INTEGRITY}\n"}, DIGEST.hex()),
        ({"integrity": f"{INTEGRITY}?opt-a?opt_b"}, DIGEST.hex()),
    ],
    ids=short_id,
)
def test_integrity_forms(dist: dict[str, object], digest: str) -> None:
    assert link(demo(tarball=DEMO_TARBALL, **dist)) == f"{FILES}/{digest}/demo/-/demo-1.0.0.tgz"


def non_canonical_base64() -> str:
    """A SHA-512 written with non-zero padding bits: it decodes to DIGEST all the same."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    canonical = b64(DIGEST)
    last = alphabet.index(canonical[85])
    altered = canonical[:85] + alphabet[last | 1] + "=="
    assert altered != canonical
    assert base64.b64decode(altered) == DIGEST
    return altered


@pytest.mark.parametrize(
    "integrity",
    [
        1,
        True,
        [INTEGRITY],
        {"sha512": b64(DIGEST)},
        "sha512-",
        "sha512-!!!",
        "sha512-" + b64(DIGEST)[:-2],
        "sha512-" + b64(DIGEST + b"\0"),
        "sha512-" + b64(DIGEST[:32]),
        "sha512-" + base64.urlsafe_b64encode(bytes([0xFB] * 64)).decode(),  # base64url
        "sha512-" + non_canonical_base64(),
        f"{INTEGRITY} sha512-{b64(bytes(64))}",  # two different digests
        f"{INTEGRITY}?café",
        f"{INTEGRITY}\N{NO-BREAK SPACE}",  # not SRI whitespace, so part of the token
    ],
    ids=short_id,
)
def test_unusable_integrity_fails_the_rewrite(integrity: object) -> None:
    with pytest.raises(NpmMetadataError, match="integrity"):
        rewrite(demo(tarball=DEMO_TARBALL, integrity=integrity))


@pytest.mark.parametrize(
    "tarball",
    [
        "https://evil.example/demo/-/demo-1.0.0.tgz",
        "http://registry.npmjs.org/demo/-/demo-1.0.0.tgz",
        "https://registry.npmjs.org.evil.example/demo/-/demo-1.0.0.tgz",
        "https://registry.npmjs.org@evil.example/demo/-/demo-1.0.0.tgz",
        "https://registry.npmjs.org:443/demo/-/demo-1.0.0.tgz",  # equivalent, but not as configured
        "https://REGISTRY.npmjs.org/demo/-/demo-1.0.0.tgz",
        "//registry.npmjs.org/demo/-/demo-1.0.0.tgz",
        "/demo/-/demo-1.0.0.tgz",
        "demo-1.0.0.tgz",
        "",
        f"{FILES}/{HEX}/demo/-/demo-1.0.0.tgz",
        f"{REGISTRY}/other/-/other-1.0.0.tgz",
        f"{REGISTRY}/demo/-/other-1.0.0.tgz",
        f"{REGISTRY}/demox/-/demox-1.0.0.tgz",
        f"{REGISTRY}/Demo/-/Demo-1.0.0.tgz",
        f"{REGISTRY}/demo/-/demo-1.0.0.tgz/../../../other/-/other-1.0.0.tgz",
        f"{REGISTRY}/demo/-/../../other/-/other-1.0.0.tgz",
        f"{REGISTRY}/demo/-/%2e%2e/demo-1.0.0.tgz",
        f"{REGISTRY}/demo/x/demo-1.0.0.tgz",
        f"{REGISTRY}/demo/demo-1.0.0.tgz",
        f"{REGISTRY}/-/demo-1.0.0.tgz",
        f"{REGISTRY}/demo/-/-/demo-1.0.0.tgz",
        f"{REGISTRY}/demo/-/demo-1.0.0.tgz?cache=bust",
        f"{REGISTRY}/demo/-/demo-1.0.0.tgz#x",
        f"{REGISTRY}/demo/-/demo-1.0.0.tar.gz",
        f"{REGISTRY}/demo/-/demo-latest.tgz",
        f"{REGISTRY}/demo/-/demo-1.0.0.tgz ",
        f" {REGISTRY}/demo/-/demo-1.0.0.tgz",
        f"{REGISTRY}/demo/-/demo-1.0.0.tgz\n",
    ],
    ids=short_id,
)
def test_tarballs_off_the_registry_layout_fail_the_rewrite(tarball: str) -> None:
    with pytest.raises(NpmMetadataError):
        rewrite(demo(tarball=tarball, integrity=INTEGRITY))


def scoped(tarball: str) -> dict[str, object]:
    return {"name": "@scope/demo", "versions": {"1.0.0": {"dist": {"tarball": tarball}}}}


def test_scoped_links_keep_the_scope_slash() -> None:
    rewritten = rewrite(scoped(f"{REGISTRY}/@scope/demo/-/demo-1.0.0.tgz"), name="@scope/demo")
    cache_link = rewritten["versions"]["1.0.0"]["dist"]["tarball"]
    assert cache_link == f"{FILES}/-/@scope/demo/-/demo-1.0.0.tgz"


@pytest.mark.parametrize(
    "tarball",
    [
        f"{REGISTRY}/@scope%2fdemo/-/demo-1.0.0.tgz",
        f"{REGISTRY}/@scope%2Fdemo/-/demo-1.0.0.tgz",
        f"{REGISTRY}/@scope/demo/-/@scope/demo-1.0.0.tgz",  # Artifactory's layout
        f"{REGISTRY}/@scope/demo/-/scope-demo-1.0.0.tgz",
        f"{REGISTRY}/@other/demo/-/demo-1.0.0.tgz",
        f"{REGISTRY}/demo/-/demo-1.0.0.tgz",
    ],
    ids=short_id,
)
def test_scoped_tarballs_must_use_the_registry_layout(tarball: str) -> None:
    with pytest.raises(NpmMetadataError):
        rewrite(scoped(tarball), name="@scope/demo")


def test_registry_below_a_path() -> None:
    registry = "https://npm.example.com/repository/npm"
    adapter = NpmAdapter(registry=registry, public_url=PUBLIC)
    assert fetch("@scope/demo", adapter=adapter).url == f"{registry}/@scope%2fdemo"
    tarball = f"{registry}/@scope/demo/-/demo-1.0.0.tgz"
    document = {"name": "@scope/demo", "versions": {"1.0.0": {"dist": {"tarball": tarball}}}}
    rewritten = rewrite(document, name="@scope/demo", adapter=adapter)
    cache_link = rewritten["versions"]["1.0.0"]["dist"]["tarball"]
    back = adapter.route(cache_link.removeprefix(f"{PUBLIC}/npm/"), {})
    assert isinstance(back, Fetch)
    assert back.url == tarball
    # A tarball at the host's root is not below the registry.
    outside = {
        "name": "@scope/demo",
        "versions": {
            "1.0.0": {"dist": {"tarball": "https://npm.example.com/@scope/demo/-/demo-1.0.0.tgz"}}
        },
    }
    with pytest.raises(NpmMetadataError):
        rewrite(outside, name="@scope/demo", adapter=adapter)


# --- the rewrite: documents ------------------------------------------------------------------


def test_metadata_without_versions_passes_through() -> None:
    # Unpublished packages keep a document without versions; nothing in it is installable.
    document = {"name": "demo", "time": {"unpublished": {"time": "2026-01-01T00:00:00.000Z"}}}
    assert rewrite(document) == document
    assert rewrite({"name": "demo", "versions": {}}) == {"name": "demo", "versions": {}}


def test_text_and_numbers_survive_the_round_trip() -> None:
    document = demo(tarball=DEMO_TARBALL)
    document["description"] = 'café ☃ \U0001f600 \ud800 \x00 \\ "quoted"'
    document["big"] = 123456789012345678901234567890
    document["float"] = 0.1
    document["nested"] = [{"a": [None, True, False, -1, 1.5e-300]}]
    rewritten = rewrite(document)
    rewritten["versions"]["1.0.0"]["dist"]["tarball"] = DEMO_TARBALL
    assert rewritten == document


def test_duplicate_keys_resolve_as_in_javascript() -> None:
    # JSON.parse keeps the last of duplicate keys, as Python does. Re-serializing leaves
    # only the value that was checked, so no client can read the other.
    evil = "https://evil.example/demo-1.0.0.tgz"
    dist = f'{{"tarball":"{evil}","tarball":"{DEMO_TARBALL}"}}'
    body = f'{{"name":"demo","versions":{{"1.0.0":{{"dist":{dist}}}}}}}'
    rewritten = rewrite(body.encode())
    assert rewritten["versions"]["1.0.0"]["dist"] == {"tarball": f"{FILES}/-/demo/-/demo-1.0.0.tgz"}
    swapped = body.replace(evil, "EVIL").replace(DEMO_TARBALL, evil).replace("EVIL", DEMO_TARBALL)
    with pytest.raises(NpmMetadataError):
        rewrite(swapped.encode())


@pytest.mark.parametrize(
    "document",
    [
        [],
        "demo",
        None,
        1,
        {"versions": {}},
        {"name": "other", "versions": {}},
        {"name": "Demo", "versions": {}},  # names are case-sensitive
        {"name": ["demo"], "versions": {}},
        {"name": "demo", "versions": None},
        {"name": "demo", "versions": []},
        {"name": "demo", "versions": {"1.0.0": None}},
        {"name": "demo", "versions": {"1.0.0": "1.0.0"}},
        {"name": "demo", "versions": {"1.0.0": {}}},
        {"name": "demo", "versions": {"1.0.0": {"dist": None}}},
        {"name": "demo", "versions": {"1.0.0": {"dist": [DEMO_TARBALL]}}},
        {"name": "demo", "versions": {"1.0.0": {"dist": {}}}},
        {"name": "demo", "versions": {"1.0.0": {"dist": {"tarball": None}}}},
        {"name": "demo", "versions": {"1.0.0": {"dist": {"tarball": 1}}}},
        {"name": "demo", "versions": {"1.0.0": {"dist": {"tarball": [DEMO_TARBALL]}}}},
        # One version that cannot be vouched for fails the whole document.
        {
            "name": "demo",
            "versions": {
                "1.0.0": {"dist": {"tarball": DEMO_TARBALL, "integrity": INTEGRITY}},
                "2.0.0": {"dist": {"tarball": "https://evil.example/demo/-/demo-2.0.0.tgz"}},
            },
        },
    ],
    ids=short_id,
)
def test_malformed_metadata_fails_the_rewrite(document: object) -> None:
    with pytest.raises(NpmMetadataError):
        rewrite(document)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"\xff\xfe{}",
        '{"name":"demo","versions":{}}'.encode("utf-16"),
        b'\xef\xbb\xbf{"name":"demo","versions":{}}',  # a byte order mark
        b'{"name":"demo","versions":{},"x":NaN}',
        b'{"name":"demo","versions":{},"x":Infinity}',
        b'{"name":"demo","versions":{},"x":-Infinity}',
        b'{"name":"demo","versions":{},"x":1e999}',
        b'{"name":"demo","versions":{},"x":' + b"1" * 5000 + b"}",
        b'{"name":"demo","versions":{},"x":' + b"[" * 100_000 + b"]" * 100_000 + b"}",
        b'{"name":"demo","versions":{}} trailing',
        b'{"name":"demo","versions":{}}{}',
        b'{"name":"demo","versions":{},}',
        b"{'name':'demo','versions':{}}",
    ],
    ids=short_id,
)
def test_bodies_that_are_not_json_fail_the_rewrite(body: bytes) -> None:
    with pytest.raises(NpmMetadataError):
        rewrite(body)


@pytest.mark.parametrize(
    "content_type",
    [
        ABBREVIATED_METADATA,
        FULL_METADATA,  # a registry that ignores the abbreviated Accept sends everything
        "application/json; charset=utf-8",
        "Application/VND.npm.Install-V1+JSON",
        " application/json ; charset=UTF-8",
    ],
)
def test_abbreviated_requests_take_either_document(content_type: str) -> None:
    assert link(demo(tarball=DEMO_TARBALL), content_type=content_type).startswith(FILES)


def test_full_requests_refuse_abbreviated_metadata() -> None:
    with pytest.raises(NpmMetadataError, match="abbreviated"):
        rewrite(
            demo(tarball=DEMO_TARBALL), accept="application/json", content_type=ABBREVIATED_METADATA
        )
    assert link(demo(tarball=DEMO_TARBALL), accept="application/json", content_type=FULL_METADATA)


@pytest.mark.parametrize(
    "content_type",
    [
        "",
        "text/html",
        "text/plain",
        "application/octet-stream",
        "application/jsonx",
        "application/json-seq",
        "application/problem+json",
        "application/vnd.npm.install-v2+json",
        "json",
    ],
)
def test_responses_that_are_not_metadata_fail_the_rewrite(content_type: str) -> None:
    with pytest.raises(NpmMetadataError, match="not metadata"):
        rewrite(demo(tarball=DEMO_TARBALL), content_type=content_type)


def test_error_messages_quote_untrusted_text_briefly() -> None:
    document = {"name": "demo", "versions": {"v" * 10_000: {"dist": {"tarball": "\x1b" * 10_000}}}}
    with pytest.raises(NpmMetadataError) as error:
        rewrite(document)
    message = str(error.value)
    assert len(message) < 400
    assert "\x1b" not in message
