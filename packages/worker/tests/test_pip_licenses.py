# SPDX-License-Identifier: MIT
"""Installed metadata files and retained synthetic blobs; no installers or network."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from sandbox_science.contracts import Lockfile, LockfileRef, PipDistribution
from sandbox_science.contracts.lockfile_diffs import LockfileDiffPage, LockfileSelection
from sandbox_science.contracts.revision_packages import RevisionPackagePage
from sandbox_science.worker import lockfile_diffs, provisioning
from sandbox_science.worker.artifacts import Blobs

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def metadata(tmp_path: Path, headers: bytes, *, kind: str = "dist-info") -> provisioning.Places:
    site = tmp_path / "site"
    site.mkdir()
    path = site / f"example-1.{kind}"
    body = b"Metadata-Version: 2.4\nName: example\nVersion: 1\n" + headers
    if kind == "egg-info-file":
        path = site / "example-1.egg-info"
        path.write_bytes(body)
    else:
        path.mkdir()
        (path / ("METADATA" if kind == "dist-info" else "PKG-INFO")).write_bytes(body)
        (path / "INSTALLER").write_text("uv\n")
    return provisioning.Places(str(tmp_path / "conda-meta"), (str(site),), None)


def package(places: provisioning.Places) -> PipDistribution:
    lock = Lockfile.model_validate(provisioning.lockfile(places))
    assert len(lock.pip) == 1
    assert lock.skipped == 0
    return lock.pip[0]


@pytest.mark.parametrize("kind", ["dist-info", "egg-info", "egg-info-file"])
def test_reads_installed_declaration_without_importing_distribution(tmp_path: Path, kind: str):
    places = metadata(tmp_path, b"License-Expression: MIT OR Apache-2.0\n\n", kind=kind)
    site = Path(places.site[0])
    marker = tmp_path / "must-not-execute"
    (site / "example.py").write_text(f"raise RuntimeError({str(marker)!r})")
    (site / "setup.py").write_text(f"open({str(marker)!r}, 'w').write('executed')")
    (site / "hostile.pth").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()")
    value = package(places)
    assert value.license == "MIT OR Apache-2.0"
    assert value.version == "1"
    assert not marker.exists()


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (b"License-Expression: MIT\nLicense: AGPL-3.0\n\n", "MIT"),
        (b"lIcEnSe-ExPrEsSiOn: (MIT OR\r\n Apache-2.0)\r\n\r\n", "(MIT OR Apache-2.0)"),
        (b"License: BSD-3-Clause\n\n", "BSD-3-Clause"),
        (
            b"Classifier: Topic :: Science\nClassifier: License :: OSI Approved :: MIT License\n\n",
            "License :: OSI Approved :: MIT License",
        ),
        (b"License: " + b"L" * 256 + b"\n\n", "L" * 256),
        (b"\nLicense-Expression: AGPL-3.0\n", None),
        (b"License-File: ../../private\n\n", None),
        (b"License-Expression:\nLicense: MIT\n\n", None),
        (b"License-Expression: UNKNOWN\nLicense: MIT\n\n", None),
        (b"License-Expression: MIT\nlicense-expression: MIT\n\n", None),
        (b"License: MIT\nLicense: Apache-2.0\n\n", None),
        (b"Classifier: License :: MIT\nClassifier: License :: BSD\n\n", None),
        (b"License-Expression: " + b"L" * 257 + b"\nLicense: MIT\n\n", None),
        (b"License: UNKNOWN\n\n", None),
        (b"License: MIT\x00hidden\n\n", None),
        (b"License: MIT\x7f\n\n", None),
        (b"License: MIT\tOR BSD\n\n", None),
        (b"License: \xc3\xa9\n\n", None),
        (b"License: MIT\nDescription: \xff\n\n", None),
        (b"License: MIT\nbroken header\n\n", None),
        (b"License: " + b"L" * (64 * 1024) + b"\n\n", None),
    ],
)
def test_declared_headers_are_bounded_unambiguous_and_do_not_use_body(
    tmp_path: Path, headers: bytes, expected: str | None
):
    assert package(metadata(tmp_path, headers)).license == expected


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_final_metadata_file_is_regular_nofollow_and_nonblocking(tmp_path: Path, kind: str):
    places = metadata(tmp_path, b"License: MIT\n\n")
    path = Path(places.site[0]) / "example-1.dist-info" / "METADATA"
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("Name: example\nVersion: 1\nLicense: must-not-be-read\n\n")
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    value = package(places)
    assert value.name == "example"  # Existing name/version fallback is preserved.
    assert value.license is None


def test_license_file_path_is_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    places = metadata(tmp_path, b"License-Expression: MIT\nLicense-File: ../../private\n\n")
    opened: list[str] = []
    original = provisioning.read_regular

    def read(path: str, cap: int) -> bytes | None:
        opened.append(path)
        assert "private" not in path
        return original(path, cap)

    monkeypatch.setattr(provisioning, "read_regular", read)
    assert package(places).license == "MIT"
    assert all(Path(path).name in {"METADATA", "INSTALLER", "direct_url.json"} for path in opened)


def retain(blobs: Blobs, raw: bytes, revision: int) -> LockfileSelection:
    digest = hashlib.sha256(raw).hexdigest()
    writer = blobs.writer()
    writer.write(raw)
    assert writer.commit(digest)
    return LockfileSelection(
        revision=revision,
        record_sha256=f"{revision:064x}",
        lockfile=LockfileRef(sha256=digest, size=len(raw), pip_distributions=1),
    )


def test_snapshot_retained_inventory_and_license_only_diff_are_coherent(tmp_path: Path):
    places = metadata(tmp_path, b"License-Expression: MIT\n\n")
    before = Lockfile.model_validate(provisioning.lockfile(places)).model_dump_json().encode()
    source = Path(places.site[0]) / "example-1.dist-info" / "METADATA"
    source.write_text(
        "Metadata-Version: 2.4\nName: example\nVersion: 1\nLicense-Expression: AGPL-3.0-only\n\n"
    )
    after = Lockfile.model_validate(provisioning.lockfile(places)).model_dump_json().encode()
    blobs = Blobs(tmp_path / "blobs")
    a, b = retain(blobs, before, 0), retain(blobs, after, 1)
    scope = {"project_id": "p", "environment_id": "e", "observed_at": NOW}
    page = RevisionPackagePage.model_validate(
        {**scope, **lockfile_diffs.revision_packages(blobs.root, b, 0, 8)}, strict=True
    )
    row = page.packages[0]
    assert isinstance(row.package, PipDistribution)
    assert row.package.license == row.license.declared == "AGPL-3.0-only"
    assert row.license.agpl is True
    diff = LockfileDiffPage.model_validate(
        {**scope, **lockfile_diffs.compare(blobs.root, (a, b), 0, 8)}, strict=True
    )
    changed = diff.differences[0]
    assert changed.changed_fields == ("license",)
    assert changed.before_license is not None
    assert changed.before_license.declared == "MIT"
    assert changed.before_license.agpl is False
    assert changed.after_license == row.license
    for field in ("package", "license"):
        copied = page.model_dump(mode="json")
        copied["packages"][0][field]["license" if field == "package" else "declared"] = "BSD"
        if field == "license":
            copied["packages"][0][field]["agpl"] = False
        with pytest.raises(ValidationError, match="declared license"):
            RevisionPackagePage.model_validate_json(json.dumps(copied), strict=True)
    copied = diff.model_dump(mode="json")
    copied["differences"][0]["after_license"]["declared"] = "BSD"
    copied["differences"][0]["after_license"]["agpl"] = False
    with pytest.raises(ValidationError, match="declared license"):
        LockfileDiffPage.model_validate_json(json.dumps(copied), strict=True)


def test_old_raw_lockfile_remains_unknown_and_equal_to_explicit_null(tmp_path: Path):
    legacy = Lockfile(pip=(PipDistribution(name="example", version="1"),)).model_dump(mode="json")
    del legacy["pip"][0]["license"]
    raw = json.dumps(legacy).encode()
    lock = Lockfile.model_validate_json(raw, strict=True)
    assert lock.pip[0].license is None
    blobs = Blobs(tmp_path / "blobs")
    a, b = retain(blobs, raw, 0), retain(blobs, lock.model_dump_json().encode(), 1)
    digest = a.lockfile
    assert digest is not None
    assert digest.sha256 == hashlib.sha256(raw).hexdigest()
    inventory = RevisionPackagePage.model_validate(
        {
            "project_id": "p",
            "environment_id": "e",
            "observed_at": NOW,
            **lockfile_diffs.revision_packages(blobs.root, a, 0, 8),
        },
        strict=True,
    )
    assert inventory.packages[0].license.declared is None
    assert inventory.packages[0].license.agpl is None
    diff = lockfile_diffs.compare(blobs.root, (a, b), 0, 8)
    assert diff["total"] == 0


@pytest.mark.parametrize("value", [True, 42, [], {}, "", "L" * 257, "MIT\nBSD", "MIT\x00"])
def test_strict_pip_license_field_refuses_invalid_raw_types_and_bounds(value: object):
    raw = json.dumps({"name": "example", "version": "1", "license": value}).encode()
    with pytest.raises(ValidationError):
        PipDistribution.model_validate_json(raw, strict=True)


@pytest.mark.parametrize(
    ("declared", "wrong"), [("MIT", True), ("AGPL-3.0-only", False), ("GNU Affero GPL", False)]
)
def test_declared_flag_is_bound_in_inventory_and_each_difference_side(
    tmp_path: Path, declared: str, wrong: bool
):
    blobs = Blobs(tmp_path / "blobs")
    lock = Lockfile(pip=(PipDistribution(name="example", version="1", license=declared),))
    a, b = (
        retain(blobs, lock.model_dump_json().encode(), 0),
        retain(
            blobs,
            Lockfile(pip=(PipDistribution(name="example", version="2", license=declared),))
            .model_dump_json()
            .encode(),
            1,
        ),
    )
    scope = {"project_id": "p", "environment_id": "e", "observed_at": NOW}
    page = RevisionPackagePage.model_validate(
        {**scope, **lockfile_diffs.revision_packages(blobs.root, a, 0, 8)}, strict=True
    ).model_dump(mode="json")
    page["packages"][0]["license"]["agpl"] = wrong
    with pytest.raises(ValidationError, match="AGPL flag"):
        RevisionPackagePage.model_validate_json(json.dumps(page), strict=True)
    diff = LockfileDiffPage.model_validate(
        {**scope, **lockfile_diffs.compare(blobs.root, (a, b), 0, 8)}, strict=True
    ).model_dump(mode="json")
    for side in ("before_license", "after_license"):
        copied = json.loads(json.dumps(diff))
        copied["differences"][0][side]["agpl"] = wrong
        with pytest.raises(ValidationError, match="AGPL flag"):
            LockfileDiffPage.model_validate_json(json.dumps(copied), strict=True)
