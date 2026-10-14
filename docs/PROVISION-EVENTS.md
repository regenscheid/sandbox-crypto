<!-- SPDX-License-Identifier: MIT -->
# Provisioning observations in the event store

The worker now attempts three bounded observational event kinds in its existing
configured policy event store: `provision.requested`,
`provision.awaiting_permission` and `provision.finished`. This implements that
portion of [CP-16](specs/conda-provisioning.md). It adds no route, setting, GUI,
grant, resolver/install command, recovery decision or operator authority.
`environment.revision_created`, `environment.unmanaged_change` and
`probe.finished` are outside this slice.

The source label is `worker`. Event-store filesystem access remains the writer
boundary; this label does not authenticate an operator. The observation-only
writer accepts those three kinds and a fixed payload schema, including an
explicit `observation-only` authority marker. It cannot append approval/denial
kinds through that interface, and the existing approval reader ignores these
observational kinds. Existing permission/withdrawal behavior is unchanged.

Each payload is at most 4 KiB: original provision/request operation and
project/session/environment identities, original operation sequence, state,
stage, outcome, reason/error kind, nullable revision and permission references,
source timestamps and bounded file/probe/execution counts. The current stage
can be derived from the saved nonterminal state; terminal stage stays as saved.
Nullable revisions/outcomes remain unknown. These facts do not infer whether an
unknown effect happened. Purpose, package specs, notes, free error messages,
next steps, paths/URLs and diagnostic output are omitted, and their raw content
is not hashed. The stable event ID hashes only a fixed format domain, kind,
original provision ID and saved sequence. Exact primitive/type/count checks
precede aggregate encoding; prepared payloads are independently copied.

The internal projection/emit API receives a trusted caller assertion that its
record was saved. It does not independently authenticate the caller or cross-read
the worker database. The production Provisioner supplies its canonical saved
record at these fixed emission points.

Events follow saved worker records: requested in the original owned run before
its turn; awaiting permission after the actual permission request/state is saved;
finished after a canonical terminal save. Fresh startup classification of an
unfinished operation emits a finished observation marked
`startup_classification`; it does not backfill a missing request/history. Exact
submission replay and a second startup of an already finished operation emit
nothing again. Permission wait deadlines are captured before the observation
await, so writing the observation does not extend the existing wait.

Worker records and the event store are separate databases. This is partial
observational coverage, not an atomic outbox or exactly-once history. A crash,
projection refusal, saturated writer, absent configuration or unavailable store
can leave no event. An append can commit before its acknowledgment is lost;
its outcome then stays unknown and this producer never retries it. Missing
observations never prove that an operation or permission request did not run.
The canonical operation remains authoritative. Failures generate fixed warnings
without raw exception text and do not relabel installer/recovery outcomes.

At most eight observation append owners are active. Their actual threads remain
owned through repeated caller cancellation and shutdown before the borrowed
Permissions/EventStore writer may close. The observer never closes that writer. A terminal operation releases its environment queue turn before awaiting the final observation. Its task remains registered for environment forget and shutdown until the actual append drains; the normal retention timer begins afterward. Final observations from different operations can therefore arrive in a different order from their canonical transitions.
The existing event-store five-second lock timeout is not a hard fsync/thread
termination deadline: observation waits and cleanup can last longer while
admitted host work drains. Per-record/in-flight bounds do not establish a total
event-store disk quota, bounded global history readers or whole-worker lifetime
ownership. Existing event-store retention, chain/checkpoint guarantees and
permission-writer ownership still apply.

Component checks use disposable real EventStore/worker SQLite files, blocked
synthetic threads and scripted provisioning/fake-package fixtures. They check
hash-chain validity, safe omission/bounds/strict primitives, no approval answers,
original transition sequences and exact replay, startup qualification, ambiguous
commit acknowledgment without retry, original failure classification, owner
caps, first cancellation identity and actual close/thread drain. These checks
use no real installer, OpenShell environment or model. Independent source review,
root combined gates and real runtime acceptance remain separate.

[OpenShell Lab35](../.agents/docs/agent-reports/babbage-openshell-lab-35-2026-10-13.md) passed on signed runtime `d236b6b`/helper `fbbe3b61`, with independent complete teardown at2026-10-14T03:52:41.221801Z. The small scalar pilot preceded two installs; six original-step artifacts and four requested/finished observations matched the selected production APIs. Observed micromamba2.9.0/pip26.2.1 remain self-reports; no genuine awaiting-permission transition occurred. The slice is delivered as [PR #121](https://github.com/regenscheid/sandbox-crypto/pull/121), merged at `d2fe8fa` on reviewed signed `1f9698a` after all four fresh checks passed (run `38510468628`). Primary main is clean, locked and tree-identical to that reviewed head.
