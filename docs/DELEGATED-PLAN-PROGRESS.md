<!-- SPDX-License-Identifier: MIT -->
# Internal delegated own-track progress

`delegated_plan_tools.py` supplies an internal adapter for an explicitly granted
child to read its current track and append progress through the canonical
[session-plan store](SESSION-PLANS.md). It does not register a model tool or launch
a child. The [delegation records](DELEGATION-RECORDS.md) remain the sole source of
prepared parent/delegation/child identity and handback history. Kernel routing,
model configuration and a controller-owned delegated run loop remain separate.

## Supplied authority and canonical admission

`await DelegatedPlanProgress.open(plans, records, projects, context, ...)` requires
an explicit `PlanAuthority(role="delegated")`, a canonical prepared mutation, the
original open `SessionSnapshot`, and a synchronous fresh-admission callback.
Constructing those objects or returning `True` is not authentication. The trusted
caller must establish and retain its controller authority; the callback is a
cooperative, bounded, non-effecting admission check, not an atomic authentication
barrier against an independently changing external controller.

The authority's original child, project/session, current plan/version and track
must match the prepared record and explicit environment context. The entire
granted track's current step IDs and fingerprints must equal the retained brief.
A historical `TrackPin`, kernel binding, model argument or handback cannot mint a
grant. The canonical prepared record must still have no terminal handback. Both
stores must borrow the same already initialized writable `DocumentStore`.

The factory borrows the caller's original session snapshot only until construction
settles. The caller retains its ownership. The adapter acquires and owns a new
snapshot descriptor, then proves continuity with that original. Every admitted
operation rechecks the exact open project/session/environment, held record/root
identity and current canonical pathname, including after SQLite acquires the
writer and immediately before commit. Any project-record replacement is
conservatively held, even if its bytes happen to be identical. Close/reopen,
changed environment or canonical root/file replacement cannot revive the adapter.

## Reads, successful delivery and progress

`read_current()` returns a complete bounded `GrantedRead`: exact scope, child,
delegation, event/head/status/progress cursors, track and every granted step. Each
step carries the compact canonical assertion, fingerprint and cursor. An old
assertion whose intent no longer matches is explicitly historical; its current
display status is `pending`. A new explicit prepared child/grant is needed after
a body-version change; the adapter never silently adopts it.

The host may call `model_delivered(read)` only after a successful model call
actually contained that exact complete read. It checks object identity, strictly
revalidated bounded contents, and the still-current head/status/progress/event
pin. Copied or mutated reads, history, safe-point summaries, mutation receipts,
replay, a failed model call or a same-batch read cannot clear this fence. This
trusted-host assertion establishes neither provider-wire receipt nor model
understanding or scientific verification. No model calls occur in this adapter.

`safe_point()` scans at most 64 canonical scoped events per operation and reports
whether another read is needed or the prefix remains held. Large existing
histories can be drained over multiple bounded calls; model delivery cannot skip
an undrained prefix. Any event from another actor, including USER, requires a new
complete read and successful delivery. Missing or malformed visited events
refuse. This validates visited prefixes, not all retained history in one call.

`progress(Progress(...), resolved_model=...)` offers only the existing bounded
progress request. Exact current body version, step cursor and mandatory workflow
status revision enter the canonical CAS transaction. Only an active original
grant with a cleared reread fence may write. A private non-owning facade invokes
the unchanged `SessionPlans.progress`; canonical child/session/admission checks
run inside the owner's actual `BEGIN IMMEDIATE`, before mutation and before
commit. The facade does not run constructors, create schema, open a second
connection, nest a transaction or close the borrowed store.

Canonical replay retains the original operation/authority/request and historical
receipt. Changing the payload or actual resolved model conflicts. Unknown model
identity stays null; an optional tool-call attribution supplied by the host also
retains its existing core replay semantics. A historical receipt never clears a
current reread requirement. Any failed owned operation restores its pre-call
event cursor, discards pending delivery and requires reread, so rolled-back events
cannot conceal a later USER event reusing that sequence.

`history(step_id, after=..., target=..., limit=...)` exposes only an originally
granted step, at most eight whole canonical rows. Decoded scope, step ID, version,
historical track and step fingerprint must agree with the canonical historical
body in the same read transaction. Corrupt filter keys cannot expose another
track's note. Rows are not clipped; the complete envelope is at most 256 KiB and
preserves its pinned target, cursor and `more` flag.

There are no create/revise/activate/approval/finish methods, cross-track writes,
grant defaults or automatic completion inference. A child asserting `completed`
does not finish a workflow or claim the goal achieved. Evidence references remain
typed claims and confer no resource read or execution authority. Stored workflow
status itself grants no execution or approval authority.

## Work ownership and limits

Each adapter admits one actual thread at a time, immediately refusing another
call rather than queueing it. Exact copied model/primitive types, declared field
counts, UTF-8/JSON expansion, finite integers and structural budgets are checked
before aggregate encoding. Requests have a 96 KiB preflight budget; complete
returned models have a 256 KiB cap. Core schema, record/page and transactional
retention quotas continue to apply unchanged.

`close()` refuses new work and drains the actual admitted thread before closing
only the adapter's snapshot descriptor. Repeated caller cancellation preserves the
first cancellation and waits for actual work to settle. The caller must keep the
borrowed stores alive through this drain. The adapter has no independent response
deadline; an outer timeout initiates cancellation but is not proof of completion.
A commit followed by cancellation or a lost return stays durably recorded, with
the return outcome unknown and the adapter held. No automatic retry occurs.
Project locking spans bounded control file/SQL work, never a model or worker
await. File/SQL failures, an event-loop/process abort or an uncooperative external
owner still need caller reconciliation; this is not a security isolation claim.

## Validation boundary

Focused isolated tests compose real temporary project/control stores with fake
controller admission. They cover exact grant/scope and canonical child ending,
status/step CAS, USER revisions and actor events, immutable replay/model conflict,
historical changed intent, copied-input bounds, corrupt history, rollback cursor
restoration, large-prefix safe points, canonical root replacement, setup failure,
and repeated cancellation/timeout/close through actual thread and FD drain.
There is no service, model, worker, VM, provider or research execution in these
checks. Independent review, combined repository gates and any later delegated
runtime acceptance remain separate from this source prerequisite.
