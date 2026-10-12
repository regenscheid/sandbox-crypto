# SPDX-License-Identifier: MIT
"""Authenticated USER macro CAS and exact current/historical document contexts."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_science.orchestrator.documents import USER, DocumentStore, store_path

from .test_reviews import client as client

pytestmark = pytest.mark.asyncio


async def test_settings_scope_origin_and_untrusted_author(client):
    base = "/api/v0/projects/p1/macros"
    assert (await client.get("/api/v0/projects/missing/macros")).status == 404
    assert (await (await client.get(base)).json())["revision"] == 0
    value = {"expected": 0, "macros": {r"\F": "x"}, "operation_id": "first"}
    assert (await client.post(base, json=value)).status == 403
    origin = str(client.make_url("/")).rstrip("/")
    headers = {"Origin": origin}
    assert (
        await client.post(base, json={**value, "author": "USER"}, headers=headers)
    ).status == 400
    result = await client.post(base, json=value, headers=headers)
    assert result.status == 200
    assert (await result.json())["macros"] == {r"\F": "x"}
    assert (await (await client.post(base, json=value, headers=headers)).json())["replayed"]
    assert (
        await client.post(base, json={**value, "operation_id": "stale"}, headers=headers)
    ).status == 409
    assert (await (await client.get("/api/v0/projects/p2/macros")).json())["macros"] == {}
    deep = '{"macros":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    assert (
        await client.post(base, data=deep, headers={**headers, "Content-Type": "application/json"})
    ).status == 400
    client.session.cookie_jar.clear()
    assert (await client.get(base)).status == 401


async def test_current_and_explicit_historical_contexts(client, state_dir: Path):
    from sandbox_science.orchestrator.documents.project_macros import ProjectMacros

    docs = DocumentStore(store_path(state_dir))
    try:
        settings = ProjectMacros(docs)
        settings.change(
            "p1",
            {"expected": 0, "macros": {r"\F": "old", r"\G": "project"}, "operation_id": "old"},
            USER,
        )
        doc = docs.create(
            "p1",
            name="macro-doc",
            title="Macros",
            text="---\nmacros:\n  '\\G': 'document'\n---\n\n$\\F+\\G$",
            author=USER,
        ).document_id
        settings.change(
            "p1",
            {"expected": 1, "macros": {r"\F": "new", r"\G": "project"}, "operation_id": "new"},
            USER,
        )
    finally:
        docs.close()
    base = f"/api/v0/projects/p1/documents/{doc}"
    current = await (await client.get(base)).json()
    historical = await (await client.get(base + "?revision=1")).json()
    assert current["macros"] == {r"\F": "new", r"\G": "document"}
    assert historical["macros"] == {r"\F": "old", r"\G": "document"}
    assert historical["macro_context"]["known"]
    assert current["macro_context"]["key"] != historical["macro_context"]["key"]
    assert (await client.get(f"/api/v0/projects/p2/documents/{doc}")).status == 404


async def test_repair_cache_scope_is_explicit_bounded_and_project_checked(client):
    from sandbox_science.orchestrator.model import ModelTurn, ScriptedModel
    from sandbox_science.workspace.mathrepair import MathRepairService, parse_request

    model = ScriptedModel([ModelTurn(text="x"), ModelTurn(text="y"), ModelTurn(text="z")])
    service = MathRepairService(model, reviewer="scripted")
    value = {"tex": "x^", "display": False, "error": "e", "project": "p1", "macro_context": "1"}
    try:
        first = await service.repair(parse_request(value), "s")
        assert (await service.repair(parse_request(value), "other")).cached
        changed = await service.repair(parse_request({**value, "macro_context": "2"}), "s")
        foreign = await service.repair(parse_request({**value, "project": "p2"}), "s")
        assert (first.tex, changed.tex, foreign.tex) == ("x", "y", "z")
    finally:
        await service.close()
    headers = {"Origin": str(client.make_url("/")).rstrip("/")}
    base = "/api/v0/math-repair"
    assert (await client.post(base, json=value, headers=headers)).status == 200
    assert (
        await client.post(base, json={**value, "project": "missing"}, headers=headers)
    ).status == 404
    for changed in ({"macro_context": "x" * 257}, {"project": []}, {"macro_context": False}):
        assert (await client.post(base, json={**value, **changed}, headers=headers)).status == 400
