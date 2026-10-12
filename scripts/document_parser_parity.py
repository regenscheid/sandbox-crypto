# SPDX-License-Identifier: MIT
"""Project the server's document parse for the browser's differential tests.

Input is a JSON list of whole source texts on stdin; output preserves every block's
parent, source extent and text, and every formula's exact code-point offsets. This
test utility runs no model, worker or service.
"""

from __future__ import annotations

import json
import sys

from sandbox_science.orchestrator.documents.blocks import parse
from sandbox_science.orchestrator.documents.identity import Allocator, fresh


def main() -> None:
    """Read a bounded corpus and emit its complete parser projections."""
    sources: object = json.load(sys.stdin)
    if (
        not isinstance(sources, list)
        or len(sources) > 4096
        or any(not isinstance(text, str) or len(text) > 20000 for text in sources)
    ):
        raise ValueError("expected at most 4096 source texts, each at most 20000 characters")
    results: list[object] = []
    for text in sources:
        parsed = parse(text)
        mapped = fresh(parsed, Allocator(0, 0))
        results.append(
            {
                "blocks": [
                    {
                        "id": mapped.block_ids[b.index],
                        "kind": b.kind,
                        "parent": b.parent,
                        "depth": b.depth,
                        "lines": b.lines,
                        "start": b.start,
                        "end": b.end,
                        "firsts": b.firsts,
                        "text": b.text,
                    }
                    for b in parsed.blocks
                ],
                "spans": [
                    {
                        "id": mapped.span_ids[s.index],
                        "block": s.block,
                        "kind": s.kind,
                        "tex": s.tex,
                        "open": s.open,
                        "close": s.close,
                        "positions": [s.open_start, s.tex_start, s.tex_end, s.close_end],
                    }
                    for s in parsed.spans
                ],
            }
        )
    json.dump(results, sys.stdout, ensure_ascii=True)


if __name__ == "__main__":
    main()
