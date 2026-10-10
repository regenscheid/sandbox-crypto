# Implementation roadmap

Date: 2026-10-10. No implementation milestone is complete. The three source investigations are complete as a planning input; runtime adaptation remains untested. These gates combine CScience compatibility, the requested deployment split, and first-class math/source/document workflows.

The [detailed implementation plan](IMPLEMENTATION-PLAN.md) defines shared contracts, component ownership and work packages beneath these milestones. The [consistency review](CONSISTENCY-REVIEW.md) records resolved ambiguities and reusable components. Begin with W0/W1: execution tracing and a small retained-tool shell/Python worker slice.

## M0 — Map retained behavior and execution entry points

Use the [source study](SOURCE-STUDY.md) and [feature plan](FEATURE-PLAN.md) as the baseline. Trace shell, Python/R kernels, provisioning, files, remote compute, parser/renderer, and MCP execution paths. Add native Sage and exact PDF/source anchors to the compatibility contract. Record actual review configuration separately from inspected defaults. Finish a focused OpenScience base diff only where extraction looks useful.

Define the [external skill integration contract](EXTERNAL-SKILLS.md): discovery/body loading, raw structured outcomes, plans/approvals, lineage, project identity, board/knowledge storage and binary transfer. The pack stays external and can update its host bindings; reproduce useful semantics rather than require exact legacy APIs.

**Exit:** an execution call map, concrete behavior fixtures, and a measured proposal for adapting the existing runtime. No default decision to replace the research harness or GUI.

## M1 — Prove the worker boundary on local and hosted targets

In a separate development checkout, retain the orchestrator/research loop. First route an ordinary shell command and two stateful Python cells to another container, publish a file, interrupt work and reconnect. Then add native Sage cells, PDF parsing/rendering and capability probes. Include R if required by retained workflows. Check Sage preparsing and exact finite-field/matrix output; preserve PDF page anchors and inspect mathematical transcription against a render. Observe processes/files to verify research code is outside the orchestrator.

Start on one available target, then run the same boundary fixture on a hosted Kubernetes pool and a local container pool. Evaluate local Docker/Podman or another container runtime alongside local Kubernetes on macOS/Docker Desktop or Babbage Linux. Select a working local backend by measured execution/network policy enforcement, monitoring and persistence. Both deployment classes are initial gates; local Kubernetes is optional.

**Exit:** container A orchestrates; container B executes and preserves Python/Sage kernel state; files, PDF page evidence, LaTeX results and useful errors return through the same research tools on local and hosted targets. Record which macOS/Linux environments were actually exercised. Capture patch count and upstream coupling. If blocked, explain the exact missing seam before evaluating a compatible replacement harness.

## M2 — Establish persistent projects and the complete local research workflow

Validate matching OpenShell components and the [backend checks](OPENSHELL-EVALUATION.md). Add persistent project/tool/data state, separate workbook kernels, and optional dedicated session containers created from the base image. Verify that project-installed tools are not implicitly inherited while shared project data remains accessible. Add isolated Conda installation, bounded batch environments, cancellation, event replay and restart reconciliation. Fresh verification environments are a proposed extension using the same lifecycle component. Test normal package traffic, policy-selected blocking/permission escalation/alert-and-continue, suspicious fixture response and monitor failure.

Preserve current Science-style automatic consistency review scheduling and finding/confirmation behavior; introduce the separate reproduction capability as an extension. Connect the project ledger/board, PDF source anchors, LaTeX living documents, attributed user edits/comments and revision-checked agent patches. Test a stale patch and an assumption change that marks dependent work for reconsideration.

Exercise idle preservation and restoration: install a tool, alter a configuration/file, record source/data hashes, suspend or snapshot the environment, resume/recreate it and verify all recorded state. Test shared writes from two sessions, conflict reporting and GUI closure during an active computation.

**Exit:** two workbooks share the project container with distinct kernels; a third uses its own persistent container and accesses shared project knowledge/data. The complete paper → Sage → document → user feedback → automatic consistency review path works locally. Concurrency, review confirmation, failures and kernel resets are explicit; no host-execution fallback. Evaluate fresh reproduction separately if enabled.

## M3 — Validate the complete workflow across local and hosted deployments

Repeat the complete M2 workflow on the local and hosted targets already established in M1. Test image availability/architecture, resources, persistent storage, idle restore, same-project sharing across session containers, networking, transfer, permissions and cleanup. Use the same platform contract; vary target configuration deliberately.

**Exit:** unchanged research tools and the complete M2 workflow run on both local and hosted deployments, including Sage, PDFs, project sharing, documents, automatic consistency review and idle recovery. Any enabled reproduction extension uses the same backends. Record startup/transfer cost, environment coverage and worker-loss behavior. This is an initial gate, not a late expansion.

## M4 — Validate independent service placement and recovery

Package/route the GUI independently. Test hosted orchestration with local workers, all-hosted deployment, and remaining local/remote combinations. Exercise orchestrator restart, closed browsers, interrupted transfer, worker restart, duplicate submissions, and relocation/rollback.

**Exit:** placement changes configuration and deliberate data movement; projects, review findings, and execution history remain coherent. Local workers do not require a publicly exposed container-engine API. No claim of live kernel memory migration.

## M5 — Validate useful cryptography workflows

Broaden the already working Sage/PDF/document path to representative cryptography research: lattice/fpylll, parameter estimation, implementation validation, counterexample search and literature-to-report work. Evaluate curated skills against a baseline. Capture assumptions, domains, versions, seeds, precision, output and review limitations. Assess optional Magma on the actual licensed target.

**Exit:** the system preserves the valued CScience experience and produces reviewable research evidence across both execution placements. Add further domains and UI changes only as actual gaps justify them.

## First end-to-end fixture

1. Import a small mathematical PDF, inspect an equation and create a page/region-anchored ledger claim.
2. A skill-guided workbook executes a shell command, two stateful Python cells, and native Sage finite-field/matrix checks in its project worker.
3. Publish structured outputs and provenance; inspect them from a workbook with a dedicated container.
4. Draft a LaTeX living document linking exact sources/runs. Accept user edits/comments and reconcile a concurrent agent patch.
5. Run automatic consistency review and retain findings, author response and confirmation. If the proposed reproduction extension is enabled, separately rerun the exact computation in a fresh worker through the common execution API.
6. Update the project board. Repeat against Kubernetes workers, then test interruption and recovery.

Define `finite-field-identity-v1`: over GF(17), check `a^17 = a` for all 17 elements and `a^16 = 1` for all 16 nonzero elements. Return canonical JSON with field order, counts, and assertion results. Local and cluster runs should agree on exact output while retaining distinct execution evidence. This checks integration and reproduction, not novel cryptographic reasoning.

Keep source layout and stack selection provisional until the adaptation spike establishes which runtime pieces can be retained.
