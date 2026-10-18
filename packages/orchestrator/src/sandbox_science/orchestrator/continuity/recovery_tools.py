# SPDX-License-Identifier: MIT
"""Tool adapters whose original outcome the Resume reconciler understands."""

from typing import Final

from sandbox_science.orchestrator.tools import PDF_SPECS

OBSERVATION_TOOLS: Final = frozenset(
    {"wait_delegations", "delegation_status", "wait_agent_message", "wait_jobs"}
)
WORKER_TOOLS: Final = frozenset(
    {"shell", "python", "sage", "start_job", *(spec.name for spec in PDF_SPECS)}
)
RECONCILABLE_TOOLS: Final = WORKER_TOOLS | OBSERVATION_TOOLS | {"ask_user", "delegate"}
