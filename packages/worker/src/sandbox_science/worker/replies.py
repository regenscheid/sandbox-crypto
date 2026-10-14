# SPDX-License-Identifier: MIT
"""Values read from an executor's replies, checked and bounded: what an executor sends is
untrusted. A result that cannot be stored as reported is shrunk here too."""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from sandbox_science.contracts import ErrorInfo, ErrorKind, Result, RichOutput, StreamOutput

from .errors import WorkerError

MAX_ERROR_TEXT = 64 * 1024
MAX_TRACEBACK_LINES = 100
MAX_TRACEBACK_LINE = 8 * 1024


def integer(value: object, low: int = -(2**31), high: int = 2**31 - 1) -> int | None:
    """``value`` if it is an integer from ``low`` to ``high``; None otherwise."""
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return None


def epoch(value: object) -> datetime | None:
    """The time ``value`` seconds after the epoch, or None if it is not one."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(value, UTC)
    return None


@dataclass(frozen=True)
class Range:
    """A range of a file as the executor read it; ``sha256`` only when it was asked for."""

    size: int
    sha256: str | None
    modified: datetime | None
    offset: int
    data: bytes
    content_b64: str
    eof: bool
    stamp: str | None = None


def seconds(value: object) -> float:
    """A non-negative, finite number of seconds; 0 for anything else."""
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return max(0.0, float(value))
    return 0.0


def clipped(value: object, limit: int) -> str | None:
    """A string from the executor, keeping its head and tail if it is over ``limit``."""
    if not isinstance(value, str):
        return None
    if len(value) <= limit:
        return value
    half = limit // 2
    return f"{value[:half]}\n[… {len(value) - 2 * half} characters omitted …]\n{value[-half:]}"


def size(value: str) -> int:
    """UTF-8 bytes of a text, invalid characters counted as replaced."""
    return len(value.encode("utf-8", "replace"))


def checked[Model: BaseModel](model: type[Model], **fields: Any) -> Model:
    """A reply from the executor as a contract model; a malformed one is an internal error."""
    try:
        return model.model_validate(fields)
    except ValidationError as exc:
        raise WorkerError(
            ErrorKind.INTERNAL, f"the executor sent a malformed reply ({model.__name__})"
        ) from exc


def job_stream(stream: StreamOutput | None) -> StreamOutput | None:
    if stream is None:
        return None
    text = clipped(stream.text, 16384) or ""
    return stream.model_copy(
        update={"text": text, "truncated": stream.truncated or text != stream.text}
    )


def stream(raw: object, name: str, limit: int) -> StreamOutput | None:
    """A stream's summary; its text keeps its head and tail within three times the limit."""
    if not isinstance(raw, dict):
        return None
    raw = cast(dict[str, Any], raw)
    text = clipped(raw.get("text"), 3 * limit)
    total = integer(raw.get("total_bytes"), 0, 2**62)
    if text is None or total is None:
        return None
    truncated = raw.get("truncated") is True or text is not raw.get("text")
    return StreamOutput.model_validate(
        {"name": name, "text": text, "total_bytes": total, "truncated": truncated}
    )


def outputs(raw: object, limit: int) -> tuple[RichOutput, ...]:
    """Rich outputs up to the limit; a note stands for those beyond it."""
    outputs: list[RichOutput] = []
    spent, omitted = 0, 0
    for item in cast(list[object], raw) if isinstance(raw, list) else []:
        try:
            output = RichOutput.model_validate(item)
        except ValidationError:
            continue
        spent += sum(size(value) for value in output.data.values())
        if spent > limit + 65536:
            omitted += 1
            continue
        outputs.append(output)
    if omitted:
        note = f"[{omitted} outputs omitted: over the output limit]"
        outputs.append(RichOutput(type="display", data={"text/plain": note}))
    return tuple(outputs)


def error(raw: object) -> ErrorInfo | None:
    if not isinstance(raw, dict):
        return None
    raw = cast(dict[str, Any], raw)
    lines = raw.get("traceback")
    traceback = cast(list[object], lines)[-MAX_TRACEBACK_LINES:] if isinstance(lines, list) else []
    return ErrorInfo.model_validate(
        {
            "kind": raw.get("kind"),
            "message": clipped(raw.get("message"), MAX_ERROR_TEXT) or "",
            "name": clipped(raw.get("name"), 256),
            "value": clipped(raw.get("value"), MAX_ERROR_TEXT),
            "traceback": [clipped(line, MAX_TRACEBACK_LINE) or "" for line in traceback],
        }
    )


def shrunk(result: Result) -> Result:
    """The result without its bulk, for when it is too large to store as reported."""
    note = "[omitted: the result was too large to store]"

    def noted(output: StreamOutput | None) -> StreamOutput | None:
        return (
            None if output is None else output.model_copy(update={"text": note, "truncated": True})
        )

    error = result.error
    if error is not None:
        error = error.model_copy(
            update={"message": error.message[:1000], "value": None, "traceback": ()}
        )
    return result.model_copy(
        update={
            "stdout": noted(result.stdout),
            "stderr": noted(result.stderr),
            "outputs": (RichOutput(type="display", data={"text/plain": note}),)
            if result.outputs
            else (),
            "error": error,
            "written_files": (),
            "written_files_truncated": bool(result.written_files),
        }
    )
