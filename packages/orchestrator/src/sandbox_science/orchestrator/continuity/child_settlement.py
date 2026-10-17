# SPDX-License-Identifier: MIT
"""Typed child completion survives final-save/handback and ledger crash windows."""

from __future__ import annotations

from dataclasses import replace

from pydantic import TypeAdapter

from sandbox_science.orchestrator.aio import threaded
from sandbox_science.orchestrator.control import ControlError, ControlStore
from sandbox_science.orchestrator.control.errors import CONFLICT
from sandbox_science.orchestrator.control.migrations import Schema
from sandbox_science.orchestrator.delegation.hook import ChildEnd, observed
from sandbox_science.orchestrator.delegation.records import DelegationRecords, Ending
from sandbox_science.orchestrator.delegation.work import record_work
from sandbox_science.orchestrator.ledger import Ledger

PAYLOAD_BYTES = 524288
_ADAPTER = TypeAdapter(ChildEnd)


def install(schema: Schema) -> None:
    """Append-only payload with independently durable save and ledger receipts."""
    schema.execute("""CREATE TABLE research_child_settlements (
        project TEXT NOT NULL, session TEXT NOT NULL, run TEXT NOT NULL,
        delegation TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)
            AND length(CAST(payload AS BLOB))<=524288),
        saved_status INTEGER CHECK(saved_status IN (0,1)),
        settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)),
        PRIMARY KEY(project,session,run), UNIQUE(project,session,delegation),
        FOREIGN KEY(project,session,run) REFERENCES research_attempts(project,session,run),
        FOREIGN KEY(delegation) REFERENCES delegations(delegation_id)
    ) STRICT""")
    schema.execute("""CREATE TRIGGER research_child_settlement_identity BEFORE UPDATE
        ON research_child_settlements WHEN old.project!=new.project OR old.session!=new.session
        OR old.run!=new.run OR old.delegation!=new.delegation OR old.payload!=new.payload
        OR old.settled>new.settled OR (old.saved_status IS NOT NULL
        AND old.saved_status IS NOT new.saved_status)
        BEGIN SELECT RAISE(ABORT,'child settlement identity is immutable'); END""")
    schema.execute(
        "CREATE INDEX research_children_pending ON research_child_settlements "
        "(project,session,settled,run)"
    )


class ChildSettlement:
    """Persist exact typed evidence before final save, replay only saved attempts."""

    def __init__(self, control: ControlStore, ledger: Ledger) -> None:
        self.control, self.ledger = control, ledger

    def keep(self, project: str, session: str, run: str, delegation: str, end: ChildEnd) -> None:
        payload = _ADAPTER.dump_json(end).decode()
        if len(payload.encode()) > PAYLOAD_BYTES:
            raise ValueError("typed child outcome exceeds 512 KiB")
        with self.control.write() as tx:
            if (
                tx.sql.execute(
                    "SELECT 1 FROM delegations WHERE project_id=? AND session_id=? AND run=? "
                    "AND delegation_id=?",
                    (project, session, run, delegation),
                ).fetchone()
                is None
            ):
                raise ControlError(CONFLICT, "child checkpoint has no original delegation")
            prior = tx.sql.execute(
                "SELECT payload FROM research_child_settlements "
                "WHERE project=? AND session=? AND run=?",
                (project, session, run),
            ).fetchone()
            if prior is not None:
                if prior[0] != payload:
                    raise ControlError(CONFLICT, "child checkpoint already has another outcome")
                return
            tx.sql.execute(
                "INSERT INTO research_child_settlements "
                "(project,session,run,delegation,payload) VALUES (?,?,?,?,?)",
                (project, session, run, delegation, payload),
            )

    def page(
        self, project: str, session: str, *, after: str = "", root: str | None = None
    ) -> tuple[str, ...]:
        """Filter missing final records before LIMIT so they cannot starve saved children."""
        with self.control.read() as tx:
            return tuple(
                row[0]
                for row in tx.sql.execute(
                    "SELECT c.run FROM research_child_settlements c LEFT JOIN runs r "
                    "ON r.project_id=c.project AND r.session_id=c.session AND r.name=c.run "
                    "JOIN research_attempts a ON a.project=c.project AND a.session=c.session "
                    "AND a.run=c.run "
                    "JOIN research_work w ON w.project=a.project AND w.session=a.session "
                    "AND w.work=a.work "
                    "WHERE c.project=? AND c.session=? AND c.settled=0 AND c.run>? "
                    "AND (? IS NULL OR w.root=?) AND (r.name IS NOT NULL OR c.saved_status=1) "
                    "ORDER BY c.run LIMIT 100",
                    (project, session, after, root, root),
                )
            )

    def candidate(self, project: str, session: str, delegation: str) -> str | None:
        """A fallback cleanup must prefer a saved typed checkpoint over interruption."""
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT c.run FROM research_child_settlements c LEFT JOIN runs r "
                "ON r.project_id=c.project AND r.session_id=c.session AND r.name=c.run "
                "WHERE c.project=? AND c.session=? AND c.delegation=? "
                "AND (r.name IS NOT NULL OR c.saved_status=1)",
                (project, session, delegation),
            ).fetchone()
        return row[0] if row else None

    async def settle(self, project: str, session: str, run: str) -> ChildEnd:
        with self.control.read() as tx:
            row = tx.sql.execute(
                "SELECT c.delegation,c.payload,c.saved_status FROM "
                "research_child_settlements c LEFT JOIN runs r ON r.project_id=c.project "
                "AND r.session_id=c.session AND r.name=c.run "
                "WHERE c.project=? AND c.session=? AND c.run=? "
                "AND (r.name IS NOT NULL OR c.saved_status=1)",
                (project, session, run),
            ).fetchone()
        if row is None:
            raise ControlError(CONFLICT, "child outcome has no saved final run record")
        delegation, payload, saved_status = row
        end = _ADAPTER.validate_json(payload)
        if saved_status != 0:
            end = replace(
                end,
                stopped="persistence_error",
                failed=True,
                result=None,
                errors=(
                    *end.errors,
                    "the child's record or delivery acknowledgements could not be saved; "
                    "persistence not confirmed",
                ),
            )
        kept = await threaded(
            DelegationRecords(self.control).end,
            project,
            session,
            delegation,
            Ending(
                observed(end),
                end.stopped,
                end.final_text,
                end.refused,
                end.result,
                end.errors,
                end.alias,
                end.models,
                end.read_entry,
                end.read_proofs,
                end.files,
            ),
        )
        await record_work(self.ledger, project, session, kept, end.created)
        await threaded(self._settled, project, session, run)
        return end

    def _settled(self, project: str, session: str, run: str) -> None:
        with self.control.write() as tx:
            tx.sql.execute(
                "UPDATE research_child_settlements SET settled=1 "
                "WHERE project=? AND session=? AND run=?",
                (project, session, run),
            )

    async def drain(self, project: str, session: str, *, root: str | None = None) -> None:
        """Stable cursor, bounded memory, and no model or worker execution."""
        after = ""
        while page := await threaded(self.page, project, session, after=after, root=root):
            for run in page:
                await self.settle(project, session, run)
            after = page[-1]
