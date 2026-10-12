# Handoff

Written 2026-10-11 at the end of a long session, for the next agent. Read this first, then [STATUS.md](STATUS.md) (phase, progress, open questions), [BACKLOG.md](BACKLOG.md) (the checklist), [CONVENTIONS.md](CONVENTIONS.md) (how code and docs are written), and [DECISIONS.md](../docs/DECISIONS.md) (D01 to D85). Those files are kept current; this one says what they do not: where the work stopped, how it has been done, and what the user expects.

## Where things stand

- **Table parser parity, 2026-10-12:** `fix/table-parser-delivery` in `parallel-delivery` integrates independently reviewed source `4cdb40f` with central tracking at signed `9419abec`. New profile 2 matches browser table trim and source spans; immutable profile 1 histories select their original parser before parsing, with truthful restore metadata and unchanged IDs. Root final combined gates on signed `9419abec` passed 9,817 Python tests (43 opt-in skips, two existing serializer warnings), 571 web tests including the 1,076-source differential corpus, 24 post-build math cases, both production builds and locked/static/license/protocol checks. Source/test/helper/configuration stayed unchanged; subsequent edits describe acceptance and tracking only. Fresh exact-head PR CI and merge remain required. No services, browser or model were used for the parser gate; the differential test exercises the actual pinned parser implementations.

- **Parallel continuation, 2026-10-12:** the user explicitly asked for more parallel agents. Three authors work from main `1890db0` with separate ownership: `impl/kernel-start-revision` in `background-worker` (contracts/worker and narrow tool disclosure); `impl/project-macros` in `project-macros` (control/settings/math and GUI); `fix/table-parser-parity` in `table-parser-parity` (server table rule and shared parser cases). Root keeps central tracking in `parallel-delivery`, then integrates it into feature delivery. Finished authors rotate onto independent reviews; no author starts acceptance services or real models. No item is complete yet. Preserve all old worker refs, especially `impl/background-jobs-worker`; do not reuse pending-archive worktrees.

- **Isolated math notices, 2026-10-12:** `impl/math-license-notices` starts from merged main `05ba353` in the background-worker worktree; old GUI refs are preserved. The shared collector emits complete code-only KaTeX MIT/React Apache/SVG OFL notices, project MIT, unchanged launcher and six-file hashes, verified by the established copier entry point. No dependencies, renderer changes, fonts/CSS/Svelte runtime or services. All 41 focused notice cases pass; baseline renderer SHA `2091762bb88d332906540ef726015221fc839d4d4965652f6c53616cef03478f` and launcher SHA `623911cf4f65425523e07f56f80d86deda1e26aa629ce2c8ae1e363ee61e202b` are retained in `/tmp/math-license-baseline.json`. Independent review and root six-file acceptance pass. Final gates passed 486 web tests (41 notice cases), 24 post-build math cases, both builds and locked/static/license/protocol checks. Source-identical full Python 9,749/43/two warnings is explicitly reused. Actual main `b2f2e62` (merged PR #92) is integrated at signed `fe19020`; fresh combined 515 web tests/check/build pass, and all 70 GUI files match root copy-accepted bytes exactly. The unchanged six-file math distribution/build/24 results and backend baseline are explicitly reused. Independent exact-head review cleared signed `a4be274`; PR #93 merged at actual main `1890db0` after all four fresh checks passed (run `38295449131`). [Report/archive](docs/agent-reports/math-license-notices-2026-10-12.md) preserve fresh gates, six-file hashes, actual origins and root acceptance. Root owns integration/delivery; no source/runtime/publication changes outside notice building.

- **Copy original LaTeX, 2026-10-12:** approved narrow W4/LD-44 work is in `impl/workspace-copy-latex`, reusing `/home/andy/.codex/worktrees/catalog-delivery/sandbox-crypto` from actual main `c6ccce61c6823aa2a74071e010527c065bce2487`. Independently reviewed signed union `837962af8a428362a6843dda216180f38307bbb9` has parents exact feature/helper `1902131` and actual main `05ba353`; protected license inputs remain exact main. Fresh combined checks passed 501 web tests (29 copy cases), 195 focused Python cases, 24 post-build math checks, both builds and all locked/static/license/protocol checks. Full Python 9,749/43/two warnings is explicitly reused from unchanged backend `123b2e6` plus fresh #90/#91 CI; helper review includes four mocked cleanup probes and separate preflight. Logs remain `/tmp/sandbox-copy-latex-{web,python}-final.log`; public delivery notes are `/tmp/sandbox-copy-latex-delivery-notes.md`. Root-owned [browser acceptance](docs/agent-reports/workspace-copy-latex-2026-10-12.md) passed 19 native Copy→paste cases on the exact build, with no warning/error logs, unchanged ranges, rich HTML and ordinary code/editor behavior. The unsaved paste draft was discarded; source revision 1 remains. Both transient runners/ports, marked state, private info and tab were removed at 18:38:03.217625 UTC; two synthetic loopback repair requests used no real model. Product/helper/assets remain exact `837962af`. PR #92 merged at actual main `b2f2e62` after all four fresh CI checks passed on signed `d547d500` (run `38292786944`). No author-started services or model calls, new dependencies, backend changes or CSP changes.

- **GUI license notices, 2026-10-12:** `impl/workspace-license-notices` in `/home/andy/.codex/worktrees/background-worker/sandbox-crypto` preserves source `6b2b6bb` and independently reviewed signed actual-main union `1032cd4` (parents `6b2b6bb` and `c6ccce6`). Source/test/license/config are frozen. Fresh gates pass 472 web tests (27 notice cases), 24 math cases, both builds and locked/static/license/protocol checks. Full Python 9,749/43/two warnings is explicitly reused from source-identical `123b2e6` plus fresh #90 CI. Root-owned [HTTP acceptance](docs/agent-reports/workspace-license-notices-2026-10-12.md) fetched all 70 files exactly; runner/port/marked-state teardown passed at 18:14:20.931549 UTC. The archive retains 21 exact records, including complete generated notices, file manifest, fresh gates and input-comparison proof. No browser/model/VM/research execution, new dependency or platform-code allowlist expansion. PR #91 merged at actual main `05ba353` after all fresh checks passed (run `38290115991`). The separate isolated math-check notice follow-on merged as PR #93, recorded above.

- **Failed-run records, 2026-10-12:** `impl/failed-run-records` in `/home/andy/.codex/worktrees/catalog-delivery/sandbox-crypto`, based on main `3c91c30`, has independently reviewed signed source `123b2e6`. Ordinary loop failures retain partial observations with fixed phase metadata and no final answer; delivered-only save-before-ACK and no failed math finalization preserve lifecycle authority. The first interruption propagates through later cleanup failures without save/ACK. Final gates passed 9,749 Python tests (43 opt-in skips, two existing warnings), 445 web tests, 24 math checks and all required checks/builds. Root-owned [CLI/browser acceptance](docs/agent-reports/failed-run-records-2026-10-12.md) passed, with 22 exact archived text records and one screenshot. Browser acceptance used signed `50e20b1`; GUI/server/helper source and 67 assets are unchanged on final source. Final-source CLI acceptance passed separately. Complete browser and final CLI teardown was verified at 17:39:04.081374 and 17:47:42.873911 UTC. Two interrupted Python predecessors are explicitly incomplete. PR #90 merged at `c6ccce6` after fresh Python 3.12/3.13/web and security checks passed (run `38287920059`). No services/models remain active. Cancellation/process-death persistence, live streaming, session-end semantics and scheduling remain open.
- **Capacity errors:** the user instructed, "try again after 5 minutes whenever you have a capacity error." Wait five minutes and retry the same task and model; do not change models. This is an agent execution preference, not a new product retry feature.

- **Merged MAIN finding-prose continuation, 2026-10-12:** `impl/finding-prose-math` in `/home/andy/.codex/worktrees/background-harness/sandbox-crypto` preserves prior refs. Signed source `eb5a50f`, actual-main union `c509bc1` and final bounded replay correction `f7bb0a3` implement only MAIN fix/rebut explanation fields ([scope](../docs/FINDING-PROSE-MATH.md)). Independent exact-head review passed. Original scientific action/evidence/actor and parent CAS stay unchanged on ordinary body correction; reviewer decisions independently pin the explanation. Shared quotas/checks/closing/save-ACK, empty macros, session-private evidence and held unknown legacy work are covered. Final combined gates pass: 9,730 Python tests (43 opt-in skips, two existing warnings), 443 web tests, 24 post-build math checks and all required checks/builds. The Python predecessor run was deliberately interrupted for the replay correction and is not final evidence; final Python passed on `f7bb0a3`, with unchanged completed web/helper/assets reused. Root-owned [browser acceptance](docs/agent-reports/finding-prose-math-2026-10-12.md) passed exact original/current provenance, six child states, separate mechanical/scientific confirmation and session isolation; overall fixture review remains explicitly inconclusive. Full teardown was independently verified at 15:37:06.734290 UTC. Helper SHA-256 `7bc82ebf57e01b7905327dabdd2cc558896b94374ea97db06c4f8ec08a92d94b`. Product/helper/assets are frozen. PR #89 merged at `3c91c30` after fresh Python 3.12/3.13/web and security checks passed (run `38272517996`). The clean merged worktree archive was requested; do not reuse its pending-archive path. No author-started services/models. Ledger and excluded prose, model repairs and idle work remain open.

- **Worker observation acceptance, 2026-10-12:** `impl/output-observations` has
  independently reviewed signed freeze `35863776`, integrating actual main `d2b133f`
  (PR #87). It retains bounded exact raw-byte/root regular-stat sidecars, original
  request/attempt/incarnation/known immutable VM pins and atomic terminal detail/gaps
  with scoped internal replay. Invalid/quota/corrupt derived state preserves healthy
  completion and canonical recovery. All integrated gates pass: 83 focused cases,
  9,714 Python tests (43 opt-in skips, two existing serializer warnings), 441 web
  tests, 24 post-build math cases and all required checks/builds.
  Root-owned [Lab24](docs/agent-reports/babbage-openshell-lab-24-2026-10-12.md) passed
  nineteen terminal markers/eighteen reports, raw identities, partial outputs,
  missing/truncation gaps and original-attempt/kernel continuity through graceful
  worker restart. PDF was unavailable. Full teardown was independently verified
  at 14:59:02.325873 UTC. Source/test/helper/configuration stayed frozen; subsequent
  changes are acceptance documentation/archive only. PR #88 merged at `744d1b5`
  after fresh Python 3.12/3.13/web and security checks passed (run `38268375467`).
  RA-5 still requires every file minus project ignore patterns. No public feed,
  consumer, scheduler, content capture or artifact revisions are enabled. Mutation
  completeness, deletion/link proof, writer/producer/input lineage and attempt-end
  content remain open. Next design original-attempt-bound one-shot capture receipts
  and authenticated transport; do not enable automatic fan-out from this journal.

- **Comment/reply D82 acceptance, 2026-10-12:** final signed source `77ecc171` and helper SHA `972b459ba8223cb374818098e375c948ddddbee3dc817ac0b5027051b116a62e`, independently reviewed. [Scope](../docs/COMMENT-MATH.md) preserves immutable original/current correcting actors, ordinary edit authority and exact thread/body/hash CAS, canonical parent/macros, shared closing/save-ACK and explicit held unknown/parser-bound work. A preliminary browser found the dependency-only reflag bug; the corrected shared lifecycle spends the two-reflag budget only on proved failing block edits and reconciles old accepted-owner state. Final combined gates pass: 9,631 Python tests (43 opt-in skips, two existing warnings), 441 web tests, 24 post-build math checks and all locked/static/license/protocol checks/builds. Fresh root-owned [browser acceptance](docs/agent-reports/comment-math-2026-10-12.md) passed corrected budgets, retained independent drafts/stale CAS, USER-only-own-body authority, MAIN correction of delegated text with original history, exact accepted/removal evidence and session isolation. Browser tab/server/marked fixture were removed at 13:50:58.272097 UTC; final logs had no warning/error. Source/test/helper/assets remain unchanged after the freeze. PR #87 merged at `d2b133f` after all three fresh CI checks passed on `16bb1308`. Ledger/finding prose, model repair proposals, idle work and automatic compaction remain open.

- **External conformance acceptance, 2026-10-12:** root-owned
  [Lab23](docs/agent-reports/babbage-openshell-lab-23-2026-10-12.md) passed the unchanged
  external suite on frozen main `73c07aa`: 23 cases per linux/amd64 profile, minimal
  and crypto-base; 23 local variants per run were intentionally skipped. Both actual
  Python 3.14.8 guests retained effective/declared tier 3 and stable distinct boots.
  Complete teardown and both owned fixture removals were independently verified at
  12:46:22 UTC. No product/image/assertion/timeout changes; Sage was advertised but
  not tested by this suite. External conformance/probe and linux/amd64 profile
  acceptance are complete; arm64, Kubernetes and multi-platform work remain open.
  Keep this distinct from capture Lab22's source `b535856` and local fault tests.

- **Capture prerequisite accepted locally, 2026-10-12:** the automatic-output readiness
  assessment found the first worker whole-file hash was unbounded. The independent
  bounded helper/ownership slice now preserves ordinary reads, fails visibly on
  legacy capture peers and retains durable admission for unknown guest cleanup.
  Original immutable VM proof and actual host drain are distinct. Independent review
  cleared signed source `cbcfc7e`; final-source gates passed 9,592 Python tests/43
  skips, 60 capture/lifecycle cases, 436 web/24 post-build math checks and all required
  checks/builds. Independent review cleared signed combined freeze `b535856`;
  root-owned [Lab22](docs/agent-reports/babbage-openshell-lab-22-2026-10-12.md) passed exact
  binary/empty retention, prompt regular/sparse/symlink/FIFO refusal and healthy
  publication/read/execution access on Python 3.14.8. Independent inspection found
  zero capture leases/helpers; full teardown was verified at 12:36:45 UTC. Exact signed source `ea31757` and both acceptance archives merged as PR #86 at
  `9c07725`, with all three fresh CI checks successful (run `38251872668`,
  13:01:50 UTC). No product/test/configuration/helper change followed validation. No automatic output discovery,
  attempt-end bytes, producer/input lineage or closed-writer claim is enabled.
  See [capture boundary](../docs/ARTIFACTS.md#bounded-worker-capture-prerequisite).

- **CP-13 admission continuation:** [sticky recovery fence](../docs/RECOVERY-FENCE.md) signed source `e320236` is independently reviewed and passed final local gates: 9,532 Python tests (43 opt-in skips), 436 web tests, 24 post-build math checks, locked/static/license/protocol checks and both builds. Main `73c07aa`, including merged PR #85, is integrated. Root's first cancel/restart [Lab21](docs/agent-reports/babbage-openshell-lab-21-2026-10-12.md) pass used predecessor `ef77f6d`; the final fresh pass used exact `e320236` and proved graceful worker-offline recovery, protected-work refusal, preserved existing kernel/files/history/lockfile/peer and explicit idempotent delete/reuse with a new immutable sandbox/boot. Both labs are completely removed; final teardown was independently verified at 12:13:54 UTC. PR #85 merged after all three fresh CI checks passed; no source/helper edits followed the runtime freeze. Restorer and operator clear remain separate follow-ons. No lab/service was started by the source author.

- **Earlier continuation snapshot, 2026-10-12:** main `3c91c30` includes PR #69 through #89 with fresh CI successful, including catalog #76, outbox #77, source adapter #79, atomic fixture #80, manual delivery #81 and artifact inventory #82. Document-revision D82 now has independently reviewed signed source and [real browser acceptance](docs/agent-reports/document-math-2026-10-12.md). Its exact artifact source union passed final combined gates: 9,459 Python tests (43 opt-in skips), 434 web tests, locked/static/license/protocol checks and both builds. One manually constructed CLI test namespace needed three optional math defaults; its cleanup assertions and timeouts are unchanged, and the final full rerun passed. Rebuilt GUI bytes match the browser archive. D82 PR #83 and artifact evidence PR #84 merged after all fresh CI jobs passed. Main #84 validation recorded 9,494 Python/436 web tests. Renderer configuration remains operator-owned/default unavailable. The comment/reply continuation is tracked above; full LD-53 ledger/finding-prose coverage and idle dispatch remain open. Normal development is Python 3.13; the supported 3.12 CI matrix remains.

- **Manual catalog delivery continuation:** a separate internal adapter and durable journal passed independent source review and combined acceptance: 9,365 Python tests (43 opt-in skips), 431 web tests and all required checks/builds. Writer-guard planning, the worker source adapter and the atomic synthetic OpenShell fixture are integrated. Explicit approved endpoint/configuration bindings, complete pinned targets and exact worker storage acknowledgement never imply environment file publication. There is no scheduler, automatic client discovery or execution hook; uncertain writer state still holds LD-49 publication. See [manual delivery](../docs/LIVING-DOCUMENTS.md#manual-catalog-delivery) for the current boundary.

- **Resume update, 2026-10-12:** the user resumed work here after the pause below. Lab 18 passed and was completely removed; its [report](docs/agent-reports/babbage-openshell-lab-18-2026-10-12.md) and recorded evidence are committed with reusable checks. Background jobs passed independent review and [lab 19](docs/agent-reports/babbage-openshell-lab-19-2026-10-12.md), including live browser controls and once-only notices after a harness process restart; that lab is also completely removed. Document editing passed independent review and [browser acceptance](docs/agent-reports/document-editing-2026-10-12.md). The executor daemon passed [lab 20](docs/agent-reports/babbage-openshell-lab-20-2026-10-12.md), including the corrected original-VM stop/start fence after an initial daemon-loss cleanup failure; all Lab20 state was removed. Comments, anchors and work requests passed independent review and [browser acceptance](docs/agent-reports/document-comments-2026-10-12.md). The consistency reviewer and findings GUI passed independent review and [browser acceptance](docs/agent-reports/consistency-review-2026-10-12.md), including a final confirmation-scope correction that preserves unrelated author-work retries. All disposable browser state was removed. The isolated math checker prerequisite merged as PR #74 with CI green on Python 3.12 and 3.13. The worker document-catalog prerequisite passed independent review and combined validation (9,253 Python tests, 429 web tests); environment publication, direct edits and the helper remain open. Main already included PR #68 when this continuation began; lab 18 merged as #69.
- **The user paused new work before a break.** Their words: "don't stop things in process, but start anything new. i want to a take a break soon", read as *don't start anything new*, which they did not correct. Everything in process then finished and merged. **This pause was lifted when the user said "we can continue here" on 2026-10-11.**
- **Milestones** ([roadmap](../docs/ROADMAP.md), [implementation plan](../docs/IMPLEMENTATION-PLAN.md)):
  - M0: specs and decisions, nearly done. Approved: Conda provisioning, research artifacts, consistency review. Decided on their main questions: living documents, ledger. Still drafts for the user to review: tools, PDF sources, skills, plans and delegation.
  - M1: done (its exit run passed in lab 9).
  - M2, phase 1 on babbage: partially implemented. Worker execution and several research-workspace slices have recorded acceptance; the complete integrated M2 workflow remains open.
  - M3 to M5: not started beyond keeping the design compatible.

### Merged on 2026-10-11 (the latest first)

| PR | What |
| --- | --- |
| #67 | Living documents, first slice: parser with stable block and math IDs (both parsers checked on shared cases in CI), an SQLite control store (D85, proposed), the agent's document tool with rebased or rejected patches, save-time math normalization, front-matter macros with bounds, and a read-only document view and history in the GUI |
| #66, #65 | Background jobs (D83, confirmed): the tools spec's section, with agents waiting on jobs and a low-cost watcher model checking them. Specified, not built |
| #64 | The provisioning tool (D84, proposed): `provision` and `provision_status`, dry runs in the sandbox, AGPL refusal, policy decisions, file-set permission requests honored by the middleware as grants, installs in the environment's turn, probes, failure records |
| #63 | Lab 17 report |
| #62 | A user unit that keeps the MCP proxy's forward up across sandbox restarts (`examples/systemd`) |
| #61 | Math repairs in the workspace, adapted from the user's cscience-crypto code (MIT): delimiters, rule repairs, the reviewer model, Unicode fallback, a lexer that refuses formulas defining macros, a page-wide KaTeX time budget |
| #60, #59, #57 | Spec drafts and the user's decisions on living documents, the ledger, research artifacts and consistency review (D65 amended, D81, D82) |
| #58 | The workspace GUI, first slice (D80): Svelte app and aiohttp server, read-only |
| #56, #55 | Per-sandbox package-cache budgets; event-store checkpoints in the journal |
| #54 | Package commands take turns, with a revision and a lockfile for each change (Conda provisioning, first slice) |
| #53 | Shared data across a project's environments (D79) |

## What the user expects

Standing instructions, given over the session:
- **Keep advancing the plan autonomously**, installing software as needed (the pause above was lifted). **Merge pull requests into main** once CI is green. **Use several agents in parallel.**
- **Decision requests in plain language**: the practical effect, an example, and a recommendation, before asking. The user is a cryptography researcher; explain platform terms (they asked what "shell" and "turn" mean).
- **Bug reports for upstream projects** go in `.agents/upstream/`, one file each; filing them is the user's call.
- Never enter real credentials; test tokens are visibly synthetic (`test-secret-…`).
- No sudo, and no change to system or security settings. No persistent configuration (such as systemd units in `~/.config`) without permission; transient `systemd-run --user` units are fine.
- Downloaded files are untrusted: put them in their own directory, check checksums, and run Python on them with `-I`. npm installs use `--ignore-scripts`.
- Docker is shared with the user's own projects: only create, tag or remove `sandbox-science/*` images, and never prune.
- Tear labs down completely. Never run destructive commands on real data, even as a test.
- Commits are signed through the user's SSH configuration (plain `git commit`). No attribution lines in commits or pull request descriptions. Branch before committing; main changes only through merged pull requests.

## Waiting on the user

Nothing here blocks the queued work: each item has a default in place.

- **Blockers:**
  - a real model needs the user's LiteLLM gateway URL and a virtual key, without which the M2 exit run cannot pass;
  - crypto-research-mcp needs the user's OAuth consent for the connector auth service.
- **Decisions from this session, each with a recommendation:**
  - LD-41: `\(x\)` followed by a digit, or holding a dollar or a line break, stays in backslash form, since `$$…$$` would render it as displayed math.
  - The escaped-bracket rule (#61): within a paragraph, `\[…\]` is math only when padded inside both brackets or holding `\ ^ _ = { } + < >` outside Markdown escapes, and `\[^1\]` stays a footnote.
  - Formulas that define macros are shown as written, where LD-28 would apply the definition.
  - Front-matter macro limits (#67): 200 macros, 1,000 characters each, none defining another or using an argument twice, at most 2,000 characters of expansion per formula.
  - D85, the SQLite control store.
  - D84's details: one permission request per install; a waiting operation keeps its environment's turn; Conda and pip packages in separate requests; pip from wheels only; AGPL exceptions in the policy; a catalog only the platform extends; the CP-13 admission prerequisite refuses new work and passed Lab21, with PR #85 merged after all three fresh CI checks passed; restoration remains open.
  - 60 model repairs per browser session per hour.
  - Whether to file `.agents/upstream/katex-trycombinechars-quadratic-splice.md`.
  - KaTeX in a Web Worker as a slice of its own.
  - Living documents' question 15 (spec index): stray and renamed files under `documents/`, edits from overlapping calls, and the helper working only during a call.
  - Background jobs' details: 24 hours by default and 7 days at most; 4 at once in a session and 8 in an environment; closing a session cancels its jobs; no background cells.
- **The four draft specs:** tools and PDF sources are built and running as drafted (recommend approving them as drafted); skills, and plans and delegation, have real choices (seven open questions each), to go through with the user one question at a time when the work reaches them.
- **Older questions:** STATUS.md's *Questions with a recommendation* (images, the MCP proxy, the event-store timer, the GUI's origins, PSF-2.0 and MIT-0 in tier 1, the crypto-analysis-skills copyright holder).

## Queued work, in the suggested order

1. **Lab 18 on babbage — completed 2026-10-12** ([report](docs/agent-reports/babbage-openshell-lab-18-2026-10-12.md)) for the provisioning tool, with the checks #64 lists:
   - real dry-run output and pins in a sandbox;
   - CP-F1 to F4, F8 and F10;
   - the grant ending with its operation;
   - the catalog's probes (python-flint unchecked);
   - an install cut short.

   Use [`labs/`](labs/README.md).
2. **Background jobs and executor recovery passed runtime acceptance** (D83; [as built](../docs/BACKGROUND-JOBS.md), [lab 19](docs/agent-reports/babbage-openshell-lab-19-2026-10-12.md), [lab 20](docs/agent-reports/babbage-openshell-lab-20-2026-10-12.md)). Jobs, calls and kernel globals survive worker restarts under the same daemon, with authoritative bounded replay. The initial daemon-loss probe left an escaped child alive because OpenShell denied peer ownership reads. The corrected original-VM stop/start fence passed the expanded checks, preserving disk and losing old process/kernel identity; sandbox deletion and idle endpoints also passed. All Lab20 state was removed. Automatic wake-up, the live message inbox, persisted watcher settings and the shared spend ledger remain later control-application work.
3. **W4, living documents slice 2:**
   - user edits and conflicts in the GUI (LD-13, LD-14) — completed with [browser acceptance](docs/agent-reports/document-editing-2026-10-12.md), including reviewed patches, reverts and workbook promotion;
   - comments, anchors and work requests (LD-21 to LD-24) — completed with [browser acceptance](docs/agent-reports/document-comments-2026-10-12.md); artifact/PDF anchors and annotation export remain open;
   - the canonical document catalog/staging prerequisite is implemented and independently reviewed ([worker](../docs/WORKER.md#document-catalog-prerequisite)); the [control publication outbox](../docs/LIVING-DOCUMENTS.md#publication-outbox-prerequisite) records source changes atomically and exports complete pinned targets with bounded recent history. The [manual delivery journal](../docs/LIVING-DOCUMENTS.md#manual-catalog-delivery) transfers exact targets and reconciles worker storage acknowledgement with explicit approved bindings. Files under `documents/`, direct edits and `ss-doc` (LD-49 to LD-52) remain coordinated follow-on work;
   - the internal [worker document source adapter](../docs/WORKER-DOCUMENT-SOURCE.md) now retains exact captures/refusals and replay receipts with explicit worker provenance and named patch authority. Its trusted binding/capture arguments still require future authenticated transport and writer proof; no public ingress or filesystem restoration is enabled;
   - before enabling runtime replacement/removal, complete the [protected-filesystem feasibility prerequisite](docs/agent-reports/document-writer-guard-2026-10-12.md). The investigation is source-only: existing peer `/proc` checks and notification counters cannot prove every arbitrary writer closed. FUSE is proposed, not implemented or validated under OpenShell. Prove handle/mmap lifetime, namespace exclusion, capture/ack revalidation, capacity and failure/restart behavior while preserving healthy kernels and workload confinement. Keep uncertain state held and LD-49 open; Lab20's VM recycle is lifecycle cleanup, not ordinary document sync;
   - document-revision math findings (LD-53 to LD-55, D82) now connect atomic saves, historical block ownership, mechanical checks/findings and closing-only delivery ([as built](../docs/MATH-CHECKS.md)); independent review, browser acceptance and final combined local gates pass (9,459 Python/434 web tests). PR #83 merged after fresh CI passed. Comment/reply checks and ordinary remediation passed the independently reviewed local/browser slice above; ledger and finding-prose checks/remediation, idle dispatch and automatic history compaction remain open.
4. **W4, automatic consistency review** (approved spec): implementation, independent review and [browser acceptance](docs/agent-reports/consistency-review-2026-10-12.md) passed, including exact confirmation scope without consuming author-work retries. Scripted fixtures establish plumbing and authority, not real-model judgment. Follow-on work includes automatic execution-output inventory/reviewer ingestion, live message/final-answer transport, idle scheduling, shared monetary accounting and ledger/finding-prose D82 integration.
5. **W4, the ledger, board and frontier** ([ledger](../docs/specs/ledger.md)).
   - The [immutable artifact inventory](../docs/ARTIFACTS.md) first slice implements explicit harness publication, bounded exact worker transfer, immutable control revisions and internal pinned/latest/evidence reads. Durable unknown intents hold rather than republishing mutable paths; known pins survive transfer/retention refusal. Actual producer and observed inputs remain unknown. Independent review and all local combined gates passed (50 focused cases, 9,415 Python/431 web tests); PR #82 merged after all fresh CI checks passed. The bounded explicit-publication reviewer consumer passed independent review, D82/main integration and [browser acceptance](docs/agent-reports/artifact-review-2026-10-12.md). Its final local gates passed 9,494 Python tests (43 opt-in skips), 436 web tests and all required checks/builds; PR #84 merged after all three fresh CI checks passed. It copies exact <=64KiB raw UTF8 under session snapshot grants, records finite unresolved-operation observations and supports typed pinned artifact fixes. Empty sources stay exact; binary/non-UTF8/oversize sources remain incomplete. Automatic execution-output inventory, imports/retention, rich parsing and PDF/GUI consumers remain open.
6. **The rest of W4:**
   - the PDF viewer with anchors;
   - skills and the external crypto skill pack;
   - plans and delegation;
   - chat in the GUI;
   - approvals in the GUI (D54).

   Skills, and plans and delegation, need the user's review of their specs first.
7. **Provisioning slice 3:**
   - restoring a revision after a failed install (CP-13, CP-20);
   - disk limits;
   - uninstall and upgrade;
   - the GUI (CP-26).
8. **Worker:**
   - environment IDs per project, and limits per client;
   - the recorded lockfile timing flake is addressed: the original hostile inputs and 0.25 s bound run in a fresh interpreter, and deterministic canonical-byte/size-cap checks accompany bounded per-entry serialization. This excludes prior test heap history, while host scheduling remains a limit of timing checks.
9. **MCP:** connector auth (blocked), the proxy's refinements in the backlog. **A real model** (blocked). Then the **M2 exit run**: paper → Sage → document → the user's feedback → automatic review → board update, on babbage.

## How the work has been done

- **One slice per agent**, launched with `isolation: "worktree"` and a written brief.
- **What a brief holds:**
  - the spec requirements and fixtures to implement, and what is out of scope;
  - the files to read;
  - the conventions and the user's constraints above;
  - a scratch directory of the agent's own in the session scratchpad, which agents share;
  - "open a PR, make CI green, do not merge, and report in your final message".

  Agents report only in their final message, and follow-ups go to the same agent (SendMessage), which keeps its context.
- **Adversarial review for every substantial code PR**, by a separate agent that does not modify the repository. It reports findings by severity, each with a file and line, a scenario, a probe and a fix. The author fixes them, then the reviewer verifies the fixes and looks for regressions the fixes caused. Repeat until nothing critical, high or medium remains. Rounds have always found real problems, including in fixes: #53, #54 and #61 each took three.
- **Labs on babbage** check what only OpenShell shows: the seccomp filter, the VM's file system, the middleware's headers. Run a lab before merging when a PR's behavior depends on the runtime, testing the branch from its worktree's virtual environment.
- **Merging:**
  - `gh pr merge N --merge`, then in the main checkout `git fetch && git merge --ff-only origin/main && uv sync --locked`;
  - then remove the merged worktrees with `git worktree remove` once they are clean and their commit is on main;
  - decision numbers go to the next free D-number on main at merge time: two branches once both took D83, and renumbered.
- **Docs move with the code:** STATUS, BACKLOG and PLAN-CHANGES change in the same pull request. Lab reports go in `.agents/docs/agent-reports/`, with the index and `INVENTORY.json` hashes.

## Gotchas

- **The lab's `env.sh` breaks `gh`.** It points `XDG_CONFIG_HOME` at the lab, so `gh` in that shell asks to log in; use another shell.
- **Watching CI too early.** `gh pr checks N --watch` started right after `gh pr create` can exit at once with "no checks reported". Wait a few seconds.
- **Agent transcripts are huge.** Never read an agent's output file directly; extract the last tool calls with a short script if you must check on one.
- **The forward unit restarts forever.** It runs with `Restart=always` and no start limit, and is stopped explicitly; `labs/lab_down.sh` now does it.
- **The main checkout is live during a lab.** The lab's cache and middleware run from the main checkout's editable install, so don't switch branches there during a lab: use worktrees.
- **AppArmor and Claude Desktop.** babbage restricts unprivileged user namespaces through AppArmor, and Claude Desktop's shells are exempt, so qualification checks run through `systemd-run --user` (see the memory note).
- **The scratchpad is per session.** The lab scripts were copied to [`labs/`](labs/README.md) for that reason. Briefs and probes from this session are not kept.
- **A memory directory is loaded each session.** It is at `~/.claude/projects/-home-andy-Git-Projects-sandbox-crypto/memory/` and has notes on babbage's AppArmor, decision requests, autonomous merging, and these lab and CI gotchas.
