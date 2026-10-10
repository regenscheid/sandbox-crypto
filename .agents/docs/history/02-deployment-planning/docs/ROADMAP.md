# Implementation roadmap

Date: 2026-10-10. No implementation milestone is complete. These gates prioritize CScience compatibility and the requested deployment split.

## M0 — Map retained behavior and execution entry points

Record the pinned CScience baseline and identify shell, Python/R kernel, provisioning, file, remote-compute, parser/renderer, and MCP execution paths. Document the skills and automatic review behavior the user values, including triggers, context, model settings, and revision handling. The earlier OpenScience Desktop fork has been located; finish a focused base diff only where extraction looks useful.

**Exit:** an execution call map, concrete behavior fixtures, and a measured proposal for adapting the existing runtime. No default decision to replace the research harness or GUI.

## M1 — Prove the CScience-to-worker boundary locally

In a separate development checkout, retain the orchestrator/research loop and route an ordinary shell command, stateful Python cells, file handoff, and capability probe to another container. Include R if required by the retained workflows. Observe processes/files to verify research code is outside the orchestrator. Prototype with the smallest worker adapter that can preserve semantics.

**Exit:** container A orchestrates; container B executes and preserves kernel state; files and useful errors return through the same research tools. Capture patch count and upstream coupling. If blocked, explain the exact missing seam before evaluating a compatible replacement harness.

## M2 — Establish OpenShell lifecycle and review reproduction

Validate matching OpenShell release components and the checks in [OpenShell evaluation](OPENSHELL-EVALUATION.md). Add persistent project storage, approved dependency installation, fresh batch/review environments, cancellation, event replay, and restart reconciliation. Add isolated automatic Conda installation and the first trusted monitor tests: normal package traffic, a denied upload to an allowed fixture host, a suspicious fixture response, and monitor failure. Preserve automatic review scheduling and role settings; demonstrate a review that requests its own computation.

**Exit:** interactive work survives normal turns; a fresh reviewer environment reproduces a recorded exact result; failures and kernel resets are explicit; no host-execution fallback.

## M3 — Repeat with Kubernetes workers

Keep orchestration local initially and route the same tools to a Kubernetes-backed pool. Test image availability, architecture, resources, persistent storage, networking, transfer, permissions, and cleanup on the actual cluster.

**Exit:** unchanged research tool calls and skill workflow run locally or on Kubernetes. Stateful cells, project dependencies, artifacts, and automatic review work in both profiles. Record startup/transfer cost and behavior on worker loss. This is an initial gate, not a late expansion.

## M4 — Validate independent service placement and recovery

Package/route the GUI independently. Test hosted orchestration with local workers, all-hosted deployment, and remaining local/remote combinations. Exercise orchestrator restart, closed browsers, interrupted transfer, worker restart, duplicate submissions, and relocation/rollback.

**Exit:** placement changes configuration and deliberate data movement; projects, review findings, and execution history remain coherent. Local workers do not require a publicly exposed container-engine API. No claim of live kernel memory migration.

## M5 — Validate useful cryptography workflows

Run exact algebra/Sage, lattice/fpylll, parameter-estimation, literature-to-report, and fresh reproduction fixtures using existing skills where useful. Capture assumptions, versions, seeds, precision, tool outputs, and review limitations. Assess optional Magma on the actual licensed target.

**Exit:** the system preserves the valued CScience experience and produces reviewable research evidence across both execution placements. Add further domains and UI changes only as actual gaps justify them.

## First end-to-end fixture

1. The orchestrator uses a skill-guided research task to create code in a scoped project workspace.
2. A worker executes a shell command and two Python cells sharing a variable, then runs an exact finite-field check.
3. The worker returns structured output and an artifact manifest.
4. Automatic review receives the claim and evidence; a fresh worker independently reruns the calculation.
5. Repeat against a Kubernetes pool, then test interruption and recovery.

Define `finite-field-identity-v1`: over GF(17), check `a^17 = a` for all 17 elements and `a^16 = 1` for all 16 nonzero elements. Return canonical JSON with field order, counts, and assertion results. Local and cluster runs should agree on exact output while retaining distinct execution evidence. This checks integration and reproduction, not novel cryptographic reasoning.

Keep source layout and stack selection provisional until the adaptation spike establishes which runtime pieces can be retained.
