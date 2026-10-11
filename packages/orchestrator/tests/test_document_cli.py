# SPDX-License-Identifier: MIT
"""``sandbox-science-harness document``: the user's commands on living documents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.documents import Author, DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore

POLICY = "sha256:" + "a" * 64
AGENT = Author(kind="agent", session_id="s1", run_id="run-1", role="main")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    projects = ProjectStore(root)
    projects.create("p1", "minimal", POLICY)
    projects.open_session("p1", "s1", dedicated=False)
    return root


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = cli.main(list(argv))
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_import_list_show_and_history(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "ff.md"
    source.write_text("# Fermat\r\n\r\nLet \\(p = 17\\).\r\n", encoding="utf-8")
    where = ("--state-dir", str(state), "--project", "p1")
    status, out, _ = _run(capsys, "document", "import", *where, "--name", "ff", str(source))
    assert status == 0
    created = json.loads(out)
    assert created["outcome"] == "created"
    store = DocumentStore(store_path(state))
    first = store.revision("p1", "ff")
    # Line breaks as markdown-it reads them, and the delimiters in the dollar form.
    assert first.text == "# Fermat\n\nLet $p = 17$.\n"
    assert first.info.author.kind == "import"
    assert first.info.author.user == "user"
    source.write_text("# Fermat\n\nLet $p = 17$ be prime.\n", encoding="utf-8")
    status, out, _ = _run(capsys, "document", "import", *where, "--name", "ff", str(source))
    assert json.loads(out)["revision"] == 2
    status, out, _ = _run(capsys, "document", "list", *where)
    assert status == 0
    assert " ff  r2  project  ff" in out
    status, out, _ = _run(capsys, "document", "show", *where, "ff")
    blocks = json.loads(out)
    assert [block["kind"] for block in blocks] == ["heading", "paragraph"]
    status, out, _ = _run(capsys, "document", "history", *where, "ff")
    history = json.loads(out)
    assert [entry["changed"] for entry in history["revisions"]] == [[], [blocks[1]["id"]]]
    store.close()


def test_suggestions_decided_from_the_command_line(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    where = ("--state-dir", str(state), "--project", "p1")
    store = DocumentStore(store_path(state))
    store.create("p1", name="ff", title="F", text="Text.\n", author=AGENT)
    status, out, _ = _run(capsys, "document", "suggestions", *where, "ff", "on")
    assert (status, out.strip()) == (0, "document ff takes agent patches as suggestions")
    suggested = store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="r",
        operation_id="o",
        operations=[{"op": "replace", "block": "b1", "markdown": "Better."}],
    )
    status, out, _ = _run(
        capsys, "document", "decide", *where, "ff", suggested.patch_id or "", "accept"
    )
    assert json.loads(out)["outcome"] == "accepted"
    assert store.revision("p1", "ff").text == "Better.\n"
    status, out, _ = _run(capsys, "document", "delete", *where, "ff", "--reason", "done")
    assert json.loads(out)["outcome"] == "deleted"
    status, out, _ = _run(capsys, "document", "list", *where, "--deleted")
    assert "(deleted)" in out
    store.close()


def test_document_commands_report_their_errors(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    where = ("--state-dir", str(state), "--project", "p1")
    status, _, err = _run(capsys, "document", "show", *where, "missing")
    assert status == 1
    assert "not_found" in err
    status, _, err = _run(capsys, "document", "list", "--state-dir", str(state), "--project", "p9")
    assert status == 1
    assert "no project" in err
    source = tmp_path / "x.md"
    source.write_text("x", encoding="utf-8")
    status, _, err = _run(
        capsys, "document", "import", *where, "--session", "s9", "--name", "x", str(source)
    )
    assert status == 1
    assert "no session s9" in err
