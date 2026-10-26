# SPDX-License-Identifier: MIT
"""How a server's complete tool listing differs from its pin, and the record that says so
(D96, D103).

Two readers of listings share this code. The policy middleware reads the JSON listings it
can inspect whole as they pass to a sandbox (``policy_middleware.mcp.ToolListings``). An
event-stream listing it cannot read passes unread; the agent runtime's direct MCP client
reads it, removes the tools the compiled native rules deny before the model sees it, and
reports each tool's name, digest and hint, which the worker compares here with the
project's pin (``worker.classifications``). Either way a drift is the same
``mcp-tools-drift`` record, whose ``DriftLedger`` holds the server's non-read calls until
the listing is accepted; the record's source says which reader saw it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from .detection import LISTING_UNREAD_RECORD, pin_digest
from .mcp_wire import ListedTool

__all__ = [
    "DRIFT_FINDING",
    "LISTING_UNREAD_RECORD",
    "MAX_DRIFT_NAMES",
    "Drift",
    "drift_record",
    "listing_digest",
    "listing_drift",
]

MAX_DRIFT_NAMES: Final = 100
"""Tool names a drift record lists of those added, removed and changed."""
DRIFT_FINDING: Final = "tool_list_drift"
"""The finding a listing that differs from its pin raises (D96)."""


@dataclass(frozen=True, slots=True)
class Drift:
    """How a server's complete tool listing differs from its pinned list."""

    server: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...] = ()
    """Pinned tools whose name, description or input schema changed."""
    digests: Mapping[str, str] = field(default_factory=dict[str, str])
    """The listing's digest of each added and changed tool (``mcp_wire.tool_digest``)."""
    hints: Mapping[str, str] = field(default_factory=dict[str, str])
    """The class each added tool's hints give it (D96)."""
    listing: str | None = None
    """The digest of the whole listing (``pin_digest`` of every tool's digest by name),
    which an acceptance accepts: the same listing seen again is no new drift."""


def listing_digest(seen: Mapping[str, ListedTool]) -> str:
    """The digest of a whole listing, by tool name."""
    return pin_digest({name: tool.digest for name, tool in seen.items()})


def listing_drift(
    server: str, pinned: Mapping[str, str | None], seen: Mapping[str, ListedTool]
) -> Drift | None:
    """How listing ``seen`` (its tools by name) differs from ``pinned`` (each pinned tool's
    digest by name; None pins the name alone), or None when it does not."""
    changed = tuple(
        sorted(
            name
            for name, digest in pinned.items()
            if digest is not None and name in seen and seen[name].digest != digest
        )
    )
    added = tuple(sorted(set(seen) - set(pinned)))
    removed = tuple(sorted(set(pinned) - set(seen)))
    if not (added or removed or changed):
        return None
    digests = {name: seen[name].digest for name in (*added, *changed)}
    hints = {name: seen[name].hint.value for name in added}
    return Drift(server, added, removed, changed, digests, hints, listing_digest(seen))


def drift_record(
    drift: Drift, pin: str | None, subject: Mapping[str, object], request_id: str | None
) -> dict[str, object]:
    """The payload of the ``mcp-tools-drift`` record of ``drift`` from the pin whose digest
    is ``pin``: who listed (``subject``), what drifted, bounded to ``MAX_DRIFT_NAMES`` of
    each kind, and the counts."""
    added, changed = drift.added[:MAX_DRIFT_NAMES], drift.changed[:MAX_DRIFT_NAMES]
    return {
        **subject,
        "request_id": request_id,
        "finding": DRIFT_FINDING,
        "pin": pin,
        "listing": drift.listing,
        "added": list(added),
        "removed": list(drift.removed[:MAX_DRIFT_NAMES]),
        "changed": list(changed),
        "digests": {name: drift.digests[name] for name in (*added, *changed)},
        "hints": {name: drift.hints[name] for name in added},
        "counts": {
            "added": len(drift.added),
            "removed": len(drift.removed),
            "changed": len(drift.changed),
        },
    }
