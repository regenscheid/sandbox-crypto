# SPDX-License-Identifier: MIT
"""Static inventory provenance, determinism, invalidation and binding admission."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sandbox_science.worker import binding_inventory as inventory


def package(root: Path, version: str = "1", variable: str = "ZEBRA_INPUT") -> None:
    (root / "conda-meta").mkdir(exist_ok=True, parents=True)
    (root / "bin").mkdir(exist_ok=True)
    code = root / "bin" / "zebra"
    code.write_text(f'#!/bin/sh\necho "${{{variable}}}"\nexit 99\n')
    code.chmod(0o755)
    (root / "conda-meta" / "zebra.json").write_text(
        json.dumps(
            {
                "name": "zebra",
                "version": version,
                "build": "0",
                "files": ["bin/zebra"],
            }
        )
    )


def test_inventory_is_deterministic_and_records_static_source_provenance(tmp_path: Path) -> None:
    package(tmp_path)
    first = inventory.generate(tmp_path, (), dpkg=tmp_path / "no-dpkg")
    second = inventory.generate(tmp_path, (), dpkg=tmp_path / "no-dpkg")
    assert inventory.canonical(first) == inventory.canonical(second)
    (entry,) = first["entries"]
    assert entry == {
        "variable": "ZEBRA_INPUT",
        "package": "conda:zebra@1:0",
        "source": "bin/zebra",
        "sha256": hashlib.sha256((tmp_path / "bin/zebra").read_bytes()).hexdigest(),
        "rule": "literal-read-v1",
    }
    assert first["namespaces"] == ["ZEBRA_"]
    assert first["command_sources"][0]["package"] == "conda:zebra@1:0"
    body = {k: v for k, v in first.items() if k != "digest"}
    assert first["digest"] == hashlib.sha256(inventory.canonical(body)).hexdigest()
    package(tmp_path, "2", "ZEBRA_DATA")
    changed = inventory.generate(tmp_path, (), dpkg=tmp_path / "no-dpkg")
    assert changed["digest"] != first["digest"]
    assert changed["entries"][0]["package"] == "conda:zebra@2:0"
    assert changed["entries"][0]["variable"] == "ZEBRA_DATA"


def test_package_cache_invalidates_before_failed_or_unstable_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package(tmp_path)
    generate = inventory.generate
    monkeypatch.setattr(inventory, "generate", lambda p, s: generate(p, s, dpkg=tmp_path / "dpkg"))
    cached = inventory.Inventory()
    identity = ["one"]
    destination = tmp_path / "saved.json"
    first = cached.current(tmp_path, (), lambda: identity[0], destination)
    assert cached.projection(["ZEBRA_INPUT", "ZEBRA_", "QZPACK_DIR"], 1)["reserved"] == [
        "ZEBRA_INPUT"
    ]
    assert cached.projection(["ZEBRA_"], 1)["namespaces"] == ["ZEBRA_"]
    cached.require({"QZPACK_DIR"})
    with pytest.raises(inventory.InventoryError, match="reserved tool variables"):
        cached.require({"ZEBRA_INPUT"})
    package(tmp_path, "2", "OTHER_INPUT")
    identity[0] = "two"
    second = cached.current(tmp_path, (), lambda: identity[0], destination)
    assert first["digest"] != second["digest"]
    cached.require({"ZEBRA_INPUT"})
    with pytest.raises(inventory.InventoryError):
        cached.require({"OTHER_INPUT"})
    identity[0] = "three"
    (tmp_path / "conda-meta/zebra.json").write_text("broken")
    with pytest.raises(inventory.InventoryError):
        cached.current(tmp_path, (), lambda: identity[0], destination)
    with pytest.raises(inventory.InventoryError, match="unavailable"):
        cached.require({"QZPACK_DIR"})
    package(tmp_path)
    changing = iter(["four", "five"])
    with pytest.raises(inventory.InventoryError, match="packages changed"):
        cached.current(tmp_path, (), lambda: next(changing), destination)
    with pytest.raises(inventory.InventoryError):
        cached.require({"QZPACK_DIR"})


def test_bounds_refuse_generation_and_external_symlinks_are_never_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package(tmp_path)
    outside = tmp_path.parent / "outside-helper"
    outside.write_text('echo "$ESCAPED_VALUE"')
    (tmp_path / "bin/zebra").unlink()
    (tmp_path / "bin/zebra").symlink_to(outside)
    found = inventory.generate(tmp_path, (), dpkg=tmp_path / "dpkg")
    assert found["entries"] == []
    assert any("outside package root" in item for item in found["coverage"])
    (tmp_path / "bin/zebra").unlink()
    package(tmp_path)
    monkeypatch.setattr(inventory, "MAX_TOTAL", 1)
    with pytest.raises(inventory.InventoryError, match="total program bytes"):
        inventory.generate(tmp_path, (), dpkg=tmp_path / "dpkg")


def test_literals_and_computed_access_coverage(tmp_path: Path) -> None:
    data = (
        b'import os\nos.getenv("EXACT_VALUE")\nos.environ["DIRECT_VALUE"]\n'
        b'os.environ.get("OTHER_VALUE")\nos.getenv("DYNAMIC_" + str(1))\n'
    )
    assert {"EXACT_VALUE", "DIRECT_VALUE", "OTHER_VALUE"} <= inventory.variable_reads(data, ".py")
    package(tmp_path)
    found = inventory.generate(tmp_path, (), dpkg=tmp_path / "dpkg")
    assert any("computed" in note for note in found["coverage"])


def test_reviewed_families_have_versioned_metadata_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package(tmp_path)
    meta = tmp_path / "conda-meta/zebra.json"
    meta.write_text(meta.read_text().replace('"zebra"', '"python"'))
    generate = inventory.generate
    monkeypatch.setattr(inventory, "generate", lambda p, s: generate(p, s, dpkg=tmp_path / "dpkg"))
    cached = inventory.Inventory()
    cached.current(tmp_path, (), lambda: "version1", tmp_path / "manifest.json")
    from sandbox_science.contracts.worker import BindingInventory

    projection = BindingInventory.model_validate(cached.projection(["PYTHON_CUSTOM"], 0))
    assert projection.reserved == ("PYTHON_CUSTOM",)
    (proof,) = projection.provenance
    assert proof.package == "conda:python@1:0"
    assert proof.source == "conda-meta/zebra.json"
    assert proof.sha256 == hashlib.sha256(meta.read_bytes()).hexdigest()


def test_metadata_symlinks_and_special_files_never_redirect_or_block_scanning(
    tmp_path: Path,
) -> None:
    package(tmp_path)
    meta = tmp_path / "conda-meta/zebra.json"
    outside = tmp_path.parent / "external-metadata"
    outside.write_bytes(meta.read_bytes())
    meta.unlink()
    meta.symlink_to(outside)
    with pytest.raises(inventory.InventoryError, match="escapes"):
        inventory.generate(tmp_path, (), dpkg=tmp_path / "dpkg")
    meta.unlink()
    import os

    os.mkfifo(meta)
    with pytest.raises(inventory.InventoryError, match="bounded regular file"):
        inventory.generate(tmp_path, (), dpkg=tmp_path / "dpkg")
