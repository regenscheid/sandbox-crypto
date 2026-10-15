---
name: pdf-reading
description: Reading a PDF with the PDF tools, and citing or quoting it so the citation check can confirm the quote. Use it for papers, standards and other PDFs, including pages with no text layer.
---

# Reading PDFs

The PDF tools parse a file inside the project sandbox and name it by its SHA-256. They take a path in the session workspace, or shared/... for the project's shared area. They are offered where the environment has a PDF parser.

## A good order

1. Start with `pdf_info`. It lists every page by its physical number, from 1, with the label the document prints (such as iv or 12). It also names the pages with no extractable text, gives the outline, and prints the file's SHA-256.
2. Pick sections from the outline and read them with `pdf_pages`, at most 20 pages a call, such as "3-5,9". Each line comes back numbered as p<page>:<line>.
3. Find a term with `pdf_search`. It is case-insensitive, and each hit gives the physical page, the printed label, the line number and the line.
4. Make a PNG of a page, or of a region of it, with `pdf_render` when an equation, matrix, table or figure matters. For a region, read the page with `pdf_pages` and boxes set to true: each line then shows its box, [x0, y0, x1, y1] in points from the page's bottom left, and a box that covers the lines you want, with a margin, is the region to render. Publish the PNG to keep it as evidence.

For a long paper, read bounded page ranges, and say which pages you did not read. A summary of a paper is only as good as the pages behind it.

## Page labels and page numbers

The tools count pages physically. The printed label can differ: front matter in Roman numerals, a journal's own page numbers, or a standard that numbers each part afresh. When a paper says "see page 12", it means the label. Look up the physical page in the list from `pdf_info` before calling `pdf_pages`. A citation that gives both, such as "p. 12 (physical page 14)", lets a reader find the place either way.

A line has the same number in `pdf_pages` and `pdf_search`. Running headers, footers and page numbers count in the numbering. `pdf_pages` leaves them out unless asked to include them, and says which numbers it left out, so the lines it shows can start at 2. A line marked (header/footer) is the page's furniture, not the text of the paper.

## Pages with no text

`pdf_info` names the pages without extractable text, such as scanned pages. `pdf_pages` returns no lines for them, and `pdf_search` does not look at them, so a search that found nothing has not searched those pages.

Agents have no tool to look at an image, so rendering such a page does not let you read it yourself. What helps:

- name the pages you could not read in your answer, instead of guessing what they say;
- render them, publish the PNGs, and ask the user to read the part that matters;
- run OCR in the sandbox, if the project lets you install it. OCR text is your own computation, not a line the PDF tools returned, so it cannot serve as a quote's source.

Extracted text of formulas can drop subscripts and superscripts, merge symbols, or reorder a matrix. Before relying on a formula from extracted text, derive it again or have the user compare it with a render.

## Citing a place

Cite a line as p<page>:<line>, with the physical page, and add the printed label for the reader. Name the file by the SHA-256 that every PDF result prints: the citation then stays fixed if the file is moved or renamed.

## Quoting a PDF in the ledger

A claim or result can quote the reference it cites. The consistency reviewer's citation check then looks for the quoted text in what a PDF tool returned in this session, and records whether it holds the quote and supports the entry. For the check to find the quote:

- The reference entry names the PDF in its pdf field, as an artifact revision of the project. Publish the PDF first, or find its artifact ID and revision with `find_artifacts`. The check matches the PDF by its SHA-256, so the file you read must be that revision's bytes.
- Read the quoted lines with `pdf_pages`, or with `pdf_search` for a single line, in this session before recording the quote. The check reads this session's tool results, not the file.
- Give the physical page, the first and last line, and the printed label. The quote must start in the first line and end in the last, so choose the lines tightly.
- Copy the text exactly as those lines read, joined by single spaces: the same case, punctuation, hyphens at line ends and odd characters. Only white space may differ. A quote holds at least 20 characters besides white space, and at most 2,000.

The quote is an evidence item on the claim or result, in a `ledger_change` call:

```json
{"role": "supports", "type": "source_claim", "entry": "REF-3",
 "scope": "Theorem 2 as stated, for prime fields only",
 "quote": {"source": "pdf", "page": 14, "label": "12", "first_line": 7, "last_line": 9,
           "text": "the exact words of lines 7 to 9, as pdf_pages returned them"}}
```

## When a PDF quote confirms a result

The platform cannot tell a downloaded paper from a PDF an agent made. So a PDF quote confirms a published result through the citation check only when the user has vouched for that PDF: the user checks a revision of the reference that names it, in the workspace's entry view or with the harness's ledger check command. Until then the result stays unverified, whatever the check found. Once a reference's PDF is pinned, it helps to ask the user to check that reference. A quote of an ePrint passage needs no such step (see the literature-search skill).
