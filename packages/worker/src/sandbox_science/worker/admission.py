# SPDX-License-Identifier: MIT
"""On-demand admission (D69): the worker answers the proposals OpenShell drafts.

A sandbox resolves only the host names its compiled policy names. For any other
name, OpenShell's supervisor answers with a placeholder address, refuses the
connection that follows and drafts a proposal: one program, one host and port.
Drafts reach the gateway about every 10 s. The watcher lists each ready
environment's pending drafts, has ``Environments.admit`` decide their hosts, and
then rejects every draft it read, giving the decision as the reason.

It never approves one. A draft's rule is a raw-TCP endpoint for one program,
which the fail-closed policy middleware cannot inspect, so OpenShell would deny
its traffic anyway. The worker names admitted hosts in its own compilation
instead, like the hosts the policy names, and the middleware decides every
request to them.

``openshell rule get`` has no JSON output at v0.1.3, so the watcher reads its text.
A sandbox chooses the host names and program paths in it. A crafted path can at
most add hosts the sandbox could have tried anyway, which are decided like any
other, or hide its own draft.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sandbox_science.policy import normalize_host

from .environments import Admission, Environments
from .errors import WorkerError

_log = logging.getLogger(__name__)

_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_FIELD: Final = re.compile(r"  ([A-Z][A-Za-z]*): ?(.*)")
_CHUNK_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# ``host:port [tags]``, endpoints joined by ", "; an IPv6 host prints without brackets.
_ENDPOINT: Final = re.compile(r"(?:^|, )([^\s\[\],]+):([0-9]{1,5}) \[")
_BACKOFF_SECONDS: Final = 60.0
_REASON_CHARS: Final = 500


@dataclass(frozen=True, slots=True)
class Draft:
    """A proposal as ``openshell rule get`` prints it."""

    chunk_id: str
    status: str
    endpoints: tuple[tuple[str, int], ...]
    address_check: bool
    """OpenShell's address check refused the connection (its security notes say SSRF)."""


def parse_drafts(text: str) -> list[Draft]:
    """The proposals in ``openshell rule get`` output; malformed blocks are skipped."""
    blocks: list[dict[str, str]] = []
    for line in _ANSI.sub("", text).splitlines():
        match = _FIELD.fullmatch(line)
        if match is None:
            continue
        key, value = match.groups()
        if key == "Chunk":
            blocks.append({key: value.strip()})
        elif blocks and key not in blocks[-1]:
            blocks[-1][key] = value
    drafts: list[Draft] = []
    for block in blocks:
        chunk_id = block["Chunk"]
        if not _CHUNK_ID.fullmatch(chunk_id):
            continue
        endpoints = tuple(
            (host, int(port))
            for host, port in _ENDPOINT.findall(block.get("Endpoints", ""))
            if 0 < int(port) < 65536
        )
        drafts.append(
            Draft(
                chunk_id=chunk_id,
                status=block.get("Status", "").strip(),
                endpoints=endpoints,
                address_check="SSRF" in block.get("Security", ""),
            )
        )
    return drafts


def _key(host: str, port: int) -> tuple[str, int]:
    """A draft's endpoint as ``Environments.admit`` reports its decision."""
    try:
        return normalize_host(host), port
    except ValueError:
        return host, port


def _reason(draft: Draft, decisions: dict[tuple[str, int], Admission]) -> str:
    """The rejection reason OpenShell records for a draft: what the worker decided."""
    parts: list[str] = []
    for host, port in draft.endpoints:
        decision = decisions.get(_key(host, port))
        if decision is None:
            parts.append(f"{host}:{port}: not decided")
        elif not decision.admitted:
            parts.append(f"{host}:{port}: refused, {decision.reason}")
        elif decision.reason == "admitted":
            parts.append(f"{host}:{port}: admitted on demand; retry the request")
        elif draft.address_check:
            parts.append(
                f"{host}:{port}: admitted, but the name resolved to an address that is not "
                "public, which the policy never admits for it"
            )
        else:
            parts.append(
                f"{host}:{port}: admitted already; retry, since the connection came before the "
                "sandbox loaded the policy that names the host"
            )
    text = "; ".join(parts) or "the draft names no endpoint"
    return f"Sandbox Science worker: {text}"[:_REASON_CHARS]


class DraftWatcher:
    """Answers the drafts of every ready environment, every ``interval`` seconds."""

    def __init__(self, environments: Environments, interval: float) -> None:
        self.environments = environments
        self.interval = interval
        self._paused: dict[str, float] = {}

    async def run(self) -> None:
        """Check every ready environment until cancelled."""
        while True:
            await self.check_all()
            await asyncio.sleep(self.interval)

    async def check_all(self) -> None:
        loop = asyncio.get_running_loop()
        for info in self.environments.records.ready():
            if self._paused.get(info.environment_id, 0.0) > loop.time():
                continue
            try:
                await self.check(info.environment_id, info.sandbox)
            except WorkerError as error:
                # A gateway that cannot list or answer drafts now is asked again later;
                # the drafts stay pending meanwhile.
                self._paused[info.environment_id] = loop.time() + _BACKOFF_SECONDS
                _log.warning(
                    "admission for environment %s paused for %.0f s: %s",
                    info.environment_id,
                    _BACKOFF_SECONDS,
                    error.message,
                )

    async def check(self, environment_id: str, sandbox: str) -> Sequence[Admission]:
        """Decide one environment's pending drafts, then reject each with the decision."""
        cli = self.environments.cli
        text = await cli.run("rule", "get", sandbox, "--status", "pending", "--color", "never")
        drafts = [draft for draft in parse_drafts(text) if draft.status == "pending"]
        if not drafts:
            return ()
        requests = [endpoint for draft in drafts for endpoint in draft.endpoints]
        decisions = await self.environments.admit(environment_id, requests)
        by_endpoint = {(decision.host, decision.port): decision for decision in decisions}
        for draft in drafts:
            try:
                await cli.run(
                    "rule", "reject", sandbox, "--chunk-id", draft.chunk_id,
                    "--reason", _reason(draft, by_endpoint),
                )  # fmt: skip
            except WorkerError as error:
                # A block a crafted program path faked names no real draft.
                _log.warning("could not reject draft %s: %s", draft.chunk_id, error.message)
        for decision in decisions:
            _log.info(
                "environment %s: %s:%d %s",
                environment_id,
                decision.host,
                decision.port,
                decision.reason,
            )
        return decisions
