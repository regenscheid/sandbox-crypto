# SPDX-License-Identifier: MIT
"""The read register (docs/specs/context-management.md, CM-12, CM-12a): CM-F9, as far as
the register goes, and the connector descriptors against results as the servers wrote
them (crypto-research-mcp, 2026-10-16, trimmed).

Reads go in through the run's message log, as a run archives its tool results, and come
out of the register, the ``session reads`` command and a reference's ``ledger_read``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from sandbox_science.orchestrator import cli
from sandbox_science.orchestrator.context.history import Archive, RunArchive
from sandbox_science.orchestrator.context.history.descriptors import (
    descriptor,
    nist_key,
    paper_key,
    url_key,
)
from sandbox_science.orchestrator.context.history.reads import (
    Read,
    Register,
    merged,
    reads_of,
    reference_reads,
    spans,
)
from sandbox_science.orchestrator.hooks import Appended
from sandbox_science.orchestrator.model import ToolCall
from sandbox_science.orchestrator.notices import CONNECTOR_RESULT
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.orchestrator.tools import Toolbox, ToolOutcome
from sandbox_science.orchestrator.worker_client import WorkerClient

from . import eprint_results as ep
from . import library_results as lib
from .ledger_support import AnchoredLedger as Ledger
from .test_ledger_evidence import publish
from .test_ledger_tools import CONTEXT, tool, use
from .test_ledger_tools import ledger as ledger
from .test_ledger_tools import state as state

PDF = "ab" * 32
EPRINT_RECORD = (
    "[2024/1234] EagleSignV3 : A  new  secure variant of EagleSign signature over lattices\n"
    "Authors: Abiodoun Clement Hounkpevi; Sidoine Djimnaibeye; Michel Seck; Djiby Sow\n"
    "Date: 2024-08-03T14:51:25Z; 2024-08-06T21:03:11Z\nCategory: Public-key cryptography\n"
    "Archive page: https://eprint.iacr.org/2024/1234\n\nWith the potential arrival of ..."
)
FULLTEXT = (
    "[2024/1234]\n" + EPRINT_RECORD + "\n\n1 passage(s) matching 'rejection':\n\n  ...\n"
    "rejection of Lyubashevsky-2012 to achieve the zero-knowledge property."
)
UNMATCHED = "[2024/1234]\n" + EPRINT_RECORD + "\n\nNo passage matching 'pebble'."
# The e-print tools as the server answers since 2026-10-31 (the older answers above stay
# readable: archives of before hold them).
PAGES = (
    "[2024/1234] EagleSignV3\nPhysical pages 2-3 of 10.\n\n## Page 2\n\nText.\n\n## Page 3\n\n"
    'More.\n\nNext: eprint_fulltext(paper_id="2024/1234", start_page=4, page_count=2).'
)
ALL_PAGES = "[2024/1234] EagleSignV3\nPhysical pages 1-2 of 2.\n\n## Page 1\n\nA\n\nEnd of paper."
SEARCHED = (
    "[2024/1234] EagleSignV3\n\n1 passage(s) matching 'rejection':\n\nPhysical page 4\n\n"
    "rejection of Lyubashevsky-2012 to achieve the zero-knowledge property.\n"
)
SEARCH_MISSED = (
    "[2024/1234] EagleSignV3\n\nNo body passage matching 'pebble'. The body text is available."
)
FETCHED = (
    "[direct] https://eprint.iacr.org/2024/1234 (HTTP 200)\nExtraction: trafilatura\n\n"
    "# EagleSignV3\n\n##### Abstract\n\nWith the potential arrival of quantum computers, it is "
    "essential to b\n\n[Truncated. Call fetch_continue(document_id='7eace25bb1ef6dbb', "
    "offset=300) within 10 minutes; no refetch is needed.]"
)
WHOLE_PAGE = "[direct] https://example.org/note (HTTP 200)\nExtraction: trafilatura\n\n# A note"
NIST_READ = json.dumps(
    {"edition_id": "pilot:fips203-final", "designation": "FIPS 203",
     "page": {"location": {"physical_page_index": 0, "printed_page": "A"}, "markdown": "## FIPS"},
     "truncated": False, "converted_pages": 1}
)  # fmt: skip
CSRC = (
    "NIST FIPS 203 — Module-Lattice-Based Key-Encapsulation Mechanism Standard\n"
    "Source PDF: https://nvlpubs.nist.gov/nistpubs/FIPS/NIST.FIPS.203.pdf\n"
    "104821 chars of normative text\n\n1 passage(s) matching 'security category':\n\n  ...\n"
    "  security category A number"
)


def continued(offset: int, content: str, following: int | None) -> str:
    return json.dumps(
        {"content": content, "next_offset": following, "document_id": "7eace25bb1ef6dbb"}
    )


def placed(tool: str, arguments: dict[str, Any], text: str) -> list[Read]:
    return reads_of(tool, json.dumps(arguments), {"connector": "crypto-research-mcp"}, text)


@pytest.mark.parametrize(
    ("tool", "arguments", "text", "expected"),
    [
        ("e-print-mcp_get_eprint", {"paper_id": "2024/1234"}, EPRINT_RECORD,
         [("eprint", "2024/1234", "abstract_only")]),
        ("e-print-mcp_eprint_fulltext", {"paper_id": "2024/1234", "query": "rejection"},
         FULLTEXT, [("eprint", "2024/1234", "partial")]),
        ("e-print-mcp_eprint_fulltext", {"paper_id": "2024/1234", "query": "pebble"},
         UNMATCHED, [("eprint", "2024/1234", "abstract_only")]),
        ("e-print-mcp_eprint_fulltext", {"paper_id": "2025/1"},
         "Queued for the fetcher; try again shortly.", []),
        ("e-print-mcp_eprint_fulltext", {"paper_id": "2024/1234", "start_page": 2}, PAGES,
         [("eprint", "2024/1234", "partial")]),
        ("e-print-mcp_eprint_fulltext", {"paper_id": "2024/1234"}, ALL_PAGES,
         [("eprint", "2024/1234", "whole")]),
        ("e-print-mcp_search_eprint_fulltext", {"paper_id": "2024/1234", "query": "rejection"},
         SEARCHED, [("eprint", "2024/1234", "partial")]),
        ("e-print-mcp_search_eprint_fulltext", {"paper_id": "2024/1234", "query": "pebble"},
         SEARCH_MISSED, []),
        ("e-print-mcp_eprint_file", {"paper_id": "2024/1234"}, "https://signed.example/x", []),
        # Since 2026-11-25 the server's own lines may lead the header or follow its first line.
        (ep.READ, {"paper_id": ep.PAPER}, ep.OBSERVED, [("eprint", ep.PAPER, "partial")]),
        (ep.READ, {"paper_id": ep.PAPER},
         ep.read(["A"], total=1, note=ep.MIRROR, conversion=ep.RENDERED),
         [("eprint", ep.PAPER, "whole")]),
        (ep.READ, {"paper_id": ep.PAPER}, ep.read(["A"], note=None, conversion=ep.POSTSCRIPT),
         [("eprint", ep.PAPER, "partial")]),
        (ep.READ, {"paper_id": ep.PAPER, "query": "Table 1"},
         ep.compatible("Table 1", [(2, ep.PAGE_TWO)]), [("eprint", ep.PAPER, "partial")]),
        (ep.SEARCH, {"paper_id": ep.PAPER, "query": "Table 1"},
         ep.search("Table 1", [(2, ep.PAGE_TWO)], note=ep.BUDGET, conversion=ep.RENDERED),
         [("eprint", ep.PAPER, "partial")]),
        (ep.SEARCH, {"paper_id": ep.PAPER, "query": "pebble"}, ep.search("pebble", []), []),
        (ep.RECORD, {"paper_id": ep.PAPER}, ep.record(), [("eprint", ep.PAPER, "abstract_only")]),
        ("web-fetch-mcp_fetch_page", {"url": "https://eprint.iacr.org/2024/1234"}, FETCHED,
         [("web", "eprint.iacr.org/2024/1234", "partial")]),
        ("web-fetch-mcp_web_fetch", {"url": "https://example.org/note/"}, WHOLE_PAGE,
         [("web", "example.org/note", "whole")]),
        ("web-fetch-mcp_web_fetch", {"url": "https://example.org/gone"},
         "[direct] https://example.org/gone (HTTP 404)\nNot found", []),
        ("firecrawl-mcp_firecrawl_scrape", {"url": "https://Example.org/a#top"},
         '{"markdown": "# A"}', [("web", "example.org/a", "whole")]),
        ("firecrawl-mcp_firecrawl_scrape",
         {"url": "https://example.org/a.pdf", "pdfOptions": {"maxPages": 3}},
         '{"markdown": "# A"}', [("web", "example.org/a.pdf", "partial")]),
        ("firecrawl-mcp_firecrawl_research_read_paper",
         {"paperId": "arXiv:1706.03762", "question": "q"}, "Passage 1: ...",
         [("paper", "arxiv:1706.03762", "partial")]),
        ("nist-csrc-mcp_nist_read", {"edition_id": "pilot:fips203-final"}, NIST_READ,
         [("nist", "FIPS203", "partial")]),
        ("nist-mcp_csrc_fulltext", {"ref": "FIPS 203", "query": "security category"}, CSRC,
         [("nist", "FIPS203", "partial")]),
        ("nist-csrc-mcp_nist_get_pdf", {"edition_id": "pilot:fips203-final"}, "{}", []),
        ("e-print-mcp_search_eprint", {"query": "lattice"}, "10 results", []),
        ("e-print-mcp_eprint_originals", {"paper_id": "2024/1234"}, '{"originals": []}', []),
        ("paper-search-mcp_paper_copies", {"id": "10.1/x"}, "2 copies", []),
        ("paper-search-mcp_papers_by_ids", {"ids": ["10.1/x"]}, "1 paper", []),
        ("paper-search-mcp_zbmath_search", {"query": "isogeny"}, "3 results", []),
        ("web-fetch-mcp_web_fetch_hosts", {}, "eprint.iacr.org: ok", []),
        # The library server (library-mcp, 2026-11-24), under its work labels.
        ("library-mcp_read_paper", {"identifier": "2024/1234", "start_page": 2},
         lib.read("eprint/2024/1234", 2, ["A", "B"], total=10),
         [("eprint", "2024/1234", "partial")]),
        ("library-mcp_read_paper", {"identifier": "10.1007/X"},
         lib.read("doi:10.1007/x", 1, ["A"], total=1), [("paper", "doi:10.1007/x", "whole")]),
        ("library-mcp_read_paper", {"identifier": "nist:ir-8610"},
         lib.read("nist:ir-8610", 1, ["A"]), [("nist", "IR8610", "partial")]),
        ("library-mcp_read_paper", {"identifier": "W12"},
         lib.read("openalex:W12", 1, ["A"]), [("paper", "openalex:w12", "partial")]),
        ("library-mcp_read_paper", {"identifier": "2024/1234"}, lib.not_held("eprint/2024/1234"),
         []),
        ("library-mcp_read_paper", {"identifier": "2024/1234", "page_count": 9},
         "page_count must be between 1 and 5; complete pages are returned without clipping.", []),
        ("library-mcp_search_in_paper", {"identifier": "2024/1234", "phrase": "twist"},
         lib.search("eprint/2024/1234", "twist", [(3, "a twist")]),
         [("eprint", "2024/1234", "partial")]),
        ("library-mcp_search_in_paper", {"identifier": "2024/1234", "phrase": "pebble"},
         lib.search("eprint/2024/1234", "pebble", []), []),
        ("library-mcp_paper_statements", {"identifier": "2024/1234"},
         lib.header("eprint/2024/1234") + "2 numbered statement(s):\n",
         [("eprint", "2024/1234", "partial")]),
        ("library-mcp_paper_card", {"identifier": "2024/1234"},
         lib.header("eprint/2024/1234") + "kind: scheme\n- It signs.",
         [("eprint", "2024/1234", "abstract_only")]),
        ("library-mcp_paper_notes", {"identifier": "2024/1234"},
         lib.header("eprint/2024/1234") + "1 note(s) by people reading it, by page:\n", []),
        *[(f"library-mcp_{name}", {"identifier": "2024/1234"}, "Anything.", [])
          for name in ("find_papers", "paper_status", "fetch_paper", "paper_file",
                       "search_library", "related_papers", "paper_positions", "imports_of",
                       "imports_near")],
    ],
)  # fmt: skip
def test_each_descriptor_places_its_tools_reads(
    tool: str, arguments: dict[str, Any], text: str, expected: list[tuple[str, str, str]]
) -> None:
    """CM-12a: each reading tool's result, as its server writes it, says which source it
    read and how much; a link to a file, a search or a failed fetch reads none. The
    result as the model got it, after the platform's framing line, reads the same."""
    found = placed(tool, arguments, text)
    assert [(r.kind, r.source, r.extent) for r in found] == expected
    assert placed(tool, arguments, CONNECTOR_RESULT.text + text) == found


def test_a_tool_without_a_descriptor_is_unknown_and_a_client_name_finds_its_own() -> None:
    (read,) = placed("paper-search-mcp_paper_details", {"id": "x"}, "Some paper.")
    assert (read.kind, read.extent, read.label) == (
        "connector",
        "unknown",
        "paper-search-mcp_paper_details",
    )
    assert descriptor("crypto_research_mcp-e-print-mcp_get_eprint") is descriptor(
        "e-print-mcp_get_eprint"
    )
    # A failed call read nothing, whatever its tool.
    assert reads_of("e-print-mcp_get_eprint", "{}", {"connector": "c", "error": "x"}, "") == []
    assert nist_key("NIST.FIPS.203") == nist_key("fips 203") == "FIPS203"
    assert url_key("HTTPS://www.Example.ORG/Path/#x") == url_key("http://example.org/Path")
    assert url_key("http://example.org/Path") == "example.org/Path"
    assert nist_key("SP 800-56A Rev. 3") == nist_key("NIST.SP.800-56Ar3") == "SP80056AR3"
    assert paper_key("arXiv:1706.03762v5") == paper_key("1706.03762") == "arxiv:1706.03762"
    assert paper_key("doi:10.1/X") == paper_key("10.1/x") == "doi:10.1/x"


def extent(reads: list[Read]) -> str:
    found = merged(reads)
    assert found is not None
    return found.extent


def test_reads_put_together_cover_their_source() -> None:
    """Pages of a page count, byte ranges of a size and a page's characters to its end
    each add up to ``whole``; until then the source is ``partial``, with what was read."""
    first = Read("pdf", PDF, "partial", {"pages": [1, 2, 3], "of": 12})
    rest = Read("pdf", PDF, "partial", {"pages": list(range(4, 13)), "of": 12})
    assert merged([first]) == Read("pdf", PDF, "partial", {"pages": [1, 2, 3], "of": 12})
    assert extent([first, rest]) == "whole"
    halves = [Read("file", "f", "partial", {"ranges": [[s, s + 50]], "size": 100}) for s in (50, 0)]
    assert extent(halves) == "whole"
    begun = Read("web", "u", "partial", {"chars": [[0, 300]], "document": "d"})
    more = Read("web", "u", "partial", {"chars": [[300, 600]], "end": False})
    ended = Read("web", "u", "partial", {"chars": [[600, 900]], "end": True})
    assert extent([begun, more]) == "partial"
    assert extent([begun, ended]) == "partial"  # a gap: 300 to 600 was never read
    assert extent([begun, more, ended]) == "whole"
    abstract = Read("eprint", "e", "abstract_only")
    passages = Read("eprint", "e", "partial", {"passages": 2})
    assert extent([abstract, passages]) == "partial"
    # An ePrint read by page (2026-10-31): its pages add up to the paper.
    head = placed("e-print-mcp_eprint_fulltext", {}, PAGES.replace("2-3", "1-5"))
    tail = placed("e-print-mcp_eprint_fulltext", {}, PAGES.replace("2-3", "6-10"))
    assert extent([abstract, *head, passages]) == "partial"
    assert extent([abstract, *head, *tail, passages]) == "whole"
    sample = Read("file", "f", "preview_only", {"size": 100})
    assert extent([sample]) == "preview_only"
    assert spans([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10"


def pdf_pages(pages: Sequence[int], count: int = 12, sha256: str = PDF) -> dict[str, Any]:
    """A ``pdf_pages`` record, as the helper returns it (with its page count)."""
    return {
        "pdf": {
            "source": {"sha256": sha256, "path": "papers/eagle.pdf"},
            "page_count": count,
            "pages": [{"index": p, "label": str(p), "lines": []} for p in pages],
        }
    }


def test_cm_f9_the_register_observes_what_a_session_read(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CM-F9: pages 1 to 3 of a 12-page PDF, then all of it; a sample's saved file never
    read; an ePrint entry; a page fetched with one continuation short of its end; and a
    connector tool without a descriptor. The register says ``partial`` then ``whole``,
    ``preview_only``, ``abstract_only``, ``partial`` and ``unknown``, and counts the
    unplaced read."""
    projects = ProjectStore(state)
    log = RunArchive(Archive(projects.control), CONTEXT, run="run-1", parent=None)
    register = Register(projects.control)
    calls = 0

    def result(name: str, arguments: dict[str, Any], text: str, record: dict[str, Any],
               saved: dict[str, str] | None = None) -> None:  # fmt: skip
        nonlocal calls
        calls += 1
        call = ToolCall(f"call-{calls}", name, json.dumps(arguments))
        outcome = ToolOutcome(text, record, saved=saved or {})
        message = {"role": "tool", "tool_call_id": call.id, "content": text}
        asyncio.run(log.append(message, Appended("tool_result", call=call, outcome=outcome)))

    def extents() -> dict[str, tuple[str, Any]]:
        return {
            r.kind: (r.extent, r.detail.get("pages")) for r in register.sources("p", session="s")
        }

    try:
        result(
            "pdf_pages", {"path": "papers/eagle.pdf", "pages": "1-3"}, "...", pdf_pages([1, 2, 3])
        )
        assert extents()["pdf"] == ("partial", [1, 2, 3])
        result("pdf_pages", {"path": "papers/eagle.pdf", "pages": "1-12"}, "...",
               pdf_pages(range(1, 13)))  # fmt: skip
        sample = "x" * 5000
        result("shell", {"script": "make"}, "(a sample)", {"saved_output": {"stdout": "o.txt"}},
               {".sandbox/outputs/o.txt": sample})  # fmt: skip
        connector = {"connector": "crypto-research-mcp"}
        result("e-print-mcp_get_eprint", {"paper_id": "2024/1234"}, EPRINT_RECORD, connector)
        result("web-fetch-mcp_fetch_page", {"url": "https://eprint.iacr.org/2024/1234"},
               FETCHED, connector)  # fmt: skip
        result("web-fetch-mcp_fetch_continue", {"document_id": "7eace25bb1ef6dbb", "offset": 300},
               continued(300, "y" * 300, 600), connector)  # fmt: skip
        result("paper-search-mcp_paper_details", {"id": "x"}, "Some paper.", connector)
        found = {(r.kind, r.label): r for r in register.sources("p", session="s")}
        assert found[("pdf", "papers/eagle.pdf")].extent == "whole"
        digest = hashlib.sha256(sample.encode()).hexdigest()
        preview = found[("file", ".sandbox/outputs/o.txt")]
        assert (preview.source, preview.extent) == (digest, "preview_only")
        assert found[("eprint", "2024/1234")].extent == "abstract_only"
        page = found[("web", "https://eprint.iacr.org/2024/1234")]
        assert (page.extent, page.detail["chars"]) == ("partial", [[0, 600]])
        assert found[("connector", "paper-search-mcp_paper_details")].extent == "unknown"
        assert register.unplaced("p", "s") == {"reads": 7, "unknown": 1, "share": 1 / 7}
        # The sample's file read whole afterwards is whole.
        whole = {"read": {"path": ".sandbox/outputs/o.txt", "sha256": digest, "size": 5000,
                          "offset": 0, "shown": 5000, "complete": True}}  # fmt: skip
        result("read_file", {"path": ".sandbox/outputs/o.txt"}, "...", whole)
        assert {r.source: r.extent for r in register.sources("p", session="s")}[digest] == "whole"
    finally:
        projects.close()
    # The command line shows the session's register.
    argv = ["session", "reads", "--state-dir", str(state), "--project", "p", "--session", "s"]
    assert cli.main(argv) == 0
    shown = json.loads(capsys.readouterr().out)
    assert {s["source"]: s["extent"] for s in shown["sources"]}["papers/eagle.pdf"] == "whole"
    assert shown["unplaced"]["unknown"] == 1


@pytest.mark.asyncio
async def test_a_reference_shows_how_far_its_sources_were_read(state: Path, ledger: Ledger) -> None:
    """LG-15 as CM-12 amends it: ``ledger_read`` of a reference shows the register's
    extents for its PDF (by its artifact revision's SHA-256) and its identifiers, read
    in any session of the project; another PDF read does not count."""
    artifact, digest = publish(ledger, state, b"%PDF-1.4\n")
    projects = ProjectStore(state)
    log = RunArchive(Archive(projects.control), CONTEXT, run="run-1", parent=None)
    try:
        for n, (name, arguments, text, record) in enumerate([
            ("pdf_pages", {"pages": "1-3"}, "...", pdf_pages([1, 2, 3], sha256=digest)),
            ("e-print-mcp_eprint_fulltext", {"paper_id": "2024/1234", "query": "rejection"},
             FULLTEXT, {"connector": "crypto-research-mcp"}),
            ("pdf_pages", {"pages": "1"}, "...", pdf_pages([1], 1, "cd" * 32)),
        ]):  # fmt: skip
            call = ToolCall(f"call-{n}", name, json.dumps(arguments))
            message = {"role": "tool", "tool_call_id": call.id, "content": text}
            outcome = ToolOutcome(text, record)
            await log.append(message, Appended("tool_result", call=call, outcome=outcome))
    finally:
        projects.close()
    fields = {"identifiers": {"eprint": "2024/1234", "url": "https://example.org/unread"},
              "authors": ["A. Author"], "published": "2024",
              "pdf": {"artifact_id": artifact, "revision": 1}}  # fmt: skip
    reference = {"type": "reference", "title": "EagleSignV3", "statement": "s", "fields": fields}
    await use(tool(ledger), "ledger_change", reason="r", changes=[reference])
    shown = await use(tool(ledger), "ledger_read", entry="REF-1")
    assert shown["read"] == [
        {"kind": "pdf", "source": "papers/eagle.pdf", "extent": "partial",
         "covered": {"pages": "1-3", "of": 12}},
        {"kind": "eprint", "source": "2024/1234", "extent": "partial",
         "versions": "any: the reads of all its versions count together"},
    ]  # fmt: skip
    # Another entry type shows none.
    claim = {"type": "issue", "title": "Q", "statement": "Open?"}
    await use(tool(ledger), "ledger_change", reason="r", changes=[claim])
    assert "read" not in await use(tool(ledger), "ledger_read", entry="ISS-1")


PAGE = "# A long page\n" + "lattice reduction " * 2300  # about 41,000 characters


@pytest.mark.parametrize(
    ("tool", "arguments", "text"),
    [
        ("firecrawl-mcp_firecrawl_scrape", {"url": "https://example.org/long"},
         json.dumps({"markdown": PAGE})),
        ("web-fetch-mcp_fetch_page", {"url": "https://example.org/long"},
         "[direct] https://example.org/long (HTTP 200)\nExtraction: trafilatura\n\n" + PAGE),
    ],
)  # fmt: skip
def test_a_result_the_toolbox_cut_is_never_whole(
    tool: str, arguments: dict[str, Any], text: str
) -> None:
    """A page the server returned whole, but longer than the toolbox shows: the model saw
    its head and tail (``tools.bound``), so the read is ``partial``, with what was shown,
    and adds nothing toward a whole read."""
    toolbox = Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT)
    record: dict[str, Any] = {"connector": "crypto-research-mcp"}
    shown = toolbox._cut(text, record)  # the toolbox's own bound, as a call's result gets it
    assert len(text) > 40000 > len(shown)
    assert record["cut"]["length"] == len(text)
    (read,) = reads_of(tool, json.dumps(arguments), record, shown)
    assert (read.source, read.extent) == ("example.org/long", "partial")
    assert read.detail["cut"] == record["cut"]
    assert "chars" not in read.detail
    whole = placed(tool, arguments, text)  # the same page, shown whole
    assert extent([read, *whole]) == "whole"
    assert extent([read]) == "partial"


def test_a_page_that_quotes_the_truncation_notice_is_not_cut() -> None:
    quoted = WHOLE_PAGE + (
        "\n\nThe server says [Truncated. Call fetch_continue(document_id='x', offset=10) "
        "within 10 minutes; no refetch is needed.] when it cuts a page.\n\nMore text."
    )
    (read,) = placed("web-fetch-mcp_web_fetch", {"url": "https://example.org/note"}, quoted)
    assert read.extent == "whole"


def test_a_samples_file_is_its_recorded_revision_and_a_redirect_counts_for_both_urls(
    state: Path,
) -> None:
    """A sample's saved file goes by the SHA-256 its call recorded, so a file that is not
    UTF-8 can still be read whole; a fetch redirected elsewhere counts for the URL asked
    for and the one it reached."""
    recorded = {"recorded_files": {"files": [
        {"path": ".sandbox/outputs/o.bin", "sha256": "ef" * 32, "size": 9}]}}  # fmt: skip
    (sample,) = reads_of("shell", "{}", recorded, "", {".sandbox/outputs/o.bin": "\ufffd" * 3})
    assert (sample.source, sample.extent, sample.detail) == ("ef" * 32, "preview_only", {"size": 9})
    moved = "[direct] https://www.example.org/new (HTTP 200)\nExtraction: x\n\n# New"
    (read,) = placed("web-fetch-mcp_web_fetch", {"url": "http://example.org/old"}, moved)
    assert (read.source, read.detail) == ("example.org/old", {"final": "example.org/new"})
    projects = ProjectStore(state)
    log = RunArchive(Archive(projects.control), CONTEXT, run="run-1", parent=None)
    try:
        asked = json.dumps({"url": "http://example.org/old"})
        call = ToolCall("c1", "web-fetch-mcp_web_fetch", asked)
        outcome = ToolOutcome(moved, {"connector": "crypto-research-mcp"})
        message = {"role": "tool", "tool_call_id": "c1", "content": moved}
        asyncio.run(log.append(message, Appended("tool_result", call=call, outcome=outcome)))
        fields = {"identifiers": {"url": "https://example.org/new/"}}
        (found,) = reference_reads(projects.control, "p", fields)
    finally:
        projects.close()
    assert (found["source"], found["extent"]) == ("http://example.org/old", "whole")


def test_a_paper_read_through_either_server_is_one_source() -> None:
    """CM-12a: pages of ePrint 2024/1234 read through the e-print server and through
    the library (``eprint/2024/1234``) are one paper's, and together they read it whole;
    a reference's ePrint identifier names it as either server does."""
    eprint = placed("e-print-mcp_eprint_fulltext", {"paper_id": "2024/1234", "start_page": 2},
                    PAGES)  # fmt: skip
    library = placed("library-mcp_read_paper", {"identifier": "2024/1234"},
                     lib.read("eprint/2024/1234", 1, ["A"], total=10)) + placed(
        "library-mcp_read_paper", {"identifier": "eprint/2024/1234", "start_page": 4},
        lib.read("eprint/2024/1234", 4, ["D", "E", "F", "G", "H"], total=10),
    ) + placed("library-mcp_read_paper", {"identifier": "2024/1234", "start_page": 9},
               lib.read("eprint/2024/1234", 9, ["I", "J"], total=10))  # fmt: skip
    assert {(r.kind, r.source) for r in eprint + library} == {("eprint", "2024/1234")}
    assert extent(library) == "partial"
    assert extent([*eprint, *library]) == "whole"


def test_a_reading_result_cut_between_pages_reads_the_pages_shown() -> None:
    """CM-12a: a reading tool's result over its limit loses whole pages from its end
    (``descriptors.paged``); the read covers the pages shown and is ``partial``, and
    another read of the pages left out completes it. Any other connector result keeps
    the general limit."""
    toolbox = Toolbox(cast(WorkerClient, SimpleNamespace()), CONTEXT, output_chars=6000)
    texts = ["x" * 3000 for _ in range(5)]
    text = CONNECTOR_RESULT.text + lib.read("eprint/2024/1234", 1, texts, total=5)
    record: dict[str, Any] = {"connector": "crypto-research-mcp"}
    shown = toolbox._cut(text, record, "library-mcp_read_paper")  # twice output_chars
    assert 3000 < len(shown) <= 12000 < len(text)
    assert "cut" not in record
    assert record["paged"]["pages"] == [4, 5]
    assert shown.endswith("read them with a call that starts at page 4.]")
    (read,) = reads_of("library-mcp_read_paper", json.dumps({"identifier": "2024/1234"}),
                       record, shown)  # fmt: skip
    assert (read.source, read.extent, read.detail["pages"]) == ("2024/1234", "partial", [1, 2, 3])
    rest = placed("library-mcp_read_paper", {"identifier": "2024/1234", "start_page": 4},
                  lib.read("eprint/2024/1234", 4, ["D", "E"], total=5))  # fmt: skip
    assert extent([read, *rest]) == "whole"
    other: dict[str, Any] = {"connector": "crypto-research-mcp"}
    assert len(toolbox._cut(text, other, "web-fetch-mcp_web_fetch")) <= 3000 + 64
    assert "cut" in other


def test_a_paper_the_library_files_under_another_identifier_is_one_source(state: Path) -> None:
    """CM-12a: a library read asked for by ePrint number that answers under the paper's
    published DOI names both (``aliases``): a reference with either identifier sees the
    e-print server's pages and the library's together, as one source, and together they
    complete it; a read asked for by a looser name links nothing."""
    published = "doi:10.1109/eurosp.2018.00032"
    projects = ProjectStore(state)
    log = RunArchive(Archive(projects.control), CONTEXT, run="run-1", parent=None)
    register = Register(projects.control)
    calls = 0

    def result(name: str, arguments: dict[str, Any], text: str) -> None:
        nonlocal calls
        calls += 1
        call = ToolCall(f"call-{calls}", name, json.dumps(arguments))
        outcome = ToolOutcome(text, {"connector": "crypto-research-mcp"})
        message = {"role": "tool", "tool_call_id": call.id, "content": text}
        asyncio.run(log.append(message, Appended("tool_result", call=call, outcome=outcome)))

    def read(fields: dict[str, Any]) -> list[dict[str, Any]]:
        return reference_reads(projects.control, "p", {"identifiers": fields})

    try:
        result(
            "e-print-mcp_eprint_fulltext",
            {"paper_id": ep.PAPER, "start_page": 1, "page_count": 2},
            ep.OBSERVED,
        )
        result("library-mcp_read_paper", {"identifier": "ePrint 2017/634", "start_page": 3},
               lib.read(published, 3, ["C"], total=16))  # fmt: skip
        assert [r["covered"]["pages"] for r in read({"eprint": ep.PAPER})] == ["1-2"]
        assert register.completed("p", "eprint", ep.PAPER) is None
        result("library-mcp_read_paper", {"identifier": ep.PAPER, "start_page": 4},
               lib.read(published, 4, [f"P{n}" for n in range(4, 17)], total=16))  # fmt: skip
        (one,) = read({"eprint": ep.PAPER})
        assert one["kind"] == "eprint"  # its source shown is the label last read
        assert (one["extent"], one["covered"]["pages"]) == ("whole", "1-16")
        (other,) = read({"doi": "10.1109/EuroSP.2018.00032"})
        assert (other["kind"], other["extent"]) == ("paper", "whole")
        assert register.completed("p", "eprint", ep.PAPER) is not None
        assert register.completed("p", "paper", published) is not None
        assert read({"eprint": "2024/999"}) == []
        # In the session's own list it is one source too.
        assert [r.extent for r in register.sources("p", session="s")] == ["whole"]
    finally:
        projects.close()


def test_a_library_read_names_the_exact_identifier_it_was_asked_for_by() -> None:
    """The call's identifier is the read's alias when it is exactly a ledger identifier
    of another key than the work label's, in each library tool that reads; a title, a
    lenient form or the label's own identifier adds none."""
    text = lib.read("doi:10.1109/eurosp.2018.00032", 1, ["A"], total=16)
    (read,) = placed("library-mcp_read_paper", {"identifier": "2017/634"}, text)
    assert (read.kind, read.source) == ("paper", "doi:10.1109/eurosp.2018.00032")
    assert read.detail["aliases"] == [["eprint", "2017/634"]]
    found = lib.search("doi:10.1109/eurosp.2018.00032", "twist", [(3, "a twist")])
    (searched,) = placed("library-mcp_search_in_paper", {"identifier": "eprint/2017/634"}, found)
    assert searched.detail["aliases"] == [["eprint", "2017/634"]]
    card = lib.header("eprint/2017/634") + "kind: scheme\n- It encapsulates."
    (carded,) = placed("library-mcp_paper_card", {"identifier": "10.1109/EuroSP.2018.00032"}, card)
    assert carded.detail == {"aliases": [["paper", "doi:10.1109/eurosp.2018.00032"]]}
    for loose in ("ePrint 2017/634", "Kyber", "doi:10.1109/eurosp.2018.00032"):
        (plain,) = placed("library-mcp_read_paper", {"identifier": loose}, text)
        assert "aliases" not in plain.detail, loose
