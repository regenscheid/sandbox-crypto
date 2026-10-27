# SPDX-License-Identifier: MIT
"""Which built-in tools' effects stay inside the session, for the delegated runs' fence
(D99, delegated agents spec DA-7).

The fence gates outward effects only. A tool named here changes nothing outside the
session's sandbox and files: computation, the session's own files, kernels, background
jobs and installs through the package cache. Outward HTTP from that code is gated where
it leaves the sandbox, by the policy middleware's escalations, not here. Every other
tool of the ``effect`` class is outward for the fence: connector tools (the policy
middleware holds the ones the policy escalates), host admissions, publishing, delegating, and the
ledger's and documents' writes.

This is the interim classification of built-in tools; a new built-in tool is outward unless
it is named here, so it is gated until someone classes it. Connector tools take D96's
effect classes instead (``connector_reach``), as a classifying client gives each listed
tool's class, and only a tool whose every call is a ``read`` stays in the session.
Delegated runs are offered connector tools (DA-3): the fence passes their reads, and lets a
call held for the user through whatever the plan (``fence.ChildFence``); the harness
records each connector call's class and reach. The MCP proxy classified its listings; the
agent runtime's client, the only one since the proxy retired (D98, 2026-10-26), passes the
server's own listing, whose classes are not taken, so every connector call is outward.
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
        "edit_file",
        "pdf_render",
        "start_job",
        "provision",
    }
)
"""The built-in ``effect`` tools whose effects stay in the session. ``pdf_info``,
``pdf_pages`` and ``pdf_search`` are ``read`` tools (``tools.PDF_READS``), as ``read_file``,
``list_files``, ``glob`` and ``grep`` are, which every gate lets through."""


EFFECT_CLASSES: Final = frozenset(
    {"read", "write", "communicate", "browse", "admin", "unclassified"}
)
"""D96's effect classes, as the policy library names them (``sandbox-science/effect``)."""


def reach(tool: str) -> Reach:
    """Where a call of ``tool``, of the ``effect`` class, can act: outward unless named."""
    return "session" if tool in SESSION_TOOLS else "outward"


def connector_reach(effect_class: str | None) -> Reach:
    """Where a connector call can act, by its tool's effect class: a ``read`` stays in the
    session; any other class, or none known, is outward."""
    return "session" if effect_class == "read" else "outward"
