# SPDX-License-Identifier: MIT
"""A shared tag must contain exactly the two tested immutable image digests."""

from __future__ import annotations

import importlib.util
import json
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


def test_pushed_architecture_index_selects_image_not_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = "registry.example/ss/minimal"
    pushed = "sha256:" + "a" * 64
    leaf = "sha256:" + "b" * 64
    attestation = "sha256:" + "c" * 64
    manifest = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}, "digest": leaf},
            {
                "platform": {"os": "unknown", "architecture": "unknown"},
                "digest": attestation,
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": leaf,
                },
            },
        ]
    }
    monkeypatch.setattr(publish, "docker", lambda *args: json.dumps(manifest))
    assert publish.platform_manifest(repo + "@" + pushed, "linux/amd64") == repo + "@" + leaf
    manifest["manifests"][1]["annotations"]["vnd.docker.reference.digest"] = pushed
    with pytest.raises(ValueError, match="unrelated"):
        publish.platform_manifest(repo + "@" + pushed, "linux/amd64")


def test_publication_checks_both_inputs_then_verifies_the_written_index(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = "registry.example/ss/crypto-base"
    amd = "sha256:" + "a" * 64
    arm = "sha256:" + "b" * 64
    index = "sha256:" + "c" * 64
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str) -> str:
        calls.append(args)
        if (
            args[0] == "inspect"
            and args[2] == "{{json .Manifest}}"
            and args[-1].endswith(("@" + amd, "@" + arm))
        ):
            return json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json"})
        if args[0] == "inspect" and args[-1].endswith("@" + amd):
            return json.dumps(
                {
                    "os": "linux",
                    "architecture": "amd64",
                    "config": {
                        "Labels": {
                            "io.github.regenscheid.sandbox-science.profile": "crypto-base",
                            "org.opencontainers.image.revision": "rev",
                        }
                    },
                }
            )
        if args[0] == "inspect" and args[-1].endswith("@" + arm):
            return json.dumps(
                {
                    "os": "linux",
                    "architecture": "arm64",
                    "config": {
                        "Labels": {
                            "io.github.regenscheid.sandbox-science.profile": "crypto-base",
                            "org.opencontainers.image.revision": "rev",
                        }
                    },
                }
            )
        if args[0] == "inspect":
            return json.dumps(
                {
                    "digest": index,
                    "manifests": [
                        {"platform": {"os": "linux", "architecture": "amd64"}, "digest": amd},
                        {"platform": {"os": "linux", "architecture": "arm64"}, "digest": arm},
                    ],
                }
            )
        return ""

    monkeypatch.setattr(publish, "docker", fake_docker)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish_index.py",
            "crypto-base",
            repo + ":rev",
            repo + "@" + amd,
            repo + "@" + arm,
            "--private-registry-confirmed",
        ],
    )
    assert publish.main() == 0
    assert calls[4][0] == "create"
    assert capsys.readouterr().out.strip() == repo + "@" + index
