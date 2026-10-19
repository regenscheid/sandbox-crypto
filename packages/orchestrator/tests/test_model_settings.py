# SPDX-License-Identifier: MIT
"""Project-selected agent aliases and efforts from a trusted deployment catalog."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

from sandbox_science.orchestrator.documents import USER, DocumentError, DocumentStore, store_path
from sandbox_science.orchestrator.documents.models import Author
from sandbox_science.orchestrator.model_profiles import ModelProfile
from sandbox_science.orchestrator.model_settings import AgentModelRole, ProjectModels
from sandbox_science.orchestrator.projects import ProjectStore

DEFAULTS: dict[AgentModelRole, ModelProfile] = {
    "main": ModelProfile(alias="sol", api="responses", reasoning_effort="max"),
    "delegated": ModelProfile(alias="sol", api="responses", reasoning_effort="max"),
    "reviewer": ModelProfile(alias="luna", api="responses", reasoning_effort="medium"),
}
CATALOG: dict[str, Literal["chat", "responses", "messages"]] = {
    "sol": "responses",
    "luna": "responses",
    "claude": "messages",
}


@pytest.fixture
def models(tmp_path: Path) -> Iterator[ProjectModels]:
    ProjectStore(tmp_path).create("p1", "minimal", "sha256:" + "a" * 64)
    documents = DocumentStore(store_path(tmp_path))
    yield ProjectModels(documents)
    documents.close()


def test_defaults_and_revisioned_idempotent_user_change(models: ProjectModels) -> None:
    shown = models.get("p1", DEFAULTS, CATALOG)
    assert shown.revision == 0
    assert shown.roles["reviewer"].reasoning_effort == "medium"
    roles = {
        "main": {"alias": "sol", "reasoning_effort": "high"},
        "delegated": {"alias": "luna", "reasoning_effort": "medium"},
        "reviewer": {"alias": "luna", "reasoning_effort": "low"},
    }
    change = {"expected": 0, "roles": roles, "operation_id": "models-1"}
    saved, replayed = models.change("p1", change, USER, defaults=DEFAULTS, available=CATALOG)
    assert (saved.revision, saved.roles["main"].reasoning_effort, replayed) == (1, "high", False)
    assert models.change("p1", change, USER, defaults=DEFAULTS, available=CATALOG) == (saved, True)
    with pytest.raises(DocumentError, match="changed meanwhile"):
        models.change(
            "p1",
            {**change, "operation_id": "models-2"},
            USER,
            defaults=DEFAULTS,
            available=CATALOG,
        )


def test_only_catalog_aliases_and_supported_effort_are_saved(models: ProjectModels) -> None:
    base = {
        "main": {"alias": "sol", "reasoning_effort": "max"},
        "delegated": {"alias": "sol", "reasoning_effort": "max"},
        "reviewer": {"alias": "luna", "reasoning_effort": "medium"},
    }
    agent = Author(kind="agent", run_id="r", session_id="s", role="main")
    with pytest.raises(DocumentError, match="only the user"):
        models.change(
            "p1",
            {"expected": 0, "roles": base, "operation_id": "x"},
            agent,
            defaults=DEFAULTS,
            available=CATALOG,
        )
    outside = {**base, "main": {"alias": "arbitrary", "reasoning_effort": "max"}}
    with pytest.raises(DocumentError, match="outside the deployment catalog"):
        models.change(
            "p1",
            {"expected": 0, "roles": outside, "operation_id": "x"},
            USER,
            defaults=DEFAULTS,
            available=CATALOG,
        )
    messages = {**base, "reviewer": {"alias": "claude", "reasoning_effort": "medium"}}
    with pytest.raises(DocumentError, match="does not accept effort"):
        models.change(
            "p1",
            {"expected": 0, "roles": messages, "operation_id": "x"},
            USER,
            defaults=DEFAULTS,
            available=CATALOG,
        )
