<!-- SPDX-License-Identifier: MIT -->
# Controller-only selected-run transport

This source slice connects authenticated HTTP/client calls to the existing internal
child dispatcher. It enables no launcher, model, model tool, default profile,
approval, concurrency/depth/spend policy or automatic child activation. Actual
integrated controller/OpenShell acceptance and delivery belong to root integration.

## Authority

`ClientSettings.run_controller` defaults to false. The pool token and ordinary
project tokens do not inherit it. A deployment must explicitly configure a
project-scoped controller client; `run_restart` is a separate false-by-default
permission and requires `run_controller`. A trusted controller derives original
parent/delegation/child identity from its canonical control database and checks
current session/grant/configuration before sending each new operation. DTOs,
prompts, operation IDs and bearer project access cannot manufacture that proof.
The worker authenticates the configured controller and project/environment
ownership; it does **not** independently authenticate the remote control DB.

The private worker registry retains the exact configured controller identity and
original enrollment receipt. This identity includes the existing configured
credential digest and permissions; it never leaves the worker in responses,
model inputs or error messages. Configuration or credential changes refuse
historical ownership rather than implicitly re-enrolling it. Endpoint/VM/daemon
replacement cannot change a retained binding. Fresh enrollment needs an already
connected, healthy managed peer with exact selected-run protocol support; it
never calls a MAIN cell, starts a session, opens an executor or wakes a VM.

The registry has a committed incomplete initialization marker, an anchored owner
count, a 1,024-owner/eight-MiB logical metadata bound, and no deletion/eviction
operation. Partial/missing/incoherent state holds selected ingress while ordinary
MAIN routes remain available. Initialization refuses existing unowned run-binding
history. Complete coordinated erasure of every relevant store marker is not
proved detectable. This is retained-state coherence, not malicious-host integrity.
Enrollment owner insertion shares the existing original-binding transaction.

## Exact API

Every route is a controller-only `POST /v1/run-kernels/<action>`, accepts no query,
and returns `Cache-Control: no-store`, including errors. Unknown fields, duplicate
JSON keys, nonfinite values, wrong types and overlarge input refuse. Contracts
live in `contracts/run_api.py`; public `ExecutionRequest` stays unchanged.

| Action | Request | Response |
| --- | --- | --- |
| `enroll` | original `scope`, `operation_id` | `RunKernelEnrollment` with worker-derived binding |
| `submit` | distinct `RunCellRequest` | `RunCellExecution`; 201 new, 200 exact retained replay |
| `get`, `cancel` | `binding`, `execution_id`, `attempt_id` | exact `RunCellExecution` |
| `events` | same original attempt plus `after`, `limit` (1–32) | `RunEventPage` with selected execution, contiguous events, actual `next_after` and retained `last_seq` |
| `observe` | `binding` | passive `RunKernelObservation` |
| `close` | `binding`, `operation_id` | `RunControlResult` (`pending` or `done`) |
| `restart` | close fields plus exact language | `RunControlResult`, only with explicit restart permission |

The original complete binding and retained owner are checked on every selected
call. New effects use the dispatcher's original channel/VM/incarnation/closure
proof and a live callback tied to the authenticated controller, its original
scope and ingress shutdown/withdrawal state. Close writes the existing sticky
child tombstone before guest cleanup. Exact control replay returns the historical
pending/done receipt; it never resends an uncertain pending control. Historical
get/enrollment receipts do not certify current eligibility. Existing whole-session
closure retains its ordinary authority over all session kernels; this transport
adds no filesystem isolation or child-private artifact/storage claim.

Legacy execution get/events/cancel routes refuse selected records by bounded
marker **before** using the ordinary record reader, even for the pool token.
Legacy submit already refuses adopting selected operation rows. Knowing a selected
execution ID therefore cannot bypass original binding/controller checks. Ordinary
MAIN request serialization, operation digests and execution behavior are unchanged.

## Bounds, ownership and unknown outcomes

Requests are at most two MiB, with a five-second body deadline. Selected managed
environment authorization also preflights its retained document type/512-KiB
byte size before strict decoding; it does not use the legacy unbounded reader.
Existing selected cell code remains at most 256 KiB. Each response/event page is at most 512 KiB;
large retained executions/events refuse rather than materialize the older general
execution allowance, truncate a record or pretend it is empty. Pages consume an
actual contiguous prefix and may contain fewer than the requested count to fit
the aggregate bound. An unpageable first event, missing sequence or bad selected
row holds. No complete-global event-store or RSS bound is claimed.

The ingress admits at most eight actual requests, two per configured principal
and one per exact child scope; there is no queue. These are physical resource
caps, not model/delegation policy defaults. Actual request/body/RPC owners and
slots survive timeout, repeated cancellation and shutdown until their work
settles. SQL remains synchronous under the existing worker Store owner; no new
thread/connection/file owner is invented and blocked filesystem I/O has no hard
liveness guarantee. Passive observation uses the existing no-activity lane.

`WorkerClient` exposes `enroll_run`, `submit_run_cell`, `get_run`, `run_events`,
`cancel_run`, `observe_run`, `close_run` and `restart_run`. Responses must match
exact original scope/attempt/operation/action and strict bounded schemas. Selected
requests admit at most eight physical client owners, never redirect or fall back to
legacy routes. The transport defaults to a finite ten-second request budget, with explicit caller `timeout_seconds` up to
190 seconds; this is not a role/model budget. Submission never automatically
retries. Caller cancellation and client close drain actual selected HTTP/body
owners, including ignored cancellation, before the owned HTTP session closes.

Logout/revocation/close racing an already admitted remote write can leave a
committed but unacknowledged effect. A transport timeout or interruption proves
neither rollback nor guest termination. The caller must retain uncertainty and
reconcile an exact original receipt/attempt; it cannot blindly retry with a new
operation, replace a binding or claim a scientific outcome. No project/control
lock spans a network/model await. Live controller/model enforcement, canonical
child-attempt recovery and finite actual-worker acceptance remain separate.

## Worker shutdown and reconnect

Normal ingress shutdown withdraws only its process-local new/queued admission and
drains actual request owners. It writes no closure/tombstone, deletes no enrollment
and sends no `run_close` or VM stop. Existing `ExecutionManager.close` is unchanged:
it detaches daemon bridges and preserves eligible dispatched daemon attempts;
queued/unrecoverable attempts can become LOST under the existing rules. A new app
with the same configured principal can retain the original enrollment and read its
historical attempts. New effects still need the exact original managed VM/daemon
proof; changed configuration, incarnation or VM cannot silently re-enroll. An
explicit earlier child/session close remains sticky across that restart.

Unit composition checks app shutdown/manager detach and original enrollment
reopen with synthetic channels; it does not prove live daemon continuity or model
resume. Root-owned real HTTP/controller/OpenShell acceptance must prove that
continuity. Passive selected observation and historical reads perform bounded auth
and retained SQL reads but no data writes or wake/activity action. No zero-all-SQL
or continuous-liveness claim is made.
