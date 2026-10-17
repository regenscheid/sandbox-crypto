# Status

Updated 2026-10-16, main `7eb3b1f1`. This file is replaced, not appended to, and stays under 10 KB. History is in the git log, PR descriptions and [lab reports](docs/agent-reports/).

## Phase and focus

- **Owner priority, 2026-10-16:** research foundations before the UI redesign. Parent ownership and orchestrator-led tree resume (D87), plus a separate orchestrator sandbox (D47), are confirmed. [Research continuity](../docs/specs/research-continuity.md) drafts their implementation with bounded memory and durable state. No runtime changes are made by this draft. Automatic crash resume is undecided.

- **Phase 1 (babbage).** M1's exit run passed ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)), M2's exit checkpoint was met in [lab 39](docs/agent-reports/babbage-openshell-lab-39-2026-10-15.md), and **the first milestone for real use is met** in [lab 40](docs/agent-reports/babbage-openshell-lab-40-2026-10-15.md). The owner also runs a personal instance on babbage, apart from the labs ([runbook](../docs/PERSONAL-BABBAGE.md), #510); it was updated to `52c757c1` and again after #506.
- **Context management is built; its acceptance is not met.** The owner approved the [spec](../docs/specs/context-management.md) on 2026-10-15 (#439). Steps (a) to (g) are merged: the archive and history tools, the ledger's research state, both adapters, token accounting and the session view, the digest, folds and their enrichment, overviews and retired markers, the region within its bound, the `research_state` stop, the refit on a switch to a smaller model, fold proposals and their GUI.
  - **Lab 44** ([report](docs/agent-reports/babbage-openshell-lab-44-2026-10-16.md), #496) was round 1, on GPT models alone (the owner's USD 500). P3 to P6, P8 and P9 pass in every valid session. P1 fails in all five: no answer cited the decision's handle, which the owner then made a prompt rule and an advisory review note rather than a bar (#509). P2 fails in two. `gpt-600` and `second-250` folded too little to count. The skeleton-only control passed as well as the enriched arms but repeated more work after folds (+2.6 calls against -0.4 and -2.0; one session each). USD 277.95 reported, at most USD 363 at listed rates. Its bugs: tiers priced (#491, #494), a 524 retried and a carried fold listed (#502), a prompt cache key per session (#501). The owner's checklist in the report is pending.
  - **Next:** round 2 (the Claude arms, the 600,000 arms, the control repeated), measuring the state block's placement at rebuilds first (CM-25; an open design question), and the gateway: Claude thinking replay stays off until a stable LiteLLM 1.107.0 and the live check (CM-F30). It runs 1.102.1.
- **Skills:** K1 to K7b are built. K7's allow rule is the owner's option 3 (#486), and helper staging came with it (#493, worker protocol 6). Left: K7c, K8, editing overrides in the GUI, and the generated deny-list below.
- **Figures** ([spec](../docs/specs/figures.md), approved 2026-10-16, #436): F1 to F5 and F9 to F11 are built, so pinned references, the frame, Vega-Lite charts, the `figures` skill and `view_image` (drawing in the worker, protocol 7; the reviewer's too). The preview origin is another site by default (#506). Left: F6 (Mermaid), F7 (the check), F8, F11b, F12, F13 and the browser lab.
- **Images** were rebuilt on 2026-10-16 for `safe.bareRepository=explicit`: minimal and crypto-base `f09b3668`, orchestrator `a6be3886`, mcp-proxy `b0978e32`, all passing `images/test.sh` (#505, #507).
- **How work is done:** the plan's [working rules](CONVENTIONS.md#working-rules) and the [handoff](handoff.md).

## Real use (labs 40 to 44)

All used the real model and crypto-research-mcp.

- **Labs 40 to 42:** the whole path with delegation and connectors (162 skills imported), then skills in runs. Their bugs are fixed (#445 to #449 for lab 42).
- **Lab 43** ([report](docs/agent-reports/babbage-openshell-lab-43-2026-10-16.md)): meaning search stores and scores vectors, but a paraphrase fell under the cutoff (fixed per model family, #474). Both adapters work across runs through LiteLLM 1.102.1. GPT calls had no cost, and an overloaded response ended a run (fixed, #476, #477). Reviews with no reviewer are off (#475). The digest was not in that instance.
- **Lab 44:** above.
- **Not yet seen live:** the figures, `view_image` and the K7 staging in real runs, and folds on Claude models.

## What works today

- **Launcher:** `sandbox-science up`, `down`, `status` and `skills update` run the worker, the run service, the workspace and the MCP proxy from one config ([quickstart](../docs/QUICKSTART.md)), including `[model] api` and `images`.
- **Worker:** OpenShell VM sandboxes at tier 3 with persistent Python and Sage kernels, file handoff, PDF tools, drawing of figures, background jobs and provisioning. Protocol 7 ([compatibility](../docs/COMPATIBILITY.md)); the control store is at version 56.
- **Harness:** the tool loop, the run service, MCP connectors, planning, living documents, comments, automatic consistency review (a rejected evidenced rebuttal leaves a finding `challenged`), math checks, the job watcher, recorded files, project memory, `ask_user`.
- **Context** ([spec](../docs/specs/context-management.md)): the archive and history tools; a session view with folds, overviews and a state block with a digest; fold proposals the user accepts or dismisses. GPT (`/v1/responses`) and Claude (`/v1/messages`) clients are chosen per alias; every alias uses chat completions until the owner turns them on.
- **Delegation, ledger, skills, cost:** as in the specs. Cost is the gateway's, or priced from its model information and marked so; with a daily limit, an unpriced call counts it as reached.
- **Workspace GUI:** documents, comments, reviews, plans, chat, jobs, kernels, provisioning, artifacts, a PDF viewer, the ledger with its research state, figures in text, fold proposals, the Skills page, the Memory page, a session's run tree.
- **Not built:** Mermaid and the figure check, exports of figures, helper lineage, per-pack thresholds, approvals in the GUI, execution-pinned evidence.
- **Tests:** 13,655 Python tests are collected, 891 of them in the slow tier, and 2,173 web tests (fresh counts at this sync).

## What comes next

1. Context management: the owner's checklist, then round 2.
2. Figures F6 to F8, then F11b and exports; the browser lab for the frame.
3. A skills lab on whether skills help; K7c, K8 and the generated deny-list.
4. Other [BACKLOG](BACKLOG.md) items: questions from delegated runs, memory proposals' basis (PM-3, PM-4), vouching for PDFs, approvals in the GUI.
5. Phase 5, which includes moving the domain packages out of the orchestrator.

## Known issues

- **Not fixed, from labs 40 to 44:** `eprint_fulltext` matches single words; a citation check and a finding can disagree; the copied-number check reads decimal numbers only; a check keeps the sandbox's write tools; effect tools refuse until a run reads the plan, a finished one included; `mcp_tools` matches long queries strictly; GPT through `/responses` reasons little when a tool fits. The weaker model errs on purpose ([handoff](handoff.md#labs)).
- **Skills:** a delegated run reads its project's skills when it starts, so a moved pin can give a child a newer set; removing a pack from a project's `skills` does not unpin it; an import's pass may derive no short description for long ones. The deny-list's tool families are listed by hand, and each security check has found tools it lacks (#503 added more).
- **Charts:** a chart that derives values with heavy literals is not flagged until the figure check (F7).
- **Background job slots:** a session has 4, and a delegation tree can take them all. It needs a decision.
- **CI stalls:** `test (fast)` has run until its limit with no log. Rerun the job.
- **Upgrades:** the orchestrator, the workspace and the worker upgrade together. Control stores before version 2 are refused.

## Open questions

None stops work in progress.

- **Q12 to 14 of the skills spec** stay recorded as the coordinator's reversible acceptance (#421, #438); the owner has not confirmed them.
- **The state block's placement at rebuilds (CM-25):** to be measured in round 2 before it is decided.
- **Lab 44's checklist (the owner):** whether CM-P1's bar stands as the owner set it, and whether the two CM-P2 failures count.
- **Models:** the `reviewer`, `watcher` and `delegated` aliases are all `sandbox-orchestrator` for now.
- **PDF vouching:** whether the e-print server names a PDF's hash in a recorded result.
- **Living documents:** question 15. **Math display** ([workspace](../docs/WORKSPACE.md#math-repairs)): five open points. **Images** ([images](../docs/IMAGES.md)): no registry on babbage; G6K at x86-64-v3; GPL libraries in `minimal`; crypto-base's compilers, R and OpenJDK; IACR LaTeX classes.
- **MCP proxy:** MCP 2026-07-28 only when a server needs it; binary results withheld. **Event store:** installing the checkpoint timer from `examples/systemd` on babbage. **Upstream reports** in [`.agents/upstream/`](upstream/): whether to file them.
- **crypto-analysis-skills:** confirm the copyright holder before merging its MIT license.

## Known facts

- OpenShell's VM driver is experimental at v0.1.3; babbage's tier 3 depends on it (D43), and it has worked in every lab.
- babbage restricts unprivileged user namespaces through AppArmor. Claude Desktop's shells are exempt, so qualification checks run from a plain terminal or `systemd-run --user`.
