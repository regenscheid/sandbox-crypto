# SPDX-License-Identifier: MIT
"""A fold record as the archive keeps it, and as a view renders it
(docs/specs/context-management.md, CM-17, CM-24, CM-46).

A record is archived once, as it was built, in a message of the kind ``fold``: its
content is the whole record as text, which ``history_read`` returns, and its stored form
is JSON beside it (``FORMAT``), from which each rebuild renders it. The archive never
changes it. Its stubs are the tool results it turned into stubs when it was applied:
every later view shows those as stubs too.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final, cast

from .archive import Archived, escape

Json = dict[str, Any]

FORMAT: Final = "sandbox-fold-1"
LABELS: Final = (
    "--- fold ",
    "--- The user's own words",
    "--- End of the user's own words",
    "--- msg-",
    "--- run ",
    "Outline:",
    "Markers:",
    "Narrative",
    "Search keys:",
    "Session record, written by the platform",
    "Fold records, written by the platform",
    "Research state, rendered by the platform",
    "Platform instruction",
)
"""How the platform's own lines begin: a fold record's, the state block's and their
framings. A line from a message or a model that begins so is quoted (CM-20, CM-F15)."""
MARKER: Final = re.compile(r"-\s*(?:decision|caveat|read|failures)\s*:", re.IGNORECASE)
"""A line that begins like one of a record's marker lines."""
"""The stored form of a fold record, beside its text."""
STUB_CHARS: Final = 5_000
"""A tool result longer than this, older than the newest ``STUBS_KEPT``, is stubbed."""
STUBS_KEPT: Final = 5


@dataclass(frozen=True)
class FoldRecord:
    """A fold of the positions ``first`` to ``last``, each with its message ID."""

    level: int
    first: tuple[int, str]
    last: tuple[int, str]
    runs: tuple[str, ...]
    dates: tuple[str, str]
    skeleton: tuple[str, ...]
    markers: tuple[Json, ...]
    keys: tuple[tuple[str, str], ...]
    stubs: tuple[str, ...] = ()
    narrative: str | None = None
    state: str = "skeleton_only"
    """``skeleton_only``, or ``applied`` for a fold with its narrative."""

    def stored(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":")).encode()

    @classmethod
    def of(cls, row: Archived) -> FoldRecord | None:
        """The record a ``fold`` message keeps, or ``None`` for any other message."""
        entry = row.entry
        if entry.kind != "fold" or entry.native_format != FORMAT or entry.native is None:
            return None
        data = cast(Json, json.loads(entry.native))
        return cls(
            level=int(data["level"]),
            first=(int(data["first"][0]), str(data["first"][1])),
            last=(int(data["last"][0]), str(data["last"][1])),
            runs=tuple(data["runs"]),
            dates=(str(data["dates"][0]), str(data["dates"][1])),
            skeleton=tuple(data["skeleton"]),
            markers=tuple(cast(Json, marker) for marker in data["markers"]),
            keys=tuple((str(key), str(at)) for key, at in data["keys"]),
            stubs=tuple(data["stubs"]),
            narrative=data.get("narrative"),
            state=str(data.get("state") or "skeleton_only"),
        )

    def text(self, fold: str, *, whole: bool = False) -> str:
        """The record as text: as the view shows it, or ``whole``, as the archive keeps
        it, with the IDs of each tool's failed calls listed."""
        (start, first), (end, last) = self.first, self.last
        lines = [
            f"--- fold {fold} · level {self.level} · positions {start} to {end} "
            f"({first} to {last}) · runs {', '.join(self.runs)} · "
            f"{self.dates[0]} to {self.dates[1]} ---",
            "Outline:",
            *(guarded(line) for line in self.skeleton),
        ]
        if self.markers:
            lines.append("Markers:")
            lines += [f"- {marker_line(marker, fold)}" for marker in self.markers]
        if self.narrative:
            # The run's model wrote it: every line quoted, so none reads as the record's.
            lines.append(
                "Narrative, written by the run's model, its values checked by code; quoted:"
            )
            lines += [f"> {line}" for line in self.narrative.splitlines()]
        else:
            lines.append("Narrative: none (a skeleton-only fold).")
        if self.keys:
            shown = "; ".join(f"{flat(key)} ({at})" for key, at in self.keys)
            lines.append("Search keys: " + shown)
        if whole:
            for marker in self.markers:
                if marker.get("kind") == "failures":
                    ids = ", ".join(cast(list[str], marker.get("ids") or []))
                    lines.append(f"Failed calls of {marker.get('subject')}: {ids}")
        return "\n".join(lines)

    @property
    def chars(self) -> int:
        """The record's size as the view shows it, without its marker lines."""
        markers = sum(len(marker_line(m, "msg-" + "0" * 16)) + 3 for m in self.markers)
        return len(self.text("msg-" + "0" * 16)) - markers


def marker_line(marker: Mapping[str, Any], fold: str) -> str:
    """A stored marker as its line, on one line whatever its text held."""
    if marker.get("kind") == "failures":
        return (
            f"failures: {marker.get('count')} {flat(str(marker.get('subject')))} calls failed "
            f"or timed out; their IDs are in fold {fold}"
        )
    return f"{flat(str(marker.get('kind')))}: {flat(str(marker.get('text')))}"


def flat(text: str) -> str:
    """``text`` on one line, its whitespace runs single spaces."""
    return " ".join(text.split())


def guarded(line: str) -> str:
    """``line`` with a backslash before it when it begins like one of the platform's
    own (``LABELS``, ``MARKER``)."""
    lead = line.lstrip()
    return f"\\{line}" if lead.startswith(LABELS) or MARKER.match(lead) else line


def stub(row: Archived, call: Mapping[str, Any] | None) -> Json:
    """A tool result as its stub (CM-24): its tool and key arguments, its size, whether
    a saved file behind it was read whole, and its ID, which ``history_read`` returns
    whole. The message keeps its header and call ID."""
    function = cast(Json, (call or {}).get("function") or {})
    arguments = str(function.get("arguments") or "")
    if len(arguments) > 300:
        arguments = arguments[:300] + " [...]"
    content = row.entry.content or ""
    saved = "a sample, its saved file not shown here" if '"saved"' in content else "no saved file"
    text = (
        f"(Stub: the {row.entry.source or function.get('name')} result of {len(content):,} "
        f"characters is left out of this view; {saved}. history_read {row.message_id} "
        f"returns it whole. Its arguments: {arguments})"
    )
    message = row.chat()
    message["content"] = str(message["content"]).split("\n", 1)[0] + "\n" + escape(text)
    return message


def stubbed(rows: Sequence[Archived], kept: Sequence[str] = ()) -> tuple[str, ...]:
    """The tool results among ``rows`` that a fold applied now stubs: those longer than
    ``STUB_CHARS``, older than the newest ``STUBS_KEPT``, but retrievals, and but those
    already ``kept`` as stubs."""
    results = [row for row in rows if row.entry.kind == "tool_result"]
    older = results[:-STUBS_KEPT] if len(results) > STUBS_KEPT else []
    return tuple(
        row.message_id
        for row in older
        if len(row.entry.content or "") > STUB_CHARS
        and row.entry.source not in ("history_search", "history_read")
        and row.message_id not in kept
    )
