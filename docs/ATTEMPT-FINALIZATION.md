# Attempt finalization

`orchestrator/finalization.py` owns completion ordering for fresh children and
resumed branches. The harness assembles typed child evidence and passes it to
`AttemptFinalizer`; recovery retains responsibility for branch scheduling,
ownership claims and blocking interrupted work.

A child completes these steps in order:

1. Persist its exact typed outcome with `ChildSettlement.keep`.
2. Validate and save the final run record. A requested transcript is written first.
3. Acknowledge the deliveries named by that record, continuing other hooks if one
   acknowledgement fails. Archived delivery IDs are read in bounded pages.
4. Write the existing work completion receipt, which confirms persistence only if
   every requested write and acknowledgement succeeded.
5. Settle the saved child outcome, its delegation handback and ledger operation,
   then persist the settlement receipt.
6. Release the worker kernels only for completed or explicitly stopped child work,
   and not for a returned task that awaits its parent's review: its kernels stop when
   it is accepted or cancelled ([plans and delegation](specs/plans-and-delegation.md#parent-review),
   PD-46). A returned task's own child environment is not handed back or released at
   step 1 either: its hand-back is planned and kept with the result, and made when the
   task closes.

The fresh child path and resumed child path both settle before releasing kernels.
A resumed root uses the same record-and-acknowledgement steps without a child
checkpoint, handback or kernel release. The CLI and ordinary root run service retain
`harness.save` and its integer return value; it delegates to the same persistence
implementation.

`RecordCompletion` reports saved-record, acknowledgement and completion-receipt
stages independently of the run's status. A model failure can have status 1 and
fully confirmed persistence. `AttemptCompletion` additionally carries the settled
child result and whether kernels were retained, released or left for cleanup.
These values report the completed call; the existing durable receipts remain the
source of truth after a process restart. A typed settlement is kept per run (control
store step 88): a reopened task has one for each of its runs, and an outcome is written
only while its run is the delegation's latest, so a late replay of an earlier run's
settlement never writes over the next.

A saved record by itself does not prove successful child completion. Missing or
failed acknowledgement/completion receipts produce the existing
`persistence_error` handback, preserving its report without advertising a result.
Checkpoint, ownership, cancellation and settlement errors keep their existing
propagation rules. A settlement error leaves its durable checkpoint for replay;
cleanup has not yet run. Worker API failures and cleanup timeouts are logged and
reported as deferred cleanup without changing the committed handback. Resume
retries terminal child cleanup under its current root authority; the ordinary
kernel sweep remains available as well. Cancellation during cleanup propagates,
while the settled result stays durable.

The public crash tests terminate a process after record save, acknowledgement,
work completion and ledger commit through both caller paths. They reopen the
stores and verify truthful handbacks, delivery state and idempotent settlement.
The resumed crash fixture isolates finalization from admission and claim setup;
full Resume ownership and scheduling remain covered by the tree recovery tests.
