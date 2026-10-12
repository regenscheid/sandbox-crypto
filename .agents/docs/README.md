# Planning analyses and research record

This folder keeps the agent source investigations behind the Sandbox Science plan, and records of runtime validation. Source inspection and planning are distinct from runtime validation: only the lab records below report observed behaviour.

## Individual agent investigations

- [Claude Science](agent-reports/claude-science.md): research behavior, skills, review triggers, documents, kernels and execution seams, as evidence for the reimplementation (D03, D11).
- [Claude Code and Desktop](agent-reports/claude-code-desktop.md): official skills, roles, hooks, MCP, shared knowledge and living-document behavior, including limits of public prompt evidence.
- [OpenScience Desktop and the earlier fork](agent-reports/openscience-desktop.md): source-level reuse candidates, native-desktop dependencies and incomplete execution/persistence paths.
- [OpenShell v0.1.3 policy schema](agent-reports/openshell-policy-v0.1.3.md): endpoint, L7, MCP and provider semantics, address handling and compiler constraints, as input to the policy compiler (D62, D68).
- [Document writer guard investigation, 2026-10-12](agent-reports/document-writer-guard-2026-10-12.md): source-only analysis of notification, lease and protected-filesystem boundaries after Lab20's unreadable peer-process evidence. A protected-filesystem feasibility prerequisite is proposed; FUSE is unimplemented and unvalidated under OpenShell, and LD-49 remains open.

## Runtime validation

- [OpenShell v0.1.3 lab 23 on babbage, 2026-10-12](agent-reports/babbage-openshell-lab-23-2026-10-12.md): unchanged external backend conformance on frozen main `73c07aa`, with 23 cases passed per linux/amd64 minimal/crypto-base profile and effective/declared tier 3. [Evidence](agent-reports/babbage-openshell-lab-23-evidence-2026-10-12.json) retains 20 exact text records. Python kernels were tested; advertised Sage, other architectures/backends and capture fault semantics were not newly validated. The complete lab and both owned fixture roots were removed.

- [OpenShell v0.1.3 lab 22 on babbage, 2026-10-12](agent-reports/babbage-openshell-lab-22-2026-10-12.md): bounded explicit artifact capture on signed `b535856`, with exact binary/empty retention, prompt regular/sparse/symlink/FIFO refusal and preserved healthy publication/read/execution access. [Evidence](agent-reports/babbage-openshell-lab-22-evidence-2026-10-12.json) retains API, independent guest/SQLite and verified teardown records. Lost-bridge, blocked host work and corrupt state remain local hostile tests; the disposable lab was completely removed.

- [OpenShell v0.1.3 lab 21 on babbage, 2026-10-12](agent-reports/babbage-openshell-lab-21-2026-10-12.md): sticky recovery refusal after cancellation and graceful worker stop during observed package linking, preserved running kernel and healthy peer, file/lockfile recovery access, and confirmed original-VM deletion/reuse. [Cancellation evidence](agent-reports/babbage-openshell-lab-21-cancel-evidence-2026-10-12.json) and [final offline evidence](agent-reports/babbage-openshell-lab-21-offline-evidence-2026-10-12.json) name their exact signed source commits. Both disposable labs were completely removed.
- [Pinned artifact review browser acceptance, 2026-10-12](agent-reports/artifact-review-2026-10-12.md): exact original and corrected source, audited empty text, explicit binary/non-UTF-8 incompleteness, delayed selection, session scope, forged-reference refusal and attributed confirmation. [Evidence](agent-reports/artifact-review-browser-evidence-2026-10-12.json), [confirmed fix](agent-reports/artifact-review-confirmed-2026-10-12.png). All disposable browser state was removed.
- [Document math findings browser acceptance, 2026-10-12](agent-reports/document-math-2026-10-12.md): actual local KaTeX, exact author provenance, USER exclusion, accepted/removed confirmation, unavailable checks, two reflags, delayed selection, session scope and retained concurrent USER decisions. [Evidence](agent-reports/document-math-browser-evidence-2026-10-12.json), [marked repair](agent-reports/document-math-display-2026-10-12.jpg), [retained draft](agent-reports/document-math-stale-draft-2026-10-12.jpg). All disposable browser state was removed.
- [Consistency review browser acceptance, 2026-10-12](agent-reports/consistency-review-2026-10-12.md): immutable evidence, unavailable reviewer, delayed selection, retained claims/conflicts, attributed confirmation and exact preservation of pending author work. The initial coverage issue and corrected result are retained. [Final evidence](agent-reports/consistency-review-evidence-2026-10-12.json), [confirmed fix](agent-reports/consistency-review-confirmed-2026-10-12.jpg), [attribution history](agent-reports/consistency-review-history-2026-10-12.jpg). All browser fixture state was removed.

- [OpenShell v0.1.3 lab 20 on babbage, 2026-10-12](agent-reports/babbage-openshell-lab-20-2026-10-12.md): same-attempt worker restart, offline completion/timeout, kernel continuity and bounded replay; the initial daemon-loss cleanup failure and corrected same-VM stop/start fence, preserved disk, lost old process/kernel identity, independent sandbox deletion and idle endpoints. [Final evidence](agent-reports/babbage-openshell-lab-20-final-evidence-2026-10-12.json); initial evidence and failure remain linked in the report. All Lab20 state was removed.

- [Document comments browser acceptance, 2026-10-12](agent-reports/document-comments-2026-10-12.md): durable main-agent requests, stale formula anchors, attributed revisions/replies, resolve/reopen, concurrent drafts and paged history. [Evidence](agent-reports/document-comments-evidence-2026-10-12.json), [queued request](agent-reports/document-comments-queued-2026-10-12.jpg), [responded thread](agent-reports/document-comments-response-2026-10-12.jpg), [retained draft](agent-reports/document-comments-draft-2026-10-12.jpg).

- [Document editing browser acceptance, 2026-10-12](agent-reports/document-editing-2026-10-12.md): retained source/block drafts, concurrent conflicts, reviewed patches, stable-ID reverts and workbook promotion. [Evidence](agent-reports/document-editing-evidence-2026-10-12.json), [conflict view](agent-reports/document-editing-conflict-2026-10-12.jpg), [accepted suggestion](agent-reports/document-editing-accepted-patch-2026-10-12.jpg).

- [OpenShell v0.1.3 lab 19 on babbage, 2026-10-12](agent-reports/babbage-openshell-lab-19-2026-10-12.md): background jobs, policy quotas, full logs, event reconnection, cancellation and lifecycle cleanup; real browser waits, watcher notes and cancellation; durable completion notices after a harness process restart. [Recorded evidence](agent-reports/babbage-openshell-lab-19-evidence-2026-10-12.json), [GUI view](agent-reports/babbage-openshell-lab-19-gui-2026-10-12.jpg).

- [OpenShell v0.1.3 lab 18 on babbage, 2026-10-12](agent-reports/babbage-openshell-lab-18-2026-10-12.md): the provisioning tool with real dry runs and pinned installs, exact file-set grants and withdrawal, independent environments, all nine catalog probes, and cancellation and worker restart after linking began. [Recorded evidence](agent-reports/babbage-openshell-lab-18-evidence-2026-10-12.json).

- [OpenShell v0.1.3 lab on babbage, 2026-10-10](agent-reports/babbage-openshell-lab-2026-10-10.md): the policy middleware against a real gateway with the VM driver, and what the lab changed (D69, D70).
- [OpenShell v0.1.3 lab 2 on babbage, 2026-10-10](agent-reports/babbage-openshell-lab-2-2026-10-10.md): the package cache with real clients in a sandbox, provider credentials, and the MCP proxy's compiled policy (D57, D69, D71).
- [OpenShell v0.1.3 lab 3 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-3-2026-10-11.md): the compiled credential bindings (D57, amended), and plain-HTTP credential substitution seen at runtime.
- [OpenShell v0.1.3 lab 4 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-4-2026-10-11.md): the W1 first slice in an OpenShell VM sandbox, and the seccomp restrictions it had to work around.
- [OpenShell v0.1.3 lab 12 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-12-2026-10-11.md): the harness from the `orchestrator` image in its own sandbox, using project records to have the worker create a default and a dedicated environment; OpenShell's 19-character limit on sandbox names.
- [OpenShell v0.1.3 lab 13 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-13-2026-10-11.md): the MCP proxy in its own sandbox, reached from the host and an orchestrator sandbox; credential substitution, the version header and server requests at the fence; synthetic addresses for names, which moved the address checks into the compiled policy.
- [OpenShell v0.1.3 lab 14 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-14-2026-10-11.md): the worker with clients limited to projects against a real gateway, and a host the harness admitted ahead of use reachable on the first connection.
- [OpenShell v0.1.3 lab 15 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-15-2026-10-11.md): the `mcp-proxy` image with its launcher as the sandbox's command: placeholders in the command's environment, the proxy back after a stop and start, and the forward to start again.
- [OpenShell v0.1.3 lab 16 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-16-2026-10-11.md): shared data across a project's default and dedicated environments (D79), before and after the review: a conflict copy and its notice, file requests checking the project's version across VMs, and the sync's cost per call.
- [OpenShell v0.1.3 lab 17 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-17-2026-10-11.md): the shared-data sync as merged, in VMs (its atomic swap and the check for files in use); package commands taking turns with the real pip and micromamba through the cache, run on each round of PR #54's review; the cache's budgets per sandbox and the middleware's header; and the MCP proxy's forward kept up by a unit across a stop and start.
- [OpenShell v0.1.3 lab 11 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-11-2026-10-11.md): idle suspension (W3): what a stopped sandbox keeps, how long resuming takes, and a busy kernel keeping its environment up.
- [OpenShell v0.1.3 lab 10 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-10-2026-10-11.md): admission on demand (D69): why the host-less endpoint had to go, how long admission takes, and how an admission interrupts transfers.
- [OpenShell v0.1.3 lab 9 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-9-2026-10-11.md): the M1 exit run, with a real paper, Sage, the crypto-base image and the harness in its own sandbox.
- [OpenShell v0.1.3 lab 8 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-8-2026-10-11.md): the W1 hardening and the package clients in a real sandbox, the conformance suite passing there, and why the executor stays dumpable under OpenShell.
- [OpenShell v0.1.3 lab 7 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-7-2026-10-11.md): the harness in its own sandbox under the orchestrator policy, driving tools in a sandbox the worker created (the W1 boundary end to end).
- [OpenShell v0.1.3 lab 6 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-6-2026-10-11.md): the worker creating, revising and deleting a research sandbox under stored policy revisions (D73).
- [OpenShell v0.1.3 lab 5 on babbage, 2026-10-11](agent-reports/babbage-openshell-lab-5-2026-10-11.md): a real SageMath from conda-forge through the worker and the harness, its performance against the host, and the device paths and `PATH` it needed.

These are dated investigation reports. Recommendations inside them are not confirmed requirements; the [decision register](../../docs/DECISIONS.md) resolves later choices, and the [critical review](../critical-review.md) and [proposed changes](../proposed-changes.md) record the 2026-10-10 review.

## Implementation notes

- [Document mirror and helper interface plan, 2026-10-12](agent-reports/document-mirror-plan-2026-10-12.md): proposed publication, direct-edit and helper boundaries for LD-49 to LD-52, with ownership and adversarial checks. The catalog prerequisite is implemented; coordinated control outbox/source adapter/helper work and the protected-filesystem writer-guard prerequisite remain open. The note describes interfaces, not an integrated runtime mirror.

## Earlier planning states

Earlier plan snapshots are preserved as git tags rather than copied directories:

| Tag | Content |
| --- | --- |
| `plan-03-pre-review` | The plan before the review (`docs/` at that commit). The two earlier working drafts are in that commit's tree under `.agents/docs/history/01-initial-planning` and `.agents/docs/history/02-deployment-planning`. |
| `plan-04-reviewed` | The plan after the review was merged (PR #1). |

Retrieve a file from a snapshot with `git show <tag>:<path>`, for example `git show plan-03-pre-review:docs/ARCHITECTURE.md`.

[INVENTORY.json](INVENTORY.json) records the preserved report paths, sizes and SHA-256 hashes; it excludes itself.
