# SPDX-License-Identifier: MIT
"""Write a lab's launcher config, outside the repository, from the example.

    uv run python -I .agents/labs/launcher_config.py OUT --state-dir DIR [--project ID] \\
        [--profile PROFILE] [--session ID ...] [--mcp [--provider-env-file FILE]] \\
        [--skills-pack NAME=GIT[@REF] ...] [--skills-embedding-model ALIAS] \\
        [--skills-metadata-model ALIAS]

The tracked example names no real secret file: whoever writes a config also chooses the
OpenShell CLI that gets the registered values. A lab with MCP names the owner's
file here, as the coordinator gives it, so only the lab's own config holds its path. This
writes the path alone and never opens the file.

OUT gets every setting of examples/sandbox-science.toml, with absolute paths, the state
directory DIR and, if given, the project ID, its profile (such as crypto-base), its
sessions (each ``--session`` one, in the project's environment, in place of the example's)
and, with ``--mcp``, crypto-research-mcp on the direct path (``[mcp_direct]``, D98; the MCP
proxy retired on 2026-10-26), with the tool index the example's commented [mcp_direct]
table gives it (``index`` and ``about``), so that a lab adds nothing by hand
(lab 40, B7; lab 41). Each ``--skills-pack`` is a ``[skills.packs.NAME]`` from a git URL
or a repository's path, with a ref after the last ``@`` that no ``/`` or ``:`` follows,
and the project pins every pack given; ``--skills-embedding-model`` and
``--skills-metadata-model`` set ``[skills]``'s two models (lab 42, B8).
It is written with mode 600, and refused inside a git checkout. With ``--mcp``, the
policy is examples/policy.yaml with crypto-research-mcp's address in place of its
placeholder, written as policy.yaml beside OUT: the runtime reaches the server it names.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

from sandbox_science.launcher import toml
from sandbox_science.launcher.config import load_config

REPOSITORY = Path(__file__).resolve().parents[2]
EXAMPLE = REPOSITORY / "examples/sandbox-science.toml"
PROFILE = REPOSITORY / "examples/openshell/profiles/cloudflare-access.yaml"
CREDENTIALS = {
    "CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID",
    "CF-Access-Client-Secret": "CF_ACCESS_CLIENT_SECRET",
}
"""crypto-research-mcp's credential headers, each the variable with the provider's
placeholder in the agent runtime's sandbox."""
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


def tool_index(example: Path) -> dict[str, object]:
    """``index`` and ``about``, the tool index's settings, as the example's commented
    ``[mcp_direct]`` table, at its end, gives them."""
    lines = example.read_text(encoding="utf-8").splitlines()
    if "# [mcp_direct]" not in lines:
        raise SystemExit(f"{example} has no commented [mcp_direct] table")
    start = lines.index("# [mcp_direct]")
    table = tomllib.loads(
        "\n".join(line.removeprefix("# ").removeprefix("#") for line in lines[start:])
    )["mcp_direct"]
    return {key: table[key] for key in ("index", "about")}


def skills_pack(value: str) -> tuple[str, dict[str, str]]:
    """``NAME=GIT[@REF]`` as a pack's name and table: a ref follows the last ``@`` when no
    ``/`` or ``:`` does, so ``git@host:owner/repo`` keeps its user."""
    name, equals, origin = value.partition("=")
    if not equals or not name or not origin:
        raise argparse.ArgumentTypeError("a pack is NAME=GIT[@REF]")
    place, at, ref = origin.rpartition("@")
    if at and place and ref and "/" not in ref and ":" not in ref:
        return name, {"git": place, "ref": ref}
    return name, {"git": origin}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--project")
    parser.add_argument("--profile", help="the project's profile, such as crypto-base")
    parser.add_argument(
        "--session",
        action="append",
        metavar="ID",
        help="a session in the project's environment (repeat); in place of the example's",
    )
    parser.add_argument(
        "--mcp", action="store_true", help="crypto-research-mcp on the direct path (D98)"
    )
    parser.add_argument("--provider-env-file", type=Path)
    parser.add_argument(
        "--skills-pack",
        action="append",
        type=skills_pack,
        metavar="NAME=GIT[@REF]",
        help="a skill pack the project pins (repeat)",
    )
    parser.add_argument("--skills-embedding-model", metavar="ALIAS")
    parser.add_argument("--skills-metadata-model", metavar="ALIAS")
    args = parser.parse_args()
    out = args.out.expanduser().absolute()
    if any((folder / ".git").exists() for folder in out.parents):
        parser.error(f"{out} is inside a git checkout: write the lab's config outside it")
    if args.provider_env_file is not None and not args.mcp:
        parser.error("--provider-env-file needs --mcp")
    data = load_config(EXAMPLE).model_dump(mode="json", exclude_none=True)
    data["state_dir"] = str(args.state_dir.expanduser().absolute())
    (project,) = data["projects"]
    if args.project is not None:
        project["id"] = args.project
    if args.profile is not None:
        project["profile"] = args.profile
    if args.session is not None:
        project["sessions"] = [{"id": session, "dedicated": False} for session in args.session]
    if args.mcp:
        direct: dict[str, object] = {
            "providers": ["crypto-research-mcp"],
            "servers": {"crypto-research-mcp": {"credentials": dict(CREDENTIALS)}},
            **tool_index(EXAMPLE),
        }
        if args.provider_env_file is not None:
            direct["provider_env_file"] = str(args.provider_env_file.expanduser().absolute())
            direct["provider_profile"] = str(PROFILE)
        data["mcp_direct"] = direct
    packs = dict(args.skills_pack or ())
    models = {
        "embedding_model": args.skills_embedding_model,
        "metadata_model": args.skills_metadata_model,
    }
    if packs or any(models.values()):
        data["skills"] = {
            **data.get("skills", {}),
            **{key: alias for key, alias in models.items() if alias is not None},
            "packs": {**data.get("skills", {}).get("packs", {}), **packs},
        }
        project["skills"] = list(dict.fromkeys([*project.get("skills", []), *packs]))
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.mcp:
        policy = out.parent / "policy.yaml"
        write_private(policy, mcp_policy(Path(data["worker"]["policy"])))
        data["worker"]["policy"] = str(policy)
    write_private(out, toml.dumps(data))
    load_config(out)  # the launcher takes it
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
