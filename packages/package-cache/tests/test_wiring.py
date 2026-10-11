# SPDX-License-Identifier: MIT
"""Wiring the ecosystem adapters to the settings."""

from pathlib import Path

from sandbox_science.package_cache.settings import Settings
from sandbox_science.package_cache.wiring import build_adapters


def settings(tmp_path: Path, **tables: object) -> Settings:
    return Settings.model_validate(
        {"public_url": "http://cache.test:8090", "cache_dir": tmp_path / "cache", **tables},
        strict=False,
    )


def test_every_enabled_ecosystem_gets_its_adapter(tmp_path: Path) -> None:
    adapters = build_adapters(settings(tmp_path, pypi={}, conda={}, cargo={}, go={}, npm={}))
    assert sorted(adapters) == ["cargo", "conda", "go", "npm", "pypi"]
    assert all(adapter.prefix == prefix for prefix, adapter in adapters.items())


def test_ecosystems_without_a_table_are_disabled(tmp_path: Path) -> None:
    assert sorted(build_adapters(settings(tmp_path, pypi={}, npm={}))) == ["npm", "pypi"]
    assert build_adapters(settings(tmp_path)) == {}


def test_rewritten_links_point_at_the_public_url(tmp_path: Path) -> None:
    adapters = build_adapters(settings(tmp_path, cargo={}))
    route = adapters["cargo"].route("index/config.json", {})
    assert route is not None
    assert b"http://cache.test:8090/cargo/dl/" in getattr(route, "body", b"")
