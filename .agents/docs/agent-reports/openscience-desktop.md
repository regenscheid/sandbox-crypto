# Open Science Desktop and earlier fork: research-support feature inspection

Date: 2026-10-10.

This is a dated source-inspection working report. It records evidence, recommendations, and limitations from the parallel investigation. It does not supersede later confirmed project decisions in the current planning documents.

## Scope and evidence

Inspected repository source read-only through GitHub:

- Upstream `ai4s-research/open-science`, commit `aa7f27667c5f4fffdfa583066beed5214fc03e2a`, dated 2026-10-08; release/package line 0.6.0.
- Andy's earlier fork `regenscheid/open-science-crypto`, main commit `a7e39ea09cac2b9ef29e10e463de6d70ab82526b`, dated 2026-08-26; package 0.4.0, Tauri/Rust/React/OpenCode base.
- Upstream's pinned `ai4s-research/ai4s-skills` pack at `2519e8d11fd49429fb41d39c2f35b7d453814b11`.
- The actual PDF skill license from upstream's pinned Anthropic office skill revision `9d2f1ae187231d8199c64b5b762e1bdf2244733d`.

No builds, installations, runtime launches, deployments, tests, or authentication changes were performed. Implemented below means visible source implementation, not live verification. Older design documents contain historical and planned features; source and explicit status statements take precedence.

The target requirements informing recommendations were CScience's functional research baseline; independently placed GUI, orchestrator, and workers; persistent project execution environments with optional separate session/workbook environments; shared project data; Conda provisioning; automatic reviews; trusted monitoring; first-class LaTeX, SageMath, and PDF workflows; project-wide research board/ledger; and living documents accepting user edits and comments.

## Verified features and incorporation recommendations

| Area | Verified source behavior | Incorporation recommendation |
| --- | --- | --- |
| Projects and sessions | Projects are named workspace folders. Multiple sessions share a project by their `directory`. Project import supports managed copy or in-place references. | Preserve the project/session distinction and project-shared data. Use stable project IDs and durable storage, with execution assigned independently to a project container or optional session container. |
| Skills | Core, pinned external, and user/project skill layers. The Skills page lists skills actually loaded by the runtime and identifies their source. Some advertised core directories are only `.gitkeep` placeholders. | Curated, versioned skill packs with visible origin, dependencies, scope, and actual license. A directory or advertised catalog is not proof that a skill exists. |
| Automatic review | A background reviewer receives a fork of the completed conversation, explicit changed paths, and produces structured findings persisted to the original checkpoint. User-enabled; excludes review turns and subagent sessions to avoid loops. | Strong salvage candidate. Review exact artifact/run versions using separately constrained execution. Include shell-generated changes through reliable worker-side recording. |
| Direct document editing | Recent upstream supports Markdown/text editing, rendered preview, autosave, changed-on-disk detection, and manual-edit provenance. Editing remains desktop-only. | Reuse editor/preview behavior and attribution of human edits. Add browser editing, revision checks, revision-anchored comments, suggestions, and accept/reject operations as a service. |
| Knowledge | Workspace scaffold contains `AGENTS.md`, `KNOWLEDGE.md`, current facts in `knowledge/`, and append-only daily `notes/`. Global and project memory are editable. | Useful file structure, but a structured research board/ledger is a new feature. Give claims, conjectures, tasks, evidence, reviews, and decisions stable IDs and history across sessions. |
| LaTeX | Markdown uses `remark-math` and KaTeX. It normalizes bracket delimiters and tolerates invalid math without blanking a whole message. Present in fork and upstream. | Retain renderer and delimiter tests. Extend consistently to board entries, comments, documents, tool results, notebook output, and exports while preserving original LaTeX. |
| SageMath | Local notebook kernel language is Python or R. The bridge is Tauri-local and returns no execution result in browser mode. The fork has a `Sage` execution-contract variant, not a working Sage notebook implementation. | First-class Sage remains new work: verified worker capability, persistent kernel, `.sage` preprocessing, notebook integration, structured results, and LaTeX representations. |
| PDF | Real PDF files preview through native iframe viewers. A separate `PdfInspector` renders a styled HTML facsimile. A traceability helper extracts flattened text, citation identifiers, and numeric claims. | Reuse traceability checks, then add integrated page navigation, page/region citation anchors, links between extracted passages and the viewer, search, and reliable equation/table handling. |
| Provenance and runs | Append-only artifact versions retain tool/session/model, text or diff, environment, and package snapshot. Runs track commands, code, outputs, timing/logs, and environment; SQLite indexes JSONL across workspaces. Remote helpers record remote facts separately. | Strong domain and UI salvage. Production recording must move to trusted orchestrator/worker services and capture the actual executing worker, image, Conda changes, inputs, and outputs. |
| Runtime/provider/MCP boundaries | Typed SDK session/lifecycle/event/capability API; provider/MCP configuration remains specific to concrete OpenCode integration. ACP negotiates supported MCP transports. Curated science connector catalog and local/remote custom MCP settings exist. | Preserve explicit capability discovery and per-project connector scope. Execution-bearing MCP services should run in project/session workers when appropriate. |

## Source evidence

### Projects and shared sessions

- [`crates/osd-core/src/project.rs`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/crates/osd-core/src/project.rs), lines 1-4: folder-based project/session grouping; lines 185-192: project scaffold and initial snapshot; lines 294-303: copy/in-place import.
- [`apps/desktop/src/lib/runtime.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/runtime.ts), lines 402-418: projects are shared workspaces and sessions group by directory.

This is a useful product model. It does not supply the proposed persistent per-project container model by itself.

### Skills, review loops, and findings

- [`runtime/skills/README.md`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/skills/README.md): layered skill distribution; only directories with `SKILL.md` deploy as core skills; pinned external packs.
- [`apps/desktop/src/app/routes/SkillsPage.tsx`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/app/routes/SkillsPage.tsx): real skill/agent inventory, installation UI, origin classification.
- [`apps/desktop/src/lib/autoReview.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/autoReview.ts): reviewer identity, explicit changed-file scope, opt-in gates, loop prevention.
- [`runtime/opencode-profile/agent/reviewer.md`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/opencode-profile/agent/reviewer.md): evidence-backed bounded findings; profile denies edit/task; traceability and internal-consistency remit, without certifying scientific truth.
- [`runtime/opencode-profile/command/science-review.md`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/opencode-profile/command/science-review.md): manual review command and structured output contract.
- [`apps/desktop/src/lib/review.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/review.ts): parsing structured findings for cards.
- [`apps/desktop/src/lib/runtime.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/runtime.ts), lines 1940-2050: persists findings, creates fork, sends reviewer prompt.

Limits to preserve in planning: mutating-tool detection explicitly excludes `bash`, so shell-generated files can be missed by this trigger. A conversation fork with a path list does not demonstrate immutable file snapshots. Profile/prompt read-only intentions do not establish operating-system enforcement of every reviewer action. Our worker and snapshot model should enforce these properties directly.

### Living documents and editable memory

- [`apps/desktop/src/components/inspector/FilePreviewInspector.tsx`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/inspector/FilePreviewInspector.tsx), lines 135-145: changed-on-disk state; lines 248-307: desktop-only edit gate, save, manual-edit provenance, autosave, and disk change detection.
- [`apps/desktop/src/lib/artifactFile.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/artifactFile.ts), lines 190-198: write bridge throws outside desktop.
- [`apps/desktop/src/components/notebook/NotebookEditor.tsx`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/notebook/NotebookEditor.tsx), lines 178-218: follows agent file changes without overwriting unsaved local edits; notebook autosave.
- [`apps/desktop/src/components/settings/MemoryCard.tsx`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/settings/MemoryCard.tsx), lines 19-26 and 106-147: explicit human-editable global/project memory layers.
- [`runtime/harness/README.md`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/harness/README.md): current-fact knowledge versus daily logs.
- [`crates/osd-core/src/harness.rs`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/crates/osd-core/src/harness.rs): non-clobbering scaffold seeding; this is not the agent loop.

No completed collaborative living-document service with revision-anchored comments and suggestions was established in this inspection. Do not describe ordinary editable files or memory files as that service. Likewise, no completed structured project research board/ledger was established.

### Math, Sage, and PDFs

- [`apps/desktop/src/components/markdown-viewer/MarkdownViewer.tsx`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/markdown-viewer/MarkdownViewer.tsx), lines 72-102: math delimiter normalization and KaTeX plugins.
- [`apps/desktop/src/lib/kernel.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/kernel.ts), lines 14-35: Python/R persistent local kernels and desktop-only invocation.
- [`fork execution/mod.rs`](https://github.com/regenscheid/open-science-crypto/blob/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/apps/desktop/src-tauri/src/execution/mod.rs), line 297: Sage enum variant, not evidence of a working Sage kernel path.
- [`FilePreviewInspector.tsx`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/inspector/FilePreviewInspector.tsx), lines 745-751: native PDF iframe rendering.
- [`PdfInspector.tsx`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/inspector/PdfInspector.tsx), line 36: styled HTML facsimile; integrated pdf.js explicitly deferred there.
- [`runtime/skills/core/traceability-review/pdf_extract.py`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/skills/core/traceability-review/pdf_extract.py), lines 22-63: library fallbacks and text extraction; lines 125-128: citation/claim/text output. Page text is joined into one string rather than retained as navigable page/region anchors.
- [`traceability-review/SKILL.md`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/skills/core/traceability-review/SKILL.md): citation, number, and figure traceability procedure, including extracting PDF text first.

### Provenance, experiment runs, and placement limits

- [`apps/desktop/src/lib/provenance.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/provenance.ts), lines 84-105: recording returns immediately outside Tauri; artifact write/diff derivation is reusable separately.
- [`crates/osd-core/src/provenance.rs`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/crates/osd-core/src/provenance.rs): append-only version records, environment snapshots, package lists, content/diff, and run linkage. Its package snapshot addressing uses Rust `DefaultHasher`; do not present this as a cryptographic integrity proof.
- [`apps/desktop/src/lib/runs.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/runs.ts), lines 145-173: local experiment event derivation; remote records intentionally come from remote helpers rather than the laptop environment. Lines 224-244: recording returns immediately outside Tauri.
- [`crates/osd-core/src/runs_index.rs`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/crates/osd-core/src/runs_index.rs): incremental SQLite query index over durable local/remote JSONL records across project and session workspaces.
- [`ProvenancePanel.tsx`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/components/inspector/ProvenancePanel.tsx): history, environment/package disclosure, originating conversation, reproduce prompt.
- [`runtime/skills/core/remote-compute/SKILL.md`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/runtime/skills/core/remote-compute/SKILL.md): saved-machine discovery, SSH/Slurm execution and result retrieval, remote run recording. This is not a Kubernetes worker implementation.
- [`README.md`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/README.md): explicitly identifies absent headless local kernels and absent headless provenance/run appends.

For distributed execution, record facts at the trusted worker/controller boundary. Do not stamp remote artifacts with the GUI machine's environment or depend on an open desktop client to maintain the research record.

### Agent and MCP abstraction

- [`packages/sdk/src/runtime.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/packages/sdk/src/runtime.ts): typed lifecycle, sessions, forking, history, prompts, capability listing, interactive requests. Provider/MCP configuration is deliberately outside the generic interface. Some explanatory comments about the number of implementations lag behind the actual ACP implementation; inspect code, not only comments.
- [`packages/sdk/src/acp/AcpRuntime.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/packages/sdk/src/acp/AcpRuntime.ts), lines 181-220: negotiated session capabilities and MCP transports; unsupported optional MCP transports are filtered.
- [`apps/desktop/src/lib/scienceConnectors.ts`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/lib/scienceConnectors.ts): actual curated connector definitions. The smaller `runtime/mcp/README.md` is historical/planned context, not the complete implemented catalog.
- [`apps/desktop/src/app/routes/SettingsPage.tsx`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/apps/desktop/src/app/routes/SettingsPage.tsx), lines 1497-1689: curated setup, Jupyter setup, inventory, local/remote custom MCP configuration.
- [`fork science_mcp.rs`](https://github.com/regenscheid/open-science-crypto/blob/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/apps/desktop/src-tauri/src/science_mcp.rs): managed isolated uv environment for science MCP packages. This is package isolation, not the desired project execution security boundary.

## Earlier fork: salvage and incomplete work

The fork's strongest salvage is its execution vocabulary and failure scenarios: versioned specifications/events, permission receipts, lifecycle journals, replay acknowledgements, cleanup ownership, and approval presentation suitable for a later web interface.

- [`execution/mod.rs`](https://github.com/regenscheid/open-science-crypto/blob/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/apps/desktop/src-tauri/src/execution/mod.rs), lines 49-54: startup authority can be constructed, but serving agent requests and rebuilding it on workspace changes remain later slices.
- [`PROGRESS.md`](https://github.com/regenscheid/open-science-crypto/blob/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/PROGRESS.md), latest August entries: agent-serving path incomplete; Linux parity not claimed at that historical snapshot. Historical passing test counts were not rerun in this investigation.
- [`secure-workbook-execution.md`](https://github.com/regenscheid/open-science-crypto/blob/a7e39ea09cac2b9ef29e10e463de6d70ab82526b/docs/rfc/secure-workbook-execution.md), lines 330-365: persistent per-project environment is a Phase 2 proposal; earlier one-shot immutable/no-network proof is insufficient for automatic provisioning. Later parts propose grants, proxy requests, and bounded waiting; these proposals are not completed features.

Several useful files have identical Git blob hashes in the fork and recent upstream: reviewer prompt, science-review command, automatic-review helper, structured review parser, provenance frontend helper, PDF traceability extractor and skill, and harness instructions. Their concepts remain relevant; prefer recent upstream for newer editor/browser improvements.

OpenShell may replace much of the engine-specific OCI control code. Reuse contracts, lifecycle/error cases, and tests as design evidence before deciding whether implementation-level reuse is worthwhile. No code here has been demonstrated port-ready.

## External skill packs: useful ideas and limits

Upstream pins packs in [`scripts/dev/fetch-skills.sh`](https://github.com/ai4s-research/open-science/blob/aa7f27667c5f4fffdfa583066beed5214fc03e2a/scripts/dev/fetch-skills.sh).

The [`ai4s-skills` pack at `2519e8d`](https://github.com/ai4s-research/ai4s-skills/tree/2519e8d11fd49429fb41d39c2f35b7d453814b11) is [MIT licensed](https://github.com/ai4s-research/ai4s-skills/blob/2519e8d11fd49429fb41d39c2f35b7d453814b11/LICENSE). Its research-explorer, literature-survey, experiment-suite, paper-writer, integrity-auditor, and orchestration procedures offer examples of structured artifact contracts.

Its [`experiment-suite`](https://github.com/ai4s-research/ai4s-skills/blob/2519e8d11fd49429fb41d39c2f35b7d453814b11/skills/experiment-suite/SKILL.md), lines 95-101, explicitly allows seeded plausible simulated results alongside measured results. Data and figure contracts are valuable, but cryptographic workflows should keep synthetic illustration separate from measured experiments, exhaustive finite checks, and formal proof. A simulated fallback must not silently satisfy a research completion gate.

OSD's skill README and fetch-script comments describe Anthropic office skills as Apache-2.0. The [actual pinned PDF skill license](https://github.com/anthropics/skills/blob/9d2f1ae187231d8199c64b5b762e1bdf2244733d/skills/pdf/LICENSE.txt) contains restrictive Anthropic service terms and does not match that label. Do not infer redistribution permission from OSD's description. Learn from behavior and independently implement tools, or verify the actual license applicable to each candidate component before copying it.

## Recommended incorporation priorities

1. Checkpoint reviews with structured, evidence-backed findings and enforced reviewer limits.
2. Project-shared evidence, artifact lineage, and execution records captured without a desktop client.
3. LaTeX throughout the GUI and output, preserving editable source.
4. Browser living documents with revision-aware human edits, comments, and suggestions.
5. Worker-backed Sage notebooks and PDF extraction/navigation linked to precise source locations.
6. A project research board connecting questions, conjectures, experiments, evidence, reviews, and decisions across workbooks.

The best immediate salvage is domain contracts, review parsing/presentation, math rendering, provenance/run models and UI, skill organization, deterministic traceability helpers, and newer upstream editor behavior. Desktop-specific execution and the unfinished fork OCI path remain references rather than demonstrated distributed foundations.
