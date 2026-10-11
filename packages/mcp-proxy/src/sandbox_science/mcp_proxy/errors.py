# SPDX-License-Identifier: MIT
"""Exceptions raised by the MCP proxy."""

from __future__ import annotations

from collections.abc import Mapping


class McpProxyError(Exception):
    """Base class for MCP proxy errors."""


class SettingsError(McpProxyError):
    """The settings file is missing, malformed or inconsistent."""


class StoreError(McpProxyError):
    """A policy or a project binding could not be read, validated or written."""


class MessageError(McpProxyError):
    """A JSON-RPC message or an event stream is malformed."""


class RefusalError(McpProxyError):
    """The proxy refuses a call before sending it to the server.

    ``reason_code`` is what the caller is told, with ``data`` such as a permission request's
    number; the message is for the proxy's log.
    """

    def __init__(
        self, reason_code: str, detail: str, *, data: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.data = dict(data or {})


class UpstreamError(McpProxyError):
    """A server gave no usable answer; the request may have reached it."""

    def __init__(self, reason_code: str, detail: str, *, status: int | None = None) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.status = status


class SessionGoneError(UpstreamError):
    """The server no longer knows the session (404), so the caller must start a new one."""
