# Internal worker document source adapter

> **Archived.** This code was removed from main; it is preserved on the tag `archive/document-sync-2026-10`. See the [refactoring plan](../../refactor-plan/PLAN.md), decision 2.

The control-side prerequisite for LD-50 is implemented in
`documents.worker_source.WorkerSourceAdapter`. It converts an attested immutable capture
to named document operations, or retains its exact rejected bytes. It is an internal
library boundary: no public route, worker client, helper, filesystem capture or mirror
is enabled. The worker catalog and control publication outbox are separate prerequisites.
Nothing here establishes that an environment has no open writers.

## Trusted context and delegated authority

The future authenticated transport supplies `TrustedWorkerBinding`: project, stable
worker binding, environment, exact current or historically attested incarnation, and the
document/catalog sequence/revision/digest from a retained publication. Optional session,
execution, attempt and author-run attribution must be established by that transport;
code cannot choose them. The adapter independently checks recorded project/environment
and session ownership before preparation and again at commit. Current project APIs
cannot rebind a session's environment. A future mutable binding registry needs its own
fenced lifetime; this module does not mint that authority.

`contracts.document_edits.DirectEditCandidate` is frozen, protocol 1, with strict bounded
integers, IDs and lowercase SHA256 digests. It carries edit/operation/capture IDs, the
document, catalog sequence, optional base revision plus written digest, and raw byte
length/digest. It carries no path, URL, policy, user or Author. Catalog sequence identifies
delivery provenance and is never used as an apply-base.

Changes have `Author(kind="worker", role="delegated")`, with binding/environment/
incarnation and only established context. The operation actor is stable across retries:
`worker:<binding>:<environment>:<incarnation>`. Ambiguous attribution remains without a
session; it is never guessed from the document path or whichever call happened first.
Worker authors obey suggestions and named-patch checks. They cannot create/delete a
document, save a whole text through the privileged import path, revert, promote, decide
suggestions, respond to findings, or use agent comment/request authority. The GUI and
agent change descriptions identify worker code explicitly. Session-attributed worker
revisions contribute to the existing review feed; ambiguous revisions do not start a
session's review or invent a main-agent run.

## Capture, retention and budgets

`VerifiedCapture` holds immutable bytes and independent complete/writer-closed evidence.
An uncertain writer, incomplete transfer, or capture larger than **4 MiB** returns
`held`, without consuming the operation or claiming exact control-side retention. The
worker must retain the source/capture. Notification-only monitoring, unreadable peer
processes and advisory locks do not establish closure. Even a recorded receipt always
has `safe_to_restore=false`: publication requires a separate protected writer boundary.

Raw bytes are hashed before UTF8 decoding, newline/BOM handling or math normalization.
Unknown base, missing/mismatched immutable revision or publication digest, deleted
document, invalid UTF8, oversized document text, and unsafe source layouts are retained
as explicit refusals. A document still holds at most **1 MiB** of UTF8 text. The current
head is only a review anchor for a raw refusal; it is never inferred as the submitted
base. Such a refusal has no operations to apply and the ordinary patch preview/apply
path refuses it. A later user can explicitly edit the current document after inspecting
the bytes; applying an empty retained patch cannot overwrite anything.

Logical retained-data budgets are configurable and checked under the same SQLite write
transaction. Their defaults are:

| Scope | Raw bytes | Capture records |
| --- | ---: | ---: |
| Project/environment | 16 MiB | 128 |
| Worker binding | 32 MiB | 256 |
| Project | 64 MiB | 512 |
| Whole control store | 256 MiB | 2,048 |

All retained captures count, including successful edits, unresolved refusals and
suggestions. Record limits also bound empty captures. There is no eviction or cleanup
in this prerequisite; unresolved raw data never expires silently. Once a budget fills,
new captures remain held until a later explicit retention workflow exists. These are
logical data budgets, not filesystem quotas or a promise of equal SQLite/WAL disk size.
No large capture is mislabeled as fully retained from a sample.

## Transaction, replay and publication

The shared source preparation uses the parser's existing text/operation/normalization
bounds outside SQLite's write lock. Named operations preserve stable block/math IDs,
rebase over disjoint changes, retain same-block conflicts, and obey unnamed-change
guards. Commit requires the exact prepared document head; a race returns transient
`busy` for retry with the same ID. The current suggestions setting is checked at commit.
Adapter empty texts take the same engine path and cannot call the privileged whole-text
save. Existing USER empty-draft saves keep their prior operation identity across upgrades.

One `DocumentStore` transaction commits the patch or revision, source event, ordinary
operation reservation, exact raw bytes and immutable receipt in
`worker_source_receipts` within `control.sqlite3`. A failure rolls all of them back.
The candidate hash binds its canonical payload and trusted provenance; receipt hashes
bind the compact original outcome and retention identity. Changed payloads under either
the operation or edit ID conflict. A lost acknowledgement or process restart replays
the original receipt before source parsing and creates no second revision. Worker IDs
share the ordinary document operation namespace rather than permitting a second change
through another tool with the same actor/ID.

Receipts are bounded to **64 KiB** before storage and before parsing on replay. They
contain the outcome, revision/base/rebase/patch identity and bounded message. Full
warnings, changed block inventories and conflicts remain in the immutable patch or
revision, with `details_omitted` explicit. Internal raw reads are scoped to the exact
project/binding/environment/incarnation/document and return at most **64 KiB** per call.
No arbitrary filesystem path is read.

The source event sequence is a durable reference; it is not a catalog sequence. An
applied receipt says publication is pending, and never invents a transport acknowledgement.
The central SQL [publication hook](../../../docs/LIVING-DOCUMENTS.md)
executes inside the same document transaction; this adapter neither implements nor
imports it. Integration checks inject a failure after the hook's append and verify
rollback of source, raw capture, receipt and publication cursor, followed by one exact
retry/replay. There is no async I/O or
model/tool call under the source write lock.

## Validation and remaining work

`test_worker_document_source.py` exercises exact raw refusals, disjoint rebase/math IDs,
same-block conflict, suggestions, unknown/forged identity, bounds, all four quota scopes,
concurrent empty-capture exhaustion, transaction rollback, head/settings races, restart
replay and shared operation collisions. All fixtures are synthetic and make no worker,
model or environment calls. Existing editor/annotation/consistency fixtures protect the
shared store and authority paths.

Independent adversarial review passed, including bounded corrupt receipt reads. Combined
acceptance with the merged catalog and outbox passed 9,307 Python tests (43 opt-in skips),
431 web tests, strict/static/license/protocol checks and both production builds. This
includes 33 adapter cases and the SQL-hook rollback/retry integration. The author's
earlier broad run encountered an unrelated fake OpenShell JSON rewrite race; its isolated
case and all 79 environment tests passed on rerun without changing assertions, and the
final combined suite passed. These are control-side tests, not runtime capture proof.

Authenticating and fencing bindings, delivering and acknowledging document changes, acquiring a proven
writer boundary, filesystem capture/restore, retention resolution, USER raw-download
routes, and `ss-doc` remain separate work. This slice does not complete LD-49 to LD-52
or establish safe environment overwrites.
