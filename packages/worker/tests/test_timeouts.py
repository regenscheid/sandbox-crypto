# SPDX-License-Identifier: MIT
"""Timing parameters must be positive, finite seconds wherever the worker takes them.

A zero or negative interval would make a polling loop spin (a zero heartbeat would
busy-loop every event stream), and a NaN would never end a wait.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sandbox_science.worker.app import create_app
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.catalog import Catalog
from sandbox_science.worker.channel import ChannelTimeouts
from sandbox_science.worker.environments import Environments
from sandbox_science.worker.manager import ExecutionManager
from sandbox_science.worker.provisioner import Provisioner
from sandbox_science.worker.store import Store
from sandbox_science.worker.suspend import Suspender
from sandbox_science.worker.timeouts import ManagerTimeouts, seconds

BAD = [0, -1.0, math.nan, math.inf, True]


@pytest.mark.parametrize("value", BAD)
def test_seconds_refuses_what_is_not_a_positive_finite_time(value: Any) -> None:
    with pytest.raises(ValueError, match="positive number of seconds"):
        seconds("wait", value)
    assert seconds("wait", 0.001) == 0.001


@pytest.mark.parametrize("value", BAD)
def test_manager_and_channel_timeouts_refuse_bad_values(value: Any) -> None:
    with pytest.raises(ValueError, match="cancel_grace"):
        ManagerTimeouts(cancel_grace=value)
    with pytest.raises(ValueError, match="close_grace"):
        ChannelTimeouts(close_grace=value)
    with pytest.raises(ValueError, match="write"):
        ChannelTimeouts(write=value)
    with pytest.raises(ValueError, match="transfer_write"):
        ChannelTimeouts(transfer_write=value)
    with pytest.raises(ValueError, match="transfer_reply"):
        ChannelTimeouts(transfer_reply=value)


@pytest.mark.parametrize("value", BAD)
def test_the_event_heartbeat_must_be_positive(value: Any) -> None:
    manager = cast(ExecutionManager, SimpleNamespace())
    with pytest.raises(ValueError, match="heartbeat"):
        create_app(manager, "test-secret-" + "t" * 40, heartbeat=value)
    with pytest.raises(ValueError, match="file_write_timeout"):
        create_app(manager, "test-secret-" + "t" * 40, file_write_timeout=value)


@pytest.mark.parametrize("value", BAD)
def test_suspender_intervals_must_be_positive(value: Any) -> None:
    environments = cast(Environments, SimpleNamespace())
    manager = cast(ExecutionManager, SimpleNamespace())
    with pytest.raises(ValueError, match="sample_timeout"):
        Suspender(environments, manager, 60.0, sample_timeout=value)
    with pytest.raises(ValueError, match="interval"):
        Suspender(environments, manager, 60.0, interval=value)


@pytest.mark.parametrize("name", ["poll", "lockfile_wait", "retain"])
def test_provisioner_waits_must_be_positive(tmp_path: Path, name: str) -> None:
    store = Store(tmp_path / "worker.sqlite3")
    manager = SimpleNamespace(
        store=store,
        blobs=Blobs(tmp_path / "blobs"),
        max_artifact_size=65536,
        revisions=SimpleNamespace(),
    )
    waits: dict[str, Any] = {name: 0.0}
    try:
        with pytest.raises(ValueError, match=name):
            Provisioner(
                cast(ExecutionManager, manager),
                catalog=cast(Catalog, SimpleNamespace()),
                policies=None,
                permissions=None,
                cache=None,
                **waits,
            )
    finally:
        store.close()
