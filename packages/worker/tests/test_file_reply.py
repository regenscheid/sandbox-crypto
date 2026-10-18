# SPDX-License-Identifier: MIT
"""An empty range after EOF is valid, but claimed out-of-file data is not."""

import base64

import pytest

from sandbox_science.contracts import ErrorKind
from sandbox_science.worker.errors import WorkerError
from sandbox_science.worker.files import _range_reply


def test_range_reply_accepts_empty_eof_after_file_end() -> None:
    found = _range_reply(
        {"content_b64": "", "size": 6, "offset": 13500, "eof": True, "sha256": "a" * 64},
        13500,
        10000,
        hashed=True,
    )
    assert (found.data, found.offset, found.size, found.eof) == (b"", 13500, 6, True)


@pytest.mark.parametrize(
    "reply",
    [
        {"content_b64": "", "size": 6, "offset": 13500, "eof": False},
        {"content_b64": base64.b64encode(b"x").decode(), "size": 6, "offset": 13500, "eof": True},
        {"content_b64": "", "size": 6, "offset": 3, "eof": True},
        {"content_b64": "", "size": 13500, "offset": 13500, "eof": False},
    ],
)
def test_range_reply_rejects_false_eof_or_data_past_end(reply: dict[str, object]) -> None:
    offset = reply["offset"]
    assert isinstance(offset, int)
    with pytest.raises(WorkerError) as error:
        _range_reply(reply, offset, 10000, hashed=False)
    assert error.value.kind == ErrorKind.INTERNAL
