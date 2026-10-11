# SPDX-License-Identifier: MIT
"""Exceptions raised by the event store."""

from __future__ import annotations


class EventStoreError(Exception):
    """Base class for event store errors: the store cannot be opened, read or appended to."""


class RecordError(EventStoreError):
    """A record, or a value for one, is malformed.

    ``seq`` is the record's sequence number when the error concerns a stored record.
    """

    def __init__(self, message: str, seq: int | None = None) -> None:
        super().__init__(message)
        self.seq = seq


class CheckpointError(EventStoreError):
    """A checkpoint is malformed."""
