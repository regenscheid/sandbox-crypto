# SPDX-License-Identifier: MIT
"""Files that recent environment lockfiles pin are kept from eviction and counted."""

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_science.contracts.revisions import MAX_LOCKFILE_BYTES
from sandbox_science.package_cache import cli
from sandbox_science.package_cache.adapter import Kind
from sandbox_science.package_cache.pins import LockfilePins, lockfile_pins
from sandbox_science.package_cache.settings import load_settings
from sandbox_science.package_cache.store import ACCESS_INTERVAL, Removal, Store, StoreStats

DAY = 86400.0


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000_000.0

    def __call__(self) -> float:
        return self.now


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def lockfile(store: Path, *, conda: tuple[bytes, ...] = (), pip: tuple[bytes, ...] = ()) -> Path:
    """Write a lockfile into a worker lockfile store, named by the SHA-256 of its JSON."""
    document = {
        "format": "sandbox-science-lockfile",
        "version": 1,
        "python": "3.12.1",
        "platform": "linux-64",
        "conda": [
            {"name": f"c{number}", "version": "1", "build": "0", "sha256": sha(body)}
            for number, body in enumerate(conda)
        ],
        "pip": [
            {"name": f"p{number}", "version": "1", "sha256": sha(body)}
            for number, body in enumerate(pip)
        ]
        + [{"name": "from-an-index", "version": "2", "sha256": None}],
        "skipped": 0,
    }
    data = json.dumps(document).encode()
    path = store / "blobs" / sha(data)[:2] / sha(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def age(path: Path, seconds: float, clock: Clock) -> None:
    os.utime(path, (clock.now - seconds, clock.now - seconds))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def lockfiles(tmp_path: Path) -> Path:
    return tmp_path / "worker" / "lockfiles"


@pytest.fixture
def pins(lockfiles: Path, clock: Clock) -> LockfilePins:
    return LockfilePins(lockfiles, protect_days=30, clock=clock, refresh=0)


@pytest.fixture
def store(tmp_path: Path, clock: Clock, pins: LockfilePins) -> Iterator[Store]:
    with Store(
        tmp_path / "cache", max_size=1000, clock=clock, entry_overhead=0, pins=pins
    ) as store:
        yield store


def put(store: Store, key: str, body: bytes) -> None:
    store.put(key, store.write_temp(body), kind=Kind.ARTIFACT, content_type="application/x-test")


def fill(store: Store, clock: Clock, names: str) -> None:
    for name in names:
        put(store, name, name.encode() * 200)
        clock.now += ACCESS_INTERVAL + 1


def test_recent_lockfiles_pin_their_conda_and_pip_digests(
    lockfiles: Path, pins: LockfilePins, clock: Clock
) -> None:
    lockfile(lockfiles, conda=(b"numpy",), pip=(b"wheel",))
    old = lockfile(lockfiles, conda=(b"old",))
    age(old, 31 * DAY, clock)
    assert pins() == {sha(b"numpy"), sha(b"wheel")}


def test_no_store_or_unreadable_lockfiles_pin_nothing(
    lockfiles: Path, pins: LockfilePins, caplog: pytest.LogCaptureFixture
) -> None:
    assert pins() == frozenset()
    good = lockfile(lockfiles, conda=(b"kept",))
    bad = good.parent / (good.name[:2] + "0" * 62)
    bad.write_text("not json")
    other = good.parent / (good.name[:2] + "1" * 62)
    other.write_text(json.dumps({"format": "something-else", "conda": [{"sha256": "a" * 64}]}))
    link = good.parent / (good.name[:2] + "2" * 62)
    link.symlink_to(good)
    large = good.parent / (good.name[:2] + "3" * 62)
    large.write_bytes(b" " * (MAX_LOCKFILE_BYTES + 1))
    # Names that are not the store's are not lockfiles at all.
    (good.parent / "README").write_text(json.dumps({"format": "sandbox-science-lockfile"}))
    with caplog.at_level(logging.WARNING):
        assert pins() == {sha(b"kept")}
    assert "could not be read" in caplog.text


def test_a_reading_is_reused_until_refresh(lockfiles: Path, clock: Clock) -> None:
    pins = LockfilePins(lockfiles, protect_days=30, clock=clock, refresh=60)
    lockfile(lockfiles, conda=(b"first",))
    assert pins() == {sha(b"first")}
    lockfile(lockfiles, conda=(b"second",))
    assert pins() == {sha(b"first")}
    clock.now += 60
    assert pins() == {sha(b"first"), sha(b"second")}


def test_eviction_without_pins_removes_the_least_recently_used(
    tmp_path: Path, clock: Clock
) -> None:
    with Store(tmp_path / "plain", max_size=1000, clock=clock, entry_overhead=0) as store:
        fill(store, clock, "abcdef")
        assert store.evict() == Removal(entries=2, blobs=2, bytes=400)
        assert store.keys() == ["c", "d", "e", "f"]
        assert store.stats() == StoreStats(entries=4, bytes=800, protected_bytes=0)


def test_eviction_skips_pinned_files_and_takes_the_next_least_recent(
    store: Store, lockfiles: Path, clock: Clock
) -> None:
    lockfile(lockfiles, conda=(b"a" * 200,), pip=(b"b" * 200,))
    fill(store, clock, "abcdef")
    assert store.stats() == StoreStats(entries=6, bytes=1200, protected_bytes=400)
    assert store.evict() == Removal(entries=2, blobs=2, bytes=400)
    assert store.keys() == ["a", "b", "e", "f"]
    assert store.blob_path(sha(b"a" * 200)).exists()
    assert store.stats() == StoreStats(entries=4, bytes=800, protected_bytes=400)


def test_an_expired_lockfile_no_longer_protects(
    store: Store, lockfiles: Path, clock: Clock
) -> None:
    age(lockfile(lockfiles, conda=(b"a" * 200,)), 31 * DAY, clock)
    fill(store, clock, "abcdef")
    assert store.stats().protected_bytes == 0
    store.evict()
    assert store.keys() == ["c", "d", "e", "f"]


def test_include_pinned_evicts_by_age_alone(store: Store, lockfiles: Path, clock: Clock) -> None:
    lockfile(lockfiles, conda=(b"a" * 200, b"b" * 200))
    fill(store, clock, "abcdef")
    assert store.evict(include_pinned=True) == Removal(entries=2, blobs=2, bytes=400)
    assert store.keys() == ["c", "d", "e", "f"]


def test_pinned_bytes_over_the_cap_warn_and_nothing_pinned_is_evicted(
    store: Store, lockfiles: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    lockfile(lockfiles, conda=tuple(name.encode() * 200 for name in "abcdef"))
    fill(store, clock, "abcdefg")
    with caplog.at_level(logging.WARNING):
        assert store.evict() == Removal(entries=1, blobs=1, bytes=200)
    assert store.keys() == list("abcdef")
    assert store.stats() == StoreStats(entries=6, bytes=1200, protected_bytes=1200)
    assert "more than max_size" in caplog.text
    # Once an hour at most: eviction may run after every stored file meanwhile.
    caplog.clear()
    put(store, "h", b"h" * 200)
    with caplog.at_level(logging.WARNING):
        store.evict()
    assert "more than max_size" not in caplog.text
    assert store.keys() == list("abcdef")


def test_settings_resolve_the_lockfile_store_and_bound_protect_days(tmp_path: Path) -> None:
    path = tmp_path / "cache.toml"
    base = 'public_url = "http://cache.test:8090"\ncache_dir = "cache"\n'
    path.write_text(base)
    settings = load_settings(path)
    assert settings.lockfile_dir is None
    assert settings.protect_days == 30
    assert lockfile_pins(settings) is None
    path.write_text(base + 'lockfile_dir = "worker/lockfiles"\nprotect_days = 7\n')
    settings = load_settings(path)
    assert settings.lockfile_dir == tmp_path / "worker" / "lockfiles"
    assert lockfile_pins(settings) is not None
    for days in (0, 3651):
        path.write_text(base + f"protect_days = {days}\n")
        with pytest.raises(Exception, match="protect_days"):
            load_settings(path)


def test_prune_keeps_pinned_files_unless_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfiles = tmp_path / "worker" / "lockfiles"
    lockfile(lockfiles, conda=(b"0" * 300,))
    path = tmp_path / "cache.toml"
    path.write_text(
        'public_url = "http://cache.test:8090"\ncache_dir = "cache"\nmax_size = "3000"\n'
        'max_artifact_size = "500"\nmax_metadata_size = "500"\n'
        'lockfile_dir = "worker/lockfiles"\n'
    )
    clock = Clock()
    clock.now = time.time() - 3600
    with Store(tmp_path / "cache", max_size=10**9, clock=clock) as store:
        for number in range(5):
            put(store, f"k{number}", str(number).encode() * 300)
            clock.now += 61
    assert cli.main(["stats", "--settings", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["protected_bytes"] == 300
    assert cli.main(["prune", "--settings", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["protected_bytes"] == 300
    with Store(tmp_path / "cache", max_size=3000) as store:
        assert store.keys() == ["k0", "k4"]
    path.write_text(path.read_text().replace('max_size = "3000"', 'max_size = "1500"'))
    assert cli.main(["prune", "--settings", str(path), "--include-pinned"]) == 0
    capsys.readouterr()
    with Store(tmp_path / "cache", max_size=1500) as store:
        assert store.keys() == ["k4"]
