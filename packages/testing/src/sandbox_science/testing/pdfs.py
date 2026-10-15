# SPDX-License-Identifier: MIT
"""Small PDFs whose text and positions are known, for the PDF helper's tests and the
citation check's (docs/specs/pdf-sources.md, PDF-F1 to PDF-F6)."""

from __future__ import annotations

from collections.abc import Sequence


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
    artifacts: dict[int, Sequence[tuple[float, float, str]]] | None = None,
) -> bytes:
    """``pages`` holds (x, y, text) lines per page; ``labels`` (start index, style) ranges;
    ``outline`` (title, zero-based page) entries; ``image_page`` a page drawn as an image
    only; ``artifacts`` the (x, y, text) lines of a zero-based page drawn as running
    headers and footers, tagged ``/Artifact``, before its other lines."""
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
                (
                    "/Artifact << /Type /Pagination >> BDC\n"
                    f"BT /F1 9 Tf {x:.2f} {y:.2f} Td {_text(text)} Tj ET\nEMC\n"
                ).encode("latin-1")
                for x, y, text in (artifacts or {}).get(number, ())
            ) + b"".join(
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
        items: list[int] = []
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
    offsets: list[int] = []
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
