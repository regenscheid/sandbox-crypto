# SPDX-License-Identifier: MIT
"""Join two already-pushed architecture digests under one registry tag.

The inputs must be immutable manifests in the same repository. A buildx push may
wrap one image leaf and its attestation in an OCI index; select only the image.
In particular,
crypto-base may only go to a registry whose private access was checked by the
operator; this script never guesses whether a registry package is private.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any

PROFILES = frozenset({"minimal", "crypto-base", "orchestrator", "trusted-app", "trusted-worker"})
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def repository(reference: str, *, digest: bool) -> str:
    """Extract an image repository and refuse mutable source references."""
    if digest:
        name, separator, pin = reference.partition("@")
        if separator != "@" or not SHA256.fullmatch(pin):
            raise ValueError("source images must be pinned by sha256 digest")
    else:
        split = reference.rfind(":")
        if split <= reference.rfind("/") or not reference[split + 1 :]:
            raise ValueError("target must have an explicit tag")
        name = reference[:split]
    if "/" not in name or name.startswith("/") or any(c.isspace() for c in name):
        raise ValueError("use a fully qualified registry/repository reference")
    host = name.split("/", 1)[0]
    if "." not in host and ":" not in host and host != "localhost":
        raise ValueError("name the registry host explicitly")
    return name


def verify_index(manifest: Mapping[str, Any], sources: Mapping[str, str]) -> str:
    """Confirm the published index points to exactly the two input digests."""
    entries = manifest.get("manifests")
    if not isinstance(entries, list):
        raise ValueError("the target is not an image index")
    found: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("platform"), dict):
            raise ValueError("an index descriptor has no platform")
        platform = entry["platform"]
        key = f"{platform.get('os')}/{platform.get('architecture')}"
        if key in found or key not in sources:
            raise ValueError(f"unexpected or repeated platform {key}")
        found[key] = entry.get("digest", "")
    if found != sources:
        raise ValueError(f"published platform digests do not match inputs: {found}")
    digest = manifest.get("digest")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise ValueError("published index has no SHA-256 digest")
    return digest


def platform_manifest(reference: str, platform: str) -> str:
    """Resolve one pushed architecture index to its image leaf, excluding attestations."""
    manifest = json.loads(docker("inspect", "--format", "{{json .Manifest}}", reference))
    if not isinstance(manifest, dict):
        raise ValueError("source has no manifest")
    entries = manifest.get("manifests")
    if entries is None:
        if manifest.get("mediaType") not in {
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        }:
            raise ValueError("source is not a supported image manifest")
        return reference
    if not isinstance(entries, list):
        raise ValueError("source index has invalid descriptors")
    chosen: str | None = None
    attestations: list[Mapping[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("platform"), dict):
            raise ValueError("source descriptor has no platform")
        kind = entry["platform"]
        key = f"{kind.get('os')}/{kind.get('architecture')}"
        digest = entry.get("digest")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("source descriptor has no SHA-256 digest")
        if key == platform:
            if chosen is not None:
                raise ValueError(f"source repeats {platform}")
            chosen = digest
        elif key == "unknown/unknown":
            attestations.append(entry)
        else:
            raise ValueError(f"source contains unexpected platform {key}")
    if chosen is None:
        raise ValueError(f"source has no {platform} image")
    for entry in attestations:
        annotations = entry.get("annotations")
        if not isinstance(annotations, dict) or (
            annotations.get("vnd.docker.reference.type") != "attestation-manifest"
            or annotations.get("vnd.docker.reference.digest") != chosen
        ):
            raise ValueError("source has an unrelated unknown-platform manifest")
    return repository(reference, digest=True) + "@" + chosen


def docker(*args: str) -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise ValueError("docker CLI is not installed")
    done = subprocess.run(  # noqa: S603 - fixed CLI, no shell
        [executable, "buildx", "imagetools", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=sorted(PROFILES))
    parser.add_argument("target", help="fully qualified shared tag")
    parser.add_argument("amd64", help="same repository, pinned manifest digest")
    parser.add_argument("arm64", help="same repository, pinned manifest digest")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--private-registry-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        name = repository(args.target, digest=False)
        if name.rsplit("/", 1)[-1] != args.profile:
            raise ValueError("target repository must end with the profile name")
        if any(repository(ref, digest=True) != name for ref in (args.amd64, args.arm64)):
            raise ValueError("source digests must already be in the target repository")
        if args.profile == "crypto-base" and not args.private_registry_confirmed:
            raise ValueError("confirm the target registry package is private before crypto-base")
        resolved = {
            "linux/amd64": platform_manifest(args.amd64, "linux/amd64"),
            "linux/arm64": platform_manifest(args.arm64, "linux/arm64"),
        }
        sources = {platform: ref.partition("@")[2] for platform, ref in resolved.items()}
        revisions: set[str] = set()
        for platform, ref in resolved.items():
            image = json.loads(docker("inspect", "--format", "{{json .Image}}", ref))
            if not isinstance(image, dict) or (image.get("os"), image.get("architecture")) != tuple(
                platform.split("/")
            ):
                raise ValueError(f"{ref} is not a single {platform} image")
            config = image.get("config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if (
                not isinstance(labels, dict)
                or labels.get("io.github.regenscheid.sandbox-science.profile") != args.profile
            ):
                raise ValueError(f"{ref} has no matching profile label")
            revision = labels.get("org.opencontainers.image.revision")
            if not isinstance(revision, str) or not revision:
                raise ValueError(f"{ref} has no source revision label")
            revisions.add(revision)
        if len(revisions) != 1:
            raise ValueError("architecture images were built from different recipe revisions")
        if args.dry_run:
            docker("create", "--dry-run", "--tag", args.target, *resolved.values())
            print(f"ready to create {args.target} from two verified digests")
            return 0
        try:
            docker("create", "--tag", args.target, *resolved.values())
        except subprocess.CalledProcessError:
            raise RuntimeError(
                "index creation outcome unknown; inspect the target before retrying"
            ) from None
        manifest = json.loads(docker("inspect", "--format", "{{json .Manifest}}", args.target))
        digest = verify_index(manifest, sources)
        print(f"{name}@{digest}")
        return 0
    except (ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.exit(1, f"publish_index: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
