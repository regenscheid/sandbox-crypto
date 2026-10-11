# Behavior specifications

Date: 2026-10-11. **Status: Draft, to review with the user.** Work package W0 ([implementation plan](../IMPLEMENTATION-PLAN.md#w0--behavior-specification-and-harness-foundation-roadmap-m0), roadmap M0, D51). Nothing here is implemented.

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

Not yet specified: native Sage kernel behavior and exact PDF/source anchors (roadmap M0), living documents and the ledger, board and frontier ([research workspace](../RESEARCH-WORKSPACE.md)), and the proposed reproduction review (D27). The [external skills compatibility gates](../EXTERNAL-SKILLS.md#compatibility-gates) 5 and 6 (shared board records, connector PDFs with page anchors) belong to those specs. Gates 1–3 and 7 have fixtures here, and so does gate 4 apart from its link to a board entry.

## Conventions

- **Requirements.** Every item under *Behavior* with an ID (`SK-3`, `CR-12`) is a requirement, including its sub-bullets. IDs are never renumbered or reused; a withdrawn requirement keeps its ID and says so. Plain present-tense statements are binding. **MUST** and **MUST NOT** mark requirements whose violation would corrupt evidence, weaken isolation or contradict a confirmed decision; **SHOULD** allows a justified exception; **MAY** marks an option.
- **Sources of each requirement.** `(D35)` cites the [decision register](../DECISIONS.md); the decision's status there (confirmed, recommended or proposed) applies. `(CScience)` marks behavior observed in CScience that the reimplementation keeps (D03, D04, D35). `(new)` marks behavior this draft proposes and the user has not decided. Other notes in parentheses name the evidence for a choice, such as OpenScience Desktop or Claude Code behavior. Unmarked items restate other repository documents, listed under *Sources* in each spec.
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

| Contract | Skills | Plans and delegation | Consistency review | Conda provisioning | Research artifacts |
| --- | --- | --- | --- | --- | --- |
| `ExecutionContext` | Helper staging and kernel injection run as executions whose origin names the skill revision | Delegated agents execute in the session's context; the actor names the agent run and role | The reviewer starts no executions; the snapshot lists the attempts it covers | Installs and probes are executions on the target environment and revision | Lineage names the producing attempt, kernel and lease generation |
| `ArtifactRef` | A pack revision is an imported snapshot; each loaded body and resource is a pinned reference | Plans are artifacts; briefs and outcomes reference pinned inputs and results | The evidence snapshot is a list of pinned references | Lockfiles and install logs are artifacts | The contract itself, plus the pinned or follow-latest mode |
| `Anchor` | Findings and comments can point at a line of a skill revision | Plan steps are addressable blocks of a plan revision | Findings anchor to transcript entries, execution logs and artifact blocks | Not needed | Annotations; carry-forward to new revisions with stale and orphaned states |
| `Mutation` | Changing a source pin is a profile mutation | Plan versions and progress updates name the expected version | Finding transitions name the expected status | An install names the environment revision it was planned against | Shared publication and annotation edits name the expected revision |
| `Event` | Catalog, load, resource-read, staging and injection events from the harness | Plan, progress, delegation, question and agent-run events | Trigger inputs; review and finding events | Operation, decision, revision and probe events | Revision-created and annotation events |
| `PermissionRequest` | None of its own: a skill never grants or requests authority | Distinct from questions to the user | Not used: review verdicts grant nothing | One request covering the exact files of a resolved install when the policy escalates | Not used directly; publication follows the project grant |
| `EnvironmentRecord` | Probe results decide skill availability; a read-only staging location | The environment a delegation's tools use | Environment revisions of the covered executions | Revision history, lockfile, licenses, probes, status | Environment revision, base image digest and isolation tier in lineage |

### Proposed contract additions (new)

1. `ExecutionContext`: the actor distinguishes the user, an agent run (with its role) and a platform service. An optional origin names the tool call, delegation, plan step or skill helper revision that caused the execution, as `origin` does in the [run request example](../../examples/run-request.json).
2. `ArtifactRef`: a resolution mode, `pinned` or `latest`. Evidence uses `pinned` only.
3. `Anchor`: transcript anchors (session, message, tool call, output range), execution-log anchors (attempt, stream, line range) and image regions.
4. `Event`: a producer kind (harness, worker, policy middleware, monitor, agent run, user), so that consumers can tell observations from assertions.
5. `PermissionRequest`: a scope that is an exact set of package files resolved before an install.
6. `EnvironmentRecord`: per-revision lockfile, package licenses and probe results; a status (`ready`, `changing`, `needs_recovery`); a read-only skill staging location among its declared locations.

The specs also introduce domain records: agent runs, plans and progress, delegations and outcomes, questions, reviews and findings, provisioning operations and annotations. They reuse the shared contracts and keep their own schemas and lifecycles ([consistency review of the plan](../CONSISTENCY-REVIEW.md#shared-foundations-and-their-consumers)).

## Main open questions

Each spec lists its own. These need the user first:

1. **Review settings.** The defaults in [consistency review](consistency-review.md) start from the inspected CScience source defaults. Which settings does the user's CScience actually run with (PRODUCT.md asks for the active configuration)?
2. **Terminal review as a completion gate.** The draft holds a turn's completion until its terminal review and at most two response rounds finish. Is that how the user experiences CScience, or should the turn complete at once with findings arriving later?
3. **A `withdrawn` finding status** for accepted rebuttals and user dismissals, beside the six outcomes in the feature plan.
4. **Shell-invoked installs.** Route `conda`, `mamba` and `pip` run from the shell through the environment manager, or refuse them and require the provisioning tool?
5. **Which files become artifacts.** Every file an execution writes in its session workspace (minus ignore patterns), or only declared output locations?
6. **Delegation defaults.** Depth 1, four concurrent delegated agents per session, and a separate kernel for each delegated agent.
7. **Skill helper injection.** Inject a skill's kernel helpers when the skill is loaded (as CScience does), or only on an explicit call?
8. **Plan approval.** Should plans need the user's approval before execution by default, and should every new version of an approved plan need approval again?

## Sources

- Repository: [feature plan](../FEATURE-PLAN.md), [source study](../SOURCE-STUDY.md), [research workspace](../RESEARCH-WORKSPACE.md), [projects and sessions](../PROJECTS-AND-SESSIONS.md), [external skills](../EXTERNAL-SKILLS.md), [architecture](../ARCHITECTURE.md), [monitoring](../MONITORING.md), [package cache](../PACKAGE-CACHE.md), [policy](../POLICY.md), [implementation plan](../IMPLEMENTATION-PLAN.md), [decisions](../DECISIONS.md), [product](../PRODUCT.md), [licensing](../LICENSING.md).
- Agent reports, as behavior evidence: [Claude Science](../../.agents/docs/agent-reports/claude-science.md), [Claude Code and Desktop](../../.agents/docs/agent-reports/claude-code-desktop.md), [OpenScience Desktop](../../.agents/docs/agent-reports/openscience-desktop.md).
- Public documentation, retrieved 2026-10-11: Claude Code [skills](https://code.claude.com/docs/en/skills), [subagents](https://code.claude.com/docs/en/sub-agents) and [hooks](https://code.claude.com/docs/en/hooks); OpenScience Desktop at [`aa7f276`](https://github.com/ai4s-research/open-science/tree/aa7f27667c5f4fffdfa583066beed5214fc03e2a) (MIT); [crypto-analysis-skills](https://github.com/regenscheid/crypto-analysis-skills/tree/4089e99415f349dde406c8e57a227715002ee166) at `4089e99`.
