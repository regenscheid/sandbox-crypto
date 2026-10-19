# SPDX-License-Identifier: MIT
"""A shared tag must contain exactly the two tested immutable image digests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

PATH = Path(__file__).resolve().parents[1] / "publish_index.py"
SPEC = importlib.util.spec_from_file_location("publish_index", PATH)
assert SPEC is not None
assert SPEC.loader is not None
publish = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = publish
SPEC.loader.exec_module(publish)


def test_references_must_be_pinned_and_fully_qualified() -> None:
    digest = "sha256:" + "a" * 64
    assert publish.repository(f"registry.example/ss/crypto-base@{digest}", digest=True) == (
        "registry.example/ss/crypto-base"
    )
    with pytest.raises(ValueError, match="pinned"):
        publish.repository("registry.example/ss/crypto-base:latest", digest=True)
    with pytest.raises(ValueError, match="registry host"):
        publish.repository("ss/crypto-base:latest", digest=False)


def test_index_must_match_exact_architecture_digests() -> None:
    sources = {"linux/amd64": "sha256:" + "a" * 64, "linux/arm64": "sha256:" + "b" * 64}
    entries: list[dict[str, Any]] = [
        {"platform": {"os": "linux", "architecture": platform.split("/")[1]}, "digest": digest}
        for platform, digest in sources.items()
    ]
    manifest = {"digest": "sha256:" + "c" * 64, "manifests": entries}
    assert publish.verify_index(manifest, sources) == manifest["digest"]
    with pytest.raises(ValueError, match="do not match"):
        publish.verify_index({**manifest, "manifests": entries[:1]}, sources)
    with pytest.raises(ValueError, match="unexpected"):
        publish.verify_index(
            {
                **manifest,
                "manifests": [*entries, {"platform": {"os": "linux", "architecture": "arm"}}],
            },
            sources,
        )
