# SPDX-License-Identifier: MIT
"""A region for ``pdf_render``, from the boxes ``pdf_pages`` shows when asked: the
render's description sent the agent to boxes that ``pdf_pages`` did not show (the
pdf-reading skill, #423; docs/specs/pdf-sources.md)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sandbox_science.orchestrator.tools import PDF_SPECS, format_pdf
from sandbox_science.testing.pdfs import make_pdf
from sandbox_science.worker import pdfhelper

LINES = [(72, 700, "A Note on GF(17)"), (72, 680, "a^17 = a for every a in GF(17)")]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pytest.importorskip("pdf_oxide")
    monkeypatch.setenv("SESSION_WORKSPACE", str(tmp_path))
    (tmp_path / "note.pdf").write_bytes(make_pdf([LINES]))
    return tmp_path


def test_the_render_says_where_a_region_comes_from() -> None:
    specs = {spec.name: spec for spec in PDF_SPECS}
    assert "boxes" in specs["pdf_pages"].parameters["properties"]
    assert "pdf_pages and boxes true" in specs["pdf_render"].description
    assert "as pdf_pages reports them" not in specs["pdf_render"].description


def test_boxes_show_each_lines_region_for_a_render(workspace: Path) -> None:
    result = pdfhelper.pages("note.pdf", "1", artifacts=False)
    plain = format_pdf("pages", result)
    assert "[p1:1] A Note on GF(17)" in plain  # no boxes unless asked
    boxed = format_pdf("pages", result, boxes=True)
    found = re.search(r"^\[p1:2\] (\[[^\]]+\]) a\^17 = a for every a in GF\(17\)$", boxed, re.M)
    assert found is not None, boxed
    region = json.loads(found.group(1))
    assert region == result["pages"][0]["lines"][1]["bbox"]
    x0, y0, x1, y1 = region
    assert x0 <= 72 < x1
    assert y0 <= 680 < y1
    # The box, as shown, is a region the render takes.
    rendered = pdfhelper.render("note.pdf", 1, ",".join(str(n) for n in region), 144, None)
    assert rendered["region"] == region
    image = rendered["image"]
    assert abs(image["width"] - (x1 - x0) * 2) <= 1
    assert abs(image["height"] - (y1 - y0) * 2) <= 1
