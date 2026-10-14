# Status

Updated 2026-10-14, main `0e6a662`. This file is replaced, not appended to, and stays under 10 KB. History is in the git log, PR descriptions and [lab reports](docs/agent-reports/).

## Phase and focus

- **Phase 1 (babbage), toward milestone M2.** M1's exit run passed ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)).
- **Focus:** the [revision and refactoring plan](refactor-plan/PLAN.md), Phase 0 ("stop the bleeding"). Feature work on the M2 exit path resumes in Phase 4, after the test seams (Phase 1) and the core seams (Phase 2).
- **How work is done:** see the plan's [working rules](CONVENTIONS.md#working-rules) and the [handoff](handoff.md).

## What works today

- **Infrastructure:** the policy library, the policy middleware in front of OpenShell, the package cache (PyPI, conda, Cargo, Go, npm), the MCP proxy and the hash-chained event store.
- **Worker:** OpenShell VM sandboxes at tier 3 on babbage, with probed isolation tiers; shell calls, persistent Python and Sage kernels, file handoff and PDF tools; background jobs; an executor daemon that keeps executions and kernels through worker restarts; idle suspension and resume; on-demand host admission.
- **Provisioning (D84):** Conda and pip installs as queued operations with one permission request per install, AGPL refusal, environment revisions with lockfiles, explicit named Conda upgrades, installer observations and events, and a recovery fence after uncertain installs. Restoring a revision, uninstall, pip upgrade and disk limits are not built.
- **Harness (CLI):** the tool loop with a scripted or OpenAI-compatible model, projects and sessions, the planning phase with live progress (D86), living documents in the SQLite control store (D85), comments and work requests, automatic consistency review, mechanical math findings (D82) and the low-cost job watcher.
- **Workspace GUI:** login, documents with editing and conflicts, comments, findings, session plans with editing, jobs, the kernel panel, the provisioning view, and math rendering with repairs.
- **Not yet:** chat in the GUI, a run with a real model, the ledger and board, the PDF viewer, approvals in the GUI, MCP connectors in the harness. The delegation code is unwired and is being archived (plan decision 2).
- **Tests:** about 11,500 Python tests (11,464 passed, 43 opt-in skips at the last full run) and 828 web tests; CI runs Python 3.12 and 3.13 and the web checks.

## Refactoring plan

The [plan](refactor-plan/PLAN.md) has five phases. The owner answered its decisions 1–3 and 11 (raw evidence out of git) on 2026-10-14 and asked for autonomous work with the remaining recommendations adopted ([plan changes](PLAN-CHANGES.md)).

| Phase | State |
| --- | --- |
| 0. Stop the bleeding: bug fixes, removal of unwired code, hygiene, CI guards | In progress |
| 1. Test seams: clocks, test support, characterization tests, CI tiers | Next |
| 2. Core seams: loop hooks, control store, worker manager, contracts, workspace handlers | Later |
| 3. Shared building blocks for the M2 features | Later |
| 4. The M2 exit path: chat, a real model, ledger and board, PDF viewer, then delegation and MCP | Later |

Phase 0 is done when the bug tests pass, the unwired modules are gone and the suite still passes, STATUS is under 10 KB, and the size guard runs in CI. Done so far: raw evidence left git and history was rewritten (#123); `scripts/check_repo_hygiene.py` runs in CI.

## M2 exit path

| Step | State |
| --- | --- |
| Chat in the GUI with a live transcript (A-06, E-06, E-07) | Not started |
| A real model through the owner's LiteLLM gateway (A-09) | Blocked on the gateway key |
| Ledger and board ([spec](../docs/specs/ledger.md)) | Not started |
| User-grade review views (E-11) | Not started |
| PDF viewer on a second origin (E-08) | Not started |
| M2 exit lab on babbage | After the steps above |

## Blocked on the user

- A real model needs the owner's LiteLLM gateway URL and a virtual key.
- crypto-research-mcp needs the owner's OAuth consent for the connector auth service.

## Open questions

Each has a recommendation; nothing here blocks Phase 0.

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

- #123 `0e6a662`: raw lab evidence out of git; the refactoring plan.
- #122 `6e9608a`: explicit named Conda upgrades, accepted in [Lab36](docs/agent-reports/babbage-openshell-lab-36-2026-10-14.md).
- #121: Conda installer observations and provisioning events, accepted in [Lab35](docs/agent-reports/babbage-openshell-lab-35-2026-10-13.md).
- #120: the restricted child controller and authenticated worker transport (unwired delegation), [Lab34](docs/agent-reports/babbage-openshell-lab-34-2026-10-13.md).
- #119: declared pip license metadata in lockfiles.
