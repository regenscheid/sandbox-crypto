# Planning analyses and research record

Captured: 2026-10-10. This folder preserves working analyses and the three agent source investigations for Sandbox Science. Source inspection and planning are distinct from runtime validation; no application or deployment has been implemented or tested by these reports.

## Current planning snapshot

The [project overview](current/README.md) and [complete document directory](current/docs/) are copied from the planning work at this checkpoint. The maintained project-facing plans remain in the project's `docs/` directory. This is a dated record, not a second authority for decisions or an automatically synchronized mirror.

| Analysis | Preserved copy |
| --- | --- |
| Consistency, shared components and implementation traps | [Consistency review](current/docs/CONSISTENCY-REVIEW.md) |
| Detailed work packages, contracts, ownership and validation gates | [Implementation plan](current/docs/IMPLEMENTATION-PLAN.md) |
| Three-source synthesis and evidence locations | [Source study](current/docs/SOURCE-STUDY.md) |
| Features to incorporate | [Feature plan](current/docs/FEATURE-PLAN.md) |
| Execution topology and feasibility | [Execution plan](current/docs/EXECUTION-PLAN.md), [architecture](current/docs/ARCHITECTURE.md) |
| Projects, sessions, persistence and shared data | [Project/session analysis](current/docs/PROJECTS-AND-SESSIONS.md) |
| Living documents, ledger, frontier, math and PDFs | [Research workspace](current/docs/RESEARCH-WORKSPACE.md) |
| Trusted monitoring, intervention and Conda | [Monitoring](current/docs/MONITORING.md) |
| OpenShell backend suitability and checks | [OpenShell evaluation](current/docs/OPENSHELL-EVALUATION.md) |
| CScience and prior OpenScience fork reuse | [Reuse assessment](current/docs/REUSE-ASSESSMENT.md) |
| External cryptography skill requirements | [External skills](current/docs/EXTERNAL-SKILLS.md) |
| Requirements and milestone history | [Decisions](current/docs/DECISIONS.md), [product](current/docs/PRODUCT.md), [roadmap](current/docs/ROADMAP.md) |

The illustrative JSON records are retained in [current/examples/](current/examples/). They are planning examples, not executable manifests or validated APIs. No external skill catalog is embedded.

## Individual agent investigations

- [Claude Science](agent-reports/claude-science.md): retained research behavior, skills, review triggers, documents, kernels and execution seams.
- [Claude Code and Desktop](agent-reports/claude-code-desktop.md): official skills, roles, hooks, MCP, shared knowledge and living-document behavior, including limits of public prompt evidence.
- [OpenScience Desktop and the earlier fork](agent-reports/openscience-desktop.md): source-level reuse candidates, native-desktop dependencies and incomplete execution/persistence paths.

These are dated investigation reports. Recommendations inside them are not automatically confirmed requirements; the current project decision register resolves later user choices.

## Earlier working drafts

- [Initial planning snapshot](history/01-initial-planning/README.md)
- [Deployment planning snapshot](history/02-deployment-planning/README.md)

Earlier drafts are preserved for provenance and may contain superseded recommendations or assumptions. In particular, use the current decisions for local-container flexibility, base-image session creation, monitor actions and Science-style automatic review.

[INVENTORY.json](INVENTORY.json) records preserved paths, provenance categories, sizes and SHA-256 hashes. It covers the retained planning reports and snapshots and excludes itself.

For future planning passes, save working reports here with dates and evidence references; retain historical snapshots when replacing their conclusions. Keep the maintained project plan and confirmed decisions discoverable from the project root.
