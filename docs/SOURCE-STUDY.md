# Source study: research workflows and reusable components

Inspected 2026-10-10 by three parallel investigators. Read-only source/documentation research; no runtimes, kernels, builds, or deployments were validated. This study informs the [feature plan](FEATURE-PLAN.md).

> **Update (2026-10-10, D11):** the user decided to reimplement the needed CScience behavior rather than adapt or patch the Claude Science runtime. Where this document speaks of retaining, adapting or patching CScience mechanisms, read it as *behavior evidence for the reimplementation*. No Claude Science code, prompts, schemas or assets are copied (D56). OpenScience Desktop upstream is MIT-licensed and remains a code-reuse candidate, subject to per-file checks.

## Evidence baseline

| Source | Inspected revision / access | Scope |
| --- | --- | --- |
| CScience checkout | `063919ffab886e6eb70f12dfda8168fec44d9d1a`, package 0.8.0, upstream 0.1.60 | Local patches, capabilities, math support, provisioning |
| Extracted Claude Science | `~/Git_Projects/cscience-crypto/dist/review-0.1.60/runtime/` | Existing extracted runtime, schemas, bundled skill text, frontend assets; no extraction executed |
| Claude Code public plugin source | `anthropics/claude-code` at `2301018b1f61073c501a8e7a4813ef48c239163b` | Public review command prompt |
| Anthropic public skills | `anthropics/skills` at `dbd4588f9e1033efb41dad4bef2f7947c8993d44` | Coauthoring, skill evaluation, document/PDF examples |
| OpenScience Desktop upstream | `ai4s-research/open-science` at `aa7f27667c5f4fffdfa583066beed5214fc03e2a` | Project/session, skills, review, editor, kernel, PDF, provenance, SDK/MCP paths |
| Andy's earlier fork | `regenscheid/open-science-crypto` at `a7e39ea09cac2b9ef29e10e463de6d70ab82526b` | Execution module, lifecycle contracts, persistent-environment RFC |
| Existing external crypto skills | `regenscheid/crypto-analysis-skills` at `4089e99415f349dde406c8e57a227715002ee166` | Board/knowledge protocol, host adapter, capability mapping and completion/load contracts; content stays external |
| Official Claude documentation | Retrieved 2026-10-10; mutable online documentation | Docs, skills, roles, hooks, memory, MCP, published system prompts |

Local line references below refer to the inspected files. For Claude Science, `R` means `claude-science.js` under the extracted runtime, and `A` means its `assets/` directory. These assets were examined as evidence and were not copied into this project.

## Claude Science: preserve a substantial research baseline

| Observed capability | Source anchors | Incorporation / limit |
| --- | --- | --- |
| Skill discovery/loading, helper injection and research plans | R:1550, R:27876–27886; `A/skills/literature-review/SKILL.md:33–43` | Preserve staged work, concrete outputs, citation verification and reusable methods; route helpers through workers |
| Consistency reviewer | R:27138–27173, R:27242–27308 | Checks claims against transcript/log/artifact evidence; excludes computation, editing, installs and web search. Add a separate reproduction role |
| Checkpoint/terminal scheduling | R:666, R:29323, R:29330 | Inspected defaults include 120-second minimum checkpoint interval, artifact/input/structure triggers and kernel-lane thresholds; not evidence of active settings |
| Findings and confirmation lifecycle | R:10348–10419, R:10454–10502; `A/drizzle/sqlite/0029_verification.sql:1–43` | Preserve verdicts, evidence, rebuttal and claimed-fix confirmation. Existing superseded-version closure should not become proof of correction |
| Editable Markdown/LaTeX and annotations | `A/web-dist/assets/EditableMarkdownPreview-DXJyNKyx.js:1`, `EditableLatexPreview-BQmO7dnr.js:1`; R:1990, R:299–300, R:31042 | User-attributed versions and multipage/selection anchors already exist; useful baseline for living documents |
| User-edit notifications and stale-save warnings | R:29196, R:26015, R:27182–27219 | Agents are told to reread; stale saves still create versions. Add revision checks and reconciliation; pin evidence/reviews while allowing explicit latest-version presentation references |
| Project knowledge and lineage | `A/drizzle/sqlite/0026_memory.sql:1–21`, `0000_yummy_ken_ellis.sql:36–111`, `0005_execution_log.sql:1–21`; R:27886 | Preserve scope, stated/observed/inferred evidence, supersession, producing cells, dependency/environment snapshots |
| Board substrate, dashboard disabled | R:5419–5499, R:29311; verification schema above | Project-scoped metadata/claims/notes exist; dashboard rendering/patching is explicitly disabled pending hardening. A finished project board was not verified |
| PDF exploration/navigation | `A/skills/pdf-explore/SKILL.md:21–40,49–90,123–145,168–245`; `A/web-dist/assets/PdfPreview-DFxwvBPW.js:1` | Cached pages, outline, ranked discovery, exhaustive extraction, figure crops, scan fallback and selection comments; evaluate math fidelity separately |
| Control/analysis split and remote compute | R:1550; `A/skills/remote-compute-ssh/SKILL.md:19–32,73–99,127–132`; `compute-env-setup/SKILL.md:19–25` | Promising seams for asynchronous jobs/transfer/recovery; ordinary local kernels and tools still need execution-path tracing |
| Action-level security classifier | R:30152–30180, R:31360 | Separate approval classifier for damaging or unauthorized actions, with fallback on unavailable checks; does not establish network-content inspection |

CScience's current crypto/lattice/Sage profiles are in `pkg/platform/lib/profiles.mjs:67–148`; PDF provisioning is in `src/patches.mjs:1567–1595`. A host-preferred Sage profile is not a native upstream Sage kernel. `src/web/cscience-math.js:739–755` preserves original LaTeX and labels repairs. These are useful worker capability and GUI regression references.

## Claude Code and Desktop: public workflow mechanisms

[Claude Docs](https://support.claude.com/en/articles/16923645-get-started-with-claude-docs) is the likely living-document feature described by the user: direct and concurrent editing, anchored comments, agent requests in threads, attributed changes and requester-scoped permissions. Its public guide does not establish a specific MCP backend or mathematical round-trip fidelity.

| Public mechanism | Primary evidence | Incorporation |
| --- | --- | --- |
| Deferred skill loading and supporting resources | [Skills documentation](https://code.claude.com/docs/en/skills) | Versioned portable packages; distinguish standard fields from Code extensions |
| Scoped specialist roles | [Subagents](https://code.claude.com/docs/en/sub-agents) | Separate context/tool grants and role prompts from container allocation |
| Independent review then candidate validation | [Pinned review command](https://github.com/anthropics/claude-code/blob/2301018b1f61073c501a8e7a4813ef48c239163b/plugins/code-review/commands/code-review.md) | Scientific rubrics, exact source locations and independent checks of findings; README numeric-confidence description drifts from command behavior |
| Enforceable lifecycle conditions | [Hooks](https://code.claude.com/docs/en/hooks) | Harness completion gates with bounded retries; prompt instructions alone do not enforce evidence requirements |
| Instructions versus learned context | [Memory](https://code.claude.com/docs/en/memory) | Keep project policy, accepted knowledge, claim records and transient notes distinct |
| Shared task ownership/dependencies | [Experimental agent teams](https://code.claude.com/docs/en/agent-teams) | Persistent project board spanning sessions; avoid tying authoritative task state to worker lifetime |
| Tools, resources, prompts and connection state | [MCP](https://code.claude.com/docs/en/mcp) | Project-scoped capability registry, health and transport/auth routing |
| Fresh-reader document checks | [Pinned coauthoring skill](https://github.com/anthropics/skills/blob/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/doc-coauthoring/SKILL.md) | Check missing assumptions and ambiguous notation without author-conversation context |
| Baseline and activation evaluation | [Pinned skill-creator](https://github.com/anthropics/skills/blob/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/skill-creator/SKILL.md) | Test outcomes, cost and correct/incorrect activation of research procedures |

Anthropic's [published system-prompt archive](https://platform.claude.com/docs/en/release-notes/system-prompts/overview) covers core web/mobile prompts; it is not a complete Desktop/Code harness specification. Public skill/plugin/role prompts offer concrete evidence. Complete private prompts were not established and third-party leaked collections were excluded.

## OpenScience Desktop: selective salvage and substantive gaps

Paths below link to pinned public upstream. The user's older fork is assessed separately.

| Implemented source behavior | Evidence | Reuse boundary |
| --- | --- | --- |
| Project folders shared by sessions | [project.rs](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/crates/osd-core/src/project.rs) | Useful ownership model; assign durable IDs/storage independently of host folder paths |
| Visible loaded skills and origins | [SkillsPage.tsx](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/app/routes/SkillsPage.tsx) | Preserve provenance and scope; some core skill folders are placeholders |
| Constrained checkpoint reviewer | [autoReview.ts](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/autoReview.ts), [reviewer.md](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/opencode-profile/agent/reviewer.md) | Opt-in conversation fork, changed-path findings, loop avoidance. Bash excluded from change detection; conversation fork does not establish immutable file snapshots |
| Editor/autosave/manual-edit provenance | [FilePreviewInspector.tsx](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/inspector/FilePreviewInspector.tsx):135–145,248–307 | Editing requires Tauri and excludes gateway web; browser documents/comments need a service |
| KaTeX Markdown rendering | [MarkdownViewer.tsx](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/markdown-viewer/MarkdownViewer.tsx):72–102 | Reuse rendering/fallback patterns throughout mathematical UI |
| Python/R native kernels | [kernel.ts](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/kernel.ts) | Browser path returns no kernel; no demonstrated Sage implementation |
| PDF preview and text traceability | [PdfInspector.tsx](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/inspector/PdfInspector.tsx):36, [pdf_extract.py](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/skills/core/traceability-review/pdf_extract.py) | Real files use native iframe preview; inspector is an HTML facsimile. Flattened extraction loses page/region anchors |
| Append-only artifacts/runs and environment facts | [provenance.rs](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/crates/osd-core/src/provenance.rs), [runs.ts](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/runs.ts) | Strong domain model; frontend recording returns early outside Tauri, so move recording to durable services |
| Knowledge/notes scaffolding | [harness README](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/harness/README.md), [harness.rs](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/crates/osd-core/src/harness.rs) | Useful current-knowledge/daily-note distinction; seeds files, not an orchestration engine or completed project board |
| Runtime capability/MCP surfaces | [SDK runtime](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/packages/sdk/src/runtime.ts), [AcpRuntime.ts](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/packages/sdk/src/acp/AcpRuntime.ts) | Useful interfaces; concrete provider/MCP transports still constrain adapters |

The fork's [execution module](https://github.com/regenscheid/open-science-crypto/blob/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/apps/desktop/src-tauri/src/execution/mod.rs):49–54 leaves serving agent requests and rebuilding authority as later work. Its [persistent-project RFC](https://github.com/regenscheid/open-science-crypto/blob/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/docs/rfc/secure-workbook-execution.md):330–365 is a phase-two proposal. Salvage typed execution/events, permission receipts, lifecycle journals, replay/ACK and cleanup ownership as design/test material; do not claim a completed OCI/Kubernetes backend.

## Asset and evidence handling

The user's existing external pack was reviewed separately for [platform requirements](EXTERNAL-SKILLS.md). It already supplies project board/knowledge workflows and loading/host contracts; compatibility does not require embedding its methods in this repository. Its historical runtime observations must be rechecked against the chosen harness.

The pinned [ai4s-skills](https://github.com/ai4s-research/ai4s-skills/tree/2519e8d11fd49429fb41d39c2f35b7d453814b11) pack is MIT and offers data/figure contracts. Its experiment suite also permits plausible seeded simulated fallback data. The new workbench must label synthetic illustrations explicitly and keep them out of empirical/proof completion gates.

OSD labels bundled Anthropic office skills as Apache-2.0, but the [actual pinned PDF license](https://github.com/anthropics/skills/blob/9d2f1ae187231d8199c64b5b762e1bdf2244733d/skills/pdf/LICENSE.txt) contains Anthropic service restrictions. The current [Anthropic repository](https://github.com/anthropics/skills) also distinguishes source-available document skills from open-source examples. Verify per-file terms before redistribution; this study copied no runtime code or skill packs.

Source inspection establishes behavior in these revisions, not successful operation under the new architecture. Kernel relocation, document concurrency, mathematical PDF fidelity, monitor coverage and Kubernetes persistence still require the [roadmap](ROADMAP.md) tests.
