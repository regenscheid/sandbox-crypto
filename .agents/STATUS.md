# Status

Updated 2026-10-15, main `0195f834`. This file is replaced, not appended to, and stays under 10 KB. History is in the git log, PR descriptions and [lab reports](docs/agent-reports/).

## Phase and focus

- **Phase 1 (babbage).** M1's exit run passed ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)), M2's exit checkpoint was met in [lab 39](docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md), and **the first milestone for real use is met** in [lab 40](docs/agent-reports/babbage-openshell-lab-40-2026-10-15.md).
- **Paused.** The owner is deciding how to manage the context of long runs ([Open questions](#open-questions)). Until the decision, **no new work starts**, and work in flight finishes. Waiting for it: skills K6a (#437), the skills lab and the rest of the backlog.
- **Focus before the pause:** the [revision and refactoring plan](refactor-plan/PLAN.md). Phases 0 to 4 are done apart from leftovers, and skills K1 to K5 are built. Left: skills K6 onward, compaction and the backlog, then Phase 5.
- **How work is done:** the plan's [working rules](CONVENTIONS.md#working-rules) and the [handoff](handoff.md).

## Real use (labs 40 and 41)

Both labs used the real model and crypto-research-mcp.

- **Lab 40** ran the whole path with delegation and connectors (162 skills imported). Its eight bugs are fixed.
- **Lab 41** checked what merged since. Passed: depth 2, tasks on issues, connector search, cost by role and history. The claimed-action check flagged earlier runs' true statements. Its nine bugs are fixed (#412, #414 to #416, #418 to #420, #424, #425), and a claimed fix is checked against the file it names (#422).
- **No lab has run since.** Lab 41's fixes, skills in runs and the PDF line numbering are untested with a real model.

## What works today

- **Launcher:** `sandbox-science up`, `down`, `status` and `skills update` run the worker, the run service, the workspace and the MCP proxy from one config, which also names skill packs ([quickstart](../docs/QUICKSTART.md)).
- **Infrastructure:** the policy library and middleware, the package cache, the MCP proxy, the event store and approvals.
- **Worker:** OpenShell VM sandboxes at tier 3 with persistent Python and Sage kernels, file handoff, PDF tools, background jobs and Conda and pip provisioning. Protocol 5 ([compatibility](../docs/COMPATIBILITY.md)); the control store is at version 43. `pdf_pages` and `pdf_search` number lines one way, and `pdf_pages` shows line boxes on request.
- **Harness:** the tool loop; the run service for GUI chat; an MCP client with tools loaded on demand and a failed listing tried again ([MCP connectors](../docs/MCP-CONNECTORS.md)); planning; living documents; comments; automatic consistency review, which matches narrated actions against what each run and its delegated runs did; math checks; the job watcher; recorded files; project memory ([memory](../docs/MEMORY.md)); `ask_user` ([questions](../docs/QUESTIONS.md)). Agents' writes refuse hidden characters.
- **Delegation** ([spec](../docs/specs/plans-and-delegation.md)): off by default, per project (`project delegation`). Checks (the platform writes the brief) and tasks (the parent writes it; a plan track, an outcome on an issue, result files pinned). A run tree, a cap on runs at once, limits on a runaway child and a fallback model alias. A confirmed check counts as the `independent_check`.
- **Ledger** ([ledger](../docs/LEDGER.md)): seven entry types, pinned evidence, decisions and the gate, flags, work records, and the frontier with history. The citation check confirms an agent's published result; the copied-number check compares long numbers.
- **Cost:** each call's cost as the gateway reports it, by run and by role (main, delegated, reviewer, watcher, skill embeddings). LiteLLM enforces the budget, and a refusal ends the run. A project may set a daily cost limit.
- **Skills** ([skills](../docs/SKILLS.md), spec approved): `skills import` takes a pack from git or an archive and pins it. Each run gets its project's skills: an index in the system message, the `skills` tool (search, load, read), skills loaded at every start, and a discovery notice before the first model call and each user message. The seven built-in skills are pinned at each run's start. Matching is by words and, with an embedding model, by meaning. The launcher imports a pack once and updates it on request.
- **Workspace GUI:** documents; comments; reviews and findings; plans; chat with open questions; jobs; kernels; provisioning; artifacts and a PDF viewer; the ledger; the Memory page; a session's run tree.
- **Not built:** the skills import's model pass (K6a, #437), overrides of what it derives, per-pack thresholds, helper staging and a skills page in the GUI; compaction; approvals in the GUI; execution-pinned evidence; editable figures, charts and `view_image`.
- **Tests:** 13,068 Python tests are collected, 888 of them in the slow tier, and 2,055 web tests (#433). A pull request's checks take about eight minutes.

## In progress

- **#437** (draft): K6a, the import's model pass with topics, entry skills and broken links. On hold by the owner's instruction: not reviewed or merged until the owner decides.
- **#436:** the figures spec (FG-1 to FG-43), waiting for the owner's decisions on its eight open questions.

## What comes next

1. The owner's decision on context management, from the survey under way, then compaction as decided.
2. The paused work: K6a (#437), skills K6b onward, and a lab with skills in runs to confirm labs 40 and 41's fixes.
3. The figures spec after the owner's answers, then editable figures, charts and `view_image`.
4. Other [BACKLOG](BACKLOG.md) items: questions from delegated runs, a reference's read extent (LG-15), memory proposals' basis (PM-3, PM-4), vouching for PDFs, approvals in the GUI.
5. Phase 5, which includes moving the domain packages out of the orchestrator.

## Known issues

- **Not fixed, from labs 40 and 41:**
  - `eprint_fulltext` matches single words, so a phrase may find nothing.
  - A citation check and a finding can disagree (a quote `supported`, a `fail` finding on the unit).
  - The copied-number check reads decimal numbers only, and only artifacts in the review's window.
  - A check keeps the sandbox's write tools (`write_file`, `publish`, `provision`).
  - Effect tools refuse until a run reads the plan, a finished plan included. Only the closing answer was relaxed (#420).
  - `mcp_tools` matches long queries strictly, so a fitting tool may go unlisted.
  - The weaker model errs. Labs keep it on purpose ([handoff](handoff.md#labs)).
- **Skills:** a delegated run reads its project's skills when it starts, so a pin that moves while a tree runs can give a child a newer set than its root's. Removing a pack from a project's `skills` does not unpin it.
- **Background job slots:** a session has 4, and a delegation tree's runs can take them all, leaving its main run none. It needs a decision.
- **CI stalls:** the `test (fast)` job has run until its limit with no log; #256's heartbeat names what was stuck. Rerun the job.
- **Upgrades:** the orchestrator, the workspace and the worker upgrade together. Control stores before version 2 and worker stores before version 1 are refused.

## Open questions

Only the first stops work.

- **Context management (the owner, open).** The owner's priority is strong recall and reasoning over long research projects (days, several million tokens of data); auditability and budget control come second. An offline evaluation of moving the loop onto Pydantic AI or LangChain, against extending our own loop (clearing, search, provider-native compaction, a summary), covered request size, prompt caching and integration cost. Its replay used recorded turns and no real summarizer, so it could not measure research quality. The owner uses both Claude and GPT models, so the approach must work for both. The owner prefers an existing library known to be good for long research sessions; failing that, the project follows closely how Claude Science and OpenScience manage context, rather than designing its own. A survey of libraries and a close study of those systems is under way, and the owner decides from its result. No live evaluation is planned. A prototype is on branch `spike/pydantic-ai`; the report is not in the repository.
- **Models:** the `reviewer`, `watcher` and `delegated` aliases are all `sandbox-orchestrator` for now.
- **PDF vouching:** whether the e-print server names a PDF's hash in a recorded result (asked of the owner). Until then only the user's check of the reference vouches for a PDF quote.
- **Draft specs:** [living documents](../docs/specs/living-documents.md), question 15; the figures spec (#436), eight questions.
- **Math display** ([workspace](../docs/WORKSPACE.md#math-repairs)): the backslash form before a digit; the escaped-bracket rule; macro-defining formulas; front-matter macro limits; the repair rate.
- **Images** ([images](../docs/IMAGES.md)): no registry on babbage; G6K at x86-64-v3; GPL libraries in `minimal` (an SBOM before publishing); crypto-base's compilers, R and OpenJDK; IACR LaTeX classes.
- **MCP proxy** ([MCP connectors](../docs/MCP-CONNECTORS.md)): MCP 2026-07-28 only when a server needs it; binary results withheld; whether worker code may use MCP resources, prompts and completion.
- **Event store:** installing the checkpoint timer from `examples/systemd` on babbage.
- **Upstream reports** in [`.agents/upstream/`](upstream/): whether to file them.
- **crypto-analysis-skills:** confirm the copyright holder before merging its MIT license; the import found no license.

## Known facts

- OpenShell's VM driver is experimental at v0.1.3; babbage's tier 3 depends on it (D43), and it has worked in every lab.
- babbage restricts unprivileged user namespaces through AppArmor. Claude Desktop's shells are exempt, so qualification checks run from a plain terminal or `systemd-run --user`.
