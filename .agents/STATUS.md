# Status

Updated 2026-10-18 on local `dev` at `1f9eb6f0`, with context-inspector and history-tool changes in progress. Signed commits are local and unpushed. The SNOVA acceptance facts below describe the 2026-10-17 frozen trial, not the later continuation or the current live service; verify live state before taking research action. Replaced, not appended; under 10 KB.

## Phase and focus

- **Bounded real-model acceptance is complete; the scientific program stays open.** The literature-grounded SNOVA DISCOVER plan is reviewed at revision 8, with three max-effort child reports incorporated. [Final report](docs/agent-reports/snova-research-acceptance-2026-10-17.md) records scope, artifact hashes and limitations. Shared observed spend: $66.2841164, plus one unknown call with a $4 reservation, within the $200 ceiling. The research export is preserved; all disposable lab VMs, processes, ports and owned state are gone. UI 29183 and Local Codex 5175 remain running.
- **Reasoning effort:** [role profiles](../docs/MODEL-PROFILES.md) and audited Resume overrides are built (`43889b45`, callback context fix `0d320dba`), independently reviewed, with 215 integration tests plus focused gates. Actual resumed parent and child Responses calls now report `max`. Original prompt pins remain byte-for-byte equal: the trial uses a compatibility checkout rather than silently replacing prompts on Resume. A failed-preflight ancestor guard is separately signed in `0d2d5e66`.
- **Research depth:** the initial SNOVA parent did not finish its plan, but a child prematurely completed a baseline using a simplified model. MAIN 10/planning 2 guidance now distinguishes extended exploration from a finite pilot/checklist (`f96d8d15`), with 189 focused tests and independent review. This prompt increment is not yet deployed in the frozen trial runtime. Discover mode was explicitly declared; initially no supporting skill-reference reads were recorded. The live agent subsequently read the required references and recorded a workflow audit; skills framing 4 (`36147403`) makes the procedure/reference obligation explicit.
- **Resource policy:** [adaptive allowances](../docs/specs/adaptive-research-resources.md) are planned, not implemented. Time/token/turn/storage warnings should reach the calling parent, which can renew allowances within hard user/security/capacity ceilings. Parent decisions need durable requests, aggregate reservations and a reserved control allowance.

## Verified local foundations

- The normal launcher runs orchestration in its own OpenShell VM; research code executes in worker VMs. Recursive explicit Resume, durable parent/child settlement, immutable starting inputs, bounded archive views and generated installed-tool restrictions are implemented. Earlier combined acceptance is in the [foundation report](docs/agent-reports/foundation-acceptance-2026-10-17.md); later changes require their own gates.
- The actual trial exposed an 8 MiB active replay limit before token folding. Provider-envelope projection preserves exact native archives while excluding redundant envelope fields from replay; active replay now has a 64 MiB backstop. Parent memory exhaustion now drains children through native and confined paths; private control protocol 3 now also carries role-specific profile pins and refuses older peers.
- Root-level Resume recovered an interrupted delegated operation without repeating its append. A lost volatile kernel required explicit recomputation acknowledgment; no kernel-state restoration is claimed. One interrupted model call has unknown usage and an explicit $4/full-token-upper-bound reservation, distinct from observed spend.
- The SNOVA parent folded and continued at a 200,000-token ceiling/150,000 trigger; observed input reached 179,729. Two children folded and completed reports. Child folds are skeleton-only, with original-history retrieval; narrative enrichment needs shared allowance accounting. The measured retry repair `46ccc5a9` passed 51 tests, independent review and bounded live exercise. A later first-level fold freed 63,741 tokens; its short narrative relies on the outline/documents. Broad scientific continuity, Claude and 600,000-token acceptance remain open.
- [Large-artifact acceptance](docs/agent-reports/large-artifacts-2026-10-17.md): 100 MiB upload passed actual OpenShell framed delivery and worker hash verification. A child produced a 512 MiB retained artifact; full and range downloads with matching digest succeeded after deleting its worker VM. Local filesystem blobs are independent of containers; 1 GiB/file, 2 GiB/call, 16 GiB/project and 64 GiB/store. S3/PVC operation remains unqualified. Back up the adjacent blob directory with the database.
- Final frozen runtime/image `e2cf7f42` passed all 17 container/Landlock checks; schema 68, worker 9, executor 3, private control 3. MAIN 9/planning 1/skills framing 3 remained pinned during root Resume. The runtime worktree is now archived. New dev guidance is for new work; upgrade components together and never launch from an actively edited checkout.

## Workspace and remaining product work

- Local UI has project/session navigation, chat and immutable 100 MiB file uploads, a drag-resizable half-window Documents/Plan/Agents/Activity pane, document filters and tabs, shared Markdown/LaTeX and revisioned editors. The saved-attempt context inspector and chunked original-history reader are now integrated locally on `dev`, with focused API, DOM, type and build checks; authenticated live browser acceptance is pending ([spec](../docs/specs/context-inspector.md)). A delegated parent can retrieve its descendants' archived history through the history tools; checks remain own-run only.
- Figures are built through F5 and F9–F11; Mermaid, figure checks/exports and browser acceptance remain. Skills K7a/K7b and generated binding restrictions are built; remaining items stay in BACKLOG.
- Advanced unknown-outcome reconciliation uses authenticated API routes. The UI exposes Resume and blocked reasons; a full graphical reconciliation editor remains open.
- Next: authenticated browser and OpenShell acceptance of the local inspector and descendant-history changes, then adaptive allowances, delegated narrative enrichment and fold-repair telemetry. The earlier frozen trial established no novel attack; later SNOVA results live in its preserved acceptance archive and need current verification before resuming. Do not restart paid research or push implicitly.

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
