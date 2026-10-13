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

## Visible Session-view polling

The open Session view mounts a passive kernel panel for its exact loaded project,
session and environment. It shows current, predates or unknown revision status,
generation and exact start/current revisions, with the worker receipt time. Empty
and unavailable are distinct. A previous observation remains explicitly dated and
labeled as an unknown current state when polling fails or pauses.

One request is owned until actual settlement, with an eight-second deadline and
12 KiB response limit. Successful polls schedule the next read three seconds after
completion; errors back off to at most thirty seconds. Hidden pages pause reads.
Visibility, view replacement, unmount and logout invalidate old replies even if
their transport ignores abort. Logout starts a sticky page-lifetime read fence,
including for a panel mounted while the logout HTTP request is pending. A new
login requires a page reload before these reads resume.

The browser validates exact scope, aware receipt time, unique kernel/language
identities and revision/status coherence. JavaScript cannot represent the entire
worker integer range exactly: unsafe generation or revision integers refuse the
observation rather than displaying rounded pins. No new restart action is added.

This is visible-session eventual observation. Background scheduling, SSE, durable
agent inbox/cursor and save-before-ACK delivery, browser-offline alerts, idle agent
wake and automatic restart remain separate from whole CP-12 completion. Retained
transcript output alone is not a live transport.

Local tests cover bounded registry reads, special files and growth, strict damaged
records, complete close/reopen ABA, revoked login, actual WorkerClient HTTP
composition and negative peers, immediate admission, HTTP disconnect behavior,
response deadlines, repeated cancellation and shutdown ordering. Combined gates
on reviewed signed `70f6761` pass 10,032 Python/614 web/24 post-build math checks
and required static/license/protocol checks and both builds.

Root-owned [browser/OpenShell Lab27](../.agents/docs/agent-reports/babbage-openshell-lab-27-2026-10-12.md)
passed actual shared-package staleness without another A execution, fresh B and
isolated C, worker stop/recovery continuity, both real VMs stopping during passive
reads, thirty unavailable reads without wake, ordinary resume/state loss and
logout fencing. The accepted 70-file production build and source/helper pins are
retained. All owned services, VMs, browser tabs and private state were removed and
independently verified. Native hidden-tab pause was not proved because the browser
automation surface retained visible document state; focused lifecycle tests cover
that behavior separately. PR #99 merged at `afb049a` after all four fresh exact-head checks passed on reviewed signed `fe5f4d7` (run `38325477608`).
