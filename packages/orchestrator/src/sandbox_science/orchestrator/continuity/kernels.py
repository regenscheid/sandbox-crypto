# SPDX-License-Identifier: MIT
"""Remember kernel identities and bounded, opt-in cell reconstruction provenance."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from sandbox_science.contracts import (
    SYSTEM_CLOCK,
    Execution,
    ExecutionContext,
    ExecutionState,
    KernelInfo,
    KernelObservation,
    new_id,
)
from sandbox_science.contracts.worker import CellRequest
from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT
from sandbox_science.orchestrator.worker_client import WorkerClient

from .records import Ownership, check_owner


def _pure_python_assignments(code: str) -> bool:
    """Only literal assignments into fresh Python globals are automatic replay.

    No calls, attribute access, subscripts, imports, loops or expressions are run
    under this verifier. That excludes arbitrary external effects and nondeterminism
    even when a model marks a cell replayable.
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return False
    if not 1 <= len(tree.body) <= 32:
        return False
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            return False
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id.startswith("_"):
            return False
        try:
            value = ast.literal_eval(statement.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return False
        if type(value) is int:
            if value.bit_length() > 256:
                return False
        elif type(value) is str:
            if len(value.encode()) > 4096:
                return False
        elif type(value) not in (bool, type(None)):
            return False
    return True


def _unjournaled_cell(db: sqlite3.Connection, project: str, session: str) -> bool:
    """A dispatched cell without its own durable provenance makes replay unsafe."""
    return (
        db.execute(
            "SELECT 1 FROM tool_intents i WHERE i.project=? AND i.session=? "
            "AND i.tool IN ('python','sage') AND NOT EXISTS ("
            "SELECT 1 FROM research_observations o WHERE o.project=i.project "
            "AND o.session=i.session AND o.kind='kernel_cell' "
            "AND o.identity=i.operation_id) LIMIT 1",
            (project, session),
        ).fetchone()
        is not None
    )


class KernelJournal:
    def __init__(self, control: ControlStore, owner: Ownership, scope: str | None) -> None:
        self.control, self.owner, self.scope = control, owner, scope

    async def record(self, observation: KernelObservation) -> None:
        await threaded(self._record, observation)

    async def record_tool(self, record: dict[str, Any], observation: KernelObservation) -> None:
        await threaded(self._record_tool, record, observation)

    def _record(self, observation: KernelObservation) -> None:
        owner = self.owner
        kernels = [k.model_dump(mode="json") for k in observation.kernels if k.run == self.scope]
        # An absent observation cannot erase evidence that a kernel existed.
        if not kernels:
            return
        encoded = json.dumps(kernels)
        if len(encoded.encode()) > 16384:
            raise ValueError("kernel identity observation exceeds its storage bound")
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            self._save_snapshot(tx.sql, encoded)

    def _save_snapshot(self, db: sqlite3.Connection, encoded: str) -> None:
        owner = self.owner
        db.execute(
            "INSERT INTO research_observations VALUES (?,?,?,'kernels','latest',?,?) "
            "ON CONFLICT(project,session,work,kind,identity) DO UPDATE "
            "SET detail=excluded.detail,observed=excluded.observed",
            (owner.project, owner.session, owner.work, SYSTEM_CLOCK.now().timestamp(), encoded),
        )

    def _record_tool(self, record: dict[str, Any], observation: KernelObservation) -> None:
        owner = self.owner
        kernels = [k.model_dump(mode="json") for k in observation.kernels if k.run == self.scope]
        if not kernels:
            return
        encoded = json.dumps(kernels)
        if len(encoded.encode()) > 16384:
            raise ValueError("kernel identity observation exceeds its storage bound")
        detail: dict[str, Any] | None = None
        raw = record.get("execution")
        if record.get("tool") in {"python", "sage"} and isinstance(raw, dict):
            execution = Execution.model_validate(raw)
            result = execution.result
            kernel = result.kernel if result is not None else execution.kernel
            if (
                kernel is not None
                and kernel.run == self.scope
                and kernel.language == record["tool"]
                and result is not None
                and result.execution_count is not None
            ):
                try:
                    arguments: object = json.loads(record.get("arguments") or "{}")
                except (ValueError, TypeError):
                    arguments = {}
                values = cast(dict[str, object], arguments) if isinstance(arguments, dict) else {}
                code = values.get("code")
                text = code if isinstance(code, str) else ""
                safe = (
                    values.get("replayable") is True
                    and record.get("kernel_replay_binding_free") is True
                    and execution.state == ExecutionState.SUCCEEDED
                    and result.status == "ok"
                    and not result.written_files
                    and not result.deleted_files
                    and not result.written_files_truncated
                    and not result.deleted_files_truncated
                    and not result.overlapping_change
                    and 0 < len(text.encode()) <= 8192
                    and kernel.language == "python"
                    and _pure_python_assignments(text)
                )
                detail = {
                    "operation": execution.operation_id,
                    "language": kernel.language,
                    "kernel_id": kernel.kernel_id,
                    "generation": kernel.generation,
                    "run": kernel.run,
                    "count": result.execution_count,
                    "code_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "code": text if safe else None,
                    "replayable": safe,
                    "timeout_seconds": values.get("timeout_seconds", 120),
                    "environment_revision": result.environment_revision,
                    "current_revision": kernel.current_revision,
                }
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            if detail is not None:
                saved = json.dumps(detail, separators=(",", ":"))
                if len(saved.encode()) > 16384:
                    raise ValueError("kernel cell provenance exceeds its storage bound")
                tx.sql.execute(
                    "INSERT INTO research_observations VALUES (?,?,?,'kernel_cell',?,?,?) "
                    "ON CONFLICT(project,session,work,kind,identity) DO NOTHING",
                    (
                        owner.project,
                        owner.session,
                        owner.work,
                        detail["operation"],
                        SYSTEM_CLOCK.now().timestamp(),
                        saved,
                    ),
                )
            self._save_snapshot(tx.sql, encoded)

    async def reconcile(
        self,
        client: WorkerClient,
        context: ExecutionContext,
        *,
        accept_loss: bool = False,
        accepted_by: Literal["user", "automatic"] = "user",
    ) -> None:
        """Bring the work's kernel back, or recompute from recorded history (RC-11, D100).

        Without ``accept_loss`` a lost kernel that cannot be rebuilt exactly stops
        recovery. Every Resume passes it (D100): the loss is recorded with what was lost
        and who accepted it, the work expects no kernel, its next cell starts a clean one,
        and the resumed agent's view says the kernel stopped and its variables are gone
        (``KERNEL_LINES``), so the agent recomputes them from its recorded cells. A partial
        reconstruction is then acknowledged automatically, with the same provenance record
        an operator's ``kernel_recompute`` decision leaves, and discarded."""
        pending = await threaded(self._pending_restore)
        if pending is not None:
            work, identity, detail, observed_at = pending
            snapshot_sha256 = detail.get("snapshot_sha256")
            acknowledged = isinstance(snapshot_sha256, str) and await threaded(
                self._recompute_acknowledged, work, observed_at, snapshot_sha256
            )
            if not acknowledged and not accept_loss:
                raise ControlError(
                    CONFLICT, "lost_kernel: partial reconstruction needs operator review"
                )
            language = detail.get("language")
            run = detail.get("run")
            if language not in ("python", "sage") or run != self.scope:
                raise ControlError(CONFLICT, "lost_kernel: reconstruction scope is uncertain")
            observed = await client.observe_kernels(context)
            live = any(k.language == language and k.run == self.scope for k in observed.kernels)
            if live and not accept_loss:
                raise ControlError(
                    CONFLICT, "lost_kernel: partial kernel is live; close it before recompute"
                )
            if not acknowledged:
                await threaded(
                    self._acknowledge_recompute, work, identity, snapshot_sha256, accepted_by
                )
            if live:
                # Partly rebuilt state is not the recorded state: start a clean
                # generation in its place. No recorded cell is run again here.
                await client.restart_kernel(
                    context, "python" if language == "python" else "sage", self.scope
                )
            # Clear the saved lineage atomically after the decision; the next cell
            # lazily starts a clean kernel.
            await threaded(self._abandon_restore, work, identity)
            return
        expected = await threaded(self._expected)
        if not expected:
            return
        observation = await client.observe_kernels(context)
        actual = {
            (k.language, k.kernel_id, k.generation)
            for k in observation.kernels
            if k.run == self.scope
        }
        missing = expected - actual
        if not missing:
            return
        # Never destroy a different live kernel or reconstruct multiple independent
        # histories in one uncheckpointed operation.
        if len(missing) != 1 or any(
            k.language == next(iter(missing))[0] for k in observation.kernels if k.run == self.scope
        ):
            if accept_loss:
                await threaded(self._accept_loss, missing, accepted_by)
                return
            raise ControlError(
                CONFLICT, "lost_kernel: saved kernel identity or generation is unavailable"
            )
        language, kernel_id, generation = next(iter(missing))
        if language not in ("python", "sage"):
            if accept_loss:
                await threaded(self._accept_loss, missing, accepted_by)
                return
            raise ControlError(CONFLICT, "lost_kernel: saved kernel language is unavailable")
        typed_language = language
        cells = await threaded(self._replay_cells, language, kernel_id, generation)
        if cells is None:
            if accept_loss:
                await threaded(self._accept_loss, missing, accepted_by)
                return
            raise ControlError(
                CONFLICT, "lost_kernel: complete replayable cell provenance is unavailable"
            )
        identity = hashlib.sha256(f"{language}\0{kernel_id}\0{generation}".encode()).hexdigest()
        try:
            await self._rebuild(client, context, typed_language, identity, cells)
        except ControlError:
            if not accept_loss:
                raise
            # The exact rebuild failed: acknowledge, discard any partial kernel and
            # continue with the loss reported (D100). The pending branch never replays.
            if await threaded(self._pending_restore) is not None:
                await self.reconcile(client, context, accept_loss=True, accepted_by=accepted_by)
            else:
                await threaded(self._accept_loss, missing, accepted_by)

    async def _rebuild(
        self,
        client: WorkerClient,
        context: ExecutionContext,
        typed_language: Literal["python", "sage"],
        identity: str,
        cells: list[dict[str, Any]],
    ) -> None:
        """Replay verified literal cells into a fresh kernel, under a durable intent."""
        language = typed_language
        kernel_id, generation = cells[0]["kernel_id"], cells[0]["generation"]
        await threaded(self._begin_restore, identity, cells)
        # The durable intent deliberately remains pending if restart/replay has an
        # unknown or failed outcome. An operator can inspect it and choose recovery;
        # the next Resume never silently repeats these effects.
        replacement = await client.restart_kernel(
            context, typed_language, self.scope, only_if_absent=True
        )
        if (
            replacement.kernel_id == kernel_id and replacement.generation == generation
        ) or replacement.current_revision != cells[0]["current_revision"]:
            raise ControlError(
                CONFLICT, "lost_kernel: replacement kernel or package revision differs"
            )
        restored: list[dict[str, Any]] = []
        for index, cell in enumerate(cells, start=1):
            operation = f"restore-{identity[:20]}-{index}-{new_id('cell')[:12]}"
            request = CellRequest(
                operation_id=operation,
                context=context,
                language=typed_language,
                run=self.scope,
                code=cell["code"],
                timeout_seconds=cell["timeout_seconds"],
            )
            execution = await client.run(request)
            result = execution.result
            actual_kernel = result.kernel if result is not None else None
            if (
                execution.state != ExecutionState.SUCCEEDED
                or execution.operation_id != operation
                or execution.context != context
                or execution.kind != "cell"
                or result is None
                or result.status != "ok"
                or result.execution_count != index
                or actual_kernel is None
                or (actual_kernel.kernel_id, actual_kernel.generation)
                != (replacement.kernel_id, replacement.generation)
            ):
                raise ControlError(
                    CONFLICT, "lost_kernel: replayed cell did not rebuild exact state"
                )
            restored.append(
                {
                    **cell,
                    "operation": operation,
                    "kernel_id": replacement.kernel_id,
                    "generation": replacement.generation,
                    "current_revision": replacement.current_revision,
                    "environment_revision": result.environment_revision,
                    "replayed_from": cell["operation"],
                }
            )
        rebuilt = await client.observe_kernels(context)
        if not any(
            (k.language, k.kernel_id, k.generation)
            == (language, replacement.kernel_id, replacement.generation)
            and k.run == self.scope
            for k in rebuilt.kernels
        ):
            raise ControlError(CONFLICT, "lost_kernel: rebuilt kernel is no longer observable")
        await threaded(self._finish_restore, identity, rebuilt, restored)

    def _replay_cells(
        self, language: str, kernel_id: str, generation: int
    ) -> list[dict[str, Any]] | None:
        owner = self.owner
        with self.control.read() as tx:
            # A tool intent is saved before dispatch, while the kernel journal is
            # saved after the archived result. A crash between those commits must
            # make reconstruction fail closed, even if the rows we did journal form
            # a contiguous prefix. Include every cell in the session because the
            # main kernel can outlive multiple main work items.
            if _unjournaled_cell(tx.sql, owner.project, owner.session):
                return None
            rows = tx.sql.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? "
                "AND kind='kernel_cell' AND json_extract(detail,'$.language')=? "
                "AND json_extract(detail,'$.kernel_id')=? "
                "AND json_extract(detail,'$.generation')=? ORDER BY rowid LIMIT 65",
                (owner.project, owner.session, language, kernel_id, generation),
            ).fetchall()
        if not 1 <= len(rows) <= 64:
            return None
        cells = [json.loads(row[0]) for row in rows]
        revision = cells[0]["current_revision"]
        if revision is None or sum(len(cell.get("code") or "") for cell in cells) > 65536:
            return None
        for index, cell in enumerate(cells, start=1):
            code = cell.get("code")
            if (
                cell.get("count") != index
                or cell.get("replayable") is not True
                or not isinstance(code, str)
                or not code
                or cell.get("run") != self.scope
                or cell.get("current_revision") != revision
                or hashlib.sha256(code.encode()).hexdigest() != cell.get("code_sha256")
            ):
                return None
        return cells

    def _begin_restore(self, identity: str, cells: list[dict[str, Any]]) -> None:
        owner = self.owner
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            # Recheck inside the intent transaction: a newly admitted cell
            # cannot slip between the read-only lineage scan and first effect.
            if _unjournaled_cell(tx.sql, owner.project, owner.session):
                raise ControlError(CONFLICT, "lost_kernel: cell lineage changed before replay")
            prior = tx.sql.execute(
                "SELECT 1 FROM research_observations WHERE project=? AND session=? "
                "AND kind='kernel_restore' AND identity=?",
                (owner.project, owner.session, identity),
            ).fetchone()
            if prior is not None:
                raise ControlError(
                    CONFLICT, "lost_kernel: prior reconstruction intent needs review"
                )
            snapshot = tx.sql.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? "
                "AND work=? AND kind='kernels' AND identity='latest'",
                (owner.project, owner.session, owner.work),
            ).fetchone()
            if snapshot is None:
                raise ControlError(CONFLICT, "lost_kernel: saved snapshot disappeared")
            tx.sql.execute(
                "INSERT INTO research_observations VALUES (?,?,?,'kernel_restore',?,?,?)",
                (
                    owner.project,
                    owner.session,
                    owner.work,
                    identity,
                    SYSTEM_CLOCK.now().timestamp(),
                    json.dumps(
                        {
                            "state": "intent",
                            "cells": len(cells),
                            "language": cells[0].get("language"),
                            "kernel_id": cells[0].get("kernel_id"),
                            "generation": cells[0].get("generation"),
                            "run": cells[0].get("run"),
                            "snapshot_sha256": hashlib.sha256(snapshot[0].encode()).hexdigest(),
                        }
                    ),
                ),
            )

    def _finish_restore(
        self, identity: str, observation: KernelObservation, restored: list[dict[str, Any]]
    ) -> None:
        owner = self.owner
        kernels = [k.model_dump(mode="json") for k in observation.kernels if k.run == self.scope]
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            tx.sql.execute(
                "UPDATE research_observations SET detail=?, observed=? WHERE project=? "
                "AND session=? AND work=? AND kind='kernel_restore' AND identity=? "
                "AND json_extract(detail,'$.state')='intent'",
                (
                    json.dumps({"state": "done", "cells": len(restored)}),
                    SYSTEM_CLOCK.now().timestamp(),
                    owner.project,
                    owner.session,
                    owner.work,
                    identity,
                ),
            )
            if tx.sql.execute("SELECT changes()").fetchone()[0] != 1:
                raise ControlError(CONFLICT, "lost_kernel: reconstruction ownership changed")
            for cell in restored:
                saved = json.dumps(cell, separators=(",", ":"))
                if len(saved.encode()) > 16384:
                    raise ValueError("kernel cell provenance exceeds its storage bound")
                tx.sql.execute(
                    "INSERT INTO research_observations VALUES (?,?,?,'kernel_cell',?,?,?)",
                    (
                        owner.project,
                        owner.session,
                        owner.work,
                        cell["operation"],
                        SYSTEM_CLOCK.now().timestamp(),
                        saved,
                    ),
                )
            self._save_snapshot(tx.sql, json.dumps(kernels))

    def _pending_restore(self) -> tuple[str, str, dict[str, Any], float] | None:
        owner = self.owner
        with self.control.read() as tx:
            rows = tx.sql.execute(
                "SELECT work,identity,detail,observed FROM research_observations "
                "WHERE project=? AND session=? AND kind='kernel_restore' "
                "AND json_extract(detail,'$.state')='intent' "
                "AND json_extract(detail,'$.run') IS ? ORDER BY observed LIMIT 2",
                (owner.project, owner.session, self.scope),
            ).fetchall()
        if len(rows) > 1:
            raise ControlError(CONFLICT, "lost_kernel: multiple reconstruction intents need review")
        if not rows:
            return None
        work, identity, encoded, observed = rows[0]
        return work, identity, json.loads(encoded), observed

    def _acknowledge_recompute(
        self, work: str, identity: str, snapshot: object, accepted_by: str
    ) -> None:
        """The provenance an operator's ``kernel_recompute`` decision leaves, made by Resume."""
        owner = self.owner
        detail = json.dumps(
            {
                "resolution": {
                    "kind": "kernel_recompute",
                    "identity": snapshot if isinstance(snapshot, str) else None,
                    "restore": identity,
                    "reason": "lost kernel recomputed from recorded history under Resume",
                },
                "operator": accepted_by,
                "automatic": True,
            },
            separators=(",", ":"),
        )
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            tx.sql.execute(
                "INSERT INTO research_observations "
                "VALUES (?,?,?,'resolution:kernel_recompute',?,?,?) "
                "ON CONFLICT(project,session,work,kind,identity) DO NOTHING",
                (
                    owner.project,
                    owner.session,
                    work,
                    "auto-" + identity,
                    SYSTEM_CLOCK.now().timestamp(),
                    detail,
                ),
            )

    def _accept_loss(self, missing: set[tuple[str, str, int]], accepted_by: str = "user") -> None:
        """Record the kernels accepted as lost and by whom; expect none for the work."""
        owner = self.owner
        lost = [
            {"language": language, "kernel_id": kernel_id, "generation": generation}
            for language, kernel_id, generation in sorted(missing)
        ]
        detail = json.dumps({"lost": lost, "accepted_by": accepted_by}, separators=(",", ":"))
        identity = hashlib.sha256(detail.encode()).hexdigest()
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            tx.sql.execute(
                "INSERT INTO research_observations VALUES (?,?,?,'kernel_loss_accepted',?,?,?) "
                "ON CONFLICT(project,session,work,kind,identity) DO NOTHING",
                (
                    owner.project,
                    owner.session,
                    owner.work,
                    identity,
                    SYSTEM_CLOCK.now().timestamp(),
                    detail,
                ),
            )
            self._save_snapshot(tx.sql, "[]")

    def _recompute_acknowledged(self, work: str, since: float, snapshot: str) -> bool:
        owner = self.owner
        with self.control.read() as tx:
            return (
                tx.sql.execute(
                    "SELECT 1 FROM research_observations WHERE project=? AND session=? "
                    "AND work=? AND kind='resolution:kernel_recompute' AND observed>=? "
                    "AND json_extract(detail,'$.resolution.identity')=? LIMIT 1",
                    (owner.project, owner.session, work, since, snapshot),
                ).fetchone()
                is not None
            )

    def _abandon_restore(self, work: str, identity: str) -> None:
        owner = self.owner
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            tx.sql.execute(
                "UPDATE research_observations SET detail=?,observed=? WHERE project=? "
                "AND session=? AND work=? AND kind='kernel_restore' AND identity=? "
                "AND json_extract(detail,'$.state')='intent'",
                (
                    json.dumps({"state": "abandoned", "operator_recompute": True}),
                    SYSTEM_CLOCK.now().timestamp(),
                    owner.project,
                    owner.session,
                    work,
                    identity,
                ),
            )
            if tx.sql.execute("SELECT changes()").fetchone()[0] != 1:
                raise ControlError(CONFLICT, "lost_kernel: reconstruction intent changed")
            self._save_snapshot(tx.sql, "[]")

    def _expected(self) -> set[tuple[str, str, int]]:
        owner = self.owner
        with self.control.write() as tx:
            check_owner(tx.sql, owner)
            row = tx.sql.execute(
                "SELECT detail FROM research_observations WHERE project=? AND session=? AND work=? "
                "AND kind='kernels' AND identity='latest'",
                (owner.project, owner.session, owner.work),
            ).fetchone()
            if row is None and self.scope is None:
                # The main Python/Sage kernel belongs to the session, not to one
                # agent work item. A new main work can inherit it and lose it
                # before making its first successful passive observation.
                prior = tx.sql.execute(
                    "SELECT o.detail,o.observed FROM research_observations o "
                    "JOIN research_work w USING(project,session,work) "
                    "WHERE o.project=? AND o.session=? AND o.kind='kernels' "
                    "AND o.identity='latest' AND w.role='main' "
                    "ORDER BY o.observed DESC,o.rowid DESC LIMIT 1",
                    (owner.project, owner.session),
                ).fetchone()
                if prior is not None:
                    detail, observed = prior
                    acknowledged = tx.sql.execute(
                        "SELECT 1 FROM research_observations WHERE project=? AND session=? "
                        "AND kind='resolution:kernel_recompute' AND observed>=? LIMIT 1",
                        (owner.project, owner.session, observed),
                    ).fetchone()
                    if acknowledged is None or detail == "[]":
                        # A manual restart after the last snapshot makes its
                        # lineage uncertain until a later observation exists.
                        restarted = tx.sql.execute(
                            "SELECT 1 FROM tool_intents WHERE project=? AND session=? "
                            "AND tool='restart_kernel' AND julianday(created)>"
                            "(2440587.5 + ?/86400.0) LIMIT 1",
                            (owner.project, owner.session, observed),
                        ).fetchone()
                        if restarted is not None:
                            raise ControlError(
                                CONFLICT, "lost_kernel: restart after saved identity needs review"
                            )
                        self._save_snapshot(tx.sql, detail)
                        row = (detail,)
        return (
            set()
            if row is None
            else {(k["language"], k["kernel_id"], k["generation"]) for k in json.loads(row[0])}
        )


MAX_LOST_CELLS: Final = 20
"""The cells of a lost kernel generation a cell's result lists, the newest (Kernels 7)."""
MAX_SAVED_FILES: Final = 20
"""The files those cells wrote that the result lists, the newest."""
_LINE_CHARS: Final = 120
_PATH_CHARS: Final = 256


@dataclass(frozen=True)
class LostCell:
    """One cell of a lost generation, as the records keep it: its archived result's
    message ID and its call's purpose, or, for a call without a purpose, its tool and its
    code's first line. A cell the archive does not hold (a replayed one) has neither."""

    language: str
    count: int | None
    message: str | None = None
    purpose: str | None = None
    first_line: str | None = None


@dataclass(frozen=True)
class LostGeneration:
    """The newest ``MAX_LOST_CELLS`` cells of a lost generation, oldest first, how many
    earlier ones are left out, and the paths they wrote and did not delete since, the
    most recently written last."""

    cells: tuple[LostCell, ...]
    earlier: int
    written: tuple[str, ...]


class LostKernels:
    """What a kernel that lost its state had computed, read from the records: the kernel
    cell observations ``KernelJournal`` keeps after each cell, and the archive's tool
    records and purposes. Nothing here reads the model's context or writes."""

    def __init__(self, control: ControlStore, project: str, session: str, scope: str | None):
        self.control, self.project, self.session, self.scope = control, project, session, scope

    async def lost(self, language: str, current: KernelInfo | None) -> LostGeneration | None:
        """The generation of ``language``'s kernel (the session's, or the scope's run's)
        whose cells the records keep last, other than ``current``: the one whose state a
        cell's ``state_lost`` reports gone. ``None`` when the records hold none."""
        return await threaded(self._lost, language, current)

    def _lost(self, language: str, current: KernelInfo | None) -> LostGeneration | None:
        with self.control.read() as tx:
            db = tx.sql
            found = db.execute(
                "SELECT json_extract(detail,'$.kernel_id'),json_extract(detail,'$.generation') "
                "FROM research_observations WHERE project=? AND session=? AND kind='kernel_cell' "
                "AND json_extract(detail,'$.run') IS ? AND json_extract(detail,'$.language')=? "
                "AND NOT (json_extract(detail,'$.kernel_id') IS ? "
                "AND json_extract(detail,'$.generation') IS ?) ORDER BY observed DESC,rowid DESC "
                "LIMIT 1",
                (
                    self.project,
                    self.session,
                    self.scope,
                    language,
                    current.kernel_id if current is not None else None,
                    current.generation if current is not None else None,
                ),
            ).fetchone()
            if found is None:
                return None
            rows = db.execute(
                "SELECT identity,json_extract(detail,'$.count') FROM research_observations "
                "WHERE project=? AND session=? AND kind='kernel_cell' "
                "AND json_extract(detail,'$.run') IS ? AND json_extract(detail,'$.language')=? "
                "AND json_extract(detail,'$.kernel_id')=? "
                "AND json_extract(detail,'$.generation')=? "
                "ORDER BY json_extract(detail,'$.count'),rowid",
                (self.project, self.session, self.scope, language, *found),
            ).fetchall()
            cells: list[LostCell] = []
            written: dict[str, None] = {}
            for operation, count in rows:
                record = db.execute(
                    "SELECT t.message_id,m.purpose,json_extract(t.record,'$.tool'),"
                    "substr(CASE WHEN json_valid(json_extract(t.record,'$.arguments')) THEN "
                    "json_extract(json_extract(t.record,'$.arguments'),'$.code') END,1,4096),"
                    "json_extract(t.record,'$.recorded_files.files'),"
                    "json_extract(t.record,'$.execution.result.written_files'),"
                    "json_extract(t.record,'$.execution.result.deleted_files') "
                    "FROM archive_tool_records t JOIN archive_messages m "
                    "ON m.project=t.project AND m.message_id=t.message_id "
                    "WHERE t.project=? AND t.session=? AND t.operation_id=? LIMIT 1",
                    (self.project, self.session, operation),
                ).fetchone()
                if record is None:
                    cells.append(LostCell(language, count))
                    continue
                message, purpose, tool, code, recorded, wrote, deleted = record
                cells.append(
                    LostCell(
                        str(tool or language),
                        count,
                        message,
                        purpose,
                        None if purpose is not None else _first_line(code),
                    )
                )
                _changed(written, recorded, wrote, deleted)
        shown = tuple(cells[-MAX_LOST_CELLS:])
        return LostGeneration(shown, len(cells) - len(shown), tuple(written))


def _first_line(code: object) -> str:
    lines = [line.strip() for line in str(code or "").splitlines() if line.strip()]
    first = lines[0] if lines else ""
    return first if len(first) <= _LINE_CHARS else first[: _LINE_CHARS - 1] + "…"


def _changed(written: dict[str, None], recorded: object, wrote: object, deleted: object) -> None:
    """Apply one call's files to ``written``, in order: the recorder's revisions and
    tombstones when it kept them, otherwise what the worker reported."""
    files = _json_list(recorded)
    if files:
        for item in files:
            if not isinstance(item, dict):
                continue
            entry = cast(dict[str, object], item)
            path = entry.get("path")
            if isinstance(path, str) and len(path) <= _PATH_CHARS:
                written.pop(path, None)
                if entry.get("deleted") is not True:
                    written[path] = None
        return
    for path in _json_list(wrote):
        if isinstance(path, str) and len(path) <= _PATH_CHARS:
            written.pop(path, None)
            written[path] = None
    for path in _json_list(deleted):
        if isinstance(path, str):
            written.pop(path, None)


def _json_list(value: object) -> list[object]:
    if not isinstance(value, str):
        return []
    try:
        found: object = json.loads(value)
    except (ValueError, RecursionError):
        return []
    return cast(list[object], found) if isinstance(found, list) else []
