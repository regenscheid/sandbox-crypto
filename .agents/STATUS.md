# Status

Updated 2026-10-16, main `4743cafb`. This file is replaced, not appended to, and stays under 10 KB. History is in the git log, PR descriptions and [lab reports](docs/agent-reports/).

## Phase and focus

- **Phase 1 (babbage).** M1's exit run passed ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)), M2's exit checkpoint was met in [lab 39](docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md), and **the first milestone for real use is met** in [lab 40](docs/agent-reports/babbage-openshell-lab-40-2026-10-15.md).
- **Context management is decided and being built.** The owner approved the [spec](../docs/specs/context-management.md) on 2026-10-15 (#439): folds of older work, a 600,000-token default ceiling, one design for GPT and Claude. The pause on new work is lifted. The build plan is in the [BACKLOG](BACKLOG.md#repository).
  - **Done:** (a) the archive and history tools, (b1) the ledger's research-state fields, (b2) provenance, (b3) the read register, (c) the GPT adapter, (d) the Claude adapter, (e1) token accounting and the session view, and (g) the review's archive positions. The GUI shows the new ledger fields (#463).
  - **In progress:** (f1) the fold engine and (e2) the state block's digest.
  - **Next:** (f2) overviews and marker retention, (f3) fold proposals, then lab round 1, the acceptance condition: nine continuity probes on both model families.
  - **Gateway:** it runs LiteLLM 1.102.1. Claude thinking replay stays off until a stable 1.107.0 is installed and the live check (CM-F30) passes against its image.
- **Skills:** K6a and K6b are built. K7 waits for the owner ([Open questions](#open-questions)).
- **How work is done:** the plan's [working rules](CONVENTIONS.md#working-rules) and the [handoff](handoff.md).

## Real use (labs 40 to 42)

All three used the real model and crypto-research-mcp.

- **Lab 40** ran the whole path with delegation and connectors (162 skills imported). Its eight bugs are fixed.
- **Lab 41** checked what merged since. Its nine bugs are fixed, and a claimed fix is checked against the file it names (#422).
- **Lab 42** ([report](docs/agent-reports/babbage-openshell-lab-42-2026-10-16.md)) ran skills in real runs. Import, pins, the index, `load`, `read`, the 100,000-character cap, discovery and built-ins passed. Embeddings never worked (B1), reviews lacked plan changes and a delegated run's output (B2, B3), and five smaller bugs. All eight are fixed (#445 to #449).
- **No lab has run since.** Lab 42's fixes, the archive, both adapters and the session view are untested with a real model.

## What works today

- **Launcher:** `sandbox-science up`, `down`, `status` and `skills update` run the worker, the run service, the workspace and the MCP proxy from one config, which also names skill packs and model APIs ([quickstart](../docs/QUICKSTART.md)).
- **Infrastructure:** the policy library and middleware, the package cache, the MCP proxy, the event store and approvals.
- **Worker:** OpenShell VM sandboxes at tier 3 with persistent Python and Sage kernels, file handoff, PDF tools, background jobs and Conda and pip provisioning. Protocol 5 ([compatibility](../docs/COMPATIBILITY.md)); the control store is at version 51.
- **Harness:** the tool loop; the run service for GUI chat; an MCP client with tools loaded on demand ([MCP connectors](../docs/MCP-CONNECTORS.md)); planning; living documents; comments; automatic consistency review, which matches narrated actions against what each run, its delegated runs and earlier runs did; math checks; the job watcher; recorded files; project memory ([memory](../docs/MEMORY.md)); `ask_user` ([questions](../docs/QUESTIONS.md)).
- **Context** ([spec](../docs/specs/context-management.md)): every message of a session is archived with an ID header, and `history_search` and `history_read` get exact values back. A run's view holds the earlier runs' messages as sent, with token accounting; until folds exist it leaves out the oldest and keeps the user's words. GPT (`/v1/responses`) and Claude (`/v1/messages`) clients are chosen per alias; every alias uses chat completions until lab round 1.
- **Delegation** ([spec](../docs/specs/plans-and-delegation.md)): off by default, per project. Checks and tasks, a run tree, limits on a runaway child and a fallback model alias. A confirmed check counts as the `independent_check`.
- **Ledger** ([ledger](../docs/LEDGER.md)): seven entry types, pinned evidence, decisions and the gate, flags, work records and the frontier. Issue kinds, reopen conditions, caveats, standing labels, a basis and anchors on every change, and each reference's read extent.
- **Cost:** each call's cost as the gateway reports it, by run and by role. LiteLLM enforces the budget, and a refusal ends the run.
- **Skills** ([skills](../docs/SKILLS.md)): import from git or an archive, pinned per project, with the import's model pass (topics, entry skills, short descriptions) and a project's overrides of it. Runs get an index, the `skills` tool, start loads, discovery notices and the seven built-ins. Matching is by words and, with an embedding model, by meaning.
- **Workspace GUI:** documents, comments, reviews, plans, chat, jobs, kernels, provisioning, artifacts and a PDF viewer, the ledger with its research state, the Memory page, a session's run tree.
- **Not built:** folds and the digest (in progress), K7 helper staging and bindings, per-pack thresholds, a skills page in the GUI, approvals in the GUI, execution-pinned evidence, editable figures, charts and `view_image`.
- **Tests:** 13,313 Python tests are collected, 888 of them in the slow tier, and 2,070 web tests (#463). A pull request's checks take about eight minutes.

## In progress

- **#459 and #461:** the fold engine, built by code (f1, first part) and its enrichment call with checks (second part).
- **#465:** the state block's digest (e2).
- **#436:** the figures spec (FG-1 to FG-43), waiting for the owner's decisions.

## What comes next

1. Context management: finish (f1) and (e2), then (f2), (f3) and lab round 1.
2. A lab with skills in runs after the fixes, and K7 once the owner chooses its allow rule.
3. The figures spec after the owner's answers, then editable figures, charts and `view_image`.
4. Other [BACKLOG](BACKLOG.md) items: questions from delegated runs, memory proposals' basis (PM-3, PM-4), vouching for PDFs, approvals in the GUI.
5. Phase 5, which includes moving the domain packages out of the orchestrator.

## Known issues

- **Not fixed, from labs 40 to 42:**
  - `eprint_fulltext` matches single words, so a phrase may find nothing.
  - A citation check and a finding can disagree.
  - The copied-number check reads decimal numbers only, and only artifacts in the review's window.
  - A check keeps the sandbox's write tools.
  - Effect tools refuse until a run reads the plan, a finished plan included.
  - `mcp_tools` matches long queries strictly.
  - An import's pass may derive no short description for long ones.
  - The weaker model errs. Labs keep it on purpose ([handoff](handoff.md#labs)).
- **Skills:** a delegated run reads its project's skills when it starts, so a moved pin can give a child a newer set than its root's. Removing a pack from a project's `skills` does not unpin it.
- **Background job slots:** a session has 4, and a delegation tree can take them all. It needs a decision.
- **CI stalls:** `test (fast)` has run until its limit with no log. Rerun the job.
- **Upgrades:** the orchestrator, the workspace and the worker upgrade together. Control stores before version 2 and worker stores before version 1 are refused.

## Open questions

None stops work in progress.

- **Response loop in reviews (the owner).** One reviewer error can still cost many author calls (lab 42: 1.6 million input tokens). **A:** a rebuttal that cites pinned evidence ends its finding's part in the turn, and a review that rejects it leaves the finding `unaddressed` for the user. **B:** a `fail` the turn never delivered reaches the next run as a count, not an interrupting notice. Put to the owner in #449.
- **K7's allow rule for bindings (the owner).** Staging sets the environment variables the pass derived, and a deny-list alone cannot stop a hostile pack. **1:** only variables named for the pack. **2:** the user approves each variable. **3:** both. Recommended: 3 (#450).
- **Figures spec (#436):** eight questions. **Skills questions 12 to 14** stay recorded as the coordinator's acceptance (#421, #438); the owner's own acceptance is unconfirmed.
- **Models:** the `reviewer`, `watcher` and `delegated` aliases are all `sandbox-orchestrator` for now.
- **PDF vouching:** whether the e-print server names a PDF's hash in a recorded result. Until then only the user's check vouches for a PDF quote.
- **Living documents:** question 15.
- **Math display** ([workspace](../docs/WORKSPACE.md#math-repairs)): the backslash form before a digit; the escaped-bracket rule; macro-defining formulas; front-matter macro limits; the repair rate.
- **Images** ([images](../docs/IMAGES.md)): no registry on babbage; G6K at x86-64-v3; GPL libraries in `minimal`; crypto-base's compilers, R and OpenJDK; IACR LaTeX classes.
- **MCP proxy:** MCP 2026-07-28 only when a server needs it; binary results withheld; MCP resources, prompts and completion for worker code.
- **Event store:** installing the checkpoint timer from `examples/systemd` on babbage.
- **Upstream reports** in [`.agents/upstream/`](upstream/): whether to file them.
- **crypto-analysis-skills:** confirm the copyright holder before merging its MIT license.

## Known facts

- OpenShell's VM driver is experimental at v0.1.3; babbage's tier 3 depends on it (D43), and it has worked in every lab.
- babbage restricts unprivileged user namespaces through AppArmor. Claude Desktop's shells are exempt, so qualification checks run from a plain terminal or `systemd-run --user`.
