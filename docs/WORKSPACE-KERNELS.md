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
nullable revisions; the worker client checks their scope and coherence.
`observed_at` remains the worker's receipt time. It does not prove loaded module
versions, current package contents or future continuity. A successful empty list means an observed
connected session with no running kernels; unavailable never becomes empty.
All success and API error responses use `Cache-Control: no-store`.

| Condition | HTTP status |
| --- | --- |
| No valid workspace login | 401 |
| Absent project/session, or session already recorded closed | 404 |
| Invalid path identifier, body or query arguments | 400 |
| Operator worker connection absent, or observer unsupported | 422 |
| Observer unavailable, invalid response, deadline, or a damaged project record | 503 |

The existing app-owner login, Host checks and browser cookie rules still apply.
This adds no per-user project ACL, worker permission, lifecycle action or model
authority. Missing worker environment access is an unavailable observation rather
than disclosure of another environment. There is no fallback to explicit
`/v1/kernels/status`, capabilities, reconnect or any research execution route.

## Server

The route reads the project record with `ProjectStore.get` in a thread, takes the
session's environment from it, and asks the worker under the six-second call
timeout of `Timeouts`. The observer client has its own five-second deadline, an
8 KiB response limit and no redirects. A timeout answers 503; it does not cancel
or restart research kernels. Concurrent reads, from two tabs on one session say,
are all answered. Errors go through the workspace's one error mapping
([workspace](WORKSPACE.md#errors-and-records)).

## Visible Session-view polling

The open Session view mounts a passive kernel panel for its exact loaded project,
session and environment. It shows current, predates or unknown revision status,
generation and exact start/current revisions, with the worker receipt time. Empty
and unavailable are distinct. A previous observation remains explicitly dated and
labeled as an unknown current state when polling fails or pauses.

The panel reads through the workspace's shared poller and request function
([workspace](WORKSPACE.md#the-client)): an eight-second deadline, a 12 KiB answer limit,
three seconds between reads, backoff to thirty seconds, no reads while the page is
hidden, and none after sign-out until the page is reloaded. A reply that arrives
after the page was hidden or the panel replaced is dropped. The browser checks
only that an observation names the panel's project, session and environment; the
revision fields are shown as the server sends them. The panel has no restart
action.

The panel is an eventual observation of a visible session. It does not deliver
notices to agents (the [kernel check](KERNEL-REVISIONS.md#kernel-check-before-each-turn) does that), push
updates, alert while the browser is offline, wake an idle agent or restart a
kernel.

The tests (`packages/workspace/tests/test_routes_kernels.py`, `test_kernels.py`
and `web/workspace/src/lib/kernels.test.ts`) cover the response's shape, the
status codes, damaged records, the worker client against negative peers and
response deadlines. The panel was
checked against real VMs, including stopped ones that a read must not wake, in
[Lab27](../.agents/docs/agent-reports/babbage-openshell-lab-27-2026-10-12.md).
