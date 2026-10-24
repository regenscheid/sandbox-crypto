# Operator recovery controls

Implementation contract for explicit uncertainty resolution and USER token renewal. These are authenticated
workspace operator routes, protected by the existing login and same-origin write
guard. They are never model tools and never contact a model, worker or executor.

- `GET .../sessions/{session}/recovery` returns independently paged work and Resume
  request/status rows, at most 100 each. Work includes exact current attempt/epoch,
  state/reason and original unresolved model/tool IDs, paged separately when needed.
- `GET .../sessions/{session}/recovery/works/{work}` pages unresolved identities for
  that exact work across its original attempts, at most 100 per kind; no tool argument
  payloads are copied into listings. Cursor queries use indexed identities/sequences.
- `POST .../sessions/{session}/recovery/resolve` accepts exact work, current attempt,
  epoch, original run, kind, identity and a nonempty bounded operator reason.
  Kinds are `model_usage`, `model_reservation`, `fold_estimate_link`, `operation`,
  `kernel_recompute`. Model usage requires an explicitly supplied finite
  nonnegative cost; omitted cost never means zero.
- Inside one write transaction, reject wrong scope, stale current attempt/epoch,
  completed/stopped work, running work, a live session lease or queued/running
  Resume. Exact identical decisions are idempotent; different decisions for the
  same original identity conflict. A retry never duplicates archive messages.
- Model settlement updates only the exact original run's pending intent belonging
  to this work. It retains the operator's supplied cost and uncertainty note,
  without fabricating model output or token counts. Optional observed input/output
  token counts must both be supplied or both unknown; delegated work with a token
  grant requires both counts before settlement. Archive records retain the exact
  original child attempt parent rather than the stable parent work identity. Supplied cost counts in budgets
  by the original intent's dispatch time and model role, explicitly labelled as an
  operator declaration in the decision and archive notice.
- `model_reservation` leaves actual usage and charge unknown and retains the
  original unresolved intent. It requires a positive finite cost upper bound and
  positive input/output token upper bounds. The bounds count in cost and token
  gates until observed usage is available; the archive labels them as a reservation.
- A legacy cancelled fold estimate without an intent ID remains unpriced even
  when an unresolved fold intent has a reservation. `fold_estimate_link` explicitly
  binds the stored copy's exact source, position, event sequence when applicable,
  and JSON value to one already reserved unresolved fold intent in the same
  original work. The reservation's input-token bound must cover the stored
  estimate. The operator must supply evidence in the reason. The link creates
  an archive notice and suppresses only that duplicate estimate; the reservation
  still counts and the actual charge remains unknown. Missing, changed, already
  linked or ambiguous copies are refused. Saved collection, summary and event
  fallbacks retain their precedence.
- Operation settlement requires the original unresolved intent. Archive a clearly
  labelled operator uncertainty tool result preserving original tool/call/arguments
  and operation ID; acknowledge result unavailability, never success or nonexecution.
  This satisfies pending-intent reconciliation without repeating an operation.
- Kernel recompute requires explicit acknowledgement of lost state and an existing
  saved kernel observation. Its SHA-256 digest is the required `identity`, and
  `run` must equal the supplied current attempt. A changed snapshot is refused.
  Archive a visible notice retaining the acknowledged IDs, clear that observation and
  retain the decision; no kernel/code/model execution happens automatically.
- On Resume, a lost Python kernel may be reconstructed without a model call only
  when its entire bounded cell history is journaled and each cell was explicitly
  marked replayable, contains only fixed literal assignments, made no observed
  file changes, and used no skill bindings. An unjournaled cell intent, missing
  result count, changed package revision, live replacement kernel, or prior
  unfinished reconstruction intent refuses automatic replay. The worker's
  conditional restart will not replace a kernel that appeared after observation.
  The reconstruction intent is durable before the first worker effect; uncertain
  or partial effects require explicit operator review rather than a silent retry.
  A successful reconstruction journals the replacement lineage for later losses.
  Recovery status exposes a pending reconstruction identity and detail across
  the session. New main work is refused while a session-kernel reconstruction is
  unresolved. Resume acknowledges lost state itself when no operator has (D100),
  recording the same `resolution:kernel_recompute` provenance marked automatic,
  restarts a live partial kernel clean, and atomically records the abandoned intent
  and an empty snapshot. The next cell starts a clean kernel. The operator decision
  remains available, for a reconstruction left outside a Resume.
  The conditional restart field raises the lockstep worker protocol to 10.
- Decisions live in `research_observations`, scoped by project/session/work/kind and
  original identity, with detail bounded to 16 KiB. Decision, pending-intent settlement
  and visible archive record commit atomically. Oversized original tool arguments
  fail closed rather than being truncated or executed.

*Removed 2026-10-24 (D102):* the USER token override described next, and its
`token-grants` routes, are gone; a child's tokens are a soft cap that checks in with its
parent. Earlier `token_grant` observations stay in the store as history.
`POST .../sessions/{session}/recovery/token-grants` was a narrow USER recovery
override for paused direct children of the root whose stop reason is `run_budget`.
It takes the exact work, current attempt, epoch, expected grant revision, a new
cumulative token ceiling, and a reason. The child must have an original durable
blueprint and delegation. The new ceiling must exceed its previous grant without
exceeding the current project check/task setting; the project must have a spending
cap. An active work tree, live lease or queued Resume refuses it. The immutable
decision and archive notice commit together; an identical retry returns the same
decision, including after the work later resumes. A changed request for that revision
conflicts. On Resume, the work's latest cumulative ceiling minus lifetime observed
tokens and unknown-call token reservations becomes the next attempt's allowance.
The original blueprint, delegation brief and prior usage remain intact. Grants follow
the stable work identity across later attempts. Nested children are excluded until
ancestor-aware allocation is built. This endpoint is an operator override, not a
claim that the calling agent evaluated progress or approved the extension. The
project's cost limit still checks observed/reserved spend at safe points; concurrent
future calls are not pre-reserved by a token grant.

Focused fixtures cover pagination/scope, all stale/active/terminal refusals, explicit
cost validation, exact original-attempt matching, idempotence/conflicts, atomic
rollback, preserved operation identity and no dispatch, and kernel acknowledgement.
