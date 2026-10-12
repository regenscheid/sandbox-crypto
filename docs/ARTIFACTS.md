# Immutable artifact inventory

## Implemented boundary (2026-10-12)

The harness can retain explicit `publish` results in the canonical control store
when run with `--state-dir`. This is the first bounded slice of the approved
[research-artifacts specification](specs/research-artifacts.md), supporting the
ledger, bounded consistency evidence and future PDF anchors. It does not parse rich content
or depend on document writer closure. Existing worker publication remains available
without a state directory, with its existing `ArtifactRef` and behavior.

`contracts/artifact_inventory.py` distinguishes worker snapshot references from
control revision references. `ArtifactInventory` in the orchestrator owns logical
IDs, immutable bytes/revisions and bounded event pages in `control.sqlite3`.
`ArtifactPublisher` connects a trusted run/context and worker endpoint to that
inventory. These are internal interfaces; no new HTTP route, GUI capability,
scheduler, permission grant or worker deployment change is enabled.

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

Worker publication now requires executor `artifact_capture_protocol=1`. A legacy
executor receives a visible `unsupported` refusal; ordinary `read_file` behavior
is unchanged. This prerequisite bounds the previously unbounded first whole-file
hash before any automatic execution-output inventory is enabled.

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
hint now searches only the newest 256 scoped terminal records with a separate
64 KiB per-record cap; only row IDs are materialized. Larger, corrupt or older
records produce `unknown`, never lineage proof.

Local hostile tests cover sparse/growing/special sources, malformed replies,
repeated cancellation, delayed spawn, blocked host I/O, lost transports, corrupt
lease metadata, exact original-binding fences and actual HTTP disconnect behavior.
Independent review cleared signed source `cbcfc7e`. Final-source local gates passed
60 capture/lifecycle cases, 9,592 Python tests (43 opt-in skips), 436 web tests and
24 post-build math checks, plus locked/static/license/protocol checks and both builds.
Independent review also cleared signed combined freeze `b535856`. Root-owned
[OpenShell Lab22](../.agents/docs/agent-reports/babbage-openshell-lab-22-2026-10-12.md)
passed exact 24-byte binary/empty retention, twelve prompt regular/sparse/symlink/FIFO
refusals and healthy publication/read/execution access on Python 3.14.8. Independent
inspection found five artifacts, three terminal shell executions and zero capture
leases/helpers; complete teardown was verified at 12:36:45 UTC. The disposable worker
used a 1 MiB limit; this does not validate maximum-size successful capture. Lost-bridge,
blocked host-I/O and corrupt-state behavior remain local hostile evidence. Actual main
PR #85 ancestry and acceptance docs change no product, test, configuration or helper
bytes. Exact source `ea31757` and both Lab22/Lab23 archives merged as PR #86 at
`9c07725` after all three fresh CI checks passed (run `38251872668`, 13:01:50 UTC). The marked reusable recipe
is in [the lab guide](../.agents/labs/README.md#lab22-bounded-artifact-capture).

Automatic execution-output inventory remains open. `written_files` is a bounded
executor-reported before/after stat manifest, not a complete mutation journal or
proof of which process wrote a path. Concurrent writers, restored metadata,
deleted/transient paths and invalid-path aliases require explicit coverage limits.
Future capture must pin the trusted execution/attempt and original incarnation;
later source bytes are observations at capture time, not fabricated attempt-end
bytes or producer/input lineage. LD-49's stronger writer proof remains separate.

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
their documented retention budgets. This slice offers no purge/retention UI.

## Evidence and remaining work

Tests compose the actual authenticated worker HTTP API with control retention,
including binary/PDF-shaped bytes without parsing them. Hostile cases cover unknown
publication replies, exact pinned retry after path change/restart, endpoint changes,
quota tightening, zero-byte/count/metadata limits, corrupt large SQLite fields,
receipt substitution, transfer digest/size/header errors, redirects, deadlines,
transaction rollback, forged context/USER attribution and repeated cancellation.
These are local fixtures, not new OpenShell acceptance evidence.

The final combined local suite passed 9,415 Python tests (43 opt-in skips), all 50
artifact cases, 431 web tests and the 24-test built Node/KaTeX boundary. Locked
dependency, formatting, lint, strict type, license and protocol checks and both
production builds passed. Independent review cleared the exact combined source,
including receipt substitution, cancellation ownership and existing parameterized
worker media metadata. PR #82 merged after the fresh Python 3.12, Python 3.13
and web CI jobs passed.

The later document-math source combination passed 9,459 Python tests (43 opt-in
skips), 434 web tests and all required locked/static/license/protocol checks and
both builds. Its manually constructed CLI cleanup fixture adds the three optional
math defaults without changing assertions or deadlines; the final full rerun and
68 affected publication/math/migration/workspace API cases pass.

The explicit-publication consumer below has a separate validation boundary.

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
borrowed control store closes. Independent adversarial review, 35 focused cases,
144 affected component/API cases and two raw-preview SSR cases cover this boundary.
The D82/main source union passed 9,494 Python tests (43 opt-in skips), 436 web tests,
locked/static/license/protocol checks and both production builds.
[Browser/API acceptance](../.agents/docs/agent-reports/artifact-review-2026-10-12.md)
verified exact original/changed bytes, empty and incomplete sources, scoped fix claims
and delayed selection. The synthetic state and server were removed; no live model,
worker or OpenShell VM was used for that browser check. PR #84 merged after all three fresh CI checks passed.

Automatic execution-output inventory (RA-5/RA-F2/CRF-6), imports/MCP binary results,
deletions/tombstones, retention drop workflows, observed inputs/kernel lineage,
public read/download routes, ledger integration, rich previews and PDF parsing/anchors
remain separate slices. Explicit publication does not prove an atomic source pass
or the stronger living-document writer-closure boundary.
