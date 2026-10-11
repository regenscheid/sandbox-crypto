# SPDX-License-Identifier: MIT
"""Living documents: Markdown with LaTeX math, written by the user and agents together.

The server side of W4's first slice of living documents (docs/specs/living-documents.md):

- ``parser`` and ``blocks``: the Markdown profile, parsed as the browser parses it, into
  blocks and math spans with their places in the text (LD-4 to LD-6, LD-40);
- ``identity``: block and span IDs carried across revisions, and their statuses (LD-7,
  LD-8);
- ``normalize`` and ``checks``: LaTeX's other delimiters saved in the dollar form, and
  warnings on math KaTeX does not see (LD-30, LD-41, LD-42);
- ``operations`` and ``engine``: patches, rebased or refused (LD-16 to LD-18);
- ``store``: documents, revisions, patches and their events in the control store's
  SQLite database (LD-1, LD-2, LD-10 to LD-12, LD-19, LD-20, LD-39);
- ``tool``: the agents' document tool, and the notices of others' changes (LD-15).
"""

from .blocks import Block, Parsed, Span, parse
from .errors import DocumentError
from .identity import Mapped
from .models import USER, Author, ChangeResult, DocumentInfo, PatchInfo, Revision, RevisionInfo
from .store import FILE_NAME, DocumentStore, store_path
from .tool import DocumentTool, with_notices

__all__ = [
    "FILE_NAME",
    "USER",
    "Author",
    "Block",
    "ChangeResult",
    "DocumentError",
    "DocumentInfo",
    "DocumentStore",
    "DocumentTool",
    "Mapped",
    "Parsed",
    "PatchInfo",
    "Revision",
    "RevisionInfo",
    "Span",
    "parse",
    "store_path",
    "with_notices",
]
