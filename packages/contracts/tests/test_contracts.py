# SPDX-License-Identifier: MIT
"""Validation of the shared contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from sandbox_science.contracts import (
    CellRequest,
    Event,
    Execution,
    ExecutionContext,
    ExecutionRequest,
    ExecutionState,
    PdfRequest,
    ReadFileRequest,
    ShellRequest,
    WriteFileRequest,
    new_id,
    validate_relative_path,
)

CONTEXT = ExecutionContext(project_id="p1", session_id="s1", environment_id="env1")
REQUEST = TypeAdapter[ExecutionRequest](ExecutionRequest)
EVENT = TypeAdapter[Event](Event)


def test_shell_requests_need_exactly_one_command() -> None:
    with pytest.raises(ValidationError, match="exactly one of script and argv"):
        ShellRequest(operation_id="op", context=CONTEXT)
    with pytest.raises(ValidationError, match="exactly one of script and argv"):
        ShellRequest(operation_id="op", context=CONTEXT, script="ls", argv=("ls",))
    with pytest.raises(ValidationError, match="must name a program"):
        ShellRequest(operation_id="op", context=CONTEXT, argv=())
    with pytest.raises(ValidationError, match="must not be empty"):
        ShellRequest(operation_id="op", context=CONTEXT, script="   ")
    with pytest.raises(ValidationError, match="NUL"):
        ShellRequest(operation_id="op", context=CONTEXT, argv=("echo", "a\x00b"))


def test_requests_are_told_apart_by_kind() -> None:
    shell = REQUEST.validate_python(
        {"kind": "shell", "operation_id": "op", "context": CONTEXT, "script": "ls"}
    )
    cell = REQUEST.validate_python(
        {"kind": "cell", "operation_id": "op", "context": CONTEXT, "code": "1"}
    )
    assert isinstance(shell, ShellRequest)
    assert isinstance(cell, CellRequest)
    with pytest.raises(ValidationError):
        REQUEST.validate_python(
            {
                "kind": "cell",
                "operation_id": "op",
                "context": CONTEXT,
                "code": "1",
                "language": "fortran",
            }
        )


@pytest.mark.parametrize("value", [".", "a", "a/b.txt", "shared", "shared/x", "..a", "a..b"])
def test_workspace_paths(value: str) -> None:
    assert validate_relative_path(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "/etc/passwd", "../x", "a/../b", "a//b", "a/./b", "a/", "./a", "a\x00b", "x" * 5000],
)
def test_workspace_paths_refused(value: str) -> None:
    with pytest.raises(ValueError, match="path"):
        validate_relative_path(value)


def test_limits_are_bounded() -> None:
    with pytest.raises(ValidationError):
        ShellRequest(operation_id="op", context=CONTEXT, script="ls", timeout_seconds=0)
    with pytest.raises(ValidationError):
        ShellRequest(operation_id="op", context=CONTEXT, script="ls", timeout_seconds=float("inf"))
    with pytest.raises(ValidationError):
        ShellRequest(operation_id="op", context=CONTEXT, script="ls", output_limit_bytes=10)
    with pytest.raises(ValidationError):
        ReadFileRequest(context=CONTEXT, path="a", length=64 * 1024 * 1024)


def test_identifiers_are_path_safe() -> None:
    for bad in ("", "a/b", ".hidden", "-x", "a b", "x" * 200):
        with pytest.raises(ValidationError):
            ExecutionContext(project_id=bad, session_id="s", environment_id="e")
    assert new_id("exe").startswith("exe-")
    assert len(new_id("exe")) == len("exe-") + 24


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        WriteFileRequest.model_validate(
            {"context": CONTEXT, "path": "a", "content_b64": "", "mode": "0777"}
        )


def test_events_round_trip() -> None:
    execution = Execution(
        execution_id="exe-1",
        attempt_id="att-1",
        operation_id="op",
        kind="shell",
        context=CONTEXT,
        state=ExecutionState.SUCCEEDED,
        submitted_at=datetime.now(UTC),
    )
    for event in (
        {
            "type": "state",
            "execution_id": "exe-1",
            "seq": 1,
            "time": "2026-10-11T00:00:00Z",
            "state": "running",
        },
        {
            "type": "stream",
            "execution_id": "exe-1",
            "seq": 2,
            "time": "2026-10-11T00:00:00Z",
            "name": "stdout",
            "text": "hi",
        },
        {
            "type": "result",
            "execution_id": "exe-1",
            "seq": 3,
            "time": "2026-10-11T00:00:00Z",
            "execution": execution.model_dump(mode="json"),
        },
    ):
        parsed = EVENT.validate_python(event)
        assert EVENT.validate_json(EVENT.dump_json(parsed)) == parsed


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"operation": "pages"}, "pages needs pages"),
        ({"operation": "search"}, "search needs query"),
        ({"operation": "render"}, "render needs page"),
        ({"operation": "pages", "pages": "1-3,x"}, "pattern"),
        ({"operation": "render", "page": 1, "region": [5, 5, 1, 1]}, "x0 < x1"),
        ({"operation": "render", "page": 1, "region": [0, 0, float("inf"), 1]}, "finite"),
        ({"operation": "render", "page": 1, "dpi": 1000}, "less than or equal"),
        ({"operation": "info", "path": "/etc/passwd"}, "relative"),
    ],
)
def test_pdf_requests_need_their_arguments(fields: dict[str, Any], message: str) -> None:
    request = {"operation_id": "op", "context": CONTEXT, "path": "paper.pdf", **fields}
    with pytest.raises(ValidationError, match=message):
        PdfRequest.model_validate(request)


def test_pdf_requests_are_execution_requests() -> None:
    request = TypeAdapter(ExecutionRequest).validate_python(
        {
            "kind": "pdf",
            "operation_id": "op",
            "context": CONTEXT,
            "operation": "pages",
            "path": "shared/paper.pdf",
            "pages": "3-5,9",
        }
    )
    assert isinstance(request, PdfRequest)
    assert (request.dpi, request.include_artifacts) == (150, False)
