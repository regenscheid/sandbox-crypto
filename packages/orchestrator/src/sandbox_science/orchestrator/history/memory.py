# SPDX-License-Identifier: MIT
"""Production run memory: historical sequences on disk, only the model view in RAM."""

from __future__ import annotations

from collections.abc import Callable, MutableSequence, Sequence
from pathlib import Path
from typing import Any, Final, cast

from sandbox_science.orchestrator.control import ControlStore
from sandbox_science.orchestrator.model import Message
from sandbox_science.orchestrator.projects import ModelUsage

from .archive import Archive
from .collections import Collection, Members
from .log import RunArchive

VIEW_BYTES: Final = 8 * 1024 * 1024
FINAL_BYTES: Final = 1024 * 1024


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
            resolve_message=lambda identifier: archive.message(scope[0], identifier).chat(),
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
            return cast(Message, value)

        collection = Collection[Message](self.control, self.scope, "messages", decode=decode)
        if collection:
            raise ValueError("a stored run memory identity already exists")
        collection.extend(initial)
        return collection

    def tools(self) -> MutableSequence[dict[str, Any]]:
        return Collection[dict[str, Any]](self.control, self.scope, "tools")

    def usage(self) -> MutableSequence[ModelUsage]:
        return Collection(
            self.control,
            self.scope,
            "usage",
            encode=lambda usage: usage.model_dump(mode="json"),
            decode=ModelUsage.model_validate,
        )

    def members(self, name: str) -> Members:
        return Members(self.control, self.scope, name)

    def reference(self) -> dict[str, object]:
        return {
            "format": "run-history-v1",
            "project": self.scope[0],
            "session": self.scope[1],
            "run": self.scope[2],
        }
