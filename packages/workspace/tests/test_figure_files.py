# SPDX-License-Identifier: MIT
"""Figure files on the preview origin (docs/specs/figures.md, FG-12, FG-13): a PNG or a
JPEG served for a capability the workspace minted, only as its recorded type and only
when its first bytes are that type's, within 8 MiB and 50 megapixels; anything else is a
link. Files are published through the inventory's public methods, as the harness does.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from sandbox_science.orchestrator.artifacts import ArtifactInventory
from sandbox_science.orchestrator.documents import DocumentStore, store_path
from sandbox_science.workspace.preview import (
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SIDE,
    image_problem,
    jpeg_size,
    png_size,
)

from .test_pdf_viewer import origins as origins
from .test_pdf_viewer import publish
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
        "svg": publish(inventory, "op-svg", "plots/e.svg", b"<svg/>", "image/svg+xml"),
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
    answer = await figure_link(workspace, files["svg"])
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
