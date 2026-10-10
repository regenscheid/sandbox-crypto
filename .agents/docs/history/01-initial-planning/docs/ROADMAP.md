# Implementation roadmap

Date: 2026-10-10. Milestones are dependency gates, not calendar promises. No implementation milestone is complete yet.

## M0 — Establish sources and requirements

Locate the earlier OpenScience Desktop effort and record its upstream/base and user changes. Confirm the first exact-algebra and lattice evaluation tasks, initial host/pool, model endpoint, and data-placement constraints. Resolve the OpenShell v0.1.3 deployment documentation discrepancy. Decide which source components merit a small extraction trial.

**Exit:** source inventory and a small set of measurable fixtures; uncertain items explicitly tracked. The missing earlier project blocks a confident salvage estimate, not the independent execution spike.

## M1 — Prove the execution boundary

Implement a disposable deterministic runner with the matching OpenShell SDK/CLI. Exercise the feasibility table in [OpenShell evaluation](OPENSHELL-EVALUATION.md), including cleanup, cancellation, resource bounds, isolation failure, and a remote worker target. Record raw evidence and measured startup/transfer overhead.

**Exit:** one pinned batch calculation and artifact manifest reproduced on two execution hosts; denied access is actually denied; interruption/cleanup status is truthful. Choose OpenShell or document why another backend needs evaluation.

## M2 — Build the portable core

Create the API/orchestrator, durable metadata/events, artifact interface, and outbound worker controller with one environment profile. Add idempotent submissions, leases/fencing, restart reconciliation, log limits, and server-side provenance. Initially use a CLI and a stub model response. Keep Docker/cluster privileges on the execution infrastructure.

**Exit:** orchestrator restart, controller restart, browser absence, failed transfer, duplicate submission, and cancellation do not corrupt run history or silently replay effects. An unavailable backend never triggers a host shell fallback.

## M3 — Add the research workbench and harness

Choose the harness integration after demonstrating confined tools and persisted session recovery. Build or extract the project/conversation view, execution inspector, worker/environment selector, and artifact viewer. Connect one real model gateway and a small curated research-tool set. Add a reviewer with a fresh evidence-only sandbox.

**Exit:** a user can ask for a small math experiment, inspect the proposed code/environment, run it, see live logs and artifact lineage, and reproduce it after closing the UI. Harness local tools and plugin/MCP subprocesses have no execution path around the approved boundary.

## M4 — Validate independent placement

Package UI, control-plane/harness, and worker controller separately. Exercise all-local, local-control/remote-compute, remote-control/local-compute, remote-UI/local-control, and all-remote deployments, then the remaining combinations. Use the same domain API and scientific fixture in each profile.

**Exit:** switching placement changes endpoints, allowed transfers, and storage configuration rather than research code. NAT/private-network cases use authenticated worker connections or explicit tunnels. Drain/redeploy/restore retains projects and run history; rollback has been exercised. No live process migration claim.

## M5 — Make the cryptography/math workbench useful

Publish independently locked Python/crypto, lattice, Sage, and document profiles with functional probes. Add exact-result checks, precision/seed capture, estimator revision tracking, source-backed reports, and independent reproductions. Connect existing crypto/NIST/ePrint research services where available. Evaluate optional Magma on its real licensed target.

**Exit:** at least one finite-field/algebra workflow, one lattice workflow, and one source-to-report workflow produce reviewable experiment bundles. Missing capability/policy/license states are explicit and actionable. Scientific correctness is checked separately from successful process exit.

## M6 — Expand after evidence

Add persistent notebooks/checkpoints, GPUs, cluster scheduling, proof tools, more science profiles, teams, and optional desktop packaging as justified by actual workflows. Any public multi-tenant deployment needs a separate identity, isolation, resource-accounting, and operations review.

## First implementation slice

Build `submit → lease → stage → create sandbox → execute → collect → record → reproduce` for a single scripted finite-field identity. Use a prebuilt image and a private fixture dataset. Defer provider choice, broad skill catalogs, autonomous delegation, notebook memory, and visual polish until this path has real evidence.

Define `finite-field-identity-v1` concretely: construct GF(17), check `a^17 = a` for all 17 elements and `a^16 = 1` for all 16 nonzero elements, and write JSON containing the field order, counts checked, and boolean assertion results. Both hosts must produce the same canonical result JSON; logs, timing, hardware, and attempt IDs remain distinct. This is an exact integration fixture, not an evaluation of novel mathematical reasoning.

Proposed future source layout (directories are not yet created):

```text
apps/web/                 browser UI and same-origin API proxy configuration
services/control/         API, scheduler, persistence, tool gateway
services/harness/         replaceable model loop; may initially share control image
services/worker/          outbound controller and OpenShell adapter
contracts/                versioned API, run/event/profile schemas
environments/             image definitions, locks, functional probes
skills/crypto-math/       curated research workflows
deploy/                   tested local and remote manifests
evaluations/              deterministic scientific and isolation fixtures
```

