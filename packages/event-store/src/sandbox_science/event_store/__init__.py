# SPDX-License-Identifier: MIT
"""Append-only event store with a hash chain, so monitor evidence is tamper-evident.

docs/EVENT-STORE.md describes the record encoding, what verification can and
cannot detect, and checkpoints.
"""

from .canonical import MAX_PAYLOAD_BYTES, canonical_json, canonical_payload, parse_payload
from .errors import CheckpointError, EventStoreError, RecordError
from .records import FORMAT, GENESIS, Checkpoint, Record, format_time, record_hash
from .store import DEFAULT_TIMEOUT, EventStore, StoreReader
from .verify import Problem, Verification, verify_rows, verify_store

__all__ = [
    "DEFAULT_TIMEOUT",
    "FORMAT",
    "GENESIS",
    "MAX_PAYLOAD_BYTES",
    "Checkpoint",
    "CheckpointError",
    "EventStore",
    "EventStoreError",
    "Problem",
    "Record",
    "RecordError",
    "StoreReader",
    "Verification",
    "canonical_json",
    "canonical_payload",
    "format_time",
    "parse_payload",
    "record_hash",
    "verify_rows",
    "verify_store",
]
