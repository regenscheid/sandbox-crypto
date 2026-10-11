# SPDX-License-Identifier: MIT
"""Execution events: state changes and output, numbered per execution.

A client that loses its connection asks for the events after the last
sequence number it saw; the last event of an execution is a ``ResultEvent``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from .model import Contract, Identifier
from .worker import Execution, ExecutionState, RichOutput


class _EventBase(Contract):
    execution_id: Identifier
    seq: Annotated[int, Field(ge=1)]
    time: datetime


class StateEvent(_EventBase):
    type: Literal["state"] = "state"
    state: ExecutionState


class StreamEvent(_EventBase):
    """Text from stdout or stderr; it stops once the stream reaches its limit."""

    type: Literal["stream"] = "stream"
    name: Literal["stdout", "stderr"]
    text: str


class OutputEvent(_EventBase):
    type: Literal["output"] = "output"
    output: RichOutput


class ResultEvent(_EventBase):
    """The final event: the execution in its terminal state."""

    type: Literal["result"] = "result"
    execution: Execution


Event = Annotated[StateEvent | StreamEvent | OutputEvent | ResultEvent, Field(discriminator="type")]
