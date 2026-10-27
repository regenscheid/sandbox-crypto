# SPDX-License-Identifier: MIT
"""``sandbox-science migrate-mcp-config CONFIG``: a config's ``[mcp_proxy]`` as ``[mcp_direct]``.

The MCP proxy retired on 2026-10-26 (D98) and ``[mcp_proxy]`` is refused; the agent runtime's
own client reaches the servers, configured by ``[mcp_direct]`` (docs/MCP-CONNECTORS.md,
"Migrating from [mcp_proxy]"). This rewrites the one table into the other:

- ``servers`` become ``[mcp_direct.servers.NAME]``, without a ``url``: the project policy's
  is used. Each server's credential headers come from the policy's ``headers`` entry, each
  header whose value is one ``${provider:NAME.KEY}`` mapped to the environment variable the
  proxy's ``credentials`` gave ``NAME.KEY``;
- ``project``, ``providers``, ``about``, ``index``, ``provider_env_file`` and
  ``provider_profile`` are kept as they were written;
- the proxy's own keys (``image``, ``credentials``, ``sandbox``, ``listen``, ``approvals``,
  ``policy``, ``targets``, ``ceiling``) are dropped: the direct path compiles from the
  worker's policy, targets file and pool ceiling, and the user always answers held calls
  in the workspace.

Only the ``[mcp_proxy]`` tables' lines change, and the rest of the file is kept as it was;
the new ``[mcp_direct]`` takes their place. What cannot be carried over (a ``bearer`` or
``oauth`` server, whose token goes out with a prefix the direct path cannot name, or a
header with text around its placeholder) is said, for the owner to set by hand. The result
is checked as a config before it is written; the original is kept beside it, as
``CONFIG.mcp-proxy.bak``. The launcher never opens ``provider_env_file``, and nor does this.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from sandbox_science.policy import PolicyLoadError, parse_policy
from sandbox_science.policy.loader import read_limited
from sandbox_science.policy.model import BearerAuth, HeadersAuth, OAuthAuth, ProjectPolicy

from . import toml
from .config import LauncherConfig, LauncherError
from .state import write_private

KEPT = ("project", "providers", "provider_env_file", "provider_profile", "index")
"""Keys of ``[mcp_proxy]`` that ``[mcp_direct]`` takes as they are."""
DROPPED = {
    "image": "the proxy's image",
    "credentials": "mapped onto each server's credential headers",
    "sandbox": "the proxy's sandbox",
    "listen": "the proxy's forward",
    "approvals": "the user always answers held calls in the workspace",
    "policy": "the direct path compiles from [worker] policy",
    "targets": "the direct path compiles with [openshell.environments] targets",
    "ceiling": "the direct path compiles with [openshell.environments] ceiling",
}
_PLACEHOLDER = re.compile(
    r"\$\{provider:([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.([A-Z][A-Z0-9_]*)\}"
)
_HEADER = re.compile(r"\s*\[\[?\s*([^\]#]*?)\s*\]\]?\s*(?:#.*)?")
_BACKUP = ".mcp-proxy.bak"


def main(path: Path, *, show: bool = False) -> int:
    """Rewrite the config at ``path``, keeping the original beside it, or print it."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LauncherError(f"cannot read {path}: {exc}") from None
    rewritten, notes = migrate(text, path.absolute().parent)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    if show:
        sys.stdout.write(rewritten)
        return 0
    backup = path.with_name(path.name + _BACKUP)
    if backup.exists():
        raise LauncherError(f"{backup} exists already: move it away, then run this again")
    shutil.copy2(path, backup)
    write_private(path, rewritten)
    os.chmod(path, backup.stat().st_mode & 0o777)
    print(f"rewrote [mcp_proxy] as [mcp_direct] in {path}; the original is {backup}")
    return 0


def migrate(text: str, base: Path) -> tuple[str, list[str]]:
    """``text``, a launcher config, with its ``[mcp_proxy]`` rewritten as ``[mcp_direct]``,
    and what the owner should know or do by hand. Relative paths are against ``base``."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LauncherError(f"the config is not TOML: {exc}") from None
    proxy = raw.get("mcp_proxy")
    if not isinstance(proxy, dict):
        raise LauncherError("the config has no [mcp_proxy] table to migrate")
    if "mcp_direct" in raw:
        raise LauncherError("the config has [mcp_direct] already: remove [mcp_proxy] by hand")
    spec = cast(dict[str, Any], proxy)
    notes: list[str] = []
    direct: dict[str, Any] = {key: spec[key] for key in KEPT if key in spec}
    servers = spec.get("servers")
    if not isinstance(servers, list) or not servers:
        raise LauncherError("mcp_proxy.servers names no server")
    policy = _policy(raw, base, notes)
    credentials = cast(Mapping[str, str], spec.get("credentials") or {})
    direct["servers"] = {
        str(name): _server(str(name), policy, credentials, notes)
        for name in cast(list[object], servers)
    }
    if "about" in spec:
        direct["about"] = spec["about"]
    for key, why in DROPPED.items():
        if key in spec:
            notes.append(f"mcp_proxy.{key} is dropped: {why}")
    if spec.get("approvals") is False:
        notes.append(
            "mcp_proxy.approvals was off: on the direct path an escalated call is held for the "
            "user's answer in the workspace, and the policy decides which calls escalate"
        )
    section = (
        "# Migrated from [mcp_proxy] by sandbox-science migrate-mcp-config: the MCP proxy\n"
        "# retired on 2026-10-26 (D98, docs/MCP-CONNECTORS.md).\n"
        + toml.dumps({"mcp_direct": direct})
        + "\n"
    )
    rewritten = _replaced(text, section)
    _check(rewritten, base)
    return rewritten, notes


def _policy(raw: Mapping[str, Any], base: Path, notes: list[str]) -> ProjectPolicy:
    """The worker's project policy, whose servers' credential headers are carried over."""
    worker = raw.get("worker")
    named = cast(dict[str, Any], worker).get("policy") if isinstance(worker, dict) else None
    if not isinstance(named, str):
        raise LauncherError("the config's [worker] names no policy")
    path = base / Path(named).expanduser()
    try:
        data = read_limited(path)
    except PolicyLoadError as exc:
        raise LauncherError(f"cannot read the project policy: {exc}") from None
    try:
        return parse_policy(data, source=str(path)).policy
    except PolicyLoadError as exc:
        if "retired with the MCP proxy" not in str(exc):
            raise LauncherError(f"the project policy is not valid: {exc}") from None
    notes.append(
        f"{path} sets mcp.proxy.worker_access or worker_tools, which sandbox-science up now "
        "refuses: research code has no MCP access since the MCP proxy retired; remove them"
    )
    return parse_policy(data, source=str(path), stored=True).policy


def _server(
    name: str, policy: ProjectPolicy, credentials: Mapping[str, str], notes: list[str]
) -> dict[str, Any]:
    """``[mcp_direct.servers.NAME]``: its credential headers, each the environment variable
    that holds its placeholder; the URL is the policy's."""
    entry = policy.mcp.servers.get(name)
    if entry is None:
        raise LauncherError(f"mcp_proxy.servers names {name}, which the project policy does not")
    auth = entry.auth
    headers: dict[str, str] = {}
    if isinstance(auth, BearerAuth | OAuthAuth):
        notes.append(
            f"{name} is a {auth.mode} server: its token goes out as 'Bearer <token>', which "
            f"[mcp_direct.servers.{name}] credentials cannot name; set them by hand"
        )
    elif isinstance(auth, HeadersAuth):
        for header, value in auth.headers.items():
            found = _PLACEHOLDER.fullmatch(value)
            if found is None:
                if "${provider:" in value:
                    notes.append(
                        f"{name}'s header {header} has text around its placeholder, which "
                        "the direct path cannot send; set it by hand"
                    )
                continue  # a literal header: the policy sends it, it is no credential
            reference = f"{found[1]}.{found[2]}"
            variable = credentials.get(reference)
            if variable is None:
                notes.append(
                    f"mcp_proxy.credentials gives no variable for {reference}, {name}'s "
                    f"header {header}: set it in [mcp_direct.servers.{name}] credentials"
                )
                continue
            headers[header] = variable
    return {"credentials": headers} if headers else {}


def _replaced(text: str, section: str) -> str:
    """``text`` without the lines of its ``[mcp_proxy]`` tables, ``section`` in their
    place. A table runs from its header to the next header; a header inside a multi-line
    string is no header."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    inside = placed = False
    quote: str | None = None
    for line in lines:
        header = None if quote is not None else _HEADER.fullmatch(line.rstrip("\r\n"))
        if header is not None:
            name = re.sub(r"\s*\.\s*", ".", header[1]).strip("\"'")
            inside = name == "mcp_proxy" or name.startswith("mcp_proxy.")
            if inside and not placed:
                kept.append(section if not kept or kept[-1].strip() == "" else "\n" + section)
                placed = True
        if not inside:
            kept.append(line)
        for marker in ('"""', "'''"):
            if line.count(marker) % 2:
                quote = None if quote == marker else (quote or marker)
    if not placed:
        raise LauncherError("[mcp_proxy] is not a table of its own here: rewrite it by hand")
    return "".join(kept)


def _check(text: str, base: Path) -> None:
    """Refuse a rewritten config that is not one."""
    try:
        raw = tomllib.loads(text)
        LauncherConfig.model_validate(raw, context={"base": base})
    except tomllib.TOMLDecodeError as exc:
        raise LauncherError(
            f"the rewritten config would not be TOML ({exc}): rewrite it by hand"
        ) from None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'the file'}: {error['msg']}"
            for error in exc.errors()
        )
        raise LauncherError(f"the rewritten config would not be valid: {problems}") from None
