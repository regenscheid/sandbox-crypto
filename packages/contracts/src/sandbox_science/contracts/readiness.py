# SPDX-License-Identifier: MIT
"""Passive worker readiness; never resolves an environment or starts an executor."""

from typing import Literal

from pydantic import Field

from .model import Contract


class WorkerReadiness(Contract):
    ready: Literal[True] = True
    protocol: int = Field(strict=True, ge=1)
