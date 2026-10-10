# Detailed implementation plan

Date: 2026-10-10. Proposed engineering sequence; no work package has been implemented or validated. Confirmed product requirements remain in [decisions](DECISIONS.md). This plan incorporates the [consistency review](CONSISTENCY-REVIEW.md) and provides detail beneath the milestone [roadmap](ROADMAP.md).

## First implementation slice

Retain the CScience research loop in container A. Route one ordinary shell tool and two stateful Python cells into container B using a common worker contract. Return streamed output, a published file and observed execution location through the retained tools. Interrupt an operation and reconnect without launching it twice. This slice tests the central architectural assumption before adding the complete product workflow.

Its deliverables are an execution call map, an adapter or a precise blocked seam, a small integration fixture, and a record of processes/files/policy actually observed. A standalone batch demonstration cannot establish that CScience's everyday research tools have moved. Do not build a new GUI or choose a replacement harness to complete this slice.

## Ownership and packaging

| Deployable unit | Initial logical modules | Authority and durable state |
| --- | --- | --- |
| Web GUI/API proxy | Retained research views, shared math renderer, PDF viewer, document/comment and permission interfaces | Authenticates to control APIs; caches/projections are disposable. Independent endpoint configuration supports separate hosting. |
| Control application/orchestrator | Retained harness, project/session catalog, artifact metadata, ledger/documents, review coordination, permission decisions, execution router | Owns research history, scoped grants, revisions and execution intent. Initially retain the supported state store on private durable storage. |
| Trusted worker/policy infrastructure per pool | Worker service, environment/kernel managers, backend adapter, OpenShell gateway/supervisor if adopted, monitor integration | Owns runtime credentials, observed execution state, leases and policy enforcement. Keep this authority outside research-writable environments. |
| Project/session/batch research containers | Scientific tools, shell, persistent kernels, approved Conda operations, parsers and renderers | Own bounded mutable execution state and granted data access. No cluster administration, enrollment or broad provider credentials. |

Artifact bytes may initially live on private durable filesystem storage with authenticated transfer. Their repository interface should permit another backend later. Do not require remote workers to mount the orchestrator's database or filesystem. The diagram's modules need clear interfaces, not separate deployments for every feature.

## Shared contracts to establish first

Names below describe proposed interfaces, not already implemented public APIs. Resolve names and serialization against retained CScience types in W0/W1.

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

Keep event transport separate from research semantics. Domain updates and their publication event must commit coherently, for example through a transactional outbox in the retained store. Replay/deduplication is required; a universal event-sourced database and exactly-once external effects are not assumed. Monitor observations identify their trusted producer separately from agent assertions.

## Ordered work packages

### W0 — Establish the retained-runtime seams (roadmap M0)

Trace ordinary shell, Python/R kernel startup/execution, package installation, file operations, PDF/TeX helpers, remote compute, and execution-bearing MCP subprocesses. Classify trusted metadata operations separately from generated code. Record the actual automatic review configuration and its evidence/finding lifecycle; inspected defaults are not necessarily the active settings.

Map existing project, artifact, annotation, claim and verification types onto the shared references above. Inspect only relevant OpenScience fork components and their per-file terms. Inventory where an absolute local path or browser/native callback currently carries meaning. Define a narrow test seam without altering upstream behavior merely to simplify the spike.

**Exit evidence:** caller/adapter map, representative tool inputs/outputs and errors, review compatibility fixtures, retention/extraction proposal, and a patch-burden record. If a seam is unavailable, identify the exact limitation before evaluating replacement code.

### W1 — Define shared references and one worker adapter (M0 → M1)

Implement the smallest context/artifact/event/operation interfaces needed for the first slice. Keep the existing persistent Python JSON-lines protocol behind a kernel adapter. Add worker-side capability probes, scoped file staging/publication, streamed output, cancellation, idempotent submission and attempt lookup. Do not yet generalize every possible backend or kernel.

Run the control application in one container and execution in another on an available local target. Use an observed process/container identity and a harmless file available only in the worker to establish location. Verify that a host-only sentinel outside granted paths is inaccessible. Close/reconnect the client during streaming and reconcile the same attempt. Reject unsupported execution instead of falling back beside the orchestrator.

**Exit evidence:** ordinary shell and two stateful Python cells work through retained tools; file publication, interruption and reconnect preserve correct identity and outcomes. Capture actual boundary enforcement, errors and any untested coverage. This is the first concrete implementation checkpoint.

### W2 — Certify local and hosted execution with math/source capabilities (M1)

Map one backend adapter to the selected runtime. If using OpenShell, pin matching components and let its gateway own the chosen driver lifecycle. Start with one local option, then exercise hosted Kubernetes through the same contract. Local Kubernetes can be the first local path; supporting every local engine is unnecessary.

Add a native Sage kernel adapter, evaluating its Jupyter kernel rather than emulating Sage in ordinary Python. Exercise preparsing, finite fields, polynomial rings, exact matrices, LaTeX results, interruption and reset. Add worker PDF extraction/rendering with source hashes and page coordinates; preserve originals. Test Conda provisioning and hooks under the same enforced environment/network injection as ordinary execution.

Connect the trusted monitor and durable decision queue. Demonstrate block, permission escalation with approve/deny, and alert-and-continue using harmless fixtures. An approval path must prevent the unauthorized effect and show a safe continuation; an arbitrary denied network request must not be automatically replayed. Separately test middleware failure policy and expose unsupported inspection paths.

**Exit evidence:** both a local container deployment and hosted Kubernetes pass the boundary, native Sage, PDF, provisioning and monitor fixtures. Record tested OS/architecture/runtime, storage and network controls; do not infer macOS parity from Linux success. Missing required policy support blocks certification rather than reducing isolation.

### W3 — Make project/session state durable (M2)

Create the persistent default project environment and separate kernels/workspaces for two workbooks. Create a third workbook with a dedicated environment from the base image. Demonstrate that project-installed tools are absent from the new dedicated environment while granted shared data and project records remain available.

Serialize package mutations in shared environments and advance their recorded revision. Publish outputs through the artifact interface. Exercise two sessions writing the same versioned resource and return an explicit conflict. For mutable shared files, choose a managed write/lease mechanism or present conflicting copies; a writable shared mount alone does not meet the requirement.

Implement retain/suspend or capture/restore using one environment lifecycle component. Account for every allowed writable location, including tool/configuration state outside data volumes. Install a tool, modify files/configuration, idle the project, restore the same logical environment and verify recorded state. Simulate a failed capture/restore without discarding the only copy. Report kernel memory loss; closing the GUI must not terminate ongoing work.

**Exit evidence:** project/workbook ownership, base-image initialization, shared data, conflict reporting, dependency revisions, idle recovery and browser-independent execution work on the selected backends. A package manifest without restored state is insufficient.

### W4 — Integrate the initial research workspace (M2 → M3)

Use the shared artifact/revision/anchor contracts for living documents, comments, sources and ledger entries. Keep open questions/issues, scientific findings, results and frontier candidates as typed entries in one project ledger; board/frontier/history are views. Link review findings without merging their resolution lifecycle into scientific result status.

Support attributed user edits and revision-checked agent patches; demonstrate stale-patch reconciliation and orphaned anchors. Use one TeX-preserving renderer across research views. Evaluate PDF.js navigation against exact page/region anchors while retaining worker extraction/rendering. Assumption changes should identify dependent claims/reviews for reconsideration, not silently rewrite conclusions.

Retain Science-style automatic consistency review and actual configured checkpoint/terminal triggers. Persist exact evidence revisions, author responses, claimed fixes and confirmation/supersession outcomes. Drive triggers from durable events, including published shell-created files. Published frontier results enter from their citations without reproduction gates.

Register the external crypto skill pack through a common platform binding, recording effective revision and actual loading. Adapt its tools to the shared project resources and typed outcomes; keep the catalog external and one authoritative project history.

**Exit evidence:** PDF → native Sage → shared result → LaTeX living document → user edit/comment → automatic consistency review → board update works across shared and dedicated workbooks on local and hosted workers. Confirm D23–D26, D30–D35 and D37 through the integrated workflow.

### W5 — Validate independent placement and failure recovery (M3 → M4)

Package the GUI independently and configure explicit authenticated routes. Validate all eight local/remote combinations of GUI server, orchestrator and worker pool, building on the certified local and hosted backends. Use worker-initiated control transport where local workers sit behind NAT; do not require an exposed container-engine API.

Exercise orchestrator restart/fencing, worker loss, duplicate requests, interrupted artifact transfer, cancellation while disconnected and pending permission decisions across browser closure. Reconcile old attempts before dispatching replacements. Relocate durable control state and permitted environment/data state with rollback until continuity is established. Do not promise live process-memory migration.

**Exit evidence:** a placement/failure matrix records what actually ran, preserved state, unresolved attempts, startup/transfer costs and leaked resources. Research scripts/tools do not change with placement. Hosted and local deployment support remains an initial product gate, not a future-only expansion.

### W6 — Broaden representative cryptography workflows (M5)

Use the established boundaries for lattice/fpylll work, parameter estimates, counterexample searches and implementation checks. Capture mathematical domains, assumptions, seeds/precision, effective skills and tool versions. Evaluate optional licensed Magma on an authorized target. Select deeper fixtures from real user research after the deterministic integration path works.

**Exit evidence:** useful research tasks preserve CScience behavior and produce navigable source, execution, document and review evidence on both deployment classes. Additional tools must use the same environment, permission and artifact mechanisms.

## Separate proposed extension: fresh computational reproduction

After W2/W3 establish execution and captured environments, implement reproduction as another bounded execution request with pinned evidence, read-only inputs and separate outputs. Reuse routing, kernel adapters, environment capture, transfer, events and policy decisions. A reviewer coordinates the job; do not build a second execution subsystem.

A small exact Sage result should reproduce without producer kernel memory and report match/mismatch/unavailable with its assumptions. This can run alongside W4, but is a proposed extension rather than a prerequisite for every automatic consistency review. Any project-result promotion gate needs explicit configuration; published results remain exempt from independent checking.

## Decisions to make through implementation evidence

| Choice | Evidence point | Default until then |
| --- | --- | --- |
| Adapted CScience or compatible replacement | W0/W1 patchability, semantics and retained behavior | Retain the harness, GUI and supported store |
| First local backend and OpenShell adoption | W2 enforcement, monitor, lifecycle and platform matrix | One available local candidate; no promised universal engine support |
| Persistence/synchronization mechanism | W3 writable-state coverage, conflict and restore checks | Preserve the original environment/state until a verified alternative exists |
| Shared UI/kernel libraries | W2/W4 native math, output, anchor and security behavior | Retained Python adapter; candidate Sage Jupyter/PDF.js/KaTeX integrations |
| New database or additional service | Actual retained-store/availability limitation | Logical modules in the control application; no migration prerequisite |

Immediate next work is W0/W1. Detailed policy rules and research categories can be refined against the working slice; another broad planning round is unnecessary before tracing and adapting the execution seam.
