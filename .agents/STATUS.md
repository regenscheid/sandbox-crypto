# Status

Updated 2026-10-14, main `959267c`. This file is replaced, not appended to, and stays under 10 KB. History is in the git log, PR descriptions and [lab reports](docs/agent-reports/).

## Phase and focus

- **Phase 1 (babbage), toward milestone M2.** M1's exit run passed ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)).
- **Focus:** the [revision and refactoring plan](refactor-plan/PLAN.md). Phases 0 to 3 are done apart from leftovers, and Phase 4, the M2 exit path, is under way.
- **How work is done:** see the plan's [working rules](CONVENTIONS.md#working-rules) and the [handoff](handoff.md).

## What works today

- **Infrastructure:** the policy library, the policy middleware, the package cache, the MCP proxy with `compile-policy`, the hash-chained event store, and one approval state machine (`resolve`) for the middleware and the proxy.
- **Worker:** OpenShell VM sandboxes at tier 3 on babbage, with probed isolation tiers; shell calls, persistent Python and Sage kernels (also per delegated run), file handoff, PDF tools and background jobs. An executor daemon keeps executions and kernels through worker restarts and pushes events to the worker. One worker runs per state directory. Worker API protocol 5 ([compatibility](../docs/COMPATIBILITY.md)); a call's result lists the files it deleted; `manager.py` is a 543-line facade.
- **Provisioning (D84):** Conda and pip installs as queued operations with one permission request per install, AGPL refusal, revisions with lockfiles, explicit named Conda upgrades and a recovery fence. Restoring a revision, uninstall, pip upgrade and disk limits are not built.
- **Harness:** the tool loop with a scripted or OpenAI-compatible model (retries, a context limit, token usage); `sandbox-science-harness serve`, the run service that answers GUI chat; an MCP client through the proxy; planning with live progress; living documents; comments and work requests; automatic consistency review; math checks of the current revision; the job watcher. The project ledger ([ledger](../docs/LEDGER.md)) has entries of seven types, typed links, `ledger_*` tools for the agent and a results-file import. The control store (`control.sqlite3`) is at schema version 18.
- **Workspace GUI:** login; documents with editing, conflicts and history; comments; user-grade review and finding views; session plans; a chat panel with the run's events as they are written; transcripts; jobs; kernels; provisioning; math rendering; the project's artifact list and a PDF viewer on a second origin.
- **A real model:** a first live call through the owner's gateway worked on 2026-10-14. The alias `sandbox-orchestrator` serves every role.
- **crypto-research-mcp:** the Access service token alone is enough. It sees 72 tools from seven servers. Nothing has called it through the proxy yet.
- **Specified, not built:** project memory and the automatic recording of executions' files and delegated runs' result files (both approved, #251).
- **Not built:** the ledger's decisions and gate, evidence, frontier, flags, statement math checks, pack import and GUI views (the views are in review); delegation (the run tree's parent link and per-run kernels exist; nothing starts delegated runs), approvals in the GUI (the CLI answers them), compaction of long conversations.
- **Tests:** 11,748 Python tests are collected, 835 of them in the slow tier. CI's full run at #256 passed 11,606 (53 opt-in skips) on 3.12 and 3.13, with 854 web tests. A pull request's checks take about four minutes.

## Refactoring plan

| Phase | State |
| --- | --- |
| 0. Stop the bleeding | Done |
| 1. Test seams | Done; F-04 step 4 (the fast tier's wall-clock waits) is left |
| 2. Core seams, five lanes | Done apart from leftovers; lane W's launcher is in progress |
| 3. Shared building blocks | Done apart from `Anchor` and `Mutation`, which wait for the ledger |
| 4. The M2 exit path | Under way (below) |

The plan's [progress section](refactor-plan/PLAN.md#progress) lists each finding with its PR. Phase 2's measures: `_run_task` is 55 lines; no source file disables private-usage checks; the control store has one schema version; `manager.py` is 543 lines; the workspace has one handler pattern.

- **Lane S leftovers:** B-10 for the findings tables and the prompt-provenance columns; B-14's per-thread reads. The ledger backend's first four steps are merged (#252 to #255).
- **Lane W leftovers:** C-08 for executor endpoints, recycles and kernels, and versions for the worker's other databases. `executor.py` still holds the kernel class and the protocol framing.
- **Lane G leftovers:** typed answers for the review, finding, math-check, comment and job routes (ISO times, real booleans). The ledger views are #259.

## Open work

- **Ledger views,** lane G: the read-only board and entry view (#259), then the issue list and an entry's history (#260, stacked on #259).
- **Ledger backend,** lane S: next are decisions and the gate, evidence, the frontier, flags, statement math checks and the pack import.
- **File recording,** lane O: the worker reports deleted files (#258, merged); the harness's recorder, the artifact list and the project settings are next.
- **Project memory:** specified, not started.
- **Local launcher,** lane W: `session open` (#261) is the first of its stack.

## M2 exit path

| Step | State |
| --- | --- |
| Chat in the GUI with a live transcript | Built |
| A real model through the owner's gateway | The call works; a whole run with a real model is next |
| Ledger and board ([spec](../docs/specs/ledger.md)) | The store, links, tools and read routes are built; the views are in review |
| User-grade review views (E-11) | Built |
| PDF viewer on a second origin (E-08) | Built |
| M2 exit lab on babbage | After the steps above, and after a lab for the worker changes since lab 36 |

## Known issues

- **CI stalls.** The `test (fast)` job sometimes ran until its limit with no log (six runs on 2026-10-14). #256 added per-test limits, a deadline that prints the run, a minute-by-minute heartbeat and stack dumps. The cause is not found; if it recurs, the log names what was stuck.
- **Flaky tests.** The run service's lost-lease test is fixed (#231), and so is a backoff test that failed on #251's CI (#257).
- **Worker changes untested on OpenShell.** No lab has run since lab 36. The executor's files moved into siblings (C-16), kernels use abstract Unix sockets, the executor collects zombies, the daemon pushes events, and kernels are per run. The PR descriptions list what the next lab checks.
- **Repeat runs fill `/tmp`:** `--count 50` keeps every test's temporary directory; use `-o tmp_path_retention_policy=failed`.
- **Upgrades:** the orchestrator, the workspace and the worker upgrade together (protocol 5). Control stores from before version 2 and worker stores from before version 1 are refused; export documents first ([living documents](../docs/LIVING-DOCUMENTS.md)). Project JSON files are not read.

## Blocked on the owner

- Nothing blocks the model: the gateway key works.
- Later, for MCP: the owner registers the OpenShell provider for the proxy's sandbox before a first call through the proxy ([MCP connectors](../docs/MCP-CONNECTORS.md#crypto-research-mcp-a-cloudflare-access-service-token)).
- The decisions below.

## Open questions

Each has a recommendation; none blocks the current work.

- **Draft specs:** [skills](../docs/specs/skills.md) and [living documents](../docs/specs/living-documents.md) (question 15). [Plans and delegation](../docs/specs/plans-and-delegation.md) questions 6 and 7.
- **Math display choices:** LD-41's backslash form for `\(x\)` before a digit; the escaped-bracket rule; formulas that define macros shown as written; front-matter macro limits; 60 model repairs per browser session per hour ([workspace](../docs/WORKSPACE.md#math-repairs)).
- **Images** ([images](../docs/IMAGES.md)): no registry on babbage for now; G6K at x86-64-v3; GPL runtime libraries in `minimal`, with an SBOM before publishing; keep crypto-base's compilers, R and OpenJDK; projects may carry the IACR LaTeX classes.
- **MCP proxy** ([MCP connectors](../docs/MCP-CONNECTORS.md)): MCP 2026-07-28 only when a server needs it; binary results withheld until artifact references exist; keep `max_argument_bytes`; whether worker code may use `resources/read`, `prompts/get` and `completion/complete`.
- **Event store:** installing the checkpoint timer from `examples/systemd` on babbage ([event store](../docs/EVENT-STORE.md#checkpoints-in-the-journal)).
- **Upstream reports** in [`.agents/upstream/`](upstream/): whether to file the OpenShell, libzmq, KaTeX and cabal-install descriptions.
- **crypto-analysis-skills:** confirm the copyright holder before merging its MIT license.

## Known facts

- OpenShell's VM driver README calls the driver experimental at v0.1.3; babbage's tier 3 depends on it (D43), and it has worked in every lab.
- babbage restricts unprivileged user namespaces through AppArmor. Claude Desktop's shells are exempt, so qualification checks run from a plain terminal or `systemd-run --user`.

## Last five merges

- #258 `959267c`: the worker reports the files a call deleted (RA-33, protocol 5).
- #251: the specs for file recording and project memory, approved.
- #257: the run service's backoff test.
- #255: the ledger's read-only routes with typed answers (LG-34).
- #254: the agent's ledger tools and results files (LG-41); #252 and #253, the ledger's store and links, merged just before.
