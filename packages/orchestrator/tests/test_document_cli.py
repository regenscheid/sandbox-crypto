# SPDX-License-Identifier: MIT
"""``sandbox-science-harness document``: the user's commands on living documents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.consistency.store import ReviewStore
from sandbox_science.orchestrator.documents import Author, DocumentStore, store_path
from sandbox_science.orchestrator.mathchecks.jobs import MathJobs
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
    status, out, _ = _run(
        capsys, "document", "import", *where, "--name", "ff", "--base", "1", str(source)
    )
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
    decide = ("document", "decide", *where, "ff", suggested.patch_id or "", "accept")
    # First the text as it would be saved, exactly; then the user accepts it at that base.
    status, out, err = _run(capsys, *decide)
    assert (status, out) == (1, "Better.\n")
    assert "run again with --base 1" in err
    assert store.revision("p1", "ff").text == "Text.\n"
    status, out, err = _run(capsys, *decide, "--base", "2")
    assert status == 1
    assert "is at revision 1, not 2" in err
    status, out, _ = _run(capsys, *decide, "--base", "1")
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


def test_an_import_never_undoes_later_revisions(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    where = ("--state-dir", str(state), "--project", "p1")
    source = tmp_path / "ff.md"
    source.write_text("\ufeff# Fermat\n\nText.\n", encoding="utf-8")
    assert _run(capsys, "document", "import", *where, "--name", "ff", str(source))[0] == 0
    store = DocumentStore(store_path(state))
    # A byte-order mark is not text: the heading stays a heading.
    assert store.revision("p1", "ff").text == "# Fermat\n\nText.\n"
    store.patch(
        "p1",
        "ff",
        base=1,
        author=AGENT,
        reason="r",
        operation_id="o",
        operations=[{"op": "replace", "block": "b2", "markdown": "The agent's text."}],
    )
    # Without the revision the file was made from, an import is refused.
    status, _, err = _run(capsys, "document", "import", *where, "--name", "ff", str(source))
    assert status == 1
    assert "give --base" in err
    status, _, err = _run(
        capsys, "document", "import", *where, "--name", "ff", "--base", "1", str(source)
    )
    assert status == 1
    assert "revision 2 is current, not 1" in err
    # Exported, edited and imported again from the revision it was exported from.
    exported = tmp_path / "out.md"
    status, _, err = _run(capsys, "document", "export", *where, "ff", "--output", str(exported))
    assert (status, err.strip()) == (
        0,
        "revision 2 of document ff: import the file again with --base 2",
    )
    exported.write_text(exported.read_text(encoding="utf-8") + "\nMore.\n", encoding="utf-8")
    status, out, _ = _run(
        capsys, "document", "import", *where, "--name", "ff", "--base", "2", str(exported)
    )
    assert json.loads(out)["revision"] == 3
    assert store.revision("p1", "ff").text == "# Fermat\n\nThe agent's text.\n\nMore.\n"
    status, out, _ = _run(capsys, "document", "export", *where, "ff", "--revision", "1")
    assert out == "# Fermat\n\nText.\n"
    store.close()


def test_an_imported_revision_is_scheduled_for_a_math_check(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "ff.md"
    source.write_text("Let $p = 17$.\n", encoding="utf-8")
    where = ("--state-dir", str(state), "--project", "p1")
    assert _run(capsys, "document", "import", *where, "--name", "ff", str(source))[0] == 0
    store = DocumentStore(store_path(state))
    try:
        jobs = MathJobs(ReviewStore(store))
        check = jobs.claim("p1", "worker")
        assert check is not None
        assert jobs.job(check)["revision"] == 1
    finally:
        store.close()


def test_the_user_records_a_ledger_check(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``ledger check``: the user's record that they checked an entry at a revision."""
    from .ledger_support import AnchoredLedger as Ledger

    documents = DocumentStore(store_path(state))
    claim = {"type": "claim", "origin": "project", "title": "K", "statement": "s"}
    Ledger(documents).change(
        "p1", {"operation_id": "made", "reason": "r", "changes": [claim]}, AGENT
    )
    documents.close()
    where = ("--state-dir", str(state), "--project", "p1")
    status, out, _ = _run(
        capsys, "ledger", "check", *where, "C-1", "--revision", "1", "--note", "Read it."
    )
    assert status == 0
    assert json.loads(out)["entries"][0]["revision"] == 2
    status, _, err = _run(capsys, "ledger", "check", *where, "C-1", "--revision", "1")
    assert (status, "conflict" in err) == (1, True)


def test_the_user_checks_a_reference(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``ledger check`` of a reference, which vouches for the PDF the revision names
    (LG-16 as the coordinator decided)."""
    from .ledger_support import AnchoredLedger as Ledger
    from .test_ledger import REFERENCE_FIELDS

    documents = DocumentStore(store_path(state))
    reference = {"type": "reference", "title": "A paper", "statement": "A paper.",
                 "fields": REFERENCE_FIELDS}  # fmt: skip
    Ledger(documents).change(
        "p1", {"operation_id": "ref", "reason": "r", "changes": [reference]}, AGENT
    )
    documents.close()
    where = ("--state-dir", str(state), "--project", "p1")
    status, out, err = _run(capsys, "ledger", "check", *where, "REF-1", "--revision", "1")
    assert status == 0, err
    assert json.loads(out)["entries"][0]["revision"] == 2


def test_the_user_assesses_a_claimed_obstruction(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ledger assess``: the user accepts an obstruction claimed on an issue (LG-7)."""
    from .ledger_support import AnchoredLedger as Ledger

    documents = DocumentStore(store_path(state))
    ledger = Ledger(documents)
    issue = {"type": "issue", "title": "Below 2^80?", "statement": "Is there one?"}
    ledger.change("p1", {"operation_id": "made", "reason": "r", "changes": [issue]}, AGENT)
    claim = ledger.change(
        "p1",
        {"operation_id": "claim", "reason": "r",
         "work": [{"key": "t", "entry": "I-1", "tried": "Swept.", "ending": "limit",
                   "limit": "time"},
                  {"entry": "I-1", "tried": "No factor base.", "ending": "obstruction_claimed",
                   "references": [{"work": "new:t"}]}]},
        AGENT,
    ).work[1]  # fmt: skip
    documents.close()
    where = ("--state-dir", str(state), "--project", "p1")
    status, out, err = _run(
        capsys, "ledger", "assess", *where, "I-1", "--claim", claim, "--verdict", "accepted",
        "--revision", "1", "--note", "Read the argument.",
    )  # fmt: skip
    assert status == 0, err
    assert len(json.loads(out)["work"]) == 1


def test_the_user_drops_a_dominates_link(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``ledger drop``: the user drops a dominates link an entry holds, with the reason."""
    from sandbox_science.orchestrator.documents import USER

    from .ledger_support import AnchoredLedger as Ledger
    from .test_ledger_frontier import CLASSICAL, KEY_RECOVERY, published

    documents = DocumentStore(store_path(state))
    ledger = Ledger(documents)
    ledger.define_category("p1", KEY_RECOVERY, AGENT)
    from .test_ledger import NEW

    reference = NEW["reference"]
    p4 = published("P4", CLASSICAL, time="$2^{0.29n}$", memory=70)
    p1 = {**published("P1", CLASSICAL, time=120, memory=60),
          "links": [{"kind": "dominates", "entry": "R-1", "note": "Faster."}]}  # fmt: skip
    for op, change in (("ref", reference), ("p4", p4), ("p1", p1)):
        ledger.change("p1", {"operation_id": op, "reason": "r", "changes": [change]}, USER)
    documents.close()
    where = ("--state-dir", str(state), "--project", "p1")
    drop = ("ledger", "drop", *where, "R-2", "--dominates", "R-1", "--revision", "1")
    status, out, _ = _run(capsys, *drop, "--reason", "Not for small n.")
    assert (status, json.loads(out)["entries"][0]["revision"]) == (0, 2)
    status, _, err = _run(capsys, *drop, "--reason", "Again.")
    assert (status, "conflict" in err) == (1, True)


def test_the_user_clears_ledger_flags(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``ledger clear``: the user clears open flags on an entry with the reason."""
    from .ledger_support import AnchoredLedger as Ledger

    documents = DocumentStore(store_path(state))
    ledger = Ledger(documents)
    claim = {"type": "claim", "origin": "project", "title": "K", "statement": "s"}
    cited = [{"role": "supports", "type": "proof", "entry": "C-1"}]
    result = {"type": "result", "origin": "project", "title": "R", "statement": "s"}
    for op, change in (
        ("made", claim),
        ("cites", {**result, "evidence": cited}),
        ("revised", {"entry": "C-1", "expected": 1, "statement": "Other."}),
    ):
        ledger.change("p1", {"operation_id": op, "reason": "r", "changes": [change]}, AGENT)
    (flag,) = ledger.entry("p1", "R-1").flags
    documents.close()
    where = ("--state-dir", str(state), "--project", "p1")
    status, out, _ = _run(
        capsys, "ledger", "clear", *where, "R-1", "--flag", flag.flag_id, "--reason", "Holds."
    )
    assert (status, json.loads(out)["flags_cleared"]) == (0, [flag.flag_id])
    status, _, err = _run(
        capsys, "ledger", "clear", *where, "R-1", "--flag", flag.flag_id, "--reason", "Again."
    )
    assert (status, "conflict" in err) == (1, True)
    status, out, _ = _run(
        capsys, "ledger", "reopen", *where, "R-1", "--flag", flag.flag_id, "--reason", "No."
    )
    assert (status, len(json.loads(out)["flags_reopened"])) == (0, 1)
    status, out, _ = _run(capsys, "ledger", "clear", *where, "R-1", "--all", "--reason", "Yes.")
    assert (status, json.loads(out)["cleared_total"]) == (0, 1)
