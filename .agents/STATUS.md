# Status

Updated 2026-10-15, main `b8487377`. This file is replaced, not appended to, and stays under 10 KB. History is in the git log, PR descriptions and [lab reports](docs/agent-reports/).

## Phase and focus

- **Phase 1 (babbage).** M1's exit run passed ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)). M2's exit checkpoint was met in [lab 39](docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md), as a testing checkpoint.
- **Next:** the first milestone for real use, which is plan items 7 and 9: delegation and MCP connectors ([below](#what-comes-next)).
- **Focus:** the [revision and refactoring plan](refactor-plan/PLAN.md). Phases 0 to 3 are done apart from leftovers. Phase 4's M2 exit path is done; its items 7 and 9 and the frontier stack are open.
- **How work is done:** the plan's [working rules](CONVENTIONS.md#working-rules) and the [handoff](handoff.md).

## M2 exit checkpoint

| Lab | Result |
| --- | --- |
| [37](docs/agent-reports/babbage-openshell-lab-37-2026-10-15.md) (#289) | The launcher, protocol 5 and a real-model chat passed. Two bugs fixed (#287, #288) |
| [38](docs/agent-reports/babbage-openshell-lab-38-2026-10-15.md) (#305) | Not met: reviews rarely completed, the entry view hid evidence, the paper came from arXiv. Fixed in #295, #297 to #299, #303, #312 to #316 and #319 |
| [39](docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md) (#331) | **Met, with conditions** |

Lab 39 ran the path with the real model and the MCP connector: a paper through the e-print tools, Sage and Python, a document with 21 checked formulas, a comment that woke the idle session, seven completed reviews, and the board with pinned evidence and the gate's answer ("5 of 6 clauses hold"). Two workbooks shared a container with distinct kernels, and a third ran in its own. Establishing a result was not reached; M2 does not ask for it.

The conditions it listed: #327 to #330 had to merge (they have), and two bugs stay open, B5 and B6 ([known issues](#known-issues)).

## What comes next

1. **Delegation** (plan item 7, [spec](../docs/specs/plans-and-delegation.md)): delegated runs as roles of the one loop. The run tree's parent link and kernels per run exist; nothing starts delegated runs. First piece, the owner's choice: a "check this claim" delegation, a fresh-context run whose report is the ledger's `independent_check`. Until then the user checks proved results.
2. **MCP connectors for real research** (item 9): the client, the proxy, the launcher's proxy sandbox and the provider registered from the owner's file all work, and lab 39 made real calls. B5 comes first, since the connector's tools do not fit the daily budget; its fix is in review.
3. Then Phase 5, which includes moving the domain packages out of the orchestrator, and the after-M2 backlog (questions to the user, editable figures, Vega-Lite and Mermaid charts, `view_image`).

## What works today

- **Launcher:** `sandbox-science up`, `down` and `status` run the worker, the run service, the workspace and, with `[mcp_proxy]`, the MCP proxy in its own OpenShell sandbox, from one config ([quickstart](../docs/QUICKSTART.md)). `down` suspends the research sandboxes. `up` registers the MCP provider from the owner's file.
- **Infrastructure:** the policy library and middleware, the package cache, the MCP proxy with `compile-policy`, the hash-chained event store, and one approval state machine.
- **Worker:** OpenShell VM sandboxes at tier 3, with persistent Python and Sage kernels (per delegated run too), file handoff, PDF tools and background jobs. One worker per state directory. Protocol 5 ([compatibility](../docs/COMPATIBILITY.md)). Conda and pip provisioning as queued operations (D84); restoring a revision, uninstall and disk limits are not built.
- **Harness:** the tool loop with an OpenAI-compatible model; the run service for GUI chat, where a comment to an idle session starts a run and the model budget is kept at a run's safe points (#333); an MCP client; planning with live progress; living documents; comments and work requests; automatic consistency review, which reads the ledger; math checks of documents, comments and ledger statements; the job watcher; the project memory ([memory](../docs/MEMORY.md)), given to every role; and the files calls write, recorded as artifacts, with `find_artifacts` ([artifacts](../docs/ARTIFACTS.md)). Agents' writes refuse hidden characters (threat model A3).
- **Ledger** ([ledger](../docs/LEDGER.md)): seven entry types, typed links, evidence with pinned targets, decisions and the gate, flags for reconsideration, statement math checks, `ledger_*` tools and a results-file import. The control store (`control.sqlite3`) is at schema version 25.
- **Workspace GUI:** documents with editing and history; comments; review and finding views; plans; chat with the run's events as written; jobs; kernels; provisioning; math; the artifact list and a PDF viewer on a second origin; the ledger's board, issue list, entry view (evidence, findings, flags, the gate, "I checked this revision") and history; the Memory page. Hidden characters show as code points, and rendered text gets a banner.
- **Model and connector:** the gateway's alias `sandbox-orchestrator` serves every role. crypto-research-mcp's Access token alone is enough; it lists 72 tools from seven servers.
- **Not built:** the frontier, the pack import and export, work records, execution-pinned evidence; delegation; questions to the user; approvals in the GUI; skills; compaction of long conversations.
- **Tests:** 12,417 Python tests are collected, 879 of them in the slow tier, and 1,379 web tests (at #333). A pull request's checks take about four minutes.

## In progress

- **The frontier stack,** lane S: #304 (categories, migration 26), #310 (best known results), #321 (`dominates` links), #323 (history, migration 27), each stacked on the one before. An Opus review found three high defects: a disputed result was shown with measures it was never eligible on, a cap could push recorded results out of the read, and an agent's published result counted as verified. The fixes are under way in the stack, and #326, off main, makes an entry leave `refuted`, `withdrawn` or `superseded` only by a decision.
- **B5,** lane O, in three stacked parts: #332 (a listing leaves out the tools the policy denies), #334 (the tools as an index, loaded on demand) and #335 (the launcher's settings for the index). The index is about 1,200 to 1,800 tokens against 17,000.
- **Queued,** lane S: B6, after the frontier.

## Known issues

- **B5, high: the connector's tools are in every model call.** All 72 tools are about 17,000 tokens a call, and a run's calls cost about 25,000 tokens with them against 13,000 without. A project's 2,000,000-token day does not cover the path twice.
- **B6, medium: nothing checks a number the model copied** against the computation that holds it. In lab 39 a 135-digit value was copied wrongly into the document and the ledger, and the reviews compared the two with each other. Queued for lane S.
- **Flaky test:** `test_a_killed_service_leaves_a_run_the_next_one_ends_interrupted` in `test_run_service.py`. Rerun the job.
- **CI stalls:** the `test (fast)` job sometimes ran until its limit with no log; #256 added a heartbeat that names what was stuck.
- **Lab 39, not fixed:** `eprint_fulltext` found no passage, and `R-1` stayed proposed: the agent read a structural refusal as the gate's answer.
- **Repeat runs fill `/tmp`:** use `-o tmp_path_retention_policy=failed`, and never put `--basetemp` in `/tmp`.
- **Upgrades:** the orchestrator, the workspace and the worker upgrade together (protocol 5). Control stores before version 2 and worker stores before version 1 are refused; export documents first ([living documents](../docs/LIVING-DOCUMENTS.md)).

## Open questions

Each has a recommendation; none blocks the current work, and the gateway key and the Access token work.

- **Per-role model aliases:** `sandbox-orchestrator` serves MAIN, the reviewer and the watcher until the owner sets one per role.
- **Draft specs:** [skills](../docs/specs/skills.md) and [living documents](../docs/specs/living-documents.md) (question 15); [plans and delegation](../docs/specs/plans-and-delegation.md) questions 6 and 7.
- **Math display** ([workspace](../docs/WORKSPACE.md#math-repairs)): LD-41's backslash form for `\(x\)` before a digit; the escaped-bracket rule; macro-defining formulas shown as written; front-matter macro limits; 60 model repairs per browser session per hour.
- **Images** ([images](../docs/IMAGES.md)): no registry on babbage for now; G6K at x86-64-v3; GPL runtime libraries in `minimal`, with an SBOM before publishing; keep crypto-base's compilers, R and OpenJDK; projects may carry the IACR LaTeX classes.
- **MCP proxy** ([MCP connectors](../docs/MCP-CONNECTORS.md)): MCP 2026-07-28 only when a server needs it; binary results withheld; whether worker code may use `resources/read`, `prompts/get` and `completion/complete`.
- **Event store:** installing the checkpoint timer from `examples/systemd` on babbage.
- **Upstream reports** in [`.agents/upstream/`](upstream/): whether to file them.
- **crypto-analysis-skills:** confirm the copyright holder before merging its MIT license.

## Known facts

- OpenShell's VM driver README calls the driver experimental at v0.1.3; babbage's tier 3 depends on it (D43), and it has worked in every lab.
- babbage restricts unprivileged user namespaces through AppArmor. Claude Desktop's shells are exempt, so qualification checks run from a plain terminal or `systemd-run --user`.

## Last five merges

- #333: the run service keeps the model budget at a run's safe points. #331: the lab 39 report.
- #330: `find_artifacts` finds a file by the path it lists. #329: `up` moves a project to a changed policy.
- #328: crypto-research-mcp's tools have no prefix in the example policy.
