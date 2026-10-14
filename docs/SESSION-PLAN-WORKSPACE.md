# Read-only workspace session plans

The workspace's plan route returns the bounded current projection of a session's
plan from the [session-plan store](SESSION-PLANS.md). It reads recorded control
state; it does not create plans, approve work, start research, contact a worker or
wake a session. The session view's plan panel polls it ([workspace](WORKSPACE.md#session-plans)).
USER changes to a plan's text go through the separate
[plan changes route](SESSION-PLAN-EDITING.md) and its [editor](SESSION-PLAN-EDITOR.md).
Plan history pages and approval enforcement are not built.

## Authenticated scope and response

`GET /api/v0/projects/{project}/sessions/{session}/plan` requires the existing
workspace login and host guard. The request accepts only its path scope, with no
query parameters or body. The server derives the read authority from that scope
and a digest of the session cookie. Browser JSON cannot choose an
actor, worker, environment, database, plan reference or evidence grant.

The immutable JSON wrapper contains `project_id`, `session_id`, the trusted host
receipt time `observed_at`, nullable `session_closed_at`, and nullable `plan`.
`plan` is the core's `CompactPlan`:

| Field | Meaning |
| --- | --- |
| `event_cursor` | Latest authoritative event cursor in this project/session |
| `head` | Exact scope/reference, workflow status, status revision, progress cursor and explicit goal outcome |
| `version` | Exact scope/reference/base, full bounded body, original author/reason and recorded time |
| `steps` | Every current step in body order, with its current fingerprint, match flag and nullable latest assertion |

Each latest assertion includes its exact reference, progress sequence, step
fingerprint, status, author, recorded time and evidence count. Progress notes and
evidence contents are explicitly absent from this compact view. The head's goal
outcome retains its complete summary, evidence claims and remaining work; it is
never clipped to fit. Evidence references are claims, with no lookup or resource
read authority.

A changed step can retain an older `completed` assertion while
`matches_current_step=false`. That assertion does not complete the revised step.
Likewise, an attributed `in_progress` status is recorded progress, not proof that a
process is running. Workflow `completed` retains the separate `achieved`,
`partially_achieved` or `not_achieved` qualification. Neither `active` nor
`awaiting_approval` grants execution or implements an approval barrier.

## Reading

The route reads the project record with `ProjectStore.get`, then the plan through
the app's read-only `DocumentStore` connection, in a thread, under the six-second
call timeout. The read-only connection sees committed progress from another
connection without a saved run or completed model turn. A read never creates a
database, migrates a schema or initializes a writer.

Closed sessions remain readable as recorded history and return their closure
time. The read neither reopens them nor changes activity time. A missing
project/session is 404. A wholly absent plan namespace or a scope with no retained
plan rows yields 200 with `plan: null`. Partial schema, damaged canonical rows, or
retained scoped history without its current pointer is unavailable (503), rather
than an apparently empty plan; so is a damaged project record. A missing login is
401; invalid request scope, query or body is 400. Success and errors for this GET
carry `Cache-Control: no-store`, including host-guard refusals. Concurrent reads
of one session are all answered.

## Tests

`packages/workspace/tests/test_routes_plans.py` and `test_plans.py` cover
committed live progress, closed and scoped history, revised intent, explicit
outcomes, authentication, absent versus corrupt state and bounded giant rows.
They use synthetic control activity; they do not show browser rendering, model
planning or approval enforcement.
