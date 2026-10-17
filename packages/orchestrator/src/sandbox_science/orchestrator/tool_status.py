# SPDX-License-Identifier: MIT
"""Compact tool status shared by live events and archived record previews."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import JsonValue


def tool_outcome(record: Mapping[str, Any]) -> dict[str, JsonValue]:
    """A tool call's state, error kind and exit code, as far as its record has them; for
    the ``skills`` tool, what it found, loaded or read (lab 42)."""
    stored = record.get("_record_storage")
    metadata: Mapping[str, object] = (
        cast(Mapping[str, object], stored) if isinstance(stored, dict) else {}
    )
    if metadata.get("format") == "tool-record-v1":
        outcome = metadata.get("outcome")
        if isinstance(outcome, dict):
            summary = cast(dict[str, object], outcome)
            if set(summary) == {"state", "error", "exit_code"}:
                return {key: scalar(value) for key, value in summary.items()}
    if isinstance(skills := record.get("skills"), dict):
        return _skills_outcome(cast(Mapping[str, Any], skills))
    execution = record.get("execution")
    found = cast(Mapping[str, Any], execution) if isinstance(execution, dict) else record
    error = record.get("error")
    kind = cast(Mapping[str, Any], error).get("kind") if isinstance(error, dict) else error
    result = found.get("result")
    code = cast(Mapping[str, Any], result).get("exit_code") if isinstance(result, dict) else None
    return {
        "state": scalar(found.get("state")),
        "error": scalar(kind),
        "exit_code": scalar(code),
    }


def _skills_outcome(record: Mapping[str, Any]) -> dict[str, JsonValue]:
    """A ``skills`` call in a few words: the skills found, loaded or refused, or the file
    read, or what refused the call."""
    status = str(record.get("status", ""))
    action = record.get("action")
    if action == "search" and status == "ok":
        found = cast(list[object], record.get("found") or [])
        said = f"found {len(found)}" + (f": {', '.join(map(str, found))}" if found else "")
        if record.get("scoring") in ("lexical", "partial") and "embedding" in record:
            said += " (by words alone)" if record["scoring"] == "lexical" else " (partly by words)"
    elif action == "load" and isinstance(loads := record.get("loads"), list):
        by: dict[str, list[str]] = {}
        for load in cast(list[Mapping[str, Any]], loads):
            name = str(load.get("id") or load.get("name") or "?")
            by.setdefault(str(load.get("status", "")).replace("_", " "), []).append(name)
        said = "; ".join(f"{what} {', '.join(names)}" for what, names in by.items())
    elif action == "read" and status == "ok":
        said = f"read {record.get('path', '?')}" + (" (binary)" if record.get("binary") else "")
    else:
        return {"state": "refused", "error": scalar(status or None), "exit_code": None}
    return {"state": said[:300], "error": None, "exit_code": None}


def scalar(value: object) -> JsonValue:
    return value if isinstance(value, str | int | float | bool) or value is None else str(value)
