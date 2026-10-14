<!-- SPDX-License-Identifier: MIT -->
# Installer observations and sanitized log artifacts

New provisioning operations retain `installer_steps`: the original version,
resolve and install step operation IDs, accepted execution/attempt IDs, known
terminal state, nullable observed version, and pending/stored/unavailable
artifact metadata. Legacy records without this field remain unknown (`null`).
This implements the installer observation and partial log-artifact portion of
[CP-16](specs/conda-provisioning.md), using existing project-scoped artifact reads.
It adds no route, grant, UI, policy setting or permission decision.

Before resolving a Conda or pip request, the provisioner runs exactly one fixed
`micromamba --version` or `pip --version` observation, respectively, with a
10-second/4-KiB command bound. It uses the same PATH entrypoint as the existing
resolver/install commands, in the original operation's environment/turn and
exact step authorization. The observation is a bounded self-reported version
at that time; it does not authenticate executable bytes or guarantee that PATH
cannot change later. Known failure, missing/malformed/truncated output stays
unknown. A lost or timed-out observation stops before resolve/install. Existing
CP-13 recovery and launch fences still apply. Already-satisfied requests need
no installer and create no version observation.

Each original step's host-generated JSON artifact is at most 64 KiB, also
limited by the configured artifact cap. It binds the provisioning request,
step operation, execution, attempt, original project/session/environment,
terminal state, timestamps, exit/signal/timing, and nullable version token.
It records stream sizes/truncation and explicit scanned/omitted counts. Only
five fixed whole diagnostic lines are retained; arbitrary diagnostic text,
package-manager JSON, paths, URLs and tracebacks are omitted. No raw-output
hash is retained. The bounded version token is the explicit exception to this
fixed-message rule. These are partial sanitized diagnostics, not full logs or
a general secret detector. Original execution retention remains unchanged.

The artifact's `source_path` is a logical generated name,
`provisioning/{provision_id}/{label}.json`; `produced_by=null` avoids claiming
that the guest wrote these exact bytes. The artifact hash covers only the
sanitized JSON. Original terminal executions must match both operation scope
and `Execution.origin`, and accepted IDs must already belong to that operation.
Copied prepared metadata is checked against a fresh bounded safe projection
before publication; only that independently formed projection crosses an await.
This checks internal data consistency, not authentication of trusted Python.

Pending step intent is saved before submission, accepted IDs before waiting,
and terminal facts before blob I/O. The owned blob write/fsync finishes before
its artifact reference, then the operation receives that reference. Those are
separate durability boundaries: interruption may leave a sanitized orphan blob
or reference, or pending metadata. None proves a step did not run or authorizes
repeating an installer. Conflicting replay, missing/corrupt existing artifacts
and same-label repeated step admission refuse. Restart keeps existing
classification; it does not redispatch or backfill logs.

At most four whole artifact-publication owners are active. Bounded file threads
remain owned through repeated cancellation and shutdown; the original
cancellation is propagated after durable reference/operation facts where known.
New artifact I/O uses no-follow, nonblocking regular-file checks on replay.
BlobWriter construction now closes its original raw FD if stream wrapping fails
and removes only a temporary pathname whose original identity is still proved.
This does not add a global filesystem isolation guarantee.

Artifact retention failure is reported with a fixed unavailable reason and
never exports raw exception text. It does not rewrite the install execution as
failed or retry it. Missing original execution metadata stops further steps.
The existing artifact retention/lifetime policy applies; this slice supplies
no new garbage collector, aggregate disk quota, binary attestation or complete
CP-16/CP-13 restoration claim.

Component checks use synthetic executions, actual temporary blob/SQLite files,
a scripted manager, and the existing fake-package-manager provisioning fixtures.
They exercise safe omission/bounds, original scope/copy refusal, retained JSON
corruption, exact replay, failed publication, four-owner admission, repeated
cancellation, constructor FD cleanup, version-before-resolve composition and
legacy recovery compatibility. Real installs, services and OpenShell acceptance
remain root-owned and separate from these source checks.
