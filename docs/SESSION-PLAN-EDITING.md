# USER session-plan body editing

The workspace server accepts an authenticated USER revision of an existing
[session plan](SESSION-PLANS.md)'s body, using the plan's ordinary immutable
version CAS. The route does not create plans, change workflow status or progress,
record a goal outcome, approve a plan, delegate or grant execution. The
[structured editor](SESSION-PLAN-EDITOR.md) in the GUI uses this route; the
[read-only view](SESSION-PLAN-WORKSPACE.md) stays read-only.

## Route and exact request

`POST /api/v0/projects/{project}/sessions/{session}/plan/changes` requires the
existing workspace login and same-origin/Host write guards. Its only scope is
the URL path; query parameters are refused. Content type must be
`application/json`. The strict JSON request contains exactly:

```json
{
  "operation_id": "user-edit-example",
  "base": {"plan_id": "plan-example", "version": 1},
  "body": {
    "goal": "Check the claim",
    "feasibility": "Start with a known case",
    "outputs": [{"title": "Result", "description": "Derive it", "evidence_standard": "Check it"}],
    "phases": [{
      "phase_id": "analysis", "title": "Analysis",
      "steps": [{"step_id": "check", "title": "Check", "description": "Verify the known case", "evidence_standard": "Retain the result"}]
    }]
  },
  "reason": "Why the USER revised the plan"
}
```

The body must pass the complete `PlanBody` schema, including at least one output,
phase and step. It retains ordered outputs, tracks, phases, steps, their stable IDs and
track references. It accepts all valid body changes rather than imposing a text
editor's narrower field set. Every nested field is validated; extra authority,
status, progress, outcome or approval fields are refused.

The server constructs USER authority from the path scope and the workspace's one
USER (`user_id` `user`, as documents name their USER author). The request cannot
choose an actor, run, model, environment, database or grant. The identity, and so
the operation namespace, is the same for every login. Plan versions recorded
before this used a digest of the login cookie; they show as USER too.

The route limits the complete request to 128 KiB, including whitespace and JSON
escaping. The core's canonical ASCII-JSON body limit remains 64 KiB; titles and
other text retain their existing schema limits. UTF-8, nonblank text, no NUL,
unique stable IDs/titles and valid references are checked by the existing core.
Neither Markdown nor LaTeX in body fields is executed.

## Mutation, history and response

Only `SessionPlans.revise` is called. It atomically commits the exact base's next
immutable body version, USER author, reason, event, operation/receipt and current
projection in the canonical FULL-durability transaction. Existing progress and
workflow status remain attributed history. Changed step intent makes its older
assertion nonmatching; a body edit cannot silently certify or reset progress.
A body edit does not change the plan's workflow status. Terminal plans refuse new body edits.

Success is JSON:

```text
{project_id, session_id, receipt: PlanReceipt}
```

The receipt has the exact operation ID, `action: "revise"`, resulting `ref`,
event sequence, status revision and progress cursor. It is a historical
acknowledgement, not a current plan snapshot. An exact committed request under
the same USER namespace can replay after later versions or terminal state while
the logical session remains open. Changed request content under the same
operation conflicts. Consumers refresh the readonly GET to learn current state.

Stale body CAS and operation conflicts return 409, with `error.current` containing
the exact `PlanRef` when the core supplies it. Invalid JSON/schema or a tightened
core retention quota is 400; authority refusal is 403; an absent recorded scope
or plan in a healthy existing store is 404; request-envelope overflow is 413.
Closed sessions refuse with 409. Corrupt records/schema, a damaged project
record, I/O, busy locks and the response deadline return 503. A missing login is
401. Every route response, including guard failures, has `Cache-Control:
no-store`. Errors retain no submitted body, arbitrary
exception, SQLite value, credential or upstream response text.

An initial 400/403/404/409/413 refusal performs no new body mutation. A timeout,
disconnect, 503 or lost response leaves the caller's outcome potentially
unknown: the transaction may have committed. Repeating the request with the same
operation ID replays its receipt if it did. After a prior unknown
outcome, a later definitive refusal cannot disprove that earlier commit. For
example, closure is checked before operation replay; a closed session cannot
reconcile the old request through this route. A different login also cannot
replay the earlier USER principal. No operation-absence proof is exposed.

## Writing

The route checks the IDs, the content type and the body, then in a thread under
the six-second call timeout: it reads the project record and refuses a closed
session; it checks through the app's read-only `DocumentStore` connection that the
session has a plan (`SessionPlans.summary`, `SessionPlans.has_records`), so an edit
never creates a plan; and it calls `SessionPlans.revise` through the app's writable
connection. `revise` checks again, in its own transaction, that the session is
open, so a close that lands after the route's check refuses the edit (409), and an
edit never lands on a closed session. Between edits, the plan's version CAS and the operation ID are
the concurrency control. A concurrent model or tool operation is serialized by
the core transaction and CAS; no lock is held across a network or model await.

This is **logical-session CAS**, not an opaque cross-request session lifetime
fence. A later request in a reopened logical session may edit the current plan if
its ordinary CAS succeeds. The route provides no live worker or kernel continuity
authority. The existing MAIN successful-model current-plan reread fence observes
the attributed USER event; this backend adds no ACK queue or retroactive effect
cancellation.

## Tests and scope

Local tests use real HTTP and cover immutable USER, body and progress history,
exact replay and stale CAS, authority injection, request and core limits, closed
and terminal scopes, a close waiting for an edit in progress, a missing pointer
and SQL rollback. A scripted MAIN test edits through HTTP at the model boundary:
a read and an effect in the same batch stay fenced until the new read reaches a
successful model call.

Approval routes, USER workflow or progress editing, a history UI, explicit
cross-request session lifetimes and delegation are not built.
