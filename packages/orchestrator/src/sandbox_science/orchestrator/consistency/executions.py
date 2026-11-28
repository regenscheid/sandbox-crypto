# SPDX-License-Identifier: MIT
"""An execution as review evidence: a bounded copy of what the worker recorded, a run's own
or a delegated run's (docs/specs/consistency-review.md, CR-2)."""

from __future__ import annotations

from typing import cast

from sandbox_science.contracts.context import other_sessions_upload

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
    context = cast(Json, record.get("context") or {})
    session = str(context.get("session_id", ""))
    # Another session's upload is never this execution's, whatever a worker reported.
    written = [
        str(p)
        for p in cast(list[object], result.get("written_files") or [])
        if not other_sessions_upload(session, str(p))
    ]
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
        written_files=[clipped(p, 256) for p in written[:100]],
        written_files_truncated=bool(result.get("written_files_truncated")) or len(written) > 100,
        outputs_note="Rich output and shell artifact inventory are not copied by this slice.",
    )
    if payload["error"]:
        payload["error"] = {
            k: clipped(str(v), 1024)
            for k, v in payload["error"].items()
            if k in ("kind", "message")
        }
    job = cast(Json, record.get("job") or {})
    if job.get("attempts"):
        # A restarted job is one execution: its earlier attempts were lost, and its result
        # and written files are its last attempt's own (job restarts).
        payload["attempt"] = job.get("attempt")
        payload["lost_attempts"] = [
            {
                "attempt": attempt.get("attempt"),
                "attempt_id": attempt.get("attempt_id"),
                "started_at": attempt.get("started_at"),
                "lost_at": attempt.get("lost_at"),
                "reason": clipped(str(attempt.get("reason", "")), 512),
                # What it wrote that the next attempt found, by its digest then.
                "wrote": [
                    {"path": clipped(str(file.get("path", "")), 256), "sha256": file.get("sha256")}
                    for file in cast(list[Json], attempt.get("survived") or [])[:20]
                    if not other_sessions_upload(session, str(file.get("path", "")))
                ],
                "wrote_known": attempt.get("survived") is not None,
                "not_attributed": len(
                    cast(list[object], attempt.get("survived_unattributed") or [])
                ),
            }
            for attempt in cast(list[Json], job.get("attempts"))[:10]
        ]
    payload["omitted_rich_outputs"] = len(result.get("outputs", []))
    payload["canonical_execution"] = record.get("execution_id")
    complete = complete and not (
        payload["omitted_rich_outputs"] or written or payload["written_files_truncated"]
    )
    return payload, complete
