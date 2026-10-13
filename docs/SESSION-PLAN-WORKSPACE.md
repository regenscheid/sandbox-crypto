# Read-only workspace session plans

Date: 2026-10-12. This backend exposes the bounded current projection from the
[internal session-plan store](SESSION-PLANS.md). It reads recorded control state;
it does not create plans, approve work, start research, contact a worker or wake a
session. GUI polling and MAIN tool integration are independently delivered
consumers. A USER editor, history pages and approval enforcement remain separate.

## Authenticated scope and response

`GET /api/v0/projects/{project}/sessions/{session}/plan` requires the existing
workspace login and host guard. The request accepts only its path scope, with no
query parameters or body. The server derives the read authority from that scope
and a digest of the original authenticated cookie. Browser JSON cannot choose an
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

The wrapper revalidates copied models and exact scope/reference/base/cursor,
ordered step coverage, current fingerprints, completion coherence and
same-project outcome evidence before returning bytes. It preserves strict types
and timezone-aware receipt and retained timestamps. The entire UTF-8 response,
including wrapper fields, must fit 256 KiB. Excess data refuses the read; there is
no partial JSON or silently dropped step/outcome.

## Registry continuity and closed history

The reader opens a non-symlink project directory and regular project record with
`O_NOFOLLOW`, using `O_NONBLOCK` for the final file. It checks the file's size
before allocation, reads at most the 1 MiB registry limit plus one byte, and
validates its exact project, session, environment binding and timestamps. It
retains the original file and directory descriptors until all admitted work ends.
It never uses the unbounded `ProjectStore.get` or a lifecycle method.

After the canonical SQL read, a second bounded registry snapshot must match the
held root/file identity, original bytes and exact session/context. All supported
registry writers atomically replace their records. Keeping the original inode
open detects a complete close/reopen cycle even if identical bytes are restored.
Any concurrent project-record replacement is conservatively unavailable,
including an unrelated edit. This is receipt-time continuity, not a lock over
future registry changes or a promise about unsupported in-place writers.

The original login is checked before reads and after every asynchronous boundary,
including after the final registry check. Logout followed by a new login does
not authorize a reply belonging to the old cookie. No lifecycle lock is held
across file or SQL work.

Closed sessions remain readable as recorded history and return their closure
time. The reader neither reopens them nor changes activity time. A missing
project/session is 404. A wholly absent plan namespace or a scope with no retained
plan rows yields 200 with `plan: null`. Partial schema, damaged canonical rows, or
retained scoped history without its current pointer is unavailable (503), rather
than an apparently empty plan. An ended login is 401; invalid request scope,
query or body is 400. Success and errors for this GET carry `Cache-Control:
no-store`, including host-guard refusals.

## Ownership, quotas and shutdown

[`plans.py`](../packages/workspace/src/sandbox_science/workspace/plans.py) owns a
separate **readonly** `DocumentStore` connection to the app's canonical control
path. Even an injected writable app store cannot initialize plan schema through
this reader. The read-only store sees committed progress from another connection
without requiring a saved run or completed model turn. GET never creates a
database, migrates a schema, initializes a writer or falls back to another scope.

Admission is immediate and bounded: eight actual owned reads globally, two per
login and one per project/session. A private one-thread executor performs the
bounded registry and SQL work, so it cannot occupy the shared executor with
blocked calls. Queued work counts toward these limits. SQL uses the core's
consistent compact read and bounded record projections; no full history or
unbounded progress notes are materialized.

The caller has a six-second response budget including queue time. Timeout or
caller cancellation cancels only the owned coroutine; an admitted file/SQL call
retains its task, admission and descriptors until it actually finishes. An HTTP
disconnect does not prove that server work ended. Arbitrarily blocked filesystem
or SQLite I/O has no claimed hard cleanup deadline. While cleanup is blocked,
capacity remains held and shutdown waits. A canceled initial registry read also
closes the descriptors returned by its actual completed thread.

Shutdown stops admission and drains actual queued/running work before closing
this reader's connection and executor. Repeated cancellation cannot detach that
drain. The app's nested cleanup preserves kernel-proxy and shared WorkerClient
closure after the plan reader settles, including when its close propagates
cancellation. The app's document stores are separate connection owners.

## Validation boundary

Focused local HTTP and direct-owner tests cover committed live progress,
closed/scoped history, revised intent, explicit outcomes, authentication and
logout, full-response refusal, absent versus corrupt state, bounded giant rows,
registry growth/symlinks/FIFOs, copied-model coherence, exact-byte close/reopen
ABA, immediate admission, actual HTTP disconnect, cancellation-owned descriptors
and SQL work, repeated shutdown cancellation and shared-client cleanup order.

These tests use synthetic control activity and local aiohttp composition. They
establish no browser rendering, model planning, automatic polling, research
execution, approval enforcement or OpenShell behavior. Root-owned combined
gates and browser acceptance remain separate from this source milestone.

Local Python 3.13 validation: **47 focused API/ownership cases passed** and
**242 affected cases passed**, including those 47 plus the existing workspace
app/API/kernel and core SessionPlans suites. Ruff/format and combined
workspace/orchestrator Pyright passed. No full repository suite or runtime/browser
acceptance is claimed by this author milestone.
