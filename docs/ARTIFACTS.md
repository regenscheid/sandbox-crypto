# Immutable artifact inventory

## What is built

The harness can retain explicit `publish` results in the canonical control store
when run with `--state-dir`. This implements explicit publication from the approved
[research-artifacts specification](specs/research-artifacts.md); the consistency
review reads its events and content as evidence. It does not parse rich content
or depend on document writer closure. Existing worker publication remains available
without a state directory, with its existing `ArtifactRef` and behavior.

`contracts/artifact_inventory.py` distinguishes worker snapshot references from
control revision references. `ArtifactInventory` in the orchestrator owns logical
IDs, immutable bytes/revisions and bounded event pages in `control.sqlite3`.
`ArtifactPublisher` connects a trusted run/context and worker endpoint to that
inventory. These are internal interfaces, with no HTTP route, GUI, scheduler or
permission grant of their own.

## Identity, bytes and provenance

A logical artifact belongs to a project and canonical workspace path. Shared files
use `shared/<path>`; session files use `sessions/<session>/<path>`. Literal POSIX
paths must be relative, bounded and free of empty, dot, parent or control-character
segments. The control ID starts with `artifact-`, distinct from the worker's
`art-` snapshot ID. Publishing changed bytes at the same scoped path appends a
revision with a parent, SHA-256, exact size, media type and timestamp. Publishing
the same hash/size as the current head retains that revision and appends an
attributed `artifact.reproduced` event. Its original revision metadata stays intact.

`ArtifactLink(mode="latest")` resolves a head and its exact metadata in one read
transaction. Evidence should use the returned `ArtifactRevisionRef`, whose pinned
project/ID/revision/hash/size/media identity is checked on content reads. Reads
verify retained byte length and SHA-256; they never invoke a renderer or parser.
Revision and event rows are append-only, with SQLite guards against update/delete.
Project mismatches are refused; caller-controlled metadata grants no ownership.

Known provenance describes the **publishing action**: trusted project/session/
environment context, run ID, tool-call ID, CLI alias, the actual gateway-reported
model for that call when present, and a fingerprint of the approved worker URL.
The model's JSON cannot choose those fields or impersonate USER. No CLI alias is
presented as a resolved model identity. Unknown resolved model stays null.

The worker's `produced_by` is retained only as `worker_last_writer_hint`. The
control inventory records `producer="unknown"` and `observed_inputs="unknown"`.
A last recorded execution that listed a changed path does not prove which process
wrote the published bytes. No producing-run, kernel-input or observed-file lineage
is invented from paths, tool order, a session's last execution or model statements.

The existing worker publisher rejects symlink traversal and verifies the retained
blob against its reported hash and size. It reads mutable source ranges through
separate file opens. This establishes exact retained bytes, not an atomic source
snapshot or proof that all handles/maps were closed. A concurrently written source
can fail publication or yield the bytes matching that pass's digest. The control
side transfers the immutable worker blob, so subsequent path changes cannot alter
its pinned result. This does not establish the stronger writer proof required for
replacing living document files (LD-49 remains open).

## Intent, transfer and recovery

### Bounded worker capture prerequisite

Worker publication requires executor `artifact_capture_protocol=1`. A legacy
executor receives a visible `unsupported` refusal; ordinary `read_file` behavior
is unchanged. The capture protocol bounds the first whole-file hash.

Each range runs fixed stdlib-only code in a sandbox child using Python `-I -S`,
fixed arguments and a minimal environment. The child opens parents relative to
held directory descriptors with no symlink traversal, opens the final entry
nonblocking, then proves it is regular before reading. It refuses an initially
oversized file before hashing; growth consumes at most the byte cap plus one.
The first pass hashes the whole file while retaining only the requested range.
Later bounded ranges must match its stat identity stamp and size. The final copied
blob must match the first exact SHA-256 and size. This proves retained bytes, not
atomic source capture or writer/producer identity.

| Capture resource | Bound |
| --- | ---: |
| Source size | Configured worker maximum, capped at 1 GiB |
| One retained range / hash read block | 8 MiB / 1 MiB |
| Helper input / bounded reply overhead | 16 KiB / 16 KiB |
| Durable admitted publications, global / environment | 4 / 2 |
| Actual guest helpers per executor | 2 |
| Operation budget, including lock waits and host I/O | 30 seconds by default |
| Cleanup acknowledgement attempt, including transport write | 1 second |

There is no capture admission queue. The programmatic operation budget can be
0.05–60 seconds; no deployed configuration is changed. The worker's existing
1 GiB publication default remains. The helper streams hashes instead of allocating
the whole source. A private four-thread host pool owns blob operations without
occupying the default executor with blocked work.

Timeout/cancellation sends TERM, then KILL after 0.2 seconds to the exact owned
helper and drains stdout/process wait. Fixed helper code creates no descendants.
Repeated cancellation cannot discard a spawn, close a blob FD while its thread
still uses it, or release actual helper capacity before exit. Filesystem I/O stuck
in an unkillable kernel wait may outlive the budget and cleanup grace. The guest
pool retains that real task and slot; no strict wall-time/reap guarantee is made.
Host filesystem threads likewise retain their lease and FD until actual return,
even after the operation deadline.
The host wire request may be cancelled after its bounded cleanup-ACK attempt,
but this is never evidence of guest cleanup.

FULL-synchronous SQLite leases reserve global/environment capacity before any
dispatch await and persist each exact helper ID, incarnation and known original
sandbox name/immutable ID before sending. A positive capture/cancel reply must
name that exact helper and explicitly prove cleanup. Generic errors, lost replies,
transport closure and a new incarnation do not release admission. Once actual host
work drains, missing proof leaves a durable `held` lease. Only exact same-incarnation
cleanup ACK on reconnect or an existing verified stop/delete fence for its original
VM may resolve guest uncertainty. A verified VM fence marks an active host lease
guest-clean but never releases it before host completion. Corrupt state fails closed.

The lifecycle still assumes one active worker owns its host tasks. Atomic SQL
admission also counts other connections, but an `active` row left by a crashed or
foreign worker has no proof of host cleanup and remains a conservative hold.
There is no lease expiry, automatic operator repair or new-incarnation reset.
Unknown legacy/local-backend bindings cannot borrow a managed VM's deletion proof.
A clean held slot may require a same-daemon reconnect after a lost late ACK.

The bounded client publication request has a 10-second deadline; the server's
default budget is 30 seconds. Default aiohttp handlers can continue after peer
disconnect. A client timeout therefore means an unknown outcome, not immediate
remote cancellation. Existing durable control publication intent handles this
uncertainty without retrying the mutable path. The worker's unproven last-writer
hint searches only the newest 256 scoped terminal records with a separate
64 KiB per-record cap; only row IDs are materialized. Larger, corrupt or older
records produce `unknown`, never lineage proof.

Local hostile tests cover sparse/growing/special sources, malformed replies,
repeated cancellation, delayed spawn, blocked host I/O, lost transports, corrupt
lease metadata, exact original-binding fences and actual HTTP disconnect behavior.
Checked on OpenShell in
[Lab22](../.agents/docs/agent-reports/babbage-openshell-lab-22-2026-10-12.md), with a
1 MiB worker limit, so maximum-size capture is not covered there. The lab recipe
is in [the lab guide](../.agents/labs/README.md#lab22-bounded-artifact-capture).

### Worker-local terminal observations

The worker retains a bounded typed sidecar from the executor's existing
regular-file stat walk. It adds no scan, content read, hash or unowned filesystem
operation. `written_files` and ordinary execution/API behavior remain compatible.
The sidecar is independent of the result schema, so malformed or oversized derived
metadata produces a gap while the healthy research outcome remains unchanged.

Paths carry an explicit `session` or `shared` area and canonical base64 of the
original relative POSIX bytes, before presentation replacement or the legacy
`shared/` prefix can alias them. Invalid UTF-8 names remain distinct opaque names;
they are not converted into paths the current UTF-8 capture API can consume.
The only source kinds are `regular_stat_difference`, `regular_observed` when a
limited/error before-report cannot establish novelty, and `not_seen_after`.
The last means missing from this after-report, **not a deletion or tombstone**.
Links and special files are unavailable. All mutation coverage is incomplete,
even for an empty, non-truncated report. Same-size rewrites with restored timestamps,
transient writes/deletes, concurrent writers and process causality remain unproven.

Before the first dispatch, the worker stores the accepted execution/attempt/operation,
request digest and context beside the original channel incarnation and known paired
managed sandbox name/immutable ID. Recovery identity remains canonical execution
state even when the derived pin is unavailable. Recovery never fills missing original
facts from today's endpoint. The accepted actor is informational; actual producer,
observed inputs, publishing run/model, image digest and input/kernel lineage are not
invented. Source scan intervals use the executor's untrusted monotonic clock. A separate
trusted host `received_at` records journal receipt/commit preparation; it does not
claim attempt-end source bytes or reuse a possibly legacy execution end timestamp.

Terminal execution, final events, daemon source cursor and observation detail/gap
commit in the same worker SQLite transaction. Exact terminal replay leaves the first
record unchanged. Derived quota/validation/counter/binding corruption cannot veto a
healthy terminal record: if no trustworthy scoped sequence exists, one fixed invalid
marker remains tied to that attempt with no invented sequence, and scoped history
reads are explicitly unavailable. SQLite I/O or whole-store failure still affects
the shared transaction's durability; this is not a second independently durable store.

| Observation resource | Engineering cap |
| --- | ---: |
| Additional raw metadata per before/after report | 256 entries / 64 KiB bookkeeping |
| One normalized report | 64 entries / 16 KiB |
| One fixed terminal marker / scoped read page | 12 KiB / 32 records and 64 KiB |
| Retained detail, global / project | 4,096 reports and 32 MiB / 8 MiB |
| Detail evictions during one terminal commit | 64 |

Detail retention removes oldest eligible payloads while retaining their fixed
attempt markers, hashes and explicit `evicted` gaps. Quota refusal retains a `quota`
marker. Counters, cursors and eviction floors are project/session/environment scoped;
another scope's activity does not create sequence holes or disclose a global
cursor; shared global retention can still evict scoped detail.
Internal `Store.observation_page` pins a target and stream identity and clips SQL
metadata before parsing. History begins at an explicit feature baseline, with no
startup backfill of older terminals. Older unfinished attempts have unavailable
original pins. Fixed markers/bindings follow existing execution history; these detail
caps do not bound that history, SQLite/WAL high-water usage or process RSS. The existing
20,000-file/depth-128 walk still has potentially large directory-name lists and no
new hard I/O deadline; the separately bounded sidecar does not fix those limitations.

The sidecar has no public feed, WorkerClient method, consumer, scheduler,
content capture, artifact revision, permission or runtime capability advertisement.
Checked on OpenShell in
[Lab24](../.agents/docs/agent-reports/babbage-openshell-lab-24-2026-10-12.md); the
[lab recipe](../.agents/labs/README.md#lab24-worker-local-output-observations)
inspects only its disposable worker's internal read-only journal. RA-5's
every-file-minus-ignore-patterns requirement is not met by this incomplete stat
report.

### Publication operations

An artifact operation is separate from document operations, keyed by trusted
project/session and operation ID. Before the worker RPC, the inventory reserves a
bounded durable intent binding the exact request, publishing context and worker
endpoint fingerprint. An identical completed replay returns its immutable receipt;
changed request/context/endpoint reuse conflicts.

There is no transaction spanning worker and control. If the worker response is
unknown after disconnect/crash, the saved intent holds and **never republishes the
mutable path** under the same operation. The operator must investigate and use a
deliberately new operation for a new publication. The worker reference is pinned
durably before its bytes are fetched. A known pin can retry transfer after restart,
using that exact project/hash/size/reference even when the source path has changed.
Endpoint changes require a new operation; old intents and pins are retained.

`WorkerClient.publish_snapshot` and `verified_artifact_content` are bounded methods
specific to this flow. The POST reply is at most 16 KiB; transfer has a 10-second
total deadline, no redirects or decompression, capped error bodies, exact project
query binding, strict size/digest metadata, a matching digest header and verified
bytes before retention. Ordinary client methods and their defaults are unchanged.

Bytes are verified outside the write lock. One transaction commits the BLOB,
immutable revision/head, event and completion receipt. Failure leaves no partial
revision or head advance. A completed replay checks its canonical reference against
the pinned worker hash/size, project and canonical path. SQLite admission uses one
private worker thread; at most eight admitted publication tasks retain ownership
through caller cancellation. The caller must drain `ArtifactPublisher.aclose()`
before closing its borrowed `DocumentStore` connection or HTTP client. CLI cleanup
preserves that ordering even when cancellation repeats during drain.

A `recorded` receipt acknowledges retained control bytes. A `held` response does
not undo a successful worker publication: the tool/run record preserves the known
worker reference separately. Reserved operations retain their latest bounded hold
observation, including transfer/hash/quota failures, without surrendering their pin
or retry ownership. This is not an append-only record of every transfer attempt.
Admission, corruption or disk failures can prevent retaining that observation;
the returned message identifies this and cannot promise a durable audit record.
Unknown intents and unresolved pins are never silently evicted.

## Retention bounds

`ArtifactLimits` supplies programmatic limits; no deployed configuration changes
are required. Defaults are:

| Resource | Default |
| --- | ---: |
| One retained artifact revision | 8 MiB |
| Retained revision bytes per project / global | 64 MiB / 256 MiB |
| Retained revision count per project / global | 4,096 / 16,384 |
| Operation count per project / global | 8,192 / 32,768 |
| Global metadata budget | 64 MiB |
| Intent / worker or receipt / event or revision metadata | 8 KiB / 16 KiB / 16 KiB |
| Concurrent admitted publications | 8 |
| Metadata/event page | 10 records |

Each operation reserves 40 KiB of metadata capacity before the remote call.
Reservations count unknown outcomes and zero-byte publications. Revision count
limits also count zero-byte files. Completion rechecks byte/count/metadata budgets,
including tightened limits; preserving a previously reserved known worker outcome
does not depend on new retention capacity. Full retention refuses new work rather
than deleting unresolved history. SQL clips variable metadata/BLOB fields before
Python allocation. Logical budgets do not claim to bound SQLite/WAL physical disk
usage or total process RSS; operation retries, receipts and revision bytes consume
their documented retention budgets. There is no purge or retention UI.

## Tests and evidence reads

Tests compose the actual authenticated worker HTTP API with control retention,
including binary/PDF-shaped bytes without parsing them. Hostile cases cover unknown
publication replies, exact pinned retry after path change/restart, endpoint changes,
quota tightening, zero-byte/count/metadata limits, corrupt large SQLite fields,
receipt substitution, transfer digest/size/header errors, redirects, deadlines,
transaction rollback, forged context/USER attribution and repeated cancellation.
These are local fixtures, not OpenShell evidence.

Successful tool records include `artifact_inventory` with a pinned control receipt,
beside the original `artifact` worker snapshot. Internal `resolve`, `content`,
`list` and `events` methods form the evidence read boundary. A consumer can request
a smaller `content(max_bytes=...)` limit: the SQLite BLOB prefix itself is bounded
before Python allocation, including corrupt rows whose stored size is incorrect.
Empty BLOBs return exact empty bytes rather than SQLite's NULL substring result.

### Explicit-publication consistency consumer

With the inventory present, the live review controller consumes its durable events,
not the tool callback's metadata. Each session advances an independent project-event
cursor; only events attributed to that publishing session grant content. Shared
artifacts still need an explicit publication/evidence grant in that session. One
safe point scans at most ten events and ten unseen unresolved operations; a terminal
boundary scans at most 100 of each through a pinned event head. Feed inserts and the
source cursor commit together. Identical replay and concurrent ingestors cannot
count a new revision twice; reproductions count zero new revisions. Remaining work
is explicit incomplete ingestion and resumes at the next live boundary.

A source copy is at most 64 KiB of exact verified bytes, decoded as strict UTF-8.
The initial allowlist is `text/plain`, `text/markdown`, `text/csv`,
`text/tab-separated-values` and `application/json`, with no parameter or only a
UTF-8/US-ASCII charset parameter. JSON and Markdown remain raw strings, never parsed
or rendered. Quotes, backslashes, CRLF and a BOM are preserved. Binary control
characters, other media/charsets, invalid UTF-8, oversized or unavailable bytes
produce metadata-only incomplete evidence with a reason. No clipped source is
labelled complete. `review_read` returns source characters, the pinned original
byte SHA and availability separately from the evidence JSON digest. Empty output
returns empty text and an audited empty-source marker, never invented source lines.
A snapshot's copied text cannot change when the worker path or artifact head changes.

An unresolved operation gets one finite `artifact:operation:<ID>` observation:
unknown remote outcome is distinct from a known worker snapshot whose control bytes
are not retained. Neither grants source content or initiates a worker request. A
later successful retention emits its normal event. Old uncertainty belongs to its
own reviewed window and does not make every future window incomplete. This is a
first uncertainty observation, not an audit of every transfer retry.

Author fix claims can carry typed exact `artifact_revisions`. They must identify
artifacts anchored by that finding and exact revisions already observed for the
same project/session. Confirmation pins those references from the durable transition
and requires full audited exposure of original, response and changed source; missing
artifact content cannot be confirmed from metadata. Whole-artifact anchors are
supported; physical line, parsed block and PDF-region anchors remain separate work.
Supersession records an observed newer revision and never claims that the fix worked.
The reviewer still has only snapshot grants, with no artifact lookup tool, worker
client, download, filesystem, parser, renderer or execution authority.

The consumer owns a private single-thread executor and at most eight admitted SQL
units. Cancellation detaches the await; shutdown drains actual work before the
borrowed control store closes. Checked in the browser in
[artifact review acceptance](../.agents/docs/agent-reports/artifact-review-2026-10-12.md),
with synthetic state and no live model, worker or OpenShell VM.

## Not built

- Automatic execution-output inventory (RA-5, RA-F2, CRF-6). It needs an
  execution/attempt/incarnation-bound one-shot capture intent, status and receipt
  first, so that an unknown outcome never republishes a mutable path and a known
  receipt pins exact retained bytes for retries.
- Complete telemetry and snapshotting, deletion and link semantics, ignore patterns,
  authenticated feed binding and control ingestion of the worker's observations.
- Imports and MCP binary results, deletions and tombstones, retention drop
  workflows, observed inputs and kernel lineage, public read/download routes, ledger
  integration, rich previews, and PDF parsing and anchors.
- The stronger writer proof that living-document files need (LD-49). Explicit
  publication does not prove an atomic source pass.
