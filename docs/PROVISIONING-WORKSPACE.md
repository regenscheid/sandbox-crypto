# Read-only provisioning workspace API

## Recorded packages at a selected revision

`GET /api/v0/projects/{project}/environments/{environment}/provisioning/observations/revisions/{revision}/packages?pin={raw_record_sha256}&offset=0&limit=8`
returns `RevisionPackagePage`. The worker counterpart uses `/v1/environments/`
and required `project_id`. The canonical raw record hash and full retained lockfile
reference bind each page. Stale pins refuse with 409, scope failures with 404;
workspace responses are `no-store`.

Inventories include unchanged/base packages and retained unmanaged revisions,
independently of an install's files or comparison differences. Entries retain
canonical ecosystem/name/location ordering, package metadata and declared Conda
and optional [pip licenses](PIP-LOCKFILE-LICENSES.md). Missing or ambiguous pip
declarations remain unknown. AGPL text matching reports a declaration;
it does not certify compliance or installation state. Missing, skipped,
unsupported or unproved sources stay held. Source URLs are escaped text.

The browser explicitly loads a selected revision and follows bounded next/previous
pages, without inventory polling. Selection, hide and logout drain the existing
physical fetch/body owner. Deadlines are worker 8s/client 9s/proxy 10s/browser 12s.
The worker shares the comparison reader's two global/one environment read lane,
with 8 MiB lockfiles, 16 KiB entry records, 16 MiB index accounting and 64 KiB
responses. Reads perform no guest work or keepalive. Component/full gates and
actual-main source review pass. Root-owned Lab32 passed worker/workspace/native
inventory paging, all 62 unchanged entries and six GP additions. Separately
qualified stopped-VM reads preserve actual retired endpoints and unchanged SQL
between fresh proofs for the original VM. All owned lab state is removed and
independently audited. The [Lab32 report/archive](../.agents/docs/agent-reports/babbage-openshell-lab-32-2026-10-13.md)
retains the original helper failure and qualified result; final delivery review
and fresh CI remain.

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
The final owned proof also reopens the canonical directory and project pathname
without following links and compares both to the retained descriptors. Agreement
between two old held descriptors alone cannot authorize a replaced canonical root.

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
Success JSON is decoded as strict UTF-8 and checked for duplicate object keys
(including escaped-equivalent keys) before Pydantic can normalize a repeated scope,
sequence or policy action. Non-JSON numeric constants are refused as well.

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

The corrected author checkpoint on Python 3.13.15 passes 211 affected tests, including the 79
new proxy/transport/registry cases, whole workspace/orchestrator typing, scoped Ruff
and format checks, locked synchronization and the 75-distribution license check.
The companion observation-contract source was pinned before and after the affected
run from its parallel worktree; no worker code was executed. Existing WorkerClient
method ASTs remain unchanged, with only the bounded observer snapshot/recheck seam
added. These local results are distinct from root's later integrated gates/runtime.

## Integrated viewer acceptance

Root-owned [OpenShell Lab30](../.agents/docs/agent-reports/babbage-openshell-lab-30-2026-10-13.md) passed on signed clean `220cddd`, with exact corrected reader/GUI parity, real GP/Flint records, separate file approvals, native browser views, offline503 and120 stopped-environment GETs with unchanged SQL. All owned state was removed and independently checked. [PR #110](https://github.com/regenscheid/sandbox-crypto/pull/110) merged at `25159e1` on independently clear signed `70f9d12` after all four fresh checks passed (run `38415726829`). Primary main is clean/synced, locked dependencies checked and tree-identical to the reviewed head. The accepted report/archive remain immutable. The later [Lab31 comparison acceptance](../.agents/docs/agent-reports/babbage-openshell-lab-31-2026-10-13.md) completed on signed `ec1faea`: exact retained GP/Flint comparisons, canonical raw/ref/blob proof, native nine-difference pages in both directions and thirty paired reads while the original VM was Stopped. The first workspace interruption and original tuple/list helper assertion are preserved; source-reviewed root normalization qualifies unchanged complete SQL values, and helper-only `f0e0157` fixes that representation after teardown. All owned state was independently removed. Comparison final archive review/fresh CI delivery remains in progress; unchanged/base package-license visibility and GUI operator actions stay separate.
