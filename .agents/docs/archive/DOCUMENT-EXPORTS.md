# Document export inputs

> **Archived.** This code was removed from main; it is preserved on the tag `archive/document-sync-2026-10`. See the [refactoring plan](../../refactor-plan/PLAN.md), decision 2.

Living documents remain Markdown containing LaTeX mathematics. The internal
export-input snapshot is a prerequisite for LD-35–38, not a `.tex` editor,
converter, PDF exporter or worker-staging implementation. The existing
`document export` CLI still writes only the stored Markdown source.

## Internal API and pins

`documents.exports.ExportInputs(documents, projects)` accepts an already
authenticated, trusted USER or ordinary MAIN/delegated `Author`. It independently
checks project existence and agent session membership; it grants no worker,
filesystem or policy authority. Workers, imports, platform actors and reviewers
cannot prepare snapshots. This API has no public route, tool or GUI control.

`prepare(project, {document_id, revision?, operation_id}, author)` returns a
`contracts.document_exports.ExportInputReceipt`. An omitted revision selects
the current document head and current project macros in one control-store read
transaction. An explicit revision selects that immutable source and its saved
project-macro revision. Missing legacy historical macro pins remain unknown;
today's settings are never substituted for historical proof. Document macros
override project macros using the existing bounded resolver.

The snapshot retains exact stored UTF-8 Markdown, its revision/hash and workbook
scope, the raw saved parser-metadata JSON/hash, and the saved block-map byte count
and hash when boundedly proved. Supported profiles 1 and 2 are read under their
own saved rules. Each proved math span retains its stable block/span IDs, kind,
parser-original TeX body/hash, delimiters and available original-source offsets
(code points). Missing offsets remain absent. The original stored Markdown also
retains container/list/quote markers; offsets do not imply that parser TeX is a
contiguous raw-source substring in every container. No display repair, Unicode
fallback, renderer result or model proposal changes these inputs.

Raw source, map and setting selection happen together. Markdown parsing and
snapshot assembly run after the read transaction closes. A short write
transaction rechecks replay and quotas, then appends the immutable snapshot and
operation receipt together. Concurrent later source/settings changes do not
repin a completed read selection. An operation namespace is project + trusted
USER identity or agent session + operation ID. Equal retries retain the original
snapshot, requester and settings across new runs/restarts; changed payloads
conflict. Read/replay reconstructs derived spans, macros, profile and held status
from canonical source/map plus independently retained original selection pins,
so a self-consistent corrupt projection/checksum cannot substitute export text.
`get(project, snapshot_id)` is project scoped and works on readonly stores;
missing legacy snapshot tables return not found without migrating them.

## Held inputs and bounds

`prepared` means this internal input snapshot is proved under the supported
profile and dependency policy. It does **not** mean conversion was attempted,
resources were resolved, TeX is safe to execute, or an export completed.
`resources_resolved` is always false in this slice.

Unsupported profiles, over-budget maps, unknown historical macros, incomplete
macro resolution, deleted source selection and unsupported resource syntax
produce immutable `held` snapshots with fixed reasons. Missing parser evidence
cannot establish effective document overrides, so context and span completeness
remain explicitly unknown. Source/map corruption that contradicts its hashes or
types is refused rather than recorded as proved data.

Resource resolution (LD-31–34) is not implemented. The conservative lexical
gate holds Markdown links/images/reference definitions, common project/artifact
reference schemes, raw HTML and enumerated file/dependency/dynamic TeX controls
in the source or effective definitions. Even harmless hyperlinks and code
examples containing that syntax are held. TeX control-word boundaries stop before
digits/underscores, including `\input0`, `\openin1` and `\write18`. This is an
explicit limited dependency detector, not an interpreter or general TeX security
certification. Unknown TeX effects need sandbox enforcement in the later worker
execution slice; a prepared snapshot grants no execution authority. Unsupported
references are never silently dropped or claimed resolved.

Limits are logical retained-data budgets, not filesystem quotas:

| Input or retained collection | Limit |
| --- | --- |
| UTF-8 source | 1 MiB |
| Saved map read/hash/parse | 4 MiB; larger maps produce a compact held pin with unknown hash/profile |
| Saved parser metadata | 4 KiB |
| USER identity | 128 UTF-8 bytes |
| Encoded snapshot | 4 MiB |
| Snapshot spans | 20,000, within the encoded snapshot bound |
| Project retention | 256 records / 64 MiB |
| Global retention | 1,024 records / 256 MiB |

SQL type/byte-length probes and bounded projections precede retained-data
materialization or JSON parsing. Quota accounting uses actual UTF-8/BLOB bytes
for every stored column (integers as decimal text), plus a record count; SQL
overhead is outside these logical budgets. Both quotas are enforced atomically
on append. Equal replay needs no additional reservation. There is no eviction or
expiry. Capacity/snapshot-size refusal writes nothing and never vetoes an
ordinary document edit. Quotas are configurable only through strict internal
`ExportBudgets`, not source text or a public request.

## Remaining pipeline and validation boundary

The next execution slice still needs protected read-only worker staging, explicit
project-initiated USER execution context, a validated TeX tool capability and
export-specific artifact lineage. Pandoc has only an HTML capability probe today.
Pandoc/TeX run as worker programs under existing policy/provisioning/recovery
fences, never as host conversion or GPL imports into platform code. Conversion
must preserve original math, macro overrides, theorem kinds and labels. Failure
logs/partial files must be retained without presenting a failed PDF as complete;
comments remain an explicit optional sidecar. None of these features is enabled.

Local validation passes 134 affected document/store/macro tests, including 53
export-input cases, plus Ruff and strict Pyright checks. Focused hostile tests
cover source/context selection, concurrent edits, immutable
replay, saved-profile compatibility, dependency holds, canonical reconstruction,
corrupt 20 MiB rows, scope, exact byte/count quotas and rollback. Full combined
delivery gates belong to the parent integration. No worker/OpenShell runtime,
browser, provider, conversion or PDF acceptance is claimed for this prerequisite.
