# Planning analyses and research record

This folder keeps the agent source investigations behind the Sandbox Science plan, and records of runtime validation. Source inspection and planning are distinct from runtime validation: only the lab records below report observed behaviour.

## Individual agent investigations

- [Claude Science](agent-reports/claude-science.md): research behavior, skills, review triggers, documents, kernels and execution seams, as evidence for the reimplementation (D03, D11).
- [Claude Code and Desktop](agent-reports/claude-code-desktop.md): official skills, roles, hooks, MCP, shared knowledge and living-document behavior, including limits of public prompt evidence.
- [OpenScience Desktop and the earlier fork](agent-reports/openscience-desktop.md): source-level reuse candidates, native-desktop dependencies and incomplete execution/persistence paths.
- [OpenShell v0.1.3 policy schema](agent-reports/openshell-policy-v0.1.3.md): endpoint, L7, MCP and provider semantics, address handling and compiler constraints, as input to the policy compiler (D62, D68).

## Runtime validation

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

## Earlier planning states

Earlier plan snapshots are preserved as git tags rather than copied directories:

| Tag | Content |
| --- | --- |
| `plan-03-pre-review` | The plan before the review (`docs/` at that commit). The two earlier working drafts are in that commit's tree under `.agents/docs/history/01-initial-planning` and `.agents/docs/history/02-deployment-planning`. |
| `plan-04-reviewed` | The plan after the review was merged (PR #1). |

Retrieve a file from a snapshot with `git show <tag>:<path>`, for example `git show plan-03-pre-review:docs/ARCHITECTURE.md`.

[INVENTORY.json](INVENTORY.json) records the preserved report paths, sizes and SHA-256 hashes; it excludes itself.
