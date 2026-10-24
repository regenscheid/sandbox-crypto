# SPDX-License-Identifier: MIT
"""Offline check/render of the bundle, and generation of its Secrets' contents (site); no
cluster, image or subprocess access."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from . import kubernetes_site, kubernetes_site_config
from .kubernetes_bundle import render
from .kubernetes_config import BundleError, load


def write_bundle(path: Path, content: bytes) -> None:
    """Atomically expose a new private file; never replace an existing destination."""
    descriptor, name = tempfile.mkstemp(prefix=".kubernetes-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate site-private TOML, write one deterministic Kubernetes List, or generate
    the Secrets' contents into a new private directory."""
    parser = argparse.ArgumentParser(prog="sandbox-science-kubernetes", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "render", "site"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name != "check":
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "site":
            site, bundle = kubernetes_site_config.load(args.config)
            kubernetes_site.write(args.output, kubernetes_site.plan(site, bundle))
            print(
                f"generated the Secrets' contents in {args.output}; read its README.md, "
                "which lists what is left to the operator; no cluster changes made"
            )
            return 0
        bundle = render(load(args.config))
        if args.command == "render":
            write_bundle(
                args.output, (json.dumps(bundle, indent=2, sort_keys=True) + "\n").encode()
            )
            print("rendered offline; no images published and no cluster changes made")
        else:
            print(
                "valid offline configuration; Secret contents and cluster compatibility unverified"
            )
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("error: cannot create a new output file; no overwrite is permitted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
