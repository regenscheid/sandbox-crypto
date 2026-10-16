# SPDX-License-Identifier: MIT
"""A ledger for tests about something other than provenance (CM-9).

An agent's change that creates an entry or changes what it says names an anchor, which
these tests do not set out to test. ``AnchoredLedger`` gives each such change and work
record of an agent that names none an anchor on a message of the agent's session: one
user message per session, archived once and found again by later ledgers on the same
store, so that a retried change is the same change. Imports keep the platform's own
anchors. Tests of provenance use ``Ledger``.
"""

from __future__ import annotations

from typing import Any

from sandbox_science.orchestrator.control import ControlError
from sandbox_science.orchestrator.documents import Author
from sandbox_science.orchestrator.history.archive import Archive, Entry
from sandbox_science.orchestrator.ledger import Ledger
from sandbox_science.orchestrator.ledger.models import AnchorInput, Mutation, MutationResult

Json = dict[str, Any]
SEED_RUN = "test-seed"


class AnchoredLedger(Ledger):
    def anchor(self, project: str, session: str) -> str | None:
        """The ID of the message the agents of ``session`` anchor on; none for a session
        the project does not hold, whose change the ledger refuses anyway."""
        archive = Archive(self.control)
        kept = archive.messages(project, session, run=SEED_RUN, limit=1)
        if kept:
            return kept[0].message_id
        seed = Entry(role="user", kind="user", content="Work on the project's questions.")
        try:
            return archive.append(project, session, SEED_RUN, seed).message_id
        except ControlError:
            return None

    def apply(
        self, project: str, mutations: list[tuple[Mutation, Json | None]], author: Author
    ) -> list[MutationResult]:
        seed = None
        if author.kind == "agent" and author.session_id is not None:
            seed = self.anchor(project, author.session_id)
        if seed is not None:
            mutations = [
                (_anchored(m, seed) if provenance is None else m, provenance)
                for m, provenance in mutations
            ]
        return super().apply(project, mutations, author)


def _anchored(mutation: Mutation, seed: str) -> Mutation:
    anchors = [AnchorInput(message=seed)]
    changes = [
        c if c.anchors is not None else c.model_copy(update={"anchors": anchors})
        for c in mutation.changes
    ]
    work = None
    if mutation.work is not None:
        work = [
            w if w.anchors is not None else w.model_copy(update={"anchors": anchors})
            for w in mutation.work
        ]
    return mutation.model_copy(update={"changes": changes, "work": work})
