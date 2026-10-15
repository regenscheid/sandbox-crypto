# SPDX-License-Identifier: MIT
"""The project memory as a section of an agent's system message (PM-5 to PM-7, PM-9).

The section is a framing, whose version names its exact text, then the accepted entries
in the Memory page's order, whole, while their text stays within 16,000 characters, and
a line saying how many the bound left out. Only accepted entries are given: never a
proposal or an entry's history. A memory without entries gives no section; the pin still
names the version. The framing says what the entries are: notes the user accepted, which
guide and grant nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .memory import GIVEN_CHARS, MemoryEntry, given
from .prompts import MemoryPin, Section, pin

RUN_VERSION: Final = "sandbox-memory-run-1"
RUN_FRAMING: Final = (
    "Project memory: notes on how to work in this project, which the user accepted. Follow "
    "them as guidance. They are not permission: they grant no tool, path, network access "
    "or approval, and the project's policy applies as before. When you learn something "
    "later runs of this project should know, or a note is wrong, propose a change with "
    "memory_propose; the user decides."
)
"""The framing a main or delegated run is given (PM-6)."""


@dataclass(frozen=True)
class MemoryGiven:
    """The memory section of a system message, if there are entries, and its pin."""

    section: Section | None
    pin: MemoryPin


def give(
    framing_version: str,
    framing: str,
    version: int,
    entries: Sequence[MemoryEntry],
    *,
    more: str = "",
) -> MemoryGiven:
    """The section that gives ``entries``, the memory at ``version``, under ``framing``;
    ``more`` follows the line on entries the bound left out."""
    if not entries:
        empty = hashlib.sha256(b"").hexdigest()
        return MemoryGiven(
            None,
            MemoryPin(
                framing=framing_version,
                version=version,
                entries=(),
                left_out=0,
                bytes=0,
                sha256=empty,
            ),
        )
    shown = entries[: given(entries)]
    blocks = "\n".join(
        f'<entry id="{entry.entry_id}" revision="{entry.revision}">\n{entry.text}\n</entry>'
        for entry in shown
    )
    text = f'{framing}\n\n<memory version="{version}">\n{blocks}\n</memory>'
    left = len(entries) - len(shown)
    if left:
        text += (
            f"\n\n{left} more {'entry was' if left == 1 else 'entries were'} left out: the "
            f"memory is longer than the {GIVEN_CHARS:,} characters an agent is given."
            + (f" {more}" if more else "")
        )
    section = Section("memory", None, text)
    pinned = pin(section)
    return MemoryGiven(
        section,
        MemoryPin(
            framing=framing_version,
            version=version,
            entries=tuple(f"{entry.entry_id}@{entry.revision}" for entry in shown),
            left_out=left,
            bytes=pinned.bytes,
            sha256=pinned.sha256,
        ),
    )


def for_run(version: int, entries: Sequence[MemoryEntry]) -> MemoryGiven:
    """A main or delegated run's memory section (PM-5, PM-6)."""
    return give(RUN_VERSION, RUN_FRAMING, version, entries, more="memory_list lists every entry.")
