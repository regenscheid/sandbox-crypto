# Claude Science research-support feature investigation

Date: 2026-10-10

Status: completed source-inspection working report. This records observations and recommendations from the investigation, not final project decisions. Later confirmed requirements and decisions are in the current sandbox-science planning documents.

## Scope and evidence limits

The investigation examined existing local source and extracted distribution assets read-only. No Claude Science runtime, analysis kernel, provider, container, or service was executed, and no software was downloaded or installed. Source presence does not prove a feature works in the current installation or that a setting is active.

The working CScience checkout was identified as `dev`, HEAD `063919ffab886e6eb70f12dfda8168fec44d9d1a`, package version `0.8.0`, pinning upstream Claude Science `0.1.60`. The primary inspected upstream source was:

`/home/andy/Git_Projects/cscience-crypto/dist/review-0.1.60/runtime/claude-science.js`

Supporting assets were under:

`/home/andy/Git_Projects/cscience-crypto/dist/review-0.1.60/runtime/assets/`

Below, **R** refers to the primary upstream JavaScript source and **A** to that assets directory. Its bundled JavaScript contains long minified lines; line anchors identify the relevant inspected region, while named functions and strings help locate the exact implementation. The older `dist/docker-linux-x64/runtime/cscience-runtime.js` distribution was not used as the source for current upstream feature claims. The GitHub-Actions-only inspection helper was not run or bypassed.

User requirements informing this investigation were preservation of useful CScience skills and automatic reviews; automatic Conda provisioning inside isolation; a persistent project execution container with optional dedicated session containers and same-project data sharing; independently placeable GUI/orchestrator/workers; trusted contextual traffic monitoring; first-class LaTeX, SageMath and PDF research; editable living documents; and a project research board or ledger.

## Feature inventory

### Skills and research organization

R:1550 documents skill discovery followed by exact-name loading, generated references for connected MCP methods, scoped agent/skill management, reusable procedures and specialist profiles. A skill may carry `kernel.py` helpers that are automatically injected into the analysis kernel. The source distinguishes procedure-oriented skills from role-oriented specialist profiles.

`A/skills/literature-review/SKILL.md:33–43` describes retrieval before citation, citation verification, backward and forward citation-graph expansion, and retraction checks. Lines 45–67 emphasize synthesis and calibrated confidence rather than adjacent summaries or fabricated references. These are useful workflow principles for cryptography literature analysis, with domain-specific source services and evidence standards.

Recommendation: preserve skills as versioned workflow packages with declared helper code, tool requirements, worker capabilities and provenance. Record which skill version and auto-injected helper code contributed to a result. Skills should guide research methods without silently broadening execution authority.

### Concrete plans and delegation

R:27876–27886 defines plans with feasibility assessment, desired outputs, ordered phases/steps and independent delegation tracks. Plans are stored as artifacts; changing the current plan appends a version of that plan rather than silently replacing its history. Step progress can associate a named delegate with a track.

Recommendation: retain planning and progress in the orchestrator and project metadata. Worker placement should remain transparent to the research plan. A logical delegated agent need not imply a new execution container; the project/session execution context determines placement.

### Automatic scientific consistency review

R:27138–27173 defines a dedicated REVIEWER profile. Its job is to trace claims to transcripts, execution logs and artifacts. It excludes Python/Bash/R analysis execution, editing, package installation, web search and further delegation. Its instructions explicitly distinguish trace checking from recomputation and require contradictory evidence rather than treating an unavailable earlier value as proof of fabrication.

R:27242–27308 includes checks for materially misleading claims, claimed actions that did not happen, values contradicting tool output, source misrepresentation, forged source pointers, contradictory artifact captions and unsound methods for the stated claim. The rubric weights durable artifacts more strictly than incidental chat wording.

Recommendation: preserve this lightweight consistency reviewer. Add separate mathematical verification and computational reproduction review types. The built-in transcript reviewer is not proof certification or independent reproduction. A cryptographic claim should identify whether its support is a proof, exact finite verification, bounded search, numerical experiment or inference.

### Review triggers, confirmation and recovery

R:666 contains configurable source defaults, including a 120-second minimum checkpoint interval, artifact delta of three, an authored-input threshold of 2,000 characters, a structural-block trigger, and a kernel lane of 40 cells with a 30-minute interval. These are source defaults, not verified live configuration. R:29323 implements checkpoint predicates. R:29330 performs terminal-review selection using artifacts, tool activity, existing unresolved findings or a closing-claim classifier.

R:28236 implements detached checkpoint dispatch, pending-window merging, optional hold periods, watermark tracking, dropped-reviewer reconciliation and bounded recovery. These mechanisms show that reviewer execution and main-agent execution need coordinated lifecycle handling.

R:10348–10419 documents the findings interface. Findings retain verdict, severity, evidence, reviewer identity/model/kind, transcript or artifact-version anchor, rebuttal, resolution and reflag count. Background fail-level reviews may interrupt the agent; background warnings are retrieved explicitly. Unaddressed findings remain unfixed even when a review loop stops resurfacing them. Branch scoping matters when sessions fork.

R:10454–10502 documents agent self-reported fixes. A self-report moves a finding to `claimed`, pending reviewer confirmation, rather than directly to `resolved`. It includes a required explanation visible to the user. Delegates cannot unilaterally perform the root session's confirmation transition.

Supporting schema:

- `A/drizzle/sqlite/0029_verification.sql:1–43`: session claims, verification checks, verdicts, evidence, reviewer provenance, rebuttal and lifecycle.
- `A/drizzle/sqlite/0115_verification_checks_resolution.sql:1–30`: resolution metadata and the `open`, `claimed`, `resolved`, `unaddressed` states.

Recommendation: make reviews consume durable worker events, exact artifact versions and provenance. Preserve incomplete, inconclusive, failed and pending-review outcomes rather than flattening them into success. Reviewer restarts and dropped windows need visible recovery semantics.

Improvement to consider: R:10415–10418 and 10494–10497 describe automatic closure of findings against superseded artifact versions when a new version is saved. The new design should distinguish a finding being superseded from a reviewer independently confirming the issue is fixed.

### Living documents, user edits and comments

Editable Markdown and LaTeX previews exist at:

- `A/web-dist/assets/EditableMarkdownPreview-DXJyNKyx.js:1`
- `A/web-dist/assets/EditableLatexPreview-BQmO7dnr.js:1`

R:1990 creates attributed user-edit artifact versions. R:31042 implements annotation create/read/update/delete routes. R:299–300 defines point, text-selection, screenshot and HTML-element anchors, including page numbers, selected text, prefix context and multipage PDF segments. `A/drizzle/sqlite/0085_annotations_unified.sql:1–14` stores project ownership, target identity and content checksum.

R:29196 shows wait notifications waking agents for user edits and directing them to reread the changed artifact before writing again. The living-document behavior therefore has direct Science source evidence; it need not be assumed to originate exclusively in a Claude Desktop MCP service.

Recommendation: support project-owned document identities, revision histories, comments, anchored feedback and agent notifications. Preserve the distinction between a human edit and an agent-produced revision. Comments and source-document anchors should survive worker/container replacement.

### Document concurrency and references

R:26015 emits stale-base warnings after saving when a newer version, including a user edit, appeared after the agent's base version. The source warns about potentially omitted changes, but the save has already produced a new version. This is not evidence of optimistic concurrency rejection or automatic merge.

R:27182–27219 distinguishes immutable version references from artifact references that resolve to the latest version. Live references let a document show a regenerated figure without rewriting every reference. Reviewed claims still need exact version anchors.

Recommendation: improve warning-only stale saves with optimistic concurrency and explicit merge/fork behavior. Living documents may resolve selected references to the latest figure, while claims and reviews pin the exact reviewed version. Do not let a stale worker write silently obscure a newer human edit.

### Shared knowledge, artifacts and provenance

`A/drizzle/sqlite/0026_memory.sql:1–21` stores project/artifact/version/session scope, source frame, evidence category and supersession. `0052_memory_categories.sql:1–18` adds user-defined categories and auto-recall guidance. R:27886 exposes stated/observed/inferred evidence and session-private scratchpads, with project scope as the default for new shared facts. R:29299 verifies memory entity ownership and scope.

`A/drizzle/sqlite/0000_yummy_ken_ellis.sql:36–111` defines artifact dependencies and versions with checksums, parent versions, extracted code, lineage, environment snapshots and project ownership. `0004_content_snapshots.sql:1–9` adds content-addressed snapshot references. `0005_execution_log.sql:1–21` records cell source, kernel, environment, stdout/stderr, exit status and files written, and ties artifact versions to producing cells. `0129_cell_source_log.sql:1–11` stores additional cell-source records.

Recommendation: store project knowledge, claims, review records, annotations and document history in durable trusted services outside the execution containers. Add worker identity, base-image digest, environment revision, skill revision and input identities to result provenance. Keep session-private notes distinct from shared project knowledge, and explicitly record uncertainty and supersession.

### Research board: implemented substrate and limits

Existing substrate includes session claims, verification checks, project notes, project artifacts, memory and project-scoped metadata queries. Notes are defined in `A/drizzle/sqlite/0000_yummy_ken_ellis.sql:256–274`. R:5419–5499 exposes read-only SQL with project scope by default, a bounded result surface and credential-table restrictions.

However, R:29311 explicitly disables `render_dashboard` and `patch_dashboard` pending sandbox hardening. Their names in a routing table are not proof of an available working dashboard. No finished native research board was verified by this investigation.

Recommendation: build a native project research board over questions, hypotheses, claims, experiments, sources, decisions, contradictions and reviewer findings. Link each item to sessions, documents and exact evidence. This is a meaningful extension beyond the inspected substrate. Shared project access should not require every session to share a process or writable scratch directory.

### PDF parsing and navigation

`A/skills/pdf-explore/SKILL.md` provides:

- Lines 21–40: persistent parsed pages, outline, semantic scan, structured extraction, whole-document mapping and image rendering helpers.
- Lines 49–90: cached text and outline-led navigation.
- Lines 123–145: artifact-version identity resolution and high-resolution figure cropping.
- Lines 168–245: exhaustive structured extraction, page provenance and scanned-document fallback.

The helper uses cached parsing and a configurable model-assisted extraction path rather than repeatedly placing entire PDFs in context. `A/web-dist/assets/PdfPreview-DFxwvBPW.js:1` contains selection comments and annotation UI.

Recommendation: perform PDF parsing, rendering and extraction in workers, while storing durable document identities, parse outputs and page anchors at project level. Cache by source checksum and parser/version settings. Expose text and images together so formulas, matrices, tables and diagrams can be inspected visually. The inspected source does not establish mathematical OCR/extraction accuracy; that needs a dedicated evaluation corpus.

### LaTeX, SageMath and provisioning

The upstream editable LaTeX preview imports KaTeX. This is evidence of a GUI rendering surface, not proof of a complete TeX compilation toolchain. Current CScience additions include:

- `/home/andy/Git_Projects/cscience-crypto/src/patches.mjs:1567–1595`: exact-pinned default PDF toolkit provisioning.
- `/home/andy/Git_Projects/cscience-crypto/pkg/platform/lib/profiles.mjs:67–148`: document-analysis, crypto-scientific, lattice and Sage profiles. The Sage profile prefers an existing host executable, then offers managed installation; this is not evidence of a dedicated upstream Sage kernel.
- `/home/andy/Git_Projects/cscience-crypto/src/web/cscience-math.js:739–755`: visibly labels repaired math and preserves original LaTeX in fallback output.

R:25751 exposes environment creation/registration and package installation/uninstallation. R:1550 documents per-environment persistent kernels, Conda provisioning, separate Python/R namespaces and ephemeral session overlays. A project's persistent filesystem/environment and a session's live in-memory kernel are different lifecycles.

Recommendation: promote Sage to an explicit worker kernel/capability with functional probes inside the actual sandbox. Preserve source TeX, mark repairs, and support math throughout chat, documents, board items and exports. Put Conda installs, skill helpers, PDF tooling and TeX compilation inside the project's worker context. Automatically managed installation remains a product requirement, with its effects recorded and isolated to the intended environment.

## Worker-boundary precedents

R:1550 separates the stdlib-only control `repl` from persistent analysis kernels. MCP and compute operations run through the control plane; analysis preparation and processing occur in Python/R. Their current filesystem handoff model assumes a shared workspace even when process memory is separate.

`A/skills/remote-compute-ssh/SKILL.md:19–32, 73–99, 127–132` describes staged inputs, asynchronous submit/wait/harvest, durable job identities and reattachment after kernel loss. `A/skills/compute-env-setup/SKILL.md:19–25` explicitly describes SSH, scheduler clusters, container bridge runners and native BYOC adapters. Lines 29–46 distinguish portable environment requirements from provider-specific build and resolution mechanics. Lines 75–85 distinguish an import check from a functional execution witness and an agent-following-the-documented-workflow validation.

These are promising integration seams. They do not prove ordinary local Python/R/file/provisioning tools can all be redirected to persistent project containers. A feasibility spike must identify every execution path and ensure it uses the intended worker context, including helper injection, package management, PDF parsing and file manipulation. Remote worker handoffs should use project-scoped references or explicit transfers rather than assuming the orchestrator and worker share absolute host paths.

## Action monitoring versus scientific review

R:30152–30180 contains a separate auto-mode security classifier concerned with destructive/security-relevant actions, prompt injection, scope creep and accidental damage. R:31360 wires a classifier gate into approval processing, emits decision events and falls back to approval cards when classification is unavailable or the gate closes.

This is contextual action monitoring, distinct from the scientific REVIEWER. It is relevant to the user's proposed trusted monitor, but it does not establish packet-level or request/response-content inspection coverage. Preserve action context alongside a separately evaluated trusted network monitor, with deterministic isolation and policy enforcement underneath. Do not claim a model classifier can guarantee that all inappropriate behavior is detected.

## Recommended implementation sequence

1. Establish persistent project execution identities, optional session-specific execution identities, durable project storage, separate session kernel state and complete worker execution events.
2. Preserve skill-guided workflows, automatic environment provisioning and the existing consistency-review lifecycle across that boundary.
3. Implement project documents and a native research board, including human edits, comments, source anchors, concurrency protection and evidence links.
4. Treat Sage execution, PDF parsing/navigation and LaTeX rendering/export as mandatory first-class acceptance criteria.
5. Add separately scoped computational reproduction and mathematical verification reviews with evidence appropriate to cryptographic research.

The report records observed designs and recommendations only. It does not claim runtime parity, successful worker redirection, validated sandbox strength or deployment readiness. Current planning documents are the authority for later confirmed decisions.
