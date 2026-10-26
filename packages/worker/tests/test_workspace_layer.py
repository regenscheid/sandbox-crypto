# SPDX-License-Identifier: MIT
"""A workspace layer over a read-only base environment (docs/IMAGES.md, "The environment
outside the workspace"): where package commands record, what the lockfile lists, the tools'
environment, the wrappers' mirror, the binding inventory and the layout check. The process
test with a real layer is in test_provisioning.py."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from sandbox_science.contracts import Lockfile
from sandbox_science.worker import binding_inventory, provisioning, workspace_prefix
from sandbox_science.worker.executor import jupyter_path, tool_environment

PY = f"python{sys.version_info[0]}.{sys.version_info[1]}"


def _record(meta: Path, name: str, version: str, files: list[str]) -> dict[str, object]:
    record: dict[str, object] = {
        "name": name,
        "version": version,
        "build": "0",
        "url": f"https://conda.anaconda.org/conda-forge/noarch/{name}-{version}-0.conda",
        "sha256": "a" * 64,
        "files": files,
    }
    meta.mkdir(parents=True, exist_ok=True)
    (meta / f"{name}-{version}-0.json").write_text(json.dumps(record))
    return record


def _dist(site: Path, name: str, version: str, installer: str) -> None:
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n")
    (info / "INSTALLER").write_text(installer + "\n")


@pytest.fixture
def places(tmp_path: Path) -> provisioning.Places:
    """A base with two Conda packages and a distribution pip installed there, and a layer
    holding the base's records without files, one record a runtime remove dropped, one
    Conda package and two distributions installed at runtime."""
    base, layer = tmp_path / "base", tmp_path / "layer"
    below, site = base / "lib" / PY / "site-packages", layer / "lib" / PY / "site-packages"
    below.mkdir(parents=True)
    site.mkdir(parents=True)
    owned = f"lib/{PY}/site-packages/numpy-2.1.0.dist-info/METADATA"
    numpy = _record(base / "conda-meta", "numpy", "2.1.0", [owned])
    _record(base / "conda-meta", "removed", "1.0", [])
    _dist(below, "numpy", "2.1.0", "pip")  # Conda's, by its record's files
    _dist(below, "pdf_oxide", "0.3.78", "pip")
    _dist(below, "Shadowed.Name", "1.0", "pip")
    (layer / "conda-meta").mkdir()
    (layer / "conda-meta" / "numpy-2.1.0-0.json").write_text(json.dumps({**numpy, "files": []}))
    _record(layer / "conda-meta", "toolz", "1.0", [f"lib/{PY}/site-packages/toolz/__init__.py"])
    _dist(site, "tomli_w", "1.0", "pip")
    _dist(site, "shadowed-name", "2.0", "pip")
    return provisioning.Places(
        str(layer / "conda-meta"),
        (str(site),),
        None,
        str(base / "conda-meta"),
        (str(below),),
    )


def test_the_lockfile_lists_what_the_layers_interpreter_finds(
    places: provisioning.Places,
) -> None:
    lockfile = Lockfile.model_validate(provisioning.lockfile(places))
    # The layer's records, then the base's that a runtime remove left in the read-only base.
    assert sorted(package.name for package in lockfile.conda) == ["numpy", "removed", "toolz"]
    # The layer's distributions, then the base's not shadowed by one of the same name; a
    # Conda package's own distribution is not listed again.
    pip = sorted((item.name, item.version) for item in lockfile.pip)
    assert pip == [("pdf_oxide", "0.3.78"), ("shadowed-name", "2.0"), ("tomli_w", "1.0")]
    assert lockfile.skipped == 0


def test_the_fingerprint_is_the_layers_half_and_the_bases(
    places: provisioning.Places, tmp_path: Path
) -> None:
    before = provisioning.fingerprint(places)
    assert before[16:] == provisioning.base_identity(places)[:16]
    assert places.base_prefix == str(tmp_path / "base")
    # A change in the layer moves the first half only.
    _dist(Path(places.site[0]), "late", "1.0", "pip")
    after = provisioning.fingerprint(places)
    assert after[:16] != before[:16]
    assert after[16:] == before[16:]

    def rebuilt(name: str, extra: bool) -> provisioning.Places:
        """The same base built again elsewhere, with new file times, and maybe a package
        more."""
        copy = tmp_path / name
        shutil.copytree(tmp_path / "base", copy, copy_function=shutil.copy)
        if extra:
            _record(copy / "conda-meta", "mpfr", "4.2.1", [])
        below = str(copy / "lib" / PY / "site-packages")
        return provisioning.Places(
            places.conda_meta, places.site, None, str(copy / "conda-meta"), (below,)
        )

    # The same packages rebuilt: the same fingerprint, whatever their files' times.
    assert provisioning.fingerprint(rebuilt("again", False)) == after
    # Another package in the base moves the second half only.
    other = provisioning.fingerprint(rebuilt("other", True))
    assert other[:16] == after[:16]
    assert other[16:] != after[16:]


def test_a_failed_upgrade_lists_what_imports(places: provisioning.Places) -> None:
    """A transaction that removed the layer's record of a base package and linked the new
    version's distribution, then failed, leaves that distribution without a record: the
    lockfile lists it as it is, not the base's record."""
    site = Path(places.site[0])
    (Path(places.conda_meta) / "numpy-2.1.0-0.json").unlink()
    _dist(site, "numpy", "1.26.4", "conda")
    lockfile = Lockfile.model_validate(provisioning.lockfile(places))
    assert "numpy" not in [package.name for package in lockfile.conda]
    pip = {(item.name, item.version, item.installer) for item in lockfile.pip}
    assert ("numpy", "1.26.4", "conda") in pip
    # Nor does the layer get the base's record back while that distribution is there.
    layer = Path(places.conda_meta).parent
    assert provisioning.repair_layer(layer, Path(places.base_prefix or "")) == ["removed"]
    assert not (layer / "conda-meta" / "numpy-2.1.0-0.json").exists()


def test_a_removed_base_package_comes_back_to_the_layer(places: provisioning.Places) -> None:
    """Its files stay in the read-only base, importable, so its record comes back with them,
    and the package fingerprint reads as before."""
    layer, base = Path(places.conda_meta).parent, Path(places.base_prefix or "")
    (layer / "conda-meta" / "numpy-2.1.0-0.json").unlink()
    assert provisioning.repair_layer(layer, base) == ["numpy", "removed"]
    before = provisioning.fingerprint(places)
    (layer / "conda-meta" / "numpy-2.1.0-0.json").unlink()
    assert provisioning.repair_layer(layer, base) == ["numpy"]
    restored = json.loads((layer / "conda-meta" / "numpy-2.1.0-0.json").read_text())
    assert restored["files"] == []  # numpy has no program or library at the layer's top
    assert restored["version"] == "2.1.0"
    assert provisioning.fingerprint(places) == before
    assert provisioning.repair_layer(layer, base) == []


def test_places_travel_to_the_wrappers_and_an_earlier_configuration_still_reads(
    places: provisioning.Places,
) -> None:
    assert provisioning.Places.from_json(places.to_json()) == places
    earlier = {"conda_meta": "/p/conda-meta", "site": ["/p/site"], "user": None}
    whole = provisioning.Places.from_json(earlier)
    assert (whole.base_conda_meta, whole.base_site, whole.base_prefix) == (None, (), None)
    for malformed in ({**earlier, "base_site": "/b"}, {**earlier, "base_conda_meta": 3}):
        with pytest.raises(ValueError, match="malformed places"):
            provisioning.Places.from_json(malformed)


def test_an_interpreter_over_a_conda_base_is_a_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sys, "base_exec_prefix", str(base))
    # A developer's virtual environment over an interpreter that is no Conda environment.
    assert provisioning.layered_base() is None
    assert binding_inventory.layered_base() is None
    assert provisioning.Places.here().base_conda_meta is None
    (base / "conda-meta").mkdir()
    assert provisioning.layered_base() == str(base)
    assert binding_inventory.layered_base() == base
    here = provisioning.Places.here()
    assert here.base_conda_meta == str(base / "conda-meta")
    assert here.base_site
    assert all(Path(path).is_relative_to(base) for path in here.base_site)
    assert here.conda_meta == os.path.join(sys.prefix, "conda-meta")
    # A whole environment, such as a prefix seeded from an earlier image, is its own base.
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    assert provisioning.layered_base() is None


def test_tools_find_the_layer_then_the_base(tmp_path: Path) -> None:
    layer, base = tmp_path / "layer", tmp_path / "base"
    (layer / "conda-meta").mkdir(parents=True)
    env = {"PATH": f"/usr/bin:{base}/bin", "JUPYTER_PATH": f"/mine:{base}/share/jupyter"}
    found = tool_environment(env, str(layer / "bin" / "python"), str(layer), str(base))
    assert found["PATH"].split(os.pathsep) == [str(layer / "bin"), str(base / "bin"), "/usr/bin"]
    assert found["CONDA_PREFIX"] == str(layer)
    assert found["JUPYTER_PATH"].split(os.pathsep) == ["/mine", str(base / "share/jupyter")]
    assert jupyter_path({}, str(base)) == str(base / "share" / "jupyter")
    # Data that runtime installs bring, before the system's, which stay when it is unset.
    data = [str(layer / "share"), "/usr/local/share", "/usr/share"]
    assert found["XDG_DATA_DIRS"].split(os.pathsep) == data
    mine = tool_environment({"XDG_DATA_DIRS": "/x"}, str(layer / "python"), str(layer), str(base))
    assert mine["XDG_DATA_DIRS"].split(os.pathsep) == [str(layer / "share"), "/x"]
    whole = tool_environment({"PATH": "/usr/bin"}, str(layer / "bin" / "python"), str(layer))
    assert whole["PATH"].split(os.pathsep) == [str(layer / "bin"), "/usr/bin"]
    assert "JUPYTER_PATH" not in whole
    assert "XDG_DATA_DIRS" not in whole


def test_the_wrappers_mirror_lays_the_base_out_under_the_layer(tmp_path: Path) -> None:
    layer, base = tmp_path / "layer", tmp_path / "base"
    for prefix in (layer, base):
        (prefix / "bin").mkdir(parents=True)
        (prefix / "lib").mkdir()
        (prefix / "bin" / "shared").write_text("")
    (base / "bin" / "gp").write_text("")
    (base / "bin" / "pip").write_text("")
    (base / "share").mkdir()
    (base / "conda-meta").mkdir()
    state = provisioning.State(tmp_path / "state")
    places = provisioning.Places(str(layer / "conda-meta"), (), None, str(base / "conda-meta"), ())
    script = str(Path(provisioning.__file__))
    config = provisioning.Config(str(layer / "bin" / "python"), script, str(layer), places)
    assert provisioning.refresh(state, config, "") == []
    assert os.readlink(state.bin / "gp") == str(base / "bin" / "gp")
    assert os.readlink(state.bin / "shared") == str(layer / "bin" / "shared")
    assert os.readlink(state.mirror / "lib") == str(layer / "lib")
    assert os.readlink(state.mirror / "share") == str(base / "share")
    assert not (state.bin / "pip").is_symlink()  # a package manager gets a wrapper instead


def test_the_binding_inventory_scans_the_bases_records(tmp_path: Path) -> None:
    base, layer = tmp_path / "base", tmp_path / "layer"
    (base / "bin").mkdir(parents=True)
    tool = base / "bin" / "zebra"
    tool.write_text('#!/bin/sh\necho "${ZEBRA_INPUT}"\n')
    tool.chmod(0o755)
    record = _record(base / "conda-meta", "zebra", "1", ["bin/zebra"])
    (layer / "conda-meta").mkdir(parents=True)
    (layer / "conda-meta" / "zebra-1-0.json").write_text(json.dumps({**record, "files": []}))
    alone = binding_inventory.generate(layer, (), dpkg=tmp_path / "no-dpkg")
    assert alone["entries"] == []
    found = binding_inventory.generate(layer, (), dpkg=tmp_path / "no-dpkg", base=base)
    assert [entry["variable"] for entry in found["entries"]] == ["ZEBRA_INPUT"]
    assert found["entries"][0]["package"] == "conda:zebra@1:0"


@pytest.fixture
def layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """The layout prefix_layout.py's ``layer`` leaves, at test paths."""
    base = tmp_path / "base"
    (base / "conda-meta").mkdir(parents=True)
    (base / "bin").mkdir()
    (base / "bin" / PY).write_text("")
    (base / "bin" / "python").symlink_to(PY)
    prefix = tmp_path / ".sandbox-science-prefix"
    (prefix / "conda-meta").mkdir(parents=True)
    (prefix / "bin").mkdir()
    (prefix / "bin" / "python").symlink_to(base / "bin" / "python")
    (prefix / "pyvenv.cfg").write_text(
        f"home = {base}/bin\ninclude-system-site-packages = true\nversion = 3.14.8\n"
    )
    marker = {"version": 1, "base": str(base), "revision": "abc1234", "identity": "a" * 64}
    (prefix / workspace_prefix.LAYER_MARKER).write_text(json.dumps(marker) + "\n")
    alias = tmp_path / "compatibility-prefix"
    alias.symlink_to(prefix, target_is_directory=True)
    monkeypatch.setattr(workspace_prefix, "PREFIX", prefix)
    monkeypatch.setattr(workspace_prefix, "ALIAS", alias)
    monkeypatch.setattr(workspace_prefix, "BASE", base)
    monkeypatch.setattr(workspace_prefix, "FROZEN", tmp_path / "no-frozen-trees")
    return prefix, alias, base


def test_a_layer_over_the_images_base_gets_its_identity(layer: tuple[Path, Path, Path]) -> None:
    prefix, alias, base = layer
    found = workspace_prefix.report(str(alias), str(base))
    assert found["seeded"] == "a" * 64
    assert found["path"] == str(prefix)
    assert "identity" in found
    assert (prefix / workspace_prefix.MARKER).read_text() == found["identity"]


@pytest.mark.parametrize(
    "failure",
    [
        "marker-base",
        "marker-identity",
        "marker-link",
        "home",
        "system-site",
        "python",
        "running-base",
        "no-marker",
    ],
)
def test_a_layer_pointed_elsewhere_fails_the_layout(
    layer: tuple[Path, Path, Path], tmp_path: Path, failure: str
) -> None:
    """Research code can rewrite the layer; one over another environment would put other
    packages behind a lockfile that names them unchanged."""
    prefix, alias, base = layer
    other = tmp_path / "other"
    (other / "bin").mkdir(parents=True)
    (other / "bin" / "python").write_text("")
    running = str(base)
    config = prefix / "pyvenv.cfg"
    marker = prefix / workspace_prefix.LAYER_MARKER
    seeded = {"revision": "abc1234", "identity": "a" * 64}
    if failure == "marker-base":
        marker.write_text(json.dumps({"version": 1, "base": str(other), **seeded}))
    elif failure == "marker-identity":
        marker.write_text(json.dumps({"version": 1, "base": str(base), "identity": "A" * 64}))
    elif failure == "marker-link":
        (other / "marker").write_text(marker.read_text())
        marker.unlink()
        marker.symlink_to(other / "marker")
    elif failure == "home":
        config.write_text(config.read_text().replace(f"{base}/bin", f"{other}/bin"))
    elif failure == "system-site":
        config.write_text(config.read_text().replace("= true", "= false"))
    elif failure == "python":
        (prefix / "bin" / "python").unlink()
        (prefix / "bin" / "python").symlink_to(other / "bin" / "python")
    elif failure == "running-base":
        running = str(other)
    else:  # a virtual environment that is no layer
        marker.unlink()
    assert "error" in workspace_prefix.report(str(alias), running)
    assert not (prefix / workspace_prefix.MARKER).exists()


@pytest.mark.parametrize(("version", "accepted"), [(1, True), (2, True), (3, False)])
def test_the_frozen_manifest_of_either_layout_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int, accepted: bool
) -> None:
    """Images since the layer write version 2, which a worker before it refuses
    (docs/COMPATIBILITY.md); this worker reads both."""
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    manifest = {"version": version, "trees": []}
    (frozen / workspace_prefix.FROZEN_MANIFEST).write_text(json.dumps(manifest))
    monkeypatch.setattr(workspace_prefix, "FROZEN", frozen)
    monkeypatch.setattr(workspace_prefix, "PREFIX", tmp_path / "prefix")
    if accepted:
        workspace_prefix.check_frozen_links()
    else:
        with pytest.raises(OSError, match="malformed"):
            workspace_prefix.check_frozen_links()
