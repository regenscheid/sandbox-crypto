# SPDX-License-Identifier: MIT
"""The in-sandbox PDF helper against small PDFs whose text and positions are known.

The fixtures are those of docs/specs/pdf-sources.md (PDF-F1 to PDF-F6).
"""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pdf_oxide")

from sandbox_science.testing.pdfs import make_pdf
from sandbox_science.worker import pdfhelper

HELPER = Path(pdfhelper.__file__)


PAGES = [
    [(72, 700, "Preface"), (72, 680, "Lattices and LLL")],
    [(72, 700, "Contents"), (72, 680, "1 A Note on GF(17)")],
    [(72, 700, "A Note on GF(17)"), (72, 680, "a^17 = a for every a in GF(17)")],
    [],
]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    session = tmp_path / "sessions" / "s1"
    shared = tmp_path / "shared"
    session.mkdir(parents=True)
    shared.mkdir()
    monkeypatch.setenv("SESSION_WORKSPACE", str(session))
    monkeypatch.setenv("PROJECT_SHARED", str(shared))
    (session / "paper.pdf").write_bytes(
        make_pdf(
            PAGES,
            labels=[(0, "r"), (2, "D")],
            outline=[("Preface", 0), ("A Note", 2)],
            image_page=3,
        )
    )
    return session


def test_page_facts_labels_and_outline(workspace: Path) -> None:
    # PDF-F1
    result = pdfhelper.info("paper.pdf")
    assert result["page_count"] == 4
    assert [page["label"] for page in result["pages"]] == ["i", "ii", "1", "2"]
    assert [page["index"] for page in result["pages"]] == [1, 2, 3, 4]
    assert result["pages"][0]["width"] == 612.0
    assert result["outline"] == [
        {"title": "Preface", "level": 1, "page_index": 1},
        {"title": "A Note", "level": 1, "page_index": 3},
    ]
    data = (workspace / "paper.pdf").read_bytes()
    assert result["source"] == {
        "path": "paper.pdf",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert result["parser"]["name"] == "pdf_oxide"


def test_lines_carry_their_boxes(workspace: Path) -> None:
    # PDF-F2: the first line of page 3 is drawn at (72, 700) in 12-point Helvetica.
    result = pdfhelper.pages("paper.pdf", "3", artifacts=False)
    [page] = result["pages"]
    assert (page["index"], page["label"]) == (3, "1")
    first = page["lines"][0]
    assert (first["n"], first["text"]) == (1, "A Note on GF(17)")
    x0, y0, x1, y1 = first["bbox"]
    assert x0 <= 72 < x1
    assert y0 <= 700 < y1


HEADED = [(72, 700, "the twist of the curve has"), (72, 680, "order 4r with r prime")]
"""A page with a running header above its two lines and a page number below them."""
HEADER = [(72, 760, "Journal of Tests, vol. 1"), (300, 40, "7")]


def test_headers_and_footers_are_numbered_and_marked_the_same_in_every_operation(
    workspace: Path,
) -> None:
    """One numbering for pdf_pages and pdf_search: every line counts, the header and the
    page number included, so a cited line is the same line in both (CR-4)."""
    (workspace / "headed.pdf").write_bytes(make_pdf([HEADED], artifacts={0: HEADER}))
    [page] = pdfhelper.pages("headed.pdf", "1", artifacts=True)["pages"]
    assert [(line["n"], line["text"], line.get("artifact", False)) for line in page["lines"]] == [
        (1, "Journal of Tests, vol. 1", True),
        (2, "the twist of the curve has", False),
        (3, "order 4r with r prime", False),
        (4, "7", True),
    ]
    assert "artifacts_left_out" not in page
    # Left out by default, but not renumbered: the body keeps lines 2 and 3.
    [page] = pdfhelper.pages("headed.pdf", "1", artifacts=False)["pages"]
    assert [(line["n"], line["text"]) for line in page["lines"]] == [
        (2, "the twist of the curve has"),
        (3, "order 4r with r prime"),
    ]
    assert page["artifacts_left_out"] == [1, 4]
    hits = pdfhelper.search("headed.pdf", "r prime")["hits"]
    assert [(hit["n"], hit.get("artifact", False)) for hit in hits] == [(3, False)]
    hits = pdfhelper.search("headed.pdf", "journal")["hits"]
    assert [(hit["n"], hit.get("artifact")) for hit in hits] == [(1, True)]
    assert pdfhelper.info("headed.pdf")["pages"][0]["has_text"]


def test_a_page_of_headers_alone_has_text_but_no_body(workspace: Path) -> None:
    (workspace / "blank.pdf").write_bytes(make_pdf([[]], artifacts={0: HEADER}))
    [page] = pdfhelper.pages("blank.pdf", "1", artifacts=False)["pages"]
    assert (page["lines"], page["artifacts_left_out"]) == ([], [1, 2])
    assert "note" not in page  # it has text: the header and the page number
    assert pdfhelper.info("blank.pdf")["pages"][0]["has_text"]


def test_a_line_that_mixes_a_header_and_body_text_is_body_text(workspace: Path) -> None:
    """The parser puts a header's words and the body's on one line: the line is kept,
    since leaving it out would lose the body's words."""
    mixed = make_pdf([[(110, 700, "Theorem 2 holds")]], artifacts={0: [(72, 700, "J. Tests")]})
    (workspace / "mixed.pdf").write_bytes(mixed)
    [page] = pdfhelper.pages("mixed.pdf", "1", artifacts=False)["pages"]
    assert [(line["n"], line["text"]) for line in page["lines"]] == [
        (1, "J. Tests Theorem 2 holds")
    ]
    assert "artifact" not in page["lines"][0]
    assert "artifacts_left_out" not in page


def test_search_names_pages_and_lines(workspace: Path) -> None:
    # PDF-F3
    result = pdfhelper.search("paper.pdf", "gf(17)")
    found = [(hit["page_index"], hit["page_label"], hit["n"]) for hit in result["hits"]]
    assert found == [(2, "ii", 2), (3, "1", 1), (3, "1", 2)]
    assert "truncated" not in result


def test_a_region_render_is_a_png_of_the_expected_size(workspace: Path) -> None:
    # PDF-F4: one inch square at 150 dpi is 150 pixels square.
    result = pdfhelper.render("paper.pdf", 3, "72,684,144,756", 150, None)
    image = result["image"]
    assert (image["width"], image["height"]) == (150, 150)
    data = (workspace / image["path"]).read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert hashlib.sha256(data).hexdigest() == image["sha256"]
    assert struct.unpack(">II", data[16:24]) == (150, 150)
    assert (result["page_index"], result["page_label"]) == (3, "1")
    whole = pdfhelper.render("paper.pdf", 1, None, 72, "renders/p1.png")
    assert (whole["image"]["width"], whole["image"]["height"]) == (612, 792)
    assert (workspace / "renders" / "p1.png").exists()


def test_a_page_without_text_says_so(workspace: Path) -> None:
    # PDF-F5
    assert pdfhelper.info("paper.pdf")["pages"][3]["has_text"] is False
    [page] = pdfhelper.pages("paper.pdf", "4", artifacts=False)["pages"]
    assert page["lines"] == []
    assert "render" in page["note"]


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: pdfhelper.info("notes.txt"), "is not a PDF"),
        (lambda: pdfhelper.pages("paper.pdf", "4-5", artifacts=False), "outside 1-4"),
        (lambda: pdfhelper.pages("paper.pdf", "two", artifacts=False), "not a page list"),
        (lambda: pdfhelper.info("/etc/passwd"), "relative"),
        (lambda: pdfhelper.info("../s1/paper.pdf"), "'..'"),
        (lambda: pdfhelper.info("link.pdf"), "symlink"),
        (lambda: pdfhelper.info("missing.pdf"), "does not exist"),
        (lambda: pdfhelper.render("paper.pdf", 9, None, 150, None), "outside 1-4"),
        (lambda: pdfhelper.render("paper.pdf", 1, "5,5,1,1", 150, None), "x0<x1"),
        (lambda: pdfhelper.render("paper.pdf", 1, None, 1000, None), "dpi"),
        (lambda: pdfhelper.search("paper.pdf", " "), "1 to"),
    ],
)
def test_bad_input_is_an_invalid_request(workspace: Path, call: Any, message: str) -> None:
    # PDF-F6
    (workspace / "notes.txt").write_text("not a pdf")
    (workspace / "link.pdf").symlink_to(workspace / "paper.pdf")
    before = sorted(path.name for path in workspace.iterdir())
    with pytest.raises(pdfhelper.HelperError, match=message) as refused:
        call()
    assert refused.value.kind == "invalid_request"
    assert sorted(path.name for path in workspace.iterdir()) == before


def test_too_many_pages_in_one_call_are_refused(workspace: Path) -> None:
    many = [[(72, 700, f"page {number}")] for number in range(30)]
    (workspace / "long.pdf").write_bytes(make_pdf(many))
    with pytest.raises(pdfhelper.HelperError, match="at most 20"):
        pdfhelper.pages("long.pdf", "1-21", artifacts=False)
    assert len(pdfhelper.pages("long.pdf", "1-20", artifacts=False)["pages"]) == 20


def test_shared_paths_resolve_to_the_shared_area(workspace: Path) -> None:
    shared = workspace.parent.parent / "shared"
    (shared / "lib.pdf").write_bytes(make_pdf([[(72, 700, "Shared")]]))
    assert (
        pdfhelper.pages("shared/lib.pdf", "1", artifacts=False)["pages"][0]["lines"][0]["text"]
        == "Shared"
    )


def test_a_malformed_pdf_is_a_parser_exception(workspace: Path) -> None:
    (workspace / "broken.pdf").write_bytes(b"%PDF-1.7\n" + b"garbage" * 100)
    with pytest.raises(pdfhelper.HelperError) as refused:
        pdfhelper.pages("broken.pdf", "1", artifacts=False)
    assert refused.value.kind in ("exception", "invalid_request")


def test_page_labels_follow_the_pdf_styles() -> None:
    class Document:
        def page_labels(self) -> list[dict[str, Any]]:
            return [
                {"start_page": 0, "style": "RomanUpper", "prefix": None, "start_value": 4},
                {"start_page": 2, "style": "AlphaLower", "prefix": "A-", "start_value": 26},
                {"start_page": 4, "style": None, "prefix": "Cover", "start_value": 1},
            ]

    assert pdfhelper.page_labels(Document(), 6) == ["IV", "V", "A-z", "A-aa", "Cover", "Cover"]


def test_the_png_encoder_round_trips() -> None:
    rows = [bytes([255, 0, 0, 255]) * 3, bytes([0, 0, 255, 255]) * 3]
    data = pdfhelper.png(3, 2, rows)
    assert data[12:16] == b"IHDR"
    idat_length = struct.unpack(">I", data[33:37])[0]
    raw = zlib.decompress(data[41 : 41 + idat_length])
    assert raw == b"".join(b"\x00" + row for row in rows)


def test_the_command_line_prints_json_and_exits_3_on_errors(workspace: Path) -> None:
    env = {"SESSION_WORKSPACE": str(workspace), "PATH": "/usr/bin:/bin"}
    ok = subprocess.run(
        [sys.executable, "-I", str(HELPER), "pages", "paper.pdf", "--pages", "1"],
        capture_output=True, text=True, env=env, cwd=workspace, check=False,
    )  # fmt: skip
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["pages"][0]["lines"][0]["text"] == "Preface"
    bad = subprocess.run(
        [sys.executable, "-I", str(HELPER), "info", "missing.pdf"],
        capture_output=True, text=True, env=env, cwd=workspace, check=False,
    )  # fmt: skip
    assert bad.returncode == pdfhelper.EXIT_ERROR
    assert json.loads(bad.stdout)["error"]["kind"] == "invalid_request"
