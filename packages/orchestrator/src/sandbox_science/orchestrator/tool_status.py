# SPDX-License-Identifier: MIT
"""Compact tool status shared by live events and archived record previews."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

from pydantic import JsonValue

GLOSS_FILES: Final = 10
"""The most files a gloss names; it counts the rest."""
GLOSS_ERROR_CHARS: Final = 200
"""The longest first line of an error a gloss keeps."""
GLOSS_PATH_CHARS: Final = 300
"""The longest path a gloss keeps whole; a longer one keeps its head and tail."""
REFUSED: Final = frozenset(
    {"invalid_request", "unsupported", "unavailable", "policy_denied", "not_found", "conflict"}
)
"""Error kinds of a call that did not run: a definite refusal before execution."""


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


def tool_gloss(record: Mapping[str, Any], result: str) -> dict[str, JsonValue]:
    """A tool result at a glance, computed from its record by code and never from the
    model's text (B10, item 1): the outcome word, the exit code, the output's size, the
    files the call recorded or published, and for a failure its error's first line.

    ``result`` is the tool message the model was given, the size's fallback when the
    record has no output streams, and where a refusal's message is.
    """
    outcome = tool_outcome(record)
    state, kind, code = outcome["state"], outcome["error"], outcome["exit_code"]
    word = _word(state, kind, code)
    lines, size = _size(record, result)
    files, total = _files(record)
    gloss: dict[str, JsonValue] = {
        "outcome": word,
        "exit_code": code if isinstance(code, int) and not isinstance(code, bool) else None,
        "lines": lines,
        "bytes": size,
        "files": cast(list[JsonValue], files),
        "more_files": max(0, total - len(files)),
        "error": _error(record, result) if word not in ("ok", "started") else None,
    }
    return gloss


def _word(state: JsonValue, kind: JsonValue, code: JsonValue) -> str:
    """The outcome in one word, as ``tool_outcome``'s state, error kind and exit code
    tell them apart."""
    if kind == "timed_out" or state == "timed_out":
        return "timed out"
    if kind == "cancelled" or state == "cancelled":
        return "cancelled"
    if state == "refused" or kind in REFUSED:
        return "refused"
    if kind is not None or state in ("failed", "lost"):
        return "failed"
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return "failed"
    if state in ("queued", "running", "cancelling"):
        return "started"  # a background job, which goes on after its call
    return "ok"


def _size(record: Mapping[str, Any], result: str) -> tuple[int | None, int]:
    """Lines and bytes of the call's output: its streams, which are what its saved output
    files hold, when the record has them; else the result's text. Lines are unknown
    (``None``) for a stream the worker cut, and for a JSON result without one text."""
    found = _mapping(_mapping(record.get("execution")).get("result"))
    streams = [_mapping(found.get(name)) for name in ("stdout", "stderr")]
    streams = [stream for stream in streams if isinstance(stream.get("text"), str)]
    if streams:
        size = sum(_count(stream.get("total_bytes")) for stream in streams)
        if any(stream.get("truncated") for stream in streams):
            return None, size
        return sum(_lines(str(stream["text"])) for stream in streams), size
    size = len(result.encode("utf-8", "replace"))
    try:
        parsed: object = json.loads(result)
    except (ValueError, RecursionError):
        return _lines(result), size
    # A file read's or a record read's text; any other JSON result has bytes only.
    content = _mapping(parsed).get("content")
    return (_lines(content) if isinstance(content, str) else None), size


def _files(record: Mapping[str, Any]) -> tuple[list[str], int]:
    """The paths of the files the call recorded or published, the first ``GLOSS_FILES``
    of them, and how many there are."""
    paths: list[str] = []
    recorded = _mapping(record.get("recorded_files"))
    listed = recorded.get("files")
    for entry in cast(Sequence[object], listed) if isinstance(listed, list) else ():
        if isinstance(path := _mapping(entry).get("path"), str):
            paths.append(path)
    counted = sum(_count(recorded.get(key)) for key in ("recorded", "deleted"))
    artifact = _mapping(record.get("artifact"))
    if isinstance(path := artifact.get("source_path"), str) and path not in paths:
        paths.append(path)
        counted += 1
    unique = list(dict.fromkeys(paths))
    return [_cut(path) for path in unique[:GLOSS_FILES]], max(counted, len(unique))


def _error(record: Mapping[str, Any], result: str) -> str | None:
    """A failed call's error, its first line: the worker's error message, a cell's
    exception, or the first line of standard error (for a Python traceback, its last)."""
    error = record.get("error")
    message = _mapping(error).get("message")
    found = _mapping(_mapping(record.get("execution")).get("result"))
    info = _mapping(found.get("error"))
    if not isinstance(message, str) and info:
        name, value = info.get("name"), info.get("value")
        message = f"{name}: {value}" if name and value is not None else info.get("message")
    if not isinstance(message, str):
        stderr = _mapping(found.get("stderr")).get("text")
        if isinstance(stderr, str) and stderr.strip():
            lines = [line for line in stderr.splitlines() if line.strip()]
            traceback = lines[0].startswith("Traceback (most recent call last)")
            message = lines[-1] if traceback else lines[0]
    if not isinstance(message, str):
        try:
            parsed: object = json.loads(result)
        except (ValueError, RecursionError):
            parsed = None
        said = _mapping(_mapping(parsed).get("error")).get("message")
        message = said if isinstance(said, str) else None
    if message is None:
        return None
    line = next((part.strip() for part in message.splitlines() if part.strip()), "")
    return (line[: GLOSS_ERROR_CHARS - 1] + "…" if len(line) > GLOSS_ERROR_CHARS else line) or None


def _lines(text: str) -> int:
    return len(text.splitlines())


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _cut(path: str) -> str:
    if len(path) <= GLOSS_PATH_CHARS:
        return path
    half = GLOSS_PATH_CHARS // 2
    return f"{path[:half]}…{path[-half:]}"


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, dict) else {}


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
