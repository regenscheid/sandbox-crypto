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

The server constructs USER authority from the path scope and SHA-256 digest of
the original authenticated login cookie. The request cannot choose an actor,
run, model, environment, database or grant. The digest is an opaque login
principal, not the raw cookie or an account identity stable across logins. A new
login has a different operation namespace even for the same operation string.

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
`awaiting_approval` remains recorded workflow state and does not become active.
Terminal plans refuse new body edits.

Success is bounded complete JSON, at most 8 KiB:

```text
{project_id, session_id, receipt: PlanReceipt}
```

The receipt has the exact operation ID, `action: "revise"`, resulting `ref`,
event sequence, status revision and progress cursor. The wrapper revalidates its
scope, action, submitted plan and exact base-plus-one version. It is a historical
acknowledgement, not a current plan snapshot. An exact committed request under
the same USER namespace can replay after later versions or terminal state while
the logical session remains open. Changed request content under the same
operation conflicts. Consumers refresh the readonly GET to learn current state.

Stale body CAS and operation conflicts return 409, with `error.current` containing
the exact `PlanRef` when the core supplies it. Invalid JSON/schema or a tightened
core retention quota is 400; authority refusal is 403; an absent recorded scope
or plan in a healthy existing store is 404; request-envelope overflow is 413.
Closed sessions refuse with 409. Corrupt records/schema, missing or changed
control files, I/O, busy locks, capacity and response deadlines return 503.
Ended original logins return 401. Every route response, including guard failures,
has `Cache-Control: no-store`. Errors retain no submitted body, arbitrary
exception, SQLite value, credential or upstream response text.

An initial 400/403/404/409/413 refusal performs no new body mutation. A timeout,
disconnect, 401, 503 or lost response leaves the caller's outcome potentially
unknown: an admitted transaction may have committed. Postcommit binding or
receipt-validation failures are unavailable responses. After a prior unknown
outcome, a later definitive refusal cannot disprove that earlier commit. For
example, closure is checked before operation replay; a closed session cannot
reconcile the old request through this route. A different login also cannot
replay the earlier USER principal. No operation-absence proof is exposed.

## Scope and true ownership

Admission immediately reserves one scope, at most two requests per login and
eight globally. Body reads, queued work and actual synchronous work all retain
those slots. A private one-thread executor owns bounded registry reads and a
separate lazy writer connection to the app's canonical control path. The original
app connection and readonly viewer are not borrowed for mutation.

The writer retains a bounded `O_NOFOLLOW`/nonblocking regular registry file and
its directory descriptor. It acquires the canonical per-project lock with
nonblocking `flock`, rejecting symlinks, special files and multiply linked lock
files. Fresh open-session checks compare the held registry inode, bytes and
scope before final write admission and after commit. Supported registry writers
use the same project lock and atomic record replacement. A close/reopen during a
request is detected even if its visible scope returns to the old value.

This is **logical-session CAS**, not an opaque cross-request session lifetime
fence. No opened-at hash or reopen API is introduced. A later fresh request in a
reopened logical session may edit the current plan if its ordinary CAS succeeds.
This lock also provides no live worker/kernel continuity authority.

The control database must already exist as a regular non-symlink file. Its file
and parent descriptors are retained to detect observed replacement; an existing
plan/schema is read before initializing the private writer. An edit does not
create a missing database or bootstrap an absent plan. The native SQLite
connection still opens the canonical pathname: these checks do not claim an
OS-atomic defense against an external host replacing the database during initial
connection opening. Supported application operations do not replace that file.

Original login, observed transport liveness, cancellation, open registry and
file/lock binding are checked after body/queue preparation, immediately before
core write admission. SQLite uses a private connection with zero busy timeout,
so competing SQL ownership refuses rather than silently queuing after admission.
Logout or disconnect after admission is not transaction rollback. A concurrent
model/tool operation is serialized by the existing core transaction and CAS;
no lock is held across a network or model await.

The response budget is six seconds including body and queue time. Timeout or
repeated cancellation never releases a live thread's admission, descriptors or
connection. Not-yet-admitted work observes cancellation before writing. Admitted
work drains to real completion; shutdown stops admission and waits before
closing its writer and descriptors. Arbitrarily blocked filesystem/SQLite work
has no hard reap deadline and continues to hold bounded capacity. App cleanup
still attempts reader, kernel proxy and shared client cleanup if this owner
fails. The existing MAIN successful-model current-plan reread fence observes
the attributed USER event; this backend adds no ACK queue or retroactive effect
cancellation.

## Tests and scope

Local tests use real HTTP and cover immutable USER, body and progress history,
exact replay and stale CAS, authority injection, request and core limits, closed
and terminal scopes, registry close/reopen and special files, lock contention, SQL
rollback, original-cookie revocation, observed disconnect, cancellation and
shutdown ownership. A scripted MAIN test edits through HTTP at the model boundary:
a read and an effect in the same batch stay fenced until the new read reaches a
successful model call.

Approval routes, USER workflow or progress editing, a history UI, explicit
cross-request session lifetimes and delegation are not built.
