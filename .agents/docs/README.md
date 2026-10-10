# Planning analyses and research record

This folder keeps the three agent source investigations behind the Sandbox Science plan. Source inspection and planning are distinct from runtime validation; no application or deployment has been implemented or tested by these reports.

## Individual agent investigations

- [Claude Science](agent-reports/claude-science.md): research behavior, skills, review triggers, documents, kernels and execution seams, as evidence for the reimplementation (D03, D11).
- [Claude Code and Desktop](agent-reports/claude-code-desktop.md): official skills, roles, hooks, MCP, shared knowledge and living-document behavior, including limits of public prompt evidence.
- [OpenScience Desktop and the earlier fork](agent-reports/openscience-desktop.md): source-level reuse candidates, native-desktop dependencies and incomplete execution/persistence paths.
- [OpenShell v0.1.3 policy schema](agent-reports/openshell-policy-v0.1.3.md): endpoint, L7, MCP and provider semantics, address handling and compiler constraints, as input to the policy compiler (D62, D68).

These are dated investigation reports. Recommendations inside them are not confirmed requirements; the [decision register](../../docs/DECISIONS.md) resolves later choices, and the [critical review](../critical-review.md) and [proposed changes](../proposed-changes.md) record the 2026-10-10 review.

## Earlier planning states

Earlier plan snapshots are preserved as git tags rather than copied directories:

| Tag | Content |
| --- | --- |
| `plan-03-pre-review` | The plan before the review (`docs/` at that commit). The two earlier working drafts are in that commit's tree under `.agents/docs/history/01-initial-planning` and `.agents/docs/history/02-deployment-planning`. |
| `plan-04-reviewed` | The plan after the review was merged (PR #1). |

Retrieve a file from a snapshot with `git show <tag>:<path>`, for example `git show plan-03-pre-review:docs/ARCHITECTURE.md`.

[INVENTORY.json](INVENTORY.json) records the preserved report paths, sizes and SHA-256 hashes; it excludes itself.
