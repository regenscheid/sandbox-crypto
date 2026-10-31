# SPDX-License-Identifier: MIT
"""A child environment's inputs from control's retained content (child environments
spec, CE-7).

The worker copies a delegation's inputs from the session's environment; it cannot read
control's store. What control retains, it names at admission, with each revision's
SHA-256 and size, so the worker counts it in the staging bound, and sends once the
child environment is ready, before the run starts, part by part:

- a task's inputs and referenced files, by their session paths: the recorded file at
  that path, at its latest revision, when its bytes are kept, which the worker copies
  only where the session's own file is gone;
- a check's evidence artifacts, at the revisions the entry pins.

Each is staged at ``inputs/<path>``, the session path it stands for (an artifact's own
session's files without their ``sessions/<session>/`` prefix), and the worker checks
the whole file's digest at its end. A content read or a part refused fails the
delegation's environment with ``staging_failed``, naming the input.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Final, Protocol

from pydantic import JsonValue

from sandbox_science.contracts.artifact_inventory import ArtifactRevision
from sandbox_science.contracts.child_environments import (
    MAX_PART_BYTES,
    MAX_RETAINED_INPUTS,
    RetainedInput,
)
from sandbox_science.contracts.worker import validate_relative_path
from sandbox_science.orchestrator.artifacts import ArtifactInventory, scoped_path
from sandbox_science.orchestrator.documents.errors import CONFLICT, DocumentError

REFUSED_PARTS: Final = 3
"""Parts in a row the worker may refuse for their offset before staging gives up."""


class RetainedContent(Protocol):
    """Control's retained content, as a session's child environments read it."""

    def find(self, path: str) -> RetainedInput | None:
        """The recorded file at a session path, at its latest revision, when its bytes
        are kept; ``None`` otherwise."""
        ...

    def pinned(self, artifact_id: str, revision: int) -> RetainedInput | None:
        """An artifact's revision, when its bytes are kept; ``None`` otherwise."""
        ...

    def open(self, item: RetainedInput) -> AbstractContextManager[BinaryIO]:
        """The input's bytes from their start; blocking."""
        ...


def session_path(path: str, session: str) -> str:
    """An artifact's path as the session's tools name it: its own session's files without
    their ``sessions/<session>/`` prefix, ``shared/…`` and other sessions' as they are."""
    return path.removeprefix(f"sessions/{session}/")


def _input(path: str, found: ArtifactRevision) -> RetainedInput | None:
    if found.deleted or not found.content_retained:
        return None
    try:
        validate_relative_path(path)
    except ValueError:
        return None
    reference = found.reference
    return RetainedInput(
        path=path,
        artifact_id=reference.artifact_id,
        revision=reference.revision,
        sha256=reference.sha256,
        size=reference.size,
    )


@dataclass(frozen=True)
class InventoryContent:
    """``RetainedContent`` from the project's artifact inventory, for one session."""

    inventory: ArtifactInventory
    project: str
    session: str

    def find(self, path: str) -> RetainedInput | None:
        try:
            logical = scoped_path(path, self.session)
        except DocumentError:
            return None
        if not logical.startswith(("shared/", f"sessions/{self.session}/")):
            return None  # another session's file is no input of this one's
        entries, _ = self.inventory.recent(self.project, at_path=logical)
        return _input(path, entries[0]) if entries else None

    def pinned(self, artifact_id: str, revision: int) -> RetainedInput | None:
        try:
            found = self.inventory.revision(self.project, artifact_id, revision)
        except DocumentError:
            return None
        return _input(session_path(found.path, self.session), found)

    def open(self, item: RetainedInput) -> AbstractContextManager[BinaryIO]:
        found = self.inventory.revision(self.project, item.artifact_id, item.revision)
        if (found.reference.sha256, found.reference.size) != (item.sha256, item.size):
            raise DocumentError(CONFLICT, "the revision's content differs from the input's")
        return self.inventory.open_content(self.project, found.reference)


def reference_files(references: JsonValue) -> list[str]:
    """The session paths a brief's references name (PD-11), as it keeps them."""
    found: list[str] = []
    for item in references if isinstance(references, list) else []:
        if isinstance(item, Mapping) and isinstance(path := item.get("file"), str):
            found.append(path)
    return found


def resolve(
    content: RetainedContent, paths: Iterable[str], artifacts: Iterable[tuple[str, int]]
) -> tuple[RetainedInput, ...]:
    """The inputs control can give from what it retains: for each path, its recorded file;
    for each artifact, that revision; each path once, first one first. Blocking."""
    found: dict[str, RetainedInput] = {}
    for path in paths:
        item = content.find(path)
        if item is not None:
            found.setdefault(item.path, item)
    for artifact_id, revision in artifacts:
        item = content.pinned(artifact_id, revision)
        if item is not None:
            found.setdefault(item.path, item)
    return tuple(found.values())[:MAX_RETAINED_INPUTS]


class _Reader:
    """An input's bytes read forward from an offset, opened again to go back."""

    def __init__(self, content: RetainedContent, item: RetainedInput) -> None:
        self.content, self.item = content, item
        self.stack = contextlib.ExitStack()
        self.stream: BinaryIO | None = None
        self.position = 0

    def read(self, offset: int, length: int) -> bytes:
        if self.stream is None or offset < self.position:
            self.stack.close()
            self.stream = self.stack.enter_context(self.content.open(self.item))
            self.position = 0
        while self.position < offset:
            skipped = self.stream.read(min(MAX_PART_BYTES, offset - self.position))
            if not skipped:
                break
            self.position += len(skipped)
        data = self.stream.read(length) if self.position == offset else b""
        self.position += len(data)
        return data

    def close(self) -> None:
        self.stack.close()


InputSender = Callable[[int, int, bytes], Awaitable[tuple[int, bool, bool]]]
"""Sends part of a retained input (its index, offset and bytes); answers where its
received bytes end, whether the part was written, and whether the input is staged."""


async def send_input(
    content: RetainedContent,
    index: int,
    item: RetainedInput,
    send: InputSender,
    *,
    part_bytes: int = MAX_PART_BYTES,
) -> None:
    """Send one input in parts of ``part_bytes`` from where the worker's received bytes
    end. Raises ``StagingError`` naming the input."""
    reader = _Reader(content, item)
    try:
        offset, _, staged = await send(index, 0, b"")  # starts it, or says where it ends
        refused = 0
        while not staged:
            if offset > item.size:
                raise StagingError(item.path, "the worker holds more than its size")
            try:
                data = await asyncio.to_thread(
                    reader.read, offset, min(part_bytes, item.size - offset)
                )
            except (DocumentError, OSError) as error:
                raise StagingError(item.path, f"control could not read it ({error})") from error
            if not data and offset < item.size:
                raise StagingError(item.path, "control's content ended before its size")
            sent = offset + len(data)
            offset, accepted, staged = await send(index, offset, data)
            if accepted and not staged and offset != sent:
                raise StagingError(
                    item.path, f"the worker holds {offset} bytes after a part that ended at {sent}"
                )
            refused = 0 if accepted else refused + 1
            if refused > REFUSED_PARTS:
                raise StagingError(item.path, "another sender of it is under way")
    finally:
        await asyncio.to_thread(reader.close)


class StagingError(Exception):
    """An input from control's retained content that could not be staged."""

    def __init__(self, path: str, why: str) -> None:
        super().__init__(f"staging_failed: input {path}: {why}")
        self.path = path


def pending(retained: Sequence[RetainedInput], staged: Iterable[str]) -> list[int]:
    """The indexes of the planned inputs not yet staged, by the paths of those copied."""
    have = set(staged)
    return [i for i, item in enumerate(retained) if f"inputs/{item.path}" not in have]


__all__ = [
    "InventoryContent",
    "RetainedContent",
    "StagingError",
    "pending",
    "reference_files",
    "resolve",
    "send_input",
    "session_path",
]
