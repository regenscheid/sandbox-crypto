# Implementation roadmap

Date: 2026-10-10. No implementation milestone is complete. The three source investigations are complete as a planning input; nothing has been implemented. These gates combine reimplemented CScience behavior (D03, D11), the requested deployment split, and first-class math/source/document workflows.

The [detailed implementation plan](IMPLEMENTATION-PLAN.md) defines shared contracts, component ownership and work packages beneath these milestones. The [consistency review](CONSISTENCY-REVIEW.md) records resolved ambiguities and reusable components. Begin with W0/W1: behavior specifications and a minimal new-harness shell/Python worker slice, with infrastructure track P in parallel.

## M0 — Specify behavior and choose harness components

Use the [source study](SOURCE-STUDY.md) and [feature plan](FEATURE-PLAN.md) as evidence of behavior. Write behavior specifications with acceptance fixtures for the features to reimplement (D51), without copying Claude Science code, prompts or schemas (D56). Add native Sage and exact PDF/source anchors to the specs. Choose the harness building blocks under the licensing policy (D55). Assess OpenScience Desktop (MIT) reuse where it looks useful.

Define the [external skill integration contract](EXTERNAL-SKILLS.md): discovery/body loading, raw structured outcomes, plans/approvals, lineage, project identity, board/knowledge storage and binary transfer. The pack stays external and can update its host bindings; reproduce useful semantics rather than require exact legacy APIs.

Confirm the [threat model](THREAT-MODEL.md) and [licensing policy](LICENSING.md). Start the infrastructure track (implementation plan, track P) in parallel.

**Exit:** behavior specs with fixtures, and a harness-component decision with licenses recorded.

## M1 — Prove the worker boundary on babbage

Run the minimal new harness in one container. First route an ordinary shell command and two stateful Python cells to another container, publish a file, interrupt work and reconnect. Then add native Sage cells, PDF parsing/rendering and capability probes. Include R if the behavior specs require it. Check Sage preparsing and exact finite-field/matrix output; preserve PDF page anchors and inspect mathematical transcription against a render. Observe processes/files to verify research code is outside the orchestrator.

Run on babbage (phase 1) with the OpenShell MicroVM driver; compare a container driver only if the MicroVM path is blocked. Select the backend by measured execution/network policy enforcement, monitoring and persistence. Hosted Kubernetes certification is a phase 2 gate (M3) and does not block phase 1. The worker API stays backend-neutral, so no phase 1 code assumes a single host.

**Exit:** container A orchestrates; container B executes at a recorded isolation tier with mirror-only egress, and preserves Python/Sage kernel state; files, PDF page evidence, LaTeX results and useful errors return through the same research tools on babbage. Record the exact host, kernel, driver and isolation tier exercised.

## M2 — Establish persistent projects and the complete local research workflow

Validate matching OpenShell components and the [backend checks](OPENSHELL-EVALUATION.md). Add persistent project/tool/data state, separate workbook kernels, and optional dedicated session containers created from the base image. Verify that project-installed tools are not implicitly inherited while shared project data remains accessible. Add isolated Conda installation, bounded batch environments, cancellation, event replay and restart reconciliation. Fresh verification environments are a proposed extension using the same lifecycle component. Test normal package traffic, policy-selected blocking/permission escalation/alert-and-continue, suspicious fixture response and monitor failure.

Implement Science-style automatic consistency review scheduling and finding/confirmation behavior to its spec; introduce the separate reproduction capability as an extension. Connect the project ledger/board, PDF source anchors, LaTeX living documents, attributed user edits/comments and revision-checked agent patches. Test a stale patch and an assumption change that marks dependent work for reconsideration.

Exercise idle preservation and restoration: install a tool, alter a configuration/file, record source/data hashes, suspend or snapshot the environment, resume/recreate it and verify all recorded state. Test shared writes from two sessions, conflict reporting and GUI closure during an active computation.

**Exit:** two workbooks share the project container with distinct kernels; a third uses its own persistent container and accesses shared project knowledge/data. The complete paper → Sage → document → user feedback → automatic consistency review path works locally. Concurrency, review confirmation, failures and kernel resets are explicit; no host-execution fallback. Evaluate fresh reproduction separately if enabled.

## M3 — Validate the complete workflow across local and hosted deployments

Repeat the complete M2 workflow on hosted Kubernetes (the phase 2 test cluster) through the same platform contract as the local target certified in M1–M2. Test image availability/architecture, resources, persistent storage, idle restore, same-project sharing across session containers, networking, transfer, permissions and cleanup. Use the same platform contract; vary target configuration deliberately.

**Exit:** unchanged research tools and the complete M2 workflow run on both local and hosted deployments, including Sage, PDFs, project sharing, documents, automatic consistency review and idle recovery. Any enabled reproduction extension uses the same backends. Record startup/transfer cost, environment coverage and worker-loss behavior. This is an initial gate, not a late expansion.

## M4 — Validate independent service placement and recovery

Package/route the GUI independently. Certify in stages (D53): all-local (phase 1), then all-hosted, then hosted orchestration with local workers under a local policy ceiling (D49); exercise the remaining local/remote combinations as configuration variations. Exercise orchestrator restart, closed browsers, interrupted transfer, worker restart, duplicate submissions, and relocation/rollback.

**Exit:** placement changes configuration and deliberate data movement; projects, review findings, and execution history remain coherent. Local workers do not require a publicly exposed container-engine API. No claim of live kernel memory migration.

## M5 — Validate useful cryptography workflows

Broaden the already working Sage/PDF/document path to representative cryptography research: lattice/fpylll, parameter estimation, implementation validation, counterexample search and literature-to-report work. Evaluate curated skills against a baseline. Capture assumptions, domains, versions, seeds, precision, output and review limitations. Assess optional Magma on the actual licensed target.

**Exit:** the system meets the CScience behavior specs and produces reviewable research evidence across both execution placements. Add further domains and UI changes only as actual gaps justify them.

## Delivery phases (D53, D63, D64)

The user needs the whole initial feature set before the system is useful, so phases stage **placement**, not features.

| Phase | Target | Milestones | Usable when |
| --- | --- | --- | --- |
| 1 | babbage (amd64, Ubuntu 26.04), all-local, tier 3 via OpenShell MicroVM (KVM) | M0–M2, **local gates only** | The complete research workspace works on babbage: chat and workbooks, Python and Sage kernels, Conda provisioning, PDF navigation, Markdown+LaTeX living documents, ledger/board/frontier, automatic review, external skill pack, and crypto-research-mcp, under the permissive policy file |
| 2 | Kubernetes test cluster (Raspberry Pi 5 arm64 + amd64 nodes), small scale | M3 (hosted certification of M1–M2 fixtures), M4 (partial) | The same workflow runs with workers on the cluster; multi-arch images; heavy math pinned to capable nodes |
| 3 | Mac (M3 Max), further MCP servers, remote access/exposure decisions, remaining placements | M4, M5 | Placement and access choices made from phase 1–2 experience |

## First end-to-end fixture

1. Import a small mathematical PDF, inspect an equation and create a page/region-anchored ledger claim.
2. A skill-guided workbook executes a shell command, two stateful Python cells, and native Sage finite-field/matrix checks in its project worker.
3. Publish structured outputs and provenance; inspect them from a workbook with a dedicated container.
4. Draft a LaTeX living document linking exact sources/runs. Accept user edits/comments and reconcile a concurrent agent patch.
5. Run automatic consistency review and retain findings, author response and confirmation. If the proposed reproduction extension is enabled, separately rerun the exact computation in a fresh worker through the common execution API.
6. Update the project board, then test interruption and recovery. In phase 2, repeat against Kubernetes workers.

Define `finite-field-identity-v1`: over GF(17), check `a^17 = a` for all 17 elements and `a^16 = 1` for all 16 nonzero elements. Return canonical JSON with field order, counts, and assertion results. Local and cluster runs should agree on exact output while retaining distinct execution evidence. This checks integration and reproduction, not novel cryptographic reasoning.

Keep source layout provisional until W0 settles the harness components.
