# Read-only provisioning workspace API

The workspace exposes recorded provisioning observations for a project's shared
environment and retained dedicated environments. This is the read-only API part of
CP-26 in the [provisioning specification](specs/conda-provisioning.md). It does not
submit work, approve permissions, cancel an operation, rebuild an environment or
resume a guest. Browser rendering and real OpenShell acceptance are separate.

All routes require the original authenticated workspace login. The server selects
its configured `job_worker`; the browser cannot supply a worker address, token,
execution context or project policy. An environment must occur in the bounded
canonical project registry. The shared environment can contain operations from
several sessions in that project. Closed logical sessions can still identify their
dedicated environment for retained read-only history; this never reopens them.

| GET route below `/api/v0/projects/{project}` | Selection and returned evidence |
| --- | --- |
| `/provisioning/environments` | Registry-only `project_id`, `default_environment_id`, sorted `environment_ids` and `observed_at`; no worker fan-out |
| `/environments/{environment}/provisioning/observations` | Compact queue summaries, recorded package gate, nullable current revision and worker observation time |
| `.../operations/{operation}[?seq=N]` | Small operation detail, original request and recorded permission-request number; omitted sequence reads current retained detail in one snapshot, supplied sequence must match |
| `.../operations/{operation}/files?seq=N&offset=0&limit=8` | Exact resolved-file page, total and continuation; every page must retain the selected sequence |
| `.../revisions?after=-1&limit=8&target=N` | Retained revision summaries, explicit first retained revision and pinned target; omit `target` only for the first page |
| `.../revisions/{revision}/probes?pin=SHA&offset=0&limit=8` | Selected exact revision-record SHA-256 and probe page with total and continuation |

Success forwards the strict bounded worker DTO without adding session ownership or
changing the meaning of its fields. The proxy checks returned project/environment,
operation ID/sequence, revision window, offset/limit and selected record hash against
the original request. Unknown revisions, declared licenses and missing lockfile
references remain unknown. Retained history is not a complete immutable archive.
Functional and import checks remain distinct records. Viewing a permission-request
number and its resolved files does not permit answering it or accessing the policy
middleware's event store.

Selecting an operation ID from a revision opens its **current retained** operation
detail, not a claim about its state at that historical revision. The response's
actual sequence pins subsequent file pages. An omitted detail sequence is an
explicit selection mode, not a stale-page retry or a legacy full-operation fallback.

Only the listed query fields are accepted, without duplicates or request bodies.
Counters are canonical decimal integers within the companion contract's `2**62`
bound; page limits are 1 through 8. Invalid selection is 400, missing/foreign scope
is 404, stale sequence/hash selection is 409, unconfigured/unsupported observations
are 422, and unavailable transport, corruption or bounds are 503. Success and errors
are `no-store`. Proxy errors include their own receipt-time `observed_at`; the
workspace's existing authentication guard can reject a request before this route.
An error is never converted into an empty queue or a successful observation.

## Receipt-time authority and actual ownership

The project reader refuses final symlinks and special files, preflights regular-file
size, and reads at most one MiB plus one byte before parsing. At most 64 distinct
recorded environments are admitted; larger registries are refused rather than
silently clipped. Session IDs, shared-environment bindings and timezone-aware
registry times are validated. Original project/root descriptors remain held while
the worker request runs. A fresh bounded read must match their file/directory
identity, original digest and typed registry. Supported registry mutations replace
the file atomically, so a full close/reopen and identical-byte restore still fails
this check. Any concurrent project-record replacement is conservatively unavailable,
including an unrelated session change. This proves continuity at receipt, not an
atomic guarantee over a later mutation or against arbitrary trusted-host tampering.

`WorkerClient.observation_transport()` provides a private in-memory bounded copy of
the configured endpoint and headers and a borrowed shared HTTP session. Its values
are hidden from repr and are never serialized or persisted. The observer copies
headers for its request and rechecks the original client/config/session identity
afterward. It never closes the borrowed session. Ordinary WorkerClient APIs keep
their existing defaults and behavior.

Each worker read has a five-second whole-call timeout, refuses redirects and
compressed bodies, and independently caps the streamed response at 64 KiB and 256
chunks before strict JSON parsing. The full workspace response also fits 64 KiB or
is refused. No active status/reconnect/create fallback is available.

The proxy responds within a six-second caller budget. It has immediate admission
limits of eight calls globally, two per original login and one per project/environment
scope, including local environment-list reads. A private two-thread pool owns
bounded registry work; queued and running work both retain their admission. Browser
disconnect or cancellation is not proof that admitted work has finished. Timed-out
or repeatedly cancelled callers leave actual tasks, descriptors and slots owned
until drain. A blocked regular-file read can outlive the response budget; no forced
filesystem-interruption guarantee is made. Shutdown stops admission and drains
these owners before closing the shared worker client, including when cancellation
is raised after the proxy finishes cleanup. No lifecycle lock is held across a
network request.

## Scope and validation

The worker/contracts observation prerequisite supplies bounded compact projections;
the workspace never fetches a giant full operation merely to trim it. Two-lockfile
differences, declared-license summaries across revisions, complete error/notes and
execution history, middleware answer/deadline history, USER provisioning mutations
and all permission-answer authority remain outside this slice. The wider workspace
boundary is described in [WORKSPACE.md](WORKSPACE.md), and worker behavior in
[WORKER.md](WORKER.md).

Focused tests use synthetic records, disposable files and in-process HTTP peers.
They cover exact pages, unknown fields, scope/sequence/window/hash mismatches,
authentication and registry ABA, size/chunk/deadline refusal, no redirect/fallback,
admission, actual thread/descriptor lifetime and repeated-cancellation shutdown.
They do not exercise a real worker, package manager, provider, model or VM. Root-owned
combined gates, independent review and real browser/OpenShell acceptance remain
delivery requirements.

The author checkpoint on Python 3.13.15 passes 204 affected tests, including the 72
new proxy/transport/registry cases, whole workspace/orchestrator typing, scoped Ruff
and format checks, locked synchronization and the 75-distribution license check.
The companion observation-contract source was pinned before and after the affected
run from its parallel worktree; no worker code was executed. Existing WorkerClient
method ASTs remain unchanged, with only the bounded observer snapshot/recheck seam
added. These local results are distinct from root's later integrated gates/runtime.
