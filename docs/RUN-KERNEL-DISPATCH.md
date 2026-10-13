<!-- SPDX-License-Identifier: MIT -->
# Internal child-kernel dispatcher

The internal source is independently reviewed at signed `28008e1`, integrated
with actual main at signed source-gate union `7619a4b`. Fresh combined gates pass
11,133 Python tests, 43 opt-in skips and two existing warnings in 388.54s, with all
required static, locked dependency, license and protocol checks. Existing 803
web/24 math checks and 76 accepted build files are explicitly reused through exact
inputs. Original failures and the interrupted predecessor full run remain
unsuccessful evidence. Root-owned [OpenShell Lab33](../.agents/docs/agent-reports/babbage-openshell-lab-33-2026-10-13.md)
passed on frozen signed `0961999` / helper `a3fa2086`, with independent runtime
and complete owned-cleanup clearance. Final delivery review and fresh PR checks
remain separate.

The dispatcher extends the [original binding prerequisite](RUN-KERNELS.md) for
[D81](DECISIONS.md). A child uses its canonical session's existing environment
and workspace, with a separate Python or Sage interpreter. It shares the guest
UID and files with MAIN and other children. Interpreter separation provides
neither filesystem isolation nor a security boundary between researchers.

No launcher, delegated model, WorkerClient method, HTTP submission route, model
tool, approval, grant, role profile or concurrency/depth default is added.
Existing whole-session closure still owns all its kernels. Ordinary project
reads of execution records are not a new child-private storage boundary.

## Original selection and admission

The internal `RunCellRequest` has a distinct `run_cell` discriminator, complete
`RunKernelBinding`, ordinary execution context and bounded cell values. It is
absent from the public `ExecutionRequest` union. Its canonical request digest
includes the full original selection. Public `CellRequest` serialization and
its historical defaults/None fields retain their existing bytes and digests.

The binding names the original project, session, environment, parent run,
delegation, child run, managed sandbox, immutable VM ID and daemon incarnation.
A trusted caller derives those values from its actual admitted controller and
original records. A DTO, operation ID, actor, execution ID or logical session
cannot manufacture that authority. Enrollment precedes the first child effect.

Effectful dispatcher calls require an explicit synchronous `admitted` callback.
The caller owns fresh project/session-open/grant checks. The dispatcher also
requires the original already-connected managed channel, retained endpoint,
original enrollment, supported daemon capability and absence of lifecycle,
recovery, recycling or quarantine fences. It rechecks relevant proofs across
awaits and at launch. Callbacks and object comparisons do not prove controller
authentication or an atomic lock across independent control processes.

## One manager and its internal interface

`ExecutionManager` owns one `manager.run_dispatcher`. Borrow that instance;
constructing another dispatcher would create a separate control-task registry
outside the manager's drain. The dispatcher borrows the manager's synchronous
Store connection and existing channel/lifecycle owners.

| Internal method | Purpose |
| --- | --- |
| `initialize()` | Explicitly create the dispatch namespace once, or validate the existing namespace without resetting it. |
| `submit(request, admitted=...)` | Atomically accept the original child cell and schedule that accepted attempt through existing worker machinery. |
| `get(binding, execution_id, attempt_id)` | Read the exact original selected attempt, retaining its immutable binding. |
| `cancel(binding, execution_id, attempt_id, admitted=...)` | Cancel only that selected original attempt and channel. |
| `restart(binding, operation_id, language, admitted=...)` | Persist a control intent and explicitly restart one child's selected language. |
| `close(binding, operation_id, admitted=...)` | Persist sticky child closure before guest cleanup. |
| `observe(binding)` | Read selected lineage through the existing passive observer lane. |

These interfaces are frozen at the reviewed source above. For a fresh owner,
start the ordinary manager, explicitly open the canonical managed session
through a MAIN cell, enroll its original child bindings, then initialize the
dispatch namespace before child submission. For a reopened owner, validate the
existing namespace through the same manager-owned dispatcher before
`manager.start()` recovers retained attempts and endpoints. Fully drain and close
owner1 before owner2 opens those stores. Initialization is not lost-history repair.

## Atomic acceptance and recovery

The worker adds an explicit `executions.kernel_scope` marker. Genuine legacy
rows retain their legacy marker and encoding. Selected child rows use `run-v1`;
their canonical envelope, original digest, attempt/header and selection record
are retained atomically before dispatch. A missing, corrupt, wrong-type,
truncated or contradictory selected marker never enters the MAIN mapper.
Migration must distinguish genuinely old stores from lost selected history.

Separate bounded tables retain selections, generations, births, control receipts
and sticky closures. New selected metadata projections validate original SQL
types and bounded bytes before UTF-8/JSON parsing, and compare retained canonical
columns with their records. Existing ordinary execution/event reads retain their
own paths; these metadata bounds are not a global execution/event or RSS bound.
Embedded NUL or coerced text must not normalize into a known marker, digest or
incarnation. Count and logical-byte accounting includes variable scalar fields
and original envelopes before admitting new rows; there is no silent eviction.
These bounds do not measure total SQLite page allocation, RSS or hostile
administrator rewrites of every durable record.

Recovery retains the original accepted request and attempt; it queries/replays
the original daemon journal rather than submitting another command. A healthy
same-incarnation bridge reconnect can preserve the original kernel state.
Missing proof, another daemon incarnation or a replacement VM cannot re-enroll
or attach the old selection to today's kernel. Historical records remain audit
evidence after eligibility disappears.

## Interpreter identity and lineage

The daemon advertises `run_kernel_protocol=1`. Dedicated `run_cell`,
`run_restart`, `run_cancel`, `run_kernels` and `run_close` operations carry the complete
original binding and its canonical key. Unsupported peers refuse child work.
The executor maintains a separate child-kernel map keyed by project, session,
child run and language; legacy MAIN dictionaries and two-language status DTOs
retain their original namespace.

Child IDs and birth/generation fields receive finite scalar validation before
aggregate encoding. Within the fixed original incarnation, child raw and
normalized generations coincide. Retained generation and birth must agree;
contradictions refuse rather than certify stale lineage. Actual start events
and explicit restart replies may retain an original birth and start-package
pin under existing start/revision transactions. Passive status cannot create a
birth or fill an unknown start revision from today's packages.

The ordinary CP-13/package admission, package epoch, queue/start fences, output
limits, shared-workspace handling and result lifecycle remain in force. The
dispatcher is initially cell-only: no child shell command, background job or
package operation is introduced.

## Cancellation, controls and closing

Public execution-ID cancellation remains legacy-only for selected child
attempts. The dispatcher and existing worker-owned timeout, watchdog, overflow
and recovery paths use an internal exact-selection cancellation path. Fresh
proof must certify the same channel actually used for cancellation; a different
open channel cannot stand in for the original attempt. Corrupt selection is a
refusal, not permission to cancel a MAIN attempt.

Restart/close intents are durable before their RPC. Replay checks the original
operation, action, language, canonical request and selected result identity.
A completed receipt is historical. A pending receipt means the effect is
unknown and is never automatically resent. Lost postreply proof keeps the
pending audit rather than erasing uncertainty or claiming noncommit.

Child close withdraws eligibility before remote cleanup and remains sticky on
failure or caller cancellation. Queued starts/restarts recheck closure at their
actual launch boundary. Closing A drains A's admitted cell/start/control owners
while preserving MAIN and B. Whole-session and daemon close still drain their
complete existing ownership scope.

An admitted control task retains its busy counter, channel and borrowed Store
through timeout/repeated caller cancellation until its owned host task/request
work settles. The manager drains its dispatcher before detaching channels or
closing Store owners. A transport timeout or pending receipt does not prove the
guest effect settled. Cancellation of an outer future alone does not establish
that SQL or a remote effect stopped.

## Passive observations and acceptance limits

Selected status shares the existing one-per-environment/global-eight observer
lane, close token and total deadline with MAIN observations. It opens no
channel, reconnects no daemon, wakes no stopped VM, refreshes no activity and
consumes no pending state-loss notice. It writes no provisional generation or
birth. Original managed identity is checked after network awaits, including an
empty kernel list. Closure invalidates affected observation ownership.

Lab33 exercised a bounded MAIN/A/B pilot followed by 19 cells, three child
controls and one whole-session close across two canonical sessions and three
generated children. Globals/shared files, exact cancel/restart/close,
same-incarnation host reconnect, MAIN/B absence with C preserved, actual idle
retirement, thirty no-wake reads, ordinary resume/new incarnation and original
VM deletion passed. Gateway Stopped observations bracket the reads; retained
SQL/control snapshots remain equal and the actual endpoint is `[]`. The reads
use the same initialized owner and trace no SQL statements. Finite bracketing
is not continuous VM-state or scheduling-fairness proof.

All owned units/processes/listeners/private state were removed and independently
checked. The helper receipt-schema correction and unactivated teardown ownership
finding remain separate preserved source evidence; the whole runtime ran once.
Component fault probes remain distinct from real OpenShell effects. This
acceptance proves no authenticated live delegation controller, delegated
model/handback workflow, scientific correctness, filesystem isolation, monetary
accounting, idle wake or successor/replacement policy. Those remain separate
plan items.
