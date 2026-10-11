# SPDX-License-Identifier: MIT
"""The command line."""

import json
import os
import time
from pathlib import Path

import pytest
from aiohttp import web

from sandbox_science.package_cache import cli
from sandbox_science.package_cache.adapter import Kind
from sandbox_science.package_cache.settings import Settings
from sandbox_science.package_cache.store import Store

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


def fill(root: Path, bodies: list[bytes]) -> None:
    """Store bodies as entries, each accessed a minute after the one before."""
    clock = Clock()
    with Store(root, max_size=10**9, clock=clock) as store:
        for number, body in enumerate(bodies):
            blob = store.write_temp(body)
            store.put(f"k{number}", blob, kind=Kind.ARTIFACT, content_type="application/x-test")
            clock.now += 61


def test_stats_reports_the_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = settings_file(tmp_path)
    fill(tmp_path / "cache", [b"a" * 10, b"b" * 20, b"a" * 10])
    assert cli.main(["stats", "--settings", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "entries": 3,
        "bytes": 30,
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
    assert cli.main(["prune", "--settings", str(settings_file(tmp_path, "1000"))]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "evicted_entries": 2,
        "deleted_blobs": 3,
        "freed_bytes": 606,
        "entries": 3,
        "bytes": 900,
        "max_size": 1000,
    }
    assert not old.exists()
    assert young.exists()
    with Store(root, max_size=1000) as store:
        assert store.keys() == ["k2", "k3", "k4"]


@pytest.mark.parametrize("command", ["stats", "prune"])
def test_stats_and_prune_need_an_existing_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    assert cli.main([command, "--settings", str(settings_file(tmp_path))]) == 1
    assert "no package cache" in capsys.readouterr().err
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("command", ["serve", "stats", "prune"])
def test_bad_settings_are_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    assert cli.main([command, "--settings", str(tmp_path / "missing.toml")]) == 1
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


def test_unknown_log_levels_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["serve", "--settings", str(settings_file(tmp_path)), "--log-level", "loud"])
