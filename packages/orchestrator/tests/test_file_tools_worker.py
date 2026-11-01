# SPDX-License-Identifier: MIT
"""``edit_file``, ``glob`` and ``grep`` through a real worker's HTTP API and its executor:
an edit is recorded as a write is (RA-27), and the searches see what the edit made."""

from __future__ import annotations

import json

import pytest

from .test_artifact_publication import worker_url as worker_url
from .test_file_recording import Recording
from .test_file_recording import recording as recording

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]


async def test_an_edit_is_a_recorded_write_that_the_searches_see(recording: Recording) -> None:
    await recording.call("write_file", path="src/model.py", content="rate = 1\nsize = 1\n")
    ambiguous = await recording.call(
        "edit_file", path="src/model.py", old_string=" = 1", new_string=" = 2"
    )
    assert json.loads(ambiguous.text)["error"]["message"].startswith("ambiguous: ")
    edited = await recording.call(
        "edit_file", path="src/model.py", old_string="rate = 1", new_string="rate = 3"
    )
    assert edited.record["edit"]["replacements"] == 1
    assert edited.record["recorded_files"]["recorded"] == 1
    head = recording.heads()["sessions/s1/src/model.py"]
    assert head.reference.revision == 2
    assert head.reference.sha256 == edited.record["edit"]["sha256"]
    found = await recording.call("grep", pattern=r"rate = \d", mode="content")
    assert json.loads(found.text)["lines"] == ["src/model.py:1: rate = 3"]
    assert found.record["effect"] == "read"
    listed = await recording.call("glob", pattern="**/*.py")
    assert [entry["path"] for entry in json.loads(listed.text)["entries"]] == ["src/model.py"]
    # A shared edit needs the hash read_file showed, and a stale one is a conflict.
    created = await recording.call(
        "write_file", path="shared/plan.md", content="step one\n", create_only=True
    )
    unguarded = await recording.call(
        "edit_file", path="shared/plan.md", old_string="one", new_string="two"
    )
    assert json.loads(unguarded.text)["error"]["kind"] == "invalid_request"
    first = created.record["write"]["sha256"]
    done = await recording.call(
        "edit_file",
        path="shared/plan.md",
        old_string="one",
        new_string="two",
        expected_sha256=first,
    )
    assert recording.heads()["shared/plan.md"].reference.sha256 == done.record["edit"]["sha256"]
    stale = await recording.call(
        "edit_file",
        path="shared/plan.md",
        old_string="two",
        new_string="three",
        expected_sha256=first,
    )
    assert json.loads(stale.text)["error"]["kind"] == "conflict"
