# Status

Updated 2026-10-17 on `codex/research-ui`, based on foundation `631dc246` (main `57e29536`). Replaced, not appended; under 10 KB. History is in the git log, PR descriptions and [lab reports](docs/agent-reports/).

## Phase and focus

- **Research UI is implemented locally.** Left project/session navigation, central chat with file uploads, and an optional resizable Documents/Plan/Agents/Activity pane. Markdown/LaTeX rendering and existing revisioned editors are shared. Uploads retain immutable USER originals up to 100 MiB and use scoped worker delivery; schema 67. The pane reaches half the window, and Documents has filters and named tabs. Independent GPT-6.1-Sol reviews are clear. See [UI validation](docs/agent-reports/research-ui-2026-10-17.md) and [review refinements](docs/agent-reports/research-ui-refinements-2026-10-17.md). Signed local commits remain unpushed/unmerged.
- **Foundations (D47/D87):** preserved at `631dc246`; [acceptance](docs/agent-reports/foundation-acceptance-2026-10-17.md) covers recursive Resume, child settlement, bounded history, delegated folds, tool restrictions and separate OpenShell orchestration. Process restart alone never restarts paid research.

- **Early prototype (owner assessment).** Historical labs exercised individual paths; they do not establish the intended long-running research product. Real-model scientific continuity and cache acceptance remain separate from deterministic and disposable-runtime qualification. The personal services were inactive at the [foundation review](docs/agent-reports/openshell-foundations-review-2026-10-16.md); this work has not started or changed them.
- **Context management is built; its acceptance is not met.** The owner approved the [spec](../docs/specs/context-management.md) on 2026-10-15 (#439). Steps (a) to (g) are merged; the spec and acceptance reports describe the archive, folds, ledger context, accounting and model-switch behavior.
  - **Lab 44** ([report](docs/agent-reports/babbage-openshell-lab-44-2026-10-16.md), #496) was round 1 on GPT models. P3 to P6, P8 and P9 pass in valid sessions; decision-handle citations became advisory after the owner changed P1 (#509), and two P2 failures remain. Some arms folded too little to count. The owner's checklist remains pending; see the report for costs, gateway fixes and controls.
  - **Next:** round 2 (the Claude arms, the 600,000 arms, the control repeated), measuring the state block's placement at rebuilds first (CM-25; an open design question), and the gateway: Claude thinking replay stays off until a stable LiteLLM 1.107.0 and the live check (CM-F30). It runs 1.102.1.
- **Skills:** K1 to K7b are built. K7's allow rule is the owner's option 3 (#486), and helper staging came with it (#493, worker protocol 6). Left: K7c, K8, editing overrides in the GUI, and further skill authoring UI work. Generated installed-tool binding restrictions and runtime inventory digests are implemented locally ([report](docs/agent-reports/skills-inventory-2026-10-17.md)).
- **Figures** ([spec](../docs/specs/figures.md), approved 2026-10-16, #436): F1 to F5 and F9 to F11 are built, so pinned references, the frame, Vega-Lite charts, the `figures` skill and `view_image` (drawing in the worker, protocol 7; the reviewer's too). The preview origin is another site by default (#506). Left: F6 (Mermaid), F7 (the check), F8, F11b, F12, F13 and the browser lab.
- **How work is done:** the plan's [working rules](CONVENTIONS.md#working-rules) and the [handoff](handoff.md).

## Prior real use

Labs 40–44 exercised real models and crypto-research-mcp; historical results and outstanding scientific-continuity acceptance are in the [foundation report](docs/agent-reports/foundation-acceptance-2026-10-17.md) and Lab 44. The UI browser session used explicit synthetic fixtures and made no model calls.

## What works today

- **Launcher:** `sandbox-science up`, `down`, `status` and `skills update` run the worker, the run service, the workspace and the MCP proxy from one config ([quickstart](../docs/QUICKSTART.md)), including `[model] api` and `images`.
- **Worker:** OpenShell VM sandboxes at tier 3 with persistent Python and Sage kernels, file handoff, PDF tools, drawing of figures, background jobs and provisioning. Protocol 9 and executor protocol 3 ([compatibility](../docs/COMPATIBILITY.md)); the control store is at version 67 on this branch (65 at its foundation base).
- **Harness:** the tool loop, the run service, MCP connectors, planning, living documents, comments, automatic consistency review (a rejected evidenced rebuttal leaves a finding `challenged`), math checks, the job watcher, recorded files, project memory, `ask_user`.
- **Context** ([spec](../docs/specs/context-management.md)): the archive and history tools; a session view with folds, overviews and a state block with a digest; fold proposals the user accepts or dismisses. GPT (`/v1/responses`) and Claude (`/v1/messages`) clients are chosen per alias; every alias uses chat completions until the owner turns them on.
- **Delegation, ledger, skills, cost:** as in the specs. Cost is the gateway's, or priced from its model information and marked so; with a daily limit, an unpriced call counts it as reached.
- **Workspace GUI:** documents, comments, reviews, plans, chat, jobs, kernels, provisioning, artifacts, a PDF viewer, the ledger with its research state, figures in text, fold proposals, the Skills page, the Memory page, a session's run tree.
- **Not built:** Mermaid and the figure check, exports of figures, helper lineage, per-pack thresholds, approvals in the GUI, execution-pinned evidence.
- **UI validation:** 2,194 web tests and focused backend checks pass; independent review is clear. Browser retention and local two-environment transfer pass at 100 MiB. The synthetic loopback preview remains running for owner review. Real OpenShell upload qualification remains open ([refinements](docs/agent-reports/research-ui-refinements-2026-10-17.md)).
- **UI design:** dragging and ledger/session navigation browser-checked; [report](docs/agent-reports/pane-navigation-2026-10-17.md). Integration pending.
- **Foundation validation:** full run 14,028 passed and 49 opt-in skips; its sole outdated fixture was corrected and passed 2/2, with no later product change. Web: 2,180 passed; static checks and tier coverage pass. OpenShell interruption/Resume passed and the lab was torn down. Exact scope and hashes are in the [acceptance report](docs/agent-reports/foundation-acceptance-2026-10-17.md).

## What comes next

1. Review and integrate the signed foundation branch with its acceptance evidence; upgrade the launcher, image, control, workspace and worker together.
2. Separately budget real-model scientific continuity and provider cache measurements. Offline prefix comparison did not justify changing production context order.
3. Review the local UI and run a disposable OpenShell upload lab before integration; continue the [BACKLOG](BACKLOG.md). Advanced unknown-outcome reconciliation currently uses the authenticated API; the workspace exposes Resume and blocked reasons.

## Known issues

- **Not fixed, from labs 40 to 44:** `eprint_fulltext` matches single words; a citation check and a finding can disagree; the copied-number check reads decimal numbers only; a check keeps the sandbox's write tools; effect tools refuse until a run reads the plan, a finished one included; `mcp_tools` matches long queries strictly; GPT through `/responses` reasons little when a tool fits. The weaker model errs on purpose ([handoff](handoff.md#labs)).
- **Skills:** a delegated run reads its project's skills when it starts, so a moved pin can give a child a newer set; removing a pack from a project's `skills` does not unpin it; an import's pass may derive no short description for long ones. Installed-tool manifests now extend the existing deny rules; stale surviving kernels fail closed until their inventory is refreshed.
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
