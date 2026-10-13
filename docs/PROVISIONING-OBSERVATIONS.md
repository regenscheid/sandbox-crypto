<!-- SPDX-License-Identifier: MIT -->
# Read-only provisioning observations

The worker supplies bounded control-store observations for the first part of
[CP-26](specs/conda-provisioning.md). These are new GET projections; existing
provisioning routes, execution admission, permission decisions and package
lifecycle behavior are unchanged. They do not install, cancel, approve, deny,
rebuild, wake a sandbox, reconnect a guest or run a probe.

All routes are below
`/v 1/environments/{environment_id}/provisioning/observations` and require
`project_id`. The bearer client must see that project's recorded managed
environment. Deleted, unknown and foreign targets return 404, including for the
pool operator's observation reads. The current environment's creation boundary
excludes older records under a reused environment ID. This is the existing
recorded creation boundary, not a new cross-request lifetime token.

Successful responses name the exact project/environment and a timezone-aware
`observed_at`. That time identifies a worker control-store read; it does not
establish current guest health. Responses are at most 64 KiB of UTF-8 JSON and
carry `Cache-Control: no-store`.

| GET suffix | Observation |
| --- | --- |
| empty suffix | Literal package admission gate, nullable current revision, complete bounded unfinished queue summaries |
| `/operations/{provision_id}` | Current retained operation detail; optional `seq` demands that exact sequence |
| `/operations/{provision_id}/files?seq=N&offset=0&limit=8` | Selected exact-sequence resolved-file page |
| `/revisions?after=-1&limit=8&target=N` | Finite retained revision summary window; first read chooses newest target when omitted |
| `/revisions/{revision}/probes?pin=SHA&offset=0&limit=8` | Selected revision-record probe page |

Queue summaries expose operation/request/session IDs, sequence, state/outcome,
stage/reason, times, revision pins, permission-request number and exact totals.
They contain no purpose, files, probe output, commands or full operations. Queue
order follows canonical insertion/request order, without assuming wall clocks
never regress. At most 32 unfinished records are admitted to a view.

The gate is the existing versioned execution-admission record:
`ready`, `hold` or `needs_recovery`. `recorded=false` explicitly describes only the
existing absent-row default: generation 0, ready, no operation or revision. This
is not a claim that packages work or a VM is ready. Unknown current package
revision remains null. Missing or inconsistent retained recovery/owner evidence
refuses rather than becoming an all-clear.

Selecting an operation without `seq` reads its **current retained** bounded
request, summary, policy/catalog pins, next step and error kind/presence in one
snapshot. An operation ID found in revision history does not recover its state
at that historical revision. The response's `summary.seq` is the pin for later
reads. Supplying an older sequence returns 409; it never silently adopts a newer
one. File pages always require exact `seq`, declare `total`, `offset`, `limit`
and `next_offset`, and retain original declared licenses/AGPL/policy decisions.
A displayed policy decision or permission-request number grants no permission.
Error bodies, notes, changes and execution records are not exposed as detail
in this slice; their available totals are summaries only.

Revision pages expose the actual retained count and first revision, selected
upper target, requested cursor/limit and next cursor. Retention is not a complete
archive. Each row names its recorded fingerprint, attribution, lockfile reference,
command/omitted-command/probe counts and exact original record SHA-256. Lockfile
references are recorded references, not proof the blob is still available. No
lockfiles or command bodies are loaded by these views. An expired selected
window returns 409 rather than an invented empty history.

A revision can gain probes or a lockfile without changing its number. Selected
probe pages therefore require its exact record hash, not just that number.
Changed pins return 409. Probe kind/status/output and unknown revision attribution
remain literal: `import_check` is not a functional success, and a null probe
revision is visibly unknown. Pages may contain fewer rows than the requested
limit to fit the complete response bound; exact totals and continuation identify
what remains.

## Read bounds and failure behavior

The read component borrows the Store connection and owns no connection or thread.
Each synchronous request uses one SQLite read snapshot and performs no persistent
writes. It does not call full `Provisioner.get/queue` or full Store operation and
revision readers. Raw retained type/UTF-8 byte lengths are checked before JSON:
16 MiB per operation,2 MiB per revision,32 MiB aggregate selected inputs per request,
at most 32 queue rows and 8 revision/page rows. These limits can hold a larger
legacy record without changing or deleting it.

Only allowlisted compact projections or explicitly selected array elements cross
Python, with independent prefix/byte, shape, count, strict scalar and canonical
scope checks. Invalid prefixes refuse; they are not displayed as clipped complete
data. Revision hashes stream original SQLite bytes in 64 KiB chunks inside the
same snapshot. Selected files/probes are individually bounded before parsing. Unselected array
contents are not asserted valid: a corrupt selected element refuses its page
rather than being skipped or counted as a displayed complete set.
The full response, including its envelope and cursors, is checked against 64 KiB.

Unknown parameters, invalid indices and missing page pins return 400; unsupported
provisioning returns 422; stale selections return 409; malformed, corrupt or
quota-exceeding retained observations return fixed 503 without raw stored text.
There is no automatic fallback, work submission, eviction or recovery attempt.

The implementation has focused retained-state and in-process authenticated HTTP
tests, including hostile oversized rows/scalars, strict booleans, scope swaps,
exact paging, concurrent mutation, missing/evicted history, real source hashing,
no active component calls and rollback without persistent mutations. Whole
integration gates, workspace/browser acceptance and real OpenShell proof are
separate root-owned validation.

Two-lockfile comparisons, installed-package license summaries, authenticated
operator answers/expiry history, rebuild/recovery controls and full CP-26 delivery
remain separate work. A workspace cookie is not an operator permission capability.
