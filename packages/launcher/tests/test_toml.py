# SPDX-License-Identifier: MIT
"""What the launcher writes as TOML reads back as the same values."""

from __future__ import annotations

import tomllib
from typing import Any

import pytest

from sandbox_science.launcher import toml


def test_values_tables_and_arrays_of_tables_read_back() -> None:
    table: dict[str, Any] = {
        "listen": "127.0.0.1:8100",
        "text": 'quotes " and \\ backslash, a\nnewline, a tab\t, del \x7f, é and 🙂',
        "flag": True,
        "count": 3,
        "ratio": 0.5,
        "names": ["a", "b"],
        "sessions": ["a", {"id": "c", "dedicated": True}],
        "empty": [],
        "dotted.key": "kept",
        "openshell": {
            "cli": "/opt/openshell",
            "env": {"XDG_CONFIG_HOME": "/home/u/config"},
            "environments": {"profiles": {"crypto-base": "image:1"}, "ceiling": "/c.yaml"},
        },
        "clients": [
            {"name": "harness", "projects": ["p1"]},
            {"name": "workspace", "projects": ["p1", "p2"]},
        ],
    }
    assert tomllib.loads(toml.dumps(table)) == table


def test_none_is_left_out_and_other_values_are_refused() -> None:
    assert tomllib.loads(toml.dumps({"a": 1, "b": None})) == {"a": 1}
    for value in (float("nan"), object(), {1, 2}, [{"x": object()}]):
        with pytest.raises(TypeError):
            toml.dumps({"a": value})
