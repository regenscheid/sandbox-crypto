# SPDX-License-Identifier: MIT
"""The command line."""

import json
import logging
import os
import resource
import time
from pathlib import Path

import pytest
from aiohttp import web

from sandbox_science.package_cache import cli
from sandbox_science.package_cache.adapter import Kind
from sandbox_science.package_cache.app import KEEPALIVE_TIMEOUT
from sandbox_science.package_cache.settings import Settings
from sandbox_science.package_cache.store import ENTRY_OVERHEAD, Store

SETTINGS = """\
listen = "127.0.0.1:8099"
public_url = "http://cache.test:8099"
cache_dir = "cache"
max_size = "{max_size}"
max_artifact_size = "500"
max_metadata_size = "500"
"""


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def settings_file(tmp_path: Path, max_size: str = "1Mi") -> Path:
    path = tmp_path / "cache.toml"
    path.write_text(SETTINGS.format(max_size=max_size))
    return path


def fill(root: Path, bodies: list[bytes], *, status: int = 200, age: float = 0.0) -> None:
    """Store bodies as entries fetched ``age`` seconds ago, each a minute after the one before."""
    clock = Clock()
    clock.now -= age
    with Store(root, max_size=10**9, clock=clock) as store:
        for number, body in enumerate(bodies):
            blob = store.write_temp(body)
            kind = Kind.ARTIFACT if status == 200 else Kind.METADATA
            store.put(f"k{number}-{status}", blob, kind=kind, content_type="x/test", status=status)
            clock.now += 61


def test_stats_reports_the_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = settings_file(tmp_path)
    fill(tmp_path / "cache", [b"a" * 10, b"b" * 20, b"a" * 10])
    assert cli.main(["stats", "--settings", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "entries": 3,
        "bytes": 30,
        "protected_bytes": 0,
        "max_size": 1024**2,
    }


def test_prune_evicts_to_the_cap_and_deletes_old_orphans(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cache"
    fill(root, [bytes([number]) * 300 for number in range(5)])
    with Store(root, max_size=10**9) as store:
        old = store.blob_path("ab" * 32)
        young = store.blob_path("cd" * 32)
    for orphan in (old, young):
        orphan.parent.mkdir(exist_ok=True)
        orphan.write_bytes(b"orphan")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    # Each entry counts its 300 bytes and ENTRY_OVERHEAD: five are over the cap, and
    # eviction stops once three are left (90% of the cap).
    assert 5 * (300 + ENTRY_OVERHEAD) > 5000 >= 3 * (300 + ENTRY_OVERHEAD) / 0.9
    assert cli.main(["prune", "--settings", str(settings_file(tmp_path, "5000"))]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "evicted_entries": 2,
        "deleted_blobs": 3,
        "freed_bytes": 606,
        "entries": 3,
        "bytes": 900,
        "protected_bytes": 0,
        "max_size": 5000,
    }
    assert not old.exists()
    assert young.exists()
    with Store(root, max_size=5000) as store:
        assert store.keys() == ["k2-200", "k3-200", "k4-200"]


def test_prune_purges_negative_entries_past_their_use(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cache"
    # stale_if_error is a day: a negative entry is of no use a minute after that.
    fill(root, [b"gone"] * 3, status=404, age=86400 + 60 + 3600)
    fill(root, [b"missing"] * 2, status=410, age=3600)
    assert cli.main(["prune", "--settings", str(settings_file(tmp_path))]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["evicted_entries"], report["deleted_blobs"], report["freed_bytes"]) == (3, 1, 4)
    with Store(root, max_size=10**9) as store:
        assert store.keys() == ["k0-410", "k1-410"]


def test_forget_drops_one_entry_by_its_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cache"
    fill(root, [b"a bad copy", b"kept"])
    assert cli.main(["forget", "--settings", str(settings_file(tmp_path)), "k0-200"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "key": "k0-200",
        "deleted_blobs": 1,
        "freed_bytes": 10,
    }
    with Store(root, max_size=10**9) as store:
        assert store.keys() == ["k1-200"]


def test_forget_finds_the_key_of_a_request_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The request log shows paths; the adapters turn them into the store's keys.
    path = settings_file(tmp_path)
    path.write_text(path.read_text() + "[hackage]\n")
    root = tmp_path / "cache"
    key = "hackage:hackage/package/split-0.2.5.tar.gz"
    with Store(root, max_size=10**9) as store:
        store.put(key, store.write_temp(b"\x1f\x8b bad"), kind=Kind.ARTIFACT, content_type="x/y")
    for request in ("/hackage/package/split-0.2.5/split-0.2.5.tar.gz", "/hackage/x"):
        status = cli.main(["forget", "--settings", str(path), request])
        assert status == (0 if "split" in request else 1)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["key"] == key
    assert "the cache stores nothing for /hackage/x" in captured.err
    with Store(root, max_size=10**9) as store:
        assert store.keys() == []


def test_forgetting_a_missing_key_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fill(tmp_path / "cache", [b"kept"])
    assert cli.main(["forget", "--settings", str(settings_file(tmp_path)), "missing"]) == 1
    assert "no entry has the key 'missing'" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["stats", "prune", "forget"])
def test_stats_prune_and_forget_need_an_existing_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    key = ["k0-200"] if command == "forget" else []
    assert cli.main([command, "--settings", str(settings_file(tmp_path)), *key]) == 1
    assert "no package cache" in capsys.readouterr().err
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("command", ["serve", "stats", "prune", "forget"])
def test_bad_settings_are_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    key = ["k0-200"] if command == "forget" else []
    assert cli.main([command, "--settings", str(tmp_path / "missing.toml"), *key]) == 1
    assert "cannot read settings" in capsys.readouterr().err


def test_serve_runs_the_app_on_the_listen_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[tuple[web.Application, dict[str, object]]] = []

    def run_app(app: web.Application, **options: object) -> None:
        runs.append((app, options))

    def build_adapters(settings: Settings) -> dict[str, object]:
        assert settings.listen_port == 8099
        return {}

    monkeypatch.setattr(cli, "build_adapters", build_adapters)
    monkeypatch.setattr(web, "run_app", run_app)
    path = settings_file(tmp_path)
    assert cli.main(["serve", "--settings", str(path), "--log-level", "warning"]) == 0
    [(app, options)] = runs
    assert isinstance(app, web.Application)
    assert (options["host"], options["port"], options["access_log"]) == ("127.0.0.1", 8099, None)
    assert options["keepalive_timeout"] == KEEPALIVE_TIMEOUT == 15.0


@pytest.mark.parametrize(("soft", "warned"), [(1024, True), (65536, False)])
def test_serve_warns_when_the_open_file_limit_is_low(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    soft: int,
    warned: bool,
) -> None:
    def getrlimit(which: int) -> tuple[int, int]:
        assert which == resource.RLIMIT_NOFILE
        return soft, 524288

    monkeypatch.setattr(resource, "getrlimit", getrlimit)
    monkeypatch.setattr(cli, "build_adapters", lambda settings: {})
    monkeypatch.setattr(web, "run_app", lambda app, **options: None)
    caplog.set_level(logging.WARNING, logger="sandbox_science.package_cache")
    assert cli.main(["serve", "--settings", str(settings_file(tmp_path))]) == 0
    warnings = [
        record.getMessage() for record in caplog.records if "open file" in record.getMessage()
    ]
    assert bool(warnings) is warned
    if warned:
        assert "max_requests = 1024 may need 5120" in warnings[0]
        assert "LimitNOFILE" in warnings[0]


def test_unknown_log_levels_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["serve", "--settings", str(settings_file(tmp_path)), "--log-level", "loud"])


def test_serve_sets_up_the_shared_logging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_up: list[tuple[str, object]] = []
    monkeypatch.setattr(
        cli, "configure_logging", lambda service, **kw: set_up.append((service, kw))
    )
    monkeypatch.setattr(cli, "_serve", lambda settings: 0)
    argv = ["serve", "--settings", str(settings_file(tmp_path)), "--log-level", "debug"]
    assert cli.main(argv) == 0
    assert set_up == [("cache", {"level": "DEBUG"})]
