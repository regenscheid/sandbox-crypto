# SPDX-License-Identifier: MIT
"""docs/MCP-CONNECTORS.md documents every reason code and JSON-RPC error code the proxy uses."""

from __future__ import annotations

from pathlib import Path

from sandbox_science.mcp_proxy.reasons import MESSAGES, REFUSED, UPSTREAM_FAILED

DOC = Path(__file__).resolve().parents[3] / "docs" / "MCP-CONNECTORS.md"


def test_every_reason_code_is_documented() -> None:
    text = DOC.read_text(encoding="utf-8")
    missing = sorted(code for code in MESSAGES if f"`{code}`" not in text)
    assert not missing, (
        f"reason codes in mcp_proxy/reasons.py missing from {DOC.name}: {missing}; "
        "document them in its section on the MCP proxy as built"
    )


def test_error_codes_are_documented() -> None:
    text = DOC.read_text(encoding="utf-8")
    missing = [code for code in (REFUSED, UPSTREAM_FAILED) if str(code) not in text]
    assert not missing, f"JSON-RPC error codes missing from {DOC.name}: {missing}"
