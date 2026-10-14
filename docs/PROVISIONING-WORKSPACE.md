# Read-only provisioning workspace API

The workspace exposes recorded provisioning observations for a project's shared
environment and retained dedicated environments. This is the read-only API part of
CP-26 in the [provisioning specification](specs/conda-provisioning.md); the project
view's [Environments panel](PROVISIONING-WORKSPACE-VIEW.md) shows it. It does not
submit work, approve permissions, cancel an operation, rebuild an environment or
resume a guest.

All routes require the workspace login. The server selects its configured
`job_worker`; the browser cannot supply a worker address, token, execution
context or project policy. An environment must occur in the project record. The shared environment can contain operations from
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
| `.../revisions/{revision}/packages?pin=SHA&offset=0&limit=8` | A page of the revision's recorded packages and declared licenses ([below](#recorded-packages-at-a-selected-revision); [details](REVISION-PACKAGES-WORKSPACE.md)) |
| `.../lockfile-diff?before=N&before_pin=SHA&after=N&after_pin=SHA&offset=0&limit=8` | The differences between two pinned retained lockfiles ([details](LOCKFILE-DIFF-WORKSPACE.md)) |

Success forwards the strict bounded worker DTO without adding session ownership or
changing the meaning of its fields. Unknown revisions, declared licenses and missing lockfile
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
are 422, and unavailable transport, a timeout, a damaged project record, corruption
or bounds are 503. Success and errors are `no-store`. Route errors include their
own receipt-time `observed_at`; the workspace's authentication guard can reject a
request before this route.
An error is never converted into an empty queue or a successful observation.

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
responses. Reads perform no guest work or keepalive. Checked on OpenShell in
[Lab32](../.agents/docs/agent-reports/babbage-openshell-lab-32-2026-10-13.md).

## Server

A route checks its IDs and selection, reads the project record with
`ProjectStore.get` in a thread, and checks that the environment is one of the
project's. It then asks the worker under the six-second call timeout of
`Timeouts`, or the ten-second comparison timeout for a lockfile comparison or a
package page. Errors go through the workspace's one error mapping
([workspace](WORKSPACE.md#errors-and-records)), with `observed_at` added.

`WorkerClient.observation_transport()` provides a private in-memory bounded copy of
the configured endpoint and headers and a borrowed shared HTTP session. Its values
are hidden from repr and are never serialized or persisted. The observer copies
headers for its request and never closes the borrowed session. Ordinary
WorkerClient APIs keep their existing defaults and behavior.

Each worker read has a five-second whole-call timeout, refuses redirects and
compressed bodies, and independently caps the streamed response at 64 KiB and 256
chunks before strict JSON parsing. No active status/reconnect/create fallback is
available. Success JSON is decoded as strict UTF-8 and checked for duplicate object
keys (including escaped-equivalent keys) before Pydantic can normalize a repeated
scope, sequence or policy action. Non-JSON numeric constants are refused as well.

## Scope

The worker's observation routes supply bounded compact projections; the workspace
never fetches a full operation merely to trim it. Declared-license summaries across
revisions, complete error notes and execution history, the middleware's answer and
deadline history, USER provisioning changes and any authority to answer permissions
are outside this API. The wider workspace boundary is described in
[WORKSPACE.md](WORKSPACE.md), and worker behavior in [WORKER.md](WORKER.md).

The tests (`packages/workspace/tests/test_provisioning.py`, `test_lockfile_diffs.py`,
`test_revision_packages.py`) use synthetic records, disposable files and in-process
HTTP peers; `test_routes_provisioning.py` pins the routes' status codes and
shapes. They cover exact pages, unknown fields, authentication, size, chunk and
deadline refusal, and no redirect or fallback. The viewer was checked against a real worker
in [Lab30](../.agents/docs/agent-reports/babbage-openshell-lab-30-2026-10-13.md),
and the lockfile comparison in [Lab31](../.agents/docs/agent-reports/babbage-openshell-lab-31-2026-10-13.md).

The operation detail carries the bounded `upgrade` action ([explicit upgrades](PROVISIONING-UPGRADES.md)),
and the panel labels it; a record without an action is an install. Viewing it adds
no action or permission control.
