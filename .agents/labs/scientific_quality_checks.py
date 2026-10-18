# SPDX-License-Identifier: MIT
# ruff: noqa: S101
"""Independent numerical grading and review evidence for the quality lab.

Only ``REFERENCE`` is staged as worker code. Never stage this operator module,
the oracle, or the private expected case assignment to a research agent.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import recovery_acceptance as recovery
import scientific_quality_oracle as oracle

from sandbox_science.orchestrator.consistency.findings import Findings
from sandbox_science.orchestrator.consistency.runner import ReviewerRunner
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import Author, DocumentStore, store_path
from sandbox_science.orchestrator.model import ScriptedModel
from sandbox_science.orchestrator.projects import ProjectStore

SESSION = "study"
CASES = ("calibration-a", "calibration-b")

REFERENCE = """import json
def mul(a,b):
    result=0
    for bit in range(4):
        if b & (1 << bit):
            result ^= a << bit
    for bit in range(6,3,-1):
        if result & (1 << bit):
            result ^= 0x13 << (bit-4)
    return result
table=[0]+[next(y for y in range(1,16) if mul(x,y)==1) for x in range(1,16)]
rows=[[sum(table[x]^table[x^a]==b for x in range(16)) for b in range(16)] for a in range(16)]
print(json.dumps({"degree":4,"modulus":"0x13","counting":"ordered inputs", "inverse_table":table,
    "differential_uniformity":max(max(row) for row in rows[1:]),
    "witness":{"a":1,"b":1,"inputs":[x for x in range(16) if table[x]^table[x^1]==1]},
    "max_differential_probability":str(max(max(row) for row in rows[1:]))+"/16"},sort_keys=True))
"""


def grade_result(result: dict, binary: bytes, field: oracle.Field) -> dict:
    """Exhaustive finite answers only; literature and reasoning require review."""
    expected = oracle.measurements(field)
    checks = {
        key: result.get(key) == expected[key]
        for key in (
            "degree",
            "modulus_hex",
            "inverse_table",
            "differential_uniformity",
            "nonlinearity",
        )
    }
    checks["probability"] = Fraction(result["max_differential_probability"]) == Fraction(
        expected["differential_uniformity"], field.order
    )
    checks["ddt_file"] = binary == oracle.ddt_bytes(oracle.ddt(field.inverse_table()))
    checks["ddt_sha256"] = result.get("ddt_u16le_sha256") == hashlib.sha256(binary).hexdigest()
    witness = result["witness"]
    a, b = witness["input_difference"], witness["output_difference"]
    table = field.inverse_table()
    checks["witness"] = (
        type(a) is int
        and type(b) is int
        and 0 < a < field.order
        and 0 <= b < field.order
        and sorted(witness["solutions"])
        == [x for x in range(field.order) if table[x] ^ table[x ^ a] == b]
        and len(witness["solutions"]) == expected["differential_uniformity"]
    )
    return {"passed": all(checks.values()), "checks": checks}


def seed_case(documents: DocumentStore, session: str, wrong: bool, evidence: dict) -> str:
    """Seed neutral synthetic authorship; model findings are never planted live."""
    author = Author(
        kind="agent",
        session_id=session,
        run_id="fixture-author-" + session,
        role="main",
        model="synthetic-author",
    )
    text = (
        "# Inverse S-box differential profile\n\n"
        "The map is S(0)=0 and S(x)=x^-1 over GF(16), modulus x^4+x+1, "
        "with integer coefficient-bit encoding. The DDT counts ordered inputs "
        "and its maximum excludes input difference zero.\n\n"
        + (
            "Its differential uniformity is 2 and its largest differential probability is 1/8.\n"
            if wrong
            else "Its differential uniformity is 4 and its largest differential probability is 1/4.\n"
        )
    )
    doc = documents.create(
        recovery.PROJECT,
        name="differential-profile",
        title="Inverse S-box differential profile",
        text=text,
        author=author,
        session_id=session,
    )
    reviews = ReviewStore(documents)
    reviews.ingest_documents(recovery.PROJECT, session)
    reviews.observe(
        recovery.PROJECT,
        session,
        "reference-calculation-" + session,
        "execution",
        {"execution": evidence["execution_id"], "attempt": evidence["attempt_id"]},
        {"stdout": evidence["stdout"], "stderr": ""},
        effects=True,
    )
    return doc.document_id


def export_records(control: Path, emit) -> None:
    """Export public research records; no launcher files, credentials or native blobs."""

    def read(sql, arguments=()):
        projects = ProjectStore(control)
        try:
            with projects.control.read() as transaction:
                return transaction.sql.execute(sql, arguments).fetchall()
        finally:
            projects.close()

    documents = DocumentStore(store_path(control), producer="quality-export")
    try:
        revisions = []
        for doc in documents.documents(recovery.PROJECT, deleted=True):
            revisions.append(
                {
                    "document": doc.model_dump(mode="json"),
                    "revisions": [
                        documents.revision(
                            recovery.PROJECT, doc.document_id, version.number
                        ).model_dump(mode="json")
                        for version in documents.revisions(recovery.PROJECT, doc.document_id)
                    ],
                }
            )
        emit("quality-documents", revisions)
        reviews = ReviewStore(documents)
        for session in (SESSION, *CASES):
            pages, after = [], None
            while True:
                page = reviews.listing(recovery.PROJECT, session, after=after)
                pages.append(page)
                after = page.get("next_after")
                if after is None:
                    break
            emit(
                "quality-reviews-" + session,
                {
                    "pages": pages,
                    "findings": Findings(reviews).listing(recovery.PROJECT, session),
                },
            )
    finally:
        documents.close()
    emit(
        "quality-archive",
        read(
            "SELECT session,position,run,parent,message_id,role,kind,content,refs,call_id,tool_calls "
            "FROM archive_messages WHERE project=? ORDER BY session,position",
            (recovery.PROJECT,),
        ),
    )
    emit(
        "quality-folds",
        read(
            "SELECT session,run,collection,position,item FROM run_collections WHERE project=? "
            "AND collection IN ('folds','fold_calls','rebuilds') ORDER BY session,run,collection,position",
            (recovery.PROJECT,),
        ),
    )
    emit(
        "quality-plan",
        read("SELECT * FROM session_plan_records WHERE project=?", (recovery.PROJECT,)),
    )


async def selftest() -> dict:
    """Exercise immutable reviewer evidence and both verdict projections offline."""
    measured = oracle.selftest()
    for field in oracle.FIELDS:
        answer = oracle.measurements(field)
        binary = oracle.ddt_bytes(oracle.ddt(field.inverse_table()))
        assert grade_result(answer, binary, field)["passed"]
        assert not grade_result({**answer, "differential_uniformity": 99}, binary, field)["passed"]
        assert not grade_result(answer, binary[:-1] + b"\xff", field)["passed"]
    calculation = json.loads(recovery.run([sys.executable, "-I", "-c", REFERENCE]).stdout)
    assert calculation["inverse_table"] == measured["cases"][0]["inverse_table"]
    assert calculation["differential_uniformity"] == 4
    assert calculation["witness"]["inputs"] == [0, 1, 6, 7]
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="ss-quality-selftest-") as raw:
        root = Path(raw)
        projects = ProjectStore(root)
        projects.create(recovery.PROJECT, "minimal", "sha256:" + "a" * 64)
        for session in (SESSION, *CASES):
            projects.open_session(recovery.PROJECT, session, dedicated=False)
        documents = DocumentStore(store_path(root), producer="quality-selftest")
        try:
            for session, wrong in zip(CASES, (True, False), strict=True):
                seed_case(
                    documents,
                    session,
                    wrong,
                    {
                        "execution_id": "synthetic-execution-" + session,
                        "attempt_id": "synthetic-attempt-" + session,
                        "stdout": json.dumps(calculation),
                    },
                )
                reviews = ReviewStore(documents)
                review = reviews.dispatch(
                    recovery.PROJECT,
                    session,
                    "terminal",
                    author_run="fixture-author-" + session,
                    owner="quality-selftest",
                    alias="scripted",
                )
                assert review is not None
                items = reviews.manifest(recovery.PROJECT, session, review)["evidence"]
                turns = [
                    {"tool_calls": [{"name": "review_manifest", "arguments": {}}]},
                    {
                        "tool_calls": [
                            {"name": "review_read", "arguments": {"evidence": item["evidence"]}}
                            for item in items
                        ]
                    },
                ]
                if wrong:
                    document = next(item for item in items if item["kind"] == "document")
                    execution = next(item for item in items if item["kind"] == "execution")
                    turns.append(
                        {
                            "tool_calls": [
                                {
                                    "name": "review_finding",
                                    "arguments": {
                                        "severity": "fail",
                                        "category": "contradicting_value",
                                        "statement": "The draft's differential uniformity 2 contradicts the calculation's 4.",
                                        "anchors": [
                                            {"evidence": document["evidence"]},
                                            {"evidence": execution["evidence"]},
                                        ],
                                        "remedy": "Correct the uniformity and probability to match the pinned calculation.",
                                        "operation_id": "offline-contradiction",
                                    },
                                }
                            ]
                        }
                    )
                else:
                    turns.append(
                        {
                            "tool_calls": [
                                {
                                    "name": "review_checked",
                                    "arguments": {
                                        "statement": "The draft uniformity and probability match the pinned calculation.",
                                        "evidence": [item["evidence"] for item in items],
                                    },
                                }
                            ]
                        }
                    )
                turns.append(
                    {
                        "text": json.dumps(
                            {
                                "outcome": "findings" if wrong else "passed",
                                "summary": "Offline fixture plumbing only.",
                                "cap_reached": False,
                            }
                        )
                    }
                )
                await ReviewerRunner(
                    reviews,
                    recovery.PROJECT,
                    session,
                    review,
                    "quality-selftest",
                    ScriptedModel.from_json(turns),
                ).run()
                recorded = reviews.run(recovery.PROJECT, session, review)
                findings = Findings(reviews).listing(recovery.PROJECT, session)["findings"]
                assert recorded["state"] == ("findings" if wrong else "passed"), recorded
                assert len(findings) == int(wrong)
                outcomes.append(
                    {"case": session, "review_state": recorded["state"], "findings": len(findings)}
                )
            captures = {}
            export_records(root, lambda name, value: captures.__setitem__(name, value))
            assert len(captures["quality-documents"]) == 2
            assert len(captures["quality-reviews-calibration-a"]["findings"]["findings"]) == 1
            assert captures["quality-plan"] == []
        finally:
            documents.close()
            projects.close()
    assert not root.exists()
    return {
        "offline_arithmetic": measured["offline_selftest"],
        "numeric_grading": "accepts correct outputs, rejects changed claims and binary bytes",
        "profile_config": "validated without reading a key; runtime max effort remains unverified",
        "review_fixture": outcomes,
        "temporary_state_removed": True,
        "paid_calls": 0,
        "scientific_quality_qualified": False,
    }
