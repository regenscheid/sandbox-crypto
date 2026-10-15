# SPDX-License-Identifier: MIT
"""Write a lab's launcher config, outside the repository, from the example.

    uv run python -I .agents/labs/launcher_config.py OUT --state-dir DIR [--project ID] \\
        [--mcp-proxy IMAGE [--provider-env-file FILE]]

The tracked example names no real secret file: whoever writes a config also chooses the
OpenShell CLI that gets the registered values. A lab with the MCP proxy names the owner's
file here, as the coordinator gives it, so only the lab's own config holds its path. This
writes the path alone and never opens the file.

OUT gets every setting of examples/sandbox-science.toml, with absolute paths, the state
directory DIR and, if given, the project ID and the MCP proxy for crypto-research-mcp.
It is written with mode 600, and refused inside a git checkout. With the MCP proxy, the
policy is examples/policy.yaml with crypto-research-mcp's address in place of its
placeholder, written as policy.yaml beside OUT: the proxy reaches the server it names.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from sandbox_science.launcher import toml
from sandbox_science.launcher.config import load_config

REPOSITORY = Path(__file__).resolve().parents[2]
EXAMPLE = REPOSITORY / "examples/sandbox-science.toml"
PROFILE = REPOSITORY / "examples/openshell/profiles/cloudflare-access.yaml"
CREDENTIALS = ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET")
PLACEHOLDER = re.compile(r"url: https://mcp\.example\.invalid/crypto-research\b[^\n]*")
"""The example policy's placeholder line for crypto-research-mcp, with its comment."""
SERVER = "https://research-mcp-api.npages.org/mcp"
"""crypto-research-mcp's Access-protected address (docs/MCP-CONNECTORS.md)."""


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as written:
        written.write(text)


def mcp_policy(example: Path) -> str:
    """The example policy, with crypto-research-mcp's address in place of its placeholder."""
    text, count = PLACEHOLDER.subn(
        f"url: {SERVER}   # the Access-protected address", example.read_text(encoding="utf-8")
    )
    if count != 1:
        raise SystemExit(f"{example} has no single placeholder address for crypto-research-mcp")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--project")
    parser.add_argument("--mcp-proxy", metavar="IMAGE")
    parser.add_argument("--provider-env-file", type=Path)
    args = parser.parse_args()
    out = args.out.expanduser().absolute()
    if any((folder / ".git").exists() for folder in out.parents):
        parser.error(f"{out} is inside a git checkout: write the lab's config outside it")
    if args.provider_env_file is not None and args.mcp_proxy is None:
        parser.error("--provider-env-file needs --mcp-proxy")
    data = load_config(EXAMPLE).model_dump(mode="json", exclude_none=True)
    data["state_dir"] = str(args.state_dir.expanduser().absolute())
    if args.project is not None:
        (project,) = data["projects"]
        project["id"] = args.project
    if args.mcp_proxy is not None:
        proxy = {
            "image": args.mcp_proxy,
            "providers": ["crypto-research-mcp"],
            "servers": ["crypto-research-mcp"],
            "credentials": {f"crypto-research-mcp.{key}": key for key in CREDENTIALS},
        }
        if args.provider_env_file is not None:
            proxy["provider_env_file"] = str(args.provider_env_file.expanduser().absolute())
            proxy["provider_profile"] = str(PROFILE)
        data["mcp_proxy"] = proxy
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.mcp_proxy is not None:
        policy = out.parent / "policy.yaml"
        write_private(policy, mcp_policy(Path(data["worker"]["policy"])))
        data["worker"]["policy"] = str(policy)
    write_private(out, toml.dumps(data))
    load_config(out)  # the launcher takes it
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
