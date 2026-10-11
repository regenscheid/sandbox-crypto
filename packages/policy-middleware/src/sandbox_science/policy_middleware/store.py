# SPDX-License-Identifier: MIT
"""Project policies by revision, and the revision each sandbox must be decided under.

The store is the policy library's (``sandbox_science.policy.store``), which the worker
and the MCP proxy share: ``policies/<hex>.yaml`` and ``sandboxes/<sandbox id>.json``.
The worker writes both and the middleware only reads them.

A compiled sandbox policy also names its revision (``config.policy.digest``).
Requests are decided only when that revision is the one the sandbox is bound
to, so a sandbox still running an older compiled policy after a revision is
denied until OpenShell applies the new one, rather than decided under rules
the project has since changed.
"""

from __future__ import annotations

from sandbox_science.policy.store import PolicyStore, StoreError, check_digest

__all__ = ["PolicyStore", "StoreError", "check_digest"]
