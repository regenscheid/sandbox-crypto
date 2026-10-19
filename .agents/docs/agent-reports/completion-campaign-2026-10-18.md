# Completion campaign, 2026-10-18

Status: implementation and acceptance in progress. Baseline local `dev` is
`8ef121bd`. The owner authorized $150 aggregate new model-testing spend. This
does not restart SNOVA or authorize new token-grant increments. Observed test
spend at campaign start: $0. The real-model calibration started on 2026-10-19;
final costs and qualification are recorded below after settlement.

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
now avoid whole-field materialization; the oversized-field SQLite-limit regression
and independent recheck pass.

Approved root/child time and storage pauses now qualify for automatic original-tree
Resume (four public-API regressions; combined 39 tests pass). Original committed
resource-decision receipts reconcile by exact identity and request without replay.
Its author suite has 86 passing focused tests and independent review is clear.

The frozen infrastructure runtime at `9de21637`, image `9b957d6f`, has passed
automatic worker restart, agent-VM replacement and host-process-loss root Resume
while one genuine 3600-second Sage job continues under its original operation.
The host probe exposed a fixture race: systemd had not unloaded the killed
transient unit when the new start was requested. That start was explicitly refused,
then admitted once after verified `LoadState=not-found`; no research request was
reposted. Helper `5da1a6c6` waits for collection. Final Sage verification, evidence
manifest and complete owned teardown remain required.

## Integrated browser and broad checks

Authenticated synthetic browser checks at `c2f8ebf8` verified MCP availability
unknown/listed/unavailable/listed, exact policy patterns and payload bounds, with
zero research-tool calls. Memory drafts survived navigation to Skills and back;
proposal before/after and revision history were inspected. Overview filters and
the session plan/agent/activity navigation were exercised. Captured contexts
paged exact text with a stable digest. Original-history search showed a partial
negative for a 1.1 MiB message until two bounded index batches exposed its tail.
The timeline displayed timestamped folds, two response observations, 50% cache-read
share with complete counter coverage, and separately settled fold usage.

Evidence is in the private `completion-campaign-20261018` cache directory:
`browser-acceptance.json`, screenshots `ui-memory.png`,
`ui-context-composition.png`, `ui-context-timeline.png`, `ui-mcp-policy.png`,
`ui-mcp-stats.txt`, and `browser-teardown.json`. The fixture process, private root
and both public fixture ports are gone; all peer/proxy runners closed normally.

Web validation: 2,239 tests in 120 files passed without unhandled errors.
Svelte/TypeScript, Prettier, production and math builds pass. Python's frozen
integrated run reached 14,476 passes and 50 opt-in/prerequisite skips, but was
**not green**: three failures identified missing context-table/API documentation
and workspace access to a control-store implementation detail. Final corrections
and verification are pending below. Two malformed-fixture serializer warnings
and one IPython temporary-location warning were reported.

## Real-model calibration findings in progress

The first calibration reference was refused before dispatch because its worker
environment was absent. Signed helper `1996258c` creates the recorded environment
through the public worker API and reconciles the original operation before any
submission. Its six offline recovery checks and independent review pass. The
original marker was preserved; no model call occurred during the failed setup.
Only helper files were overlaid onto the frozen `ce3c9ff4` runtime; their original
and replacement hashes are recorded in `lab-helper-overlay.json`.

The real Luna review then encountered a truncated HTTP payload. Its failed review
retained the original window (`watermark=0`, `retry_end=3`, `requested=1`), so a
complete evidence snapshot was not mistaken for a completed judgment. The main
agent nevertheless ended while the retry remained live, leaving that review
dropped. Transport retry classification and terminal review waiting are being
repaired before treating calibration as passed. No injected finding or operator
correction is used to manufacture a model-quality result.
