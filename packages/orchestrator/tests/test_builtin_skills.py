# SPDX-License-Identifier: MIT
"""The built-in skill pack, ``skills/builtin/`` (docs/specs/skills.md, SK-40, SK-41;
SK-F24; docs/SKILLS.md).

The pack is read with the pack import's own validator, which must accept every skill.
Each skill's body names tools in inline code, and every inline-code span outside fenced
code must be a tool of the harness's tool table for the roles the skill is written for,
or one of the few other identifiers ``ALLOWED`` lists. A tool renamed in the harness
fails here, naming the skill, the identifier and the nearest tool name.
"""

from __future__ import annotations

import difflib
import json
import posixpath
import tarfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

from sandbox_science.orchestrator.consistency.controller import AUTHOR_SPECS
from sandbox_science.orchestrator.consistency.quotes import EPRINT_TOOLS, LIBRARY_TOOLS, PDF_TOOLS
from sandbox_science.orchestrator.delegation.prompt import processes_text
from sandbox_science.orchestrator.delegation.tracks import SPECS as TRACK_SPECS
from sandbox_science.orchestrator.loop import SYSTEM_PROMPT
from sandbox_science.orchestrator.plan_tools import NAMES as PLAN_NAMES
from sandbox_science.orchestrator.question_tool import SPECS as QUESTION_SPECS
from sandbox_science.orchestrator.skills import from_archive, snapshot
from sandbox_science.orchestrator.skills.builtin import ROLES as BUILTIN_ROLES
from sandbox_science.orchestrator.tools import HARNESS_TOOL_NAMES, SHARED_IN_CODE

PACK: Final = Path(__file__).resolve().parents[3] / "skills" / "builtin"

MAIN: Final = HARNESS_TOOL_NAMES - {spec.name for spec in TRACK_SPECS}
"""A main run's tools with every feature on: all the harness's own but a delegated
task's track tools. Connector tools are each project's, loaded through ``mcp_tools``,
and are named only in fenced code."""
DELEGATED: Final = (
    HARNESS_TOOL_NAMES
    - {spec.name for spec in AUTHOR_SPECS}
    - PLAN_NAMES
    - {spec.name for spec in QUESTION_SPECS}
)
"""A delegated task's tools: its parent's without the review, plan and question tools
(PD-16, PD-34, PD-41), with its connectors' ``mcp_tools`` (DA-3), its track's tools
(PD-9), and the delegate tools where a main run allows it two levels (PD-15)."""
TABLES: Final = {"main": MAIN, "delegated": DELEGATED}

ROLES: Final = BUILTIN_ROLES
"""Each built-in skill, and the roles whose tools it names, which are the roles whose runs
get it (skills/builtin.py). A new skill is added there."""

ALLOWED: Final = {
    "$PROJECT_SHARED": SHARED_IN_CODE,
    "$RUN_WORK": processes_text(False),
    "$TMPDIR": processes_text(False),
    "$SESSION_WORKSPACE": processes_text(False),
    "approval_required": SYSTEM_PROMPT,
    "policy_denied": SYSTEM_PROMPT,
}
"""The identifiers besides tool names that a skill may put in inline code, each with the
harness's own text that names it to the model. Other identifiers go in fenced code."""

MAX_DESCRIPTION: Final = 300
"""A description is read in the index on every call: one or two sentences."""


def _tokens(body: str) -> list[Token]:
    return MarkdownIt("commonmark").enable("table").parse(body)


def _inline(tokens: list[Token]) -> Iterator[Token]:
    for token in tokens:
        if token.type == "inline":
            yield from token.children or ()


def inline_code(body: str) -> list[str]:
    """The inline-code spans of a Markdown ``body``, in order; fenced code holds none."""
    return [token.content for token in _inline(_tokens(body)) if token.type == "code_inline"]


def unknown_tools(skill: str, body: str, roles: Sequence[str]) -> list[str]:
    """Each inline-code span of ``body`` that is neither a tool of ``roles`` nor allowed,
    once, as a line naming the skill, the identifier and the nearest tool name."""
    table = frozenset[str]().union(*(TABLES[role] for role in roles))
    found: list[str] = []
    for code in dict.fromkeys(inline_code(body)):
        if code in table or code in ALLOWED:
            continue
        near = difflib.get_close_matches(code, sorted(table), n=1)
        nearest = f"the nearest tool name is {near[0]}" if near else "no tool name is near it"
        found.append(
            f"skills/builtin/{skill}: `{code}` is no tool of the harness's table for "
            f"{' or '.join(roles)} runs ({nearest}). Name the tool as the harness does, or "
            "put another identifier in fenced code"
        )
    return found


def body(skill: str) -> str:
    """A skill's body: its ``SKILL.md`` after the front matter."""
    text = (PACK / skill / "SKILL.md").read_text(encoding="utf-8")
    opening, _, rest = text.partition("\n---\n")
    assert opening.startswith("---\n"), f"{skill}: SKILL.md starts with front matter"
    return rest


def test_the_pack_imports_whole_with_the_pack_validator(tmp_path: Path) -> None:
    """(a) The import's own reading of the pack, from an archive of its files, accepts
    every skill, leaves no file out, and finds exactly the skills ``ROLES`` lists."""
    archive = tmp_path / "builtin.tar"
    with tarfile.open(archive, "w") as tar:
        for path in sorted(PACK.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(PACK).as_posix())
    found = snapshot(from_archive(archive))
    assert found.excluded == ()
    assert found.fetched.skipped == ()
    assert found.fetched.warnings == ()
    assert sorted(skill.name for skill in found.skills) == sorted(ROLES)
    for skill in found.skills:
        # Standard front matter only (SK-40): nothing beyond the name and description.
        assert skill.metadata == {}, skill.name
        assert len(skill.description) <= MAX_DESCRIPTION, skill.name


@pytest.mark.parametrize("skill", sorted(ROLES))
def test_each_skill_names_only_tools_of_the_harness(skill: str) -> None:
    """(b) Every inline-code identifier is a tool the skill's roles are offered (SK-41)."""
    assert inline_code(body(skill)), f"{skill} names no tool"
    assert unknown_tools(skill, body(skill), ROLES[skill]) == []


def test_a_renamed_tool_fails_naming_the_skill_the_identifier_and_the_nearest_tool() -> None:
    """SK-F24: a copy of a skill that names ``pdf_page`` for ``pdf_pages`` fails."""
    renamed = body("pdf-reading").replace("`pdf_pages`", "`pdf_page`")
    assert renamed != body("pdf-reading")
    (problem,) = unknown_tools("pdf-reading", renamed, ROLES["pdf-reading"])
    assert "skills/builtin/pdf-reading" in problem
    assert "`pdf_page`" in problem
    assert "the nearest tool name is pdf_pages" in problem


def test_a_tool_of_another_role_fails() -> None:
    """A skill for main runs cannot name a tool only a delegated task is offered."""
    (problem,) = unknown_tools("computation", "Use `track_progress`.", ("main",))
    assert "`track_progress`" in problem


def test_the_allowed_identifiers_are_the_harness_own_words() -> None:
    """Each identifier ``ALLOWED`` lets through is one the harness itself names to the
    model, and none is a tool name, which needs no allowing."""
    for name, text in ALLOWED.items():
        assert name in text, name
        assert name not in MAIN | DELEGATED, name


def test_the_skills_name_the_tools_the_citation_check_reads() -> None:
    """The quoting advice holds only while the reviewer's citation check reads these
    tools' results (CR-4): a renamed source tool fails here."""
    pdf = inline_code(body("pdf-reading"))
    assert all(tool in pdf for tool in PDF_TOOLS)
    assert all(tool in body("literature-search") for tool in EPRINT_TOOLS)
    assert all(tool in body("literature-search") for tool in LIBRARY_TOOLS)


@pytest.mark.parametrize("skill", sorted(ROLES))
def test_json_examples_parse_and_links_stay_in_the_pack(skill: str) -> None:
    """Fenced JSON examples are JSON, and a relative link names a file of the pack, which
    is all an import keeps."""
    tokens = _tokens(body(skill))
    for token in tokens:
        if token.type == "fence" and token.info.strip() == "json":
            json.loads(token.content)
    files = {path.relative_to(PACK).as_posix() for path in PACK.rglob("*") if path.is_file()}
    for token in _inline(tokens):
        href = str(token.attrs.get("href", "")) if token.type == "link_open" else ""
        if not href or "://" in href or href.startswith(("#", "mailto:")):
            continue
        target = posixpath.normpath(posixpath.join(skill, href.partition("#")[0]))
        assert target in files, f"{skill}: the link {href} names no file of the pack"


def test_the_computation_skill_leads_with_the_kernels() -> None:
    """Exploration belongs in a persistent kernel: the computation skill names the kernel
    tools before ``shell``, teaches the kernel workflow before the state-loss warning, and
    describes itself kernels first, as the index shows it."""
    names = inline_code(body("computation"))
    assert names.index("python") < names.index("shell")
    assert names.index("sage") < names.index("shell")
    text = body("computation")
    assert text.index("## Explore in a kernel") < text.index("A kernel loses its state")
    assert text.index("1. Set up in one cell") < text.index("A kernel loses its state")
    front = (PACK / "computation" / "SKILL.md").read_text(encoding="utf-8").partition("\n---\n")[0]
    (description,) = [line for line in front.splitlines() if line.startswith("description:")]
    assert description.index("kernels") < description.index("shell")


def test_the_skills_answer_the_second_baseline_runs_sources_and_measurements() -> None:
    """Lane G4: a held paper is read through the library, its PDF downloaded only for
    what a page read cannot show; ``search_in_paper``'s context is a count of lines; a
    measurement carries its code's hash; independent cells run side by side; the
    assumption a source's count rests on is tested; a cell past a limit gets one or two
    other methods before it is called out of reach; a parent works while a task runs.
    The single-page quoting rules stay."""
    literature = " ".join(body("literature-search").split())
    for said in (
        "whose context is the number of lines around each passage, 0 to 20",
        "Read a paper the library holds this way first, rather than downloading its PDF.",
        "only for a paper neither the library nor the e-print server gives you, or to look "
        "at a table, a matrix or an equation as an image after you have read its page",
        "quote from one page or passage, never from a header",
        "The check finds the quote only in a read of that page at that revision.",
    ):
        assert said in literature, said
    assert "when a quote must name its lines" not in literature
    computation = " ".join(body("computation").split())
    for said in (
        "Record the SHA-256 of the code with each measurement, at the moment it runs",
        "keeps the old hash on the results it reuses, or runs them again",
        "Run independent cells (parameter sets, seeds, rows of a table) side by side",
        "within the limit the user stated, or the sandbox's where they stated none",
        "A cell past that limit is not yet out of reach",
        "try one or two other methods that could fit within the limit",
        "rather than estimate the failure from a bound on one construction",
        "report a cell you did not try as not attempted, which is different from attempted "
        "and failed",
        "Never exceed a limit the user set to do so, and after one or two serious "
        "alternatives, report rather than keep trying.",
        "test it on each instance you measure and record the test beside the result",
    ):
        assert said in computation, said
    delegating = " ".join(body("delegating-work").split())
    assert "Start a task as soon as what it needs is in hand" in delegating
    assert "wait for it only when nothing else is left" in delegating


def test_the_skills_answer_the_second_baseline_runs_checks_and_long_computations() -> None:
    """Lane G4b: a long computation checkpoints as it goes and resumes from its files, its
    timeout comes from a pilot, a restartable job is one safe to run again, and a lost job
    is waited for again; a cross-check rebuilds the numbers from the raw outputs and
    reports a disagreement with the source as data; a claim about every instance rests
    on more than one; a parent reproduces a value of an independent check itself."""
    computation = " ".join(body("computation").split())
    for said in (
        "A long computation writes its progress to files as it goes",
        "On start it reads them and skips what is done",
        "keep a checkpoint of its own where the method allows, or choose a method that has one",
        "with a margin of one and a half to two times, since a job that times out is not "
        "started again",
        "only when its command is safe to run again and resumes from its checkpoints",
        "read the job's attempts and wait again rather than start the job anew",
        "A cross-check rebuilds every reported number from the raw outputs",
        "It hard-codes no value it expects and asserts no outcome the source predicts",
        "a genuine disagreement with the source is a result, not a failed check",
        "measure more than one and say how many; where one must do, say so beside the result",
    ):
        assert said in computation, said
    delegating = " ".join(body("delegating-work").split())
    assert (
        "Before accepting a task that checks your work independently, reproduce at least one "
        "of its values yourself from its code or outputs, and say which: reading its files "
        "is not a check."
    ) in delegating
