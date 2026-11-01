# SPDX-License-Identifier: MIT
"""Record checked Python wheel metadata beside the pinned Conda bootstrap inventory."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from importlib import metadata
from pathlib import Path
from types import ModuleType

BUILD = Path(__file__).parent


def module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("missing build-time license checker")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("trusted-app", "trusted-worker"), required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--ubuntu-snapshot", required=True)
    parser.add_argument("--micromamba", required=True)
    parser.add_argument("--expect-licenses", required=True)
    parser.add_argument("--source", action="append", default=[])
    args = parser.parse_args()
    report = module("image_report", BUILD / "image_report.py")
    license_check = module("check_licenses", BUILD / "check_licenses.py")
    installed: list[dict[str, str]] = []
    for dist in metadata.distributions():
        if dist.metadata["Name"] == "pip":
            continue  # bootstrap tooling, whose Conda record already has its license
        license_ = license_check.license_of(dist)
        if license_ is None or not license_check.is_allowed(license_):
            raise ValueError(f"unapproved Python distribution license: {dist.metadata['Name']}")
        name = dist.metadata["Name"]
        installed.append({"name": name, "version": dist.version, "license": license_})
        source = (
            args.source_url
            if name.startswith("sandbox-science-")
            else f"https://pypi.org/project/{name}/"
        )
        args.source.append(f"{name}|{dist.version}|{license_}|{source}|wheel")
    args.lockfile = str(BUILD / "conda-lock.yml")
    args.policy = "permissive"
    args.out = "/usr/share/sandbox-science"
    report.command_record(args)
    path = Path(args.out) / "image.json"
    record = json.loads(path.read_text())
    requirements = (BUILD / "requirements.txt").read_bytes()
    record["python_lock_sha256"] = hashlib.sha256(requirements).hexdigest()
    record["python_installed"] = sorted(installed, key=lambda item: item["name"].lower())
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    (Path(args.out) / "requirements.txt").write_bytes(requirements)


if __name__ == "__main__":
    main()
