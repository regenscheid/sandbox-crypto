# Immutable artifact inventory

## What is built

The harness can retain explicit `publish` results in the canonical control store
when run with `--state-dir`, and records the files its calls write by themselves
([below](#recording-the-files-calls-write)). This implements explicit publication and,
for tool calls, the recording of written files from the approved
[research-artifacts specification](specs/research-artifacts.md); the consistency
review reads its events and content as evidence. It does not parse rich content
or depend on document writer closure. Existing worker publication remains available
without a state directory, with its existing `ArtifactRef` and behavior.

`contracts/artifact_inventory.py` distinguishes worker snapshot references from
control revision references. `ArtifactInventory` in the orchestrator owns logical
IDs, immutable revisions and bounded event pages in `control.sqlite3`. New content
lives in adjacent `control.sqlite3.artifacts/` hash-addressed files; old small SQLite
BLOBs remain readable. The database and blob directory are one backup unit. A PVC
can hold that directory. Optional [S3 backing](ARTIFACT-S3.md) on `k8s` verifies
remote content before allowing explicit local eviction and supports cold reads.
`ArtifactPublisher` connects a trusted run/context and worker endpoint to that
inventory. These are internal interfaces, with no scheduler or permission grant of
their own. The workspace reads the inventory through two routes, the project's
artifact list and a link that opens a published PDF in the
[PDF viewer](WORKSPACE.md#the-pdf-viewers-origin).

## Acknowledging durable worker copies

New fully retained publications commit an endpoint-bound worker acknowledgement
intent in the same transaction as their immutable control receipt. Delivery is
best effort: an unavailable worker leaves cleanup pending without downgrading a
successful artifact or repeating a mutable capture. Each run starts with at most
eight pending deliveries sharing a five-second HTTP budget, stopping at the first
failure. Replaying a completed explicit or automatic execution capture retries
its original acknowledgement without downloading or publishing again. Existing
historical receipts are not automatically treated as acknowledged.

The operator can drain a bounded page independently of any agent run:

```bash
sandbox-science-artifacts acknowledge --database /srv/research/control.sqlite3 \
  --worker https://worker.example.invalid --token-file /secure/worker.token \
  --project example-project --limit 8 --after 0
```

The token needs the project's explicit `artifact_retention` permission (the pool
token also has it). The endpoint must match the original publication's saved
digest. Output reports delivered/failed counts and an `after` cursor scoped to
that configured worker. A failed row remains pending; omit `--after` to retry from
the beginning, or use its cursor to process later rows without clearing the failure.
Delivery audit records remain in `artifact_retention_outbox`; credentials and raw
network errors are not stored there. Ordinary execution credentials cannot assert
that control storage retained content.

The local launcher grants this permission only to its trusted `harness` client;
workspace, launcher-maintenance and ordinary execution clients do not receive it.
Other deployments must set the control client's permission explicitly.

Acknowledgement alone does not unlink anything. The worker's
[`artifact-retention` command](WORKER.md#offline-capture-replica-retention) previews or applies
physical release only while that worker is stopped, under its exclusive state
lease. Every reference sharing a hash must have an exact acknowledgement; installer
references or invalid/unknown metadata preserve the bytes. Metadata and release
audit survive cleanup and restart. This does not archive or delete research
workspaces/PVCs, remote objects or control-owned content. Worker protocol 11 is a
lockstep application update; new container images have not been published.

## Identity, bytes and provenance

A logical artifact belongs to a project and canonical workspace path. Shared files
use `shared/<path>`; session files use `sessions/<session>/<path>`. Literal POSIX
paths must be relative, bounded and free of empty, dot, parent or control-character
segments. The control ID starts with `artifact-`, distinct from the worker's
`art-` snapshot ID. Publishing changed bytes at the same scoped path appends a
revision with a parent, SHA-256, exact size, media type and timestamp. Publishing
the same hash/size as the current head retains that revision and appends an
attributed `artifact.reproduced` event. Its original revision metadata stays intact.
The publication still makes the artifact a result (RA-9, RA-37): its receipt pins that
revision, so a file a call recorded and the agent then published unchanged is listed
under results.

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
| Operation budget, including lock waits and host I/O | 240 seconds by default |
| Cleanup acknowledgement attempt, including transport write | 1 second |

There is no capture admission queue. The programmatic operation budget can be
0.05–600 seconds. The worker's existing
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
cleanup ACK (on reconnect, or on the running executor when a publication finds admission
full) or an existing verified stop/delete fence for its original VM may resolve guest
uncertainty. A verified VM fence marks an active host lease
guest-clean but never releases it before host completion. Corrupt state fails closed.

One worker owns a state directory, under an exclusive lock (decision 9), so an
`active` row left by a stopped worker has no host work left: at startup one never
dispatched goes, and a dispatched one becomes `held` until its executor confirms the
cleanup. There is no lease expiry or new-incarnation reset; with the worker stopped,
`sandbox-science-worker captures --clear` forgets a lease on the operator's word.
Unknown legacy/local-backend bindings cannot borrow a managed VM's deletion proof.

The bounded client publication request has a 300-second deadline; the server's
default budget is 240 seconds. Default aiohttp handlers can continue after peer
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

`WorkerClient.publish_snapshot` and `stream_artifact_content` are bounded methods
specific to this flow. The POST reply is at most 16 KiB; transfer has a 300-second
total deadline, no redirects or decompression, capped error bodies, exact project
query binding, strict size/digest metadata, a matching digest header and verified
bytes before retention. Ordinary client methods and their defaults are unchanged.

Bytes stream through bounded chunks into a private staged file. Its exact hash/size
are verified outside the write lock, then its file and installed hash directory are
fsynced before one transaction commits the blob reference, immutable revision/head,
event and completion receipt. Failure leaves no partial
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
| One retained artifact revision | 1 GiB |
| Retained revision bytes per project / global | 16 GiB / 64 GiB |
| Revision count per project / global | 50,000 / 200,000 |
| Operation count per project / global | 100,000 / 400,000 |
| Metadata budget per project / global | 256 MiB / 1 GiB |
| Intent / worker or receipt / event or revision metadata | 8 KiB / 16 KiB / 16 KiB |
| Concurrent admitted publications | 8 |
| Concurrent staged transfers, per project / global | 2 / 4 |
| Staged bytes, per project / global | 2 GiB / 4 GiB |
| Metadata/event page | 10 records |

The defaults are sized for recording every file executions write ([research
artifacts](specs/research-artifacts.md), decision 9). An operation in flight reserves
40 KiB of metadata capacity before the remote call; one with a pinned worker reference
counts what it stored and the receipt it may still record; a finished one, or one held
without a worker reference, which never finishes, counts what it stored. Reservations count unknown outcomes and zero-byte publications.
The figures are kept per project and overall in `artifact_usage` by triggers on the
artifact tables (control store version 20), so a budget check reads one row rather
than every revision. `room()` tells a caller whether a new revision of a given size
can keep its bytes, only its hash and size, or nothing (RA-31, RA-32). Revision count
limits also count zero-byte files. Completion rechecks byte/count/metadata budgets,
including tightened limits; preserving a previously reserved known worker outcome
does not depend on new retention capacity. Full retention refuses new work rather
than deleting unresolved history. The tables check metadata sizes, JSON and states
when they are written (schema version 7), and reads take rows as they are; only
artifact content reads stay bounded by the caller's limit. Logical budgets do not claim to bound SQLite/WAL physical disk
usage or total process RSS; operation retries, receipts and revision bytes consume
their documented retention budgets. Durable transfer reservations count against
retained headroom. Logical bytes charge every retained revision; a reproduction
adds none, even when hashes deduplicate physically. Failed metadata commits after
blob installation leave the blob and reservation charged. Crashes can leave staged
files and reservations; there is no automatic GC, purge or retention UI. Explicit
operator recovery must prove inactivity and lack of references before removal.
See [durable artifact blobs](specs/durable-artifact-blobs.md).

## Revisions without bytes, tombstones and recorded files

Besides published revisions, the inventory records (research artifacts, RA-31 to
RA-34):

- a file's SHA-256 and size without its bytes, with `content_retained: false` and
  `content_note` `too_large` or `quota` (`record_metadata`). Its hash came from the file
  read in the sandbox (`hash_source: sandbox`), which the platform cannot check, unlike a
  captured revision's (`capture`). Its content read refuses, saying the bytes were not
  kept; the same hash and size as the head adds only a reproduction, but bytes kept
  later after a hash-only head are a new revision;
- a tombstone for a deleted file (`record_deletion`): a revision with `deleted: true`,
  no bytes, and an `artifact.deleted` event, checked against the budget like any
  revision; nothing when the path has no live artifact. Earlier revisions stay readable, and content written again later is a new
  revision rather than a reproduction of a deleted one.

Their source can be `execution_record` (the worker reported the file written or
deleted during an execution, which it names with the attempt's end) or
`declared_result` beside `agent_publication`, with the run's role, its parent run and
delegation for a delegated run. None claims the execution wrote the file. The
consistency review sees a revision without bytes, or a tombstone, as `unavailable`
evidence with that reason, and counts it as a change.

## Recording the files calls write

With a state directory, each run has a `FileRecorder` (`orchestrator/recording.py`;
RA-27 to RA-35). After a shell call, a kernel cell, a PDF helper or `write_file`, and
before the result goes back to the model, it records the files the worker reported:

- It reads the project's [file settings](PROJECTS-AND-SESSIONS.md#files-executions-write)
  for each call: a project that records results only records nothing here, and the
  ignore patterns leave files out, matched off the event loop; a path with a name no
  artifact can have is left out with that reason.
- It lists each directory once for the files' sizes; a directory that cannot be
  listed, or whose listing was cut short, is the reason its files are left out. A file
  within the budget and within 1 GiB is published through the run's `ArtifactPublisher`,
  under an operation ID made from the execution and the path (for `write_file`, the
  harness's operation ID for the call, never the model's call ID, which gateways number
  per answer), so a replay finds the first receipt; a
  larger file, or one the budget has no room for, is recorded with its hash and size
  (read through the worker, which hashes the whole file) and `content_retained: false`.
  When the project's revision count is full, recording stops.
- A deleted file that is a live artifact gets a tombstone.
- One call tries at most 100 files, tombstones and failures included, and keeps at most
  2 GiB of bytes: each transfer is bounded by what is left, so a file that grew after
  its listing cannot carry the call past it. A file that exceeds the remaining content
  allowance still gets hash/size metadata. A file over 4 GiB, one gone before it was
  recorded, and one the inventory refused are counted as left out, by reason. The source is `execution_record`, with the run's role, parent run and
  delegation, the tool call, the model the gateway named, and the execution with its
  attempt's end; it says the worker reported the file, not that the call wrote it.
- The call's record keeps `recorded_files` (counts, the files left out by reason, and
  up to 100 entries), the model's result carries the counts, and the run's record
  keeps up to 200 of its recorded files with their total in `summaries["files"]`.
  Recording never fails a call: if it stops part way, the files recorded before stay,
  and the report says it stopped.

A background job's files are recorded when a run first makes its completion notice:
the recorder reads the job's whole result from the worker (its status leaves the file
lists out) and records them as for a call, attributed to the operation that started
the job and its execution; the notice carries the counts. A notice made again, after
a run that failed, records them again, which keeps the first revisions. A delegated
run's calls record files the same way. Its delegation's outcome lists them (RA-36) and,
for a task, pins the result files it declared, each one a revision its own calls made
(RA-38; [research artifacts](specs/research-artifacts.md)). The GUI's artifact list shows
results first and every other recorded file under **All files**, the most recently
changed first, with who made each latest revision; it filters by session, run and a
path's text ([workspace](WORKSPACE.md); RA-37).

## Finding artifacts from a later run

Only `publish`'s result names an artifact's ID and revision, and a later run gets
earlier tasks and answers without tool results, so an agent could not cite a file
an earlier run published (lab 38, B6). `find_artifacts`, a read tool offered with
the inventory, lists the project's artifacts at their latest revision, ten at a
time, the most recently changed first, from the same query as the GUI's list. Each
gives its path, artifact ID, revision, SHA-256, size, media type, when it was
recorded, how (published, recorded from a call or declared), whether it is a result,
and whether it was deleted or kept only its hash. It finds one by its path as the
session's tools name it (`results/x.json`, or `shared/…`) or as it lists it
(`sessions/<session>/…`), by text in the path,
by a media type or its start (`image/`), or results only, and pages with
`before`. The answer fits the tool's output budget. The ledger's evidence
description names it where it explains an artifact pin.

## Tests and evidence reads

Tests compose the actual authenticated worker HTTP API with control retention,
including binary/PDF-shaped bytes without parsing them. Hostile cases cover unknown
publication replies, exact pinned retry after path change/restart, endpoint changes,
quota tightening, zero-byte/count/metadata limits, oversized writes refused,
receipt substitution, transfer digest/size/header errors, redirects, deadlines,
transaction rollback, forged context/USER attribution and repeated cancellation.
These are local fixtures, not OpenShell evidence.

Successful tool records include `artifact_inventory` with a pinned control receipt,
beside the original `artifact` worker snapshot. Internal `resolve`, `content`,
`list` and `events` methods form the evidence read boundary. `content()` defaults
to 8 MiB; consumers can request an explicit bounded `content(max_bytes=...)` limit.
The legacy SQLite BLOB prefix itself is bounded
before Python allocation, including corrupt rows whose stored size is incorrect.
Empty BLOBs return exact empty bytes rather than SQLite's NULL substring result.
`open_content(project, pinned_reference)` verifies content then yields a binary
stream without a held SQL transaction. Authenticated workspace downloads use it
independently of worker/container lifetime; existing PDF, image and model preview
limits remain separate from the 1 GiB retention cap.

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

- An artifact's history of revisions and tombstones in the GUI, and a run's page that
  lists its results, its recorded files and the gaps (RA-37).
- Complete telemetry and snapshotting, link semantics, authenticated feed binding and
  control ingestion of the worker's observations.
- Imports and MCP binary results, retention drop workflows, observed inputs and kernel
  lineage, read and download routes beyond the artifact list and the PDF viewer,
  previews other than PDFs, and annotations stored on PDFs.
- The stronger writer proof that living-document files need (LD-49). Explicit
  publication does not prove an atomic source pass.
