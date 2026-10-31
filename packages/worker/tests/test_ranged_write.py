# SPDX-License-Identifier: MIT
"""The executor's ranged write (sandbox_files.py, ``write_range``), on real files: a file
of the session written in parts kept outside the workspace, named once its whole SHA-256
and size are checked; resumed where its part ends; discarded when it differs (child
environments, CE-6 and CE-7)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.worker import executor, sandbox_files

UPLOAD = "0123456789abcdef0123456789abcdef"


def _requests(root: Path) -> sandbox_files.Requests:
    (root / "sessions" / "s1").mkdir(parents=True, exist_ok=True)
    return sandbox_files.Requests(root, max_read=1024, max_list=100)


def _write(requests: sandbox_files.Requests, **fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"session": "s1", "path": "out/big.bin", "upload": UPLOAD}
    if isinstance(fields.get("content"), bytes):
        fields["content_b64"] = base64.b64encode(fields.pop("content")).decode()
    return asyncio.run(requests.write_range({**values, **fields}))


def _refused(requests: sandbox_files.Requests, **fields: Any) -> sandbox_files.ToolError:
    with pytest.raises(sandbox_files.ToolError) as caught:
        _write(requests, **fields)
    return caught.value


def test_parts_take_the_files_name_once_whole_and_checked(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    data = b"abcdefghij" * 10
    digest = hashlib.sha256(data).hexdigest()
    assert _write(requests, offset=0, content=data[:40]) == {"size": 40, "accepted": True}
    target = tmp_path / "sessions" / "s1" / "out" / "big.bin"
    # The part is outside every workspace until the commit.
    assert not target.exists()
    assert (tmp_path / ".parts" / UPLOAD).read_bytes() == data[:40]
    assert _write(requests, offset=40, content=data[40:80])["size"] == 80
    done = _write(requests, offset=80, content=data[80:], sha256=digest, size=100)
    assert done == {"size": 100, "accepted": True, "committed": True, "sha256": digest}
    assert target.read_bytes() == data
    assert os.listdir(tmp_path / ".parts") == []
    # A commit whose reply was lost: asked again, the file has its name already.
    again = _write(requests, offset=100, content=b"", sha256=digest, size=100)
    assert again["committed"] is True


def test_a_part_not_where_the_part_ends_is_not_written(tmp_path: Path) -> None:
    """The reply says where the part ends, so a writer resumes there: offset 0 asks."""
    requests = _requests(tmp_path)
    _write(requests, offset=0, content=b"12345")
    assert _write(requests, offset=0, content=b"other") == {"size": 5, "accepted": False}
    assert _write(requests, offset=9, content=b"x") == {"size": 5, "accepted": False}
    assert (tmp_path / ".parts" / UPLOAD).read_bytes() == b"12345"
    # Without a part, only offset 0 starts one.
    other = "f" * 32
    assert _write(requests, upload=other, offset=3, content=b"x") == {
        "size": 0,
        "accepted": False,
    }


def test_parts_that_differ_are_discarded_and_name_nothing(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    target = tmp_path / "sessions" / "s1" / "out" / "big.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"earlier")
    _write(requests, offset=0, content=b"new content")
    wrong = _refused(
        requests, offset=11, content=b"", sha256=hashlib.sha256(b"other").hexdigest(), size=11
    )
    assert wrong.kind == "conflict"
    assert "they are discarded" in wrong.message
    assert target.read_bytes() == b"earlier"
    assert os.listdir(tmp_path / ".parts") == []
    # Discarding a part that is gone is no error.
    assert _write(requests, offset=0, discard=True) == {"size": 0, "accepted": True}


def test_ranged_writes_keep_to_the_session(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    (tmp_path / "shared").mkdir()
    assert _refused(requests, path="shared/a.bin", offset=0).kind == "invalid_request"
    assert _refused(requests, path="../s2/a.bin", offset=0).kind == "invalid_request"
    assert _refused(requests, upload="../escape", offset=0).kind == "invalid_request"
    assert _refused(requests, offset=-1).kind == "invalid_request"
    assert _refused(requests, offset=0, sha256="0" * 64).kind == "invalid_request"
    # A target that is a symlink is not written through.
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    link = tmp_path / "sessions" / "s1" / "link.bin"
    link.symlink_to(outside)
    _write(requests, path="link.bin", offset=0, content=b"data")
    digest = hashlib.sha256(b"data").hexdigest()
    refused = _refused(requests, path="link.bin", offset=4, sha256=digest, size=4)
    assert "symlink" in refused.message
    assert outside.read_bytes() == b"keep"


def test_a_part_left_for_a_day_is_removed_when_another_starts(tmp_path: Path) -> None:
    requests = _requests(tmp_path)
    _write(requests, offset=0, content=b"old")
    stale = tmp_path / ".parts" / UPLOAD
    hour_ago, day_ago = time.time() - 3600, time.time() - 25 * 3600
    os.utime(stale, (hour_ago, hour_ago))
    _write(requests, upload="a" * 32, offset=0, content=b"new")
    assert stale.exists()
    os.utime(stale, (day_ago, day_ago))
    _write(requests, upload="b" * 32, offset=0, content=b"new")
    assert sorted(os.listdir(tmp_path / ".parts")) == ["a" * 32, "b" * 32]


async def test_the_executor_says_and_serves_ranged_writes(tmp_path: Path) -> None:
    (tmp_path / "sessions" / "s1").mkdir(parents=True)
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    serving = executor.Executor(tmp_path, send)
    await serving.handle({"id": 1, "op": "hello"})
    assert messages[-1]["ranged_write"] == executor.RANGED_WRITE == 1
    data = b"through the executor"
    await serving.handle(
        {
            "id": 2,
            "op": "write_range",
            "session": "s1",
            "path": "a.bin",
            "upload": UPLOAD,
            "offset": 0,
            "content_b64": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    )
    assert messages[-1]["committed"] is True, messages[-1]
    assert (tmp_path / "sessions" / "s1" / "a.bin").read_bytes() == data
