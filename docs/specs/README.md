# Behavior specifications

Date: 2026-10-11. Work package W0 ([implementation plan](../IMPLEMENTATION-PLAN.md#w0--behavior-specification-and-harness-foundation-roadmap-m0), roadmap M0, D51). Each spec's own status line is authoritative; in short:

| Spec | Status | Built, as described in |
| --- | --- | --- |
| [Tools](tools.md) | Approved as drafted (2026-10-14) | Built: [worker](../WORKER.md), [background jobs](../BACKGROUND-JOBS.md) |
| [PDF sources](pdf-sources.md) | Approved as drafted (2026-10-14) | Built: [worker](../WORKER.md#pdfs) |
| [PDF viewer](pdf-viewer.md) | Approved (2026-10-14) | In progress: [workspace](../WORKSPACE.md) |
| [Conda provisioning](conda-provisioning.md) | Approved (2026-10-11), amended since | In part: [worker](../WORKER.md#provisioning-operations); restoring a revision, uninstall, pip upgrade and disk limits are not built |
| [Research artifacts](research-artifacts.md) | Approved (2026-10-11), amended since | In part: [artifact inventory](../ARTIFACTS.md) |
| [Consistency review](consistency-review.md) | Approved (2026-10-11), amended since | Built: [consistency review](../CONSISTENCY-REVIEW.md) |
| [Living documents](living-documents.md) | Draft; main questions settled by the user (2026-10-11) | In part: [living documents](../LIVING-DOCUMENTS.md); document files in environments and exports are not built |
| [Plans and delegation](plans-and-delegation.md) | Approved (2026-10-14); questions 6 and 7 remain open (5 answered with the ledger) | Plans in part: [session plans](../SESSION-PLANS.md); delegation is not built (the prototype is [archived](../../.agents/docs/archive/README.md)) |
| [Project ledger, board and frontier](ledger.md) | Approved (2026-10-14); its questions decided | Not built |
| [Skills](skills.md) | Draft | Not built |
| [Run service](run-service.md) | Approved; its questions decided (2026-10-14) | Built, the server side ([workspace](../WORKSPACE.md#chat)); the GUI's chat views are not built |
| [Project memory](project-memory.md) | Approved (2026-10-14) | Not built |

These specifications state what the new harness must do for the CScience features it reimplements (D03, D11), and how to test it. They are written in the project's own words from observed use, the agent reports and public documentation (D56). The agent reports serve as evidence of *what* CScience does; their line anchors are not reproduced. No Claude Science code, prompt, skill text, schema or SQL is used. The reviewer rubric, role prompts and tool descriptions are written independently when each feature is built.

## Index

| Spec | Covers | Main decisions |
| --- | --- | --- |
| [Tools](tools.md) | Shell, background jobs, persistent kernels, file handoff, errors, cancellation, reconnect | Written separately; D83 |
| [Skills](skills.md) | Skill sources and pinning, discovery, on-demand loading, resources, helper code, load evidence, the external crypto-analysis-skills pack | D04, D29–D32 |
| [Plans and delegation](plans-and-delegation.md) | Versioned plans, progress, tracks, delegated agents and their outcomes, questions to the user, role-specific model settings | D04, D61 |
| [Consistency review](consistency-review.md) | Checkpoint and terminal triggers, reviewer scope, evidence snapshots, findings and severity, author response, claimed-fix confirmation, bounded completion, outcomes, and `math` findings with their mechanical confirmation | D27, D28, D35, D82 |
| [Conda provisioning](conda-provisioning.md) | Requests, policy decisions through the package cache, installation, environment revisions and lockfiles, probes, failure and rollback | D16, D46, D48, D71 |
| [Research artifacts](research-artifacts.md) | Artifact identity and revisions, publication, lineage, references, annotations | D09, D10, D28, D37, D40 |
| [PDF sources](pdf-sources.md) | Page facts and labels, page text with line anchors, search, page and region renders, `PdfAnchor` | D59 |
| [PDF viewer](pdf-viewer.md) | The user's viewer for published PDFs: a second origin, capability URLs, the sandboxed frame and its CSP, PDF.js, anchors | D54, D59, T6 |
| [Living documents](living-documents.md) | Markdown with LaTeX, block and math-span IDs, attributed revisions, user edits, revision-checked agent patches and suggestions, comments and work requests, rendering, references, exports, events for reviews, math delimiter normalization and the repair of LaTeX that KaTeX rejects, document files in every environment with their helper and merged direct edits, math findings for the agents that wrote the math | D23, D25, D28, D65, D82 |
| [Run service](run-service.md) | GUI chat: the run service, one active run per session, messages queued to safe points, earlier conversation, cancel, restart, run events and their format | Refactoring plan decision 5, D83 |
| [Project memory](project-memory.md) | Short entries per project that agents read and propose, and the user accepts, declines or edits; given to every agent role, bounded; never a file in environments | Owner, 2026-10-14 (a memory tool; every role; never a file) |
| [Project ledger, board and frontier](ledger.md) | Typed entries and statuses, evidence links and evidence types, attribution, published and project results, frontier comparison, links to findings, reconsideration flags, mutations, the external pack's board records and imports; the board, frontier, issue list and history as views | D24, D29, D33, D34 |

Not yet specified: native Sage kernel behavior (roadmap M0) and the proposed reproduction review (D27). Every [external skills compatibility gate](../EXTERNAL-SKILLS.md#compatibility-gates) now has a fixture: gates 1–3 and 7 in [skills](skills.md), gate 4 in [plans and delegation](plans-and-delegation.md) with its link to a board entry in the [ledger](ledger.md) (LG-F8), and gates 5 and 6 in the ledger (LG-F7, LG-F9, LG-F11).

## Conventions

- **Requirements.** Every item under *Behavior* with an ID (`SK-3`, `CR-12`) is a requirement, including its sub-bullets. IDs are never renumbered or reused; a withdrawn requirement keeps its ID and says so. Plain present-tense statements are binding. **MUST** and **MUST NOT** mark requirements whose violation would corrupt evidence, weaken isolation or contradict a confirmed decision; **SHOULD** allows a justified exception; **MAY** marks an option.
- **Sources of each requirement.** `(D35)` cites the [decision register](../DECISIONS.md); the decision's status there (confirmed, recommended or proposed) applies. `(CScience)` marks behavior observed in CScience that the reimplementation keeps (D03, D04, D35). `(new)` marks behavior a spec proposes beyond what CScience does: in a draft the user has not decided it, and in an approved spec the user approved it with the spec. Other notes in parentheses name the evidence for a choice, such as OpenScience Desktop or Claude Code behavior. Unmarked items restate other repository documents, listed under *Sources* in each spec.
- **Contracts.** The shared contract names come from the [implementation plan](../IMPLEMENTATION-PLAN.md#shared-contracts-to-establish-first). Field and event names in these specs are descriptive; W1 settles names and serialization in `packages/contracts`.
- **Actors.** The user; agent runs with the role `main`, `delegated` or `reviewer` ([plans and delegation](plans-and-delegation.md)); and the platform services (harness, worker, policy middleware, monitor). Every event names its producer. A record made by a platform service is an observation; a statement by an agent is an assertion and is stored as one.
- **Time.** Thresholds and timeouts use an injectable clock, so fixtures can advance time.

### Fixtures

Each spec ends with acceptance fixtures: *Given* (setup and inputs), *When* (action), *Then* (expected observable results). Observable means API responses, records and events read through the API, the requests captured by the scripted model, and the process, file and network observations reported by the worker and the policy services. Internal state does not count.

| Kind | Needs | Purpose |
| --- | --- | --- |
| `scripted` | The real harness with a **scripted model** (new): an adapter behind the provider-neutral model interface (D61) that replays predetermined tool calls and text and records every request it receives. A fake worker is enough unless the fixture is also `worker` | Tests harness behavior deterministically; runs in CI |
| `worker` | A real worker: OpenShell with the MicroVM driver on babbage in phase 1 | Integration test, skipped unless `SS_INTEGRATION=1` ([conventions](../../.agents/CONVENTIONS.md#testing)) |
| `live` | Real models through LiteLLM | Measures routing or quality, such as skill activation per model alias; reports results and gates nothing unless the fixture says so |

Fixture data (a test skill pack, a local Conda channel with test packages, scripted transcripts) is written by this project under MIT when the fixture is implemented. The numeric fixture `finite-field-identity-v1` is defined in the [roadmap](../ROADMAP.md#first-end-to-end-fixture).

## Contract mapping

What each feature needs from the shared contracts. The *Contracts used* section of each spec gives the detail.

| Contract | Skills | Plans and delegation | Consistency review | Conda provisioning | Research artifacts | Living documents | Ledger |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ExecutionContext` | Helper staging and kernel injection run as executions whose origin names the skill revision | Delegated agents execute in the session's context; the actor names the agent run and role | The reviewer starts no executions; the snapshot lists the attempts it covers | Installs and probes are executions on the target environment and revision | Lineage names the producing attempt, kernel and lease generation | Exports are executions in a session's environment; helper calls and direct edits of document files name their execution where the worker can tell | Changes made by worker code name their execution where the platform can tell; evidence cites executions |
| `ArtifactRef` | A pack revision is an imported snapshot; each loaded body and resource is a pinned reference | Plans are artifacts; briefs and outcomes reference pinned inputs and results | The evidence snapshot is a list of pinned references | Lockfiles and install logs are artifacts | The contract itself, plus the pinned or follow-latest mode | Documents are control-plane artifacts, mirrored as files under `documents/` in every environment; embeds follow `latest`, evidence links are pinned, and exports pin every reference | Entries are control-plane artifacts; evidence links are pinned |
| `Anchor` | Findings and comments can point at a line of a skill revision | Plan steps are addressable blocks of a plan revision | Findings anchor to transcript entries, execution logs and artifact blocks; `math` findings to math spans | Not needed | Annotations; carry-forward to new revisions with stale and orphaned states | Blocks, math spans and text ranges, carried forward by ID | Entry fields; evidence at document blocks, PDF regions and log ranges |
| `Mutation` | Changing a source pin is a profile mutation | Plan versions and progress updates name the expected version | Finding transitions name the expected status | An install names the environment revision it was planned against | Shared publication and annotation edits name the expected revision | Patches, saves, helper calls and merged direct edits name their base and the blocks they touch; rebased when those are unchanged, a conflict otherwise | Every change names the expected revision of each entry it changes, commits all or nothing and is never rebased; operation IDs make retries safe |
| `Event` | Catalog, load, resource-read, staging and injection events from the harness | Plan, progress, delegation, question and agent-run events | Trigger inputs; review and finding events | Operation, decision, revision and probe events | Revision-created and annotation events | Revision events with changed and structural blocks and agent-written characters | Entry, work-record, flag, frontier and import events |
| `PermissionRequest` | None of its own: a skill never grants or requests authority | Distinct from questions to the user | Not used: review verdicts grant nothing | One request covering the exact files of a resolved install when the policy escalates | Not used directly; publication follows the project grant | None of its own: a work request grants nothing | Not used: decisions and flags grant nothing |
| `EnvironmentRecord` | Probe results decide skill availability; a read-only staging location | The environment a delegation's tools use | Environment revisions of the covered executions | Revision history, lockfile, licenses, probes, status | Environment revision, base image digest and isolation tier in lineage | `documents/` among every environment's locations; Pandoc and TeX probes for exports | Not needed |

### Proposed contract additions (new)

1. `ExecutionContext`: the actor distinguishes the user, an agent run (with its role) and a platform service. An optional origin names the tool call, delegation, plan step or skill helper revision that caused the execution, as `origin` does in the [run request example](../../examples/run-request.json).
2. `ArtifactRef`: a resolution mode, `pinned` or `latest`. Evidence uses `pinned` only.
3. `Anchor`: transcript anchors (session, message, tool call, output range), execution-log anchors (attempt, stream, line range) and image regions.
4. `Event`: a producer kind (harness, worker, policy middleware, monitor, agent run, user), so that consumers can tell observations from assertions.
5. `PermissionRequest`: a scope that is an exact set of package files resolved before an install.
6. `EnvironmentRecord`: per-revision lockfile, package licenses and probe results; a status (`ready`, `changing`, `needs_recovery`); a read-only skill staging location among its declared locations.
7. `Anchor`: a field of a structured record, such as a ledger entry's statement or a comment's body, with an optional text range or math span inside it.
8. `Mutation`: the parts a change touches. A document patch with a stale base applies to the current revision when none of the blocks and math spans it touches changed since, and the result names both revisions; otherwise the conflict names the changed parts.
9. `ArtifactRef`: control-plane artifacts (documents, ledger entries, plans), whose revisions come from mutations and whose history stays in the control store. A mutation's origin, in place of a producing attempt, is the user, an agent run, worker code (a helper call or a direct edit of a document's file, with its execution where the worker can tell), an import, or a platform service such as the math repair. Documents also appear as files under `documents/` in every environment, kept at their current revisions; ledger entries are not files (`ledger/…`).

Additions 1–3, which the research-artifacts and consistency-review specs use, and 5 and 6, which Conda provisioning uses, are approved with those specs (2026-10-11). Addition 4 is still proposed, and so are 7–9, which the living-documents and ledger drafts use.

The specs also introduce domain records: agent runs, plans and progress, delegations and outcomes, questions, reviews and findings, provisioning operations, annotations, document block maps, patches and suggestions, math repair records, ledger entries, work records and flags, and frontier categories. They reuse the shared contracts and keep their own schemas and lifecycles ([consistency review of the plan](../../.agents/planning-consistency-review-2026-10-10.md#shared-foundations-and-their-consumers)).

## Main open questions

Each spec lists its own. These need the user first:

1. **Review settings.** The defaults in [consistency review](consistency-review.md) start from the inspected CScience source defaults, and stand until the user's own settings are known. Which settings does the user's CScience actually run with (PRODUCT.md asks for the active configuration)?
2. **Terminal review as a completion gate.** Settled 2026-10-11: the terminal review holds the turn's completion.
3. **A `withdrawn` finding status.** Settled 2026-10-11: yes.
4. **Shell-invoked installs.** Settled 2026-10-11: through wrappers ([Conda provisioning](conda-provisioning.md)).
5. **Which files become artifacts.** Settled 2026-10-11: every file an execution writes in its session workspace, minus ignore patterns.
6. **Delegation defaults.** Answered 2026-10-14 by the owner: at least 8–10 concurrent delegated agents per session (4–5 typical); one additional level of nesting, so the main agent's delegated agents may delegate once more; a separate kernel for each delegated agent, with no kernel sharing ([plans and delegation](plans-and-delegation.md), PD-14, PD-15).
7. **Skill helper injection.** Inject a skill's kernel helpers when the skill is loaded (as CScience does), or only on an explicit call?
8. **Plan approval.** Should plans need the user's approval before execution by default, and should every new version of an approved plan need approval again?
9. **Documents as files.** Settled 2026-10-11: each living document is also a Markdown file under `documents/` in every environment, kept at its current revision. Agents change documents through the document tool or its helper (`ss-doc`), and direct edits of the files are merged, or kept as rejected patches ([living documents](living-documents.md), LD-3, LD-49 to LD-52; RA-1 amended). Ledger entries stay in the control store, as drafted.
10. **Refinements of approved text.** Settled 2026-10-11 and recorded as dated amendments: a finding on a document block or ledger field is superseded only when that block or field changes (CR-28); document and ledger edits count toward CR-10 as text, not as artifact revisions; and only the user's comments start agent work (RA-24).
11. **What the main agent may decide alone.** Settled 2026-10-11: under the gate it may establish results, accept obstructions, clear reconsideration flags and refute published results, and a project setting can reserve any of these to the user, none by default; code that calls the platform has a delegated agent's authority ([ledger](ledger.md), LG-13, LG-17).
12. **Frontier comparison.** Settled 2026-10-11: as drafted (LG-21, LG-22).
13. **Theorem-like blocks.** Settled 2026-10-11: fenced containers such as `::: lemma` (LD-4).
14. **Math normalization and repair.** Settled 2026-10-11: math is rewritten on save only in documents, direct edits of their files included, and in ledger entries; other Markdown keeps its bytes. Failed model repairs are retried after an hour, with the cache per project (LD-42, LD-46). Formulas KaTeX rejects in an agent's durable text also go back to that agent as `math` findings, delivered at the end of the turn and confirmed by the check itself (D82; LD-53 to LD-55, CR-36 to CR-38).
15. **Document files: what the file design leaves.** Drafted: deleting or renaming a document's file is undone, and new files under `documents/` are never adopted as documents (LD-51); during overlapping calls, a direct edit is based on the oldest revision any of them could have read, so it may conflict needlessly, and two calls editing one file in one environment cannot be told apart (LD-50); the helper answers only while a call is running (LD-52). Recommended: as drafted.
16. **Background jobs.** Settled 2026-10-11: long computations run as background jobs, which survive restarts of the orchestrator and of the worker, since the executor in the sandbox outlives the worker's connection (D83; [tools](tools.md)). The agent usually waits on the jobs it needs, its turn open and its model idle, while a low-cost watcher model checks each job 5 minutes after it starts and then every 30 minutes and wakes the agent when one needs attention; the user can message the agent during a wait. Adopted with the tools spec on 2026-10-14: a job's limits (24 hours by default, up to 7 days; 4 at once in a session, 8 in an environment); closing a session cancels its jobs; no background cells.
17. **Ambiguous `\(…\)` on save.** A `\(…\)` holding a dollar sign or a line break, or followed by a digit, would leave its sentence as the `$$…$$` LD-41 drafts. Recommended, and implemented for now: keep it as written ([living documents](living-documents.md), open question 15).

## Sources

- Repository: [feature plan](../FEATURE-PLAN.md), [source study](../SOURCE-STUDY.md), [research workspace](../RESEARCH-WORKSPACE.md), [projects and sessions](../PROJECTS-AND-SESSIONS.md), [external skills](../EXTERNAL-SKILLS.md), [architecture](../ARCHITECTURE.md), [monitoring](../MONITORING.md), [package cache](../PACKAGE-CACHE.md), [policy](../POLICY.md), [implementation plan](../IMPLEMENTATION-PLAN.md), [decisions](../DECISIONS.md), [product](../PRODUCT.md), [licensing](../LICENSING.md).
- Agent reports, as behavior evidence: [Claude Science](../../.agents/docs/agent-reports/claude-science.md), [Claude Code and Desktop](../../.agents/docs/agent-reports/claude-code-desktop.md), [OpenScience Desktop](../../.agents/docs/agent-reports/openscience-desktop.md). The user's own CScience compatibility layer, `cscience-crypto`, is behavior evidence for math delimiter normalization and repair ([living documents](living-documents.md)); the specs copy none of its code. The workspace adapts its math repair code, which the user contributed under the MIT License ([workspace](../WORKSPACE.md#math-repairs)).
- Public documentation, retrieved 2026-10-11: Claude Code [skills](https://code.claude.com/docs/en/skills), [subagents](https://code.claude.com/docs/en/sub-agents) and [hooks](https://code.claude.com/docs/en/hooks); Claude Docs' [getting-started documentation](https://support.claude.com/en/articles/16923645-get-started-with-claude-docs); KaTeX's [supported functions](https://katex.org/docs/supported.html) and [security guidance](https://katex.org/docs/security.html); OpenScience Desktop at [`aa7f276`](https://github.com/ai4s-research/open-science/tree/aa7f27667c5f4fffdfa583066beed5214fc03e2a) (MIT); [crypto-analysis-skills](https://github.com/regenscheid/crypto-analysis-skills/tree/4089e99415f349dde406c8e57a227715002ee166) at `4089e99`.
