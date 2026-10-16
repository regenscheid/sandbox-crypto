# SPDX-License-Identifier: MIT
"""An execution as review evidence: a bounded copy of what the worker recorded, a run's own
or a delegated run's (docs/specs/consistency-review.md, CR-2)."""

from __future__ import annotations

from typing import cast

from .store import Json, clipped


def execution_projection(record: Json) -> tuple[Json, bool]:
    """Copy bounded worker observations; mutable paths remain references, never reads."""
    result = cast(Json, record.get("result") or {})
    payload: Json = {
        k: record.get(k)
        for k in (
            "execution_id",
            "attempt_id",
            "context",
            "kind",
            "state",
            "submitted_at",
            "started_at",
            "ended_at",
            "location",
        )
    }
    request = cast(Json, record.get("request") or {})
    payload["request"] = {
        k: clipped(str(v), 4096)
        for k, v in request.items()
        if k in ("script", "code", "argv", "cwd", "language", "path")
    }
    complete = True
    for stream in ("stdout", "stderr"):
        output = cast(Json, result.get(stream) or {})
        original = str(output.get("text", ""))
        payload[stream] = clipped(original, 8192)
        payload[stream + "_truncated"] = bool(output.get("truncated")) or len(original) > 8192
        payload[stream + "_line_numbers_available"] = not payload[stream + "_truncated"]
        complete = complete and not payload[stream + "_truncated"]
    payload.update(
        exit_code=result.get("exit_code"),
        error=result.get("error"),
        elapsed_seconds=result.get("elapsed_seconds"),
        written_files=[clipped(str(p), 256) for p in result.get("written_files", [])[:100]],
        written_files_truncated=bool(result.get("written_files_truncated"))
        or len(result.get("written_files", [])) > 100,
        outputs_note="Rich output and shell artifact inventory are not copied by this slice.",
    )
    if payload["error"]:
        payload["error"] = {
            k: clipped(str(v), 1024)
            for k, v in payload["error"].items()
            if k in ("kind", "message")
        }
    payload["omitted_rich_outputs"] = len(result.get("outputs", []))
    payload["canonical_execution"] = record.get("execution_id")
    complete = complete and not (
        payload["omitted_rich_outputs"]
        or result.get("written_files")
        or payload["written_files_truncated"]
    )
    return payload, complete
