# SPDX-License-Identifier: MIT
"""Shared vocabulary: PEP 503 names and the reason codes of the library's decisions."""

from __future__ import annotations

import re
from typing import get_args

import pytest

from sandbox_science.policy import Layer, reasons
from sandbox_science.policy.names import pep503


@pytest.mark.parametrize(
    ("name", "normalized"),
    [
        ("Django", "django"),
        ("zope.interface", "zope-interface"),
        ("Typing_Extensions", "typing-extensions"),
        ("a--_..b", "a-b"),
        ("already-normal", "already-normal"),
    ],
)
def test_pep503(name: str, normalized: str) -> None:
    assert pep503(name) == normalized


def test_every_layer_has_a_reason_code_in_openshell_format() -> None:
    assert set(reasons.LAYER_CODES) == set(get_args(Layer))
    codes = [
        value for key, value in vars(reasons).items() if key.isupper() and isinstance(value, str)
    ]
    assert len(codes) == len(set(codes))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) for code in codes)
    assert set(reasons.LAYER_CODES.values()) <= set(codes)


@pytest.mark.parametrize("name", ["Django", "zope.interface", "A__b..C", "x-_.-y", "Ünïcode_Name"])
def test_the_contracts_copy_of_pep503_agrees(name: str) -> None:
    # Contracts cannot import the policy library, so they keep their own copy.
    from sandbox_science.contracts.revisions import pep503_name

    assert pep503_name(name) == pep503(name)
