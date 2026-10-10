# Research features to incorporate

Date: 2026-10-10. Synthesis of three parallel source investigations: Claude Science, Claude Code/Desktop, and OpenScience Desktop. These are proposed features, not implemented capabilities. Evidence and source boundaries are in [source study](SOURCE-STUDY.md).

## Product direction

Reimplement CScience's research loop on an explicit execution boundary (D11). Its skills, plans, versioned artifacts, PDF exploration, editable documents, and automatic consistency reviews define a substantial behavior baseline; no Claude Science code, prompts or schemas are copied (D56). Add a project-wide research board, safer document concurrency and native Sage kernels while making all execution portable between local and Kubernetes workers. Independent reproduction reviews are a separately configured proposed extension.

Project containers, optional workbook containers, automatic Conda provisioning, trusted monitoring, and independently placeable GUI/orchestration/compute remain foundational. Research features must use these boundaries rather than introduce host-only execution paths.

New workbook containers are provisioned from the base image. Automatic reviews follow current Claude Science behavior. Monitor response is policy-selected blocking, permission escalation, or alerting while continuing; these choices are confirmed.

## Initial feature set

| Feature | Incorporation decision | Concrete acceptance evidence |
| --- | --- | --- |
| Work-organizing skills and plans | Preserve discovery/loading and research planning; register the existing external cryptography pack without embedding its content | The same pinned external skill/resources run through local and cluster workers, with real load traces and concrete plan/evidence records |
| Shared research board and ledger | Track findings, open issues, and best-known results of each type, alongside assumptions, experiments and decisions | Workbooks share stable entries/history and a frontier combining published and project results; published results require no independent verification |
| Living research documents | Preserve editable Markdown/LaTeX and annotations; add revision-checked patches, anchored discussions, suggestions, and user/agent attribution | A user edits an assumption while an agent works; the agent reconciles its patch, retains comments, and marks affected evidence/reviews for reconsideration |
| LaTeX throughout | Preserve original TeX in chat, notebooks, documents, comments, board entries, reviews, and exports | Equations, aligned derivations, and matrices remain editable/copyable; rendering errors are visible; Markdown/TeX/PDF export retains mathematical meaning |
| Native SageMath | Add an explicit Sage kernel/cell language and functional capability probe | Sage preparsing, finite fields, polynomial rings, exact matrices, LaTeX output, interruption, and restart work inside both worker placements |
| Navigable mathematical PDFs | Preserve Science's cached exploration and selection comments; integrate page/region citations with viewer navigation and project knowledge | A cited equation opens the exact document revision and page/region; extraction retains provenance; scanned/math-heavy pages can be inspected as crops |
| Automatic consistency reviews | Preserve Science's claim-to-transcript/artifact checks and checkpoint/terminal triggers | False execution claims and contradictory results produce revision-anchored findings; claimed fixes await confirmation; inconclusive/capped reviews remain visible |
| Independent verification reviews (proposed extension) | Evaluate a separate reproduction role using the common execution API and pinned inputs | If enabled, a reviewer reruns a Sage result without producing-kernel state and reports match/mismatch/unavailable, assumptions, and evidentiary limits |
| Provenance and reproducibility | Retain artifact lineage, execution logs, environment snapshots, and attributed human edits | Every reported result resolves to code/input/output revisions, actual worker/image/environment, skill version, and producing execution |
| Research connectors and MCP | Provide discoverable tools/resources/prompts with project scope, health, capability, and credential routing | Source retrieval can be remote; execution-bearing adapters run in governed workers; advertised capabilities match the selected environment |

All rows above inform the initial product design; the reproduction role remains a proposed extension. Implementation can proceed in stages, but Sage, PDF navigation, documents, project knowledge and Science-style consistency reviews must be exercised before declaring research parity. Reuse shared identity, artifact/revision, anchor, event, permission and environment components as detailed in the [implementation plan](IMPLEMENTATION-PLAN.md).

## Reviews need distinct roles

1. **Consistency review:** inspect the transcript, logs, sources already available, and exact artifact revisions. Preserve the existing lightweight reviewer without granting it computation by default.
2. **Mathematical verification/reproduction:** execute selected checks in a fresh environment, examine assumptions and domains, and seek counterexamples where appropriate. Independently validate candidate findings before treating them as established defects.
3. **Action/security monitoring:** assess whether actions fit the authorized task and correlate them with externally observed process/package/network activity. This is separate from scientific correctness review.

Each scientific review identifies what it actually checked. Exact calculation, bounded exhaustive testing, numerical observation, proof argument, source assertion, and synthetic illustration have different meanings. Agreement between agents is not proof. Simulated fallback data must never satisfy a measured-experiment or proof gate.

Published results can be recorded and used in the frontier with citations and applicable scope; independent checking is not required. The proposed verification/promotion gates concern newly generated project results, not a blanket audit of cited research.

Persist finding history and author responses. Distinguish open, claimed fixed, confirmed fixed, unaddressed, superseded, and inconclusive outcomes. A new artifact version may invalidate or supersede a finding; it does not independently establish that the problem was fixed. Review triggers and completion gates should consume durable worker/artifact events, including shell-created files, rather than rely solely on agent narration or GUI callbacks.

## Skills, prompts, and shared knowledge

Use public skill and reviewer prompts as workflow references. Separate stable harness policy, project instructions, versioned skills, specialist role prompts, current project knowledge, and session scratchpads. Record the effective versions used by a run. Load procedures when relevant and make their provenance/tool dependencies visible.

The existing external crypto-analysis-skills pack already supplies cryptographic procedures. Keep that content and its maintenance in its own repository; select the pack through a project profile and evaluate relevant workflows. The user can modify its host bindings for this platform, so define clean APIs rather than require exact CScience call/file compatibility. Support paper/evidence intake, exact algebra, parameter work, validation, counterexample search, reproducibility and mathematical writing through platform capabilities. See [external skills](EXTERNAL-SKILLS.md).

Shared memory is useful context; accepted claims and enforced policy need explicit records. A project board is a view over the same ledger used by documents and reviews. The external pack's existing board/knowledge protocol informs the semantics; its bindings can change to a new platform API, with deliberate import/export where needed and one authoritative history. See [research workspace](RESEARCH-WORKSPACE.md).

## Reuse choices

| Source | Preferred use | Boundary |
| --- | --- | --- |
| CScience / Claude Science 0.1.60 | Behavior reference and acceptance fixtures only (D03, D11) | Proprietary; no code, prompt text, skill text or schema is copied (D56) |
| Claude Code/Desktop public material | Skill loading, scoped roles, review validation, completion hooks, living-document interaction | Complete private Desktop/Code system prompts and the Docs backend were not established; MCP is an integration option, not a proven explanation of Docs |
| Newer OpenScience Desktop (MIT) | First code-reuse candidate for the reimplementation: review/result formats, provenance models, editor/math patterns, visible skill/connector inventory | Browser editing, kernel execution, and provenance still have native-desktop dependencies in the inspected paths |
| Andy's OpenScience fork | Execution contracts, permission receipts, replay/lifecycle failure scenarios, persistent-environment requirements | Its agent-serving path and phase-two persistent environment are incomplete; assess selected components rather than assume a ready worker backend |

Before copying code or bundled skill assets, resolve their actual per-file terms. The OSD catalog's license labels are not sufficient evidence for all bundled external skills. Conceptual workflow learning does not require copying those assets.

## First complete research workflow

Import a paper → anchor an equation/claim in the project ledger → run exact Sage work in workbook A → inspect its published result from a separately containerized workbook B → draft a LaTeX document → accept user edits/comments safely → run automatic consistency review → update shared findings and next questions. Run this path locally and with Kubernetes workers. Exercise fresh reproduction as a separate extension using the same execution/environment infrastructure.

Use a known finite-field/matrix fixture first, then a real cryptography paper. Record math transcription errors, reproduction success, document conflicts, worker recovery, and execution location. The [roadmap](ROADMAP.md) supplies implementation gates; no framework or database choice is required to make this feature set concrete.
