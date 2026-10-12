# SPDX-License-Identifier: MIT
"""Acceptance helpers refuse unrelated state and incorrect synthetic fix claims."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path


def helper():
    return runpy.run_path(str(Path(__file__).parent / "fixtures/consistency_browser_fixture.py"))


@pytest.mark.asyncio
async def test_confirm_requires_created_marker_before_opening_a_writer(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    with pytest.raises(ValueError, match="created by this synthetic fixture"):
        await helper()["confirm"](absent)
    assert not absent.exists()


@pytest.mark.asyncio
async def test_scripted_confirmation_checks_exact_seeded_corrected_revision(
    tmp_path: Path, capsys
) -> None:
    fixture = helper()
    state = tmp_path / "synthetic-state"
    fixture["create"](state)
    marker = json.loads(capsys.readouterr().out)
    documents = DocumentStore(store_path(state))
    store = ReviewStore(documents)
    findings = Findings(store)
    try:
        document = marker["document"]
        finding = marker["finding"]
        documents.edit_source(
            "review-demo",
            document,
            base=1,
            text=fixture["REPORT"] + "\nThis still claims16.\n",
            author=USER,
            reason="Incorrect synthetic correction",
            operation_id="incorrect-edit",
        )
        findings.respond(
            "review-demo",
            "main",
            {
                "finding": finding,
                "expected": 1,
                "action": "fix",
                "explanation": "Incorrect claim must not be confirmed by the fixture.",
                "revisions": [[document, 2]],
                "operation_id": "incorrect-claim",
            },
            USER,
        )
        with pytest.raises(ValueError, match="correct the synthetic count to 15"):
            await fixture["confirm"](state)
        assert len(store.listing("review-demo", "main")["reviews"]) == 1
        assert findings.get("review-demo", "main", finding)["status"] == "claimed fixed"
        documents.edit_source(
            "review-demo",
            document,
            base=2,
            text=fixture["REPORT"].replace("count was 16.", "count was 15."),
            author=USER,
            reason="Correct synthetic count",
            operation_id="correct-edit",
        )
        findings.respond(
            "review-demo",
            "main",
            {
                "finding": finding,
                "expected": 2,
                "action": "fix",
                "explanation": "The exact seeded report now says15.",
                "revisions": [[document, 3]],
                "operation_id": "correct-claim",
            },
            USER,
        )
        await fixture["confirm"](state)
        outcome = json.loads(capsys.readouterr().out)
        assert outcome["finding"]["finding_id"] == finding
        assert outcome["finding"]["status"] == "confirmed fixed"
        assert outcome["review"]["resolved_model"] == "scripted"
        assert outcome["review"]["alias"] == "scripted-reviewer"
    finally:
        documents.close()
