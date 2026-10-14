<!-- SPDX-License-Identifier: MIT -->
# Explicit MAIN kernel-notice delivery

`KernelDeliveryController`
([kernel_delivery.py](../packages/orchestrator/src/sandbox_science/orchestrator/kernel_delivery.py))
tells a MAIN run, before a model call, that one of its session's kernels started
before the environment's latest package revision (CP-12). It connects the worker's
[passive lineage](KERNEL-NOTICE-LINEAGE.md) and the [durable inbox](KERNEL-NOTICES.md)
to explicitly started stateful MAIN runs: the CLI installs it when a run has a
`--state-dir` and a recorded project and session. It adds no public route, tool,
default grant, scheduler, system-prompt version, restart or extra closing round.
Stateless runs get no kernel notices. Scientific outcomes, plan fences and other
notice delivery and ACK rules belong to their own owners.

## Control provenance and private state

`SessionRecord.kernel_notice_lifetime` is optional for legacy records. New sessions
and each real reopen mint an opaque token under the canonical project lock,
independent of timestamp equality or clock rollback. An already-open legacy
session gets an explicit present-time baseline before enrollment. This is not a
claim about its historical lifetime. Baseline work retains bounded nofollow
registry descriptors and the canonical lock, then fsyncs the record and directory.
It finishes before planning pins its dependent registry snapshot. Closed sessions
cannot baseline or enroll. Existing USER plan editing remains logical-session CAS;
this token grants no retrospective cross-request lifetime fence to that endpoint.

`KernelBindings` ([kernel_notice_bindings.py](../packages/orchestrator/src/sandbox_science/orchestrator/kernel_notice_bindings.py),
`kernel-notice-bindings.sqlite3` and `kernel-notice.key` in the state directory)
retains one immutable random binding ID per logical project and
session, independently of lifetime rotation. A private random 32-byte key, created
exclusively with mode 0600 and file/directory fsync, authenticates the exact trusted
endpoint and authorization configuration. Its domain-separated HMAC also binds
the logical scope and opaque ID. Only the keyed pin is persisted. Raw credentials,
unkeyed credential digests, keys and private paths are excluded from notices, run
receipts and fixed notification errors. Configuration character bounds precede
UTF-8 encoding; malformed encoding is sanitized. Changed configuration refuses
without repinning. There is no reset, credential rotation or replacement enrollment.

The private strict registry has at most 256 logical scopes and 128 KiB of logical
retained data. Actual key/ID/pin bytes, empty rows, fixed metadata and diagnostic
reservations count. One fixed diagnostic slot per binding cannot grow a queue.
SQL type/byte checks precede retained scalar allocation. SQLite page/index/WAL
overhead is separate from logical quotas. Missing registry state with a retained
key, inbox or either database's WAL/SHM/journal file refuses before recreation.
Missing, partial, corrupt, replaced or unsafe private keys also refuse. A canonical
private lock serializes initialization, with a finite admission wait.

Inbox initialization commits an `initializing` registry phase before creating its
file or schema. The same private lock remains owned through schema creation,
file/directory fsync and the final `ready` device/inode pin. A crash or failure in
between remains held; no surviving file is guessed to be a completed initial state.
Concurrent first initializers observe one completed identity. Later openings
require the exact original regular private file and SQLite header, and use
`KernelNotices(initialize=False)`: no schema creation or repair occurs. Missing,
empty, replaced or dropped-table state cannot silently become an empty inbox.
The ordinary inbox constructor's default initialization behavior is unchanged.

Nofollow/nonblocking regular-file, owner, mode and link checks cover private files
and present sidecars. Original root/file descriptors and observed identities are
retained. Native SQLite still opens its canonical pathname; this is not an
OS-atomic defense against an external host replacing files during connection
opening, removing all private evidence or tampering with SQLite internals.

## One bounded historical input

`KernelDeliveryController` owns the original trusted configuration and a separate
passive client through run save and ACK. Before the first injection it requires a
complete bounded lineage response, exact project/environment/session, known
original start revision and retained VM/incarnation/raw birth plus normalized
generation. An original open registry snapshot spans the passive await and is
rechecked under the canonical project lock before synchronous inbox work. All
response entries validate before any positive admission. A capacity hold stops
later entries from erasing that hold; existing admitted notices may still drain.
Missing, malformed, empty, unknown or changed evidence admits no new notice input.
No guessed scope or current-endpoint backfill is used. VM/incarnation replacement
is held rather than automatically enrolled.

At most one complete historical prefix, including its prose and JSON, is injected
per run, with an 8 KiB total bound. Extra observations remain queued for a later
explicit run. The prefix is USER-message data, grants no authority, and leaves the
system/base/planning/stock/pilot text and ordered tool specifications untouched.
A successful host `model.complete` return marks this exact input delivered. A
failed or canceled first call has no nonnull receipt. After a successful call,
the dated input may remain in later ordinary transcript calls even when current
notification proof becomes unavailable; unrelated MAIN/tool work continues.
This records the host success boundary, not external model semantic consumption.

`RunRecord.kernel_notice_delivery` defaults to null for legacy/undelivered runs.
A nonnull receipt has an explicit exact-integer version 1 and the exact claim.
The immutable v1 prose, keys and canonical JSON formatter preserve historical
input independently of the installed default prose alias or model dump formatting.
A future format must add a new version while retaining the v1 reader/formatter.
Run validation requires matching run/scope and exactly one canonical injected
USER message. Missing, changed, wrong-role or duplicate inputs refuse. Copied host
metadata is revalidated under finite shape/character/byte limits: undeclared
model fields, unknown model types/subclasses and noninteger versions refuse before
subtype getters or unbounded encoding. Scalar/container/datetime subclasses also
refuse before overridable length, iteration, encoding or time formatting. These
are host-retention integrity checks,
not an assertion that arbitrary model objects are an authenticated network source.

## Ownership, cancellation and save-before-ACK

Passive proof has a six-second caller budget and one actual outstanding proof per
run. Timeouts cancel the proof coroutine but do not discard blocked file/thread
ownership or start overlapping work. Unknown notification state never vetoes
ordinary tools. There is no lifecycle keepalive, active-status fallback or worker
execution in the observation path.

Each controller owns finite private one-thread lanes for registry/inbox work and
run saving, with at most four pending jobs per lane. Cancellation drains actual
admitted work and retains its descriptors and slots. Queue cancellation before
save admission starts no write or ACK. Cancellation after an off-loop save starts
may leave a real durable saved-but-unACKed record; the owner drains it and propagates
the original interruption. It does not delete audit data or claim rollback.
Repeated cancellation and cleanup-origin interruptions preserve the first owned
interruption while closing the passive client, files and executors.

Claim renewal uses a separate lane from save/fsync. It proves current control
lifetime/configuration and historical claim ownership, without a live worker RPC.
Observed closed/replaced scope stops renewal. It grants no live VM continuity or
hard liveness guarantee over blocked I/O. Existing expiry/takeover and exact ACK
receipt rules remain those of the inbox.

The CLI calls the existing durable run save first. Only a positive return after
file/directory fsync enables fresh exact original-binding proof and kernel ACK.
The internal `acknowledge_saved` method trusts this caller assertion; it is not a
public saved-file verifier. Failed proof leaves the saved audit and unACKed notice
intact. Failed saves and canceled admitted saves start no kernel ACK. Existing
other-notice ACKs keep their save-first ordering; cancellation observed before
that phase skips them. An ACK already admitted before a later interruption may
finish, and no rollback is claimed. Cleanup owns all actual work through completion.

## Tests and scope

Scripted-model, store and CLI tests cover successful and failed calls, actual
fsynced run round trips, earlier success followed by failure, unchanged tool and
system inputs, historical context under unavailable proof, fresh-proof ACK refusal,
close/reopen, configuration and VM changes, private registry loss and all sidecar
families, partial and concurrent initialization, bounded 20 MiB corrupt values,
copied metadata, cancellation before and after admission, repeated cleanup and
renewal during a blocked save. Delivery through a real worker, including
failure, cancellation, lifetime refusal and idle stop with ordinary resume, was
checked on OpenShell in
[Lab29](../.agents/docs/agent-reports/babbage-openshell-lab-29-2026-10-13.md).

There is no idle scheduler or wake, no automatic reconciliation after a VM or
incarnation replacement, no routing to delegated runs and no complete notice
history.
