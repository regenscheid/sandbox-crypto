# SPDX-License-Identifier: MIT
"""Production run memory: historical sequences on disk, only the model view in RAM."""

from __future__ import annotations

import base64
from collections.abc import Callable, MutableSequence, Sequence
from pathlib import Path
from typing import Any, Final, cast

from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.model import AssistantMessage, Message
from sandbox_science.orchestrator.projects import ModelUsage

from .archive import Archive
from .collections import Collection, Members
from .log import RunArchive
from .snapshot import compact

VIEW_BYTES: Final = 8 * 1024 * 1024
FINAL_BYTES: Final = 1024 * 1024


class UsageCollection(Collection[ModelUsage]):
    def recent(self, limit: int = 8) -> list[ModelUsage]:
        """Calibration sample, without traversing all prior calls on every turn."""
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT item FROM run_collections WHERE project=? AND session=? AND run=? "
                "AND collection=? AND json_extract(item,'$.characters')>0 "
                "AND json_extract(item,'$.input_tokens') IS NOT NULL "
                "AND json_extract(item,'$.output_tokens') IS NOT NULL "
                "ORDER BY position DESC LIMIT ?",
                (*self.scope, self.name, limit),
            ).fetchall()
        return [ModelUsage.model_validate_json(text) for (text,) in reversed(rows)]

    def totals(self) -> dict[str, int | float]:
        """Exact cumulative counters for budgets, including unpriced answered calls."""
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT count(*),coalesce(sum(json_extract(item,'$.cost')),0),"
                "coalesce(sum(json_extract(item,'$.input_tokens')),0),"
                "coalesce(sum(json_extract(item,'$.output_tokens')),0) "
                "FROM run_collections WHERE project=? AND session=? AND run=? AND collection=?",
                (*self.scope, self.name),
            ).fetchone()
        return dict(zip(("calls", "cost", "input_tokens", "output_tokens"), row, strict=True))


class RunMemory:
    def __init__(
        self,
        control: ControlStore,
        scope: tuple[str, str, str],
        *,
        resolve_message: Callable[[str], Message] | None = None,
    ) -> None:
        self.scope, self.control, self.resolve_message = scope, control, resolve_message

    @classmethod
    def archived(cls, log: RunArchive) -> RunMemory:
        scope = (log.context.project_id, log.context.session_id, log.run)
        archive = Archive(log.archive.control)
        return cls(
            log.archive.control,
            scope,
            resolve_message=lambda identifier: archive.message(
                scope[0], identifier, max_bytes=VIEW_BYTES
            ).chat(),
        )

    @classmethod
    def scratch(cls, path: Path, scope: tuple[str, str, str]) -> RunMemory:
        """Local paged runtime cache. It grants no remote state or execution authority.

        The caller closes ``control`` after finishing transport/finalization. Canonical
        evidence must have committed at the host before the remote log returns.
        """
        control = ControlStore(path)
        control.initialize()
        return cls(control, scope)

    def messages(self, initial: Sequence[Message]) -> Collection[Message]:
        def decode(value: Any) -> Message:
            if isinstance(value, dict) and set(cast(dict[str, object], value)) == {
                "archive_message"
            }:
                if self.resolve_message is None:
                    raise ValueError("an archive reference requires its scoped resolver")
                return self.resolve_message(str(cast(dict[str, object], value)["archive_message"]))
            record = cast(dict[str, Any], value)
            if record.get("format") == "assistant-cache-v1":
                chat = cast(Message, record["chat"])
                native = record["native"]
                return AssistantMessage(
                    chat.get("content"),
                    chat.get("tool_calls", ()),
                    native=base64.b64decode(native, validate=True) if native is not None else None,
                    native_format=record["native_format"],
                    alias=record["alias"],
                    model=record["model"],
                    gateway_version=record["gateway_version"],
                    prefix=record["prefix"],
                    adapter_state=record["adapter_state"],
                )
            return record

        def encode(message: Message) -> object:
            if not isinstance(message, AssistantMessage):
                return message
            return {
                "format": "assistant-cache-v1",
                "chat": dict(message),
                "native": base64.b64encode(message.native).decode("ascii")
                if message.native is not None
                else None,
                "native_format": message.native_format,
                "alias": message.alias,
                "model": message.model,
                "gateway_version": message.gateway_version,
                "prefix": message.prefix,
                "adapter_state": message.adapter_state,
            }

        collection = Collection[Message](
            self.control, self.scope, "messages", encode=encode, decode=decode
        )
        if collection:
            raise ValueError("a stored run memory identity already exists")
        collection.extend(initial)
        return collection

    def tools(self) -> MutableSequence[dict[str, Any]]:
        return Collection[dict[str, Any]](self.control, self.scope, "tools")

    def evidence_tools(self) -> Collection[dict[str, Any]]:
        """Resolve retained tool references one at a time, with a visible read ceiling."""
        archive = Archive(self.control)

        def decode(value: Any) -> dict[str, Any]:
            record = cast(dict[str, Any], value)
            stored = record.get("_record_storage")
            if isinstance(stored, dict):
                reference = cast(dict[str, object], stored)
                if reference.get("project") != self.scope[0]:
                    raise ValueError("tool evidence reference belongs to another project")
                try:
                    return archive.tool_record(
                        self.scope[0], str(reference["message"]), max_bytes=VIEW_BYTES
                    )
                except ControlError as error:
                    return {**record, "_evidence_unavailable": error.kind}
            return record

        return Collection[dict[str, Any]](self.control, self.scope, "tools", decode=decode)

    def usage(self) -> UsageCollection:
        return UsageCollection(
            self.control,
            self.scope,
            "usage",
            encode=lambda usage: usage.model_dump(mode="json"),
            decode=ModelUsage.model_validate,
        )

    def members(self, name: str) -> Members:
        return Members(self.control, self.scope, name)

    def summaries(self, values: dict[str, Any]) -> dict[str, Any]:
        collection = Collection[dict[str, Any]](self.control, self.scope, "summaries")
        if not collection:
            for source, value in values.items():
                collection.append({"source": source, "value": value})
        return {source: compact(value, f"summaries/{source}") for source, value in values.items()}

    def reference(self) -> dict[str, object]:
        with self.control.read() as tx:
            counts = dict(
                tx.sql.execute(
                    "SELECT collection,count(*) FROM run_collections WHERE project=? AND session=? "
                    "AND run=? GROUP BY collection",
                    self.scope,
                )
            )
        return {
            "collections": counts,
            "format": "run-history-v1",
            "project": self.scope[0],
            "session": self.scope[1],
            "run": self.scope[2],
        }
