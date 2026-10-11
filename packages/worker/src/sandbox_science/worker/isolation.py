# SPDX-License-Identifier: MIT
"""Isolation-tier probes (D43): evidence for the tier a backend declares.

When the worker starts an executor, it asks it to describe its sandbox and to
look for a random file the worker has just written in its own state directory.
The worker compares the answer with the same facts about its own host. Evidence
only lowers the declared tier, never raises it:

- the worker host's boot ID (the same running kernel), or a boot ID either side
  cannot read, allows tier 1 at most, and only with a seccomp filter,
  no-new-privs and no effective capabilities (the threat model's hardened
  container); otherwise the tier is 0;
- a visible sentinel, or one the executor cannot check, means tier 0;
- a failed probe means tier 0.

Nothing seen from inside one sandbox shows that it has a VM of its own, so a
declared tier 3 stands when nothing contradicts it. A compromised sandbox can
answer as a well-isolated one would; since answers only lower the tier, the
probe catches misconfigured backends, not hostile sandboxes.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from sandbox_science.contracts import IsolationEvidence, ProbeFacts
from sandbox_science.contracts.worker import KernelSharing, SentinelState

from . import executor
from .channel import ExecutorChannel
from .errors import WorkerError

PROBE_TIMEOUT: Final = 30.0
MAX_ERROR: Final = 300


@dataclass(frozen=True, slots=True)
class Assessment:
    """The tier in effect and the evidence behind it."""

    tier: int
    evidence: IsolationEvidence


def host_facts() -> ProbeFacts:
    """The worker host's facts, read the way the executor reads the sandbox's."""
    return ProbeFacts.model_validate(executor.probe_facts())


def assess(
    declared: int, host: ProbeFacts, sandbox: ProbeFacts, sentinel: SentinelState
) -> Assessment:
    """Lower ``declared`` to what the evidence allows."""
    findings: list[str] = []
    tier = declared
    kernel: KernelSharing
    if host.boot_id is None or sandbox.boot_id is None:
        kernel = "unknown"
        findings.append("no boot ID shows that the sandbox has a kernel of its own")
    elif host.boot_id == sandbox.boot_id:
        kernel = "shared"
        findings.append("the sandbox runs on the worker host's kernel (same boot ID)")
    else:
        kernel = "separate"
    if kernel != "separate":
        tier = min(tier, 1)
        if sandbox.seccomp != "filter":
            findings.append("the executor runs without a seccomp filter")
            tier = 0
        if sandbox.no_new_privs is not True:
            findings.append("the executor runs without no-new-privs")
            tier = 0
        if not _no_capabilities(sandbox.capabilities):
            findings.append(
                f"the executor runs with capabilities {sandbox.capabilities}"
                if sandbox.capabilities
                else "the executor's capabilities are unknown"
            )
            tier = 0
    if sentinel == "visible":
        findings.append("a file the worker wrote in its state directory is visible in the sandbox")
        tier = 0
    elif sentinel == "unknown":
        findings.append("the executor could not tell whether the worker's files are visible")
        tier = 0
    evidence = IsolationEvidence(
        kernel=kernel, sentinel=sentinel, findings=tuple(findings), sandbox=sandbox
    )
    return Assessment(tier, evidence)


def _no_capabilities(capabilities: str | None) -> bool:
    """Whether ``CapEff`` (hex) is readable and empty."""
    try:
        return capabilities is not None and int(capabilities, 16) == 0
    except ValueError:
        return False


def failed(error: str) -> Assessment:
    """A probe that did not complete: tier 0, never the declared one."""
    error = error[:MAX_ERROR]
    evidence = IsolationEvidence(
        kernel="unknown",
        sentinel="unknown",
        findings=(f"the probe failed: {error}",),
        error=error,
    )
    return Assessment(0, evidence)


async def probe(channel: ExecutorChannel, declared: int, state_dir: Path) -> Assessment:
    """Ask a newly started executor about its sandbox, and assess the declared tier."""
    try:
        with sentinel_file(state_dir / "probe") as paths:
            reply = await channel.request("probe", sentinels=paths, reply_timeout=PROBE_TIMEOUT)
        facts = ProbeFacts.model_validate(reply.get("facts"))
        host = host_facts()
    except WorkerError as error:
        return failed(error.message)
    except ValidationError:
        return failed("the executor described its sandbox in an unexpected form")
    except OSError as exc:
        return failed(f"cannot write the sentinel file: {exc}")
    return assess(declared, host, facts, sentinel_result(reply.get("sentinels"), paths))


@contextlib.contextmanager
def sentinel_file(directory: Path) -> Generator[list[str], None, None]:
    """Write a file with a random name for the probe to look for; yield the paths naming it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"sentinel-{secrets.token_hex(12)}"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write("sandbox science worker: a sandbox must not see this file\n")
        # The path as configured and as resolved, in case a symlink leads to it.
        yield sorted({str(path.absolute()), str(path.resolve())})
    finally:
        path.unlink(missing_ok=True)


def sentinel_result(seen: object, paths: Sequence[str]) -> SentinelState:
    """``visible`` if any path showed, ``hidden`` if every path was absent, else ``unknown``."""
    if not isinstance(seen, dict):
        return "unknown"
    states = [cast(dict[object, object], seen).get(path) for path in paths]
    if "present" in states:
        return "visible"
    if states and all(state == "absent" for state in states):
        return "hidden"
    return "unknown"
