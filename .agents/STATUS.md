# Status

Updated 2026-10-15, main `326f4801`. This file is replaced, not appended to, and stays under 10 KB. History is in the git log, PR descriptions and [lab reports](docs/agent-reports/).

## Phase and focus

- **Phase 1 (babbage).** M1's exit run passed ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)), and M2's exit checkpoint was met in [lab 39](docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md). **The first milestone for real use is met** in [lab 40](docs/agent-reports/babbage-openshell-lab-40-2026-10-15.md): plan items 7 and 9, delegation and MCP connectors with tools loaded on demand.
- **Focus:** the [revision and refactoring plan](refactor-plan/PLAN.md). Phases 0 to 4 are done apart from leftovers. What is left is skills, compaction and the backlog ([below](#what-comes-next)), then Phase 5.
- **How work is done:** the plan's [working rules](CONVENTIONS.md#working-rules) and the [handoff](handoff.md).

## First real use (lab 40)

The lab ran the whole path with the real model (`sandbox-orchestrator` in every role) and crypto-research-mcp through the proxy:

- A paper found and read through the e-print tools, loaded on demand. A run's first call took 14,000 to 15,500 input tokens, against 25,000 in lab 39.
- A lemma proved in a document block, checked by a delegated run with a fresh context (`confirmed`, `complete`), linked as its independent check, and established by the main agent through the gate.
- A published result quoting the paper, shown on the frontier as confirmed by the citation check.
- A 135-digit number copied right (no finding) and with one digit changed (the platform's finding).
- Two questions to the user, answered by a button and in the user's own words.
- The gateway's costs, and the import of 162 skills, pinned to the project.

It found eight bugs, all fixed: B1, no check could be delegated with the owner's gateway (#394); B2, an agent's rebuttal came back as a notice and looped (#397); B3, the reviewer could not see what an established entry rests on (#402, #407); B4, an `mcp_tools` query loaded unrelated tools (#401); B5, the reviewer's cost was not counted (#403, #408); B6 to B8, a check's record, the lab config script and the category form's hint (#400, #404). The reviewer also matches narrated actions against what each run did (#406, #409, #411). **No lab has run since**, so B3, B4 and the narrated-action check are untested with a real model.

## What works today

- **Launcher:** `sandbox-science up`, `down` and `status` run the worker, the run service, the workspace and, with `[mcp_proxy]`, the MCP proxy in its own sandbox, from one config ([quickstart](../docs/QUICKSTART.md)).
- **Infrastructure:** the policy library and middleware, the package cache, the MCP proxy, the hash-chained event store, and one approval state machine.
- **Worker:** OpenShell VM sandboxes at tier 3 with persistent Python and Sage kernels (per delegated run too), file handoff, PDF tools, background jobs and Conda and pip provisioning. Protocol 5 ([compatibility](../docs/COMPATIBILITY.md)); the control store is at version 40.
- **Harness:** the tool loop; the run service for GUI chat; an MCP client with an index and tools loaded on demand ([MCP connectors](../docs/MCP-CONNECTORS.md)); planning; living documents; comments; automatic consistency review; math checks; the job watcher; files calls write, recorded as artifacts; project memory ([memory](../docs/MEMORY.md)); `ask_user`, which asks the user a question ([questions](../docs/QUESTIONS.md)). Agents' writes refuse hidden characters (threat model A3).
- **Delegation** ([spec](../docs/specs/plans-and-delegation.md)): off by default, per project (`project delegation --depth 1` or `2`). Checks (the platform writes the brief; the result has a verdict, coverage and any refutation) and tasks (the parent writes the brief; a track of the session plan, an outcome on an issue, result files pinned). Depth 2 lets a task delegate tasks once more. A run tree with handbacks to the direct parent, a cap on runs at once (10 by default), token and turn limits as bounds on a runaway child, a fallback model alias, and a dead tree ended on restart. A confirmed check counts as the `independent_check`.
- **Ledger** ([ledger](../docs/LEDGER.md)): seven entry types, evidence with pinned targets (quotes and delegation outcomes too), decisions and the gate, flags, work records, and the frontier with categories, best known results, `dominates` links and history. The reviewer's citation check confirms an agent's published result; the platform's copied-number check compares long numbers with the artifacts before the model reviews.
- **Cost:** each model call's cost as the gateway reports it, shown per run and by role (main, delegated, reviewer, watcher). There is no project token budget: LiteLLM enforces upstream, and a refusal ends the run (`gateway_budget`). A project may set a daily cost limit, off by default.
- **Skills:** `skills import` takes a pack from git or an archive and pins it ([skills](../docs/SKILLS.md)). Runs do not see skills yet.
- **Workspace GUI:** documents; comments; reviews and findings; plans; chat with open questions; jobs; kernels; provisioning; the artifact list and a PDF viewer; the ledger's board, issue list, entry view (evidence, quotes and their checks, work records, the gate, flags) and frontier pages with history; the Memory page; a session's run tree; the cost by role.
- **Not built:** skills in runs (index, search, discovery); compaction; approvals in the GUI; execution-pinned evidence; editable figures, charts and `view_image`. The pack import is not planned.
- **Tests:** 12,953 Python tests are collected, 879 of them in the slow tier, and 2,001 web tests (at #411). A pull request's checks take about four minutes.

## In progress

- **Skills spec** ([draft](../docs/specs/skills.md), #373, unmerged): revised to the owner's answers of 2026-10-15, and **waiting for the owner on two questions:** which built-in skills the platform ships, and how a pack's skill names that collide with a built-in skill are handled (question 15); and the system message's skill sections (question 16). Four drafted values need a yes: the index budget, the discovery notice's size and wait, what the import's model pass reads, and what is embedded. K1 (import and pinning, #376) is built; K2 (the index, search and loading) and K3 (discovery) wait for the answers.
- Nothing else is open.

## What comes next

1. Skills K2 and K3 after the owner answers, the owner's priority.
2. A lab to confirm lab 40's fixes, with skills once K2 lands.
3. Compaction: a conversation over the limit stops with `context_limit`.
4. Other [BACKLOG](BACKLOG.md) items: questions from delegated runs, a reference's read extent (LG-15), a basis for memory proposals (PM-3, PM-4), vouching for PDFs, charts as text and `view_image`, approvals in the GUI.
5. Phase 5, which includes moving the domain packages out of the orchestrator.

## Known issues

- **Not fixed, from lab 40:**
  - `eprint_fulltext` matches single words, so a phrase may find nothing (the server's behavior).
  - A citation check and a finding can disagree: a quote `supported` beside a `fail` finding that it does not support the unit.
  - The copied-number check reads decimal numbers, not names, hexadecimal or scientific forms.
  - A check keeps the sandbox's write tools (`write_file`, `publish`, `provision`); it changes no entry, document or memory.
  - The weaker model errs: stray tool calls, claimed edits it did not make. Labs keep it on purpose ([handoff](handoff.md#labs)).
- **Background job slots:** a session has 4, and a delegation tree's runs can take them all, leaving its main run none. It needs a decision.
- **CI stalls:** the `test (fast)` job has run until its limit with no log; #256 added a heartbeat that names what was stuck. Rerun the job.
- **Upgrades:** the orchestrator, the workspace and the worker upgrade together. Control stores before version 2 and worker stores before version 1 are refused.

## Open questions

Each has a recommendation; none blocks current work. The skills questions are under In progress.

- **Models:** the config names `reviewer`, `watcher` and `delegated` aliases, all `sandbox-orchestrator` for now.
- **PDF vouching:** whether the e-print server names a PDF's hash in a recorded result (asked of the owner). Until then a PDF quote confirms a result only through the user's check of the reference.
- **Draft specs:** [living documents](../docs/specs/living-documents.md), question 15.
- **Math display** ([workspace](../docs/WORKSPACE.md#math-repairs)): the backslash form for `\(x\)` before a digit; the escaped-bracket rule; macro-defining formulas as written; front-matter macro limits; 60 model repairs per browser session per hour.
- **Images** ([images](../docs/IMAGES.md)): no registry on babbage; G6K at x86-64-v3; GPL runtime libraries in `minimal`, with an SBOM before publishing; crypto-base's compilers, R and OpenJDK; IACR LaTeX classes.
- **MCP proxy** ([MCP connectors](../docs/MCP-CONNECTORS.md)): MCP 2026-07-28 only when a server needs it; binary results withheld; whether worker code may use `resources/read`, `prompts/get` and `completion/complete`.
- **Event store:** installing the checkpoint timer from `examples/systemd` on babbage.
- **Upstream reports** in [`.agents/upstream/`](upstream/): whether to file them.
- **crypto-analysis-skills:** confirm the copyright holder before merging its MIT license; the import found no license.

## Known facts

- OpenShell's VM driver README calls the driver experimental at v0.1.3; babbage's tier 3 depends on it (D43), and it has worked in every lab.
- babbage restricts unprivileged user namespaces through AppArmor. Claude Desktop's shells are exempt, so qualification checks run from a plain terminal or `systemd-run --user`.
