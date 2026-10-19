# SNOVA run: continuity and performance audit

Read-only review of the isolated acceptance project from 2026-10-17 20:55 to
2026-10-18 07:27 UTC. This covers its durable conversations, event and usage
records, reviewer feed, and service logs. It did not access the personal instance,
restart research, change limits, or make a model call. The deployed runtime was
`20ee4ba4`; this report is on a documentation branch from `576a60b9`. The two
code lines have diverged, so fixes must be integrated with current main before
deployment.

The run did real research. All 39 work items eventually answered. It used the
investigation and specialist crypto skills, pinned literature and the official
SNOVA source, built reference checks, used Sage and an unchanged C verifier, and
recorded negative results with scope. Its active scientific plan ended at cursor
107; its final answer did **not** claim a public-only forgery or a competitive
attack. The next path is a public solution to the full actual-target system.

## Measurements

| Observation | Evidence and implication |
| --- | --- |
| Eight tree Resume requests; seven blocked, one finished | `research_resumes`: model budget, persistence error, unknown usage, run budget, local gateway budget, lost kernel, and unfinished children all required external intervention or another Resume. A scoped root answer is not autonomous completion of an active plan. |
| Four model failures | Three HTTP 524 responses and one streamed Responses call with no final answer after 693 seconds. The last child was resumed successfully. The actual charges of unanswered attempts are unknown. |
| Reviewer made 1,461 priced calls, costing $51.2608664 | 107 review runs covered 39 distinct feed windows; 33 windows ran three times and two ran twice. Ninety-seven reviews capped, nine failed, one was inconclusive. None achieved complete coverage or recorded a finding. This is an **unreviewed** state, not a clean review. The same window restarts from its manifest after a cap. |
| Main and delegated calls used large contexts | 1,322 main calls consumed 157.1 million input tokens; 1,578 delegated calls consumed 126.6 million. Median input was about 119,000 and 80,000 tokens respectively. Eighty-five priced fold calls cost $12.1930192; 54 applied fold events were visible. |
| Cache fell after a fold | Native Responses usage reports 66.8% cached input overall for main and 68.2% for delegates. The 54 first main calls after an applied fold had 19.5% cached input, versus 68.6% for other main calls; six first calls of a main attempt had 7.1%. This is an observed correlation, not yet proof that summary placement caused it. Cache detail exists in native envelopes but not the normal usage projection; reviewer and fold cache rates remain unmeasured. |
| Tool friction was frequent | 237 of 1,169 shell calls exited unsuccessfully; many were exploratory or pipeline `SIGPIPE`, and 130 failed with empty stderr. There were 177 structured tool errors, including 80 ledger-change errors, eight shared-write revision refusals, path mistakes, a malformed `read_file` reply, and stale-plan conflicts. The agents often recovered, but each correction required another turn. |
| Stored payloads grew quickly | The SQLite database occupied about 1.24 GB. Assistant native envelopes in `archive_messages` held 317.5 MB; `run_collections.messages` held about 431 MiB, including 3,254 inline native envelopes in `assistant-cache-v1`. Within the native envelopes, the tools field alone accounted for 182.8 MB despite only 16 distinct tool payloads; instructions accounted for 36.8 MB despite only three distinct payloads. Review evidence held 101 MB, about 30 MB by unique SHA-256. Tool-result text was only 14.5 MB. The disk-paged `RunMemory` already prevents simple accumulation of all tool results in RAM; the observed issue is duplicate durable storage and replay I/O. |
| Logging obscured changes | The control log repeated the unknown-cost warning 3,379 times. It also had 27 artifact-capture binding warnings and eight capture-helper failures. Those warnings need outcome correlation before asserting data loss. |

Canonical observed model cost at completion was $305.7562836: main plus folds
$144.9431276, delegated $109.5522896, reviewer $51.2608664. About $88.7663624
of this total was priced from model information instead of a gateway receipt.
Fifteen model intents had no settled usage (four delegated, one main, one fold,
nine reviewer); unknown charges are not zero. The upstream LiteLLM budget is the
primary spend control. The local cost fixes in `20ee4ba4` keep unknown charges
visible without treating them as automatic exhaustion.

## Failure-path interpretation

The transient gateway failures, exhausted delegated grants, and kernel loss did
not destroy the tree: explicit Resume eventually completed it. But recovery was
operator-driven. One-hour OpenShell idle suspension removed the saved Python
kernel while retaining the sandbox files; an explicit `kernel_recompute`
acknowledgement was needed. The acceptance-only idle suspension was disabled.
This runtime had no parent-child mailbox: the archive has initial child tasks and
status reads, but does not exercise the durable messaging feature now on main.
That feature still needs a live interruption-and-resume test with a parent
correction delivered to a running child.
Unknown worker and MCP tool effects now retain their original intent; a new
attempt must reconcile that identity before replaying a mutating call. That
boundary is sound. A model timeout is different: it can have an unknown charge,
but no tool effect has yet been applied; it should not block tree continuation
merely for lack of a price. Explicit cancellation, hard gateway budget refusal,
policy denial, irreconcilable external effects, and physical context limits
remain real stop conditions.

## Implementation order and acceptance tests

1. **Durable research supervisor.** Treat `answered` with an active plan as a
   checkpoint. When all children settle, enqueue one idempotent root continuation
   from the current plan and evidence, within the existing budget. Automatically
   Resume a paused tree after classified transient model/worker failures, using
   bounded increasing backoff and a progress/no-progress check. Persist the
   supervisor decision and original operation IDs. Test a child 524, a no-final
   streamed response, a process restart and a parent waiting on children; each
   should finish without a human monitor or duplicate completed tool effects.
   Stop on explicit user cancellation and budget refusal. A check's 20-million-
   token grant still needs the owner's approval for each 10-million increase.
2. **Make review coverage attainable.** Split or prioritize feed windows by
   evidence/claim and carry a durable cursor across capped attempts. Do not
   replay an identical 400,000-token window three times. Mark uncovered spans
   visibly unreviewed and trigger deeper checks on consequential claims and
   publication. Verify that a synthetic inconsistency is found, and that a
   capped review advances coverage rather than restarting it.
3. **Recover kernel state by provenance.** Pin code, inputs, generation and
   digest for reconstructible kernels, then recompute and compare automatically
   after suspension. Require a user decision only when replay could duplicate an
   external effect or the original state cannot be verified. Test suspension
   during a long Sage job and a genuinely unreconstructible state separately.
4. **Reduce context and persistence cost.** Experiment with a stable prompt
   prefix and summary placement, measuring cache on first and post-fold calls.
   Preserve exact native responses but compress or content-address repeated
   schemas and instructions; avoid storing each native envelope again in the
   host shadow message collection. Keep historical reads lazy and bounded.
   Benchmark archive size, replay time and cache rate without changing output.
5. **Remove predictable tool retries.** Advertise available sandbox executables,
   provide a preflight for paths and write revisions, and return a structured
   diagnostic for nonzero shell exit with redirected stderr. Refresh plan state
   proactively on version changes. Keep ledger provenance and role checks, but
   expose valid request examples so agents can correct a refusal once. Test the
   malformed `read_file` range and artifact-capture identity paths directly.
6. **Expose useful telemetry.** Project native cached-token detail and model
   attempt uncertainty into the context screen, with fold markers and role
   breakdowns. Coalesce repeated identical warnings while preserving the first
   and last occurrence counts. Distinguish gateway-reported, model-info-priced,
   reserved and unknown charges in every cost view.

This audit does not establish that a repeated read, failed research experiment,
or review call was unnecessary merely because it recurred. The strongest
avoidable costs are the review-window replay and copied storage payloads; the
post-fold cache drop is a measured optimization target requiring an A/B test.

Evidence: a read-only SQLite backup of the acceptance control database,
`snova-run-audit-20261018.sqlite3` (SHA-256
`0e4ca722e32377cbf7ece1563bcfa9acdefa17ca5f582694d53e14547ccb1dc7`),
plus frozen copies of `agent-runtime.log`
(`9e6e7b7ad829fb069431bff37cc7b2eac2375994d65d85d12ed1f313d594644a`),
`control-service.log`
(`863e58d69ff594417f2b57e6a6d5bac338deb71f0d7a3474864c29293c563f18`),
and `worker.log`
(`56600f25251a7cb010534a042a184b36f4811a23e2a2d2b6ce3841cad52292d4`).
The mode-0600 copies are under the isolated lab's `audit/` directory, outside git.
