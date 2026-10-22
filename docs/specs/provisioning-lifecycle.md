<!-- SPDX-License-Identifier: MIT -->
# Provisioning lifecycle ownership

Status: accepted implementation contract for refactoring review item 8, 2026-10-22.
This preserves [Conda provisioning](conda-provisioning.md); it adds no worker API,
record field, policy authority, or provisioning action.

## Owners and results

`Provisioner` is the single operation coordinator. It owns admission, idempotent
submission, durable record updates and waiter notification, environment queues,
operation tasks, startup classification, terminal recording, cancellation, and
drain on close or environment removal. Only the coordinator updates an operation
record. Collaborators request updates through its update callback.

Stage execution resolves and decides the requested files, executes sandbox steps,
and records installer artifacts. Permission interaction owns exact-file requests,
answer polling, deadlines, withdrawal, and retries of withdrawals owed by ended
operations. Post-install reconciliation owns revision observation, recovery
classification, package changes and capability/version checks. These are concrete
provisioning collaborators, not a reusable workflow framework or mixins.

The stage runner returns a completion value containing outcome, reason, stage,
error, next step and record updates. A successful installer returns the before and
after revisions together. Permission approval returns the request identity.
Reconciliation returns a completion value; early denials and failed steps use the
same completion representation. The coordinator alone turns it into a terminal
record and wakes record waiters.

## Ordering and ownership

1. Persist a new queued record before scheduling its task. Replayed identities
   return the existing operation and do not enqueue again. One environment runs
   one installer at a time, in request order; queued cancellation never installs.
2. Persist each state transition before notifying waiters. Policy and recovery
   admission are rechecked when the operation takes its turn, and before install.
3. Resolve before authorizing. Permission and middleware dispatch retain the exact
   resolved file set, policy digest and operation identity. Approval never widens
   access to files discovered later by an installer.
4. Close permission requests before persisting the terminal record. Preserve the
   existing owed-withdrawal behavior when the permission store is unavailable.
5. Release the queue turn after terminal classification, before waiting for the
   terminal observation append. A following operation may run while an earlier
   terminal observation is draining.
6. Queue release does not release task or store ownership. Keep the operation
   discoverable through close/forget and retain its task until terminal append
   completion. Begin retention eviction only after the task drains. Close and
   forget wait for actual append work through repeated cancellation.
7. Restart classifies every unfinished operation before synchronizing package
   admission. An unseen interrupted install still requires recovery; queued work
   cannot clear a recovery mark established by another unfinished install.
8. Environment removal requires durable terminal classification. A failed write
   is retried on a later forget; finished coroutine state alone is insufficient.

## Verification

Public characterization exercises terminal visibility, queue progress while an
append blocks, and close/forget retention of the writer. Existing coverage for
queue order, cancellation, startup, recovery holds, installer records, upgrades,
permission denial/expiry/withdrawal, and exact-file authorization remains required.
Tests use synthetic local package managers and temporary stores, without real
package registries, OpenShell services, credentials, or model calls.

No open questions affect this behavior-preserving extraction.
