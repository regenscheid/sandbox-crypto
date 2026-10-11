# SPDX-License-Identifier: MIT
"""Project policies by revision, and the revision each project's MCP calls are decided under.

The store is the policy library's (``sandbox_science.policy.store``), with the policy
middleware's layout, so one directory can serve both: ``policies/<hex>.yaml``, and
``projects/<project>.json`` binding a project to one revision. The binding is read
for every call, so a new revision applies to the next call without restarting the
proxy.
"""

from __future__ import annotations

from pathlib import Path

from sandbox_science.policy.store import PolicyStore as _PolicyStore
from sandbox_science.policy.store import StoreError, check_digest

__all__ = ["PolicyStore", "StoreError", "check_digest"]


class PolicyStore(_PolicyStore):
    """Policies, and each project's binding."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, "projects")
