# SPDX-License-Identifier: MIT
"""Living documents: Markdown with LaTeX math, written by the user and agents together.

The server side of living documents (docs/specs/living-documents.md; as built in
docs/LIVING-DOCUMENTS.md). Each module's docstring says what it holds;
by area:

- parsing: ``parser``, ``table_rule``, ``blocks``, ``frontmatter``, ``normalize`` and
  ``checks``;
- identity and changes: ``identity``, ``operations``, ``engine`` and ``source``;
- storage: ``store``, ``models``, ``errors`` and ``project_macros``, in the control
  store's SQLite database;
- comments: ``annotations``, ``comment_bodies`` and ``annotation_tools``;
- math checks: ``mathqueue``, ``mathjobs``, ``mathcontroller``, ``comment_math`` and
  ``response_math``;
- the agents' document tool: ``tool``.
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
