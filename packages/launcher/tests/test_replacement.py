# SPDX-License-Identifier: MIT
"""A replacement request binds to exactly one owner."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from sandbox_science.launcher import instance
from sandbox_science.launcher.replacement import Request
from sandbox_science.launcher.supervise import start_time

NONCE = "replacement-" + "a" * 32
OWNER = "instance-" + "b" * 32


def this_launcher() -> Request:
    pid = os.getpid()
    started = start_time(pid)
    assert started is not None
    return Request(nonce=NONCE, pid=pid, started=started, boot=instance.boot_id())


@pytest.mark.parametrize(
    "owner",
    [
        {},
        {"pid": 1, "started": 1, "boot": "boot", "instance": OWNER},
        {"pid": 1, "started": 1},
        {"pid": 1, "instance": OWNER},
    ],
)
def test_a_request_names_exactly_one_owner(owner: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="exactly one owner"):
        Request.model_validate({"nonce": NONCE, **owner})


def test_an_instance_request_binds_only_that_instance() -> None:
    request = Request(nonce=NONCE, instance=OWNER)
    assert request.owned_by(OWNER)
    assert not request.owned_by("instance-" + "c" * 32)
    assert not request.owned_by()  # the host launcher is not that instance


def test_a_launcher_request_binds_only_this_launcher() -> None:
    request = this_launcher()
    assert request.owned_by()
    assert not request.owned_by(OWNER)
    assert not request.model_copy(update={"pid": os.getpid() + 1}).owned_by()
    assert "instance" not in request.model_dump(exclude_none=True)
