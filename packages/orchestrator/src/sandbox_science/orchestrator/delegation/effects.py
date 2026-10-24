# SPDX-License-Identifier: MIT
"""Which built-in tools' effects stay inside the session, for the delegated runs' fence
(D99, delegated agents spec DA-7).

The fence gates outward effects only. A tool named here changes nothing outside the
session's sandbox and files: computation, the session's own files, kernels, background
jobs and installs through the package cache. Outward HTTP from that code is gated where
it leaves the sandbox, by the policy middleware's escalations, not here. Every other
tool of the ``effect`` class is outward for the fence: connector tools (the proxy holds
the ones the policy escalates), host admissions, publishing, delegating, and the
ledger's and documents' writes.

This is the interim classification. D96's effect classes (``read``, ``write``,
``communicate``, ``browse``, ``unclassified``) replace it; until then a new built-in tool
is outward unless it is named here, so it is gated until someone classes it.
"""

from __future__ import annotations

from typing import Final, Literal

Reach = Literal["session", "outward"]
"""Where a tool call's effect can land: in the session only, or possibly beyond it."""

SESSION_TOOLS: Final = frozenset(
    {
        "shell",
        "python",
        "sage",
        "restart_kernel",
        "write_file",
        "pdf_render",
        "start_job",
        "provision",
    }
)
"""The built-in ``effect`` tools whose effects stay in the session. ``pdf_info``,
``pdf_pages`` and ``pdf_search`` are ``read`` tools (``tools.PDF_READS``), which every
gate lets through."""


def reach(tool: str) -> Reach:
    """Where a call of ``tool``, of the ``effect`` class, can act: outward unless named."""
    return "session" if tool in SESSION_TOOLS else "outward"
