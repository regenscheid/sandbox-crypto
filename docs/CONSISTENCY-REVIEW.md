# Consistency and shared-component review

Date: 2026-10-10. Planning review only. Source inspection supports the findings below; proposed components and backend mappings have not been implemented or exercised.

> **Update (2026-10-10, D11):** the user decided to reimplement the needed CScience behavior rather than adapt or patch the Claude Science runtime. Where this document speaks of retaining, adapting or patching CScience mechanisms, read it as *behavior evidence for the reimplementation*. No Claude Science code, prompts, schemas or assets are copied (D56). OpenScience Desktop upstream is MIT-licensed and remains a code-reuse candidate, subject to per-file checks.

## Conclusion

The requirements fit together. The main implementation risk is building separate infrastructure for each feature: a document history, a ledger history, a reviewer evidence store, a permission queue, and an execution journal that cannot reliably refer to one another. Share identity, immutable revisions, artifact transfer, event delivery, and scoped decisions. Preserve domain-specific meaning and lifecycle on top of those mechanisms.

Start by proving one ordinary research operation across the container boundary with the new harness (D11). The [implementation plan](IMPLEMENTATION-PLAN.md) turns this review into ordered work with evidence gates. It does not assume a new harness, database, or collection of microservices.

## Inconsistencies and ambiguities resolved

| Area | Planning drift or implementation trap | Resolution |
| --- | --- | --- |
| Functional baseline | Some feature lists read as a new workbench implementation before establishing whether CScience can be adapted | Retain its research loop and supported state store during the first spike. Measure execution seams and patch burden before deciding replacement scope. |
| Automatic review | Several acceptance paths made fresh reproduction sound mandatory alongside Science-style automatic review | Preserve current consistency review scheduling, scope and finding/confirmation behavior. Computational reproduction is a separately configured proposed extension, with its own gate. |
| Published frontier results | A universal verification gate would contradict the requirement to accept published results directly | Record published results with source, conditions and reported measures. Apply project-result evidence/promotion policy separately; no automatic rechecking of cited research. |
| Ledger and open items | “Open issues tracked separately” could imply another store | Use typed entries in the same project ledger. Result/frontier and open-issue views filter those entries and their relationships. Review findings have their own lifecycle and link to entries. |
| New versus restored sessions | A common snapshot mechanism could accidentally initialize a new workbook from the evolved project environment | New dedicated session environments start from the pinned base image. Resume/restore addresses an existing logical environment and its checkpoint. These are separate operations. |
| Shared files | A shared writable mount does not provide conflict detection | Use separate session working directories and revision-checked publication. For deliberately mutable shared files, require a managed write/lease or expose conflicting versions. State the limits of any direct writable mount. |
| Dependencies | Sharing project data could accidentally share a mutable Conda prefix across dedicated environments | Each environment owns its dependency state. Serialize package changes in a shared project environment; dedicated session environments share data references, not installed-tool state. |
| Idle preservation | A package list plus persistent volume may omit changed container files and configuration | Capture/retain every permitted mutable location, dependency/configuration state and durable volume. Verify restoration before releasing the only complete environment copy. Live kernel memory is a separate capability. |
| Local deployment | “Same Kubernetes manifests” could become an unintended Kubernetes-only requirement | Certify one local container path and hosted Kubernetes initially. Local Kubernetes is acceptable; direct Docker/Podman is optional. Select using observed enforcement and persistence. |
| Service diagram | Logical boxes could be interpreted as mandatory independent deployments | Keep document, ledger, review and permission modules together in the control application initially. Separate the GUI, orchestration, and trusted worker/policy infrastructure according to placement and authority. |
| Monitor escalation | Human approval cannot safely wait inside a bounded inspection RPC | Persist the request outside middleware. Prevent the affected effect, then resume or retry only when that operation can do so safely under the scoped decision. Do not blindly replay a network request. |
| Alerting | Endpoint audit mode and middleware failure policy could be mistaken for the requested alert-and-continue action | A middleware denial still blocks even in endpoint audit mode. Alert-and-continue must explicitly allow the operation and emit a finding. `on_error` handles middleware failure, independently of the matched rule action. |
| Completion evidence | Browser callbacks and agent narration could become authoritative proof of execution | Worker/control observations own execution status and artifact publication. Record agent assertions as assertions. Reconnect the browser by replaying durable events. |

The OpenShell approval/alert distinctions follow its current [middleware configuration documentation](https://docs.nvidia.com/openshell/latest/extensibility/supervisor-middleware/configure): the documented default RPC timeout is 500 ms, the maximum is 30 seconds, and failure policy is distinct from middleware verdicts. This establishes an integration constraint, not a tested approval mechanism.

## Shared foundations and their consumers

These are logical modules. Their ownership should be explicit even when several share one process and database.

| Common component | Reused by | Shared contract and boundary |
| --- | --- | --- |
| Project identity and resource scope | Sessions, containers, skills, documents, ledger, reviews, connectors | Stable project/session IDs and scoped resource references. Session, environment, kernel, execution and attempt remain distinct identities. |
| Artifact and revision repository | Papers, extracted pages, cell outputs, documents, ledger evidence, reviews, exports | Immutable bytes/revisions, content hashes, lineage, expected-revision mutations and publication. Large payloads travel as artifact references. |
| Typed source anchors | PDF navigation, document comments, ledger citations, review locations | One representation of revision plus block/math node, PDF page/region, or code/cell position. An uncertain reattachment is visible. |
| Durable event delivery | Worker streaming, GUI reconnect, review triggers, monitor findings, ledger projections | Correlation, event IDs, per-stream sequence/cursor, replay and deduplication. Persist domain changes and their events together; a broker is not automatically required. |
| Scoped decision workflow | Install permissions, tool grants and monitor escalation | Durable pending/approved/denied/expired state, exact operation/scope, policy revision and continuation. User preference questions and scientific verdicts are different record types. |
| Execution and environment services | Shell, kernels, package managers, parsers, renderers, batch jobs and optional reproductions | Common routing, limits, cancellation, reconciliation, files and provenance. Backend adapters map these operations to actual enforcement capabilities. |
| Capability and procedure registry | Worker selection, Conda provisioning, external skills, MCP and role configuration | Required versus observed tools; effective versions and actual load/probe evidence. A description advertised by a connector is not a successful worker probe. |
| Mathematical presentation | Chat, workbooks, documents, comments, ledger and reviews | One TeX-preserving rendering policy, math-node identity and visible errors. Full document compilation uses the ordinary worker boundary. |

Do not flatten every object into one generic status field. A scientific result, an open research question, a review finding, an execution failure and a monitor alert differ in meaning. Share references and persistence mechanisms while retaining their own schemas and transitions. Frontier selection is a research-domain projection with comparison rules; it is not the event transport or review queue.

## Concrete reuse candidates

### CScience mechanisms (behavior evidence for the reimplementation)

The current source already contains execution logs, artifact versions, scoped memory, exact-version verification records and unified annotations. They provide candidate backing structures for shared references. Extend or adapt these structures after tracing callers; do not create a competing ledger/document store merely because the new architecture diagram has more boxes. Relevant inspected migrations are `0005_execution_log.sql`, `0026_memory.sql`, `0029_verification.sql`, `0085_annotations_unified.sql` and `0115_verification_checks_resolution.sql` under `dist/review-0.1.60/runtime/assets/drizzle/sqlite/` in the existing checkout.

The bundled Python kernel worker is a long-lived JSON-lines subprocess, with request IDs, persistent variables, streamed output, interruption and usage data. It is not already a Jupyter kernel. First retain its semantics through a kernel adapter; changing the wire protocol is independent of moving execution to a worker. Evidence: `assets/kernels/kernel_worker.py`, header lines 3–21, streaming around 162–204 and main loop around 1672 onward. The broader [source study](SOURCE-STUDY.md) records review/document behavior and pinned source versions.

The local wrapper's capability profiles and installation-root discovery are useful to adapt. Run actual probes inside the assigned worker and its policy. The existing P38 Conda handling preserves the sandbox-injected proxy on Linux/macOS when no explicit CScience proxy overrides it. Reuse one environment/network injection path across kernels, shell and package operations so provisioning cannot silently bypass or break inspection. Current evidence: `pkg/platform/lib/profiles.mjs`, `pkg/platform/lib/capabilities.mjs`, and `src/patches.mjs` P38 around 1693–1778. This code inspection is not a worker integration test.

### Standard components worth testing

| Candidate | Opportunity | Required evidence before adoption |
| --- | --- | --- |
| Jupyter messaging/kernel adapters | Share execution, interruption and rich mathematical output across Sage and other compatible kernels; retain the existing Python adapter initially | Test native Sage syntax, exact types, output events and interruption through the same worker API. Keep kernel sockets private to the worker; do not expose a Jupyter server merely to use its kernel protocol. [Messaging](https://jupyter-client.readthedocs.io/en/stable/messaging.html), [Sage kernel](https://doc.sagemath.org/html/en/reference/repl/sage/repl/ipython_kernel/kernel.html) |
| PDF.js viewer | Reuse a browser PDF renderer and layer project navigation/anchors over it | Demonstrate stable page/region navigation and selection on exact source revisions. It does not replace scientific PDF extraction/OCR or mathematical transcription checks. [PDF.js](https://mozilla.github.io/pdf.js/) |
| KaTeX renderer | Share browser math rendering across every research view while preserving original TeX | Test matrix/aligned syntax, macro scope, errors, and rendering security settings. Full TeX compilation remains separate worker execution. [Security guidance](https://katex.org/docs/security.html) |
| OpenShell runtime and middleware | Reuse sandbox lifecycle and policy hooks across certified pools | Use its selected driver per gateway rather than introduce a second scheduler. Verify writable-state retention, local/cluster controls, supported inspection and the three monitor actions. [Runtimes](https://docs.nvidia.com/openshell/latest/how-it-works/sandboxes/runtimes), [evaluation](OPENSHELL-EVALUATION.md) |
| OpenScience fork contracts | Learn from typed execution/events, permission receipts, replay and teardown failure handling | Extract only components whose dependencies and current behavior are established. Native bridge and unfinished agent/persistence paths are not ready substitutes for a backend. [Assessment](REUSE-ASSESSMENT.md) |

External crypto skills remain in their own repository. A shared platform binding should expose project resources, execution, documents/ledger and decisions to them; do not copy their catalog or reproduce a separate board implementation for that pack.

## What the review does not settle

Backend choice, maintainable CScience patchability, exact persistence implementation, and a replacement harness remain evidence-dependent. Jupyter, PDF.js and KaTeX are targeted candidates, not selected infrastructure. This review recommends common contracts and implementation boundaries; it adds no requirement for a new database, workflow engine, event broker, real-time collaboration framework, or public multi-user service.
