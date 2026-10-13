<!-- SPDX-License-Identifier: MIT -->
# Explicit MAIN kernel-notice delivery

Work in progress, 2026-10-13. This coherent slice connects the existing
[passive lineage](KERNEL-NOTICE-LINEAGE.md) and [durable inbox](KERNEL-NOTICES.md)
to explicitly started stateful MAIN runs. Its initial checkpoint implements the
control lifetime and private configuration binding only. Controller, model
delivery, durable run save/ACK wiring and end-to-end qualification are still
pending; this checkpoint does not claim CP-12 MAIN delivery.

## Control provenance prerequisite

`SessionRecord.kernel_notice_lifetime` is an optional opaque token. New sessions
and every real reopen mint a new token under the canonical project lock,
independent of timestamp equality or clock rollback. An already-open legacy
session gets a present-time baseline before notification enrollment, never a
claim about its historical lifetime. The new bounded baseline path rejects
special/symlink registry and lock files, retains original registry descriptors,
and fsyncs the record and directory before a dependent snapshot/RPC. Closure
makes that lifetime ineligible without clearing old inbox data. Existing USER
plan-body editing remains logical-session CAS; no retrospective cross-request
lifetime guarantee is granted to that endpoint.

`KernelBindings` retains one immutable random binding ID per logical
project/session, independently of lifetime rotation. A private random 32-byte
key, created exclusively with mode0600 and file/directory fsync, authenticates a
versioned exact approved endpoint/authorization configuration. HMAC also binds
the logical project/session and opaque binding ID. Only a keyed digest is
persisted; no raw credential, unkeyed credential digest, key or private path may
enter a model notice, run record or error. A missing, malformed, replaced or
unsafe key against existing state refuses. Concurrent initialization is
serialized with a bounded canonical private lock. No reset or credential
rotation/replacement enrollment is offered.

The private strict SQLite registry has at most256 logical scopes and128KiB
logical retained data, including keys/IDs, actual pin bytes and fixed metadata
and diagnostic reservations. Empty rows count. SQL bounds precede retained
scalar allocation. Immutable binding columns cannot be updated/deleted by the
application; one fixed diagnostic slot records unavailable/unknown proof without
growing a queue. Database page/index/WAL overhead is separate from logical data.
Nofollow regular private files, mode/owner/link checks, zero SQL busy timeout and
observed file/root identity checks refuse unsafe state. Native SQLite still
opens its canonical path; there is no claimed OS-atomic defense against an
external host replacing the database during its initial connection opening.

## Confirmed delivery and cancellation boundaries

The eventual controller must prove exact original binding before first notice
injection and track successful model return separately from attempted input.
Already successfully delivered dated transcript context is historical data and
may remain on later ordinary calls. Unavailable notification proof cannot block
unrelated MAIN/tool work. ACK requires fresh exact original-binding proof after
durable run save. No automatic VM/incarnation replacement enrollment, restart,
tool grant, system-prompt version, extra closing call, scheduler or idle wake is
authorized by a notice.

Cancellation before save admission starts no save/ACK. Cancellation after an
owned off-loop save has been admitted may leave a durable saved-but-unACKed
record. The owner must drain actual work, propagate the original interruption
and perform no ACK; it cannot delete audit data or claim rollback. Renewal owns
historical claim data and current control lifetime/config only, not continuous
live VM/kernel proof. It must not queue behind long save/fsync work and cannot
promise liveness over blocked I/O. Existing other-notice save-before-ACK and
scientific outcomes must remain intact.

## Initial local validation

The prerequisite checkpoint passes47 focused existing-project/new-binding
cases, plus scoped Ruff/format and strict source/new-test Pyright. Tests cover
equal-time lifetime rotation, legacy baseline, special/oversized files,
concurrent private initialization, stable/replaced configuration, private-key
loss/corruption/permissions, bounded20MiB corrupt pins, scope substitution and
row/byte quotas. Initial style/type diagnostics are retained separately from
corrected passing logs. No provider/model/service/worker/VM or broad suite was
run. Root owns independent review, combined gates, runtime acceptance and
delivery; wiring remains incomplete at this checkpoint.
