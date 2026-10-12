# Living documents

Living documents are Markdown with LaTeX math that the user and agents write together (D23, D65). Their behavior is specified in [living documents](specs/living-documents.md). This page describes what is built in `packages/orchestrator` (`sandbox_science.orchestrator.documents`) and the workspace GUI. Documents carry stable block and formula IDs, immutable attributed revisions, and patches that rebase or conflict rather than overwriting newer work. The GUI supports user source and block edits, conflict resolution with retained drafts, review and application of retained patches, reverts, and moving workbook documents to project scope. Comments attach to immutable revisions, blocks, formulas or text ranges. Users can address them to a session's main agent as durable work requests. Harness `document` commands remain available for import, export, deletion and suggestion settings ([workspace](WORKSPACE.md#living-documents)).

## The control store

Documents live in one SQLite database in the harness's state directory, `<state-dir>/control.sqlite3` (D85). More than one process writes it: each harness run and the workspace server. Each change is one transaction that starts with `BEGIN IMMEDIATE`, so it holds SQLite's write lock before it reads the current revision, and commits the revision, the patch record, the operation ID and the events together, or nothing. The work of a change, parsing its text and checking it, is done before it takes the lock, against the document as it then is; under the lock, the change uses that work if the document is still as it was, and does it again otherwise, so other changes wait only for the writing. A change that waits more than 10 seconds for the lock is refused with `busy`, and can be retried under its operation ID. The database uses write-ahead logging, so readers never wait for a writer, and `synchronous=FULL`, so a change reported done survives a crash. Like the [event store](EVENT-STORE.md), it must be on a local filesystem.

| Table | Holds |
| --- | --- |
| `documents` | Each document's ID (`doc-…`), project, scope (the project, or one session's workbook), name, title, creator and the session it came from, whether agent patches wait as suggestions, its current revision, whether it is deleted, and the counters its block and span IDs come from |
| `revisions` | Every revision, append-only: its number, author, the user who accepted it if it was a suggestion, session, base and parent revisions, reason, operations, the patch it came from, its text and SHA-256, whether it is a tombstone, what was normalized, its warnings, its block map, and the profile and parser versions that made the map |
| `patches` | Every patch an agent submitted: applied, unchanged, conflicted (a rejected patch, LD-19), refused, suggested, accepted or declined, with its base, operations, author and the user's decision |
| `requests` | The first result of each change made under an operation ID, so a retry returns it |
| `events` | The changes' events (LD-39), numbered in commit order |
| `annotation_threads`, `annotation_entries` | Immutable anchor/opening author and append-only attributed create, reply, edit, resolve and reopen entries |
| `annotation_operations` | Scoped idempotent comment mutation results |
| `annotation_requests` | User-addressed requests, their session, delivery lease and queued/delivered/responded lifecycle |
| `publication_projects`, `publication_current` | Durable per-project publication cursor and current metadata for every canonical identity, including tombstones |
| `publication_events`, `publication_meta` | Bounded recent publication metadata and the completed additive bootstrap version |

Triggers refuse updates and deletions of revisions, events, annotation threads and annotation entries, as a guard against mistakes; anyone who can write the file can drop them. The schema's version is SQLite's `user_version`, 1; a database in another version, or an SQLite file that is not a control store, is refused. The workspace server reads and writes the store through the orchestrator package, as it reads the projects' records, until W5 gives the control application an API of its own.

**Events.** `document.created`; `document.revision_created`, with the revision, its base and parent, the author and the author's kind, the blocks changed, added, removed and moved, the structural blocks added, the characters of text an agent added or changed, whether delimiters were normalized, and whether it is a tombstone; `document.patch_conflicted`, with the conflicts; `document.patch_refused`, with the blocks the patch would have changed without naming them; `document.suggestion_created`; `document.suggestion_decided`; `document.settings_changed`; and `document.promoted`, recording the original session, new name, user and reason; and `annotation.created` / `annotation.changed`, committed with the thread entry and request. Each names the service that recorded it (`harness`, `harness-cli` for the user's commands, or `workspace`) and the session of the agent that made the change.

## Publication outbox prerequisite

The internal `documents.publication.Publications` reader pins complete canonical targets
for the [worker catalog](WORKER.md#document-catalog-prerequisite). Every committed revision,
including deletion, and every promotion records publication metadata in the same SQLite
transaction as its source event and operation receipt. A failed hook rolls everything back;
an exact operation retry emits no duplicate publication. The hook reads the updated SQL
head and scope rather than the caller's earlier metadata. Promotion changes the path at
the same immutable revision, without inventing a deletion of that revision.

Project publication sequence is independent of document revision and survives history
eviction. The event cache retains at most 4,096 entries per project and 65,536 globally;
current identity projections and cursors are never evicted. Content remains in immutable
revisions. These are logical metadata bounds, not a filesystem/WAL/RSS quota. Existing
schema-version-one stores receive the additive tables and a current-state baseline in
one transaction, once. Baseline entries are not lifecycle deltas: a cursor preceding
that baseline requires a complete snapshot, including an empty target for deleted-only
projects. Read-only clients never initialize or repair the store.

`plan(project, after=...)` reads the cursor and all live metadata in one consistent read.
It returns a pinned complete snapshot, a complete contiguous delta of at most 64 changes
and 64 KiB, or an already-current result. Missing/evicted history and larger deltas fall
back to the complete snapshot. Repeated changes to one document remain separate. A
target over 10,000 entries or the shared protocol's byte limits is refused in full;
worker export limits do not veto ordinary canonical edits. An already-running older
writer that bypasses the hook leaves a detectable projection mismatch: export refuses
instead of assigning its bytes to an older publication cursor.

`content(project, entry)` reads the exact pinned immutable revision, even after later
edits or deletion, and verifies its byte length and SHA-256 within that project. SQL
clips metadata, content and digest fields before returning them to Python; malformed
or oversized stored rows cannot become unbounded Python export objects. Both APIs are
synchronous and must run off asynchronous event loops.

The outbox itself performs no network delivery. Its separate manual delivery adapter
below retains pinned targets and exact storage acknowledgements. Environment file
publication, direct-edit ingestion and `ss-doc` invocation remain disabled. LD-49 to
LD-52 and their runtime fixtures remain open, including the conservative hold for
uncertain writers.

Acceptance passed 59 focused publication/store cases, independent SQL fault/retry,
retention/bootstrap and actual worker-catalog composition probes, and the combined
suite: 9,274 Python tests (43 opt-in skips), 429 web tests, strict/static/license/protocol
checks and both production builds. Corrupt 20 MiB metadata and revision rows were
refused with bounded Python export allocations; this does not measure SQLite cache
memory or prove a sandbox writer guard.

## Manual catalog delivery

`documents.delivery.CatalogDelivery` is an internal, explicitly invoked adapter.
`step(binding, client)` receives an operator-approved `WorkerClient` and immutable
`DeliveryBinding`: project, binding ID, exact positive configuration revision and
SHA-256 of the approved endpoint. It discovers no credentials, enables no clients and
has no scheduler, execution hook, agent tool or public route. Each step reads fresh
worker status and sends at most one begin/delta, page, content chunk or commit request.
The catalog-only transport caps response bodies at 8 KiB and metadata/content requests
at 64/352 KiB, refuses redirects and applies a ten-second total HTTP deadline.

A separate SQLite journal uses WAL and `synchronous=FULL`. Before the first upload it
commits the exact target, complete ordered metadata, chosen snapshot/delta mode and
original delta payload. Source advances cannot alter that pin. Immutable content is
read and verified one document at a time from the canonical revision store; this journal
stores no document content. Metadata pages and aligned chunks replay identically after
lost replies or restart. Stage expiry can replay the same pin. A reset or changed base
that makes a pending delta ambiguous holds for explicit reconciliation, preserving its
original mode and payload. A snapshot can restart on an empty worker without treating
the old local acknowledgement as current storage.

Fresh status must coherently bind project, generation, cursor, manifest, count and
bytes. Matching the exact current target can settle a lost commit reply after receipt
eviction. An older replay receipt never substitutes for actual current membership;
foreign stages, unknown ahead cursors and same-cursor different manifests hold. A
historical local acknowledgement is reported separately from freshly observed current
storage. A successful acknowledgement and pin removal commit in one transaction.

Per-binding SQL leases fence configuration changes and stale replies. Endpoint changes
require a higher operator configuration revision, retain unresolved pins/history and
reconcile fresh status. The endpoint is checked before and after network operations.
Leases never grant worker permissions. Canonical writes remain independent of delivery
failure or quota exhaustion. Future canonical revision collection must retain pinned
source revisions; current revisions are append-only.
A process that dies without releasing its lease leaves the binding busy until expiry;
the next owner reconciles the retained target rather than assuming the request failed.

Defaults bound 128 bindings, 20,000 pinned entries and 32 MiB of pinned metadata across
the journal, eight admitted steps/SQL operations and 60-second leases. `DeliveryLimits`
configures these logical bounds; they do not claim filesystem/WAL or SQLite RSS limits.
Tightened limits refuse continued admission without discarding the original pin.
All SQL uses one private thread, with bounded admission before submission. Caller
cancellation retains admitted step/transaction ownership; `aclose()` stops admission
and drains actual work before closing, even under repeated cancellation. The caller
must retain the injected client and canonical store until that drain finishes.

Focused local acceptance covers actual authenticated worker HTTP composition, lost
replies/restarts, source advances during a 600,000-byte upload, empty-target deletion only
at commit, stage expiry, worker resets, ambiguous delta holds, stale lease/endpoint
replies, corrupt pins, aggregate quotas and acknowledgement transaction failure.
No OpenShell or environment publication is exercised by this prerequisite.

## Parsing

The profile is the browser's, rule for rule ([where math begins and ends](WORKSPACE.md#where-math-begins-and-ends)): markdown-it's default dialect (markdown-it-py's `js-default`) with raw HTML off and links only to http, https and mailto addresses, which affects what parses as a link and so where math can be; markdown-it 15's code spans, which the browser runs (markdown-it-py 4.2 keeps markdown-it 14's rule, which misses a code span before an unclosed backtick once a link label that does not close was scanned, as in ``See [the note `$p$` and the `stray``, so the server runs a port of markdown-it 15.0.2's rule, credited in the file); mdit-py-plugins' YAML front matter, at the very start of the text only (the plugin also takes a list item's or quote's first line); its dollar math with the workspace's settings, with the 200-line limit on `$$` blocks applied by giving the plugin's block rule a nearer last line; LaTeX's own delimiters, `\(…\)`, `\[…\]` and bare display environments, read while parsing, a port of the browser's `mathdelimiters.ts`, with its rule for escaped brackets in a paragraph (math when padded inside both brackets, or holding a command, a brace, one of `= + < >`, or a script after its first character), read character by character as the browser reads it; and mdit-py-plugins' container for theorem-like blocks, `::: lemma (label)` to `:::`, whose kinds are those of LD-4. The parsers' versions are pinned exactly, markdown-it-py 4.2.0 and mdit-py-plugins 0.6.1, since they change in pairs with the browser's (LD-5); the profile's own version, `ss-markdown-1`, is recorded with each revision.

**Shared cases.** Three files in `web/workspace/src/lib` hold the cases both parsers must pass: `dollarmath.fixtures.json`, `mathdelimiters.fixtures.json` (PR #61's) and `documents.fixtures.json`, which gives for each document its blocks (kind, depth and lines) and its formulas (block, kind and TeX). pytest runs all three against the server's parser in the `check` job, and vitest runs the browser's parser on all three in the `web` job, the document cases through the GUI's document parser and block map ([workspace](WORKSPACE.md#living-documents)). Formulas in an image's description are not spans: the browser shows a description as text. Beyond the cases, on 2026-10-11, 15,000 random documents built from the profile's constructs, nested in lists, quotes and containers, gave the same blocks, lines and formulas in both, with the GUI's own modules; that comparison is run by hand. A comparison of the parsers' tokens, on 40,000 random documents heavy in links, images, code spans and both kinds of math and 5,000 others, found two differences, neither in a span:

- The browser's port of the dollar rules (`dollarmath.ts`) counts the backslashes before a delimiter as markdown-it-py does, reading the text's last character at index -1, but took its text to be trimmed as markdown-it-py trims a paragraph's. An image's description is not trimmed, so in `![  $d$\\](https://e.org)`, or with `\[d^2\]` in its place, the server read a formula in the description and the browser did not. The browser now reads a description as written, and shared cases pin it.
- markdown-it trims a table's lines with JavaScript's white space, markdown-it-py with Python's: a header line that begins or ends with a byte-order mark (U+FEFF), or with one of the separators U+001C to U+001F or U+0085, makes a table on one side and a paragraph on the other. A byte-order mark at the start of a document's text is dropped when it is saved, which is where one comes from; elsewhere, the GUI shows the difference as a parser mismatch (LD-9). Porting markdown-it-py's table rule with JavaScript's trim would remove it.

**Positions.** markdown-it records each block's lines, not where its text starts on them, and nothing about where an inline formula sits. The parser wraps markdown-it's rules: each block rule records, for each line of each block it makes, where the block's own text begins after the markers and indentation of the blocks around it, and the content of each paragraph, heading and table cell is mapped back to the text it was cut from. The mapping is checked against the content markdown-it built, and dropped where they differ, so a wrong position is never used; an operation on a formula whose place is unknown asks for its block to be replaced instead.

**Limits.** A revision holds at most 1 MiB of UTF-8, with blocks nested at most 32 deep, at most 10,000 blocks and 20,000 formulas; text that is not valid Unicode or holds a NUL is refused. Line breaks are stored as `\n`, as markdown-it reads them, so positions agree, and a byte-order mark at the start of the text is dropped. A change parses at most 16 MiB of text in all (`blocks.MAX_PARSED`): a patch's text is parsed again after each operation, so a patch to a text of n characters has at most 16 Mi / n operations (16 on a 1 MiB document) and is refused beyond that, and the parses that check delimiter conversions take from the same budget. Front matter is read with PyYAML's safe loader, without aliases, up to 16 KiB; what it cannot read as a title and macros is a warning, and the text stays as written. Its macros are passed to KaTeX, which expands them as it renders, and a few short ones that use one another can expand into a great many characters. So at most 200 are read, each defined in at most 1,000 characters, and a macro is left out, with a warning, when its definition defines another (as `\def` does), uses one of its arguments more than once (which would let it multiply what it is given), expands into itself, or expands to more than 2,000 characters with the macros it uses expanded. The GUI checks the same, and bounds what each formula's macros expand to in all ([workspace](WORKSPACE.md#living-documents)).

## Blocks and IDs

A block is a unit the parser finds: front matter, heading, paragraph, list, list item, quote, code block, display equation, table, theorem-like container or thematic break. Its own text is its lines without the markers and indentation of the blocks around it and without trailing blank lines; a container's includes its children's, so a container changes when a child does (LD-F5). The SHA-256 of the own text tells whether a block changed. Structural blocks (LD-40) are headings, tables, theorem-like containers and figures, a figure being a paragraph that holds one image and nothing else. A math span is one formula: inline, displayed within a paragraph, or a display equation.

Only the server assigns IDs: `b1`, `b2`, … for blocks and `m1`, `m2`, … for spans, per document, never reused. They carry from one revision to the next by LD-7's rules, in order. The spec leaves some choices open, settled here:

- **Equal blocks** (rule 2) are compared by kind and text with whitespace collapsed. They are paired along their longest common order first, then in document order, so that deleting one of two identical paragraphs removes its own ID, not the other's. The equal ones at the start and at the end pair first; between them, the longest common order is looked for only when that compares at most 250,000 pairs, and beyond it they pair in document order. Spans in a block pair the same way.
- **Similar blocks** (rule 4) count words, lowercased, and TeX commands. In each gap between blocks paired in the same order, the best-scoring pairs of the same kind are taken first; ties go in document order. A gap with more than 40,000 pairs to compare compares each block only with the 200 parent blocks nearest its place in the gap.
- **Named blocks** (rule 1): an operation that replaces a block, moves it, or rewrites text or a formula in it, names the region of the text it wrote, and the first block of the named kind starting there keeps the ID.

A block is `moved` when it is not in the longest run of blocks the two revisions share in the same order, or has another parent; `changed` when its text hash differs, whether or not it moved. A span is `changed` when its TeX or its kind (inline or displayed) changed.

## Changes

**Creating and saving.** A document is created with a name, unique among the live documents of its scope, a title and its first text. An imported whole text must come from the current revision. GUI source edits name their base and reconcile by the blocks they changed from it (LD-13), as described below. Text identical to the current revision makes no revision (LD-11). Deleting a document adds a tombstone revision with no text; its history remains, its name is free again, and an agent may delete only the documents its own session created (LD-12).

**User editing** (LD-13, LD-14). A source save names its immutable base revision, reason and operation ID. The server derives named operations from the source: equal top-level blocks first, then unmatched blocks of the same kind with shared words or TeX commands. This global comparison lets a paragraph keep its identity when a table moves across it while its second formula changes (LD-F1). It compares at most 40,000 pairs of roots, caches their token counts, and produces at most 64 operations within one shared ordinary parse budget for derivation and reconciliation. Larger or ambiguous source transformations ask for smaller block edits. Nested source changes conservatively name their top-level container, so a concurrent change anywhere in that container conflicts. A block edit can name a nested block directly.

The ordinary stale-base and unnamed-block guards apply to user operations. Source saves from the current revision preserve the exact separating whitespace after newline and delimiter normalization. Rebasing preserves the newer document's untouched blocks and lays out inserted or replaced text using the patch engine. Conflicts retain the operations in the store and report the current text and the authors who changed it. Parsing, identity matching and patch preparation run before the write lock; if a source edit's prepared head changed before commit, it returns `busy` for a safe retry instead of doing the expensive preparation under the lock.

The GUI retains the base and draft in memory across views and in this browser tab's session storage across reloads. If storage fails, the memory draft remains and closing the tab warns. It disables input while saving. Every mutation retains an operation ID keyed by its exact payload and document scope through uncertain HTTP failures and reloads; a parsed definitive result releases it. A block conflict can be deliberately resolved against the current base for that named block. Source resolution starts from the current document, keeping the old draft separately for copying desired changes; it never retries an old whole source against a newer base automatically. These are plain Markdown editors, without rich-text controls.

**Retained patches** (LD-19). History previews a conflicted, refused or suggested patch's retained operations and the current and proposed text. Missing named blocks or unnamed changes make the preview inapplicable, with the operations still readable. Explicit application requires the exact revision shown in the preview; a newer head returns a conflict requiring another review. The existing named-operation guards remain in force. Applied revisions keep the original author and record the accepting user, including for rejected agent patches. Suggestions can be accepted through this preview; declining them remains a harness command.

**Reverts and scope** (LD-1). From an earlier live revision the user can append a revision restoring that text with its original block and math IDs, while ID allocation counters continue forward. Restoring identical text with different identities still creates a revision. The current head is required, so a stale view cannot undo a newer revision silently. Restoring a deleted document is not supported yet. A workbook document can move to project scope with a project-unique name and the same ID and revisions; a separate attributed event records the scope change, which makes no text revision.

**Patches.** A patch names its base revision, an operation ID, a reason and its operations, which name blocks and spans by the base revision's IDs: `replace` a block with Markdown; `insert` Markdown before or after a block, or at the start (after the front matter) or end; `move` a block before or after another; `delete` a block; `replace_math`, a formula's TeX; and `replace_text`, a quote that occurs exactly once in a block's text, overlapping occurrences counted. Markdown is written as if at the top level; the operation lays it inside the lists and quotes around its place, with their markers on each line, and keeps it apart from the blocks beside it with blank lines. A replacement is kept apart from a line of text right before or after it the same way, but not from a list item that follows it, which ends any block before it; a blank line there would make a tight list loose. Operations apply in turn, each to the text the one before left, and the text is parsed again after each. A deleted block takes with it a blank line of its own container, never one that keeps two blocks apart, and leaves a blank line where text came right before and after it. Deleting the first block of a list item gives the marker's line to the next block, or leaves the marker on its own line where the next block is a table, a thematic break or indented past the item's content. A move takes the block out, then puts it next to its anchor as the text then stands, so a block can move out of the block that holds it, but not when it is all of its anchor. A replaced or deleted block keeps its ID only by rule 1, so an equal block elsewhere never takes it. Some patches cannot keep the blocks around them, whatever the text, and LD-18 refuses them: deleting the one block between two lists with the same marker joins them; deleting the only block of nested list items leaves markers such as `- - -`, a thematic break; and a block indented past its container's content (up to three spaces) joins a list it is moved or inserted right after. A seeded test applies every operation to random documents built from the profile's constructs to check the rest; over 2,000 more seeds than CI runs, these were its only refusals.

- **Stale bases** (LD-17). When revisions came after the base, the patch conflicts if one of them changed or removed a block or formula it names, moved a block it moves, or put a block where it inserts or moves one; containers count as changed when their children did. A conflict writes nothing but the rejected patch and its event, and names the current revision and each conflicting block or formula with its text at the base and now, and the revisions and authors that changed it. Otherwise the patch applies to the current revision by ID, and the new revision records both its base and its parent.
- **Blocks it does not name** (LD-18). After the operations, every block of the revision the patch applied to must be there with the same text, kind, attributes and formulas, inside the same block, except the blocks it rewrites, replaces or deletes with everything inside them, the blocks that contain any block it names, and the blocks it moves, which may change container. Its code spans, links and images must be where they were in its text, so a link reference definition that makes text a link, or a link text, changes the blocks that use it. Otherwise the patch is refused with `patch_changes_unnamed_blocks` and nothing but its record and event is written: so are an unclosed container that takes in the blocks after it, and a closing marker that leaves blocks outside their container. When a container of a named block loses its ID by rule 4, as when most of its words were replaced, the blocks inside it are in the container of the same kind that took its place, which is not a change of theirs.
- **Suggestions** (LD-20). A document can be set to take agent patches as suggestions, as open question 4 recommends; patches apply directly by default. A suggestion is checked as a patch is, then waits. The user's acceptance applies it as LD-17 says, attributed to its author with the acceptance recorded; a rejection writes nothing.
- **Retries.** A change made again under the same operation ID returns its first result, marked `replayed`; the same ID with another change is refused. Operation IDs belong to the session for agents, so a retry from a new run of the same session finds the first result, and to the user for the user's changes.

**Delimiters on save** (LD-41, LD-42). Text a change brings in is rewritten as it is saved so that its formulas use dollars: the whole text of a new document or a whole-text save, and only what each operation wrote for a patch (a move writes nothing new), so blocks a patch does not name are never touched. `\(…\)` becomes `$…$` without the spaces inside; `\[…\]` and `\begin{displaymath}…` become `$$…$$` in the same place, with a blank line before a block that followed a paragraph, since a `$$` line cannot end one; a display environment written outside math goes inside `$$` lines, or between `$$` within its paragraph; and an empty `\( \)` is removed, outside links and images. An empty `\[ \]` stays: Markdown writers escape brackets that way (`- \[ \] to do`), and neither parser reads it as math. The text is then parsed again, and a conversion that would change its blocks, or which formulas it has, their kinds or their TeX (with whitespace collapsed), is left out. All are tried together first; when that changes the parse, each half is tried in turn and halved again until the conversions that change it are found, within 32 parses and the change's budget; a multi-line `\[…\]` in a quote is one, since a `$$` block keeps the quote's markers in its TeX. The revision records what was converted and what was kept, and why.

One case awaits the user's decision. LD-41 drafts `$$…$$` for a `\(…\)` that single dollars would make ambiguous elsewhere: one holding a dollar sign or a line break, or followed by a digit. In this dialect `$$…$$` within a paragraph is displayed math, so the formula would leave its sentence. Such a formula is kept as written, which both parsers read as inline math, as recommended; `normalize.AMBIGUOUS_INLINE` switches to `$$…$$` ([open question 15](specs/living-documents.md#open-questions)).

**Warnings** (LD-30). Each revision's new and changed blocks are checked for math KaTeX does not see, and the findings are kept on the revision and returned to the patching agent: `unbalanced_dollar`, a `$` that opens no formula when a letter, backslash or other symbol follows it, or a stray `$$` (a `$` before a digit or a space is left alone, as amounts are written); `definition_in_math`, `\def`, `\gdef`, `\newcommand` and the like inside a formula, which the GUI shows as written, where LD-28 has it apply the definition to that formula alone, and which TeX may make global in an export; `display_swallows_text`, a displayed formula holding a `$` or a line that reads as Markdown or prose (a `$$` block in a quote is read without the quote's markers, which its TeX keeps); and `front_matter`. They are heuristics, and change nothing. KaTeX's own check, which makes `math` findings (LD-53 to LD-55, D82), is a later slice; `checks.math_check` is where it plugs in.

## The agents' document tool

With `--state-dir`, the harness gives the agent five tools over the project's documents: `document_list`; `document_read`, which shows a revision as blocks in order, each with its ID, kind and own text (containers with their opening line or marker instead, so text is not repeated), each formula's ID and TeX (a display equation's in a quote without the quote's markers, which the parser keeps in its TeX; `replace_math` takes it without them too), and the front matter's macros, in parts when it is long; `document_create`, for the project or the session's workbook; `document_patch`; and `document_delete`. Every session of the project reads and patches every document, under the project's grant (LD-1). A name finds the session's workbook document first, then the project's. Results are JSON; a conflict and a refusal come back as errors, with the conflicting blocks' texts or the blocks the patch would have changed. Unknown arguments are refused, so no argument can name an author: each change is attributed to the run, with its session, role (`main`), the model the gateway said answered, the alias asked for, and the tool call (LD-10, RA-16). A patch without an operation ID of its own uses the tool call's, so a run repeated under its run ID gets the first results.

**Notices** (LD-15). The tool remembers, for each document the run has read, created or patched, the revision it read or patched from. After each tool call, the run's next safe point, the harness looks for newer revisions of them made by anyone else, and adds a notice to that call's result (`platform_notices`) naming the revisions, their authors and the blocks they changed, once for each.

## The user's commands

Until the GUI can edit, the user works on documents with the harness's command line, on the state directory:

```bash
sandbox-science-harness document import --state-dir state --project p1 --name ff ff.md
sandbox-science-harness document export ff --output ff.md --state-dir state --project p1
sandbox-science-harness document import --base 3 --state-dir state --project p1 --name ff ff.md
sandbox-science-harness document list --state-dir state --project p1
sandbox-science-harness document show ff --state-dir state --project p1
sandbox-science-harness document history ff --state-dir state --project p1
sandbox-science-harness document suggestions ff on --state-dir state --project p1
sandbox-science-harness document decide ff patch-… accept --state-dir state --project p1
sandbox-science-harness document delete ff --reason "…" --state-dir state --project p1
```

`import` creates the document, or saves the file as its next revision, attributed to an import the user started; `--session` makes it a workbook document. A file saved over an existing document must name the revision it was made from with `--base`, which must be the current one, so an import never undoes revisions made since; `export` writes a revision's text to a file and says which revision it is. `show` prints the blocks with their IDs, `history` each revision with its author, reason and changed blocks, each block's revisions, and the patches with their states (LD-11).

## Comments and work requests

**Anchors** (LD-21, RA-21 to RA-23). An anchor names an immutable revision and stable block ID, optionally a formula ID in that block or a Unicode code-point range in its editable source. The server derives its original quote and surrounding context; callers cannot supply another document's formula or their own quote/context. A range is at most 2,048 characters. Block and formula quotes keep a 2,048-character excerpt and the complete text's hash, with an explicit excerpt marker. They never search other blocks to reattach.

The GUI projects an anchor onto the displayed revision. Unchanged text is `current`; an unchanged anchor whose block changed position is `moved`; changed text is `stale`, showing the original quote and a bounded current excerpt; a missing block or formula is `orphaned`. A range remains current in a changed block only when its original quote occurs exactly once there, counting overlapping occurrences. Original revision, IDs and context stay immutable. LD-F7 covers unrelated formula changes, moving an equation, changing its TeX, rewriting the quoted paragraph and deleting the equation.

**Threads** (RA-25). Comment bodies are Markdown with LaTeX, rendered with the displayed document's bounded front-matter macros. Replies and edits append entries; earlier bodies remain readable. A change names the expected thread revision, so concurrent replies or resolves return a conflict rather than hiding another entry. The server derives user attribution in the workspace and agent session/run/role/model/tool-call attribution in the harness. A user can resolve or reopen a thread; agents resolve or edit only threads their session and role opened. User threads remain open after an agent replies. Exact payload/document/actor operation IDs make uncertain retries safe, and GUI comment/reply drafts use separate keys from source/block drafts.

Each body holds at most 8,192 characters and 16 KiB of UTF-8; non-whitespace C0 control characters are refused, while line breaks and tabs are preserved. Thread reads paginate at 50 entries or 256 KiB of projected entries; list reads paginate at 10 threads (20 maximum internally) and 1 MiB. Neither cursor skips an unseen entry or thread. A large reply scope report keeps its full block lists in the store; public projections show their first 100 IDs, explicit total/truncation fields and a link to the revision. Agent reads further bound body excerpts and the whole tool result.

**Addressed requests** (LD-22 to LD-24, RA-24). Only a user's explicitly addressed entry queues work. The target must be a recorded session in the same project. An agent-addressed entry queues nothing. The GUI defaults a workbook document's target to its own session and otherwise asks the user to select one. Only the comment body is the user instruction; the immutable anchor, quoted document text, sources and other comments are explicitly untrusted data. No policy, worker context, capability or approval boundary changes.

The main agent receives pending requests at the start of a manually started run and at subsequent model safe points, including before accepting an answer when another model turn remains. A batch is at most 20,000 JSON characters and a run receives at most 128 requests. Full user bodies are preserved; oversized escaped quoted context is reduced to retrievable anchor identity. Main runs lease their bounded batches for 120 seconds, renewed every 30 seconds without model calls. Failed turns release their leases; requests not actually sent to a model are released at run exit. Other roles and sessions do not receive them.

A successfully received request ID goes into the run record. It becomes `delivered` only after the immutable transcript has been fsynced into the project store, then acknowledged in the control store. A crash between those two stores can replay the stable request ID after its lease expires; delivery is at least once across that crash window, once per successful run. Saving a run unsuccessfully cannot consume a request. No idle-session scheduler or automatic wake is built: idle requests stay queued until the next manually started main run. Waiting tools receive them when they return to a model safe point.

Agents answer through `document_reply`, naming the request ID and, if they changed the document, a revision made by that run/session/role. The server reports changed blocks, the anchor identity, and changes outside the anchor. Where it cannot prove a change was wholly inside the anchor, including nested multiline range changes without raw-source provenance, it discloses the block as outside or uncertain. Formula changes compare source outside the named span, so another formula or surrounding prose is disclosed. A response marks the request `responded` and leaves the thread open. `document_comments`, `document_comment`, `document_reply` and `document_resolve` use the existing document tool boundary. LD-F8's scripted HTTP fixture demonstrates the request/quote separation, run-attributed formula reply and unchanged worker context/refusal boundary; automatic idle wake and a real escalated OpenShell run are outside that fixture.

These annotations currently cover living documents. Other artifact types' PDF/image/table/notebook anchors and annotation export sidecars remain future work.

## Checkpoint triggers

Consistency reviews are not built yet. `reviews.CheckpointWindow` is the part of CR-10 that documents feed: it counts a session's agent revisions by the characters the agents wrote and the structural blocks they added, never as artifact revisions, and a checkpoint review is due once the minimum interval has passed and the agents wrote 2,000 characters or added a structural block (CR-10 as amended, LD-40). The user's own revisions count toward no review. Its snapshot is the revisions since the last dispatch.

## Measured

On babbage, documents of repeated sections, each a heading and a paragraph with three formulas:

| Document | Parse | Create | Patch | Patch from 10 revisions back |
| --- | --- | --- | --- | --- |
| 17 KiB, 200 blocks, 600 formulas | 11 ms | 12 ms | 16 ms | 24 ms |
| 85 KiB, 1,000 blocks, 3,000 formulas | 43 ms | 54 ms | 89 ms | 135 ms |
| 344 KiB, 4,000 blocks, 12,000 formulas | 260 ms | 275 ms | 541 ms | 880 ms |

A patch parses the text again after each operation and carries the IDs over; one from an older base also reads the block maps of the revisions after it. All of that happens before the write lock is taken. On a 1,012 KiB document of 5,700 blocks, a patch of one operation took 0.52 s, and one of 16 operations, the most a patch to it may have, 7.8 s.

## Not built yet

- The project's macros, beneath a document's (LD-28).
- Rich-text editing, automatic source conflict merging, declining suggestions in the GUI, and restoring deleted documents.
- Automatic idle-session wake/turn dispatch for addressed comments (part of LD-22), annotation sidecar export and anchors for artifact types other than living documents (RA-21/RA-23), and review findings on blocks (LD-25).
- References, embedded artifacts and ledger bindings (LD-31 to LD-34), and exports (LD-35 to LD-38).
- Document files in every environment, direct edits of them, and the `ss-doc` helper (LD-3, LD-49 to LD-52).
- Math repairs proposed into a document (LD-48) and math findings from KaTeX's check on save (LD-53 to LD-55, D82).
- The consistency review itself, which the trigger window will feed.
