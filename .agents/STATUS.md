# Status

Updated 2026-10-14, main `96380a4`. This file is replaced, not appended to, and stays under 10 KB. History is in the git log, PR descriptions and [lab reports](docs/agent-reports/).

## Phase and focus

- **Phase 1 (babbage), toward milestone M2.** M1's exit run passed ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)).
- **Focus:** the [revision and refactoring plan](refactor-plan/PLAN.md), Phase 2 (the core seams), in five parallel lanes. Feature work on the M2 exit path resumes in Phase 4.
- **How work is done:** see the plan's [working rules](CONVENTIONS.md#working-rules) and the [handoff](handoff.md).

## What works today

- **Infrastructure:** the policy library, the policy middleware in front of OpenShell, the package cache (PyPI, conda, Cargo, Go, npm), the MCP proxy and the hash-chained event store.
- **Worker:** OpenShell VM sandboxes at tier 3 on babbage, with probed isolation tiers; shell calls, persistent Python and Sage kernels, file handoff and PDF tools; background jobs; an executor daemon that keeps executions and kernels through worker restarts; idle suspension and resume; on-demand host admission. Worker API protocol 3, checked on every request and response ([compatibility](../docs/COMPATIBILITY.md)).
- **Provisioning (D84):** Conda and pip installs as queued operations with one permission request per install, AGPL refusal, environment revisions with lockfiles, explicit named Conda upgrades, installer observations and events, and a recovery fence after uncertain installs. Restoring a revision, uninstall, pip upgrade and disk limits are not built.
- **Harness (CLI):** the tool loop with a scripted or OpenAI-compatible model, built through `orchestrator/harness.py`; projects, sessions and runs in the SQLite control store (schema version 3); the planning phase with live progress (D86); living documents (D85); comments and work requests; automatic consistency review; mechanical math findings (D82); the low-cost job watcher; a stateless kernel check before each turn.
- **Workspace GUI:** login; documents with editing, conflicts and history; comments; review findings; session plans with editing; finished runs' transcripts; jobs; the kernel panel; the provisioning view; math rendering with repairs. It reads runs the CLI made and cannot start one.
- **Not built:** chat in the GUI, a run with a real model, the ledger and board, the PDF viewer, approvals in the GUI (the policy middleware's CLI answers them), an MCP client in the harness, the connector auth service, delegation (archived on `archive/delegation-2026-10`, to be rebuilt in Phase 4).
- **Tests:** about 11,100 Python tests (11,041 passed, 44 opt-in skips at #161's full run) and 669 web tests; CI runs Python 3.12 and 3.13, serially, and the web checks.

## Refactoring plan

| Phase | State |
| --- | --- |
| 0. Stop the bleeding | Done |
| 1. Test seams | Done apart from the CI tiers (F-03, in flight) |
| 2. Core seams, five lanes | In progress: lane C done; O, S, W and G partway |
| 3. Shared building blocks for the M2 features | Later |
| 4. The M2 exit path: chat, a real model, ledger and board, PDF viewer, then delegation and MCP | Later |

The plan's [progress section](refactor-plan/PLAN.md#progress) lists each finding with its PR.

- **Phase 1:** the worker's test harness, clock seam and conformance over the daemon are in (C-12, C-13, C-14, F-04, F-05). Left: F-03's CI tiers.
- **Lane O, the loop:** turn hooks own delivery and closing, and every subsystem is a hook (A-02); table-driven tool dispatch (A-11); one lease helper (A-12); a stateless kernel check (A-04); a `Harness` the CLI builds (A-05). `_run_task` is 57 lines. **Open:** step 5 (A-08 #165; A-13, A-14 #166).
- **Lane S, the control store:** a public `ControlStore` (B-01); one migration list, re-baselined (B-02); projects, sessions and runs in the store (A-07). Schema version 3; no source file disables private-usage checks. **In flight:** step 4 (B-14, B-17/E-12).
- **Lane W, the worker:** one usability record per environment (C-04); connections behind the manager facade (C-03 stage 1). `manager.py` is 3,059 lines. **Open:** C-03 stages 2 and 3 (#163, #164).
- **Lane C, the contracts:** done (D-01, D-02, D-07, D-11); worker protocol 3. Leftovers are in the [backlog](BACKLOG.md#repository).
- **Lane G, the workspace:** plain handlers and one error mapping (E-01, E-02); one browser request client and poller (E-03); a generated API contract (E-04, E-13); one save pattern (E-05). **In flight:** step 4 (E-14 and the small items).

## Known issues

- **Kernel start fails under parallel load.** About 1 in 100 runs under `pytest -n 16` (fewer at `-n 8`), a kernel start hangs, fails, or is miscounted by the executor's `usage` reply; it looks like jupyter_client's port choice racing between kernels started at once. Serial runs, which CI does today, do not show it. The F-03 task fixes it before the parallel tier.
- **Leaked daemon processes:** a few daemon tests (`test_recycle_intent_survives_failed_fence_and_worker_restart` and its sibling) leave a daemon running after the test. Also for the F-03 task.
- **Repeat runs fill `/tmp`:** `--count 50` keeps every test's temporary directory; use `-o tmp_path_retention_policy=failed`.
- **Upgrades:** worker protocol 3 means the orchestrator, the workspace and the worker upgrade together. Control stores from before schema version 2 are refused; export their documents first ([living documents](../docs/LIVING-DOCUMENTS.md)). Project JSON files are not read; projects are created again.

## M2 exit path

| Step | State |
| --- | --- |
| Chat in the GUI with a live transcript (A-06, E-06, E-07) | Not started; the run service builds on `Harness` |
| A real model through the owner's LiteLLM gateway (A-09) | Blocked on the gateway key |
| Ledger and board ([spec](../docs/specs/ledger.md)) | Not started |
| User-grade review views (E-11) | Not started |
| PDF viewer on a second origin (E-08) | Not started |
| M2 exit lab on babbage | After the steps above |

## Blocked on the user

- A real model needs the owner's LiteLLM gateway URL and a virtual key.
- crypto-research-mcp needs the owner's OAuth consent for the connector auth service.

## Open questions

Each has a recommendation; none blocks Phase 2.

- **Draft specs:** [skills](../docs/specs/skills.md), [plans and delegation](../docs/specs/plans-and-delegation.md) (questions 1 and 2: plan approval's default, progress across versions), [living documents](../docs/specs/living-documents.md) (question 15) and the [ledger](../docs/specs/ledger.md).
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

- #160 `96380a4`: projects, sessions and runs in the control store (A-07).
- #161: shared contract primitives and vocabulary; worker protocol 3 (D-02, D-07, D-11).
- #162: one save pattern for the workspace's editors (E-05).
- #158: a `Harness` the CLI builds (A-05).
- #155: environment connections behind the worker manager's facade (C-03 stage 1).
