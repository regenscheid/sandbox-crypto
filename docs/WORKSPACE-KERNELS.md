<!-- SPDX-License-Identifier: MIT -->
# Passive session kernel proxy

The workspace's authenticated
`GET /api/v0/projects/{project}/sessions/{session}/kernels` reads the recorded
session's kernel revisions through the existing
[passive worker observer](KERNEL-REVISIONS.md#existing-connection-observations).
It uses only the operator-configured `job_worker` connection, also used by job
views. No configured worker means an explicit unsupported response. The browser
cannot supply a worker address, environment, policy or credential; the route
accepts only its path scope, without a body or query arguments.

The success response is the existing `KernelObservation` plus `project_id`:

```json
{"project_id":"example","environment_id":"example-env","session_id":"a","current_revision":2,"kernels":[{"kernel_id":"kernel-a","language":"python","generation":1,"start_revision":1,"current_revision":2,"revision_status":"predates"}],"observed_at":"2026-10-12T22:00:00Z"}
```

The route forwards the existing revision and kernel fields, including unknown
nullable revisions, and validates their scope and coherence. `observed_at` remains
the worker's receipt time. It does not prove loaded module versions, current
package contents or future continuity. A successful empty list means an observed
connected session with no running kernels; unavailable never becomes empty.
All success and API error responses use `Cache-Control: no-store`.

| Condition | HTTP status |
| --- | --- |
| No valid workspace login, including original login revoked during the call | 401 |
| Absent project/session, or session already recorded closed | 404 |
| Invalid path identifier, body or query arguments | 400 |
| Operator worker connection absent, or observer unsupported | 422 |
| Observer unavailable, invalid response, registry damage, capacity/deadline or changed binding | 503 |

The existing app-owner login, Host checks and browser cookie rules still apply.
This adds no per-user project ACL, worker permission, lifecycle action or model
authority. Missing worker environment access is an unavailable observation rather
than disclosure of another environment. There is no fallback to explicit
`/v1/kernels/status`, capabilities, reconnect or any research execution route.

## Trusted registry continuity

The proxy uses a separate bounded `ProjectStore.session_snapshot` lookup. It
validates path identifiers, opens the registry directory and final project file
without following their symlinks, opens final components nonblocking, requires a
regular file, checks its size before reading and reads at most 1 MiB plus one
growth-detection byte. It parses strict JSON types only within that cap and checks
the requested project ID, session key/ID, environment and open-session record.
Naive session opening times are not accepted as trustworthy continuity. Ordinary
project reads, record formats and mutation behavior are unchanged.

The original file and directory descriptors remain held during the HTTP call.
The proxy makes another bounded read afterward and compares the exact registry
identity, contents and session binding, then rechecks the originally authenticated
login. Supported registry writes atomically replace the project file, so a full
close/reopen cannot disappear just because `closed_at` is empty again or timestamps
and bytes are restored. Holding the old inode prevents its reuse during this
comparison. In-place record changes also fail the metadata/content checks.
Any concurrent project-record replacement conservatively refuses that observation,
including a policy change or another session's update. A later call can read the
new binding. These are receipt-time checks, not a lock preventing subsequent
changes. No lifecycle/project lock is held over the worker request.

## Bounds and cleanup

Workspace admission is immediate, without an unbounded queue or retained scope
history: at most eight calls globally, two per authenticated login and one per
project/session. Active ownership alone retains those counters. Registry reads
use a private two-thread executor so blocked file work cannot consume the default
executor used by unrelated workspace operations. At most the admitted calls can
submit file work. Successful serialized responses are capped at 8 KiB.

The whole workspace call has a six-second response budget, including initial and
receipt registry reads and the observer HTTP call. The existing observer client
already has its five-second total HTTP deadline, 8 KiB response/error limit and
no redirects. A response deadline or canceled caller cancels the owned wrapper,
but a running filesystem read remains owned until it actually returns. Its
descriptors and admission are not released prematurely; the timed-out response is
unavailable while that cleanup remains tracked. Arbitrary kernel-level blocked
filesystem I/O cannot be forcibly bounded, and can delay final shutdown.

An HTTP peer disconnect does not necessarily cancel an aiohttp handler. The
workspace preserves the existing server cancellation defaults: admitted work may
continue until success or the response budget. Both cases remain tracked. On app
cleanup the proxy stops admissions, cancels/drains actual admitted work through
repeated caller cancellation and shuts down its private executor before the
shared WorkerClient closes. It never cancels or restarts research kernels when a
read times out.

## Delivery boundary

This backend is the prerequisite for visible Session-view polling. It does not
provide a GUI by itself, a background scheduler, SSE, durable agent inbox/cursor,
save-before-ACK delivery, browser-offline alerts, idle agent wake or automatic
kernel restart. Those remain separate from whole CP-12 completion. A later
frontend must own visible-tab polling, cancellation and view-generation checks;
retained transcript output alone is not a live transport.

Local tests cover bounded registry reads, special files and growth, strict damaged
records, complete close/reopen ABA, revoked login, actual WorkerClient HTTP
composition and negative peers, immediate admission, HTTP disconnect behavior,
response deadlines, repeated cancellation and shutdown ordering. No real OpenShell
or browser acceptance of this new workspace path has been performed by this
backend author. The existing Lab26 validates the worker observer; the new proxy
and polling path still require root-owned end-to-end runtime acceptance.
