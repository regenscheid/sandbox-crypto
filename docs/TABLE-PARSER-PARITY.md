# Unicode table parser parity

New parses use `ss-markdown-2`. Its table rule is a port of the pinned
markdown-it-py 4.2.0 rule with JavaScript's `String.prototype.trim` at row and cell
boundaries, matching the browser's pinned markdown-it 15.0.3. The port retains
upstream line maps, alignment, escaped pipes, termination rules and the limit of
65,536 autocompleted empty cells. Both upstream MIT notices are retained in
[table_rule.py](../packages/orchestrator/src/sandbox_science/orchestrator/documents/table_rule.py).

JavaScript trims U+FEFF, which Python does not trim. Python trims U+001C through
U+001F and U+0085, which JavaScript does not trim. These differences could make one
parser find a table and the other a paragraph, change a table's end, or shift a
cell's formula offsets. Table source provenance uses the same JavaScript trim
as the corrected rule. Source offsets remain code points into the exact saved text;
an escaped pipe keeps its original source location even though its backslash is
absent from the inline token's content.

This changes no browser renderer or save normalization. In particular, save still
normalizes CRLF/CR to LF and removes one leading byte-order mark, while internal
U+FEFF and other Unicode characters remain in the saved source. Other Markdown and
math rules keep their existing whitespace definitions. Profile 2 also preserves
the browser math rules' existing escape boundary in a cell that retains Python
whitespace: an opening delimiter reads backward from the first non-Python-space
character to the last non-Python-space character. This can suppress an apparent
formula when that last character is a backslash. Image descriptions keep their
existing untrimmed boundary and remain excluded from document math anchors.

## Immutable history and identities

Saved `ss-markdown-1` block maps remain readable with the original pinned table
rule and Python table provenance trim. Readers select the known stored profile
before attaching its IDs; unknown profiles remain refused. Neither historical
text nor maps, IDs, parser metadata or authors are migrated. The browser still
reports an LD-9 mismatch for a historical Unicode table where its parse disagrees
with that old map, and does not guess an ID.

An ordinary new edit uses profile 2. Unaffected blocks and formulas retain their
IDs through the existing identity matching. A paragraph/table disagreement gets
new identities. Explicit user revert keeps its existing contract: restore the
target's text and exact block/math IDs, including its profile, and record that
actual profile in the new revision's parser metadata. A later ordinary edit again
uses profile 2. The same saved-profile reader applies to immutable comment and
response bodies; their document versus ordinary-Markdown mode remains unchanged.

## Validation boundary

The shared [Unicode table fixtures](../web/workspace/src/lib/tables.fixtures.json)
cover all six differing code points at header, cell, body-row and blank-row edges,
plus quoted/list tables, astral prefixes, escaped pipes, alignment, missing cells,
invalid delimiters, paragraph/reference interruption, adjacent code and math escape
boundaries in retained cell whitespace. The server pins exact formula source
offsets; the browser pins the same blocks and TeX, including historical mismatch
protection.

A deterministic differential test runs the actual pinned browser and server
parsers over those 52 cases and 1,024 generated whole documents (1,076 total). It compares every
block's kind, parent, depth, lines, own text, first offsets and source extent, every
formula's kind and TeX, formula delimiter/source offsets in the generated corpus,
and browser attachment of server block/math IDs. It includes Unicode row/cell
edges, lists, quotes, theorem containers, code, ordinary prose and astral text.
This is a bounded regression corpus, not an exhaustive proof of parser equivalence.

The tests cover saved-history reads, ordinary edits and exact revert under both
profiles; immutable snapshots are never migrated.

Run the focused checks from the repository root:

```sh
uv run pytest packages/orchestrator/tests/test_document_parser.py packages/orchestrator/tests/test_table_profiles.py packages/orchestrator/tests/test_document_identity.py
npm --prefix web/workspace test -- src/lib/blockmap.test.ts server/table-parity.test.mjs
```

The browser differential test invokes the bounded local
[server projection utility](../scripts/document_parser_parity.py) through `uv`.
It requires the existing locked Python and npm dependencies; web CI sets up its
existing pinned uv/Python action before running npm tests. No model, worker,
service, VM or network peer participates.
