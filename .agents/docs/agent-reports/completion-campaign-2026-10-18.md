# Completion campaign, 2026-10-18

Status: implementation and acceptance in progress. Baseline local `dev` is
`8ef121bd`. The owner authorized $150 aggregate new model-testing spend. This
does not restart SNOVA or authorize new token-grant increments. Observed test
spend at campaign start: $0; no paid calls dispatched yet.

## Acceptance checklist

1. Automatic owned agent/worker recovery while a genuine long Sage job runs;
   original operation, one start, durable verified result, orchestrator-level
   Resume; host-process-loss recovery without rebooting the real host.
2. Real child narrative folds retain reasoning, rejected approaches, outstanding
   questions and exact evidence across continuation. Independently check the
   scientific answer and calibrate the cheaper reviewer on blinded fixtures.
3. Time, turn and retained-storage warning/pause/extension policies are reachable
   by parents/operators; bounded root renewal requires durable progress. Explicit
   Stop and the USER-only token-grant rule remain authoritative.
4. Live context inspector includes timestamped folds, auxiliary usage, actual
   captured dispatch composition and indexed access to original visible history.
5. Fold retry/fallback diagnostics explain each extra call; validate receipts and
   limits against both failure fixtures and real narrative folds.
6. MCP schemas, policy and explicit current availability checks, plus Overview,
   Memory and session navigation refinements pass authenticated browser review.

All items require implementation, independent review, relevant tests, updated
documentation, integration into signed local `dev`, and honest live verification.
Owned lab resources must be removed after evidence export; unrelated services and
the original research archive remain intact.

## Branch overlap

Fresh remote-head inspection found only `main`, `codex/agent-messaging` and
`spike/pydantic-ai`. Prior foundations and adaptive-resource branches are
patch-equivalent on `dev`. The older reviewer-progress branch has different
patch identities, but its bounded window and shrinking retry behavior are already
implemented with subsequent fixes; do not transplant the older overlapping patch.

## Infrastructure recovery decision

Autonomous infrastructure recovery is an explicit launcher policy. It restarts
owned host components and replaces only the recorded agent VM, preserving the
research VM and original worker-operation identities. A durable bounded retry
record prevents launcher restarts from resetting a failure loop. Automatic root
Resume admits interrupted work only under that policy, honors Stop and token/cost
gates, and reconciles supported original operations before dispatch. Unsupported
unknown effects remain visible rather than replayed.

## Intermediate verification

Signed product increments now include policy-bounded time/storage/root renewal,
actual dispatch capture and indexed original history, MCP structural schemas and
explicit availability checks, and Memory/Overview/session refinements. Independent
review found and corrected: huge JSON schema integer handling, partial HTTP-body
parsing, missing local-draft durability warnings, oversized diagnostic capture
blocking model dispatch, and a nonadvancing UTF-8 chunk edge. Bounded blob reads
are receiving a further fix after review found whole-field materialization.

Approved root/child time and storage pauses now qualify for automatic original-tree
Resume (four public-API regressions; combined 39 tests pass). Original committed
resource-decision receipts reconcile by exact identity and request without replay.
Its author suite has 86 passing focused tests; integration and peer recheck remain.

The frozen infrastructure runtime at `9de21637`, image `9b957d6f`, has passed
automatic worker restart, agent-VM replacement and host-process-loss root Resume
while one genuine 3600-second Sage job continues under its original operation.
The host probe exposed a fixture race: systemd had not unloaded the killed
transient unit when the new start was requested. That start was explicitly refused,
then admitted once after verified `LoadState=not-found`; no research request was
reposted. Helper `5da1a6c6` waits for collection. Final Sage verification, evidence
manifest and complete owned teardown remain required. No paid calls yet.
