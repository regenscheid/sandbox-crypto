# SPDX-License-Identifier: MIT
"""Files that environments' lockfiles pin are protected from eviction, up to a ceiling."""

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sandbox_science.contracts.revisions import EnvironmentRevision, LockfileRef
from sandbox_science.package_cache import cli
from sandbox_science.package_cache.adapter import Kind
from sandbox_science.package_cache.errors import SettingsError
from sandbox_science.package_cache.pins import CURRENT, GRACE, LockfilePins, lockfile_pins
from sandbox_science.package_cache.settings import load_settings
from sandbox_science.package_cache.store import ACCESS_INTERVAL, PinRank, Removal, Store, StoreStats
from sandbox_science.worker.store import RevisionRecord
from sandbox_science.worker.store import Store as WorkerStore

DAY = 86400.0
NOW = 1_000_000_000.0
FINGERPRINT = "a" * 32


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class Worker:
    """A worker state directory written through the worker's own store: the records the
    cache reads, so a change to the worker's schema fails here."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        self.store = WorkerStore(state_dir / "worker.sqlite3")
        self.revisions: dict[str, int] = {}

    def lockfile(
        self, *, conda: tuple[bytes, ...] = (), pip: tuple[bytes, ...] = ()
    ) -> LockfileRef:
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
        path = self.state_dir / "lockfiles" / "blobs" / sha(data)[:2] / sha(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return LockfileRef(sha256=sha(data), size=len(data))

    def revise(self, environment: str, lockfile: LockfileRef | None, recorded: float) -> int:
        """Record the environment's next revision, now its current one."""
        revision = self.revisions.get(environment, -1) + 1
        self.revisions[environment] = revision
        entry = EnvironmentRevision(
            revision=revision,
            fingerprint=FINGERPRINT,
            recorded_at=datetime.fromtimestamp(recorded, UTC),
            lockfile=lockfile,
        )
        self.store.add_revision(environment, RevisionRecord(revision, FINGERPRINT, ""), entry)
        return revision


@pytest.fixture
def worker(tmp_path: Path) -> Iterator[Worker]:
    worker = Worker(tmp_path / "worker")
    yield worker
    worker.store.close()


def pins_of(worker: Worker, clock: Clock | None = None, **options: object) -> LockfilePins:
    settings: dict[str, object] = {"protect_days": 7, "clock": clock or Clock(), "refresh": 0}
    settings.update(options)
    return LockfilePins(worker.state_dir, **settings)  # pyright: ignore[reportArgumentType]


def test_current_lockfiles_pin_their_conda_and_pip_digests(worker: Worker) -> None:
    worker.revise("env-1", worker.lockfile(conda=(b"numpy",), pip=(b"wheel",)), NOW - 90 * DAY)
    assert pins_of(worker)() == {
        sha(b"numpy"): (CURRENT, NOW - 90 * DAY),
        sha(b"wheel"): (CURRENT, NOW - 90 * DAY),
    }


def test_a_superseded_lockfile_pins_for_exactly_protect_days(worker: Worker) -> None:
    worker.revise("env-1", worker.lockfile(conda=(b"old",)), NOW - 30 * DAY)
    worker.revise("env-1", worker.lockfile(conda=(b"new",)), NOW - 7 * DAY)
    assert pins_of(worker)() == {
        sha(b"old"): (GRACE, NOW - 7 * DAY),
        sha(b"new"): (CURRENT, NOW - 7 * DAY),
    }
    assert set(pins_of(worker, Clock(NOW + 1))()) == {sha(b"new")}


def test_a_known_good_lockfile_pins_while_recovery_needs_it(worker: Worker) -> None:
    good = worker.revise("env-1", worker.lockfile(conda=(b"good",)), NOW - 60 * DAY)
    worker.revise("env-1", worker.lockfile(conda=(b"broken",)), NOW - 59 * DAY)
    worker.store.set_package_status("env-1", "needs_recovery", "provision-1", good)
    assert set(pins_of(worker)()) == {sha(b"good"), sha(b"broken")}


def test_a_lockfile_an_environment_returns_to_pins_again(worker: Worker) -> None:
    same = worker.lockfile(conda=(b"same",))
    worker.revise("env-1", same, NOW - 90 * DAY)
    worker.revise("env-1", worker.lockfile(conda=(b"other",)), NOW - 80 * DAY)
    worker.revise("env-1", same, NOW - 70 * DAY)
    assert pins_of(worker)() == {sha(b"same"): (CURRENT, NOW - 70 * DAY)}


def test_without_records_or_store_nothing_is_pinned_and_it_is_logged(
    tmp_path: Path, worker: Worker, caplog: pytest.LogCaptureFixture
) -> None:
    missing = LockfilePins(tmp_path / "nowhere", protect_days=7, refresh=0)
    with caplog.at_level(logging.WARNING):
        assert missing() == {}
    assert "cannot read the worker's records" in caplog.text
    caplog.clear()
    damaged = tmp_path / "damaged"
    damaged.mkdir()
    (damaged / "worker.sqlite3").write_bytes(b"not a database")
    with caplog.at_level(logging.WARNING):
        assert LockfilePins(damaged, protect_days=7, refresh=0)() == {}
    assert "cannot read the worker's records" in caplog.text
    caplog.clear()
    worker.revise("env-1", LockfileRef(sha256="c" * 64, size=10), NOW)
    with caplog.at_level(logging.WARNING):
        assert pins_of(worker)() == {}
    assert "lockfile store" in caplog.text
    assert "missing or unreadable" in caplog.text


def test_unreadable_lockfiles_pin_nothing_and_are_logged(
    worker: Worker, caplog: pytest.LogCaptureFixture
) -> None:
    worker.revise("env-1", worker.lockfile(conda=(b"kept",)), NOW)
    blobs = worker.state_dir / "lockfiles" / "blobs"
    bad = "d" * 64
    (blobs / bad[:2]).mkdir()
    (blobs / bad[:2] / bad).write_text("not json")
    worker.revise("env-2", LockfileRef(sha256=bad, size=8), NOW)
    worker.revise("env-3", LockfileRef(sha256="e" * 64, size=8), NOW)  # absent
    with caplog.at_level(logging.WARNING):
        assert set(pins_of(worker)()) == {sha(b"kept")}
    assert "2 lockfiles" in caplog.text


def test_the_read_limit_prefers_current_and_known_good_lockfiles(
    worker: Worker, caplog: pytest.LogCaptureFixture
) -> None:
    worker.revise("env-1", worker.lockfile(conda=(b"superseded",)), NOW - 2 * DAY)
    worker.revise("env-1", worker.lockfile(conda=(b"current-1",)), NOW - DAY)
    worker.revise("env-2", worker.lockfile(conda=(b"current-2",)), NOW - 3 * DAY)
    with caplog.at_level(logging.WARNING):
        pinned = pins_of(worker, max_lockfiles=2)()
    assert set(pinned) == {sha(b"current-1"), sha(b"current-2")}
    assert "only the 2 best ranked" in caplog.text


def test_a_reading_is_reused_until_refresh(worker: Worker) -> None:
    clock = Clock()
    pins = pins_of(worker, clock, refresh=60)
    worker.revise("env-1", worker.lockfile(conda=(b"first",)), NOW)
    assert set(pins()) == {sha(b"first")}
    worker.revise("env-2", worker.lockfile(conda=(b"second",)), NOW)
    assert set(pins()) == {sha(b"first")}
    clock.now += 60
    assert set(pins()) == {sha(b"first"), sha(b"second")}


class FixedPins:
    """Pins of the store's tests: given ranks, and how often the store asked."""

    def __init__(self, pinned: dict[bytes, PinRank]) -> None:
        self.pinned = {sha(body): rank for body, rank in pinned.items()}
        self.calls = 0

    def __call__(self) -> dict[str, PinRank]:
        self.calls += 1
        return self.pinned


def body(name: str) -> bytes:
    return name.encode() * 200


@pytest.fixture
def clock() -> Clock:
    return Clock()


def fill(store: Store, clock: Clock, names: str) -> None:
    for name in names:
        store.put(name, store.write_temp(body(name)), kind=Kind.ARTIFACT, content_type="x/test")
        clock.now += ACCESS_INTERVAL + 1


def open_store(tmp_path: Path, clock: Clock, pins: FixedPins | None) -> Store:
    """Cap 1000 bytes, no entry overhead; pinned files protected up to 500 bytes."""
    return Store(tmp_path / "cache", max_size=1000, clock=clock, entry_overhead=0, pins=pins)


def test_without_pins_eviction_removes_the_least_recently_used(
    tmp_path: Path, clock: Clock
) -> None:
    with open_store(tmp_path, clock, None) as store:
        fill(store, clock, "abcdef")
        assert store.evict() == Removal(entries=2, blobs=2, bytes=400)
        assert store.keys() == ["c", "d", "e", "f"]
        assert store.stats() == StoreStats(entries=4, bytes=800)


def test_below_the_ceiling_nothing_pinned_is_evicted(tmp_path: Path, clock: Clock) -> None:
    pins = FixedPins({body("a"): (CURRENT, 1.0), body("b"): (GRACE, 2.0)})
    with open_store(tmp_path, clock, pins) as store:
        fill(store, clock, "abcdef")
        assert store.protect_limit == 500
        assert store.stats() == StoreStats(
            entries=6, bytes=1200, pinned_bytes=400, protected_bytes=400
        )
        assert store.evict() == Removal(entries=2, blobs=2, bytes=400)
        assert store.keys() == ["a", "b", "e", "f"]
        # Pins are read once per eviction pass, not per batch.
        calls = pins.calls
        store.evict()
        assert pins.calls == calls


def test_pins_past_the_ceiling_are_evicted_oldest_pinned_first(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    pins = FixedPins(
        {
            body("a"): (CURRENT, 10.0),
            body("b"): (CURRENT, 20.0),
            body("c"): (GRACE, 50.0),
            body("d"): (CURRENT, 30.0),
        }
    )
    with open_store(tmp_path, clock, pins) as store:
        fill(store, clock, "abcdef")
        assert store.stats() == StoreStats(
            entries=6, bytes=1200, pinned_bytes=800, protected_bytes=400
        )
        with caplog.at_level(logging.WARNING):
            assert store.evict() == Removal(entries=2, blobs=2, bytes=400)
        # d and b rank best (current, most recent); a and c are past the 500-byte ceiling.
        assert store.keys() == ["b", "d", "e", "f"]
        assert "more than the protection ceiling of 500" in caplog.text
        assert "evicting the 400 bytes" in caplog.text
        assert store.stats().protected_bytes == store.stats().pinned_bytes == 400


def test_pins_alone_cannot_hold_the_store_over_its_cap(tmp_path: Path, clock: Clock) -> None:
    pins = FixedPins({body(name): (CURRENT, float(index)) for index, name in enumerate("abcdefg")})
    with open_store(tmp_path, clock, pins) as store:
        fill(store, clock, "abcdefg")
        store.evict()
        assert store.charged_size <= 500
        assert store.keys() == ["f", "g"]


def test_include_pinned_evicts_by_access_alone(tmp_path: Path, clock: Clock) -> None:
    pins = FixedPins({body("a"): (CURRENT, 1.0), body("b"): (CURRENT, 1.0)})
    with open_store(tmp_path, clock, pins) as store:
        fill(store, clock, "abcdef")
        assert store.evict(include_pinned=True) == Removal(entries=2, blobs=2, bytes=400)
        assert store.keys() == ["c", "d", "e", "f"]


@pytest.mark.parametrize("share", [0.0, -0.1, 1.5])
def test_the_share_must_be_a_fraction(tmp_path: Path, share: float) -> None:
    with pytest.raises(ValueError, match="protect_share"):
        Store(tmp_path / "cache", max_size=1000, protect_share=share)


def test_settings_resolve_the_worker_state_and_bound_the_grace_and_share(tmp_path: Path) -> None:
    path = tmp_path / "cache.toml"
    base = 'public_url = "http://cache.test:8090"\ncache_dir = "cache"\n'
    path.write_text(base)
    settings = load_settings(path)
    assert settings.worker_state_dir is None
    assert (settings.protect_days, settings.protect_share) == (7, 0.5)
    assert lockfile_pins(settings) is None
    path.write_text(base + 'worker_state_dir = "worker"\nprotect_days = 0\nprotect_share = 1\n')
    settings = load_settings(path)
    assert settings.worker_state_dir == tmp_path / "worker"
    assert lockfile_pins(settings) is not None
    for line in (
        "protect_days = 3651",
        "protect_days = -1",
        "protect_share = 0",
        "protect_share = 1.5",
    ):
        path.write_text(base + line + "\n")
        with pytest.raises(SettingsError, match=line.split()[0]):
            load_settings(path)


def test_prune_protects_pinned_files_unless_asked(
    tmp_path: Path, worker: Worker, capsys: pytest.CaptureFixture[str]
) -> None:
    worker.revise("env-1", worker.lockfile(conda=(b"0" * 300,)), time.time())
    path = tmp_path / "cache.toml"
    path.write_text(
        'public_url = "http://cache.test:8090"\ncache_dir = "cache"\nmax_size = "3000"\n'
        'max_artifact_size = "500"\nmax_metadata_size = "500"\nworker_state_dir = "worker"\n'
    )
    clock = Clock(time.time() - 3600)
    with Store(tmp_path / "cache", max_size=10**9, clock=clock) as store:
        for number in range(5):
            store.put(
                f"k{number}",
                store.write_temp(str(number).encode() * 300),
                kind=Kind.ARTIFACT,
                content_type="x/test",
            )
            clock.now += 61
    assert cli.main(["stats", "--settings", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["pinned_bytes"], report["protected_bytes"], report["protect_limit"]) == (
        300,
        300,
        1500,
    )
    assert cli.main(["prune", "--settings", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["protected_bytes"] == 300
    with Store(tmp_path / "cache", max_size=3000) as store:
        assert store.keys() == ["k0", "k4"]
    path.write_text(path.read_text().replace('max_size = "3000"', 'max_size = "1500"'))
    assert cli.main(["prune", "--settings", str(path), "--include-pinned"]) == 0
    capsys.readouterr()
    with Store(tmp_path / "cache", max_size=1500) as store:
        assert store.keys() == ["k4"]
