# SPDX-License-Identifier: MIT
"""Exact lockfile byte limits and compatibility with stored canonical hashes."""

import json
from pathlib import Path

import pytest

from sandbox_science.contracts import CondaPackage, Lockfile, PipDistribution
from sandbox_science.contracts.revisions import MAX_LOCKFILE_BYTES
from sandbox_science.worker import revisions
from sandbox_science.worker.artifacts import Blobs
from sandbox_science.worker.store import Store


@pytest.mark.parametrize("entries", [0, 1, 3])
def test_canonical_bytes_and_exact_framing_cap(
    entries: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    lockfile = Lockfile(
        python="3.13.15",
        platform="linux-64",
        conda=tuple(
            CondaPackage(name=f"pkg{i}", version="1.0", build="0", license='MIT \\ "quoted"')
            for i in range(entries)
        ),
        pip=tuple(PipDistribution(name=f"wheel{i}", version="2.0") for i in range(entries)),
        skipped=7,
    )
    expected = lockfile.model_dump_json().encode()
    monkeypatch.setattr(revisions, "MAX_LOCKFILE_BYTES", len(expected))
    assert revisions._serialize_lockfile(lockfile) == expected
    monkeypatch.setattr(revisions, "MAX_LOCKFILE_BYTES", len(expected) - 1)
    assert revisions._serialize_lockfile(lockfile) is None


def test_oversized_lockfile_stops_before_serializing_remaining_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {
        "conda": [
            {"name": f"pkg{i}", "version": "1.0", "build": "0", "license": "L" * 200}
            for i in range(20_000)
        ]
    }
    calls = 0
    serialize = CondaPackage.model_dump_json

    def entry_json(self: CondaPackage, *args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return serialize(self)

    def forbidden_whole_json(*args: object, **kwargs: object) -> str:
        raise AssertionError("whole-lockfile serialization bypasses the bounded encoder")

    monkeypatch.setattr(CondaPackage, "model_dump_json", entry_json)
    monkeypatch.setattr(Lockfile, "model_dump_json", forbidden_whole_json)
    monkeypatch.setattr(revisions, "MAX_LOCKFILE_BYTES", 1024)
    store = Store(tmp_path / "worker.sqlite3")
    try:
        prepared = revisions.Revisions(store, Blobs(tmp_path / "lockfiles")).prepare(raw)
        assert prepared is None
        assert 0 < calls < 10
        assert not list((tmp_path / "lockfiles").glob("blobs/*/*"))
    finally:
        store.close()


@pytest.mark.parametrize("field", ["platform", "subdir"])
def test_single_oversized_valid_string_is_refused_before_serialization(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge = "x" * (MAX_LOCKFILE_BYTES + 1)
    lockfile = (
        Lockfile(platform=huge)
        if field == "platform"
        else Lockfile(conda=(CondaPackage(name="a", version="1", build="0", subdir=huge),))
    )
    dumps = json.dumps
    entry_json = CondaPackage.model_dump_json

    def checked_json(value: object, *args: object, **kwargs: object) -> str:
        assert value is not huge, "oversized scalar reached the serializer"
        return dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def checked_entry(self: CondaPackage, *args: object, **kwargs: object) -> str:
        assert self.subdir is not huge, "oversized entry reached the serializer"
        return entry_json(self)

    monkeypatch.setattr(json, "dumps", checked_json)
    monkeypatch.setattr(CondaPackage, "model_dump_json", checked_entry)
    assert revisions._serialize_lockfile(lockfile) is None
