# SPDX-License-Identifier: MIT
"""Cryptographic-claims checks (the owner, 2026-10-23; rubric ``sandbox-consistency-9``,
D56 amended; docs/CONSISTENCY-REVIEW.md).

A. The rubric names the seven categories, the severity rule, what not to flag, and the
   reviewer's stance: it reads, computes no cost and runs nothing.
B. The finding model and the reviewer's tool accept each category.
C. A scripted reviewer's warning on prose and failure on a saved document are stored so.
D. The scientific-writing skill asks the author for the same.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, get_args

import pytest

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.models import (
    ADVISORY_CATEGORIES,
    Category,
    FindingInput,
)
from sandbox_science.orchestrator.consistency.rubric import RUBRIC, RUBRIC_VERSION
from sandbox_science.orchestrator.consistency.runner import SPECS, ReviewerRunner
from sandbox_science.orchestrator.model import ScriptedModel

from .test_consistency import MAIN, anchor, claim, dispatch
from .test_consistency import store as store

CRYPTO: Final = (
    "cost_model",
    "preconditions",
    "unqualified_security",
    "notion",
    "parameters",
    "structure_from_naming",
    "quantum_conflation",
)
SKILL: Final = (
    Path(__file__).resolve().parents[3] / "skills" / "builtin" / "scientific-writing" / "SKILL.md"
)


def flat(text: str) -> str:
    return " ".join(text.split())


# A. The rubric.


def test_the_rubric_names_each_category_once_as_a_check() -> None:
    assert RUBRIC_VERSION == "sandbox-consistency-9"
    rubric = flat(RUBRIC)
    assert "Cryptographic claims." in rubric
    for category in CRYPTO:
        assert rubric.count(f"- {category}:") == 1, category


def test_the_rubric_keeps_the_reviewer_reading_and_states_the_severity_rule() -> None:
    rubric = flat(RUBRIC)
    assert "Do not estimate a cost yourself, and never say you ran anything." in rubric
    assert "warn when it is prose (a final answer, a thread reply, a finding response)" in rubric
    assert "fail when it is in a saved artifact (a document, a ledger entry, an artifact)" in (
        rubric
    )
    # A sub-2^64 cost on a standardized set is questioned, not repeated.
    assert "question it, never repeat it" in rubric
    # Labelled sweeps and unverified parameters are not findings.
    assert "labelled as a sweep or as unverified are no finding" in rubric


def test_the_rubric_keeps_its_do_not_flag_list_and_the_pass_note() -> None:
    rubric = flat(RUBRIC)
    assert (
        "Do not flag a correct claim stated tersely, a cost model named once and referred to "
        "after, or a standard result cited without a source." in rubric
    )
    assert "record it with review_checked, never manufacture an objection to it" in rubric


# B. The categories.


def test_the_finding_model_and_the_reviewer_tool_accept_each_category() -> None:
    assert set(CRYPTO) <= set(get_args(Category))
    finding = next(spec for spec in SPECS if spec.name == "review_finding")
    assert finding.parameters["properties"]["category"]["enum"] == list(get_args(Category))
    for category in CRYPTO:
        FindingInput.model_validate(
            {
                "severity": "warn",
                "category": category,
                "statement": "A bare 2^143 names no cost model.",
                "anchors": [{"evidence": "ev-" + "0" * 24}],
                "operation_id": f"op-{category}",
            }
        )
    # None of them is advisory: they are counted and can hold a review.
    assert not set(CRYPTO) & set(ADVISORY_CATEGORIES)


# C. The severity rule, as a scripted reviewer applies it.


def finding_call(category: str, severity: str, evidence: dict[str, Any], op: str) -> Any:
    return {
        "name": "review_finding",
        "arguments": {
            "severity": severity,
            "category": category,
            "statement": f"A {category} claim.",
            "anchors": [evidence],
            "operation_id": op,
        },
    }


@pytest.mark.asyncio
async def test_a_prose_claim_is_a_warning_and_a_saved_document_claim_a_failure(store) -> None:
    reviews, documents, _ = store
    doc = documents.create(
        "p",
        name="report",
        title="Report",
        text="The scheme is IND-CCA secure at 2^256.\n",
        author=MAIN,
        session_id="s",
    ).document_id
    reviews.ingest_documents("p", "s")
    claim(reviews, "The attack costs 2^143.")
    review = dispatch(reviews)
    prose = anchor(reviews, review)
    saved = {
        **anchor(reviews, review, "document"),
        "block": documents.mapped("p", doc, 1).block_ids[0],
    }
    model = ScriptedModel.from_json(
        [
            {
                "tool_calls": [
                    {"name": "review_read", "arguments": {"evidence": prose["evidence"]}},
                    {"name": "review_read", "arguments": {"evidence": saved["evidence"]}},
                ]
            },
            {
                "tool_calls": [
                    finding_call("cost_model", "warn", prose, "prose-1"),
                    finding_call("notion", "fail", saved, "saved-1"),
                ]
            },
            {
                "text": json.dumps(
                    {"outcome": "findings", "summary": "Two claims.", "cap_reached": False}
                )
            },
        ]
    )
    await ReviewerRunner(reviews, "p", "s", review, "owner", model).run()
    assert reviews.run("p", "s", review)["state"] == "findings"
    found = {
        f["category"]: (f["severity"], f["status"])
        for f in Findings(reviews).listing("p", "s")["findings"]
    }
    assert found == {"cost_model": ("warn", "open"), "notion": ("fail", "open")}
    assert reviews.finding_states("p", "s") == {"open": 2}


# D. The author's side.


def test_the_scientific_writing_skill_mirrors_the_checks() -> None:
    text = flat(SKILL.read_text(encoding="utf-8"))
    for phrase in (
        "name the cost model and the instance and block size it was priced at",
        "Before quoting an attack's cost, show that the scheme has the structure",
        "Do not call a scheme secure, verified or proven without saying",
        "State a notion with its game and oracle access",
        "Take parameter sets from a source read in the session",
        "not from a shared name",
        "Give classical and quantum costs separately",
    ):
        assert phrase in text, phrase
