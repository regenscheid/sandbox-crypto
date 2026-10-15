# Skills

Date: 2026-10-11. **Status: Draft, to review with the user.** W0 behavior specification; conventions in the [index](README.md). Nothing in it is built yet.

## Scope

A skill is a versioned package of procedure text, with optional resources and helper code, that an agent loads when it is relevant. This spec covers skill sources and pinning, discovery, loading bodies and resources, helper code, load evidence, and the binding for the external crypto-analysis-skills pack (D30–D32). The platform keeps useful built-in skills (D04), written anew: no Claude Science skill text is used (D56).

Out of scope: the content of skills (the external pack stays in its repository, D30); whether a skill improves research (evaluated in M5); MCP prompts and resources ([MCP connectors](../MCP-CONNECTORS.md)); the shell and kernel tools ([tools](tools.md)).

Terms:

- **Skill**: a directory with a `SKILL.md` file: front matter with at least `name` and `description`, then a body. Other files in the directory are **resources**; resources meant to run are **helper code**.
- **Source**: where skills come from: `platform` (this project's own skills), `external` (a pack in another repository, pinned to a commit) or `project` (skills stored as project artifacts, which the user can edit).
- **Skill revision**: the source, its commit or artifact revision, the skill's path and the SHA-256 of its body.
- **Effective skill set**: the skills, with revisions, available to one agent run. It is fixed when the run starts.

## Behavior

### Sources and pinning

- SK-1. A project's profile lists its skill sources. An external source names a repository and a commit; a branch or tag is resolved to a commit when the source is added or updated, and both are recorded (D31). The files are imported into platform storage as a content-addressed snapshot and never copied into this repository (D30).
- SK-2. A pack is imported either by a git fetch from a worker under the project policy or by a user upload; the orchestrator does not fetch it itself (D47). The import records the repository, commit and snapshot hash (new).
- SK-3. Each skill has a qualified ID, `<source>/<name>`. `name` equals the directory name and the front-matter `name`. Relative resource paths and sibling skill directories are kept as in the source, because skills link to their siblings (`../<skill>/…`) ([external skills](../EXTERNAL-SKILLS.md)).
- SK-4. If two sources define the same name, registration reports the collision. A bare name then resolves only through a preference set in the profile; qualified IDs always work (new). A fixed precedence, as Claude Code uses, would shadow skills silently and blur load evidence.
- SK-5. Changing a pin, or editing a project skill, creates a new effective skill set for runs started afterwards. A running agent keeps the set it started with, and earlier records keep their revisions (external skills, gate 7).
- SK-6. Front matter is parsed strictly. A skill without a valid `name` and `description` is excluded and reported; other fields are kept as metadata (new).

### Discovery

- SK-7. At the start of each agent run, the model receives a catalog of the run's effective skill set: qualified name and description per skill, never the body (CScience; Claude Code's public skills documentation describes the same split). The catalog has a size budget. When it is exceeded, every skill stays listed by name, descriptions are shortened deterministically, and the run records which (new). A search tool returns full catalog entries, so a shortened catalog stays usable (new).
- SK-8. A skill can declare requirements: capabilities (`sage`, `pdf`), tools and roles. The catalog marks a skill whose requirements the run's environment does not meet as unavailable, with the reason, such as a capability without a passing probe at the current revision (new). Availability comes from probe results in the `EnvironmentRecord` ([Conda provisioning](conda-provisioning.md), CP-24), never from what is installed beside the orchestrator ([product](../PRODUCT.md)).
- SK-9. Each role has its own skill set, chosen in the project profile. The reviewer's set holds only skills that need no execution, and helper code is never available to it ([consistency review](consistency-review.md)) (new).

### Loading

- SK-10. An agent loads a skill by qualified name, or by bare name when unambiguous, with a load tool. The harness returns the body from the pinned snapshot, headed by the qualified ID, revision and body SHA-256 (CScience loads by exact name after discovery).
- SK-11. Loading MUST NOT execute anything. Directives that would run commands when a skill loads are not supported; computed content comes from ordinary executions (new; Claude Code supports load-time commands, which here would run beside the orchestrator).
- SK-12. A loaded body stays in the run's context. Loading the same revision again returns a short note instead of the body. If context compaction removes a body, the harness records it, and the next load returns the full body (new).
- SK-13. Resources are read with a resource tool from the same snapshot, by a path relative to the skill, resolved within the pack's snapshot so that sibling links work. Absolute paths, paths that leave the snapshot and symbolic links pointing outside it are refused with `resource_outside_pack`. Reads have a size limit (new).
- SK-14. A skill never widens authority. Front-matter fields that ask for tools, permissions, models or network access are advisory: the harness MAY use them to narrow tools while the skill is active and MUST NOT grant anything beyond the role's tools and the project policy (new; Claude Code's `allowed-tools` pre-approves tools, which this platform does not do). Text in a skill cannot change policy, decide a permission request or approve a plan ([threat model](../THREAT-MODEL.md), A3).

### Helper code

- SK-15. Helper code MUST run only in workers, through the same tools as other code: scripts through the shell tool, Python or Sage helpers in a session kernel ([tools](tools.md), D11).
- SK-16. When a skill with helper code is loaded, the worker stages its pack snapshot in the session's environment, once per environment and pack revision: read-only, at a location the `EnvironmentRecord` declares, and exposed through an environment variable. A pack binding can name further variables, such as the one the crypto pack reads to find its scripts (new). Staging records the revision, file hashes and attempt.
- SK-17. A skill can declare kernel helpers. When an agent run loads it while that run has a kernel of the helper's language, the harness runs the helper in that kernel as an execution whose origin is the skill revision, and again after a kernel restart, before the next cell (CScience injects such helpers automatically). If injection fails, the load result says so and the body is still returned (new).
- SK-18. If a skill's requirements are unmet, its body still loads, with a notice listing them; its helper code is not staged or injected until a probe passes (new).
- SK-19. Artifacts from executions that used helper code list the skill revision and helper file hashes in their lineage ([research artifacts](research-artifacts.md)).
- SK-20. *Withdrawn by the owner on 2026-10-14; the ID is not reused.* Helper code has no route to the platform API. Only agents act on the platform, through tool calls, as in Claude Code. A helper that produces results for the project writes them to files, and an agent decides whether to act on them, for example by importing a ledger results file ([ledger](ledger.md), LG-41). The 2026-10-11 draft, with calls from the worker attributed to the execution that made them, is not built.

### Load evidence

- SK-21. The harness records, as events it produces itself (D31):
  - the catalog given to each run: its hash, its entries and any shortening;
  - each load and resource read: qualified ID, revision, SHA-256 of the content actually returned, status, tool call ID, role, model alias and resolved model;
  - each staging and helper injection.
- SK-22. Only these events establish that a skill was loaded. A model's statement, a skill name in its output or prompt, and a failed call are not load evidence (external skills; the pack's own evaluation contract says the same).
- SK-23. Each agent run keeps a skill trace of the skills it loaded, with revisions. Results and reviews link to it, so a reported result resolves to the skill revisions in effect ([feature plan](../FEATURE-PLAN.md), provenance).
- SK-24. The GUI shows, per session, the effective skill set with sources and revisions, the skills each agent run loaded, helper executions, and why a skill was unavailable (OpenScience Desktop shows loaded skills and their origin).

### External pack binding

- SK-25. The crypto-analysis-skills pack is an external source pinned to a commit, with its content kept in its own repository (D30). The user updates its host bindings to the platform's versioned API for plans, progress, delegation, questions, project knowledge, board records, artifacts and execution (D31, D32). Exact CScience function names and file layouts are not required (D32).
- SK-26. Every run records the platform API version together with the pack revision. A pack can declare the API versions it supports, and registration refuses an incompatible pack (new).
- SK-27. Other science packs use the same mechanism; there are no pack-specific code paths (external skills).

## Contracts used

- `ArtifactRef`: an imported pack is a snapshot with import attribution (repository, commit). Each loaded body and resource is a pinned reference.
- `Event`: `skill.catalog_presented`, `skill.loaded`, `skill.resource_read`, `skill.helper_staged` and `skill.helper_injected`, produced by the harness or the worker, never by the model.
- `ExecutionContext`: staging and injection are executions whose origin names the skill revision (proposed addition in the [index](README.md#proposed-contract-additions-new)).
- `EnvironmentRecord`: probe results decide availability; the staging location is a declared read-only location.
- `Mutation`: adding a source or moving a pin changes the project profile, names its expected revision and is attributed to the user.
- `Anchor`: comments and findings can point at a line of a skill revision.
- `PermissionRequest`: loading creates none. Operations an agent performs while following a skill raise the ordinary ones.

## Acceptance fixtures

The fixtures use `ss-test-skills`, a test pack written by this project. `alpha` has a body containing the marker `SS-MARKER-ALPHA-1`; `beta` links to `reference/notes.md`, `scripts/count.sh` and `../alpha/SKILL.md`; `gamma` declares the Python kernel helper `helpers/gamma.py`, which defines `gamma_square`; `needs-gp` requires the capability `gp`.

**SK-F1 Catalog without bodies** (`scripted`; SK-7, SK-21)
- *Given* `ss-test-skills` at commit C1 and a main agent run whose scripted model loads nothing.
- *Then* one catalog event lists the four qualified names with descriptions; the captured model requests contain every description and no body marker; there is no load event.

**SK-F2 Load on demand, once** (`scripted`; SK-10, SK-12, SK-21)
- *When* the model loads `alpha` twice.
- *Then* the first result contains `SS-MARKER-ALPHA-1` under a header with C1 and the body SHA-256, and one load event records status, tool call ID, role and model alias. The second result is the short note; the captured requests contain the body once.

**SK-F3 Narration is not evidence** (`scripted`; SK-22, SK-23)
- *When* the final message says the agent followed `beta`, which it never loaded.
- *Then* there is no load event for `beta`, and the run's skill trace omits it.

**SK-F4 Resources stay in the pack** (`scripted`; SK-13)
- *When* the model reads, from `beta`, `reference/notes.md`, `../alpha/SKILL.md`, `../../../../etc/passwd` and `reference/link`, a symbolic link to `/etc/hostname`.
- *Then* the first two succeed, with events recording path and SHA-256; the last two are refused with `resource_outside_pack`, and nothing outside the snapshot was read.

**SK-F5 Helper code runs in the worker** (`scripted`, `worker`; SK-15, SK-16, SK-19)
- *When* the model loads `beta` and runs `scripts/count.sh` with the shell tool through the staged path.
- *Then* the worker reports the snapshot staged read-only with file hashes; the script runs in the worker (location evidence as in [tools](tools.md)); a write into the staged location fails; the output artifact's lineage names `ss-test-skills/beta` at C1 and the script's SHA-256; the orchestrator container shows no process for it.

**SK-F6 Kernel helper injection** (`scripted`, `worker`; SK-17)
- *Given* a session with a running Python kernel.
- *When* the model loads `gamma`, runs a cell with `gamma_square(7)`, restarts the kernel and runs the cell again.
- *Then* an injection whose origin is `ss-test-skills/gamma` at C1 precedes each cell, both cells return 49, and both cells' lineage names the helper revision.

**SK-F7 Unmet requirements** (`scripted`, `worker`; SK-8, SK-18)
- *Given* an environment without a passing `gp` probe.
- *When* the model loads `needs-gp`; later the capability is installed and its probe passes ([Conda provisioning](conda-provisioning.md), CP-F1).
- *Then* the catalog marks `needs-gp` unavailable with the reason; the load returns the body with a notice and stages nothing; the next run's catalog lists it as available.

**SK-F8 Pin change** (`scripted`; SK-5)
- *When* run R1 starts at C1, the pin moves to C2 (where `alpha`'s body differs), then R1 and a new run R2 each load `alpha`.
- *Then* R1 receives the C1 body and R2 the C2 body; their load events carry different SHA-256 values; R1's artifacts still name C1.

**SK-F9 Name collision** (`scripted`; SK-4)
- *Given* the platform source and `ss-test-skills` both define `alpha`, with no preference in the profile.
- *Then* registration reports the collision; loading `alpha` fails with an ambiguity error listing both qualified IDs; loading `ss-test-skills/alpha` succeeds.

**SK-F10 Skills grant nothing** (`scripted`; SK-14)
- *Given* a skill whose front matter lists `allowed-tools: [Bash]` and whose body says to push results with git, and a delegated agent whose role has no shell tool.
- *When* the delegated agent loads it and calls the shell tool; then the main agent follows the body and runs `git push`.
- *Then* the shell call is refused with `tool_not_granted`; the push escalates under the policy and creates a permission request; the role's tools and the policy are unchanged.

**SK-F11 Load evidence per role and model** (`live`; SK-21, SK-22)
- *Given* the real pack at its pinned commit and a probe skill whose body alone carries a marker, asked for implicitly and explicitly.
- *When* the probe runs under the main, delegated and reviewer configurations, for each model alias and effort setting.
- *Then* the report gives, per configuration, discovery, load, adherence and configuration failures, counting a load only when a load event carries the probe body's SHA-256 (external skills, gate 2). The user decides which rates are acceptable.

**SK-F12 External pack end to end** (`worker`; SK-16, SK-25)
- *Given* crypto-analysis-skills registered at its pinned commit with updated bindings.
- *When* an agent discovers and loads one skill and one resource and runs one bundled helper.
- *Then* load and resource events appear as in SK-F2; the helper runs in the babbage worker; its output is a versioned artifact whose lineage names the pack commit (external skills, gates 1 and 3).

## Open questions

1. Collisions: an explicit preference in the profile (proposed, SK-4), or a fixed precedence as in Claude Code?
2. Kernel helpers: inject them when the skill loads (proposed, as CScience does), or only on an explicit call?
3. Catalog budget: how large, given the pack's 162 skills, and should large packs rely on search alone?
4. Pack import: a worker-side git fetch into project storage (proposed, SK-2), a user upload, or both?
5. *Moot (2026-10-14):* worker code calls no platform API function (SK-20 is withdrawn).
6. How does a pack declare the platform API versions it supports: a manifest file at the pack root, or front matter?
7. Should invalid front matter exclude a skill (proposed, SK-6), or should it load without metadata, as Claude Code does?

## Sources

[External skills](../EXTERNAL-SKILLS.md), [feature plan](../FEATURE-PLAN.md) (skills and provenance), [source study](../SOURCE-STUDY.md), [product](../PRODUCT.md), [architecture](../ARCHITECTURE.md), decisions D04, D11, D29–D32, D47, D56. Behavior evidence: the [Claude Science](../../.agents/docs/agent-reports/claude-science.md), [Claude Code and Desktop](../../.agents/docs/agent-reports/claude-code-desktop.md) and [OpenScience Desktop](../../.agents/docs/agent-reports/openscience-desktop.md) reports. Public: Claude Code's [skills documentation](https://code.claude.com/docs/en/skills); crypto-analysis-skills at `4089e99` (README, workbench-knowledge, investigate references and evaluation contract).
