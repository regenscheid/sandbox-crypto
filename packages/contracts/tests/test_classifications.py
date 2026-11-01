# SPDX-License-Identifier: MIT
"""The classification review's contracts: their shapes and bounds (CL-6)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sandbox_science.contracts.classifications import (
    ClassificationAcceptance,
    ClassificationsInForce,
    ClassifiedTool,
    DriftAcceptance,
)


def test_an_acceptance_names_its_operation_and_a_reason() -> None:
    accepted = ClassificationAcceptance.model_validate(
        {"project": "p1", "operation_id": "ws:accept.1", "yaml": "x", "reason": "ok"}
    )
    assert accepted.operation_id == "ws:accept.1"
    for bad in ({"operation_id": "-x"}, {"operation_id": "a b"}, {"reason": ""}):
        with pytest.raises(ValidationError):
            DriftAcceptance.model_validate(
                {"project": "p1", "operation_id": "op", "reason": "r", **bad}
            )
    with pytest.raises(ValidationError):
        DriftAcceptance.model_validate({"project": "p1", "reason": "r"})


def test_a_tool_has_a_closed_class_and_pin_state() -> None:
    tool = {"name": "t", "effect": "read", "pin_state": "current", "mentioned": True,
            "escalates": False}  # fmt: skip
    assert ClassifiedTool.model_validate(tool).rules == []
    for bad in ({"effect": "sometimes"}, {"pin_state": "gone"}, {"sha256": "abc"}):
        with pytest.raises(ValidationError):
            ClassifiedTool.model_validate({**tool, **bad})


def test_the_listing_is_a_json_array() -> None:
    assert ClassificationsInForce([]).model_dump_json() == "[]"
