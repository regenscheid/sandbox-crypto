# Orchestrator confinement transport

Status: Proposed implementation contract for RC-27 to RC-29. This document does not
claim that the normal launcher is confined or that replacement has been qualified.

The agent-facing runtime runs in a dedicated OpenShell sandbox. It owns root and
child scheduling, prompt assembly, model adapters, model response parsing and the
model/tool loop. A trusted host control service owns durable state and validates
state/tool capabilities. The workspace remains independently placeable. Worker
execution and OpenShell lifecycle authority remain outside the agent runtime.
Moving only model HTTP requests into a sandbox does not meet this contract.

## Authority and storage

The host control service owns the existing control database, its transaction
boundaries and repository event subscribers. Its API offers named capabilities,
never SQL statements, SQL fragments, arbitrary method names, host paths or file
handles. Scope is bound to authentication, then checked against every requested
project, session, attempt and child relationship. Runtime capabilities cannot
perform workspace USER writes, grant policies or change configuration.

An attempt capability names a durable attempt, its scope and a fencing generation.
Every mutating request checks the current generation in the same transaction as
its effect. A replaced runtime's token cannot reacquire its old generation.
Idempotency identities and canonical request digests are recorded before effects;
a retry returns the recorded outcome or an explicit pending/unknown status.

The service retains run inputs, attempt/tree ownership, archive entries, tool
intents/results, fold checkpoints, exact evidence references and delivery cursors.
Success acknowledgements follow durable commits. The runtime's writable filesystem
holds only replaceable scratch/configuration. No host state directory is mounted.

## Capability operations

The transport has a version independent of the worker protocol. Requests and
responses use strict typed JSON, bounded bodies and explicit error kinds. A
handshake checks protocol, control schema, image revision and configured scope.
Readiness fails closed on incompatible or unavailable dependencies.

The operation catalog is explicit:

- Claim a queued root or an explicitly requested Resume; inspect scoped queue
  entries; renew/release its fenced ownership. A service restart alone creates no
  Resume request.
- Prepare an admitted attempt's context, pinned inputs, prompt sections, tool
  schemas, skills metadata and bounded initial view.
- Fetch bounded notices and state views; evaluate trusted limits and effect gates;
  record delivered notice IDs only after the corresponding model call is saved.
- Append a typed user/assistant/tool/notice event and its exact archive identity;
  start a tool intent with its operation identity; retrieve a bounded prior result.
- Execute one offered tool under the attempt's authority; report pending/unknown
  effects rather than repeat them. Research code always goes to the worker.
- Read/fold a bounded conversation view and commit a checkpoint against its source
  revision. Folding cannot confer authority or modify pinned user constraints.
- Admit a child against its durable parent, role, grants and Stop state; enumerate
  unfinished children; receive and acknowledge each terminal child outcome. The
  runtime schedules child loops, while the service validates their admission.
- Evaluate closing/finishing guards and atomically finalize the attempt with its
  archive/checkpoint references and delivery receipts.

There is no generic `invoke`, repository-name or Python-object deserialization
operation. Python exceptions, callbacks, live database transactions and model
objects do not cross the boundary. Long operations return durable identities and
support bounded status polling. Cancellation closes the runtime's active call;
the authoritative service continues to own already-admitted effects until their
outcome is reconciled.

## Launcher and replacement

The launcher starts the worker and control service first. It compiles the sandbox
policy with only model gateway, worker, MCP proxy and scoped state-service egress.
It records a fresh sandbox name before create and its first observed immutable ID
before phase validation. An uncertain create retains its record; observed absence
without an identity is not deletion proof. Replacement waits for identity-checked,
confirmed deletion or explicit operator reconciliation.

Only project-scoped worker/state capabilities and the runtime's model/MCP
credentials enter the sandbox. Engine, cluster, OpenShell/provider administration
credentials and management sockets never enter it. Software is read-only; scratch
is explicitly writable. Image configuration uses image paths, including math and
figure checking tools, rather than host executable/build paths.

The normal launcher has no host harness fallback. It publishes readiness only
after the runtime handshake and application readiness succeed. Startup failure,
transport loss or incompatible state leaves work visibly interrupted. Shutdown
stops admission, stores Stop intent where requested, drains/reconciles effects,
releases attempt/environment claims and tears down the runtime. Replacement alone
never deletes workload environments.

An explicit Resume fences prior attempt authority, reconciles the existing tree
and worker operations, then creates one replacement attempt. Two concurrent Resume
requests cannot create two owners. Lost kernel state is reported explicitly.

## Acceptance

Deterministic fixtures must cover malformed/oversized transport, cross-project and
cross-attempt requests, stale tokens, stale generations, repeated request IDs,
unknown effects, concurrent Resume, persisted Stop, cancelled startup and uncertain
create/delete. Tests must exercise production launcher wiring, not a parallel lab
entrypoint.

Disposable OpenShell validation must prove the normal launcher starts the actual
parent/child loops inside the runtime VM, no harness loop runs on the host, denied
egress and host paths stay denied, and startup failure has no host fallback. It
must delete and recreate the runtime VM, then explicitly Resume the original tree
while the independently running UI retains history, evidence and delivery cursors.
Worker operations must retain their original identities; lost kernels must be
reported. Validate confirmed teardown and absence of disposable processes/VMs.
Local fixtures and historical image labs do not establish this acceptance.
