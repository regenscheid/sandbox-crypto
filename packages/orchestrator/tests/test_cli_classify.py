# SPDX-License-Identifier: MIT
"""``sandbox-science-harness classify-draft`` with a fake model: never a real gateway."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator import cli_classify
from sandbox_science.orchestrator.cli import main
from sandbox_science.orchestrator.model import Message, ModelError, ModelTurn, ToolSpec
from sandbox_science.policy.classification import load_classification
from sandbox_science.policy.store import PolicyStore

ROOT = Path(__file__).resolve().parents[3]
LISTING = ROOT / "packages" / "policy" / "tests" / "fixtures" / "mcp-tools" / "crypto-research-mcp"


class FakeModel:
    """Answers one draft and remembers what it was asked."""

    def __init__(self, answer: str, *, fail: bool = False) -> None:
        self.answer, self.fail = answer, fail
        self.asked: list[Sequence[Message]] = []
        self.closed = False

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelTurn:
        self.asked.append(messages)
        if self.fail:
            raise ModelError("test: the gateway is down")
        return ModelTurn(text=self.answer)

    async def close(self) -> None:
        self.closed = True


def answer() -> str:
    return json.dumps(
        {
            "tools": {
                "web-fetch-mcp_web_fetch": {"class": "read", "reason": "Fetches a page."},
                "web-fetch-mcp_fetch_links": {"class": "read", "reason": "Lists a page's links."},
            }
        }
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    model = FakeModel(answer())

    def gateway(url: str, alias: str, key: str, **options: Any) -> FakeModel:
        assert (url, alias, key) == ("http://127.0.0.1:9", "main", "test-secret-key")
        return model

    monkeypatch.setattr(cli_classify, "gateway_model", gateway)
    return model


def arguments(tmp_path: Path, *extra: str) -> list[str]:
    key = tmp_path / "key"
    key.write_text("test-secret-key\n")
    return [
        "classify-draft",
        "crypto-research-mcp",
        "--tools",
        str(LISTING / "web-fetch-mcp.json"),
        "--prefix",
        "web-fetch-mcp_",
        "--model-url",
        "http://127.0.0.1:9",
        "--api-key-file",
        str(key),
        *extra,
    ]


def test_a_draft_is_written_for_review(tmp_path: Path, fake: FakeModel) -> None:
    out = tmp_path / "draft" / "web-fetch.yaml"
    out.parent.mkdir()
    assert main(arguments(tmp_path, "--out", str(out))) == 0
    assert fake.closed
    (asked,) = fake.asked
    assert "web-fetch-mcp_web_capture" in asked[1]["content"]
    loaded = load_classification(out.parent, "crypto-research-mcp")
    assert loaded.tools["web-fetch-mcp_web_fetch"].effect.value == "read"
    assert loaded.tools["web-fetch-mcp_web_capture"].effect.value == "unclassified"
    assert len(loaded.pins) == 6


def test_a_failed_model_call_writes_nothing(
    tmp_path: Path, fake: FakeModel, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.fail = True
    out = tmp_path / "draft.yaml"
    assert main(arguments(tmp_path, "--out", str(out))) == 1
    assert "the model call failed" in capsys.readouterr().err
    assert not out.exists()


def test_an_unreadable_tool_list_is_refused_before_any_call(
    tmp_path: Path, fake: FakeModel, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = tmp_path / "tools.json"
    listing.write_text("{}")
    command = arguments(tmp_path)
    command[command.index("--tools") + 1] = str(listing)
    assert main(command) == 1
    assert "tool" in capsys.readouterr().err
    assert fake.asked == []


def test_a_draft_is_stored_as_a_projects_for_the_review_view(
    tmp_path: Path, fake: FakeModel, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "middleware"
    command = arguments(tmp_path, "--project", "p1", "--policy-store", str(store))
    assert main(command) == 0
    assert capsys.readouterr().out == ""  # stored, not printed
    [draft] = PolicyStore(store).classification_drafts("p1", "crypto-research-mcp")
    assert draft.model == "main"
    assert draft.data.startswith(b"# SPDX-License-Identifier: MIT\n# DRAFT")
    assert draft.classification.tools["web-fetch-mcp_web_fetch"].effect.value == "read"
    # A draft puts nothing in force.
    assert PolicyStore(store).classification("p1", "crypto-research-mcp") is None
    del fake


def test_a_project_needs_the_policy_store(
    tmp_path: Path, fake: FakeModel, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(arguments(tmp_path, "--project", "p1")) == 1
    assert "go together" in capsys.readouterr().err
    assert fake.asked == []
    bad = arguments(tmp_path, "--project", "../p", "--policy-store", str(tmp_path / "s"))
    assert main(bad) == 1
    assert "not stored" in capsys.readouterr().err
