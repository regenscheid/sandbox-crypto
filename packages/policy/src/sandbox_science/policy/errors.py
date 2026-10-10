# SPDX-License-Identifier: MIT
"""Exceptions raised by the policy library."""

from __future__ import annotations


class PolicyError(Exception):
    """Base class for errors raised by the policy library."""


class PolicyLoadError(PolicyError):
    """A policy or pool-ceiling file could not be read, parsed or validated."""


class ClassificationError(PolicyError):
    """A request could not be classified unambiguously. Callers must deny it."""
