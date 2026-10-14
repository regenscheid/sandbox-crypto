# SPDX-License-Identifier: MIT
"""Math checks of documents, comments and responses to findings.

This package depends on the documents and the consistency review, and neither depends
on it. A saved revision reaches it through the control store's events: each process
that saves documents calls ``subscribe`` on its store once, where it is assembled (the
harness, the document commands and the workspace).

- ``schedule``: the save's subscriber, which records what to check;
- ``jobs``: the checks themselves (``MathJobs``), leased and bounded;
- ``comments`` and ``responses``: the same for comment and response bodies;
- ``mechanical``: the findings a check raises and closes;
- ``notices``: the notices of those findings for the main agent;
- ``controller``: the run's turn hook that drives the checks;
- ``contexts``: the macro contexts a finding is shown with.
"""

from sandbox_science.orchestrator.documents.store import DocumentStore

from .schedule import REVISION_CREATED, record_event

__all__ = ["subscribe"]


def subscribe(documents: DocumentStore) -> None:
    """Schedule a math check of every revision saved through ``documents``."""
    documents.control.subscribe(REVISION_CREATED, record_event)
