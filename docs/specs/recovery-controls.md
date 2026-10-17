# Operator recovery controls

Implementation contract for explicit uncertainty resolution. These are authenticated
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
  Kinds are `model_usage`, `operation`, `kernel_recompute`. Model usage requires an
  explicitly supplied finite nonnegative cost; omitted cost never means zero.
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
- Operation settlement requires the original unresolved intent. Archive a clearly
  labelled operator uncertainty tool result preserving original tool/call/arguments
  and operation ID; acknowledge result unavailability, never success or nonexecution.
  This satisfies pending-intent reconciliation without repeating an operation.
- Kernel recompute requires explicit acknowledgement of lost state and an existing
  saved kernel observation. Its SHA-256 digest is the required `identity`, and
  `run` must equal the supplied current attempt. A changed snapshot is refused.
  Archive a visible notice retaining the acknowledged IDs, clear that observation and
  retain the decision; no kernel/code/model execution happens automatically.
- Decisions live in `research_observations`, scoped by project/session/work/kind and
  original identity, with detail bounded to 16 KiB. Decision, pending-intent settlement
  and visible archive record commit atomically. Oversized original tool arguments
  fail closed rather than being truncated or executed.

Focused fixtures cover pagination/scope, all stale/active/terminal refusals, explicit
cost validation, exact original-attempt matching, idempotence/conflicts, atomic
rollback, preserved operation identity and no dispatch, and kernel acknowledgement.
