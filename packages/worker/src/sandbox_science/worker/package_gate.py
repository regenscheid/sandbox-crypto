# SPDX-License-Identifier: MIT
"""Typed control-side package admission state; guest protocol uses plain JSON."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sandbox_science.contracts import ErrorKind

from .errors import WorkerError

Mode = Literal["ready", "hold", "needs_recovery"]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True)
class PackageGate:
    """One monotonically versioned admission decision for an environment."""

    generation: int = 0
    mode: Mode = "ready"
    operation: str = ""
    revision: int | None = None

    def fields(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "mode": self.mode,
            "operation": self.operation,
            "revision": self.revision,
        }


def checked(generation: object, mode: object, operation: object, revision: object) -> PackageGate:
    """Refuse malformed persisted state before interpreting admission or ownership."""
    if (
        type(generation) is not int
        or not 0 <= generation < 2**62
        or mode not in ("ready", "hold", "needs_recovery")
        or not isinstance(operation, str)
        or (operation != "" and _ID.fullmatch(operation) is None)
        or (mode == "ready" and operation != "")
        or (mode == "needs_recovery" and operation == "")
        or (revision is not None and (type(revision) is not int or not 0 <= revision < 2**62))
    ):
        raise WorkerError(ErrorKind.UNAVAILABLE, "invalid retained package admission state")
    return PackageGate(generation, mode, operation, revision)


def refusal(gate: PackageGate) -> WorkerError:
    """Keep the earliest operation and recovery reference visible without clearing it."""
    good = f"; last known good revision {gate.revision}" if gate.revision is not None else ""
    return WorkerError(
        ErrorKind.UNAVAILABLE,
        f"the environment needs_recovery after operation {gate.operation}{good}; "
        "new executions and provisioning are refused; automatic restoration is unavailable",
    )
