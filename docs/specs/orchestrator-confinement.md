# Orchestrator confinement transport

Status: Implementation contract for RC-27 to RC-29. The normal launcher uses this
boundary. The [2026-10-17 qualification](../../.agents/docs/agent-reports/openshell-qualification-2026-10-17.md)
verified actual VM replacement and explicit parent/child Resume on the recorded
revision, with synthetic model responses; its scope and limits remain explicit.

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

Disconnect cleanup fences new attempt capabilities and drains each admitted call
before releasing its publishers and stores. A parent waiting on its children first
closes their owning delegation lease, so that draining does not form a lock cycle.
An in-progress child admission must settle before that lease closes. The connection
remains occupied throughout this drain; a replacement deadline can expire while a
long worker call is still settling, and replacement can be retried afterward.
Mailbox observation waits are interrupted on disconnect without consuming messages;
they do not hold cleanup until their requested timeout. Effectful operations and
job waits that may own watcher/recording work still drain. Every entered hook lease
is closed even when another lease's exit fails, and repeated cancellation joins
the same cleanup instead of abandoning its handlers or resources.

`/health` reports `runtime_connected=false` as soon as transport teardown starts.
Its separate `runtime_draining=true` retains the exclusive owner until handlers,
attempts and services have finished cleanup. Replacement waits for both flags to
clear, and a second connection is refused while either ownership phase is active.

## Capability operations

The policy compiler accepts an optional `orchestrator.state_service` endpoint and
applies the same service restrictions and endpoint uniqueness checks as the worker,
model and MCP endpoints. Adding it alone does not confine the normal launcher.

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
- Confirm whether one MCP permission request is a refused call's, for the attempt's
  project, which the host binds: yes or no from the policy middleware's records through
  the worker (`confirm_request`, D98), never the request's record.
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

Only the scoped control capability and the runtime's model credential enter the
sandbox. Worker and MCP credentials stay in trusted host handlers. Engine,
cluster, OpenShell/provider administration
credentials and management sockets never enter it. Software is read-only; scratch
is explicitly writable. Runtime executables use image paths. Math and figure
checking remain fixed trusted host capabilities using configured platform builds;
they do not evaluate research code or accept arbitrary executable paths over RPC.

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

## Runtime prerequisites and evidence boundary

Build the orchestrator image from the same source revision as the host control
service. The launcher requires its full embedded revision and the runtime compares
that revision, control protocol, control schema and worker protocol before readiness.
Private control protocol 2 carries typed active-memory exhaustion so runtime loops
can drain their children before pausing. Protocol 1 peers are refused; deploy host
control and the orchestrator image together. The control store is now schema 68,
including durable artifact blob mappings and transfer reservations.
The deployed OpenShell must support the VM driver, image uploads, immutable sandbox
IDs, providerless create and REST WebSocket upgrades through compiled endpoint policy.
OpenShell v0.1.3 passed the recorded disposable normal-launcher qualification,
including the actual scoped control transport through compiled endpoint policy.
That report distinguishes verified runtime behavior from untested live integrations.

The configured model key must itself be scoped by the gateway to the intended model
aliases and budget, without administrative access. Runtime adapter allowlists constrain
normal callers, but cannot narrow the underlying authority of a raw credential in a
compromised VM. Engine/provider/cluster credentials stay with trusted lifecycle code.
The generated control token is scoped to configured projects; attempts derive their
session, work and epoch from opaque host capabilities, never request-supplied context.

The VM asks for bounded eligible recovery branches and schedules each selected branch
through a typed admission request. The trusted coordinator owns dependency validation,
reconciliation and the session lease. It never selects and runs the next agent loop by
itself. A branch RPC has no arbitrary wall-clock lifetime; disconnect, explicit Stop
and the attempt's own limits govern termination. Paid gateway dispatch first commits
an intent, and an unobserved result remains unknown across replacement.

When only the agent runtime exits after startup, the normal launcher stays alive in
degraded status. Control, the GUI and the worker retain their process identities.
`sandbox-science replace-orchestrator --config ...` asks the identified owning
launcher to replace only its agent VM. A private bounded nonce request/receipt and
an advertised control capability prevent signalling an incompatible launcher. The
launcher confirms deletion of the original immutable sandbox ID, waits for the old
control connection to drain, rotates the runtime token/generation and admits a fresh
image handshake. Failure retains ownership and degraded status. Replacement creates
no research Resume request under the default manual policy.

`orchestrator.recover_infrastructure = true` enables autonomous recovery for an
unattended local deployment. The launcher restarts its owned worker, control and
workspace processes with bounded increasing delays and readiness checks. It
replaces a failed agent VM through the same immutable-identity reconciliation
used by explicit replacement, leaving research VMs running. Replacement attempts
are recorded before dispatch in a bounded private journal and stop after ten
consecutive failures; sixty healthy seconds reset the streak. Exhausted host
restart retries leave the remaining services and research VMs in degraded status
instead of tearing them down. A replacement left
pending by launcher loss is reported as unknown and is not automatically replayed.
A successful explicit `replace-orchestrator` reconciles the original VM and
clears that automatic replacement block.

The corresponding control policy admits interrupted work to the existing bounded
automatic tree Resume queue. It honors explicit Stop, fresh user messages, token
grants and cost admission. Worker computations are looked up by their original
operation IDs; mailbox/job/child observation waits produce an explicit interrupted
observation receipt. Unsupported uncertain effects remain blocked. Recovery does
not promise arbitrary interpreter-memory reconstruction or automatically repeat
an MCP mutation. Retry decisions survive service restarts and stop after three
attempts without recorded progress.
The drain wait uses the configured launcher `stop_timeout` (30 seconds by
default). A known refusal because the old connection has not settled may be
retried after that connection actually disconnects; admitted tool effects retain
their original identities throughout. Do not interpret a replacement refusal as
permission to redispatch an operation.

Confined paid model and embedding responses have a 6 MiB body ceiling, including auxiliary calls.
This keeps native base64 plus decoded message fields within the 24 MiB transport
frame ceiling. A larger response is explicitly refused; its pre-dispatch intent
remains unknown and blocks claiming the work complete or silently charging zero.
The 8 MiB active-view ceiling independently accounts for decoded and native data.
The launcher never widens these limits. The model client can retry a transient
gateway response within one bounded logical call; the launcher does not replay
an interrupted call when reconnecting.

Runtime disconnection closes the trusted service's admitted tasks through the same
owned cleanup as local service shutdown. It finishes receipt reconciliation and
holds unconsumed original chat input before admitting a replacement connection.
Queue admission independently reconciles committed message and annotation receipts;
a consumed input cannot start another root if its transient lease has disappeared.
Unconsumed annotation offers remain associated with their unfinished durable work,
so explicit Resume can continue them while a new user request may start separate work.

Private control protocol 3 additionally carries attempt-pinned requested model profiles
for role-specific reasoning effort. Older control/runtime peers are refused.

Unexpected capability failures retain a local diagnostic with capability name, exception
class and stack locations, never request payloads, exception text or local variables.
Prepared host callbacks restore both captured ownership and model-profile context.

### Verified cluster callback

The control command remains `sandbox-science-control --settings control.json`.
`listen` defaults to `127.0.0.1:8202`; any non-loopback IP bind requires an explicit
`tls` object with `certificate` and `private_key` file paths. IPv6 bind addresses
are bracketed. The key must be a private regular file, as capability tokens are;
projected Kubernetes Secret files must be copied into private regular mode-600
files before use. A missing or unusable TLS identity prevents startup.

The separate runtime command remains
`sandbox-science-agent-runtime --settings runtime.json`. Its `control_url` accepts
an explicit HTTPS `/control` endpoint; `control_ca_file` optionally adds a private
CA to system trust. Certificate and hostname verification are always enabled,
redirects are refused before following them, and userinfo, query strings, fragments
and other paths are invalid. Existing HTTP callbacks for the local OpenShell path
remain supported. A CA file cannot be paired with HTTP. These are trusted
deployment settings, unavailable to agent tools. TLS does not replace runtime
capability, protocol, image, generation or exclusive-connection checks.

`GET /live` reports process availability. `GET /ready` reports whether the service
accepts admission (200, or 503 during shutdown), independently of runtime presence.
`GET /health` retains `ready`, `protocol`, `runtime_connected` and
`runtime_draining`, and adds `process_alive`, `service_ready`, `draining` and
`runtime_ready` (a connected runtime whose image/worker handshake succeeded).
These passive probes create no stores, sandboxes, model calls or recovery intent.
Readiness must not depend on runtime connection: the runtime needs the Service
before connecting. Probes expose no project, credential or prompt data.

On the cluster, control issues runtime credentials itself
([runtime bootstrap](kubernetes-runtime-bootstrap.md#credential-handoff-control-is-the-authority),
KRB-6 and KRB-7). This is off unless `control.json` names
`manager_token_sha256_file`: a private file holding the lowercase SHA-256 hex of the
runtime manager's bearer token, reread per request. It then requires
`generation_file`, and control opens a second listener at `manager_listen` (default
`127.0.0.1:8444`; non-loopback requires `tls`, whose certificate it shares) serving
only `GET /health` and `POST /runtime/credentials`; the runtime listener does not
serve the latter. `/health` on both listeners gains `control_instance`, a random value
per process. At startup control writes a fresh unissued token to
`runtime_token_file` (a memory volume) and marks the last issue superseded, which
revokes any runtime. The issue journal `runtime-credentials.json` in `state_dir`
keeps each request ID, state, generation and token digest, never a token.

The request is `{"request": "<32 lowercase hex>"}` with the manager's bearer token,
at most 1 KiB. A wrong or missing token answers 404. Replaying the latest issued
request answers 200 with the same `{"generation", "token"}` while both files still
hold them. An older or superseded request answers 410 `superseded`. A new request
(or a retry of the latest unissued one) first replaces the token, then answers 409
`occupied` while a runtime is connected or draining; otherwise control mints a
`runtime-<24hex>` generation never journaled before and a 32-byte hex token, writes
the generation file, the token file and then the journal, and answers 200. A
malformed body answers 400 (413 if too large) `invalid_request`, and shutdown 503
`unavailable`.

SIGTERM stops admission and closes the active WebSocket while owned admitted
effects and service cleanup drain. Successful retained effects keep their original
identities; socket shutdown does not stop research sandboxes, submit a replacement
operation or create Resume intent. The deployment must allow sufficient termination
grace for receipt cleanup; a forced process kill still follows durable interruption
and explicit recovery semantics, not an exactly-once process-memory guarantee.
