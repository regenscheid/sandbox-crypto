# SPDX-License-Identifier: MIT
"""The adapter contract's own checks: digests, fetches and base URLs."""

import math

import pytest

from sandbox_science.package_cache.adapter import (
    GZIP_MAGIC,
    MAX_MAGIC,
    Digest,
    Fetch,
    Kind,
    check_base_url,
)

URL = "https://registry.test/file"


@pytest.mark.parametrize(("algorithm", "length"), [("md5", 32), ("sha256", 64), ("sha512", 128)])
def test_digests_have_their_algorithms_length(algorithm: str, length: int) -> None:
    Digest(algorithm, "0" * length)  # pyright: ignore[reportArgumentType]
    for wrong in (length - 1, length + 1):
        with pytest.raises(ValueError, match="invalid"):
            Digest(algorithm, "0" * wrong)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="invalid"):
        Digest(algorithm, "A" * length)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("max_age", [0, 0.5, 60])
def test_mutable_index_data_may_have_a_shorter_lifetime(max_age: float) -> None:
    assert Fetch(key="k", url=URL, kind=Kind.METADATA, max_age=max_age).max_age == max_age


@pytest.mark.parametrize("max_age", [-1, -0.001, math.inf, math.nan])
def test_lifetimes_are_finite_and_not_negative(max_age: float) -> None:
    with pytest.raises(ValueError, match="max_age must be"):
        Fetch(key="k", url=URL, kind=Kind.METADATA, max_age=max_age)


@pytest.mark.parametrize(
    "options",
    [
        {"kind": Kind.ARTIFACT},
        {"kind": Kind.METADATA, "immutable": True},
    ],
)
def test_only_mutable_index_data_has_a_lifetime(options: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="mutable index data"):
        Fetch(key="k", url=URL, max_age=0, **options)  # pyright: ignore[reportArgumentType]


def test_upstreams_use_https_and_only_index_data_is_rewritten() -> None:
    with pytest.raises(ValueError, match="https"):
        Fetch(key="k", url="http://registry.test/file", kind=Kind.METADATA)
    with pytest.raises(ValueError, match="rewritten"):
        Fetch(key="k", url=URL, kind=Kind.ARTIFACT, rewrite=lambda body, _: body)


def test_only_index_data_that_is_not_rewritten_is_stored_byte_for_byte() -> None:
    assert Fetch(key="k", url=URL, kind=Kind.METADATA, exact=True).exact
    with pytest.raises(ValueError, match="exact applies"):
        Fetch(key="k", url=URL, kind=Kind.ARTIFACT, exact=True)
    with pytest.raises(ValueError, match="exact applies"):
        Fetch(key="k", url=URL, kind=Kind.METADATA, exact=True, rewrite=lambda body, _: body)


def test_magic_numbers_are_short() -> None:
    assert Fetch(key="k", url=URL, kind=Kind.ARTIFACT, magic=GZIP_MAGIC).magic == b"\x1f\x8b"
    Fetch(key="k", url=URL, kind=Kind.ARTIFACT, magic=b"m" * MAX_MAGIC)
    with pytest.raises(ValueError, match="magic"):
        Fetch(key="k", url=URL, kind=Kind.ARTIFACT, magic=b"m" * (MAX_MAGIC + 1))


def test_only_artifacts_without_a_digest_are_revalidated() -> None:
    fetch = Fetch(key="k", url=URL, kind=Kind.ARTIFACT, immutable=True, revalidate_after=86400)
    assert fetch.revalidate_after == 86400
    with pytest.raises(ValueError, match="without a digest"):
        Fetch(key="k", url=URL, kind=Kind.METADATA, revalidate_after=86400)
    with pytest.raises(ValueError, match="without a digest"):
        Fetch(
            key="k",
            url=URL,
            kind=Kind.ARTIFACT,
            digest=Digest("sha256", "0" * 64),
            revalidate_after=86400,
        )
    for interval in (0, -1, math.inf, math.nan):
        with pytest.raises(ValueError, match="positive"):
            Fetch(key="k", url=URL, kind=Kind.ARTIFACT, revalidate_after=interval)


@pytest.mark.parametrize(
    "url",
    [
        "https://opam.ocaml.org",
        "https://opam.ocaml.org:8443/mirror",
        "https://[2001:db8::1]",
        "https://h.test/~user/repo_1.2-x",
    ],
)
def test_base_urls_are_https_without_anything_to_resolve(url: str) -> None:
    assert check_base_url(url, "a repository") == url


@pytest.mark.parametrize(
    "url",
    [
        "http://opam.ocaml.org",
        "https://opam.ocaml.org/",
        "https://opam.ocaml.org/a%2Fb",
        "https://opam.ocaml.org?x",
        "https://opam.ocaml.org#x",
        "https://user@opam.ocaml.org",
        "https://opam.ocaml.org/a b",
        "https://opam.ocaml.org/é",
        "opam.ocaml.org",
        "",
    ],
)
def test_other_base_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="a repository must be an absolute https URL"):
        check_base_url(url, "a repository")
