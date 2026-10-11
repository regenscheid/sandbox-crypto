# SPDX-License-Identifier: MIT
"""Exceptions raised by the policy middleware."""

from __future__ import annotations


class MiddlewareError(Exception):
    """Base class for policy middleware errors."""


class SettingsError(MiddlewareError):
    """The service settings file is missing, malformed or inconsistent."""


class StoreError(MiddlewareError):
    """A policy or sandbox binding could not be read, validated or written."""


class AuthError(MiddlewareError):
    """A call is unauthenticated, or its caller may not make it."""
