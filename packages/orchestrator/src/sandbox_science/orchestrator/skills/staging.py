# SPDX-License-Identifier: MIT
"""Helper staging: a loaded skill's pack in the session's environment, and its bindings
in the session's executions (docs/specs/skills.md, SK-16, SK-31).

When a run loads a skill whose folder holds more than Markdown, the pack's snapshot is
staged once per revision below the environment's root, read-only, by the worker
(``/v1/skills/check`` and ``/v1/skills/put``). Sandbox code can still change the files,
so each later load checks every file's SHA-256 first and stages the snapshot again when
one differs. The run's shell, job and cell executions then carry the pack's bindings the
project sets, namespaced to the pack or approved (``allow.py``), as paths in the staged
pack; those held are named, with why, and never set. Each staging is recorded with the
revision, the files, the attempt and how it went (SK-21).
"""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Any, Final

from sandbox_science.contracts import (
    ExecutionContext,
    StageCheckRequest,
    StagedContent,
    StagedFile,
    StagePutRequest,
)

from .store import Entry, SkillStore

if TYPE_CHECKING:
    from sandbox_science.orchestrator.worker_client import WorkerClient

BATCH_BYTES: Final = 6 * 1024 * 1024
"""Content one put carries at most, base64 included, within the worker's request size."""
MAX_NAMED: Final = 20
"""Paths a staging record names of those that differed; it counts the rest."""


class Stager:
    """A run's staged packs and the bindings its executions get."""

    def __init__(
        self,
        client: WorkerClient,
        context: ExecutionContext,
        store: SkillStore,
        derived: tuple[str, str] | None,
    ) -> None:
        self.client, self.context, self.store, self.derived = client, context, store, derived
        self.bindings: dict[str, str] = {}
        """What the run's executions add to their environment: the variables set, as paths
        in the staged packs."""
        self.attempts: dict[str, int] = {}
        self.checked: set[str] = set()
        """The snapshots this run staged or found staged."""
        self._locks: dict[str, asyncio.Lock] = {}

    async def stage(self, entry: Entry) -> dict[str, Any] | None:
        """Stage ``entry``'s pack when the skill has helper code, checking what is
        there first; the record of it, or ``None`` when there is nothing to stage."""
        manifest = await asyncio.to_thread(self.store.manifest, entry.snapshot)
        own = f"{entry.name}/"
        if not any(p.startswith(own) and not p.endswith(".md") for p, *_ in manifest):
            return None
        files = tuple(StagedFile(path=p, sha256=s, size=n, executable=x) for p, s, n, x in manifest)
        record: dict[str, Any] = {
            "id": entry.qualified,
            "source": entry.source,
            "revision": entry.revision,
            "snapshot": entry.snapshot,
            "files": len(files),
        }
        async with self._locks.setdefault(entry.snapshot, asyncio.Lock()):
            found = await self._check(entry, files)
            if found.ready:
                record |= {"location": found.location, "state": "unchanged"}
            else:
                attempt = self.attempts[entry.snapshot] = self.attempts.get(entry.snapshot, 0) + 1
                fresh = set(found.differing) == {file.path for file in files}
                record |= {"attempt": attempt, "state": "staged" if fresh else "restaged"}
                if not fresh:
                    record["differing"] = list(found.differing[:MAX_NAMED])
                    record["differing_count"] = len(found.differing)
                await self._put(entry, files)
                found = await self._check(entry, files)
                record["location"] = found.location
                if not found.ready:
                    record["state"] = "failed"
                    return record
        self.checked.add(entry.snapshot)
        bound = await asyncio.to_thread(self.store.bindings, self.context.project_id, self.derived)
        given = {
            name: found.location + (f"/{path}" if path else "")
            for name, path in bound.set.get(entry.source, {}).items()
        }
        self.bindings.update(given)
        record["bindings"] = given
        if held := bound.held.get(entry.source):
            record["held"] = [{"variable": h["variable"], "reason": h["reason"]} for h in held]
        return record

    async def _check(self, entry: Entry, files: tuple[StagedFile, ...]) -> Any:
        request = StageCheckRequest(
            context=self.context,
            source=entry.source,
            snapshot=entry.snapshot,
            files=files,
            seal=True,
        )
        return await self.client.stage_check(request)

    async def _put(self, entry: Entry, files: tuple[StagedFile, ...]) -> None:
        """The snapshot's files in batches, the first removing what is there."""
        batch: list[StagedContent] = []
        size, first = 0, True
        for file in files:
            content = await asyncio.to_thread(self.store.file, entry.snapshot, file.path)
            encoded = base64.b64encode(content or b"").decode()
            if batch and size + len(encoded) > BATCH_BYTES:
                await self._send(entry, batch, reset=first)
                batch, size, first = [], 0, False
            batch.append(
                StagedContent(path=file.path, content_b64=encoded, executable=file.executable)
            )
            size += len(encoded)
        await self._send(entry, batch, reset=first)

    async def _send(self, entry: Entry, batch: list[StagedContent], *, reset: bool) -> None:
        request = StagePutRequest(
            context=self.context,
            source=entry.source,
            snapshot=entry.snapshot,
            reset=reset,
            files=tuple(batch),
        )
        await self.client.stage_put(request)


def told(records: list[dict[str, Any]]) -> str:
    """What a load's answer says of its stagings: where each pack is, what the executions
    get and what is held."""
    lines: list[str] = []
    for record in records:
        if record["state"] == "failed":
            lines.append(
                f"{record['source']}'s helper files could not be staged; run nothing from it."
            )
            continue
        bound = ", ".join(f"{k}={v}" for k, v in record.get("bindings", {}).items()) or "none"
        line = (
            f"{record['source']}'s files are staged read-only at {record['location']}; "
            f"shell, job and new kernel executions get: {bound}."
        )
        if held := record.get("held"):
            line += " Held, not set: " + "; ".join(f"{h['variable']} ({h['reason']})" for h in held)
        lines.append(line)
    return "\n".join(lines)
