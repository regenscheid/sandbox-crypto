# SPDX-License-Identifier: MIT
"""The agent runtime's MCP fence as the Kubernetes runtime manager compiles it (D98, A3).

At each creation of the runtime's sandbox, bootstrap and replacement alike, the manager
compiles the direct path's egress from the site's project policy, pool ceiling and
targets (``direct_mcp.compile_direct``, as ``sandbox-science up`` does on a host) with the
project's classification revisions in force, which it reads through the worker
(``GET /v1/runtime-classifications``, the one read its ``binding_only`` client has): the
accepted revision where one is bound, else the checked-in file. A revision accepted while
a runtime runs therefore reaches its fence at the next replacement; the policy middleware
decides by it from the next call meanwhile, so a stale fence refuses a widening and never
admits more than the middleware allows.

A binding the worker cannot read leaves the servers concerned on the checked-in file, with
a warning, and the middleware refuses their calls while it stays unreadable. A worker that
does not answer, or a file that arrives changed, leaves them on the checked-in file too,
with a warning that says so, while the middleware goes on deciding by the classification
in force; so does a revision that does not compile into the fence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from sandbox_science.contracts.classifications import RuntimeClassifications
from sandbox_science.launcher.config import LauncherError
from sandbox_science.launcher.direct_mcp import DirectEgress, compile_direct, worker_classifications
from sandbox_science.launcher.sandboxes import WorkerApi
from sandbox_science.policy.store import FenceClassifications

from .settings import RuntimeManagerSettings

Reader = Callable[[str, Sequence[str]], Awaitable[RuntimeClassifications]]
"""Reads a project's classification revisions in force for the servers named."""


def worker_reader(settings: RuntimeManagerSettings) -> Reader | None:
    """The worker's ``GET /v1/runtime-classifications`` through the manager's worker link."""
    link = settings.worker
    if link is None:
        return None
    return WorkerApi(link.url, link.token_file, link.ca_file).runtime_classifications


async def compile_egress(
    settings: RuntimeManagerSettings,
    read: Reader | None,
    say: Callable[[str], None],
    *,
    revisions: bool = True,
) -> DirectEgress | None:
    """The direct path's egress for a new sandbox, or None without ``[mcp_direct]``;
    ``revisions`` False compiles with the checked-in files alone. Raises LauncherError
    when the site's files do not compile."""
    mcp, link = settings.mcp, settings.worker
    if mcp is None or link is None:
        return None
    found: FenceClassifications | None = None
    if revisions and read is not None:
        try:
            found = worker_classifications(await read(link.project, sorted(mcp.servers)))
        except LauncherError as exc:
            found = FenceClassifications({}, {}, dict.fromkeys(mcp.servers, str(exc)[:300]))
    egress = await asyncio.to_thread(
        compile_direct,
        policy=mcp.project_policy_file,
        ceiling=mcp.ceiling_file,
        targets=mcp.targets_file,
        project=link.project,
        servers=mcp.servers,
        python=settings.python,
        classifications=found,
    )
    for warning in egress.warnings():
        say(f"the agent runtime's MCP egress: {warning}")
    for note in egress.fence_notes():
        say(f"the agent runtime's MCP egress: {note}")
    return egress
