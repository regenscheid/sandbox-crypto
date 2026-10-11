# SPDX-License-Identifier: MIT
"""Exceptions raised by the policy middleware."""

from __future__ import annotations

from sandbox_science.policy.store import StoreError

__all__ = ["AuthError", "MiddlewareError", "SettingsError", "StoreError"]


class MiddlewareError(Exception):
    """Base class for policy middleware errors."""


class SettingsError(MiddlewareError):
    """The service settings file is missing, malformed or inconsistent."""


class AuthError(MiddlewareError):
    """A call is unauthenticated, or its caller may not make it."""
