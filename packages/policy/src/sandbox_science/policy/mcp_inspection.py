# SPDX-License-Identifier: MIT
"""Which patterns the policy middleware looks for in a project's MCP answers (D98).

Three layers, by pattern name: the shipped defaults (``DEFAULT_PATTERNS``), the
deployment's patterns (the middleware's settings, ``[inspect]``) and the project policy's
``mcp.inspection.patterns``. A later layer's pattern replaces an earlier one of the same
name, so a project can turn a default from ``alert`` to ``block`` or narrow it to some
servers; ``defaults: false`` in either the deployment's or the project's section drops
the shipped defaults. docs/POLICY-MIDDLEWARE.md, "MCP traffic inspection", lists them.

Every default is written to take time linear in its input: bounded repetitions, no
nested quantifiers, and token patterns anchored where a run of their characters starts.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from .model import InspectionPattern, McpInspection

DEFAULT_PATTERNS: Final[tuple[InspectionPattern, ...]] = (
    # Instruction-like text aimed at the agent.
    InspectionPattern(
        name="ignore-instructions",
        regex=r"(?i)\bignore\s{1,4}(?:all\s{1,4})?(?:the\s{1,4})?(?:previous|prior)\s{1,4}instructions",
    ),
    InspectionPattern(name="you-are-now", regex=r"(?i)\byou\s{1,4}are\s{1,4}now\b"),
    InspectionPattern(name="system-prompt", regex=r"(?i)\bsystem\s{1,4}prompt\b"),
    InspectionPattern(
        name="chat-template-token", regex=r"<\|(?:im_start|im_end|system|endoftext)\|>"
    ),
    InspectionPattern(name="begin-system", regex=r"(?i)\bBEGIN\s{1,4}SYSTEM\b"),
    # Secrets that should not come back from a server.
    InspectionPattern(name="private-key", regex=r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"),
    InspectionPattern(
        name="aws-access-key", regex=r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"
    ),
    InspectionPattern(
        name="google-api-key", regex=r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"
    ),
    InspectionPattern(
        name="github-token",
        regex=r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})",
    ),
    # data: URLs that carry script.
    InspectionPattern(
        name="script-data-url",
        regex=(
            r"(?i)\bdata:\s{0,4}(?:text/html|text/javascript|application/(?:x-)?javascript"
            r"|application/ecmascript|image/svg\+xml)\s{0,4}[;,]"
        ),
    ),
)
"""The shipped defaults, each with the action ``alert``."""


def effective_patterns(
    deployment: Sequence[InspectionPattern],
    deployment_defaults: bool,
    project: McpInspection,
) -> tuple[InspectionPattern, ...]:
    """The patterns in force for a project: the defaults (unless the deployment or the
    project drops them), then the deployment's, then the project's, a later one replacing
    an earlier one of the same name, in first-seen order."""
    chosen: dict[str, InspectionPattern] = {}
    layers: list[Sequence[InspectionPattern]] = []
    if deployment_defaults and project.defaults:
        layers.append(DEFAULT_PATTERNS)
    layers += [deployment, project.patterns]
    for layer in layers:
        for pattern in layer:
            chosen[pattern.name] = pattern
    return tuple(chosen.values())
