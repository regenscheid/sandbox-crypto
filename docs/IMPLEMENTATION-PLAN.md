# Detailed implementation plan

Updated: 2026-10-12. Engineering sequence beneath the milestone [roadmap](ROADMAP.md). Track P has the policy library, OpenShell compiler, policy middleware, package cache and event store. W1/W2 provide the [worker service](WORKER.md), harness and native math execution. M1 passed its babbage exit run; provisioning, background jobs and executor recovery have real OpenShell acceptance. W4 has the [workspace](WORKSPACE.md) GUI with document editing, comments, consistency review and mechanical math findings checked in the browser. Delivery baseline main `9b49de9` includes PR #69 through #106 with fresh CI green; D82 passed final combined local gates (9,459 Python/434 web tests and all required checks/builds), PR #83 merged after fresh CI passed, and #84 recorded 9,494 Python/436 web tests. The integrated M2 research workflow remains open. Confirmed product requirements stay in [decisions](DECISIONS.md); this plan incorporates the [consistency review](CONSISTENCY-REVIEW.md).

The latest parallel slices merged after independent review and fresh Python 3.12/3.13, web and security checks: [table parser parity](TABLE-PARSER-PARITY.md) (PR #94), [kernel start revisions](KERNEL-REVISIONS.md) (PR #95) and [project macros](PROJECT-MACROS.md) for Markdown/KaTeX (PR #96). Final combined local gates passed 9,893 Python tests, 575 web tests, 24 post-build math cases and required static/license/protocol/build checks. Real OpenShell Lab25 and native macro browser/KaTeX acceptance remain pinned to their recorded source; all owned runtime state was removed. The observation-only existing-channel kernel transport now passes independent review, 9,926 full Python tests and actual [OpenShell Lab26](../.agents/docs/agent-reports/babbage-openshell-lab-26-2026-10-12.md) idle stop/no-wake acceptance with complete cleanup. PR #97 merged at `d61e4ad` on independently reviewed signed `8fd62e8` after all four fresh checks passed (run `38315213515`). The separate [immutable export-input snapshot prerequisite](DOCUMENT-EXPORTS.md) passes independent review, 9,979 Python/575 web/24 math and required checks/builds ([local evidence](../.agents/docs/agent-reports/document-export-inputs-2026-10-12.md)); PR #98 delivery is recorded below. It retains exact source/parser/original TeX/macro inputs, durable replay and bounded quotas with explicit unsupported-input holds. PR #98 merged at `b207c9d` on reviewed signed `013adcb` after all four fresh checks passed (run `38316931992`). The [visible-session GUI panel](WORKSPACE-KERNELS.md) now passes independent review on signed `70f6761`, 10,032 Python/614 web/24 math and real [browser/OpenShell Lab27](../.agents/docs/agent-reports/babbage-openshell-lab-27-2026-10-12.md), including complete cleanup. PR #99 merged at `afb049a` on reviewed signed `fe5f4d7` after all four fresh checks passed (run `38325477608`). Native hidden-tab behavior is explicitly unproved by the browser surface. PR #101 now delivers passive original-birth lineage and an internal durable inbox prerequisite after independent review and all four fresh checks (reviewed signed `ab8dd83`, merged `91940ab`, run `38329083314`); actual authenticated MAIN save-before-ACK delivery remains unwired. Durable agent delivery, protected worker staging and `.tex`/PDF conversion remain open.

The [CP-13 sticky admission prerequisite](RECOVERY-FENCE.md) is implemented and independently reviewed at signed `e320236`. Final combined gates passed 9,532 Python tests (43 opt-in skips), 436 web tests, 24 post-build math checks, locked/static/license/protocol checks and both builds. Root-owned [OpenShell Lab21](../.agents/docs/agent-reports/babbage-openshell-lab-21-2026-10-12.md) passed real linking cancellation, graceful worker-offline/reconnect and explicit confirmed delete/reuse, preserving existing research state and refusing new protected work. Both labs are completely removed; PR #85 merged after all three fresh CI checks passed. Restoring/clearing package state remains open; no package-list comparison proves recovery.

The additive [MAIN explanation math slice](FINDING-PROSE-MATH.md) is implemented and independently reviewed at signed `f7bb0a3`, including actual main `744d1b5`. Only MAIN fix/rebut explanation bodies are eligible; ordinary body corrections preserve original scientific actions/evidence/CAS and reviewer decisions pin the current body independently. Final combined gates passed 9,730 Python tests (43 opt-in skips, two existing warnings), 443 web tests, 24 post-build math checks and all required checks/builds. Root-owned [browser acceptance](../.agents/docs/agent-reports/finding-prose-math-2026-10-12.md) passed six child states, immutable original/current provenance, separate mechanical/scientific confirmation and session isolation; full teardown was verified at 15:37:06.734290 UTC. PR #89 merged at `3c91c30` after fresh Python 3.12/3.13/web and security checks passed (run `38272517996`). Ledger and reviewer/USER/platform prose, idle dispatch and model repair proposals remain outside this slice.

The additive [comment/reply math slice](COMMENT-MATH.md) is implemented and independently reviewed at signed `77ecc171`: immutable body revisions and ordinary corrections, exact block/corrector provenance, bounded checks and shared closing delivery. Dependency-only changes refresh evidence without consuming failed-block opportunities. Final combined gates passed 9,631 Python tests (43 opt-in skips, two existing warnings), 441 web tests, 24 post-build math checks and all locked/static/license/protocol checks/builds. Fresh root-owned [browser acceptance](../.agents/docs/agent-reports/comment-math-2026-10-12.md) verified corrected budgets, exact source/history, retained independent CAS drafts, accepted/removal/unavailable evidence and session isolation; complete teardown was verified at 13:50:58.272097 UTC. PR #87 merged at `d2b133f` after all three fresh CI checks passed on `16bb1308`. Ledger/finding-prose checks, model repair proposals, idle dispatch and automatic compaction remain open.

## Kernel notification prerequisites

[Passive original lineage](KERNEL-NOTICE-LINEAGE.md) and the [bounded internal notice inbox](KERNEL-NOTICES.md) pass independent source/union review and 10,155 combined Python cases. [OpenShell Lab28](../.agents/docs/agent-reports/babbage-openshell-lab-28-2026-10-12.md) proves original birth retention, current versus stale sessions, worker continuity, real idle stop/no wake and ordinary resume with new incarnation/loss; all owned state is removed. Unknown/legacy provenance remains local-fixture coverage. MAIN delivery is still open: authenticated control session lifetime and worker/config binding, lossless mapping, safe-point injection, successful model delivery tracking and durable save-before-ACK. The internal queue does not auto-enroll a changed VM/incarnation or wake an idle agent. PR #101 merged at `91940ab` after all four fresh checks passed on reviewed signed `ab8dd83` (run `38329083314`).

## Original prompts and planning

The user requested a planning phase and live in-process/completed plan updates (2026-10-12, D86). The independently authored [prompt catalog and design](ORCHESTRATOR-PROMPTS.md) audits actual MAIN/watcher/reviewer/repair/tool inputs and covers planning, execution, delegated tracks, background monitoring, review/correction, steering, interruption/resume and compaction. It learns behavior from the existing investigations without copying upstream prompts, code or schemas. The source-text corrections are merged as PR #100. The bounded [immutable session-plan core](SESSION-PLANS.md), six MAIN tools and conditional original phase guidance are independently reviewed at signed `4deb2dc` and `f31ef29`. Their active safe-point USER-edit reread fence requires a current plan response actually delivered to a successful model call; durable progress is visible independently of later RunRecord save. Root has integrated the independently reviewed live viewer `736b1ca`, with 656 web/24 math cases and both builds passing. The independently reviewed API `7b774f6` and signed runtime union `5259f6c` pass combined local gates (10,312 Python/43 opt-in skips/two existing warnings, corrected 127 core/API cases, 656 web/24 math and required checks/builds) and actual [browser acceptance](../.agents/docs/agent-reports/session-planning-2026-10-12.md). Live committed progress, stable-ID revision with old-completion reset, qualified outcomes, isolation, closed one-shot history, unavailable/recovery and sign-out are verified with synthetic canonical activity. Independent complete cleanup is verified at 2026-10-13T01:22:48.894078Z; PR #102 merged at `25510ab` after all four fresh checks passed on reviewed signed `1d4205f` (run `38332303702`). Narrow [MAIN input provenance](MAIN-PROMPT-PROVENANCE.md) now retains exact system/tool content pins with legacy/custom unknowns and unchanged wording/grants. Its independently reviewed signed `34591d3` and actual-main union `c64a4d3` pass [combined local gates](../.agents/docs/agent-reports/main-prompt-provenance-2026-10-12.md): 10,344 Python/656 web/24 math and required checks/builds; PR #103 merged at `8859407` after all four fresh checks passed on reviewed signed `6df40ba` (run `38334071913`). The separate [watcher reservation pins](WATCHER-PROMPT-PROVENANCE.md), reviewed signed `ba35ca4` and union `96e1585`, pass [combined local gates](../.agents/docs/agent-reports/watcher-prompt-provenance-2026-10-12.md): 10,385 Python/656 web/24 math and required checks/builds; PR #104 merged at `5f14278` on reviewed signed `c28b46d` after all four fresh checks passed (run `38335393962`). The [reviewer reservation pins](REVIEWER-PROMPT-PROVENANCE.md), reviewed signed `5e7c4e3` and union `2197931`, pass [combined local gates](../.agents/docs/agent-reports/reviewer-prompt-provenance-2026-10-12.md): 10,424 Python/656 web/24 math and required checks/builds. Exact historical rubric/ordered-tool metadata and five accounting/lease cancellation drains preserve original review authority and budgets. Actual watcher main is integrated at tree-identical signed `862238f`; PR #105 merged at `aac0633` on reviewed signed `a42d46f` after all four fresh checks passed (run `38336876907`). The separately reviewed stock/pilot implementation is described below; USER editing/approval, durable repair call/spend accounting and general external catalog loading remain open. Explicit goal outcome must qualify terminal plans; blocked/skipped steps never imply research success. Existing user authorization remains valid; approval gates apply only where required. Delegation, questions and compaction need their own enforcement and lifecycle work.

Five [explicit original research framings](STOCK-RESEARCH-PROMPTS.md) and [general pilot guidance](PILOT-COMPUTATIONS.md) now pass independent source/union review at signed `5a96c42`/`c33bdc2`. The user requested testing a script on a small scale before a long/big computation: the ordinary MAIN base2 now instructs representative/known-case/output checks, failed-trial resolution, available time/memory measurements and qualified scaling/plan evidence. Optional framing selection does not control whether planning or pilots apply. [Combined gates](../.agents/docs/agent-reports/research-prompt-workflow-2026-10-12.md) on actual-main signed `a5f3de8` pass 10,476 Python/656 web/24 math and all required checks/builds; PR #106 merged at `9b49de9` on reviewed signed `506b160` after all four fresh checks passed (run `38339832550`). This is original prompt guidance, not a computation classifier, measurement tool or proof of live-model compliance/scientific correctness.

Bounded [display-repair input identity](DISPLAY-REPAIR-PROVENANCE.md) is implemented and independently clear at signed `87e2711` and actual-main union `d2c3263`. [Combined gates](../.agents/docs/agent-reports/display-repair-input-provenance-2026-10-12.md) pass 10,508 Python/43 opt-in skips/two existing warnings, 656 web/24 math and all required static/license/protocol/build checks. The optional producing-answer descriptor retains exact existing adapted system/zero-tool/formatter identity through the original cache/in-flight result, with strict 4 KiB bounds and no dynamic formula/error hashes. Process/cache lifetime only: durable project call/spend records, resolved-model attribution and LD-46 remain open. PR #107 merged at `7f89853` on reviewed signed `979b874` after all four fresh checks passed (run `38343981986`). USER plan-body editing backend is signed `5284009` with 51 focused/271 affected cases and scoped checks passing, under independent review; structured frontend is in progress. Combined gates/browser acceptance and approval routes remain separate.

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

The existing external conformance/probe and linux/amd64 profile acceptance now passed
in [Lab23](../.agents/docs/agent-reports/babbage-openshell-lab-23-2026-10-12.md) on frozen
main `73c07aa`: 23 unchanged external cases per minimal/crypto-base profile, with
23 local variants intentionally skipped per run. Both Python 3.14.8 guests retained
effective/declared tier 3 and their original distinct boots. The complete lab and
owned fixtures were removed. This does not certify Sage-specific functionality,
arm64, Kubernetes or multi-platform images; it changes no product/image/test code.

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

The narrow LD-44 [formula copying slice](WORKSPACE.md#math-repairs) uses shared original-source metadata and an app-scoped cloned-range copy handler. Valid, repaired and Unicode formulas copy their original LaTeX and parser delimiters, with surrounding text and ordinary editor/code copying preserved. Independently reviewed signed union `837962af` preserves exact feature/helper `1902131` and actual main `05ba353`. Fresh combined checks passed 501 web tests (29 copy cases), 195 focused Python cases, 24 post-build math checks, both builds and all locked/static/license/protocol checks. Full Python 9,749/43/two warnings is explicitly reused from unchanged backend `123b2e6` plus fresh #90/#91 CI; the new acceptance helper is separately reviewed and preflighted. Root-owned [browser acceptance](../.agents/docs/agent-reports/workspace-copy-latex-2026-10-12.md) passed 19 native Copy→paste cases on the exact build, with unchanged ranges, ordinary code/editor behavior and source revision 1 retained. Complete cleanup passed at 18:38:03.217625 UTC. PR #92 merged at `b2f2e62` after all four fresh CI checks passed on signed `d547d500` (run `38292786944`). No backend, dependency or content-policy change.

The [GUI distribution](WORKSPACE.md#building-and-running) now includes checked
notices for actual bundled npm modules/runtime helpers, embedded-component notices, the existing unmodified
KaTeX fonts/glyph-derived SVG assets and project MIT code, with deterministic file coverage. Missing,
changed or uncovered inputs fail the build. This closes the notice-shipping
prerequisite locally: independently reviewed signed `1032cd4` passed fresh 472 web
tests, 24 math checks and all required checks/builds; unchanged full Python 9,749/43
is explicitly reused. Root-owned [HTTP acceptance](../.agents/docs/agent-reports/workspace-license-notices-2026-10-12.md) served all 70 files exactly with complete teardown. PR #91 merged at `05ba353` after all fresh checks passed (run `38290115991`).
It adds no dependency or platform-code license allowance. The narrow
[isolated math distribution](MATH-CHECKS.md#build-and-configure) follow-on reuses
those pinned primary/embedded inputs for code-only KaTeX MIT, React Apache and
SVG OFL attribution, without fonts/CSS/Svelte runtime. Six-file verification
preserves the existing renderer/launcher bytes and filenames; 486 web/41 notice/24 math cases and required checks/builds pass, with explicit full Python baseline reuse. Independent review and [six-file acceptance](../.agents/docs/agent-reports/math-license-notices-2026-10-12.md) pass. Signed union `fe19020` integrates actual main `b2f2e62` (PR #92); fresh combined 515 web tests/check/build pass with all 70 GUI files exactly matching copy acceptance. Unchanged math/build/24 and backend evidence are explicitly reused. Exact combined review and fresh PR CI/merge remain pending.

The narrow [failed-run record slice](WORKSPACE.md#run-records) retains partial loop observations and fixed failure phases while preserving error exit status, cleanup and delivered-only save-before-ACK. Failed records carry no final answer and cannot finalize staged ignored-math outcomes. Independently reviewed signed source `123b2e6` passed all final gates: 9,749 Python tests (43 opt-in skips, two existing warnings), 445 web tests and 24 post-build math checks. Root-owned [CLI/browser acceptance](../.agents/docs/agent-reports/failed-run-records-2026-10-12.md) and complete cleanup passed; PR #90 merged at `c6ccce6` after all fresh Python 3.12/3.13/web and security checks passed (run `38287920059`). Cancellation propagates through cleanup failures without save or ACK. Cancellation/process-death persistence, live updates and session-end handling remain open.

Document synchronization now has separate [worker catalog](WORKER.md#document-catalog-prerequisite)
and [control publication outbox](LIVING-DOCUMENTS.md#publication-outbox-prerequisite)
prerequisites. The latter records canonical changes and source events in one transaction
and exports complete pinned targets, with snapshot recovery across retention/bootstrap
gaps. A separate internal manual delivery journal now pins transfers and reconciles
exact storage acknowledgements through bounded worker calls. It supplies no scheduler
or execution hook. Writer-state safety, direct-edit capture and `ss-doc` still require
implementation and real OpenShell acceptance before environment publication is enabled.

The internal [worker source adapter](WORKER-DOCUMENT-SOURCE.md) now converts trusted
immutable captures to named patches or exact retained refusals, with explicit delegated
provenance and atomic quotas/replay receipts. Its local result never grants restoration;
authenticated capture ingress, writer proof and transport acknowledgement remain open.

Use the shared artifact/revision/anchor contracts for living documents, comments, sources and ledger entries. Keep open questions/issues, scientific findings, results and frontier candidates as typed entries in one project ledger; board/frontier/history are views. Link review findings without merging their resolution lifecycle into scientific result status.

The [immutable artifact inventory prerequisite](ARTIFACTS.md) retains explicit
harness publications with stable scoped path IDs, immutable exact bytes/revisions,
trusted publishing attribution, bounded transfer and durable uncertain-outcome
holds. Internal pinned/latest reads and atomic events are the consumer boundary.
The bounded explicit-publication reviewer consumer uses durable atomic feed/cursors,
exact raw UTF-8 source copies and scoped pinned finding/fix grants. Unsupported content
remains incomplete; no parser or worker authority is added. Independent review, final
combined local gates and [browser/API acceptance](../.agents/docs/agent-reports/artifact-review-2026-10-12.md)
passed; PR #84 merged after all three fresh CI checks passed. Automatic execution-output
inventory remains open, alongside imports, observed-input lineage, retention/deletion
workflows and rich PDF/GUI consumers.
Actual producer remains unknown; a worker last-writer hint is not provenance proof.

Before automatic reported-output capture, complete the [bounded worker capture
prerequisite](ARTIFACTS.md#bounded-worker-capture-prerequisite): fixed sandbox helper,
size/growth/hash limits, owned spawn/reap/blob I/O and durable unknown-cleanup
admission. Independent review cleared signed source `cbcfc7e`; final-source local
gates passed 60 capture/lifecycle cases, 9,592 Python/43 skips, 436 web/24 post-build
math checks and all required checks/builds. Independent review cleared signed combined
freeze `b535856`; root-owned [OpenShell Lab22](../.agents/docs/agent-reports/babbage-openshell-lab-22-2026-10-12.md)
passed exact binary/empty retention, prompt regular/sparse/symlink/FIFO refusals and
healthy publication/read/execution access on Python 3.14.8. Zero capture leases/helpers
and complete teardown were independently verified. Actual main PR #85 ancestry and
acceptance docs change no product/test/configuration/helper bytes. PR #86 merged at `9c07725` after all three fresh CI checks passed. This adds no automatic discovery.
The next [worker-local terminal observation prerequisite](ARTIFACTS.md#worker-local-terminal-observations)
is implemented and independently reviewed at signed `35863776`: bounded raw-byte/root
stat sidecars, original pre-dispatch request/attempt/incarnation/known immutable VM
pins, atomic terminal detail/gaps and scoped internal replay/retention. Derived
invalid/quota/corrupt state preserves healthy execution completion and canonical
recovery. Integrated gates passed 9,714 Python tests (43 opt-in skips), 441 web tests,
24 post-build math cases and all required checks/builds. Root-owned
[OpenShell Lab24](../.agents/docs/agent-reports/babbage-openshell-lab-24-2026-10-12.md)
passed partial outputs, raw identities, gaps and same-attempt/kernel continuity
through worker restart; complete teardown was verified. PDF was unavailable.
PR #88 merged at `744d1b5` after all fresh CI/security checks passed. No public feed, capture, consumer,
scheduler or artifact revision is enabled.
Every report remains mutation-incomplete; links/specials are unavailable and
`not_seen_after` is not deletion. Current `written_files` stat differences cannot prove
all writes, producer identity, inputs or attempt-end source bytes; RA-5/RA-F2/CR-F6
remain open, including project ignore patterns. Next design one-shot bound capture
intent/status/receipts and authenticated scoped transport before automatic fan-out;
complete filesystem telemetry/snapshotting remains a stronger separate requirement.

Support attributed user edits and revision-checked agent patches; demonstrate stale-patch reconciliation and orphaned anchors. Use one TeX-preserving renderer across research views. Evaluate PDF.js navigation against exact page/region anchors while retaining worker extraction/rendering. Assumption changes should identify dependent claims/reviews for reconsideration, not silently rewrite conclusions.

The worker document-catalog prerequisite is implemented, but runtime document publication, direct-edit integration and `ss-doc` remain open (LD-49 to LD-52). Before enabling replacement/removal, complete the [protected-filesystem feasibility prerequisite](../.agents/docs/agent-reports/document-writer-guard-2026-10-12.md): prove arbitrary writable handle/mmap lifetime, namespace exclusion, exact capture/acknowledgement revalidation, bounded admission and monitor/controller failure/restart behavior under OpenShell. FUSE is a proposed boundary requiring implementation and real VM acceptance, not a selected or validated capability. Hold uncertain writer state; helper-only locks and ordinary VM recycling do not satisfy the requirement while preserving healthy kernels. Continue independent control outbox/source adapter/helper-contract work without claiming a runtime mirror.

The independently implemented [automatic reviewer](CONSISTENCY-REVIEW.md) now provides checkpoint/terminal triggers, a separately authored rubric (D56), exact evidence revisions, attributed responses, claimed fixes and confirmation/supersession outcomes. Independent adversarial review and [browser acceptance](../.agents/docs/agent-reports/consistency-review-2026-10-12.md) passed. Remaining work includes automatic execution-output inventory/reviewer ingestion, live final-message transport, idle scheduling and shared monetary accounting. [Document math checks](MATH-CHECKS.md) now connect atomic save targets, immutable bounded checks, exact historical ownership, mechanical warn findings and closing-only delivery finalized after durable run save. Independent review and [browser acceptance](../.agents/docs/agent-reports/document-math-2026-10-12.md) pass; comments, replies, ledger fields and finding prose remain outside this document-only D82 slice. Drive triggers from durable events, including published shell-created files. Published frontier results enter from their citations without reproduction gates.

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

The M1 exit run passed on babbage (lab 9); provisioning's runtime checks passed in lab 18. Background jobs passed lab 19, including harness and browser checks; executor recovery passed lab 20, including the corrected daemon-loss lifecycle fence. Document editing, comments/work requests and automatic consistency review passed independent review and browser acceptance. Single-MAIN planning/live progress and MAIN/watcher/reviewer prompt provenance are delivered. Original research framing and general small-scale-trial guidance are delivered as PR #106. Current W4 repair input descriptor is delivered as PR #107 after independent review, full local gates and all four fresh CI checks; durable project repair call/spend accounting remains open. USER plan-body editing backend is separately under implementation. USER plan editing/approval, delegation, kernel notice delivery, protected export staging/conversion and the ledger remain separate, in the maintained order in [.agents/handoff.md](../.agents/handoff.md). Protected-filesystem feasibility is still required before runtime document replacement/removal; LD-49 and W3 package restoration remain open. The M2 exit run still needs a real model gateway and connector authorization from the user. Keep the backlog and runtime evidence beside each completed slice; draft-spec questions do not block the already-decided infrastructure work.
