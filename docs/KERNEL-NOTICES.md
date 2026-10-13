<!-- SPDX-License-Identifier: MIT -->
# Internal kernel notice inbox

This is a control-store prerequisite for the agent notification part of
[CP-12](specs/conda-provisioning.md). It retains bounded, dated observations of a
proved kernel whose start revision predates the observed package revision. It is
implemented in `orchestrator/kernel_notices.py`, with isolated persistence tests.
It is separate from [visible-session GUI polling](WORKSPACE-KERNELS.md) and
[ordinary execution result disclosures](KERNEL-REVISIONS.md).

There is no worker transport, run-loop/CLI injection, scheduler, public route or
RunRecord ACK integration in this slice. No models, services or VMs were used to
validate it. This store does not establish end-to-end CP-12 agent delivery, capture
every package transition, interrupt a blocking tool, or wake idle/offline agents.

## Trusted input and scope

`MainNoticeRun` supplies an explicit MAIN role, run ID, project, environment,
session, control-owned session-open lifetime and trusted worker binding. Its
`NoticeScope` also pins the original managed sandbox name, immutable VM ID and
executor incarnation. Every operation revalidates the typed input; a copied or
constructed model cannot bypass the positive MAIN rule. These models are internal
provenance assertions, not authentication credentials. The consumer must
independently authenticate the current MAIN context and refresh its current
session lifetime and worker binding before **every** record, hold, claim, renew,
release or ACK. No guest, browser or agent-selected object can supply that proof.

`TrustedKernelNotice` additionally requires the original retained kernel birth:
project/environment/session/language, original VM/incarnation, kernel ID, opaque
birth ID, executor-local generation and known start revision. The normalized worker
generation is a separate integer, at least the raw generation. The birth and scope
must agree exactly; start must not exceed the known observed current revision.
Both generations participate in the canonical identity. The internal birth fields
match the companion lineage contract; mapping and use of its shared identity/hash
helper remain part of the separately reviewed transport union. A flattened GUI
`KernelInfo`, receipt timestamp, name or current package list is insufficient.

Unknown legacy lineage and unknown comparison revisions use an explicit `hold`.
They never synthesize a birth or mean that a kernel disappeared. A future caller
must resolve all relevant unknown input for this scope before submitting positive
proof to recover that hold. Re-proving the identical retained pin clears an unknown
hold without appending a notice, changing an existing lease, or reviving ACKed data.

The scope enrollment retains its original binding. Changing a worker, VM or
incarnation for that logical session lifetime refuses; it does not automatically
re-enroll, clear history or revoke previously supplied actor objects. The current
binding authentication obligation above is essential. Even ordinary idle/resume
with a new incarnation needs a future explicit replacement/reconciliation policy.
This prerequisite offers no reset, lifecycle cleanup or guessed ordering across
those changes.

## Durable observations and delivery claims

A new known stale observation atomically updates its language watermark and
appends an immutable canonical JSON payload with a SHA-256 digest. Sequences are
monotonic **per enrolled scope**; `(scope, sequence)` is the identity, so another
session's activity creates no visible sequence gaps. Duplicate or older
revision/generation observations cannot append or revive a delivered notice.
A new proved current kernel advances the watermark without a stale notice. An
inconsistent identity at the same normalized generation is held explicitly.

Old pending birth notices remain claimable after a newer current birth is proved.
They are historical observations, not assertions that the old kernel still exists
or is currently stale. Future platform prose must preserve the original receipt
time, start/current revisions and birth pin, and recheck current state separately
if it describes current conditions. Unknown input never deletes those observations.

One MAIN run leases an exact oldest prefix. The receipt binds scope digest, run,
fresh random token and every sequence/digest; the payloads must match those pins.
Claims have finite deadlines. Competing SQLite connections admit one owner;
expired claims may be replaced. Renew/release/ACK require the exact still-live
receipt and whole claimed set. Partial ACKs, substituted payloads and old-owner
operations refuse atomically. Renewal does not widen a batch beyond its byte bound.

ACK is the caller's assertion that a successful model delivery and its run
were durably saved first. The module cannot verify that assertion and does not
call ACK itself. The integrated consumer must retain only batches from successful model
calls, fsync the saved run before ACK, and preserve cancellation/failed-save replay.
ACK atomically compacts only its exact items, retaining the sequence cursor,
per-language dedup watermark and one bounded last-ACK receipt. Last exact replay
has no side effects on a later claim. An older receipt displaced by a subsequent
ACK refuses; indefinite replay history is outside this prerequisite. The consumer RunRecord, rather than these compacted rows, retains delivery audit evidence.

## Explicit logical budgets

Default limits are validated strict bounded integers:

| Retained or admitted data | Default bound |
| --- | ---: |
| Enrolled scopes across the database | 256 |
| Language watermarks | 2 per scope |
| Unacknowledged notices | 64 per scope; 4,096 globally |
| Aggregate retained logical data | 8 MiB |
| Canonical notice / birth pin / scope | 4,096 / 3,072 / 1,536 bytes |
| Live receipt / last ACK receipt | 4,096 bytes each per scope |
| Complete canonical claim, including pins and envelope | 8,192 bytes; at most 16 notices |
| Finite lease | 30 seconds |

Aggregate accounting includes actual UTF-8/BLOB lengths of all retained keys,
scopes, pins, payloads and receipts, fixed scalar reservations, and a reserved
32-byte hold slot per scope. Oversized/invalid hold scalars refuse globally without
materialization. Counts and aggregate bytes are checked transactionally; no trusted
retained byte counter is used. SQLite page/index/WAL overhead is separate from
these logical data budgets. Configuration permits bounded smaller/larger limits;
tightening below retained usage refuses rather than evicting data.

Capacity pressure holds the scope without advancing its observation watermark or
sequence. Existing exact notices can drain when claim-receipt capacity permits.
If even a scope cannot be enrolled, a single reserved global capacity flag records
the gap; repeated unknown scopes cannot grow a second queue. That flag is sticky
historical gap evidence, not a current all-clear indicator. There is no automatic
expiry/eviction of unresolved notices, scope bindings or dedup watermarks. A
permanently full enrollment registry requires a future explicit retention policy.

Each synchronous method owns its SQLite connection and FULL/WAL transaction.
Input JSON is prepared before the transaction; retained reads use SQL byte prefixes
before parsing. Row counts, strict numeric columns, finite leases, receipt
structure, hashes and exact payload pins are validated. The asynchronous
consumer owns and drains actual thread/SQL work before store/context shutdown;
this module introduces no executor, background task or cancellation wrapper.

## Current MAIN consumer

The independently reviewed [explicit stateful MAIN consumer](KERNEL-NOTICE-DELIVERY.md) now implements authenticated control lifetime/configuration mapping, bounded owned passive reads, historical version-1 USER input and typed receipt/RunRecord evidence. Successful host return marks delivery; a real fsynced run and fresh original proof precede ACK. Failed first calls, failed saves and cancellation retain the qualified replay/audit boundary. Changed bindings, incomplete lineage or initialized inbox loss refuse rather than re-enroll. [OpenShell Lab29](../.agents/docs/agent-reports/babbage-openshell-lab-29-2026-10-13.md) passes actual worker/package/idle-resume acceptance and independent complete cleanup; final archive/PR CI delivery remains pending. No idle scheduler, automatic replacement reconciliation, delegate routing or full-history guarantee is added. The prerequisite evidence below remains at its original source pins.

## Historical prerequisite validation

The focused suite passes 45 tests. Strict module Pyright reports zero errors and
warnings; Ruff formatting/lint and the installed Python license check pass. No
full Python suite or runtime acceptance was run for this isolated prerequisite.

Tests cover reopen/dedup and ACK compaction, scope-local sequences,
competing claims, expiry/takeover/late ACK, exact whole-set validation, transactional
insert/delete faults, quota holds and actual-byte reservations, positive MAIN
scope, malformed/corrupt large retained rows and finite clocks. Synthetic trusted
pins prove store behavior only; they do not prove worker lineage authentication.

Root combined local gates on signed `840b48f` pass 10,155 Python cases/43 opt-in skips/two existing warnings and required static checks; unchanged PR #99 web/math/build evidence is explicitly reused. [Lab28](../.agents/docs/agent-reports/babbage-openshell-lab-28-2026-10-12.md) validates the separate passive worker lineage, not this inbox's MAIN delivery. Fresh delivery CI remains separate.

The separately reviewed consumer and Lab29 above now provide the MAIN safe-point and save-before-ACK evidence. The underlying store still trusts its caller's positive saved assertion; it is not a public verifier of arbitrary run files. Worker restart continuity and idle/resume refusal are actual runtime coverage; immutable-VM-ID replacement remains focused source coverage. Idle/offline wake, complete event history and task-worker routing remain separate work.
