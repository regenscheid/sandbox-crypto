# SPDX-License-Identifier: MIT
"""Decoding what an executor sends: every line is untrusted (review H2, H3 and M3)."""

from __future__ import annotations

import json
import time

import pytest

from sandbox_science.worker import executor
from sandbox_science.worker.channel import MAX_CONTAINERS, MAX_LINE, ProtocolError, decode_line


def test_the_worker_accepts_every_line_the_executor_sends() -> None:
    assert MAX_LINE == executor.MAX_LINE


@pytest.mark.parametrize(
    "line",
    [
        b"[" * 100000 + b"]" * 100000,  # deep nesting
        b'{"n": ' + b"1" * 5000 + b"}",  # an integer too long to convert
        b"[1, 2]",  # not an object
        b"null",
        b"",
        b'{"a": "unfinished',
    ],
)
def test_lines_that_are_not_messages_are_refused(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_line(line)


def test_a_line_of_many_small_objects_is_refused_before_parsing() -> None:
    # M3: 60 MiB of [{},{},…] took the worker from 53 to 1,618 MiB.
    line = b"[" + b"{}," * (MAX_CONTAINERS + 10) + b"{}]"
    started = time.monotonic()
    with pytest.raises(ProtocolError, match="objects and arrays"):
        decode_line(line)
    assert time.monotonic() - started < 1


def test_brackets_inside_strings_do_not_count() -> None:
    text = "{[" * MAX_CONTAINERS
    message = decode_line(json.dumps({"type": "event", "text": text}).encode())
    assert message["text"] == text


def test_values_that_cannot_be_stored_are_cleaned() -> None:
    # H2: a lone surrogate could not be serialized again; Infinity was stored as null and
    # never validated again.
    line = b'{"text": "a\\ud800b", "\\udfff": 1, "inf": Infinity, "nan": NaN, "big": 1e999}'
    message = decode_line(line)
    assert message == {"text": "a�b", "�": 1, "inf": None, "nan": None, "big": None}
    pair = decode_line(b'{"text": "\\ud83d\\ude00"}')
    assert pair["text"] == "\U0001f600"


def test_invalid_utf8_is_replaced() -> None:
    assert decode_line(b'{"text": "\xff"}') == {"text": "�"}
