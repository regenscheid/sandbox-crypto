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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pdf_oxide")

from sandbox_science.worker import pdfhelper

HELPER = Path(pdfhelper.__file__)


# A minimal PDF writer: Helvetica text at given positions, page labels, an outline, and
# optionally an image-only page.
def _text(value: str) -> str:
    return "(" + value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def make_pdf(
    pages: Sequence[Sequence[tuple[float, float, str]]],
    *,
    labels: list[tuple[int, str]] | None = None,
    outline: list[tuple[str, int]] | None = None,
    image_page: int | None = None,
) -> bytes:
    """``pages`` holds (x, y, text) lines per page; ``labels`` (start index, style) ranges;
    ``outline`` (title, zero-based page) entries; ``image_page`` a page drawn as an image only."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in at the end
    pages_ref = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    image = add(
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray"
        b" /BitsPerComponent 8 /Length 4 >>\nstream\n\x00\xff\xff\x00\nendstream"
    )
    page_refs: list[int] = []
    for number, lines in enumerate(pages):
        if number == image_page:
            content = b"q 200 0 0 200 100 400 cm /Im1 Do Q"
        else:
            content = b"".join(
                f"BT /F1 12 Tf {x:.2f} {y:.2f} Td {_text(text)} Tj ET\n".encode("latin-1")
                for x, y, text in lines
            )
        stream = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        page = add(
            (
                f"<< /Type /Page /Parent {pages_ref} 0 R /MediaBox [0 0 612 792]"
                f" /Resources << /Font << /F1 {font} 0 R >> /XObject << /Im1 {image} 0 R >> >>"
                f" /Contents {stream} 0 R >>"
            ).encode()
        )
        page_refs.append(page)
    kids = " ".join(f"{ref} 0 R" for ref in page_refs)
    objects[pages_ref - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_refs)} >>".encode()
    extra = ""
    if labels:
        nums = " ".join(f"{start} << /S /{style} >>" for start, style in labels)
        extra += f" /PageLabels << /Nums [{nums}] >>"
    if outline:
        root = add(b"")
        items = []
        for _ in outline:
            items.append(add(b""))
        for position, ((title, page), ref) in enumerate(zip(outline, items, strict=True)):
            links = f" /Parent {root} 0 R /Dest [{page_refs[page]} 0 R /XYZ 0 792 0]"
            if position > 0:
                links += f" /Prev {items[position - 1]} 0 R"
            if position < len(items) - 1:
                links += f" /Next {items[position + 1]} 0 R"
            objects[ref - 1] = f"<< /Title {_text(title)}{links} >>".encode("latin-1")
        objects[root - 1] = (
            f"<< /Type /Outlines /First {items[0]} 0 R /Last {items[-1]} 0 R /Count {len(items)} >>"
        ).encode()
        extra += f" /Outlines {root} 0 R"
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_ref} 0 R{extra} >>".encode()

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        xref,
    )
    return bytes(out)


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
