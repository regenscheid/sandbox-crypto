# SPDX-License-Identifier: MIT
"""The library server's results as its tools write them (crypto-research-mcp,
``library_mcp/tools.py`` on main, 2026-11-24: ``_label``, ``_header``, ``read_paper``,
``search_in_paper`` and ``reading.marked``), with made-up papers, for the tests of quote
sources and the read register."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

READ: str = "library-mcp_read_paper"
SEARCH: str = "library-mcp_search_in_paper"
REVISION: str = "3fa2c1d0e9b8"
"""The first 12 hexadecimal characters of the PDF's SHA-256, as ``_header`` prints it."""
WEB: str = "https://library.example.test"


def header(
    work: str,
    *,
    pages: int = 12,
    revision: str = REVISION,
    tier: str = "quality",
    converter: str = "marker",
    more: str = "",
    withdrawn: str | None = None,
) -> str:
    """``_header``: the work's label line (``_label``, with ``withdrawn.note``) and its
    rendition line; ``more`` stands for fields a later server may add to that line."""
    note = f" — WITHDRAWN ({withdrawn}; its results may not hold)" if withdrawn else ""
    return (
        f"{work} — A made-up paper on twists (2024){note}\n"
        f"{tier} rendition by {converter}, {pages} pages, revision {revision}{more}\n"
    )


def math_note(reasons: str = "unbalanced $") -> str:
    """What ``reading.marked`` adds after a block whose mathematics the conversion lost."""
    return (
        f"\n\n[The mathematics above is unreliable in this text ({reasons}); read it in the PDF.]"
    )


def read(
    work: str,
    start: int,
    texts: Sequence[str],
    *,
    total: int = 12,
    identifier: str | None = None,
    corrected: Mapping[int, str] = {},
    notes: Mapping[int, Sequence[tuple[str, str, str]]] = {},
    revision: str = REVISION,
    more: str = "",
    withdrawn: str | None = None,
) -> str:
    """``read_paper``'s answer for pages ``start`` on, each text a page's: a corrected
    page's heading says so, and each page's notes (author, quote, text) follow its text,
    as the server lays them out."""
    end = start + len(texts) - 1
    chunks: list[str] = []
    for offset, text in enumerate(texts):
        n = start + offset
        said = f" (corrected by a reader on {corrected[n]})" if n in corrected else ""
        body = text or "(no text on this page in this rendition)"
        noted = ""
        if n in notes:
            noted = "\n\n" + "\n".join(
                f"[Note by {author or 'a reader'}" + (f" on “{quote}”" if quote else "")
                + f": {' '.join(note.split())}]"
                for author, quote, note in notes[n]
            )  # fmt: skip
        chunks.append(f"## Page {n}{said}\n\n{body}" + noted)
    name = identifier or work
    footer = (
        "End of text."
        if end == total
        else f'Next: read_paper("{name}", start_page={end + 1}, page_count={len(texts)}).'
    )
    link = f"{WEB}/read?id={work}#p{start}"
    return (
        header(work, pages=total, revision=revision, more=more, withdrawn=withdrawn)
        + f"Physical pages {start}-{end} of {total}. In the web reader: {link}"
        + "\n\n"
        + "\n\n".join(chunks)
        + "\n\n"
        + footer
    )


def search(work: str, phrase: str, found: Sequence[tuple[int, str]]) -> str:
    """``search_in_paper``'s answer: each passage under its physical page."""
    if not found:
        return header(work) + (
            f"No passage matching {phrase!r}. This matches a literal phrase within a line, not "
            f'a question. The text is held (12 pages): read_paper("{work}", start_page=1).'
        )
    passages = [f"Physical page {page}\n" + text.strip() for page, text in found]
    return (
        header(work) + f"{len(found)} passage(s) matching {phrase!r}:\n\n" + "\n\n".join(passages)
    )


def not_held(work: str) -> str:
    """``read_paper``'s answer for a paper the library does not hold yet."""
    return (
        f"{work} — A made-up paper on twists (2024)\nno text held (holdings: known)\n"
        "Not held; fetching now through the free routes — read again in a minute."
    )
