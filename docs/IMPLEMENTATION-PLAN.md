# Detailed implementation plan

Updated: 2026-10-12. Engineering sequence beneath the milestone [roadmap](ROADMAP.md). Track P has the policy library, OpenShell compiler, policy middleware, package cache and event store. W1/W2 provide the [worker service](WORKER.md), harness and native math execution. M1 passed its babbage exit run; provisioning, background jobs and executor recovery have real OpenShell acceptance. W4 has the [workspace](WORKSPACE.md) GUI with document editing and comments checked in the browser. The integrated M2 research workflow remains open. Confirmed product requirements stay in [decisions](DECISIONS.md); this plan incorporates the [consistency review](CONSISTENCY-REVIEW.md).

## First implementation slice

Run a minimal new harness (D11, D55) in container A. Its shell and Python tools execute only through the worker API in container B: one ordinary shell command and two stateful Python cells. Return streamed output, a published file and the observed execution location through those tools. Interrupt an operation and reconnect without launching it twice. This slice tests the central architectural assumption before adding the complete product workflow.

Its deliverables are the harness tool loop, the worker API and one adapter, a small integration fixture, and a record of processes/files/policy actually observed. A command-line or minimal web client is enough for this slice; the full GUI comes later. The harness never has a local execution path to fall back on, because it is built without one.

## Ownership and packaging

| Deployable unit | Initial logical modules | Authority and durable state |
| --- | --- | --- |
| Web GUI/API proxy | New research views (small at first), shared math renderer, PDF viewer, document/comment and permission interfaces | Authenticates to control APIs; caches/projections are disposable. Independent endpoint configuration supports separate hosting. |
| Control application/orchestrator | New harness (D11), project/session catalog, artifact metadata, ledger/documents, review coordination, permission decisions, execution router | Owns research history, scoped grants, revisions and execution intent. Initially one embedded store (D12) on private durable storage. Semi-trusted (D47): egress limited to the model gateway and connector broker; no engine/cluster credentials. |
| Trusted worker/policy infrastructure per pool | Worker service, environment/kernel managers, backend adapter, OpenShell gateway/supervisor if adopted, monitor integration | Owns runtime credentials, observed execution state, leases and policy enforcement. Keep this authority outside research-writable environments. |
| Project/session/batch research containers | Scientific tools, shell, persistent kernels, approved Conda operations, parsers and renderers | Own bounded mutable execution state and granted data access. No cluster administration, enrollment or broad provider credentials. |

Artifact bytes may initially live on private durable filesystem storage with authenticated transfer. Their repository interface should permit another backend later. Do not require remote workers to mount the orchestrator's database or filesystem. The diagram's modules need clear interfaces, not separate deployments for every feature.

## Shared contracts to establish first

Names below describe proposed interfaces, not already implemented public APIs. Settle names and serialization in W0/W1 from the behavior specification.

| Contract | Minimum semantics | Consumers |
| --- | --- | --- |
| `ExecutionContext` | Project, session, logical environment, environment revision, kernel where relevant, execution, immutable attempt and lease generation; policy revision and authenticated actor | Tool dispatch, worker events, monitor correlation, provenance, review evidence |
| `ArtifactRef` | Stable resource ID, immutable revision/content hash, media type and producing attempt or import attribution; access resolved in project scope | Files, sources, outputs, documents, ledger, exports, reviews |
| `Anchor` | Exact artifact revision plus typed block/math node, PDF physical page/display label/region, or code/cell location; stale/orphan state | Viewer navigation, citations, comments and review locations |
| `Mutation` | Target, expected revision, operation ID, actor and patch; conflict response with current revision | Document edits, ledger updates, managed shared-file publication |
| `Event` | Event ID, type/schema version, stream/cursor, context references, timestamp, producer and payload/artifact references | Reconnect, streaming, review triggers, projections and monitor observations |
| `PermissionRequest` | Affected operation/attempt, matched rule and policy revision, requested scope, expiry, decision and safe continuation reference | Install/tool permissions and monitor escalation; preference questions remain distinct |
| `EnvironmentRecord` | Owner, base image digest, policy, dependency/configuration revision, data grants, mutable locations, backend binding and optional restoration checkpoint | New project/session creation, resume/restore, provisioning and optional reproduction |

For example, a document patch against revision 12 must not overwrite a user edit at revision 13. A review references revision 12 explicitly; a PDF citation references the imported PDF hash and physical page. A permission granted to one upload cannot authorize another destination or payload. Retries retain an operation ID; genuinely new execution attempts receive new attempt IDs.

Keep event transport separate from research semantics. Domain updates and their publication event must commit coherently, for example through a transactional outbox in the control store. Replay/deduplication is required; a universal event-sourced database and exactly-once external effects are not assumed. Monitor observations identify their trusted producer separately from agent assertions.

## Ordered work packages

### W0 — Behavior specification and harness foundation (roadmap M0)

Write short behavior specifications, in the project's own words, for the CScience features to reimplement (D51, D56). Draw on observed use, the user's experience and public documentation. The agent reports' line anchors into the proprietary bundle are evidence of *what* the behavior is; they are not text to copy. Cover at least:

- tool semantics: shell (working directory, stdout/stderr, exit codes, cancellation), persistent kernels, file handoff, errors;
- skills: discovery, on-demand loading, helper code, recorded load evidence;
- plans and delegation: versioned plans, tracks, delegated agents, role-specific model settings;
- automatic consistency review: checkpoint and terminal triggers, constrained reviewer scope, finding severity, author response, claimed-fix confirmation, bounded completion;
- automatic Conda provisioning: request, install, record, probe;
- research artifacts: versions, annotations, lineage.

Each spec ends with acceptance fixtures that the new implementation must pass.

Choose the harness building blocks (D55, D60, D61): Python throughout the server side; a provider-neutral model interface over the LiteLLM proxy, with the OpenAI-compatible adapter first and Anthropic Messages later; the MCP client SDK; and own code for the research loop. Before choosing, assess reuse from OpenScience Desktop upstream (MIT) and OpenCode (MIT), and from the user's own `cscience-crypto` wrapper code where it embeds no upstream code. Record each reused file's license and revision.

**Exit evidence:** behavior specs with fixtures, a harness-component decision with licenses recorded, and a mapping from the specs to the shared contracts above.

### W1 — Define shared references and one worker adapter (M0 → M1)

Implement the smallest context/artifact/event/operation interfaces needed for the first slice, and the harness tool loop that calls them. Use the Jupyter messaging protocol for the Python kernel from the start (D41), so Sage and R later share the same adapter. Add worker-side capability probes, scoped file staging/publication, streamed output, cancellation, idempotent submission and attempt lookup. Do not yet generalize every possible backend or kernel.

Run the control application in one container and execution in another on an available local target. Use an observed process/container identity and a harmless file available only in the worker to establish location. Verify that a host-only sentinel outside granted paths is inaccessible. Close/reconnect the client during streaming and reconcile the same attempt. Reject unsupported execution instead of falling back beside the orchestrator.

**Exit evidence:** ordinary shell and two stateful Python cells work through the new harness's tools; file publication, interruption and reconnect preserve correct identity and outcomes. Capture actual boundary enforcement, errors and any untested coverage. This is the first concrete implementation checkpoint.

### P — Harness-independent infrastructure track (parallel with W0–W2)

Proposed by the plan review (D51). These components do not depend on harness details. Build them as MIT code under the [licensing policy](LICENSING.md):

- Worker API schema and worker service, with the persistent-kernel, argv, file-staging, streaming, cancellation and attempt-reconciliation operations.
- Read-only-rootfs environment layout with declared volumes, lockfile capture and rebuild (D48).
- Policy middleware, which decides research sandboxes' requests inside OpenShell's proxy in place of a separate egress proxy (D69, D70, `packages/policy-middleware`), and the package mirror (D44).
- Event store with an append-only hash chain, so monitor evidence is tamper-evident ([event store](EVENT-STORE.md), D74, `packages/event-store`).
- Adversarial fixture suite ([monitoring](MONITORING.md)).
- Isolation-tier probes that report the tier actually provided (D43).
- The policy compiler from the project policy file ([policy](POLICY.md), D62) to OpenShell network/MCP/filesystem policy, with the hard floor, pool ceiling, most-restrictive-wins precedence and escalation timeout. It reports rules it cannot enforce. The policy model, validation and evaluation are implemented in `packages/policy` (D68), and its OpenShell compiler (D69) produces the research sandboxes' admission endpoints and policy-middleware attachment, and the MCP proxy's policy.
- Multi-arch (amd64/arm64) base images: the published `minimal` image and the `crypto-base` recipe with a private build pipeline and registry (D46), plus a babbage MicroVM gateway configuration (D63).
- Secrets through OpenShell providers with endpoint binding and least-privilege tokens (D57), and MCP endpoint policies with tool allowlists (D58). Add the connector auth service (OAuth discovery, registration, PKCE with `resource`, external refresh when needed) and the MCP proxy sandbox (the only MCP endpoint reachable from workers and the orchestrator) ([MCP connectors](MCP-CONNECTORS.md), D66, D67); connect crypto-research-mcp as the phase 1 fixture. A fixture confirms that the sandbox sees only placeholders and that a placeholder sent to an unbound host fails with `credential_endpoint_mismatch`.

Implement the worker API against OpenShell only (D52). Keep it backend-neutral by maintaining a **backend conformance fixture suite** (the W1/W2 fixtures, the adversarial suite and the tier probes) that any future backend must pass; the reference fallback stays a written design until a concrete limitation justifies building it.

**Exit evidence:** the W1 fixture runs through the worker service on OpenShell, with mirror-only egress and the adversarial suite passing; the conformance suite is runnable against a backend adapter without harness changes.

### W2 — Certify babbage execution with math/source capabilities (M1, phase 1)

Map one backend adapter to the selected runtime. If using OpenShell, pin matching components and let its gateway own the chosen driver lifecycle. In phase 1 that is the MicroVM driver on babbage; supporting every local engine is unnecessary. Hosted Kubernetes certification is W2h in phase 2.

Add a native Sage kernel adapter, evaluating its Jupyter kernel rather than emulating Sage in ordinary Python. Use the `crypto-base` profile so Sage is present at session start; verify runtime installation from conda-forge through the mirror for additional tools (D46). Platform code speaks the Jupyter protocol to Sage's own kernel and never imports Sage. Measure first-install time, and Sage/fpylll performance under each isolation tier. Exercise preparsing, finite fields, polynomial rings, exact matrices, LaTeX results, interruption and reset. Add worker PDF extraction/rendering with source hashes and page coordinates, starting with pdf_oxide (D59); preserve originals. Test Conda provisioning and hooks under the same enforced environment/network injection as ordinary execution.

Connect the trusted monitor and durable decision queue. Demonstrate block, permission escalation with approve/deny, and alert-and-continue using harmless fixtures. An approval path must prevent the unauthorized effect and show a safe continuation; an arbitrary denied network request must not be automatically replayed. Separately test middleware failure policy and expose unsupported inspection paths.

**Exit evidence:** babbage passes the boundary, native Sage, PDF, provisioning, monitor and adversarial fixtures at its recorded isolation tier. Record OS, architecture, runtime, storage and network controls. Missing required policy support blocks certification rather than reducing isolation.

### W2h — Certify hosted execution (phase 2, roadmap M3)

Run the same W2 fixtures on the Kubernetes test cluster through the unchanged worker API: Agent Sandbox controller, a CNI that enforces NetworkPolicy, gVisor RuntimeClass on amd64 and arm64 nodes, tier 3 where nodes support it, and multi-arch images. **Exit evidence:** per node architecture and tier, the same fixtures pass. Do not infer arm64 or macOS parity from amd64 success.

### W3 — Make project/session state durable (M2)

Background jobs (D83) passed [lab 19](../.agents/docs/agent-reports/babbage-openshell-lab-19-2026-10-12.md), including time/concurrency limits, whole logs, job tools, durable completion notices, waits and watcher check-ins. The reconnectable sandbox executor passed [lab 20](../.agents/docs/agent-reports/babbage-openshell-lab-20-2026-10-12.md): same-attempt replay and kernel continuity through worker restarts, offline completion/timeout, cancellation after reconnect, and explicit loss when the owner is gone. Daemon-incarnation loss requires a verified stop/start of the original managed VM before replacement readiness; disk persists while old processes and kernel memory are lost. Bounded history may eventually evict old ended attempts, and unresolved live endpoints delay startup. Automatic session wake-up and project-wide model spend accounting still need the long-running control application; a CLI-only run must report those as unavailable.

Create the persistent default project environment and separate kernels/workspaces for two workbooks. Create a third workbook with a dedicated environment from the base image. Demonstrate that project-installed tools are absent from the new dedicated environment while granted shared data and project records remain available.

Serialize package mutations in shared environments and advance their recorded revision. Publish outputs through the artifact interface. Exercise two sessions writing the same versioned resource and return an explicit conflict. For mutable shared files, choose a managed write/lease mechanism or present conflicting copies; a writable shared mount alone does not meet the requirement.

Implement retain/suspend or capture/restore using one environment lifecycle component. Account for every allowed writable location, including tool/configuration state outside data volumes. With the read-only root filesystem layout (D48), the writable locations are the declared volumes, so capture becomes volume snapshots plus image digest plus lockfile. Install a tool, modify files/configuration, idle the project, restore the same logical environment and verify recorded state. Simulate a failed capture/restore without discarding the only copy. Report kernel memory loss; closing the GUI must not terminate ongoing work.

**Exit evidence:** project/workbook ownership, base-image initialization, shared data, conflict reporting, dependency revisions, idle recovery and browser-independent execution work on the selected backends. A package manifest without restored state is insufficient.

### W4 — Integrate the initial research workspace (M2 → M3)

Document synchronization now has separate [worker catalog](WORKER.md#document-catalog-prerequisite)
and [control publication outbox](LIVING-DOCUMENTS.md#publication-outbox-prerequisite)
prerequisites. The latter records canonical changes and source events in one transaction
and exports complete pinned targets, with snapshot recovery across retention/bootstrap
gaps. Delivery, worker acknowledgement reconciliation, writer-state safety, direct-edit
capture and `ss-doc` still require implementation and real OpenShell acceptance before
environment publication is enabled.

The internal [worker source adapter](WORKER-DOCUMENT-SOURCE.md) now converts trusted
immutable captures to named patches or exact retained refusals, with explicit delegated
provenance and atomic quotas/replay receipts. Its local result never grants restoration;
authenticated capture ingress, writer proof and transport acknowledgement remain open.

Use the shared artifact/revision/anchor contracts for living documents, comments, sources and ledger entries. Keep open questions/issues, scientific findings, results and frontier candidates as typed entries in one project ledger; board/frontier/history are views. Link review findings without merging their resolution lifecycle into scientific result status.

Support attributed user edits and revision-checked agent patches; demonstrate stale-patch reconciliation and orphaned anchors. Use one TeX-preserving renderer across research views. Evaluate PDF.js navigation against exact page/region anchors while retaining worker extraction/rendering. Assumption changes should identify dependent claims/reviews for reconsideration, not silently rewrite conclusions.

The worker document-catalog prerequisite is implemented, but runtime document publication, direct-edit integration and `ss-doc` remain open (LD-49 to LD-52). Before enabling replacement/removal, complete the [protected-filesystem feasibility prerequisite](../.agents/docs/agent-reports/document-writer-guard-2026-10-12.md): prove arbitrary writable handle/mmap lifetime, namespace exclusion, exact capture/acknowledgement revalidation, bounded admission and monitor/controller failure/restart behavior under OpenShell. FUSE is a proposed boundary requiring implementation and real VM acceptance, not a selected or validated capability. Hold uncertain writer state; helper-only locks and ordinary VM recycling do not satisfy the requirement while preserving healthy kernels. Continue independent control outbox/source adapter/helper-contract work without claiming a runtime mirror.

The independently implemented [automatic reviewer](CONSISTENCY-REVIEW.md) now provides checkpoint/terminal triggers, a separately authored rubric (D56), exact evidence revisions, attributed responses, claimed fixes and confirmation/supersession outcomes. Independent adversarial review and [browser acceptance](../.agents/docs/agent-reports/consistency-review-2026-10-12.md) passed. Remaining work includes immutable shell artifact inventories, live final-message transport, idle scheduling and shared monetary accounting. The isolated [KaTeX checker](MATH-CHECKS.md) supplies mechanical observations; save scheduling and D82 finding delivery remain integration work. Drive triggers from durable events, including published shell-created files. Published frontier results enter from their citations without reproduction gates.

Register the external crypto skill pack through a common platform binding, recording effective revision and actual loading. Adapt its tools to the shared project resources and typed outcomes; keep the catalog external and one authoritative project history.

**Exit evidence:** PDF → native Sage → shared result → LaTeX living document → user edit/comment → automatic consistency review → board update works across shared and dedicated workbooks on babbage in phase 1, then on the Kubernetes test cluster in phase 2. Confirm D23–D26, D30–D35 and D37 through the integrated workflow.

### W5 — Validate independent placement and failure recovery (M3 → M4)

Package the GUI independently and configure explicit authenticated routes. Certify placements in stages (D53): all-local, then all-hosted, then hosted-control/local-compute with the local policy ceiling (D49). The remaining combinations stay design-compatible and are exercised as configuration variations, building on the certified local and hosted backends. Use worker-initiated control transport where local workers sit behind NAT; do not require an exposed container-engine API.

Exercise orchestrator restart/fencing, worker loss, duplicate requests, interrupted artifact transfer, cancellation while disconnected and pending permission decisions across browser closure. Reconcile old attempts before dispatching replacements. Relocate durable control state and permitted environment/data state with rollback until continuity is established. Do not promise live process-memory migration.

**Exit evidence:** a placement/failure matrix records what actually ran, preserved state, unresolved attempts, startup/transfer costs and leaked resources. Research scripts/tools do not change with placement. Hosted and local deployment support remains an initial product gate, not a future-only expansion.

### W6 — Broaden representative cryptography workflows (M5)

Use the established boundaries for lattice/fpylll work, parameter estimates, counterexample searches and implementation checks. Capture mathematical domains, assumptions, seeds/precision, effective skills and tool versions. Evaluate optional licensed Magma on an authorized target. Select deeper fixtures from real user research after the deterministic integration path works.

**Exit evidence:** useful research tasks meet the CScience behavior specs and produce navigable source, execution, document and review evidence on both deployment classes. Additional tools must use the same environment, permission and artifact mechanisms.

## Separate proposed extension: fresh computational reproduction

After W2/W3 establish execution and captured environments, implement reproduction as another bounded execution request with pinned evidence, read-only inputs and separate outputs. Reuse routing, kernel adapters, environment capture, transfer, events and policy decisions. A reviewer coordinates the job; do not build a second execution subsystem.

A small exact Sage result should reproduce without producer kernel memory and report match/mismatch/unavailable with its assumptions. This can run alongside W4, but is a proposed extension rather than a prerequisite for every automatic consistency review. Any project-result promotion gate needs explicit configuration; published results remain exempt from independent checking.

## Decisions to make through implementation evidence

| Choice | Evidence point | Default until then |
| --- | --- | --- |
| Harness building blocks | W0 behavior specs, license check and reuse assessment | Provider-neutral model interface over the LiteLLM proxy, OpenAI-compatible first and Anthropic Messages later (D61), + MCP SDK + own research loop (D55) |
| First local backend and OpenShell adoption | W2 enforcement, monitor, lifecycle and platform matrix | OpenShell with the MicroVM driver on babbage (D52, D63); the conformance suite keeps the worker API backend-neutral |
| Persistence/synchronization mechanism | W3 writable-state coverage, conflict and restore checks | Preserve the original environment/state until a verified alternative exists |
| Shared UI/kernel libraries | W2/W4 native math, output, anchor and security behavior | Jupyter-protocol kernels; candidate PDF.js/KaTeX integrations |
| New database or additional service | Actual store/availability limitation | Logical modules in the control application on one embedded store (D12) |

The M1 exit run passed on babbage (lab 9); provisioning's runtime checks passed in lab 18. Background jobs passed lab 19, including harness and browser checks; executor recovery passed lab 20, including the corrected daemon-loss lifecycle fence. Document editing, comments/work requests and automatic consistency review passed independent review and browser acceptance. Current work is W4 document outbox/source adapter/helper preparation, with a protected-filesystem feasibility prerequisite before runtime document replacement/removal and the ledger next, in the order in [.agents/handoff.md](../.agents/handoff.md). LD-49 and W3 package restoration remain open. The M2 exit run still needs a real model gateway and connector authorization from the user. Keep the backlog and runtime evidence beside each completed slice; draft-spec questions do not block the already-decided infrastructure work.
