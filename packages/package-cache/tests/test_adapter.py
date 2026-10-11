# SPDX-License-Identifier: MIT
"""The adapter contract's own checks: digests and fetches."""

import math

import pytest

from sandbox_science.package_cache.adapter import Digest, Fetch, Kind

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
