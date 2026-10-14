<!-- SPDX-License-Identifier: MIT -->
# Original run/kernel binding prerequisite

> **Archived.** This code was removed from main; it is preserved on the tag `archive/delegation-2026-10`. See the [refactoring plan](../../refactor-plan/PLAN.md), decision 2.

This internal prerequisite records a controller-attributed child run's original
managed endpoint. It prepares for [D81](../../../docs/DECISIONS.md): children are runs in their
existing session, using its environment, workspace and grants, with kernels of
their own. It does not route a cell, start a child or kernel, or complete D81.

No worker HTTP route, WorkerClient method, tool, launcher, policy, approval,
profile loader or grant is added. `ExecutionContext`, existing MAIN kernels,
session observations, lineage keys and cancellation behavior are unchanged.
The draft delegation concurrency/depth and role defaults remain separate.

## Attribution and original identity

`RunKernelScope`
contains the explicit project, session, environment, original parent run,
delegation and child run IDs. Parent and child must be distinct and cannot
impersonate USER. All identifiers are bounded ASCII. The future caller must
derive these values from its genuine admitted controller context and original
records. Construction and validation establish neither authentication nor that
admission. Actor, operation, execution or synthetic session IDs cannot stand in
for the child run.

`RunKernelBinding` adds a complete original managed sandbox name, immutable VM
ID and daemon incarnation. It is an endpoint enrollment observation, **not** a
kernel birth, start revision, loaded state, successful dispatch or run-start
proof. Enrollment after an earlier unseen start cannot establish that earlier
start. A future dispatcher must enroll before its first child effect and retain
the binding alongside the original accepted attempt.

The version-1 binding key hashes compact sorted ASCII JSON containing the complete
binding under `run-kernel-binding-v1` followed by NUL. Receipt time and operation
ID are excluded from that identity key. The separate canonical request digest
includes the original binding and operation ID under `run-kernel-enrollment-v1`
followed by NUL. All fields are validated before aggregate encoding; copied extra
fields and model/scalar subclasses are refused before their callbacks.

## Internal methods and proof

[`ExecutionManager.enroll_run_kernel(scope, operation_id, initialize=False)`](../../../packages/worker/src/sandbox_science/worker/manager.py)
is synchronous and internal. Its new method is not called by existing APIs.
It requires an already connected, unfenced, unlocked, nonclosed/nondetaching
channel and an explicitly configured trusted managed VM identity. The single
bounded retained endpoint row must have original TEXT identity fields matching
that exact channel incarnation and managed name/immutable ID. Unknown,
unmanaged, corrupt, missing or contradictory identity refuses.

The method performs no await, RPC, CLI, reconnect, wake, environment/kernel
creation, lifecycle lock acquisition or activity/busy-counter update. It
rechecks the exact environment/channel objects, incarnation, retained endpoint
and trusted managed identity before and after the enrollment transaction, and
after commit. A failure inside the transaction rolls back its record and first
schema creation. A postcommit failure returns unavailable while retaining the
original audit; it does not erase, re-enroll or repeat possible effects.

A readable CP-13 package hold does not prevent this metadata observation.
Enrollment grants no package admission exemption: a later dispatcher must apply
ordinary CP-13 and controller/session admission before actual launch. Receipt
time is a trusted host observation, not an atomic external VM-health or
filesystem snapshot and not an opaque session-open lifetime.

`RunKernelBindings`
borrows the canonical worker `Store` connection and transaction seam. Its lower
level `enroll(binding, operation_id, initialize=False, validate=None)` stores
explicit trusted attribution. It cannot authenticate endpoints by itself;
the manager supplies its synchronous fresh-proof callback. `get(scope)` reads
one exact historical scoped receipt without requiring or inventing a current
endpoint. Historical reads confer no eligibility to use that VM or a kernel.

## Durability, replay and bounds

One immutable row contains the complete binding, required explicit format
version, operation/request digest, binding key and trusted UTC receipt time.
The original child cannot change session, environment, parent, delegation, VM
or incarnation. A delegation or parent-scoped operation cannot alias another
child. A different project's scoped read sees no record. Exact replay returns
the original receipt unchanged, including its time; changed requests conflict.
Worker reconnect on the same original managed incarnation may replay the
existing binding. A newer incarnation or replacement VM cannot backfill it.

Namespace creation occurs only during explicitly requested first enrollment,
inside the same transaction as its record. Ordinary Store startup and legacy
execution paths neither initialize nor read it. Missing default namespace is
not found; partial/unsupported schemas and corrupt records refuse. Complete
erasure of every namespace table cannot be distinguished from prior absence
without an external retained initialization proof. `initialize=True` is not
that proof and must not become automatic lost-history recovery.

| Engineering bound | Limit |
| --- | --- |
| Identifier | 128 ASCII characters |
| Canonical receipt | 4 KiB |
| Retained bindings | 256/project; 1,024/global |
| Logical retained bytes | 2 MiB/project; 8 MiB/global |
| Historical read | One selected bounded row |

Every stored variable column, full receipt and fixed row reservation contributes
to transactional accounting; one fixed metadata reservation counts globally.
Count ceilings are checked before the bounded aggregate pass, and scalar byte
charges are type/range guarded before summation. A full quota or invalid charge
refuses a new binding. There is no eviction, overwrite, rebinding or ID reuse.
These logical limits do not measure SQLite page/index allocation, RSS or
arbitrary external database growth. Immutable triggers and canonical coherence
checks do not authenticate against an administrator rewriting all database state.

Selected SQL type/byte projections precede Python parsing. Empty/wrong-type or
overlong endpoint and receipt fields cannot normalize into known provenance.
Retained duplicate JSON keys, mismatched columns/request/key/charge and malformed
versions refuse. Damage to this unwired namespace does not enter ordinary
legacy Store startup, execution or kernel generation reads.

The component owns no thread, async task, connection or close method. The
existing synchronous Store transaction owns admitted work. A future async
adapter must serialize use of the borrowed connection and retain/drain actual
queue, SQL and file ownership through timeout, repeated cancellation and
shutdown. Cancelling a future is not proof that SQL stopped. No new async
ownership or cross-request session-lifetime guarantee is implemented here.

## Companion internal dispatcher

The [internal child-kernel dispatcher](RUN-KERNEL-DISPATCH.md) has independently
reviewed source `28008e1`, integrated at `7619a4b`, with fresh 11,133 Python and
required static/locked/license/protocol gates passed. Its root-owned
[Lab33 acceptance](../../docs/agent-reports/babbage-openshell-lab-33-2026-10-13.md)
and complete independent cleanup are clear; final delivery/fresh CI remain
separate. This enrollment prerequisite alone still performs no dispatch. Its
companion implements these run-aware paths together:

1. A trusted admitted controller supplies the original child scope and enrollment
   before dispatch; fresh session-open/project/grant checks remain caller-owned.
   Legacy MAIN requests retain their existing namespace. No model parameter can
   manufacture controller authority or reuse another child's binding.
2. Negotiate a distinct executor run-kernel protocol and refuse legacy peers
   before accepting child work. Carry the exact original enrollment/attempt and
   VM/incarnation through recovery; unknown admission or effect is reconciled,
   never silently repeated or attached to today's endpoint.
3. Route a child to a `(project, session, child run, language)` interpreter using
   the existing session workspace/environment. Use separate bounded run
   generation/birth records, preserving raw and normalized generation and exact
   original birth/start revision. Sharing workspace and uid establishes no new
   filesystem or security isolation between children.
4. Bind cell state/result, restart and child-only close/cancel to that exact run
   and original incarnation. Existing execution-ID cancellation stays exact;
   parent/sibling kernels survive child cancellation or restart. Session close
   still stops every run. CP-13 launch gates and kernel-start transactions remain
   mandatory, including cancellation while queued or starting.
5. Add a separate bounded run-aware status/lineage projection and passive
   no-wake checks. Preserve the <=2-language legacy MAIN GUI/notice response and
   keys until an explicit consumer increment is reviewed. No provisional birth,
   current-package inference, activity refresh or state-lost consumption occurs
   during passive reads.

Focused tests use disposable local Store databases and constructed channels
that never start. They cover original identity, immutable replay, wrong scopes,
strict copied inputs, quota/transaction rollback, real commit followed by lost
acknowledgment, concurrent first enrollment, bounded corrupt reads, same-original
incarnation replay and changed/unknown managed proof. No service, model, RPC,
research kernel, VM or browser is started by this prerequisite's author. Root
integration, independent review, combined gates and later real OpenShell D81
acceptance remain separate.

Author validation on Python 3.13: 217 affected tests pass, including all 44 new
record/fake-channel cases and the existing contract suite. Whole contracts/worker
source plus the new tests type-check with zero errors/warnings. Full-repository
Ruff and format, locked dependency sync, 75 installed license checks and protocol
generation checks pass. Initial style/type diagnostics remain retained separately;
the initial 38 focused cases and later 43-case run were successful predecessor
runs, not substitutes for the final affected output. No full-repository Python
suite or real kernel/VM acceptance is claimed here.
