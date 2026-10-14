# SPDX-License-Identifier: MIT
"""The macro contexts a finding is shown with: its own (a math finding's is its check's),
the one its history shows, and each response body's."""

from __future__ import annotations

from typing import Any

from sandbox_science.orchestrator.consistency.models import FindingMacroContexts
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents.errors import INTERNAL, DocumentError
from sandbox_science.orchestrator.documents.project_macros import (
    MacroContext,
    finding_anchors,
    response_view,
    view,
)

from .jobs import MathJobs

Json = dict[str, Any]


def finding_macro_contexts(store: ReviewStore, project: str, finding: Json) -> FindingMacroContexts:
    """The macro contexts of ``finding``, as ``Findings.get`` or ``listing`` gives it."""
    documents = store.documents
    with documents.control.read() as tx:
        anchors = finding_anchors(tx.sql, project, finding["finding_id"])
    if finding["origin"] == "math":
        jobs = MathJobs(store)
        job = jobs.job(finding["math_check_id"])
        if job["project"] != project:
            raise DocumentError(INTERNAL, "finding check is outside its project")
        pinned = jobs.effective_context(job)
        own = (
            {**pinned, "macros": pinned["effective"]}
            if pinned
            else view(documents, project, historical=True)
        )
    else:
        own = view(documents, project, anchors=anchors)
    responses = {
        body["source"]: MacroContext.model_validate(
            response_view(documents, project, body["source"])
        )
        for entry in finding.get("history", [])
        if (body := entry.get("response_body"))
    }
    return FindingMacroContexts(
        finding=MacroContext.model_validate(own),
        history=MacroContext.model_validate(view(documents, project, historical=True)),
        responses=responses,
    )
