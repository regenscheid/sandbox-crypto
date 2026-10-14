<!-- SPDX-License-Identifier: MIT -->
# Internal child turn journal

`child_attempts.py` retains a canonical child's explicitly supplied input and
bounded partial conversation before the explicitly configured
[child controller](DELEGATED-CHILD-CONTROLLER.md) performs work. This module adds
no model call, launcher, tool, worker transport, grant, policy default or active
delegated run. [DelegationRecords](DELEGATION-RECORDS.md) remains the sole source of
original parent/delegation/child identity and terminal raw/validated handback.
The [dispatcher](RUN-KERNEL-DISPATCH.md) and delegated own-track adapter are
separate consumers; this journal does not authenticate their callers or clear a
model-delivery fence.

## Original identity and explicit inputs

`ChildAttempts` borrows one existing `DocumentStore`, its canonical
`DelegationRecords` and a `ProjectStore`. `initialize=True` is an explicit first
setup operation only. Ordinary construction/read requires the existing namespace;
partial, unsupported or inconsistent state refuses. An entirely erased namespace
cannot prove that it never existed. A recovering caller must not use first setup
to repair lost history; it must retain original receipts and reconcile instead.

`AttemptAuthority` is trusted host attribution: original project/session/environment,
parent/delegation/child IDs and an explicit controller ID. DTO construction,
matching IDs or a callback returning `True` cannot establish authentication. The
caller must supply its actual controller authority independently. No model
argument, historical brief, profile or kernel DTO mints it.

`begin_attempt` binds the exact canonical prepared child, its original non-null
control-owned session lifetime, original held registry identity, explicit profile
catalog ID/digest/requested alias, original system, ordered tool schema strings,
initial logical messages and every applied budget ceiling. The requested profile
must match the canonical brief's profile intent; a non-null intended alias must
also match. Raw provider connection/settings configuration is not accepted or
hashed. Profile identity remains caller-supplied metadata, not proof that an
adapter applied settings or a provider resolved an alias.

`pin_session` is a bounded local continuity observation. It checks the original
open snapshot, current nofollow canonical root/file and exact session/environment.
The opaque existing session lifetime is continuity evidence, not authority.
New begin/reserve admission repeats those checks and the caller's exact-True
cooperative admission after entering the actual writer and before commit. Any
registry replacement, including an identical-byte replacement, conservatively
invalidates the original pin. There is no new USER approval requirement.

## Reservations, partial observations and historical facts

The journal allocates an attempt ID distinct from the genuine child ID. This
prerequisite permits one attempt per canonical child, with exact historical
operation replay. Another operation/controller cannot allocate a new attempt to
bypass unresolved work. Takeover, expiry, new-attempt reconciliation and automatic
resume are deliberately unwired; this is not a delegation concurrency/depth default.

`reserve_turn` checks exact head CAS, the immutable conversation prefix and original
single system message. It atomically charges one call, the explicit output-token
reservation and encoded retained messages plus ordered tool metadata before any
external await. Caller ceilings specify maximum calls, per-call/total reserved
output tokens, cumulative retained input bytes and wall seconds. These fields do
not silently reinterpret the brief's requested limits. UTC wall admission starts
at the durable begin; a backward or expired time refuses. Actual call deadlines,
adapter output limits and provider spend controls remain controller duties. The
current controller applies finite call/reservation/deadline checks; caller-applied
provider limits are a trusted composition assertion, and monetary enforcement is
unsupported.

There is one unresolved model reservation per attempt. `observe_turn` names that
exact reservation and retains either an assistant message with exact logical text
and requested call IDs/names/arguments, or a fixed unknown reason. Malformed tool
argument JSON is retained unchanged. Actual response model, finish reason and
usage remain nullable; the requested alias is separate. Reservations and host
observations prove neither provider dispatch/wire bytes, cost, model understanding
nor scientific success. Even reported low usage does not refund a reservation.

Timeout, cancellation, interruption or retention failure leaves the reservation
charged and held. A fixed unknown fact cannot mean “not sent” or authorize retry.
A later observed return from the same original reservation can append history;
it never reruns that call. Exact-retention refusal leaves the original reservation
pending; the host may record `retention_failed`, never silently call again.

`checkpoint` appends a complete bounded partial conversation without rewriting its
prefix. Explicit original unknown-effect pins are sticky: they may be added but
cannot be removed or cleared by this prerequisite. Their presence blocks another
reservation. Actual selected-worker reconciliation must later supply its own
coherent authenticated boundary; an ordinary tool message is not that proof.

`seal_local_attempt` retains one fixed host ending without implying an overall
delegation outcome or achieved goal. Late observe/checkpoint/seal facts require
the exact existing controller/attempt/reservation and append CAS, but can survive
session close, controller revocation, missing live registry, VM loss or a later
canonical handback. They remain historical, cannot reserve new work or revive a
grant, and do not mutate that handback. Any later `DelegationRecords.finish` is a
separate explicit controller action after durable partial state.

Historical replay returns the exact original event before live admission and
performs no new work. Changing body/model/profile/schema/owner conflicts. It
cannot recreate the own-track adapter's exact `GrantedRead` correlation, clear
USER changes or ACK MAIN notices. A fresh successful model call containing a
complete current granted read is still required by that adapter.

## Atomic storage and caller ownership

Each event binds original authority, local sequence/previous-record hash, exact
operation, request digest and original attempt. The same transaction writes the
event, per-child original-start/committed-tail anchor and global committed event/
child counts. Reads validate those anchors, canonical bytes and counts. Losing a
pending suffix, whole child history, its head or both refuses; it cannot appear
as an unused child or empty conversation. These checks detect retained corruption,
not a privileged writer deliberately rewriting all history and anchors.

All immutable rows, mutable heads and meta overhead count toward transactional
logical quotas. Each selected record is SQL-clipped to 512 KiB plus one byte;
scalar columns are original-type checked/capped before fetching. One attempt
loads at most 1,024 events and 4 MiB including its head; a model input/checkpoint
is at most 256 KiB and observation 64 KiB. Logical project/global bounds are
8,192 project event/head rows and 16 MiB; globally, 32,768 event/head/meta rows and
64 MiB. The shared meta row and its 512-byte reservation are charged globally.
Models additionally bound
messages, tools, fields, depth, integers, copied types and encoding expansion.
These are storage/admission caps, not an exact RSS, WAL/disk or provider-token limit.
Schema JSON is duplicate-rejecting and recursion failures become fixed refusals.

Methods are synchronous. The journal owns no thread, connection or original
snapshot descriptor and has no close method. A caller using `to_thread` must retain
and drain that actual work through cancellation before closing the borrowed store
or original snapshot. The existing store lock serializes its actual transactions
and close. New-work project locking covers only bounded local control work, never
a model or worker await; late facts need no live project lock. No whole-runner
cancellation identity, SIGKILL durability or automatic recovery claim is added.

## Validation scope

Focused deterministic tests use real temporary canonical control/project stores
and scripted host observations. They cover exact replay/owner/profile/scope,
single reservation and explicit budgets, nullable actual reply facts, malformed
arguments and invalid retention, historical post-close facts, sticky effects,
concurrent CAS, precommit admission/root replacement, postcommit lost ACK, quota
rollback, selected SQL/copy/decoder bounds, missing schema and committed-row loss,
plus actual caller thread/store-close drain through repeated cancellation.
They perform no model, worker, provider, VM, service or research execution.
Independent review, root integration/full gates and restricted child runtime
acceptance remain separate.
