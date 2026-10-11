# SPDX-License-Identifier: MIT
"""Tests for the PyPI adapter (docs/PACKAGE-CACHE.md).

The fixtures in fixtures/pypi are real project pages from pypi.org's simple index,
captured on 2026-10-10 in both formats and trimmed (fixtures/pypi/manifest.json says
how). Each rewritten page is parsed again here, and every link in it must route back
through the adapter to the upstream file it replaced, with the published digest.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.package_cache.adapter import Adapter, Digest, Fetch, Kind, Static
from sandbox_science.package_cache.adapters.pypi import (
    HTML_TYPE,
    JSON_TYPE,
    TEXT_HTML_TYPE,
    PypiAdapter,
    is_distribution_of,
    negotiate,
    normalize_name,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pypi"
MANIFEST: list[dict[str, str]] = json.loads((FIXTURES / "manifest.json").read_text())["fixtures"]

INDEX = "https://pypi.org/simple"
FILES = "https://files.pythonhosted.org"
PUBLIC = "http://host.openshell.internal:8090"
CACHE_FILES = f"{PUBLIC}/pypi/files"

# The Accept headers pip and uv send.
PIP_ACCEPT = (
    "application/vnd.pypi.simple.v1+json, application/vnd.pypi.simple.v1+html; q=0.1, "
    "text/html; q=0.01"
)
UV_ACCEPT = (
    "application/vnd.pypi.simple.v1+json, application/vnd.pypi.simple.v1+html;q=0.2, "
    "text/html;q=0.01"
)

# Files the rewrite leaves out of the fixtures: formats pip and uv do not install.
NOT_OFFERED = {
    "requests": {"requests-2.23.0-py2.7.egg"},
    "numpy": {"numpy-1.6.0.win32-py2.7.exe"},
    "setuptools": {
        "setuptools-0.6c11-1.src.rpm",
        "setuptools-0.6c11-py2.7.egg",
        "setuptools-0.6c11.win32-py2.7.exe",
    },
}

SHA = "a" * 64
BLAKE = "ab/cd/" + "e" * 60
WHEEL_PATH = (
    "packages/d7/73/c16e5f3f0d37c60947e70865c255a58dc408780a6474de0523afd0ec553a/"
    "sampleproject-4.0.0-py3-none-any.whl"
)
WHEEL_URL = f"{FILES}/{WHEEL_PATH}"
WHEEL_SHA = "c23e447ea90d796d1e645c35c4b2de125040add12a845825546f91c93f391b6b"


def make_adapter(
    *, index: str = INDEX, files: str = FILES, public_url: str = PUBLIC
) -> PypiAdapter:
    return PypiAdapter(index=index, files=files, public_url=public_url)


def fetch_page(adapter: PypiAdapter, path: str, accept: str | None) -> Fetch:
    headers = {} if accept is None else {"accept": accept}
    route = adapter.route(path, headers)
    assert isinstance(route, Fetch)
    return route


def rewrite(fetch: Fetch, body: bytes, content_type: str) -> bytes:
    assert fetch.rewrite is not None
    return fetch.rewrite(body, content_type)


class Page(HTMLParser):
    """An independent reading of an HTML page: its pypi: metas and its anchors."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.metas: dict[str, str | None] = {}
        self.anchors: list[tuple[dict[str, str | None], str]] = []
        self._anchor: dict[str, str | None] | None = None
        self._text: list[str] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._anchor, self._text = dict(attrs), []
        elif tag == "meta":
            values = dict(attrs)
            name = values.get("name")
            if name is not None and name.startswith("pypi:"):
                self.metas[name] = values.get("content")

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            self.anchors.append((self._anchor, "".join(self._text)))
            self._anchor = None


def check_link(
    adapter: PypiAdapter, link: str, project: str, upstream: str, sha: str | None
) -> None:
    """A rewritten link points at the cache and routes back to the upstream file."""
    assert link.startswith(f"{CACHE_FILES}/{project}/{sha or '-'}/packages/")
    route = adapter.route(link.removeprefix(f"{PUBLIC}/pypi/"), {})
    assert isinstance(route, Fetch)
    assert route.url == upstream
    assert route.key == f"{upstream} sha256={sha or '-'}"
    assert route.kind is Kind.ARTIFACT
    assert route.immutable
    assert route.digest == (Digest("sha256", sha) if sha else None)
    assert route.rewrite is None
    # pip and uv fetch the core metadata (PEP 658) by appending .metadata to the link.
    metadata = adapter.route(f"{link.removeprefix(f'{PUBLIC}/pypi/')}.metadata", {})
    assert isinstance(metadata, Fetch)
    assert metadata.url == f"{upstream}.metadata"
    assert metadata.key == metadata.url
    assert metadata.kind is Kind.METADATA
    assert metadata.immutable
    assert metadata.digest is None


# --- construction -----------------------------------------------------------------------


def test_is_an_adapter() -> None:
    adapter: Adapter = make_adapter()
    assert adapter.prefix == "pypi"


@pytest.mark.parametrize(
    "settings",
    [
        {"index": "http://pypi.org/simple"},
        {"index": "https://pypi.org/simple/"},
        {"index": "https://pypi.org/simple?x=1"},
        {"index": "https://user@pypi.org/simple"},
        {"index": "pypi.org/simple"},
        {"files": "http://files.pythonhosted.org"},
        {"files": "https://files.pythonhosted.org/"},
        {"files": "https://files.pythonhosted.org#x"},
        {"public_url": "http://host.openshell.internal:8090/"},
        {"public_url": "ftp://host.openshell.internal"},
        {"public_url": "http://user:pass@host.openshell.internal:8090"},
        {"public_url": ""},
    ],
)
def test_rejects_invalid_settings(settings: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="must be an absolute"):
        make_adapter(**settings)


# --- content negotiation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        (PIP_ACCEPT, JSON_TYPE),
        (UV_ACCEPT, JSON_TYPE),
        (JSON_TYPE, JSON_TYPE),
        (HTML_TYPE, HTML_TYPE),
        ("text/html", TEXT_HTML_TYPE),
        ("application/vnd.pypi.simple.latest+json", JSON_TYPE),
        ("application/vnd.pypi.simple.latest+html", HTML_TYPE),
        ("APPLICATION/VND.PYPI.SIMPLE.V1+JSON", JSON_TYPE),
        # No header, an empty one, or only wildcards: HTML, as PyPI answers.
        (None, TEXT_HTML_TYPE),
        ("", TEXT_HTML_TYPE),
        ("*/*", TEXT_HTML_TYPE),
        ("text/*", TEXT_HTML_TYPE),
        ("application/*", HTML_TYPE),
        # Quality values decide.
        (f"{JSON_TYPE};q=0.4, {HTML_TYPE};q=0.5", HTML_TYPE),
        (f"{JSON_TYPE};q=0.001, text/html", TEXT_HTML_TYPE),
        (f"text/html;q=0.9, {JSON_TYPE};q=0.95", JSON_TYPE),
        (f"{JSON_TYPE};q=0, */*", TEXT_HTML_TYPE),
        ("text/html;q=0, */*;q=0.5", HTML_TYPE),
        (f"{JSON_TYPE}, */*;q=0.1", JSON_TYPE),
        # Ties: a named format beats a wildcard match; among named ones JSON wins.
        (f"text/html, {JSON_TYPE}", JSON_TYPE),
        (f"{JSON_TYPE};q=0.5, {HTML_TYPE};q=0.5", JSON_TYPE),
        (f"{HTML_TYPE}, text/html", HTML_TYPE),
        ("text/html, */*", TEXT_HTML_TYPE),
        # The most specific range sets the quality.
        (f"application/*;q=0.1, {JSON_TYPE};q=0.9, */*;q=0.5", JSON_TYPE),
        (f"application/*;q=0.9, {JSON_TYPE};q=0.2", HTML_TYPE),
        # Spacing and parameters.
        (f"  {JSON_TYPE} ; q=1.000 ,text/html;q=0.5", JSON_TYPE),
        (f"{JSON_TYPE};level=1;q=0.3, {HTML_TYPE};q=0.2", JSON_TYPE),
        (f"{JSON_TYPE};q=0.3;ext=1, {HTML_TYPE};q=0.2", JSON_TYPE),
        # Malformed ranges are skipped; with none left the header counts as */*.
        ("garbage", TEXT_HTML_TYPE),
        (f"{JSON_TYPE};q=2", TEXT_HTML_TYPE),
        (f"{JSON_TYPE};q=0.0001, text/html;q=0.5", TEXT_HTML_TYPE),
        (f"{JSON_TYPE};q, {HTML_TYPE};q=0.5", HTML_TYPE),
        (f"*/json, {HTML_TYPE};q=0.5", HTML_TYPE),
    ],
)
def test_negotiates_the_page_format(accept: str | None, expected: str) -> None:
    assert negotiate(accept) == expected


@pytest.mark.parametrize(
    "accept",
    [
        "application/json",
        "application/vnd.pypi.simple.v2+json",
        "image/*",
        "*/*;q=0",
        f"{JSON_TYPE};q=0, {HTML_TYPE};q=0, text/html;q=0",
        f"{JSON_TYPE};q=0, application/*;q=0, text/*;q=0",
    ],
)
def test_nothing_acceptable(accept: str) -> None:
    assert negotiate(accept) is None
    route = make_adapter().route("simple/numpy/", {"accept": accept})
    assert isinstance(route, Static)
    assert route.status == 406


# --- project pages ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("accept", "content_type"),
    [(UV_ACCEPT, JSON_TYPE), (HTML_TYPE, HTML_TYPE), (None, TEXT_HTML_TYPE)],
)
def test_project_page(accept: str | None, content_type: str) -> None:
    fetch = fetch_page(make_adapter(), "simple/numpy/", accept)
    assert fetch.url == "https://pypi.org/simple/numpy/"
    assert fetch.kind is Kind.METADATA
    assert not fetch.immutable
    assert fetch.digest is None
    assert fetch.headers == {"Accept": content_type}
    assert fetch.content_type == content_type
    assert fetch.rewrite is not None
    assert content_type in fetch.key


@pytest.mark.parametrize(
    ("name", "normalized"),
    [
        ("Typing_Extensions", "typing-extensions"),
        ("typing.extensions", "typing-extensions"),
        ("ZOPE.interface", "zope-interface"),
        ("a--b__c..d-_.e", "a-b-c-d-e"),
        ("Django", "django"),
        ("x", "x"),
        ("7", "7"),
    ],
)
def test_project_names_are_normalized(name: str, normalized: str) -> None:
    adapter = make_adapter()
    fetch = fetch_page(adapter, f"simple/{name}/", UV_ACCEPT)
    assert normalize_name(name) == normalized
    assert fetch.url == f"https://pypi.org/simple/{normalized}/"
    assert fetch.key == fetch_page(adapter, f"simple/{normalized}/", UV_ACCEPT).key


def test_page_keys_name_everything_the_stored_page_depends_on() -> None:
    adapter = make_adapter()
    keys = {
        fetch_page(adapter, "simple/numpy/", accept).key
        for accept in (JSON_TYPE, HTML_TYPE, TEXT_HTML_TYPE)
    }
    keys.add(
        fetch_page(make_adapter(public_url="https://cache.example"), "simple/numpy/", None).key
    )
    keys.add(fetch_page(make_adapter(files="https://files.example"), "simple/numpy/", None).key)
    keys.add(
        fetch_page(make_adapter(index="https://index.example/simple"), "simple/numpy/", None).key
    )
    assert len(keys) == 6


@pytest.mark.parametrize(
    "path",
    [
        "",
        "simple",
        "simple/",
        "simple/numpy",
        "simple/numpy//",
        "simple//numpy/",
        "simple/numpy/index.html",
        "simple/numpy/?format=json",
        "simple/-numpy/",
        "simple/numpy-/",
        "simple/_numpy/",
        "simple/.numpy/",
        "simple/num%70y/",
        "simple/num py/",
        "simple/n\N{LATIN SMALL LETTER U WITH DIAERESIS}mpy/",
        "simple/../",
        "simple/./",
        "simple/a/b/",
        "simple/numpy/\n",
        "simple/numpy\\",
        "SIMPLE/numpy/",
        "/simple/numpy/",
        "json/numpy",
        "pypi/numpy/json",
        "numpy/",
        "files/",
    ],
)
def test_refuses_other_paths(path: str) -> None:
    assert make_adapter().route(path, {"accept": UV_ACCEPT}) is None


# --- files --------------------------------------------------------------------------------


def files_path(project: str, filename: str, digest: str = SHA) -> str:
    return f"files/{project}/{digest}/packages/{BLAKE}/{filename}"


def fetch_file(adapter: PypiAdapter, path: str) -> Fetch:
    route = adapter.route(path, {})
    assert isinstance(route, Fetch)
    return route


@pytest.mark.parametrize(
    ("project", "filename"),
    [
        ("sampleproject", "sampleproject-4.0.0-py3-none-any.whl"),
        ("sampleproject", "sampleproject-4.0.0-1-py3-none-any.whl"),
        ("typing-extensions", "typing_extensions-4.16.0-py3-none-any.whl"),
        ("zope-interface", "zope.interface-7.0-cp312-cp312-manylinux_2_17_x86_64.whl"),
        ("django", "Django-5.1-py3-none-any.whl"),
        ("sampleproject", "sampleproject-4.0.0.tar.gz"),
        ("typing-extensions", "typing_extensions-4.16.0.tar.gz"),
        ("python-dateutil", "python-dateutil-2.8.2.tar.gz"),
        ("mysql-python", "MySQL-python-1.2.5.zip"),
        ("numpy", "numpy-1.6.0.zip"),
        ("pytz", "pytz-2004b.tar.bz2"),
        ("foo", "foo-1.0.tgz"),
        ("setuptools", "setuptools-0.6c11.tar.gz"),
        ("foo", "foo-1!2.0.post1.dev3.tar.gz"),
        ("foo", "foo-1.0+local.7.tar.gz"),
        ("foo", "foo-v1.0rc1.tar.gz"),
        # Ambiguous legacy names: the stem splits as foo + 1-2 or foo-1 + 2.
        ("foo", "foo-1-2.tar.gz"),
        ("foo-1", "foo-1-2.tar.gz"),
        ("foo-1", "foo_1-2.tar.gz"),
    ],
)
def test_distribution_files(project: str, filename: str) -> None:
    assert is_distribution_of(filename, project)
    adapter = make_adapter()
    url = f"{FILES}/packages/{BLAKE}/{filename}"
    route = adapter.route(files_path(project, filename), {})
    assert route == Fetch(
        key=f"{url} sha256={SHA}",
        url=url,
        kind=Kind.ARTIFACT,
        immutable=True,
        digest=Digest("sha256", SHA),
    )
    route = adapter.route(files_path(project, filename, "-"), {})
    assert route == Fetch(key=f"{url} sha256=-", url=url, kind=Kind.ARTIFACT, immutable=True)
    route = adapter.route(f"{files_path(project, filename)}.metadata", {})
    assert route == Fetch(
        key=f"{url}.metadata", url=f"{url}.metadata", kind=Kind.METADATA, immutable=True
    )


def test_file_keys_include_the_digest() -> None:
    # Requests naming different digests, or none, must never share a fetch or a stored
    # file: a request naming a wrong digest would otherwise make a concurrent request
    # naming the right one fail (502), and a body verified against one digest must never
    # be served for another.
    adapter = make_adapter()
    filename = "sampleproject-4.0.0-py3-none-any.whl"
    digests = (WHEEL_SHA, SHA, "-")
    files = [fetch_file(adapter, files_path("sampleproject", filename, d)) for d in digests]
    assert {route.url for route in files} == {f"{FILES}/packages/{BLAKE}/{filename}"}
    assert len({route.key for route in files}) == len(digests)
    assert [route.digest for route in files] == [
        Digest("sha256", WHEEL_SHA),
        Digest("sha256", SHA),
        None,
    ]
    # The core metadata (PEP 658) has no digest of its own and nothing verifies it, so
    # every spelling of its path shares one entry, which no distribution shares.
    metadata_keys = {
        fetch_file(adapter, f"{files_path('sampleproject', filename, d)}.metadata").key
        for d in digests
    }
    assert metadata_keys == {f"{FILES}/packages/{BLAKE}/{filename}.metadata"}
    assert metadata_keys.isdisjoint(route.key for route in files)


@pytest.mark.parametrize(
    ("project", "filename"),
    [
        # A file of another project, requested under this project's name.
        ("requests", "urllib3-2.2.3-py3-none-any.whl"),
        ("requests", "urllib3-2.2.3.tar.gz"),
        ("requests", "urllib3-2.2.3-py3-none-any.whl.metadata"),
        # A longer name that starts with the project's name.
        ("django", "django_evil-1.0-py3-none-any.whl"),
        ("django", "django-evil-1.0.tar.gz"),
        ("django", "Django.Evil-1.0.zip"),
        ("django", "django-2fa-1.0.tar.gz"),
        ("django-evil", "django-1.0.tar.gz"),
        # A PEP 625 sdist escapes "-" in the name, so foo_1 is not foo.
        ("foo", "foo_1-2.tar.gz"),
        ("foo", "foo_1-2-py3-none-any.whl"),
        # Not a version after the name.
        ("foo", "foo-bar.tar.gz"),
        ("foo", "foo-.tar.gz"),
        ("foo", "foo.tar.gz"),
        ("foo", "foo-1.0-r123-bogus.tar.gz"),
        # Malformed wheel names.
        ("foo", "foo-1.0.whl"),
        ("foo", "foo-1.0-py3-none.whl"),
        ("foo", "foo-1.0-1-2-py3-none-any.whl"),
        ("foo", "foo--py3-none-any.whl"),
        # Formats pip and uv do not install from an index.
        ("foo", "foo-1.0-py2.7.egg"),
        ("foo", "foo-1.0.win32-py2.7.exe"),
        ("foo", "foo-1.0-1.src.rpm"),
        ("foo", "foo-1.0.msi"),
        ("foo", "foo-1.0.dmg"),
        ("foo", "foo-1.0.tar.xz"),
        ("foo", "foo-1.0.TAR.GZ"),
        ("foo", "foo-1.0.tar.gz.asc"),
        ("foo", "foo-1.0-py3-none-any.whl.asc"),
        ("foo", "foo-1.0-py2.7.egg.metadata"),
        ("foo", "foo-1.0-py3-none-any.whl.metadata.metadata"),
        # Characters outside the file name grammar.
        ("foo", ".foo-1.0.tar.gz"),
        ("foo", "foo-1.0%2Ftar.gz"),
        ("foo", "foo%201.0.tar.gz"),
        ("foo", "foo 1.0.tar.gz"),
        ("foo", f"foo-1.0-py3-none-{'x' * 240}.whl"),
    ],
)
def test_refuses_files_that_are_not_the_projects(project: str, filename: str) -> None:
    assert make_adapter().route(files_path(project, filename), {}) is None


def test_file_of_project_b_under_project_a_is_refused() -> None:
    adapter = make_adapter()
    assert adapter.route(files_path("urllib3", "urllib3-2.2.3-py3-none-any.whl"), {}) is not None
    assert adapter.route(files_path("requests", "urllib3-2.2.3-py3-none-any.whl"), {}) is None
    assert adapter.route(files_path("requests", "requests-2.32.3-py3-none-any.whl"), {}) is not None


@pytest.mark.parametrize(
    "path",
    [
        # The project segment must be the normalized name.
        f"files/Requests/{SHA}/packages/{BLAKE}/requests-2.0.0.tar.gz",
        f"files/typing_extensions/{SHA}/packages/{BLAKE}/typing_extensions-4.0.0.tar.gz",
        f"files/typing--extensions/{SHA}/packages/{BLAKE}/typing_extensions-4.0.0.tar.gz",
        f"files/-requests/{SHA}/packages/{BLAKE}/requests-2.0.0.tar.gz",
        f"files//{SHA}/packages/{BLAKE}/requests-2.0.0.tar.gz",
        # The digest must be lowercase SHA-256 hex or "-".
        f"files/requests/{'A' * 64}/packages/{BLAKE}/requests-2.0.0.tar.gz",
        f"files/requests/{'a' * 63}/packages/{BLAKE}/requests-2.0.0.tar.gz",
        f"files/requests/{'a' * 65}/packages/{BLAKE}/requests-2.0.0.tar.gz",
        f"files/requests/--/packages/{BLAKE}/requests-2.0.0.tar.gz",
        f"files/requests//packages/{BLAKE}/requests-2.0.0.tar.gz",
        f"files/requests/{SHA}",
        # The files host's layout, and nothing else.
        f"files/requests/{SHA}/{BLAKE}/requests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/ab/cd/{'e' * 59}/requests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/ab/cd/{'E' * 60}/requests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/abc/d/{'e' * 60}/requests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/{BLAKE}/x/requests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/{BLAKE}/",
        f"files/requests/{SHA}/packages/source/r/requests/requests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/{BLAKE}/../requests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/{BLAKE}/%2E%2E/requests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/{BLAKE}%2Frequests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/{BLAKE}\\requests-2.0.0.tar.gz",
        f"files/requests/{SHA}/packages/{BLAKE}/requests-2.0.0.tar.gz?x=1",
        f"files/requests/{SHA}/packages/{BLAKE}/requests-2.0.0.tar.gz#sha256={SHA}",
        f"files/requests/{SHA}/packages/{BLAKE}/requests-2.0.0.tar.gz/",
        f"/files/requests/{SHA}/packages/{BLAKE}/requests-2.0.0.tar.gz",
    ],
)
def test_refuses_malformed_file_paths(path: str) -> None:
    assert make_adapter().route(path, {}) is None


# --- rewriting the captured pages -----------------------------------------------------------


def manifest(suffix: str) -> list[dict[str, str]]:
    return [entry for entry in MANIFEST if entry["file"].endswith(suffix)]


@pytest.mark.parametrize("entry", manifest(".json"), ids=lambda entry: entry["file"])
def test_rewrites_captured_json_pages(entry: dict[str, str]) -> None:
    adapter = make_adapter()
    project = entry["project"]
    fetch = fetch_page(adapter, entry["path"], UV_ACCEPT)
    assert fetch.url == entry["url"]
    body = (FIXTURES / entry["file"]).read_bytes()
    upstream: dict[str, Any] = json.loads(body)
    rewritten: dict[str, Any] = json.loads(rewrite(fetch, body, entry["content_type"]))

    assert rewritten["meta"] == {"api-version": "1.4"}
    assert rewritten["name"] == upstream["name"] == project
    assert rewritten["project-status"] == upstream["project-status"]
    assert rewritten["versions"] == upstream["versions"]
    dropped = NOT_OFFERED.get(project, set())
    kept = [file for file in upstream["files"] if file["filename"] not in dropped]
    assert len(kept) == len(upstream["files"]) - len(dropped)
    assert [file["filename"] for file in rewritten["files"]] == [file["filename"] for file in kept]
    for new, old in zip(rewritten["files"], kept, strict=True):
        assert new.keys() == old.keys()
        assert {key: value for key, value in new.items() if key != "url"} == {
            key: value for key, value in old.items() if key != "url"
        }
        check_link(adapter, new["url"], project, old["url"], old["hashes"].get("sha256"))


@pytest.mark.parametrize("entry", manifest(".html"), ids=lambda entry: entry["file"])
@pytest.mark.parametrize("accept", [HTML_TYPE, TEXT_HTML_TYPE])
def test_rewrites_captured_html_pages(entry: dict[str, str], accept: str) -> None:
    adapter = make_adapter()
    project = entry["project"]
    fetch = fetch_page(adapter, entry["path"], accept)
    assert fetch.content_type == accept
    body = (FIXTURES / entry["file"]).read_bytes()
    upstream = Page(body.decode())
    output = rewrite(fetch, body, entry["content_type"])
    output.decode("ascii")
    rewritten = Page(output.decode())

    assert rewritten.metas == {
        "pypi:repository-version": "1.4",
        "pypi:project-status": "active",
    }
    dropped = NOT_OFFERED.get(project, set())
    kept = [(attrs, text) for attrs, text in upstream.anchors if text not in dropped]
    assert len(kept) == len(upstream.anchors) - len(dropped)
    assert [text for _, text in rewritten.anchors] == [text for _, text in kept]
    for (new, new_text), (old, old_text) in zip(rewritten.anchors, kept, strict=True):
        assert new_text == old_text
        assert new.keys() == old.keys()
        assert {key: value for key, value in new.items() if key != "href"} == {
            key: value for key, value in old.items() if key != "href"
        }
        new_href, old_href = new["href"], old["href"]
        assert new_href is not None
        assert old_href is not None
        old_url, _, old_fragment = old_href.partition("#")
        new_url, _, new_fragment = new_href.partition("#")
        assert new_fragment == old_fragment
        assert old_fragment.startswith("sha256=")
        check_link(adapter, new_url, project, old_url, old_fragment.removeprefix("sha256="))


def test_html_attributes_are_escaped() -> None:
    entry = next(entry for entry in manifest(".html") if entry["project"] == "sampleproject")
    fetch = fetch_page(make_adapter(), entry["path"], HTML_TYPE)
    output = rewrite(fetch, (FIXTURES / entry["file"]).read_bytes(), entry["content_type"])
    assert b'data-requires-python="&gt;=3.9"' in output
    assert b"&lt;4" in output
    # Every "<" starts a tag or the doctype, so nothing from the page escaped its attribute.
    assert re.search(rb"<(?![a-z/!])", output) is None


# --- rewriting: synthetic pages ---------------------------------------------------------------

SAMPLE = "sampleproject"


def json_file(**fields: Any) -> dict[str, Any]:
    file: dict[str, Any] = {
        "filename": "sampleproject-4.0.0-py3-none-any.whl",
        "url": WHEEL_URL,
        "hashes": {"sha256": WHEEL_SHA},
    }
    file.update(fields)
    return {key: value for key, value in file.items() if value is not ...}


def json_page(*files: object, **fields: Any) -> bytes:
    page: dict[str, Any] = {"meta": {"api-version": "1.4"}, "name": SAMPLE, "files": list(files)}
    page.update(fields)
    return json.dumps({key: value for key, value in page.items() if value is not ...}).encode()


def rewrite_json(
    body: bytes, *, adapter: PypiAdapter | None = None, content_type: str = JSON_TYPE
) -> Any:
    fetch = fetch_page(adapter or make_adapter(), f"simple/{SAMPLE}/", JSON_TYPE)
    return json.loads(rewrite(fetch, body, content_type))


def html_page(
    anchors: str, head: str = '<meta name="pypi:repository-version" content="1.4">'
) -> bytes:
    return (
        f"<!DOCTYPE html><html><head>{head}<title>Links</title></head><body>{anchors}</body></html>"
    ).encode()


def anchor(href: str = f"{WHEEL_URL}#sha256={WHEEL_SHA}", attributes: str = "") -> str:
    return f'<a href="{href}"{attributes}>sampleproject-4.0.0-py3-none-any.whl</a><br/>'


def rewrite_html(
    body: bytes, *, adapter: PypiAdapter | None = None, content_type: str = HTML_TYPE
) -> bytes:
    fetch = fetch_page(adapter or make_adapter(), f"simple/{SAMPLE}/", HTML_TYPE)
    return rewrite(fetch, body, content_type)


def test_json_link_points_at_the_cache() -> None:
    page = rewrite_json(json_page(json_file()))
    assert page["files"][0]["url"] == f"{CACHE_FILES}/{SAMPLE}/{WHEEL_SHA}/{WHEEL_PATH}"


def test_json_without_sha256_uses_a_dash() -> None:
    page = rewrite_json(json_page(json_file(hashes={"md5": "0" * 32})))
    assert page["files"][0]["url"] == f"{CACHE_FILES}/{SAMPLE}/-/{WHEEL_PATH}"
    assert page["files"][0]["hashes"] == {"md5": "0" * 32}


def test_json_hash_case_is_normalized_in_the_path_only() -> None:
    page = rewrite_json(json_page(json_file(hashes={"sha256": WHEEL_SHA.upper()})))
    assert page["files"][0]["url"] == f"{CACHE_FILES}/{SAMPLE}/{WHEEL_SHA}/{WHEEL_PATH}"
    assert page["files"][0]["hashes"] == {"sha256": WHEEL_SHA.upper()}


def test_json_relative_urls_resolve_against_the_page() -> None:
    adapter = make_adapter(files="https://pypi.org/files")
    file = json_file(url=f"../../files/{WHEEL_PATH}")
    page = rewrite_json(json_page(file), adapter=adapter)
    link = page["files"][0]["url"]
    assert link == f"{CACHE_FILES}/{SAMPLE}/{WHEEL_SHA}/{WHEEL_PATH}"
    check_link(adapter, link, SAMPLE, f"https://pypi.org/files/{WHEEL_PATH}", WHEEL_SHA)


def test_json_keeps_only_known_fields_and_caps_the_version() -> None:
    page = rewrite_json(
        json_page(
            json_file(
                **{
                    "requires-python": ">=3.9",
                    "core-metadata": {"sha256": "b" * 64},
                    "dist-info-metadata": True,
                    "data-dist-info-metadata": {"sha256": "b" * 64},
                    "gpg-sig": False,
                    "yanked": "broken <build>",
                    "size": 4661,
                    "upload-time": "2024-11-06T22:37:09.220617Z",
                    "provenance": "https://pypi.org/integrity/sampleproject/4.0.0/x/provenance",
                    "mirror-url": "https://evil.example/sampleproject-4.0.0-py3-none-any.whl",
                    "_private": 1,
                }
            ),
            meta={"api-version": "1.9", "_last-serial": 7, "extra": "x"},
            versions=["4.0.0", "5.0.0"],
            **{
                "project-status": {"status": "archived", "reason": "moved", "x": 1},
                "alternate-locations": ["https://evil.example/simple/sampleproject/"],
                "_private": {"url": "https://evil.example/"},
            },
        )
    )
    assert page["meta"] == {"api-version": "1.4"}
    assert page["project-status"] == {"status": "archived", "reason": "moved"}
    assert page["versions"] == ["4.0.0", "5.0.0"]
    assert set(page) == {"meta", "name", "project-status", "versions", "files"}
    [file] = page["files"]
    assert "mirror-url" not in file
    assert "_private" not in file
    assert file["yanked"] == "broken <build>"
    assert file["core-metadata"] == {"sha256": "b" * 64}
    assert "evil.example" not in json.dumps(page)


@pytest.mark.parametrize(("version", "expected"), [("1.0", "1.0"), ("1.1", "1.1"), ("1.04", "1.4")])
def test_json_keeps_older_api_versions(version: str, expected: str) -> None:
    assert rewrite_json(json_page(meta={"api-version": version}))["meta"] == {
        "api-version": expected
    }


@pytest.mark.parametrize(
    "file",
    [
        # Another project's file on this project's page.
        json_file(
            filename="urllib3-2.2.3-py3-none-any.whl",
            url=f"{FILES}/packages/{BLAKE}/urllib3-2.2.3-py3-none-any.whl",
        ),
        # A format pip and uv do not install.
        json_file(
            filename="sampleproject-1.0-py2.7.egg",
            url=f"{FILES}/packages/{BLAKE}/sampleproject-1.0-py2.7.egg",
        ),
        # A file name the cache would not route.
        json_file(
            filename="sampleproject 1.0.tar.gz",
            url=f"{FILES}/packages/{BLAKE}/sampleproject%201.0.tar.gz",
        ),
        # The filename field disagrees with the URL.
        json_file(filename="sampleproject-9.0.0-py3-none-any.whl"),
    ],
)
def test_json_leaves_out_files_the_cache_would_refuse(file: dict[str, Any]) -> None:
    page = rewrite_json(json_page(json_file(), file))
    assert [entry["filename"] for entry in page["files"]] == [
        "sampleproject-4.0.0-py3-none-any.whl"
    ]


OFF_HOST = [
    "https://evil.example/" + WHEEL_PATH,
    "http://files.pythonhosted.org/" + WHEEL_PATH,
    "https://user@files.pythonhosted.org/" + WHEEL_PATH,
    "https://files.pythonhosted.org@evil.example/" + WHEEL_PATH,
    "https://files.pythonhosted.org\\@evil.example/" + WHEEL_PATH,
    "https://evil.example\\.files.pythonhosted.org/" + WHEEL_PATH,
    "https://files.pythonhosted.org.evil.example/" + WHEEL_PATH,
    "https://files.pythonhosted.org:8443/" + WHEEL_PATH,
    "https://files.pythonhosted.org:99999/" + WHEEL_PATH,
    "//evil.example/" + WHEEL_PATH,
    "../../" + WHEEL_PATH,  # pypi.org, not the files host
    "/" + WHEEL_PATH,
    WHEEL_PATH,
    "",
    "#sha256=" + WHEEL_SHA,
    "file:///etc/passwd",
    "javascript:alert(1)",
]
MALFORMED = [
    WHEEL_URL + "?x=1",
    WHEEL_URL + "?",
    WHEEL_URL + "#egg=sampleproject",
    WHEEL_URL + "#sha256=xyz",
    WHEEL_URL + "#sha256=" + "a" * 63,
    WHEEL_URL + "#sha256=" + WHEEL_SHA + "&x=1",
    WHEEL_URL.replace("packages/", "pack ages/"),
    WHEEL_URL.replace("sampleproject-4", "sampleproject\N{EN DASH}4"),
    WHEEL_URL.replace("/d7/73/", "/D7/73/"),
    f"{FILES}/packages/source/s/sampleproject/sampleproject-4.0.0.tar.gz",
    f"{FILES}/sampleproject-4.0.0-py3-none-any.whl",
    f"{FILES}/packages/{BLAKE}/sub/sampleproject-4.0.0-py3-none-any.whl",
]


@pytest.mark.parametrize("url", OFF_HOST + MALFORMED)
def test_json_fails_on_links_off_the_files_host(url: str) -> None:
    with pytest.raises(ValueError, match=r"file URL|fragment"):
        rewrite_json(json_page(json_file(url=url, hashes={})))


@pytest.mark.parametrize("url", OFF_HOST + MALFORMED)
def test_html_fails_on_links_off_the_files_host(url: str) -> None:
    with pytest.raises(ValueError, match=r"file URL|fragment"):
        rewrite_html(html_page(anchor(href=url)))


def test_json_hashes_and_fragment_must_agree() -> None:
    url = f"{WHEEL_URL}#sha256={'b' * 64}"
    with pytest.raises(ValueError, match="disagree"):
        rewrite_json(json_page(json_file(url=url)))


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xfe",
        b"not json",
        b"[]",
        b'{"meta": {"api-version": "1.4"}, "meta": {"api-version": "1.4"}}',
        b'{"meta": {"api-version": "1.4"}, "name": "sampleproject", "files": [], "x": NaN}',
        b'{"meta": {"api-version": "1.4"}, "name": "sampleproject", "files": [], "x": Infinity}',
        b"[" * 100_000 + b"]" * 100_000,
        json_page(meta=...),
        json_page(meta={"api-version": "2.0"}),
        json_page(meta={"api-version": "1"}),
        json_page(meta={"api-version": 1.4}),
        json_page(meta={}),
        json_page(name="requests"),
        json_page(name=...),
        json_page(files=...),
        json_page(files={"a": 1}),
        json_page(versions="4.0.0"),
        json_page(versions=[4]),
        json_page(**{"project-status": "active"}),
        json_page(**{"project-status": {"status": "Active!"}}),
        json_page(**{"project-status": {"reason": 3}}),
        json_page("not a file"),
        json_page(json_file(filename=...)),
        json_page(json_file(filename=3)),
        json_page(json_file(url=...)),
        json_page(json_file(url=None)),
        json_page(json_file(hashes=...)),
        json_page(json_file(hashes=[WHEEL_SHA])),
        json_page(json_file(hashes={"sha256": "xyz"})),
        json_page(json_file(hashes={"sha256": "a" * 63})),
        json_page(json_file(hashes={"sha 256": WHEEL_SHA})),
        json_page(json_file(hashes={"sha256": 5})),
        json_page(json_file(**{"requires-python": 3})),
        json_page(json_file(**{"core-metadata": "true"})),
        json_page(json_file(**{"dist-info-metadata": {"sha256": "x"}})),
        json_page(json_file(**{"data-dist-info-metadata": None})),
        json_page(json_file(**{"gpg-sig": "false"})),
        json_page(json_file(yanked=1)),
        json_page(json_file(size=True)),
        json_page(json_file(size=-1)),
        json_page(json_file(size=1.5)),
        json_page(json_file(size="10")),
        json_page(json_file(**{"upload-time": "2024-11-06 22:37:09"})),
        json_page(json_file(**{"upload-time": "2024-11-06T22:37:09+00:00"})),
        json_page(json_file(**{"upload-time": 1730932629})),
        json_page(json_file(provenance="http://pypi.org/integrity/x")),
        json_page(json_file(provenance="/integrity/x")),
        json_page(json_file(provenance=True)),
    ],
)
def test_json_fails_on_malformed_pages(body: bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - every malformation has its own message
        rewrite_json(body)


@pytest.mark.parametrize(
    "content_type",
    ["text/html", HTML_TYPE, "application/json", "", f"{JSON_TYPE}; charset=latin-1"],
)
def test_json_page_fails_when_upstream_answers_in_another_format(content_type: str) -> None:
    body = json_page(json_file())
    with pytest.raises(ValueError, match=r"expected|charset"):
        rewrite_json(body, content_type=content_type)


@pytest.mark.parametrize(
    "content_type",
    [JSON_TYPE, "text/plain", "application/octet-stream", "text/html; charset=iso-8859-1"],
)
def test_html_page_fails_when_upstream_answers_in_another_format(content_type: str) -> None:
    body = html_page(anchor())
    with pytest.raises(ValueError, match=r"expected|charset"):
        rewrite_html(body, content_type=content_type)


@pytest.mark.parametrize(
    "content_type",
    [JSON_TYPE, f"{JSON_TYPE}; charset=utf-8", f'{JSON_TYPE};charset="UTF-8"', JSON_TYPE.upper()],
)
def test_json_content_types(content_type: str) -> None:
    assert rewrite_json(json_page(json_file()), content_type=content_type)["files"]


@pytest.mark.parametrize(
    "content_type", [HTML_TYPE, TEXT_HTML_TYPE, "text/html; charset=UTF-8", "TEXT/HTML"]
)
def test_html_content_types(content_type: str) -> None:
    assert b"<a href=" in rewrite_html(html_page(anchor()), content_type=content_type)


def test_html_link_points_at_the_cache() -> None:
    page = Page(rewrite_html(html_page(anchor())).decode())
    [(attrs, text)] = page.anchors
    assert attrs == {"href": f"{CACHE_FILES}/{SAMPLE}/{WHEEL_SHA}/{WHEEL_PATH}#sha256={WHEEL_SHA}"}
    assert text == "sampleproject-4.0.0-py3-none-any.whl"


def test_html_without_a_sha256_fragment_uses_a_dash() -> None:
    for href in (WHEEL_URL, f"{WHEEL_URL}#md5={'0' * 32}"):
        page = Page(rewrite_html(html_page(anchor(href=href))).decode())
        [(attrs, _)] = page.anchors
        fragment = href.partition("#")[2]
        expected = f"{CACHE_FILES}/{SAMPLE}/-/{WHEEL_PATH}"
        assert attrs["href"] == (f"{expected}#{fragment}" if fragment else expected)


def test_html_keeps_known_attributes_and_escapes_them() -> None:
    attributes = (
        ' data-requires-python="&gt;=3.9,&lt;4"'
        ' data-core-metadata="sha256=' + "b" * 64 + '"'
        ' data-dist-info-metadata="true"'
        ' data-gpg-sig="false"'
        ' data-yanked="bad &quot;release&quot; &amp; na&#xEF;ve <x>"'
        ' data-provenance="https://pypi.org/integrity/sampleproject/4.0.0/x/provenance"'
        ' data-mirror="https://evil.example/x.whl" class="x" onclick="alert(1)"'
    )
    output = rewrite_html(html_page(anchor(attributes=attributes)))
    output.decode("ascii")
    [(attrs, _)] = Page(output.decode()).anchors
    assert attrs == {
        "href": f"{CACHE_FILES}/{SAMPLE}/{WHEEL_SHA}/{WHEEL_PATH}#sha256={WHEEL_SHA}",
        "data-requires-python": ">=3.9,<4",
        "data-core-metadata": "sha256=" + "b" * 64,
        "data-dist-info-metadata": "true",
        "data-gpg-sig": "false",
        "data-yanked": 'bad "release" & na\N{LATIN SMALL LETTER I WITH DIAERESIS}ve <x>',
        "data-provenance": "https://pypi.org/integrity/sampleproject/4.0.0/x/provenance",
    }
    assert b"evil.example" not in output
    assert b"onclick" not in output


def test_html_bare_yanked_attribute_stays_yanked() -> None:
    [(attrs, _)] = Page(rewrite_html(html_page(anchor(attributes=" data-yanked"))).decode()).anchors
    assert attrs["data-yanked"] == ""


def test_html_honors_the_first_base_element() -> None:
    directory = WHEEL_URL.rpartition("/")[0]
    head = (
        f'<base href=" {directory}/ "><base href="https://evil.example/">'
        '<meta name="pypi:repository-version" content="1.1">'
    )
    body = html_page(anchor(href=f"sampleproject-4.0.0-py3-none-any.whl#sha256={WHEEL_SHA}"), head)
    page = Page(rewrite_html(body).decode())
    [(attrs, _)] = page.anchors
    assert attrs["href"] == f"{CACHE_FILES}/{SAMPLE}/{WHEEL_SHA}/{WHEEL_PATH}#sha256={WHEEL_SHA}"
    assert page.metas == {"pypi:repository-version": "1.1"}


def test_html_reads_only_anchors_metas_and_base() -> None:
    body = html_page(
        "<p>Links</p><a name=top>no href</a><!-- "
        + anchor(href="https://evil.example/x.whl")
        + " --><script>document.write('"
        + anchor(href="https://evil.example/y.whl")
        + "')</script>"
        + anchor(href=f" {WHEEL_URL}#sha256={WHEEL_SHA}\n"),
        head=(
            '<meta charset="utf-8"><meta name="pypi:repository-version" content="1.7">'
            '<meta name="pypi:project-status" content="quarantined">'
            '<meta name="pypi:project-status-reason" content="Suspected &lt;malware&gt;">'
            '<meta name="pypi:tracks" content="https://evil.example/simple/x/">'
        ),
    )
    output = rewrite_html(body)
    page = Page(output.decode())
    assert [text for _, text in page.anchors] == ["sampleproject-4.0.0-py3-none-any.whl"]
    assert page.metas == {
        "pypi:repository-version": "1.4",
        "pypi:project-status": "quarantined",
        "pypi:project-status-reason": "Suspected <malware>",
    }
    assert b"evil.example" not in output


def test_html_fails_when_a_link_inherits_the_base_query() -> None:
    body = html_page(anchor(href=f"#sha256={WHEEL_SHA}"), head=f'<base href="{WHEEL_URL}?token=x">')
    with pytest.raises(ValueError, match="not on the files host"):
        rewrite_html(body)


def test_html_without_a_version_meta_has_none() -> None:
    page = Page(rewrite_html(html_page(anchor(), head="")).decode())
    assert page.metas == {}
    assert len(page.anchors) == 1


def test_html_leaves_out_files_the_cache_would_refuse() -> None:
    anchors = anchor() + "".join(
        f'<a href="{FILES}/packages/{BLAKE}/{name}">{name}</a>'
        for name in (
            "urllib3-2.2.3-py3-none-any.whl",
            "sampleproject-1.0-py2.7.egg",
            "sampleproject-1.0.win32-py2.7.exe",
            "sampleproject%201.0.tar.gz",
        )
    )
    page = Page(rewrite_html(html_page(anchors)).decode())
    assert [text for _, text in page.anchors] == ["sampleproject-4.0.0-py3-none-any.whl"]


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xfe<html>",
        html_page(anchor(attributes=' href="https://evil.example/x.whl"')),
        html_page(anchor(attributes=' data-yanked="" data-yanked="x"')),
        html_page(anchor(), head='<meta name="pypi:repository-version" content="1.4">' * 2),
        html_page(anchor(), head='<meta name="pypi:repository-version" content="2.0">'),
        html_page(anchor(), head='<meta name="pypi:repository-version" content="latest">'),
        html_page(anchor(), head='<meta name="pypi:repository-version">'),
        html_page(anchor(), head='<meta name="pypi:project-status" content="Gone!">'),
        html_page(anchor(attributes=' data-core-metadata="false"')),
        html_page(anchor(attributes=' data-core-metadata="sha256"')),
        html_page(anchor(attributes=' data-core-metadata="sha256=xyz"')),
        html_page(anchor(attributes=" data-dist-info-metadata")),
        html_page(anchor(attributes=' data-gpg-sig="yes"')),
        html_page(anchor(attributes=' data-provenance="http://pypi.org/integrity/x"')),
        html_page(anchor(attributes=' data-provenance="/integrity/x"')),
        html_page('<a href="#top">top</a>' + anchor()),
    ],
)
def test_html_fails_on_malformed_pages(body: bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - every malformation has its own message
        rewrite_html(body)


def test_rewritten_links_use_the_configured_public_url() -> None:
    adapter = make_adapter(public_url="https://cache.example:8443/mirror")
    page = rewrite_json(json_page(json_file()), adapter=adapter)
    link = page["files"][0]["url"]
    assert link == f"https://cache.example:8443/mirror/pypi/files/{SAMPLE}/{WHEEL_SHA}/{WHEEL_PATH}"
