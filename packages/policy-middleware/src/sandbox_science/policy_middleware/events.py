# SPDX-License-Identifier: MIT
"""Records the middleware keeps for the approval flow and the monitors.

OpenShell logs every middleware decision itself (OCSF), but not the text of
our findings. The middleware therefore records what other components act on:
escalations, which become pending permission requests, and alerts. The event
store with its hash chain will replace this JSON-lines file.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol


class EventSink(Protocol):
    """Somewhere to append middleware records."""

    def record(self, event: Mapping[str, object]) -> None:
        """Append one record; raise if it could not be stored."""
        ...


class JsonLinesEventLog:
    """Append records to a file, one JSON object per line."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: Mapping[str, object]) -> None:
        """Append one record and flush it to the operating system."""
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
