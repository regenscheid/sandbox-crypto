# Behavior specifications

Date: 2026-10-11. **Status: Drafts to review with the user, except [Conda provisioning](conda-provisioning.md), [research artifacts](research-artifacts.md) and [consistency review](consistency-review.md), approved as drafted (2026-10-11).** Work package W0 ([implementation plan](../IMPLEMENTATION-PLAN.md#w0--behavior-specification-and-harness-foundation-roadmap-m0), roadmap M0, D51). Nothing here is implemented.

These specifications state what the new harness must do for the CScience features it reimplements (D03, D11), and how to test it. They are written in the project's own words from observed use, the agent reports and public documentation (D56). The agent reports serve as evidence of *what* CScience does; their line anchors are not reproduced. No Claude Science code, prompt, skill text, schema or SQL is used. The reviewer rubric, role prompts and tool descriptions are written independently when each feature is built.

## Index

| Spec | Covers | Main decisions |
| --- | --- | --- |
| [Tools](tools.md) | Shell, persistent kernels, file handoff, errors, cancellation, reconnect | Written separately |
| [Skills](skills.md) | Skill sources and pinning, discovery, on-demand loading, resources, helper code, load evidence, the external crypto-analysis-skills pack | D04, D29–D32 |
| [Plans and delegation](plans-and-delegation.md) | Versioned plans, progress, tracks, delegated agents and their outcomes, questions to the user, role-specific model settings | D04, D61 |
| [Consistency review](consistency-review.md) | Checkpoint and terminal triggers, reviewer scope, evidence snapshots, findings and severity, author response, claimed-fix confirmation, bounded completion, outcomes | D27, D28, D35 |
| [Conda provisioning](conda-provisioning.md) | Requests, policy decisions through the package cache, installation, environment revisions and lockfiles, probes, failure and rollback | D16, D46, D48, D71 |
| [Research artifacts](research-artifacts.md) | Artifact identity and revisions, publication, lineage, references, annotations | D09, D10, D28, D37, D40 |
| [PDF sources](pdf-sources.md) | Page facts and labels, page text with line anchors, search, page and region renders, `PdfAnchor` | D59 |
| [Living documents](living-documents.md) | Markdown with LaTeX, block and math-span IDs, attributed revisions, user edits, revision-checked agent patches and suggestions, comments and work requests, rendering, references, exports, events for reviews, math delimiter normalization and the repair of LaTeX that KaTeX rejects | D23, D25, D28, D65 |
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
| `ExecutionContext` | Helper staging and kernel injection run as executions whose origin names the skill revision | Delegated agents execute in the session's context; the actor names the agent run and role | The reviewer starts no executions; the snapshot lists the attempts it covers | Installs and probes are executions on the target environment and revision | Lineage names the producing attempt, kernel and lease generation | Exports are executions in a session's environment | Changes made by worker code name their execution where the platform can tell; evidence cites executions |
| `ArtifactRef` | A pack revision is an imported snapshot; each loaded body and resource is a pinned reference | Plans are artifacts; briefs and outcomes reference pinned inputs and results | The evidence snapshot is a list of pinned references | Lockfiles and install logs are artifacts | The contract itself, plus the pinned or follow-latest mode | Documents are control-plane artifacts; embeds follow `latest`, evidence links are pinned, and exports pin every reference | Entries are control-plane artifacts; evidence links are pinned |
| `Anchor` | Findings and comments can point at a line of a skill revision | Plan steps are addressable blocks of a plan revision | Findings anchor to transcript entries, execution logs and artifact blocks | Not needed | Annotations; carry-forward to new revisions with stale and orphaned states | Blocks, math spans and text ranges, carried forward by ID | Entry fields; evidence at document blocks, PDF regions and log ranges |
| `Mutation` | Changing a source pin is a profile mutation | Plan versions and progress updates name the expected version | Finding transitions name the expected status | An install names the environment revision it was planned against | Shared publication and annotation edits name the expected revision | Patches and saves name their base and the blocks they touch; rebased when those are unchanged, a conflict otherwise | Every change names the expected revision of each entry it changes, commits all or nothing and is never rebased; operation IDs make retries safe |
| `Event` | Catalog, load, resource-read, staging and injection events from the harness | Plan, progress, delegation, question and agent-run events | Trigger inputs; review and finding events | Operation, decision, revision and probe events | Revision-created and annotation events | Revision events with changed and structural blocks and agent-written characters | Entry, work-record, flag, frontier and import events |
| `PermissionRequest` | None of its own: a skill never grants or requests authority | Distinct from questions to the user | Not used: review verdicts grant nothing | One request covering the exact files of a resolved install when the policy escalates | Not used directly; publication follows the project grant | None of its own: a work request grants nothing | Not used: decisions and flags grant nothing |
| `EnvironmentRecord` | Probe results decide skill availability; a read-only staging location | The environment a delegation's tools use | Environment revisions of the covered executions | Revision history, lockfile, licenses, probes, status | Environment revision, base image digest and isolation tier in lineage | Pandoc and TeX probes for exports | Not needed |

### Proposed contract additions (new)

1. `ExecutionContext`: the actor distinguishes the user, an agent run (with its role) and a platform service. An optional origin names the tool call, delegation, plan step or skill helper revision that caused the execution, as `origin` does in the [run request example](../../examples/run-request.json).
2. `ArtifactRef`: a resolution mode, `pinned` or `latest`. Evidence uses `pinned` only.
3. `Anchor`: transcript anchors (session, message, tool call, output range), execution-log anchors (attempt, stream, line range) and image regions.
4. `Event`: a producer kind (harness, worker, policy middleware, monitor, agent run, user), so that consumers can tell observations from assertions.
5. `PermissionRequest`: a scope that is an exact set of package files resolved before an install.
6. `EnvironmentRecord`: per-revision lockfile, package licenses and probe results; a status (`ready`, `changing`, `needs_recovery`); a read-only skill staging location among its declared locations.
7. `Anchor`: a field of a structured record, such as a ledger entry's statement, with an optional text range inside it.
8. `Mutation`: the parts a change touches. A document patch with a stale base applies to the current revision when none of the blocks and math spans it touches changed since, and the result names both revisions; otherwise the conflict names the changed parts.
9. `ArtifactRef`: control-plane artifacts (documents, ledger entries, plans), whose paths lie outside the workspaces (`documents/…`, `ledger/…`) and whose revisions come from mutations. A mutation's origin, in place of a producing attempt, is the user, an agent run, worker code through the platform API (with its execution where the worker can tell), an import, or a platform service such as the math repair.

Additions 1–3, which the research-artifacts and consistency-review specs use, and 5 and 6, which Conda provisioning uses, are approved with those specs (2026-10-11). Addition 4 is still proposed, and so are 7–9, which the living-documents and ledger drafts use.

The specs also introduce domain records: agent runs, plans and progress, delegations and outcomes, questions, reviews and findings, provisioning operations, annotations, document block maps, patches and suggestions, math repair records, ledger entries, work records and flags, and frontier categories. They reuse the shared contracts and keep their own schemas and lifecycles ([consistency review of the plan](../CONSISTENCY-REVIEW.md#shared-foundations-and-their-consumers)).

## Main open questions

Each spec lists its own. These need the user first:

1. **Review settings.** The defaults in [consistency review](consistency-review.md) start from the inspected CScience source defaults, and stand until the user's own settings are known. Which settings does the user's CScience actually run with (PRODUCT.md asks for the active configuration)?
2. **Terminal review as a completion gate.** Settled 2026-10-11: the terminal review holds the turn's completion.
3. **A `withdrawn` finding status.** Settled 2026-10-11: yes.
4. **Shell-invoked installs.** Settled 2026-10-11: through wrappers ([Conda provisioning](conda-provisioning.md)).
5. **Which files become artifacts.** Settled 2026-10-11: every file an execution writes in its session workspace, minus ignore patterns.
6. **Delegation defaults.** Depth 1, four concurrent delegated agents per session, and a separate kernel for each delegated agent.
7. **Skill helper injection.** Inject a skill's kernel helpers when the skill is loaded (as CScience does), or only on an explicit call?
8. **Plan approval.** Should plans need the user's approval before execution by default, and should every new version of an approved plan need approval again?
9. **Documents and ledger entries outside the workspaces.** They live only in the control store, under paths of their own, and change only through patches and ledger mutations, never through a file an execution writes ([living documents](living-documents.md), LD-3). This adds a third path area to the approved RA-1.
10. **Refinements of approved text.** A finding on a document block or ledger field is superseded only when that block or field changes, decided when the next review is dispatched, not on every new revision (refines CR-28; LD-25, LG-25). Document and ledger revisions count toward CR-10 only through its text and structural-block predicates, not as artifact revisions (clarifies CR-10; LD-40, LG-33). Only the user's comments addressed to an agent start work, so agents cannot keep waking each other (narrows RA-24; LD-22).
11. **What the main agent may decide alone.** Establish project results under an independent-check gate, accept obstructions, clear reconsideration flags, refute published results and rank frontier results, or reserve some of these to the user ([ledger](ledger.md), LG-17, open question 2)? Calls from worker code get only a delegated agent's authority (LG-13).
12. **Frontier comparison.** Automatic only within equal conditions and on numeric measures, with explicit `dominates` links for the rest (LG-21, LG-22)?
13. **Theorem-like blocks.** Fenced containers such as `::: lemma` in the document syntax, for claims, assumptions and proofs (LD-4)?
14. **Math normalization and repair.** Documents and ledger entries are saved with dollar delimiters, and LaTeX that KaTeX rejects is repaired for display only, by rules, then by the reviewer model, then as a Unicode approximation, as the user's CScience layer does (LD-41 to LD-48). Should Markdown files that executions write be rewritten too (drafted: no, they stay as written), and should failed repair calls be retried after an hour (drafted: yes)?

## Sources

- Repository: [feature plan](../FEATURE-PLAN.md), [source study](../SOURCE-STUDY.md), [research workspace](../RESEARCH-WORKSPACE.md), [projects and sessions](../PROJECTS-AND-SESSIONS.md), [external skills](../EXTERNAL-SKILLS.md), [architecture](../ARCHITECTURE.md), [monitoring](../MONITORING.md), [package cache](../PACKAGE-CACHE.md), [policy](../POLICY.md), [implementation plan](../IMPLEMENTATION-PLAN.md), [decisions](../DECISIONS.md), [product](../PRODUCT.md), [licensing](../LICENSING.md).
- Agent reports, as behavior evidence: [Claude Science](../../.agents/docs/agent-reports/claude-science.md), [Claude Code and Desktop](../../.agents/docs/agent-reports/claude-code-desktop.md), [OpenScience Desktop](../../.agents/docs/agent-reports/openscience-desktop.md). The user's own CScience compatibility layer, `cscience-crypto`, is behavior evidence for math delimiter normalization and repair ([living documents](living-documents.md)); the specs copy none of its code. The workspace adapts its math repair code, which the user contributed under the MIT License ([workspace](../WORKSPACE.md#math-repairs)).
- Public documentation, retrieved 2026-10-11: Claude Code [skills](https://code.claude.com/docs/en/skills), [subagents](https://code.claude.com/docs/en/sub-agents) and [hooks](https://code.claude.com/docs/en/hooks); Claude Docs' [getting-started documentation](https://support.claude.com/en/articles/16923645-get-started-with-claude-docs); KaTeX's [supported functions](https://katex.org/docs/supported.html) and [security guidance](https://katex.org/docs/security.html); OpenScience Desktop at [`aa7f276`](https://github.com/ai4s-research/open-science/tree/aa7f27667c5f4fffdfa583066beed5214fc03e2a) (MIT); [crypto-analysis-skills](https://github.com/regenscheid/crypto-analysis-skills/tree/4089e99415f349dde406c8e57a227715002ee166) at `4089e99`.
