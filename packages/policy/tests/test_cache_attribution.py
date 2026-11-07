# SPDX-License-Identifier: MIT
"""The package cache's sandbox attribution: what the middleware signs and the cache checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_science.policy.cache_attribution import (
    ATTRIBUTION_WINDOW,
    MIN_KEY_BYTES,
    AttributionKeyError,
    canonical_target,
    read_key,
    sign,
    verify,
)

KEY = b"a-made-up-attribution-key-for-tests-only"
NOW = 1_800_000_000.0


def test_an_attribution_verifies_only_for_its_own_request_and_key() -> None:
    value = sign(KEY, "sbx-0001", "GET", "/pypi/simple/numpy/", NOW)
    assert value.startswith(f"v1:{int(NOW)}:")
    assert verify(KEY, value, "sbx-0001", "GET", "/pypi/simple/numpy/", NOW)
    for sandbox, method, target in (
        ("sbx-0002", "GET", "/pypi/simple/numpy/"),
        ("sbx-0001", "HEAD", "/pypi/simple/numpy/"),
        ("sbx-0001", "GET", "/pypi/simple/scipy/"),
        ("sbx-0001", "GET", "/pypi/simple/numpy/?x=1"),
    ):
        assert not verify(KEY, value, sandbox, method, target, NOW)
    other = b"another-made-up-key-of-enough-bytes"
    assert not verify(other, value, "sbx-0001", "GET", "/pypi/simple/numpy/", NOW)
    assert not verify(KEY, value.upper(), "sbx-0001", "GET", "/pypi/simple/numpy/", NOW)
    assert not verify(KEY, "", "sbx-0001", "GET", "/pypi/simple/numpy/", NOW)


def test_an_attribution_is_good_within_the_window_either_side() -> None:
    value = sign(KEY, "sbx-0001", "GET", "/npm/left-pad", NOW)
    for skew in (-ATTRIBUTION_WINDOW, 0, ATTRIBUTION_WINDOW):
        assert verify(KEY, value, "sbx-0001", "GET", "/npm/left-pad", NOW + skew)
    for skew in (-ATTRIBUTION_WINDOW - 1, ATTRIBUTION_WINDOW + 1):
        assert not verify(KEY, value, "sbx-0001", "GET", "/npm/left-pad", NOW + skew)


def test_both_sides_compare_a_path_in_openshells_canonical_form() -> None:
    # OpenShell reports URL-safe bytes unescaped and the rest with upper-case escapes; what
    # it forwards to the cache may keep the client's spelling.
    assert canonical_target("/npm/@scope%2fname") == "/npm/@scope%2Fname"
    assert canonical_target("/pypi/simple/%6Eumpy/") == "/pypi/simple/numpy/"
    assert canonical_target("/a%20b%2F%25") == "/a%20b%2F%25"
    value = sign(KEY, "sbx-0001", "GET", "/npm/@scope%2Fname", NOW)
    assert verify(KEY, value, "sbx-0001", "GET", "/npm/@scope%2fname", NOW)
    assert not verify(KEY, value, "sbx-0001", "GET", "/npm/@scope/name", NOW)


def test_the_key_is_read_from_a_private_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "attribution.key"
    path.write_bytes(KEY + b"\n")
    path.chmod(0o600)
    assert read_key(path) == KEY
    link = tmp_path / "link.key"
    link.symlink_to(path)
    with pytest.raises(AttributionKeyError, match="private regular file"):
        read_key(link)
    path.chmod(0o640)
    with pytest.raises(AttributionKeyError, match="private regular file"):
        read_key(path)
    path.chmod(0o600)
    path.write_bytes(b"k" * (MIN_KEY_BYTES - 1))
    with pytest.raises(AttributionKeyError, match="shorter than"):
        read_key(path)
    with pytest.raises(AttributionKeyError, match="cannot read"):
        read_key(tmp_path / "missing.key")
