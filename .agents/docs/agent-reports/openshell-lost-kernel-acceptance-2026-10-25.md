# Lost-kernel recovery acceptance on OpenShell 0.1.5 — 2026-10-25

Disposable synthetic qualification on babbage with the recovery-acceptance fixture
(`.agents/labs/recovery_acceptance.py`): a random `/var/tmp/ss-recovery-*` root, its own
gateway, TLS, provider state and loopback ports, and the fixture's synthetic streamed
model peer. No paid model call, no credential of the owner's, no change to the personal
instance or to the preserved SNOVA acceptance state. The peer drives the production
streamed model client, control, harness and worker paths; it says nothing about model
judgment or research quality.

## What was qualified

The question was A10's and the standing backlog item's: does lost-kernel recovery work
against a real worker, and does a cell that loses its kernel's state get the recovery
text built today (tools spec, Kernels 7: the lost generation's cells by message ID and
purpose, the files they wrote that survive, and "load saved files before recomputing").

Three runs, each from a fixed detached checkout, each torn down with the fixture's own
teardown and sanitized receipts with a SHA-256 manifest under
`/var/tmp/ss-recovery-evidence-<id>/` (outside Git):

| Run | Checkout | Steps | Result |
| --- | --- | --- | --- |
| 1 | `d0c6162d` | `tree`, `literal`, `automatic`, `history` | `tree`, `literal`, `history` passed; `automatic` failed on a stale assertion (below); `abort` |
| 2 | `fc8fc3ee` | `literal`, `lost`, `automatic` | `literal` and both `automatic` scenarios passed; `lost` failed before any kill (below); `abort` |
| 3 | `31c5eafd` | `literal`, `lost` | both passed; `abort`, since the hour-long Sage job had not reached its receipt within the wait |

OpenShell 0.1.5 (the CLI, gateway, VM driver, sandbox, supervisor and prover from the
v0.1.5 release, SHA-256 verified against the published digests) at
`~/.local/opt/openshell-0.1.5/bin`. Images `minimal` and `crypto-base:de3b570f`, and the
`orchestrator` image rebuilt from each checkout as `prepare` required (4 to 27 seconds,
from cache; loaded locally, never pushed).

## Observations

- **Recovery after real loss.** `tree` deleted the agent VM during a child's shell call
  and one explicit Resume completed the same root and child with the original effect
  reconciled. `literal` killed the exact saved kernel process and the agent VM; the
  opted-in literal cell was reconstructed into generation 2 and the check cell printed
  its exact output. Both runs met the known replacement drain refusal once, which the
  fixture retries after control reports disconnected, as designed.
- **The recovery text (run 3, `lost`).** A non-replayable seed cell with the purpose
  "Computing 20! as the lost-state seed" wrote `results/lost-seed.json`; its kernel's
  process (the PID and start time the seed cell itself reported) was killed; the next
  plain cell failed with `state_lost` and received the platform's `lost_state`: the seed
  cell by its archive message ID with that purpose, `saved_files`
  `["results/lost-seed.json"]`, and "Load saved files before recomputing; recompute the
  rest from these cells." The kernel's generation went from 1 to 2. No `earlier_cells`
  beyond the seed, since the generation held one cell.
- **In-call stream retry and automatic continuation (run 2, `automatic`).** One cut
  stream: one attempt, no saved failure, no automatic resume, `retried` recorded in the
  call's usage. Ten cuts: the first attempt saved as a model error, one automatic
  continuation, the second attempt answered. This is D100's behaviour as built; the
  fixture's earlier assertion of two attempts after one cut predated the in-call retry
  and was updated.
- **Tool mix (Kernels 13).** `tool-mix` over run 3's store: 4 runs, 1 counted from
  events (the run cut short by the VM loss), 4 python cells, 2 shell calls (the holding
  call that reconciled after the interruption now counts once), 1 job, kernel state lost
  once. No shell call ran Python, so no platform note appeared, as expected.

## Fixture changes made for this acceptance (all committed on `k8s-sync`)

- `lost`: a new step for the non-replayable case, with the kill aimed by the PID and
  start time the seed cell reports (the first version matched kernels by reading their
  working directories under `/proc`, which the probe cannot do inside the sandbox).
- `automatic`: two scenarios, one cut and ten cuts, matching the in-call retry.
- `tool-mix`: counts runs that ended without a kept record from their events, and a
  call reconciled after an interruption once, in the cut run.
- The fixture's `--duration 3600` Sage job needs about an hour before `finish`; a run
  that wants `finish` rather than `abort` should use a shorter duration or wait for it.

## Boundaries

Not assessed: model judgment, the quality of a recomputation an agent performs from the
recovery text, suspension of an idle environment (the lab turned none on), Kubernetes
placement (local VM driver only), and recovery of state beyond one literal cell, which
remains the backlog's "broaden verifiable provenance" item.
