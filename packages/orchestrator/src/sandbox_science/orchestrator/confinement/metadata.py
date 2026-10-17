# SPDX-License-Identifier: MIT
"""Deferred startup skill metadata, evaluated under an admitted attempt in the VM."""

from __future__ import annotations

import logging

from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.harness import Admitted
from sandbox_science.orchestrator.model import ChatModel
from sandbox_science.orchestrator.skills.derive import VERSION, PassError, derive
from sandbox_science.orchestrator.skills.packs import Snapshot, snapshot
from sandbox_science.orchestrator.skills.sources import Fetched, PackFile
from sandbox_science.orchestrator.skills.store import Pin, SkillStore

_log = logging.getLogger(__name__)


def restored(store: SkillStore, pin: Pin) -> Snapshot:
    imported = pin.pinned
    files: list[PackFile] = []
    for path, _, size, executable in store.manifest(imported.snapshot):
        content = store.file(imported.snapshot, path)
        if content is None or len(content) != size:
            raise ValueError("pinned skill snapshot is incomplete")
        files.append(PackFile(path, content, executable))
    result = snapshot(
        Fetched(
            imported.kind,
            imported.origin,
            imported.root,
            tuple(files),
            (),
            ref=imported.ref,
            commit=imported.commit,
            archive_sha256=imported.archive_sha256,
        )
    )
    if result.sha256 != imported.snapshot:
        raise ValueError("pinned skill snapshot digest changed")
    return result


class Metadata:
    def __init__(self, store: SkillStore, model: ChatModel, alias: str) -> None:
        self.store, self.model, self.alias = store, model, alias

    async def prepare(self, admitted: Admitted) -> None:
        if admitted.request.parent is not None:
            return
        project = admitted.context.project_id
        for pin in await threaded(self.store.pins, project):
            if (
                pin.source == "builtin"
                or await threaded(self.store.metadata, pin.pinned.snapshot, self.alias, VERSION)
                is not None
            ):
                continue
            snap = await threaded(restored, self.store, pin)
            try:
                made = await derive(self.model, snap, self.alias)
            except PassError as error:
                _log.warning("runtime skill metadata unavailable: %s", error)
                if error.cost is not None:
                    await threaded(
                        self.store.add_metadata_call,
                        project,
                        snap.sha256,
                        self.alias,
                        (None, None),
                        error.cost,
                        "gateway",
                        intent_id=error.intent_id,
                    )
                continue
            await threaded(
                self.store.add_metadata_call,
                project,
                snap.sha256,
                made.model,
                made.tokens,
                made.cost,
                made.cost_source,
                intent_id=made.intent_id,
            )
            await threaded(
                self.store.add_metadata,
                snap.sha256,
                self.alias,
                VERSION,
                made.model,
                made.derived,
                made.dropped,
            )
