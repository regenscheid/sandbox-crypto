<!-- SPDX-License-Identifier: MIT -->
# USER plan text editing

The workspace offers a structured text editor for an existing, observed, open
session plan. It edits goal and feasibility, output titles/descriptions/evidence
standards, track and phase titles, and step titles/descriptions/evidence standards.
It preserves stable IDs, array order, track links, and all untouched body fields.
No plan creation, reordering, progress, outcome, approval or execution action is
added. An awaiting-approval plan can have its wording revised without changing
its status. Closed and terminal plans remain read-only.

The editor uses the authenticated USER
[plan changes endpoint](SESSION-PLAN-EDITING.md). The server owns authorization,
canonical body validation, immutable version creation and operation replay. The
browser supplies no author or grant. Existing MAIN safe points observe the
resulting USER-attributed version through the existing plan reread fence; this
editor adds no model calls or autonomous scheduling.

## Current text and drafts

Plain text inputs show human-readable fields rather than raw JSON or authority
identifiers. The current version and recorded actor remain visible. A dirty draft
keeps its original base and reason while polling continues. Empty or temporarily
invalid text is retained so it can be corrected. Save requires nonempty valid
text: titles are at most 128 Unicode characters, other text and reason at most
4,096. The browser checks the 64-KiB canonical ASCII-JSON body limit before the
128-KiB request limit; server validation remains authoritative, including casefold
step-title uniqueness.

Draft storage is keyed by exact project, session and plan. It uses tab-scoped
session storage plus an in-memory copy, with a 512-KiB whole-envelope cap. Storage
failure preserves memory and displays a warning; it does not promise survival of
reload or tab closure. A new save is held when its exact retry cannot be stored.
Malformed or oversized existing draft storage is left untouched and editing is
held rather than silently replacing it.

A conflict keeps all draft text. “Keep draft; use current version” explicitly
adopts the observed current base while retaining the draft's complete text; it
performs no merge. It is allowed only when the complete nontext structure, IDs,
order and references match. If structure changed, “Load current; retain draft”
starts with the current body and keeps the old text with its own original
baseline for manual copying. One earlier text draft is retained; another load
cannot silently replace it. A successful newer save and current refresh preserve
that retained draft.

## Unknown save outcomes

Before a write, the browser retains its exact operation ID, base, body and reason
in the plan's draft, kept by the workspace's shared draft store ([saving](WORKSPACE.md#views)).
Transport failure, abort, 401 or 503 can follow a committed write. These outcomes
remain unknown. Retry uses the exact retained request; editing its intent is held.
After an earlier unknown outcome, a later 404 or 409 does not prove the earlier
write absent. Closed sessions can refuse before replay.

Operation replay is scoped to the authenticated USER principal. A new login may
have a different cookie digest and therefore a different operation namespace.
Retaining the operation string does not prove cross-login replay identity. After
a fresh current read, the USER can explicitly continue from current text while
keeping one exact earlier save labeled unresolved. This does not prove failure,
merge earlier changes, or fabricate an acknowledgment. A new intent gets a new
operation ID. A second unresolved outcome cannot evict the retained predecessor;
its retry remains available, but further fresh-current continuation is held.

The response is a historical save acknowledgment. Its exact project/session,
operation, action, plan and base-plus-one version must match. It triggers the
existing GET refresh and never replaces a newer observed plan, approves the plan,
or starts work. An acknowledged draft resets to the saved current body only when
the GET proves the exact version and body; earlier retained text and unresolved
requests remain available.

## Request lifetime and limits

One write at a time per project and session: a second save waits until the
first request settles, while another session stays independent. The request goes
through the workspace's shared request function with an eight-second deadline and
an 8-KiB answer limit. A 409 carries the current plan reference; other refusals
keep their status. Late unmount, scope and sign-out responses cannot apply to a
replacement editor, and after sign-out no editor writes.

Abort is not proof of server cancellation. The server checks logical session
openness at admission and commit; this route provides no opaque cross-request
session-open lifetime guarantee. The editor neither reopens a session nor claims
that a retained retry belongs to an earlier open lifetime.

The tests in `web/workspace/src/lib/planEdits.test.ts` cover reference
preservation, invalid drafts, changed structure and reload, refresh after an
acknowledged save, conflict and uncertainty, new intent after explicit
reconciliation, storage failure, one write per session, response limits,
sign-out, read-only scopes and escaped server-side rendering. A browser run
with a withheld response is in the [acceptance report](../.agents/docs/agent-reports/user-plan-editing-2026-10-13.md).
