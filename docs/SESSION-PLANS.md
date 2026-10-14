# Internal session plans and progress

The session plan core records each session's versioned plan and the attributed
progress assertions against it, implementing PD-1–5 and PD-7–9 of the
[plans and delegation specification](specs/plans-and-delegation.md). It is an
internal library in the control store. Its callers are the MAIN
[plan tools](ORCHESTRATOR-PROMPTS.md#implemented-stateful-main-planning-tools), the
read-only [workspace view](SESSION-PLAN-WORKSPACE.md) and USER
[body editing](SESSION-PLAN-EDITING.md). The core itself starts no planner, enforces
no approval gate and schedules no delegation.

`SessionPlans` in
[`session_plans.py`](../packages/orchestrator/src/sandbox_science/orchestrator/session_plans.py)
borrows an already owned `DocumentStore`. Its synchronous operations use that
store's lock, consistent reads and `BEGIN IMMEDIATE`/FULL-durability write
transactions. It never closes the connection or creates a thread. An async
caller must own and drain its admitted threaded calls before closing the borrowed
store, since cancelling a coroutine does not prove its SQL work stopped; the plan
tools do so.

## Authority and identity

Every method takes a separate trusted `PlanAuthority`, scoped to one project and
session. A USER requires its authenticated user identity. MAIN requires a trusted
run identity and may retain configured model alias, actual resolved model and tool
call. An unavailable resolved model stays `None`. Delegated authority additionally
requires an exact `PlanRef` and track grant. Constructing this internal DTO is not
authentication; no model JSON, tool output, evidence link or stored status grants
that authority. A caller must resolve it from its actual trusted context. No
production caller constructs delegated authority yet: the delegation prototype that
did is kept on the tag `archive/delegation-2026-10`, and delegation will be rebuilt
as a role of the one loop ([refactoring plan](../.agents/refactor-plan/PLAN.md),
Phase 4 item 7).

Copied Pydantic models are strictly revalidated, including nested scope, grant,
body, evidence and outcome fields. Agents cannot use the reserved USER run name,
carry a USER identity, or supply USER-only attribution. USER authority cannot carry
agent fields. All IDs are bounded ASCII identifiers; text is strict UTF-8 data
with no NUL or blank-only values. Markdown, LaTeX and other text are retained
without rendering or execution. No dependency or worker capability is added.

Operations use a separate session-plan namespace: the canonical tuple of role,
principal and operation ID, under the project/session key. The exact authority and
request enter the fingerprint. Changing a tool-call or resolved-model field on a
retry therefore conflicts; a wrapper may omit optional tool-call attribution from
this core identity and retain the actual call separately, but must preserve the
original trusted run/model provenance. It cannot rewrite a recorded actor.

## Version, workflow and progress transactions

A session has one current plan pointer. `publish` creates version 1 in `draft`;
a subsequent new plan requires the exact current predecessor reference and a
terminal `completed` or `abandoned` predecessor. `revise` requires the exact
current reference and appends a version with its base, author, reason and trusted
host receipt time. An older version remains readable. Stale updates return a
conflict with the current reference; the store never silently merges them.

Bodies retain goal, feasibility assessment, desired outputs/evidence standards,
ordered phases, ordered steps and named tracks. Step IDs and case-insensitive short
titles are unique throughout a plan; phase and track IDs are also unique. Steps
may reference only declared tracks. Reusing a stable step ID preserves its
history. Removed IDs disappear from the current projection but stay readable in
pinned progress history. A substantially different intent should get a new ID.

`progress` names the exact current body version, a step, its last observed progress
cursor, status, note and optional same-project evidence claims. MAIN and USER can
update current steps. A delegated run can update only a step in its exact granted
current version and track; moving/removing that step or revising the plan
invalidates the old grant. Delegated authority cannot revise a body, replace a
plan, change workflow status or approve anything.

Each assertion binds a hash of its exact current step fields and phase ID.
Unchanged intent can carry progress across a body revision. Changed fields, phase
or track leave the old assertion visible but `matches_current_step=false` until
reaffirmed against the current version. An old completion cannot silently certify
changed work. The per-plan progress cursor is monotonic; each step's CAS cursor is
the sequence of its latest assertion, so unrelated steps can advance independently.

The optional `expected_status_revision` on `progress` enters the operation digest
and is checked inside the same write transaction. An adapter that checks `active`
must supply it to detect a concurrent transition to `draft` or
`awaiting_approval`. Omission is an internal-core choice and provides no atomic
active-status predicate. `set_status` always checks exact body version, workflow
status revision and plan progress cursor together.

Every successful mutation commits immutable version/progress/status records as
applicable, an attributed local event bound to its exact operation ID/request hash,
its immutable operation/receipt and the current/head projections atomically. A replay returns the exact historical
receipt, even after newer state exists; that acknowledgement does not establish
what is current. Reads and mutations remain within the authority's project and
session. Events use a session-local sequence, so another scope's activity creates
no sequence holes or global-cursor disclosure.

## Completion is an attributed outcome

The draft PD-10 terminal-step rule allows `completed`, `blocked` and `skipped`.
This implementation adds an explicit goal outcome clarification, pending central
spec reconciliation: workflow `completed` must include `achieved`,
`partially_achieved` or `not_achieved`, a summary, an explicit evidence list and an
explicit remaining-work list. This is an implementation clarification, not a claim
that the draft specification was previously approved with these exact fields.

All current steps must have a matching current-intent assertion in a terminal
status. `achieved` additionally requires every current step `completed` and no
remaining work. Other outcomes require nonempty remaining work. `abandoned` is a
separate terminal workflow state with an attributed reason and retained progress;
it carries no implicit success outcome. Terminal plans cannot receive further
body/progress/status mutations; another plan is an explicit new publication.

These outcomes and evidence references are assertions. Typed artifact references
pin revision/digest; execution references pin an attempt; ledger references pin a
revision. They are same-project claims, with no resource lookup, verification,
parser, read grant, worker access or reviewer execution. No consistency reviewer
checks these completion claims.

`awaiting_approval` and `active` are workflow records only. The store permits trusted
USER/MAIN status bookkeeping; it does not implement PD-6's exact-version USER
approval, revoke execution grants, or enforce an execution barrier. Callers must
not turn status alone into approval or execution authority; the plan tools refuse
to activate an `awaiting_approval` plan.

## Bounded reads, retention and failures

Public internal methods are `publish`, `revise`, `progress`, `set_status`, `summary`,
`current`, `compact_current`, `version`, `history` and `events`. `summary` returns a small current
reference, workflow/status/progress/event cursors, goal-outcome tag and last
attributed event, without building the full current projection. It validates the
latest event against its one bounded canonical mutation, including original
attribution. `current`
returns the exact current body and current-step projections, including their
latest attributed assertions; it is not suitable for dumping at every model safe
point. `compact_current` pins the full body/head and every current step in one
read transaction, with latest assertion ref/sequence/fingerprint/status/author/time
and evidence count, explicitly excluding note and evidence content. It remains
available for long-note plans that exceed the full `current` aggregate budget;
excess compact metadata still refuses rather than dropping steps. `version` pins
one immutable body. `history` pages versions, statuses or
progress, optionally filtering a step including removed IDs. `events` pages the
scoped journal. Page targets pin a finite prefix while later mutations continue.
Read callers must respect `more` and the returned cursor; a filtered step's
progress sequences need not be contiguous because other steps share its plan
cursor.

| Engineering bound | Value |
| --- | --- |
| Encoded body | 65,536 bytes, canonical ASCII JSON |
| One retained record / SQL prefix | 81,920 bytes / bound plus one byte |
| One history/event page | At most 16 records and 262,144 record bytes |
| Current projection response | 2 MiB; explicit refusal beyond it, use history pages |
| Compact current projection | 256 KiB |
| Compact summary | 8 KiB |
| Body structure | 128 steps, 16 phases, 16 tracks, 32 outputs |
| Text / short title | 4,096 / 128 characters, also subject to encoded-byte caps |
| Evidence / remaining-work list | 8 / 16 entries |
| Project / global versions | 1,024 / 4,096 |
| Project / global progress records | 8,192 / 32,768 |
| Project / global total retained rows | 32,768 / 131,072 |
| Project and global metadata budget | 32 MiB each |

`PlanLimits` allows bounded operator overrides for retention counts and metadata.
Every immutable row, zero-content mutation, event, operation receipt and current
projection counts. Metadata accounting uses canonical record bytes plus a fixed
512-byte per-row reservation for bounded keys/row overhead; it is a logical
admission budget, not an exact SQLite/WAL/disk/RSS bound. There is no eviction,
expiry or silent forgetting of unresolved history. A full or tightened quota
refuses a new plan mutation atomically and leaves existing plans readable. It
does not block unrelated document saves or mutate their operation namespaces.

SQL projections clip record bytes before Python parsing, validate retained scope,
identity and counters, and cap aggregate page/current materialization. Corrupt or
unsupported retained state is refused rather than assigned a new cursor, actor or
completion. Historical records have immutable update/delete triggers. SQLite
I/O errors can still fail the whole transaction; the caller must report that
failure. The tables are part of the control store's schema
([living documents](LIVING-DOCUMENTS.md#the-control-store)); a store without
plans reads as an empty history. Plan versions are internal records; they are not published to the artifact
inventory.

## Tests

Local tests cover attributed USER edits and stale CAS; stable, changed and
removed steps; delegated track and version fences; workflow and progress races;
explicit outcomes; exact restart replay and scoped cursors; transactional quotas
and rollback; corruption and allocation bounds; immutable history and readonly
behavior. They are synthetic control-store tests.
