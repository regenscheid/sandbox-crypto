# SPDX-License-Identifier: MIT
"""Figure files on the preview origin (docs/specs/figures.md, FG-12 to FG-14): a PNG or a
JPEG served for a capability the workspace minted, only as its recorded type and only
when its first bytes are that type's, within 8 MiB and 50 megapixels; an SVG for the
figure frame and an HTML file as a frame's page without scripts; the figure frame's
bundle with its own policy, never served by the workspace; anything else is a link.
Files are published through the inventory's public methods, as the harness does.
"""

from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import pytest

from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.orchestrator.projects import ProjectStore
from sandbox_science.workspace.app import create_app
from sandbox_science.workspace.preview import (
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SIDE,
    Capabilities,
    create_preview_app,
    image_problem,
    jpeg_size,
    png_size,
    svg_begins,
    text_problem,
)

from .route_support import TOKEN, ClientFactory
from .test_pdf_viewer import PDF_BYTES, Clock, publish
from .test_pdf_viewer import origins as origins
from .test_pdf_viewer import published as published


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


HTML = b"<html><script>parent.postMessage('x', '*')</script></html>"
SVG_BYTES = (
    b'<?xml version="1.0"?>\n<!-- a plot -->\n<svg xmlns="http://www.w3.org/2000/svg" '
    b'onload="alert(1)"><script>alert(2)</script><image href="https://evil.example/x.png"/>'
    b"</svg>"
)


@pytest.fixture
def files(state_dir: Path) -> dict[str, Any]:
    documents = DocumentStore(store_path(state_dir))
    inventory = ArtifactInventory(documents)
    made = {
        "png": publish(inventory, "op-png", "plots/a.png", png(640, 480), "image/png"),
        "jpeg": publish(inventory, "op-jpg", "plots/b.jpg", jpeg(32, 16), "image/jpeg"),
        # A ".png" that holds HTML, recorded as an image.
        "html_png": publish(inventory, "op-hp", "plots/c.png", HTML, "image/png"),
        "huge": publish(inventory, "op-big", "plots/d.png", png(10000, 6000), "image/png"),
        "svg": publish(inventory, "op-svg", "plots/e.svg", SVG_BYTES, "image/svg+xml"),
        "html": publish(inventory, "op-html", "tables/t.html", HTML, "text/html"),
        # An ".svg" that holds HTML, recorded as an SVG.
        "html_svg": publish(inventory, "op-hs", "plots/f.svg", HTML, "image/svg+xml"),
        "pdf": publish(inventory, "op-pdf2", "plots/g.pdf", PDF_BYTES, "application/pdf"),
        "text": publish(inventory, "op-txt2", "notes.txt", b"plain", "text/plain"),
    }
    documents.close()
    return made


async def figure_link(workspace: Any, revision: Any) -> Any:
    return await workspace.get(
        f"/api/v0/projects/p1/artifacts/{revision.artifact_id}/revisions/{revision.revision}/figure",
        params={"sha256": revision.sha256},
    )


async def test_an_image_is_served_by_capability_as_its_type(origins, files) -> None:
    workspace, viewer, preview, _ = origins
    for kind, media in (("png", "image/png"), ("jpeg", "image/jpeg")):
        answer = await figure_link(workspace, files[kind])
        assert answer.status == 200
        link = await answer.json()
        assert link["media_type"] == media
        assert link["url"].startswith(f"{preview.origin}/file/")
        served = await viewer.get(link["url"].removeprefix(preview.origin))
        assert served.status == 200
        assert served.headers["Content-Type"] == media
        assert served.headers["X-Content-Type-Options"] == "nosniff"
        assert served.headers["Cache-Control"] == "no-store"
        assert served.headers["Content-Security-Policy"].endswith("; sandbox")
        # The workspace's page, another origin, may load it; nothing else is relaxed.
        assert served.headers["Cross-Origin-Resource-Policy"] == "cross-origin"
        assert await served.read() == (png(640, 480) if kind == "png" else jpeg(32, 16))
    policy = (await workspace.get("/")).headers["Content-Security-Policy"]
    assert f"img-src 'self' {preview.origin};" in policy


async def test_bytes_that_are_not_their_type_or_too_large_are_refused(origins, files) -> None:
    workspace, viewer, preview, _ = origins
    for kind, status in (("html_png", 415), ("huge", 413)):
        link = await (await figure_link(workspace, files[kind])).json()
        refused = await viewer.get(link["url"].removeprefix(preview.origin))
        assert refused.status == status, kind
        assert refused.headers["Cross-Origin-Resource-Policy"] == "same-origin"
        assert "script" not in await refused.text()
    # Another type is a link: the workspace mints nothing for it.
    answer = await figure_link(workspace, files["text"])
    assert answer.status == 422  # unsupported: the GUI shows a link


async def test_a_figure_capability_opens_its_own_file_only(origins, files, published) -> None:
    workspace, viewer, preview, clock = origins
    link = await (await figure_link(workspace, files["png"])).json()
    path = link["url"].removeprefix(preview.origin)
    # A capability opens only the route it was minted for (review of #487, I1).
    pdf = published["pdf"]
    for_viewer = preview.capabilities.mint("p1", pdf.artifact_id, 1, pdf.sha256, "pdf")
    assert (await viewer.get(f"/file/{for_viewer}")).status == 403
    assert (await viewer.get(f"/pdf/{path.removeprefix('/file/')}")).status == 403
    as_file = preview.capabilities.mint("p1", pdf.artifact_id, 1, pdf.sha256, "file")
    assert (await viewer.get(f"/file/{as_file}")).status == 415  # a PDF is no image
    assert (await viewer.get(path[:-2] + "AA")).status == 403  # forged
    clock.now += 601
    assert (await viewer.get(path)).status == 403  # expired


def test_headers_give_the_size_and_the_first_bytes_the_type() -> None:
    assert png_size(png(3, 5)) == (3, 5)
    assert jpeg_size(jpeg(7, 9)) == (7, 9)
    assert (png_size(HTML), jpeg_size(HTML)) == (None, None)
    assert jpeg_size(b"\xff\xd8\xff\xda\x00\x02") is None  # a scan before any frame
    assert jpeg_size(b"\xff\xd8\xff") is None
    assert image_problem("image/png", png(1, 1)) is None
    assert image_problem("image/png", png(0, 10)) is not None
    # Each side is capped too: a sliver within the pixels is no image a page lays out.
    assert image_problem("image/png", png(MAX_IMAGE_SIDE + 1, 1)) is not None
    assert image_problem("image/png", png(1, MAX_IMAGE_SIDE)) is None
    side = int(MAX_IMAGE_PIXELS**0.5) + 1
    assert image_problem("image/jpeg", jpeg(side, side)) == (
        413,
        "This image is larger than a page shows.",
    )


async def test_each_figure_kind_is_shown_its_own_way(origins, files) -> None:
    """FG-12, FG-14: an SVG in the figure frame, given its capability by message; an HTML
    file as a frame's page; a PDF in the viewer's figure mode."""
    workspace, _, preview, _ = origins
    svg = await (await figure_link(workspace, files["svg"])).json()
    assert svg["kind"] == "svg"
    assert svg["url"] == f"{preview.origin}/figures/#{urlencode({'w': preview.workspace_origin})}"
    assert svg["capability"]  # posted to the frame, never in its URL
    assert svg["capability"] not in svg["url"]
    html = await (await figure_link(workspace, files["html"])).json()
    assert (html["kind"], html["capability"]) == ("html", None)
    assert html["url"].startswith(f"{preview.origin}/file/")
    pdf = await (await figure_link(workspace, files["pdf"])).json()
    assert pdf["kind"] == "pdf"
    fragment = parse_qs(pdf["url"].split("#", 1)[1])
    assert (fragment["m"], fragment["w"]) == (["figure"], [preview.workspace_origin])
    assert pdf["url"].startswith(f"{preview.origin}/viewer/#")


async def test_an_svg_and_an_html_figure_are_served_inert(origins, files) -> None:
    workspace, viewer, preview, _ = origins
    svg = await (await figure_link(workspace, files["svg"])).json()
    served = await viewer.get(f"/file/{svg['capability']}")
    assert served.status == 200
    assert served.headers["Content-Type"] == "image/svg+xml; charset=utf-8"
    # The figure frame fetches it from its own origin; nothing else may load it, and
    # opened directly it downloads.
    assert served.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert served.headers["Content-Disposition"] == "attachment"
    assert served.headers["Content-Security-Policy"].endswith("; sandbox")
    html = await (await figure_link(workspace, files["html"])).json()
    page = await viewer.get(html["url"].removeprefix(preview.origin))
    assert page.status == 200
    assert page.headers["Content-Type"] == "text/html; charset=utf-8"
    assert page.headers["Content-Disposition"] == "inline"
    policy = page.headers["Content-Security-Policy"]
    # Scripts off: a sandbox with no allowances; framed by the workspace only; styles inline.
    assert policy.endswith("; sandbox")
    assert f"frame-ancestors {preview.workspace_origin}" in policy
    assert "style-src 'self' 'unsafe-inline'" in policy
    assert "unsafe-eval" not in policy
    assert "connect-src 'self'" in policy
    # An ".svg" holding HTML is not served as an SVG.
    fake = await (await figure_link(workspace, files["html_svg"])).json()
    assert (await viewer.get(f"/file/{fake['capability']}")).status == 415


def test_the_svg_check_is_linear_on_hostile_prologs() -> None:
    """Review of #492, H1: thousands of comments, unclosed ones and long doctypes are
    checked in a time that grows with their length only."""
    hostile = [
        "<!---->" * 200_000 + "<svg/>",
        "<!--" * 200_000,
        "<!-- -" * 200_000 + "<svg/>",
        "<?xml" + " " * 1_000_000,
        "<!DOCTYPE svg" + " a" * 500_000,
        " " * 2_000_000 + "<svg/>",
    ]
    start = time.monotonic()
    found = [svg_begins(text) for text in hostile]
    assert time.monotonic() - start < 2.0
    assert found == [True, False, False, False, False, True]
    assert text_problem("image/svg+xml", ("<!---->" * 200_000 + "<svg/>").encode()) is None


def test_text_figures_are_utf8_and_an_svg_begins_as_one() -> None:
    assert text_problem("image/svg+xml", SVG_BYTES) is None
    assert text_problem("image/svg+xml", "\ufeff<svg>".encode()) is None
    assert text_problem("image/svg+xml", b"<!DOCTYPE svg PUBLIC 'x'><svg/>") is None
    assert text_problem("image/svg+xml", b"<!-- a --><!DOCTYPE svg>\n<!-- b -->\n<SVG>") is None
    for bad in (
        HTML,
        b"<!DOCTYPE html><svg/>",
        b"<!DOCTYPE svg [<!ENTITY x 'y'>]><svg/>",
        b"<!DOCTYPE svg><!DOCTYPE svg><svg/>",
        b"<!DOCTYPE svgx><svg/>",
        b"\x89PNG",
        b"<svgx/>",
        b"<svg\x00/>",
        b"<!-- <svg/>",
    ):
        assert text_problem("image/svg+xml", bad) is not None, bad
    assert text_problem("text/html", HTML) is None
    assert text_problem("text/html", b"\xff\xfe<html>") is not None


async def test_the_figure_frame_bundle_has_its_own_policy(
    aiohttp_client: ClientFactory, state_dir: Path, tmp_path: Path, static_dir: Path
) -> None:
    bundle = tmp_path / "figures"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text("<title>figure</title>")
    (bundle / "assets" / "main.js").write_text("export {}")
    (tmp_path / "secret.txt").write_text("not the bundle's")
    documents = DocumentStore(store_path(state_dir), readonly=True)
    preview = await aiohttp_client(
        create_preview_app(
            documents,
            Capabilities(Clock()),
            workspace_origin=lambda: "http://127.0.0.1:8200",
            figures_dir=bundle,
        )
    )
    page = await preview.get("/figures/")
    assert page.status == 200
    policy = page.headers["Content-Security-Policy"]
    assert "style-src 'self' 'unsafe-inline'" in policy
    assert "script-src 'self';" in policy
    assert "unsafe-eval" not in policy
    assert "connect-src 'self'" in policy
    assert "frame-ancestors http://127.0.0.1:8200" in policy
    assert (await preview.get("/figures/assets/main.js")).status == 200
    assert (await preview.get("/figures/../secret.txt")).status == 404
    # The viewer keeps its own stricter policy.
    viewer = await preview.get("/viewer/")
    assert viewer.status == 404  # no viewer bundle here
    documents.close()
    # The workspace's own listener never serves the frame.
    (static_dir / "figures").mkdir()
    (static_dir / "figures" / "index.html").write_text("<title>figure</title>")
    workspace = await aiohttp_client(
        create_app(ProjectStore(state_dir, readonly=True), static_dir, login_token=TOKEN)
    )
    for path in ("/figures/", "/figures/index.html", "/FIGURES/index.html"):
        response = await workspace.get(path)
        assert response.status == 404, path
        assert "figure" not in await response.text(), path
