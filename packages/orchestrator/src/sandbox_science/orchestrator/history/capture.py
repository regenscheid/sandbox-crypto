# SPDX-License-Identifier: MIT
"""Capture the actual adapter-input conversation, never reconstructed provider state.

Prompt pins and view labels are observations from the harness. They do not grant
access or become model text. CAS bodies are shared across dispatches within a project.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from pydantic import Field, model_validator

from sandbox_science.contracts import Contract
from sandbox_science.orchestrator.model import AssistantMessage, Message, ToolSpec
from sandbox_science.orchestrator.prompts import PromptPins, schema_json

MAX_CAPTURE_BYTES = 8 * 1024 * 1024

_PINS: ContextVar[PromptPins | None] = ContextVar("capture_prompt_pins", default=None)


class Origin(Contract):
    kind: str = Field(max_length=64)
    message: str | None = Field(default=None, max_length=128)
    position: int | None = Field(default=None, ge=0)


class ContextMessage(dict[str, object]):
    """An ordinary visible message with harness provenance outside its JSON keys."""

    def __init__(self, message: Message, origin: Origin) -> None:
        super().__init__(message)
        self.origin = origin


@contextmanager
def prompt_pins(pins: PromptPins | None) -> Generator[None]:
    token = _PINS.set(pins)
    try:
        yield
    finally:
        _PINS.reset(token)


class CapturedPart(Contract):
    kind: str = Field(max_length=64)
    role: str = Field(max_length=32)
    format: Literal["text", "json"] = "json"
    text: str
    origin: Origin | None = None
    section: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=128)
    native_omitted: bool = False


class CapturedRequest(Contract):
    parts: tuple[CapturedPart, ...] = Field(default=(), max_length=65536)
    omitted: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def bounded(self) -> CapturedRequest:
        if self.omitted is not None and self.parts:
            raise ValueError("an omitted capture cannot contain partial request text")
        if len(self.model_dump_json().encode()) > MAX_CAPTURE_BYTES:
            raise ValueError("serialized capture exceeds 8 MiB")
        return self


def request(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> CapturedRequest:
    """Freeze precisely the visible messages and offered schemas before dispatch.

    System sections are split only when the byte boundaries AND every digest match
    the actual system message. Otherwise its complete JSON is kept without attribution.
    """
    parts: list[CapturedPart] = []
    pins = _PINS.get()
    for number, message in enumerate(messages):
        content = message.get("content")
        sections: list[CapturedPart] = []
        if number == 0 and message.get("role") == "system" and isinstance(content, str) and pins:
            raw, start = content.encode("utf-8"), 0
            for pin in pins.sections:
                end = start + pin.bytes
                section = raw[start:end]
                if hashlib.sha256(section).hexdigest() != pin.sha256 or (
                    end < len(raw) and raw[end : end + 2] != b"\n\n"
                ):
                    break
                sections.append(
                    CapturedPart(
                        kind="system_section",
                        role="system",
                        format="text",
                        text=section.decode("utf-8"),
                        section=pin.id,
                        version=pin.version,
                    )
                )
                start = end + 2
            if (
                len(sections) == len(pins.sections)
                and start == len(raw) + 2
                and set(message) == {"role", "content"}
            ):
                parts.extend(sections)
                continue
        parts.append(
            CapturedPart(
                kind=message.origin.kind if isinstance(message, ContextMessage) else "message",
                role=str(message.get("role", "unknown")),
                text=json.dumps(
                    message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ),
                origin=message.origin if isinstance(message, ContextMessage) else None,
                native_omitted=isinstance(message, AssistantMessage) and message.native is not None,
            )
        )
    parts.append(CapturedPart(kind="tool_schemas", role="tools", text=schema_json(tools).decode()))
    # The diagnostic must not prevent a valid model dispatch, nor exceed the
    # confined channel's 24-MiB frame once JSON strings are escaped again.
    size = 32
    for part in parts:
        size += len(part.model_dump_json().encode()) + 1
        if size > MAX_CAPTURE_BYTES - 1024 or len(parts) > 65536:
            return CapturedRequest(
                omitted="Visible request exceeds the 8 MiB serialized diagnostic capture limit; "
                "model dispatch proceeds without a text capture."
            )
    return CapturedRequest(parts=tuple(parts))


def save(
    db: sqlite3.Connection, scope: tuple[str, str, str], identity: str, value: CapturedRequest
) -> None:
    """Join the admitted intent's transaction: no capture can outlive a refused intent."""
    project, session, run = scope
    db.execute(
        "INSERT INTO context_captures VALUES(?,?,?,?,?)",
        (project, session, run, identity, value.omitted),
    )
    for ordinal, part in enumerate(value.parts):
        data = part.text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        db.execute(
            "INSERT OR IGNORE INTO context_bodies VALUES (?,?,?,?)",
            (project, digest, part.text, len(data)),
        )
        db.execute(
            "INSERT INTO context_parts VALUES (?,?,?,?,?,?,?)",
            (
                project,
                session,
                run,
                identity,
                ordinal,
                digest,
                part.model_dump_json(exclude={"text"}),
            ),
        )
