# Research run error and failure audit

Source review through signed `802504bd`. This is not a new paid SNOVA run. The
isolated acceptance service runs that revision; its research tree remains paused.
The personal instance was not accessed.

| Boundary | Observed behavior and recovery owner | Assessment |
| --- | --- | --- |
| Agent tool call, definite refusal | A worker application or MCP policy refusal becomes a bounded tool error. The agent may fix its arguments or plan on its next turn. | Preserved. |
| Agent tool call, answer lost | A mutating worker request can reach the worker before a transport/5xx failure; an MCP call can reach its server before `no_usable_answer`. Previously both were rendered as ordinary tool errors, settling the journal and permitting a new operation. The shared `UnknownEffectError` now ends the attempt without a tool result, preserving the original intent. Resume checks worker operation identity; unsupported MCP effects require evidence or explicit resolution. | Deployed in the isolated acceptance service; focused and confined-loop tests passed. |
| Background job start | `start_job` submits before recording origin and reading initial status. A later failure formerly appeared retryable even though a job may be running. The post-submit phase now uses the same unknown-effect signal and leaves the original intent pending. | Deployed; regression passed. |
| Provisioning follow-up | The worker may accept a package operation before a later status request fails. The post-submit follow-up now retains the original tool intent when that status cannot be established. | Deployed; regression passed. |
| Delegated run failure | The child saves its partial run and ends failed. Its parent receives a bounded notice with the trusted failure phase and any existing validated outcome errors. No raw exception or fabricated result is handed back. The parent can change the research path; tree Resume remains the orchestrator's responsibility. | Deployed; lifecycle test passed. |
| Final save and child handback | The earlier `eb46cf23` correction separates successful persistence from an execution exit status of 1. Existing recovery records and ACKs are durable and independently reviewed. | Deployed in the isolated acceptance service; exact process-kill windows remain to test. |
| Model gateway HTTP retries | One durable model intent currently wraps a logical `GatewayModel._call`, which can make four HTTP attempts. An earlier timed-out attempt may have charged before a later attempt succeeds. The final response alone cannot settle every charge or child token grant. Increasing to ten attempts without a per-HTTP-attempt durable identity, admission and conservative charge bound would amplify the gap. | Open P2; do not claim historical 524 exposure is zero. |
| Root and tree restart | Resume traverses durable work and checks spending, pending operations, child outcomes, epochs and environment/kernel identity. Unknown effects block their branch; stopped and completed work is not replayed. | Existing implementation; live SNOVA Resume remains paused pending cost reconciliation. |

The follow-up should be one reusable paid-attempt admission component shared by
normal and confined model paths. It must record an identity and budget bound before
each HTTP dispatch, settle each observed response, and retain a reservation for an
unanswered attempt. Then retry policy can use the requested ten attempts with
increasing delay without hiding charges. Add process-kill tests at the final-record,
ACK, child-handback and usage-settlement boundaries before calling the recovery
matrix complete. A real OpenShell interruption test must verify the worker lookup
and original sandbox preservation; local fixtures alone cannot establish that.

Validation: 269 tests passed across worker protocol, tool journal, connectors,
delegation, confined loop, recovery, jobs and provisioning before the malformed
MCP-reply correction; its four focused cases then passed. Ruff, pyright, license
and repository-hygiene checks passed. Independent read-only reviews of worker,
storage and recovery paths found no remaining P1/P2 in the local changes after
the fixes. These tests do not establish a live SNOVA continuation.

The orchestrator image `sandbox-science/orchestrator:802504bd` has digest
`sha256:11154b8d5252c2fe04967389f4644e5f3ee5f2f8c1486a32c611570c669459b5`;
its container and Landlock checks passed. Before rollout the isolated control
database and configuration were backed up with mode 0600. The acceptance service
was restarted with `--keep-sandboxes`, and its health reported ready, protocol 3
and a connected runtime. Its original SNOVA research sandbox remained running;
the work tree was still 11 completed and 4 paused, with zero live run leases and
five unsettled model intents. The separate $310 guard was restarted and active.
No Resume or paid model call was made during this rollout.
