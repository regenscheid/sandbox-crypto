# Status

Updated 2026-10-17 on `codex/research-ui`, based on foundation `631dc246` (main `57e29536`). Signed commits are local, unpushed and unmerged. Replaced, not appended; under 10 KB.

## Phase and focus

- **Real-model acceptance is in progress.** The owner authorized a $200 total arrcodex trial on Babbage, including extended SNOVA cryptanalysis, skills, MCP/PDFs, Sage, delegation, folding and recovery. The disposable lab remains active; it must be fully torn down after evidence/artifacts are preserved. Personal services are untouched; preserve the synthetic UI preview on 29183 and Local Codex on 5175.
- **Reasoning effort is the immediate priority.** The current trial actually returns `medium` for `arrcodex-gpt-6-sol`. The owner wants `max` and configurable effort by role. Per-role configuration, immutable profile pins and explicit operator Resume overrides are being implemented/reviewed. Do not claim max is live until returned provider metadata confirms it. The owner requested an in-depth, literature-grounded research plan intended to pursue novel results; that request is queued for the max-effort continuation.
- **Research depth:** the initial SNOVA parent did not finish its plan, but a child prematurely completed a baseline using a simplified model. MAIN 10/planning 2 guidance now distinguishes extended exploration from a finite pilot/checklist (`f96d8d15`), with 189 focused tests and independent review. This prompt increment is not yet deployed in the frozen trial runtime. Discover mode was explicitly declared; initially no supporting skill-reference reads were recorded. The live agent has been asked to audit the full workflow.
- **Resource policy:** [adaptive allowances](../docs/specs/adaptive-research-resources.md) are planned, not implemented. Time/token/turn/storage warnings should reach the calling parent, which can renew allowances within hard user/security/capacity ceilings. Parent decisions need durable requests, aggregate reservations and a reserved control allowance.

## Verified local foundations

- The normal launcher runs orchestration in its own OpenShell VM; research code executes in worker VMs. Recursive explicit Resume, durable parent/child settlement, immutable starting inputs, bounded archive views and generated installed-tool restrictions are implemented. Earlier combined acceptance is in the [foundation report](docs/agent-reports/foundation-acceptance-2026-10-17.md); later changes require their own gates.
- The actual trial exposed an 8 MiB active replay limit before token folding. Provider-envelope projection preserves exact native archives while excluding redundant envelope fields from replay; active replay now has a 64 MiB backstop. Parent memory exhaustion now drains children through native and confined paths; private control protocol 2 refuses older peers.
- Root-level Resume recovered an interrupted delegated operation without repeating its append. A lost volatile kernel required explicit recomputation acknowledgment; no kernel-state restoration is claimed. One interrupted model call has unknown usage and an explicit $4/full-token-upper-bound reservation, distinct from observed spend.
- The SNOVA parent has completed two real automatic folds at a 200,000-token ceiling/150,000 trigger and continued. Delegated folding is implemented/tested, but a live child fold is still pending. Broad scientific continuity, Claude folding and 600,000-token context acceptance remain open.
- [Large-artifact acceptance](docs/agent-reports/large-artifacts-2026-10-17.md): 100 MiB upload passed actual OpenShell framed delivery and worker hash verification. A child produced a 512 MiB retained artifact; full and range downloads with matching digest succeeded after deleting its worker VM. Local filesystem blobs are independent of containers; 1 GiB/file, 2 GiB/call, 16 GiB/project and 64 GiB/store. S3/PVC operation remains unqualified. Back up the adjacent blob directory with the database.
- Current deployed trial checkout is frozen at `bd738a9a`; image `acee67a0`, control schema 68, worker protocol 9, executor protocol 3 and private control protocol 2. Upgrade all components together. Never run the live launcher against an actively edited checkout.

## Workspace and remaining product work

- Local UI has project/session navigation, chat and immutable 100 MiB file uploads, a drag-resizable half-window Documents/Plan/Agents/Activity pane, document filters and tabs, shared Markdown/LaTeX and revisioned editors. Validation: 2,194 web tests plus focused backend gates; [UI report](docs/agent-reports/research-ui-refinements-2026-10-17.md). The context inspector design is browser-checked but integration remains pending ([report](docs/agent-reports/context-inspector-2026-10-17.md)).
- Figures are built through F5 and F9–F11; Mermaid, figure checks/exports and browser acceptance remain. Skills K7a/K7b and generated binding restrictions are built; remaining items stay in BACKLOG.
- Advanced unknown-outcome reconciliation uses authenticated API routes. The UI exposes Resume and blocked reasons; a full graphical reconciliation editor remains open.
- Next: finish per-role effort/recovery validation; deploy at a safe checkpoint and verify max; obtain/review the deep research plan, exercise child folds and sustained computations, preserve useful artifacts, record exact spend and limitations, then completely tear down the disposable lab. Do not merge or push.

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
