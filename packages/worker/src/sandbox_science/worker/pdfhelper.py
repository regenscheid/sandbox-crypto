# SPDX-License-Identifier: MIT
"""PDF page facts, text, search and renders, inside the sandbox (docs/specs/pdf-sources.md).

The worker copies this file beside the executor and runs it as its own process with the
environment's interpreter::

    python -I pdfhelper.py info PATH
    python -I pdfhelper.py pages PATH --pages 1-3,5 [--include-artifacts]
    python -I pdfhelper.py search PATH --query TEXT
    python -I pdfhelper.py render PATH --page N [--region X0,Y0,X1,Y1] [--dpi 150] [--out FILE]
    python -I pdfhelper.py draw PATH [--page N] [--region X0,Y0,X1,Y1]

It needs only the standard library and pdf_oxide (D59), and prints one JSON object: the
result, or ``{"error": {"kind", "message"}}`` with exit status 3. PDFs are untrusted, so
they are parsed here, under the limits of a tool call, and never beside the worker.

Paths follow the file tools' rules: relative to the session workspace, or ``shared/…``
for the project's shared area; no absolute paths, ``..`` or symlinks. Boxes are
``[x0, y0, x1, y1]`` in PDF points, origin at the bottom left of the media box.

Lines are numbered one way in every operation: each line of a page that has text, from 1
in the parser's order, running headers, footers and page numbers (tagged ``/Artifact``)
included and marked ``artifact``. ``pages`` leaves those out unless asked, but never
renumbers the rest, so a line cited from ``pages`` or ``search`` is the same line, as the
citation check reads it (docs/specs/pdf-sources.md; consistency review CR-4).

``draw`` turns a recorded figure into a PNG that ``view_image`` can send
(docs/specs/figures.md, FG-35, FG-36): a PDF page or region, an SVG, or a PNG or JPEG
too large to send, scaled down to at most ``DRAW_SIDE`` pixels on its longer side. It
writes nothing: its JSON holds the PNG in base64, with the source's SHA-256, which the
harness checks against the revision it pinned. PDFs need pdf_oxide; SVG, ``rsvg-convert``
(librsvg) or cairosvg; PNG and JPEG, Pillow. A missing one is ``unsupported``, naming it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import zlib
from base64 import b64encode
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Any, cast

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_DOCUMENT_PAGES = 5000
MAX_PAGES_PER_CALL = 20
MAX_LINES_PER_PAGE = 2000
MAX_LINE_CHARS = 2000
MAX_HITS = 200
MAX_QUERY_CHARS = 200
MAX_OUTLINE_ENTRIES = 2000
DEFAULT_DPI = 150
MAX_DPI = 300
MAX_PIXELS = 40_000_000
RENDER_DIR = "pdf-renders"
DRAW_SIDE = 1600
"""A drawn image's longer side, in pixels: what ``view_image`` sends at most."""
DRAW_BYTES = 4 * 1024 * 1024
DRAW_PIXELS = 50_000_000
"""The most pixels a PNG or JPEG drawn may have before it is scaled."""
SVG_SECONDS = 60
EXIT_ERROR = 3


class HelperError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# Paths.


def _base(shared: bool) -> Path:
    if shared:
        area = os.environ.get("PROJECT_SHARED")
        if not area:
            raise HelperError("invalid_request", "there is no shared area here")
        return Path(area)
    return Path(os.environ.get("SESSION_WORKSPACE") or ".")


def resolve(text: str, *, must_exist: bool = True) -> tuple[Path, str]:
    """The confined path for ``text``, and ``text`` as given; refuse anything else."""
    if not text or text.startswith("/") or "\x00" in text:
        raise HelperError("invalid_request", "give a path relative to the session workspace")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise HelperError("invalid_request", f"{text!r} has an empty, '.' or '..' segment")
    shared = parts[0] == "shared"
    path = _base(shared)
    for part in parts[1:] if shared else parts:
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            if must_exist:
                raise HelperError("invalid_request", f"{text!r} does not exist") from None
            continue
        if stat.S_ISLNK(mode):
            raise HelperError("invalid_request", f"{text!r} goes through a symlink")
    return path, text


def read_pdf(text: str) -> tuple[bytes, dict[str, Any]]:
    data, source = read_file(text)
    if b"%PDF-" not in data[:1024]:
        raise HelperError("invalid_request", f"{text!r} is not a PDF")
    return data, source


def read_file(text: str) -> tuple[bytes, dict[str, Any]]:
    """A regular file's bytes, read once, and its path, size and SHA-256."""
    path, given = resolve(text)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise HelperError("invalid_request", f"cannot open {given!r}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise HelperError("invalid_request", f"{given!r} is not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise HelperError("invalid_request", f"{given!r} is over {MAX_FILE_BYTES} bytes")
        data = handle.read(MAX_FILE_BYTES + 1)
    source = {"path": given, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return data, source


# The parser.


def open_document(data: bytes) -> Any:
    try:
        import pdf_oxide
    except ImportError:
        raise HelperError(
            "unsupported", "pdf_oxide is not installed in this environment (pip install pdf_oxide)"
        ) from None
    try:
        document = pdf_oxide.PdfDocument.from_bytes(data)
        count = int(document.page_count())
    except Exception as exc:
        text = str(exc)
        if "encrypt" in text.lower() or "password" in text.lower():
            raise HelperError("unsupported", f"the PDF is encrypted: {text[:300]}") from exc
        raise HelperError("exception", f"the parser rejected the PDF: {text[:300]}") from exc
    if count > MAX_DOCUMENT_PAGES:
        raise HelperError(
            "invalid_request", f"the PDF has {count} pages, over {MAX_DOCUMENT_PAGES}"
        )
    return document


def parser() -> dict[str, str]:
    import pdf_oxide

    return {"name": "pdf_oxide", "version": str(getattr(pdf_oxide, "VERSION", "unknown"))}


def _roman(number: int) -> str:
    digits = [
        (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
        (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
    ]  # fmt: skip
    out: list[str] = []
    for value, letters in digits:
        while number >= value:
            out.append(letters)
            number -= value
    return "".join(out)


def _letters(number: int) -> str:
    # PDF: a..z, then aa..zz, and so on (ISO 32000-1, 12.4.2).
    return chr(ord("a") + (number - 1) % 26) * ((number - 1) // 26 + 1)


def page_labels(document: Any, count: int) -> list[str]:
    """Each page's printed label; the physical number where the document defines none."""
    labels = [str(index + 1) for index in range(count)]
    try:
        ranges = sorted(document.page_labels() or [], key=lambda item: item["start_page"])
    except Exception:
        return labels
    for position, item in enumerate(ranges):
        start = int(item["start_page"])
        end = int(ranges[position + 1]["start_page"]) if position + 1 < len(ranges) else count
        style = str(item.get("style") or "")
        prefix = str(item.get("prefix") or "")
        first = int(item.get("start_value") or 1)
        for index in range(max(start, 0), min(end, count)):
            value = first + index - start
            match style:
                case "Decimal":
                    body = str(value)
                case "RomanLower":
                    body = _roman(value)
                case "RomanUpper":
                    body = _roman(value).upper()
                case "AlphaLower":
                    body = _letters(value)
                case "AlphaUpper":
                    body = _letters(value).upper()
                case _:
                    body = ""
            labels[index] = (prefix + body) or str(index + 1)
    return labels


def _box(raw: Any) -> list[float]:
    x, y, width, height = (float(value) for value in raw)
    return [round(x, 2), round(y, 2), round(x + width, 2), round(y + height, 2)]


def _word(word: Any) -> tuple[str, tuple[float, ...]]:
    return str(word.text), tuple(round(float(value), 3) for value in word.bbox)


def lines_of(document: Any, index: int) -> list[dict[str, Any]]:
    """Every line of page ``index`` that has text, numbered from 1 in the parser's order.
    A line none of whose words is body text, as the parser tells body text from text
    tagged ``/Artifact``, is marked ``artifact``: a running header or footer, or a page
    number. A line that mixes the two is body text."""
    body = Counter(
        _word(word)
        for line in document.extract_text_lines(index, include_artifacts=False)
        for word in line.words
    )
    lines: list[dict[str, Any]] = []
    for line in document.extract_text_lines(index, include_artifacts=True):
        text = str(line.text).strip()
        if not text:
            continue
        entry: dict[str, Any] = {
            "n": len(lines) + 1,
            "text": text[:MAX_LINE_CHARS],
            "bbox": _box(line.bbox),
        }
        words = [_word(word) for word in line.words]
        if words and not any(body[word] for word in words):
            entry["artifact"] = True
        for word in words:
            if body[word]:
                body[word] -= 1
        lines.append(entry)
        if len(lines) >= MAX_LINES_PER_PAGE:
            break
    return lines


def has_text(document: Any, index: int) -> bool:
    """Whether page ``index`` has a line of extractable text, of any kind."""
    return any(
        str(line.text).strip()
        for line in document.extract_text_lines(index, include_artifacts=True)
    )


def page_numbers(spec: str, count: int) -> list[int]:
    """Zero-based indexes for a 1-based spec such as ``1-3,5``."""
    indexes: list[int] = []
    for part in spec.split(","):
        first, _, last = part.strip().partition("-")
        try:
            start, end = int(first), int(last or first)
        except ValueError:
            raise HelperError(
                "invalid_request", f"{spec!r} is not a page list such as 1-3,5"
            ) from None
        if not 1 <= start <= end <= count:
            raise HelperError("invalid_request", f"pages {part.strip()} are outside 1-{count}")
        indexes.extend(range(start - 1, end))
    indexes = sorted(set(indexes))
    if len(indexes) > MAX_PAGES_PER_CALL:
        raise HelperError(
            "invalid_request",
            f"{len(indexes)} pages asked for; at most {MAX_PAGES_PER_CALL} a call",
        )
    return indexes


# Operations.


def info(path: str) -> dict[str, Any]:
    data, source = read_pdf(path)
    document = open_document(data)
    count = int(document.page_count())
    labels = page_labels(document, count)
    pages: list[dict[str, Any]] = []
    for index in range(count):
        x0, y0, x1, y1 = (float(value) for value in document.page_media_box(index))
        pages.append(
            {
                "index": index + 1,
                "label": labels[index],
                "width": round(x1 - x0, 2),
                "height": round(y1 - y0, 2),
                "rotation": int(document.page_rotation(index) or 0),
                "has_text": has_text(document, index),
            }
        )
    outline: list[dict[str, Any]] = []

    def walk(entries: Any, level: int) -> None:
        for entry in cast(list[dict[str, Any]], entries or []):
            if len(outline) >= MAX_OUTLINE_ENTRIES:
                return
            page = entry.get("page")
            outline.append(
                {
                    "title": str(entry.get("title", ""))[:MAX_LINE_CHARS],
                    "level": level,
                    "page_index": int(page) + 1
                    if isinstance(page, int) and 0 <= page < count
                    else None,
                }
            )
            walk(entry.get("children"), level + 1)

    try:
        walk(document.get_outline(), 1)
    except Exception:
        outline = []
    return {
        "parser": parser(),
        "source": source,
        "page_count": count,
        "pages": pages,
        "outline": outline,
    }


def pages(path: str, spec: str, *, artifacts: bool) -> dict[str, Any]:
    data, source = read_pdf(path)
    document = open_document(data)
    count = int(document.page_count())
    labels = page_labels(document, count)
    result: list[dict[str, Any]] = []
    for index in page_numbers(spec, count):
        every = lines_of(document, index)
        lines = every if artifacts else [line for line in every if not line.get("artifact")]
        entry: dict[str, Any] = {"index": index + 1, "label": labels[index], "lines": lines}
        if not artifacts and len(lines) < len(every):
            # Left out, not renumbered: the numbers stay those pdf_search gives.
            entry["artifacts_left_out"] = [line["n"] for line in every if line.get("artifact")]
        if len(every) >= MAX_LINES_PER_PAGE:
            entry["truncated"] = True
        if not every:
            entry["note"] = "no extractable text on this page; render it to read it"
        result.append(entry)
    # The page count too, so the read register can tell a whole read from a part (CM-12).
    return {"parser": parser(), "source": source, "page_count": count, "pages": result}


def search(path: str, query: str) -> dict[str, Any]:
    needle = query.strip().casefold()
    if not needle or len(needle) > MAX_QUERY_CHARS:
        raise HelperError("invalid_request", f"search for 1 to {MAX_QUERY_CHARS} characters")
    data, source = read_pdf(path)
    document = open_document(data)
    count = int(document.page_count())
    labels = page_labels(document, count)
    hits: list[dict[str, Any]] = []
    truncated = False
    for index in range(count):
        for line in lines_of(document, index):
            if needle in line["text"].casefold():
                if len(hits) >= MAX_HITS:
                    truncated = True
                    break
                hits.append({"page_index": index + 1, "page_label": labels[index], **line})
        if truncated:
            break
    result: dict[str, Any] = {"parser": parser(), "source": source, "query": query, "hits": hits}
    if truncated:
        result["truncated"] = True
    return result


def png(width: int, height: int, rows: list[bytes]) -> bytes:
    """An 8-bit RGBA PNG from its rows, with the standard library only."""

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def render(path: str, page: int, region: str | None, dpi: int, out: str | None) -> dict[str, Any]:
    if not 18 <= dpi <= MAX_DPI:
        raise HelperError("invalid_request", f"dpi must be 18 to {MAX_DPI}")
    data, source = read_pdf(path)
    document = open_document(data)
    count = int(document.page_count())
    if not 1 <= page <= count:
        raise HelperError("invalid_request", f"page {page} is outside 1-{count}")
    index = page - 1
    x0, y0, x1, y1 = (float(value) for value in document.page_media_box(index))
    scale = dpi / 72.0
    if (x1 - x0) * scale * (y1 - y0) * scale > MAX_PIXELS:
        raise HelperError("invalid_request", "the render would be too large; lower the dpi")
    box = _region(document, index, region)
    left, top, right, bottom, rows = _pixels(document, index, box, dpi, (x0, y0, x1, y1))
    image = png(right - left, bottom - top, rows)
    digest = hashlib.sha256(image).hexdigest()
    if out is None:
        suffix = "" if box is None else "-" + hashlib.sha256(str(region).encode()).hexdigest()[:8]
        out = f"{RENDER_DIR}/{source['sha256'][:12]}-p{page}{suffix}-{dpi}dpi.png"
    target, given = resolve(out, must_exist=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    except OSError as exc:
        raise HelperError("invalid_request", f"cannot write {given!r}: {exc.strerror}") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(image)
    labels = page_labels(document, count)
    return {
        "parser": parser(),
        "source": source,
        "page_index": page,
        "page_label": labels[index],
        "region": box,
        "dpi": dpi,
        "image": {"path": given, "sha256": digest, "width": right - left, "height": bottom - top},
    }


def _region(document: Any, index: int, region: str | None) -> list[float] | None:
    if region is None:
        return None
    if int(document.page_rotation(index) or 0) % 360:
        raise HelperError("unsupported", "regions of rotated pages are not supported yet")
    try:
        box = [float(value) for value in region.split(",")]
    except ValueError:
        box = []
    if len(box) != 4 or not (box[0] < box[2] and box[1] < box[3]):
        raise HelperError("invalid_request", "region must be x0,y0,x1,y1 with x0<x1 and y0<y1")
    return box


def _pixels(
    document: Any,
    index: int,
    box: list[float] | None,
    dpi: int,
    media: tuple[float, float, float, float],
) -> tuple[int, int, int, int, list[bytes]]:
    """A page drawn at ``dpi``, cut to ``box``: its bounds and its RGBA rows."""
    x0, y0, x1, y1 = media
    pixmap = document.render_pixmap(index, dpi=dpi)
    width, height, pixels = int(pixmap.width), int(pixmap.height), bytes(pixmap.data)
    left, top, right, bottom = 0, 0, width, height
    if box is not None:
        sx, sy = width / (x1 - x0), height / (y1 - y0)
        left = max(0, min(width, int((box[0] - x0) * sx)))
        right = max(0, min(width, int((box[2] - x0) * sx + 0.999)))
        top = max(0, min(height, int((y1 - box[3]) * sy)))
        bottom = max(0, min(height, int((y1 - box[1]) * sy + 0.999)))
        if right <= left or bottom <= top:
            raise HelperError("invalid_request", "the region lies outside the page")
    stride = width * 4
    rows = [
        pixels[row * stride + left * 4 : row * stride + right * 4] for row in range(top, bottom)
    ]
    return left, top, right, bottom, rows


def draw(path: str, page: int | None, region: str | None) -> dict[str, Any]:
    """A recorded figure as a PNG of at most ``DRAW_SIDE`` pixels on its longer side,
    in the result, not in a file (FG-35)."""
    data, source = read_file(path)
    if b"%PDF-" in data[:1024]:
        drawn = _draw_pdf(data, page or 1, region)
    elif page is not None or region is not None:
        raise HelperError("invalid_request", "a page and a region are for PDFs")
    elif data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        drawn = _draw_raster(data)
    elif b"<svg" in data[:4096]:
        drawn = _draw_svg(data)
    else:
        raise HelperError("unsupported", f"{path!r} is not a PDF, an SVG, a PNG or a JPEG")
    if len(drawn) > DRAW_BYTES:
        raise HelperError("unsupported", f"the drawing is over {DRAW_BYTES} bytes as a PNG")
    if not drawn.startswith(b"\x89PNG\r\n\x1a\n") or len(drawn) < 24:
        raise HelperError("exception", "the renderer did not make a PNG")
    width, height = struct.unpack(">II", drawn[16:24])
    return {
        "source": source,
        "image": {
            "media_type": "image/png",
            "width": width,
            "height": height,
            "sha256": hashlib.sha256(drawn).hexdigest(),
            "data": b64encode(drawn).decode("ascii"),
        },
    }


def _draw_pdf(data: bytes, page: int, region: str | None) -> bytes:
    document = open_document(data)
    count = int(document.page_count())
    if not 1 <= page <= count:
        raise HelperError("invalid_request", f"page {page} is outside 1-{count}")
    index = page - 1
    media = tuple(float(value) for value in document.page_media_box(index))
    x0, y0, x1, y1 = media
    box = _region(document, index, region)
    wide, high = (box[2] - box[0], box[3] - box[1]) if box is not None else (x1 - x0, y1 - y0)
    # The dots per inch that make the longer side DRAW_SIDE pixels, within MAX_PIXELS.
    dpi = max(1, min(MAX_DPI, int(72 * DRAW_SIDE / max(wide, high, 1.0))))
    while dpi > 1 and (x1 - x0) * (y1 - y0) * (dpi / 72) ** 2 > MAX_PIXELS:
        dpi //= 2
    left, top, right, bottom, rows = _pixels(document, index, box, dpi, (x0, y0, x1, y1))
    return png(right - left, bottom - top, rows)


def _draw_raster(data: bytes) -> bytes:
    try:
        image_module: Any = importlib.import_module("PIL.Image")  # optional (tier 3)
    except ImportError:
        raise HelperError(
            "unsupported", "Pillow is not installed in this environment, which scales images"
        ) from None
    image_module.MAX_IMAGE_PIXELS = DRAW_PIXELS
    try:
        with image_module.open(BytesIO(data)) as image:
            image.thumbnail((DRAW_SIDE, DRAW_SIDE))
            out = BytesIO()
            image.convert("RGBA").save(out, format="PNG")
    except Exception as exc:
        raise HelperError("exception", f"the image could not be read: {str(exc)[:300]}") from exc
    return out.getvalue()


def _draw_svg(data: bytes) -> bytes:
    rsvg = shutil.which("rsvg-convert")
    if rsvg is not None:
        try:
            done = subprocess.run(  # noqa: S603 - a fixed program, the SVG on its stdin
                [rsvg, "--format", "png", "--keep-aspect-ratio",
                 "--width", str(DRAW_SIDE), "--height", str(DRAW_SIDE)],
                input=data, capture_output=True, timeout=SVG_SECONDS, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            raise HelperError("timed_out", "drawing the SVG took too long") from None
        if done.returncode != 0:
            message = done.stderr.decode("utf-8", "replace")[:300]
            raise HelperError("exception", f"rsvg-convert could not draw the SVG: {message}")
        return done.stdout
    try:
        cairosvg: Any = importlib.import_module("cairosvg")  # optional (tier 3)
    except ImportError:
        raise HelperError(
            "unsupported",
            "nothing here draws SVG: install librsvg (rsvg-convert) or cairosvg",
        ) from None
    try:
        return bytes(cairosvg.svg2png(bytestring=data, output_width=DRAW_SIDE))
    except Exception as exc:
        raise HelperError(
            "exception", f"cairosvg could not draw the SVG: {str(exc)[:300]}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    commands = argparse.ArgumentParser(prog="pdfhelper")
    sub = commands.add_subparsers(dest="operation", required=True)
    for name in ("info", "pages", "search", "render", "draw"):
        command = sub.add_parser(name)
        command.add_argument("path")
        if name == "pages":
            command.add_argument("--pages", required=True)
            command.add_argument("--include-artifacts", action="store_true")
        if name == "search":
            command.add_argument("--query", required=True)
        if name == "render":
            command.add_argument("--page", type=int, required=True)
            command.add_argument("--region")
            command.add_argument("--dpi", type=int, default=DEFAULT_DPI)
            command.add_argument("--out")
        if name == "draw":
            command.add_argument("--page", type=int)
            command.add_argument("--region")
    args = commands.parse_args(argv)
    try:
        match args.operation:
            case "info":
                result = info(args.path)
            case "pages":
                result = pages(args.path, args.pages, artifacts=args.include_artifacts)
            case "search":
                result = search(args.path, args.query)
            case "draw":
                result = draw(args.path, args.page, args.region)
            case _:
                result = render(args.path, args.page, args.region, args.dpi, args.out)
    except HelperError as error:
        _emit({"error": {"kind": error.kind, "message": error.message}})
        return EXIT_ERROR
    _emit(result)
    return 0


def _emit(document: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    sys.exit(main())
