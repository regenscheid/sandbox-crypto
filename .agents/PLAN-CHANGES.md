# Plan changes

Dated list of changes to the plan, decisions and conventions ([conventions](CONVENTIONS.md)), newest first, one or two lines each. Progress and lab results are in PR descriptions, [lab reports](docs/agent-reports/) and the git log. The earlier long-form log is in git: `git show 0e6a662:.agents/PLAN-CHANGES.md`.

## 2026-10-15

Decision on lab 38 (#305), which the owner can reverse:

- **A comment starts a run** ([run service](../docs/specs/run-service.md), RS-6b; LD-22). A work request to an idle session starts a run as a chat message does, within the model budget, and it ends a job wait. Requests get no `held` or `refused` state, which would need a migration: where a message would be held, the run service keeps the request from starting a run until the session's next run or the service's restart, and over the budget it looks again every 15 minutes.

Decisions of the owner for the M2 exit rerun:

- **The MCP provider from the owner's file** ([MCP connectors](../docs/MCP-CONNECTORS.md#crypto-research-mcp-a-cloudflare-access-service-token)). A helper (`sandbox_science.launcher.access_provider`) registers `crypto-research-mcp` from the owner's `cloudflare.env`, the approved alternative to typing the token in. It refuses a file others may use, and passes the values only to the one OpenShell command that registers them, through its environment. "For now", it is babbage's default: `[mcp_proxy] provider_env_file` makes `up` run it whenever the gateway lacks the provider, as each lab gateway does, or on `up --refresh-provider`. The launcher never opens the file, and agents never do.

Decisions of the coordinator on the review of the ledger's gate (#276–#281), which the owner can reverse:

- **What a check or review covered** ([ledger spec](../docs/specs/ledger.md), LG-17). A check or review counts for a later revision only if it keeps the statement, fields, `assumes` links and every evidence link that bears on the entry: `supports`, `derivation` and `contradicts` of a type that can count. LG-17 named the `derivation` targets alone, which let evidence of either sign added after a review pass as reviewed. Reviews and `refuted`, `withdrawn` or `superseded` entries are not evidence that counts, and any `proof` that counts needs the independent check.
- **The user's check** (LG-17). The user's independent check counts only as the `independent_check` link the user added themselves, at the revision they checked; anything the user wrote no longer counts because an agent cites it. The workspace (a same-origin POST that carries the revision read) and the harness's `ledger check` command record it, so proved results can wait for the owner's check until delegation exists after M2.

Decision of the coordinator on the review of the project memory (#283–#291):

- **Hidden characters in text agents read**. Text the user approves and agents then read holds no character the user may not see: the server refuses, rather than strips, controls but the tab and the line feed, format characters, surrogates, private-use and unassigned code points, line and paragraph separators and every Default_Ignorable code point, written as they are or as character references (`check_agent_text`), and the GUI shows such text as its exact string, each hidden character as its code point (`ApprovalText`). The zero-width joiner and non-joiner and the left-to-right and right-to-left marks are refused too, for now: research text rarely needs them, and an agent that meets the refusal writes without them. **Reversible:** an allowlist can take them back. The project memory uses it first; documents, plans, findings and chat follow.

## 2026-10-14, later (PRs #165–#258)

Decisions of the owner since the last sync (PRs #165–#258):

- **Reviews are by risk** (the owner, to save tokens). Security paths, storage migrations, concurrency and new features get a full adversarial review. Code moves, docs, test-only PRs and fixes that implement a reviewer's suggestion get a light check. [CONVENTIONS](CONVENTIONS.md#how-work-is-done) says so.
- **Packages after the M2 exit lab** (the owner agreed). `orchestrator/control` and the domain packages (documents, mathchecks, consistency, ledger) move out of the orchestrator into packages of their own. The orchestrator keeps thin agent tool adapters, possibly in `orchestrator/tools/`. The workspace depends on the domain packages directly, not on the orchestrator. Phase 5 of the plan.
- **A real model.** The gateway is `https://litellm-api.npages.org/v1` (only `/v1` is exposed; the root answers 404). The first live call through `OpenAICompatibleModel` worked with `arrcodex-gpt-6-luna` (25 tokens in, 8 out). The owner's alias `sandbox-orchestrator` works since the owner added it to the key's allowed models. It serves MAIN, the reviewer and the watcher until the owner sets an alias per role. The harness key is a file in the owner's secrets directory (`litellm-harness.key`); agents never read it. Nothing waits on the model any more.
- **crypto-research-mcp.** It is at `https://research-mcp-api.npages.org/mcp`, and the Access service token alone is enough: an `initialize` with the two Access headers got a normal answer (server `cloudflare-mcp-portal`, protocol 2025-11-25), and without a valid client ID the server answers 401 with an OAuth challenge. With a Service Auth policy per server, the token sees 72 tools from seven servers (attack-catalog, e-print, firecrawl, nist-csrc, paper-search, patent-search, web-fetch) and three portal tools. So the connector auth service stays off M2's path. The token is in a file in the owner's secrets directory (`cloudflare.env`) that agents never read. [MCP connectors](../docs/MCP-CONNECTORS.md#what-the-server-offers) and D66 record it.
- **MCP cost control is the services' job.** The servers are self-hosted and effectively free. A tool is escalated for its effects, never for its cost. The project policy can allow the research servers' tools and keeps denying the `portal_*` tools that configure the portal. [Threat model](../docs/THREAT-MODEL.md) A4, [MCP connectors](../docs/MCP-CONNECTORS.md), D67, [policy](../docs/POLICY.md) and `examples/policy.yaml` follow: firecrawl's agent, crawl, extract, parse and scrape are allowed, and the tools that act on the world (feedback, corpus refreshes, live browser sessions, monitors) still ask. A project policy that allows every tool escalates none, and then `--admit-escalated` is not needed. Project-side MCP cost controls are a low-priority backlog item.
- **No route from sandbox code to the platform.** Only agents act, through tool calls, as in Claude Code. LD-52 (the `ss-doc` helper) and SK-20 (helper calls to the platform API) are withdrawn, skills question 5 is moot, and the ledger question 6 settlement of 2026-10-11 is reversed: code hands results to an agent in a results file, and the agent decides to import it (LG-13, LG-41).
- **The ledger** ([spec](../docs/specs/ledger.md), approved in #250). It has seven entry types, with `reference` added to the six drafted ones. Experiments and conjectures are issues, and a proved conjecture becomes a claim that answers its issue. The ledger holds the big picture and a session plan holds the work inside a session (LG-40). Pack JSONL comes in once, by a deliberate import; the platform API is the only other way in.
- **Lessons go to a project memory,** not to a ledger `lesson` type. The [spec](../docs/specs/project-memory.md) (#251, approved, not built) makes it a memory tool with a small store of its own: agents propose, the user decides, and every agent role receives the accepted entries.
- **Living documents** ([spec](../docs/specs/living-documents.md)). Agent patches apply unless the document's switch takes them as suggestions (LD-20). The user picks the session that gets a comment's work request (LD-22). The user resolves the threads the user opened, and an agent replies in a thread when it acts on the thread's comment (LD-24). Comments go into exports only on request, as a separate file (LD-38).
- **Files that executions and delegated runs write** are recorded as artifacts by default, with ignore patterns, bounds, tombstones for deleted files and a "results only" project setting; a delegated run names its result files (RA-27 to RA-39). Approved in the [research artifacts spec](../docs/specs/research-artifacts.md#files-from-executions-and-delegated-runs) (#251, not built). The worker reports deleted files since #258 (protocol 5); the harness's recorder is next.

Decisions made while building, by the coordinator under the owner's mandate (PRs #165–#258):

- **Ledger** ([as built](../docs/LEDGER.md), #252–#255). Entries, revisions and mutations use the shared pieces: receipts with scope `ledger`, the event log with subject `ledger_entry`, and text revisions of kind `statement`. A mutation changes up to 32 entries, all or none, and a stale expected revision is a conflict, never rebased. The main agent creates and revises entries, a delegated run only creates them in their first status, and other roles and code in a sandbox are refused. Entry text and results files are untrusted data. Decisions and the gate, evidence, the frontier and flags are next.
- **Run service** (#194, #206–#209). The workspace and the service share the control store: the workspace writes message rows and the service polls them. This departs from the plan's wording that the workspace proxies the service. A project's MAIN budget is 2,000,000 tokens over a rolling 24 hours. A new run carries up to 100,000 characters of earlier tasks, messages and answers. A cancel saves a record; a run that dies ends `interrupted`, and its messages are held until the user resumes.
- **MCP client** (#227, #231, #232). Connector tools are listed at a run's start through the proxy and are `effect` tools, so the plan fence applies. The harness's proxy token is a file only it reads. An escalated call comes back as `approval_required` and is answered with the proxy's `pending` and `approve`. `compile-policy --admit-escalated` is opt-in.
- **PDF viewer** ([spec](../docs/specs/pdf-viewer.md), #228, #234, #235). A second origin for the viewer, capability URLs for PDFs, a sandboxed frame, PDF.js with its tier 1 parts only and no WebAssembly.
- **Delegation** ([spec](../docs/specs/plans-and-delegation.md), #225). It is built after the M2 exit lab, as roles of the one loop. The run tree's parent link (#233, PD-32) and kernels per run (#237, PD-37, worker protocol 4) are built; nothing starts delegated runs yet.
- **Kernels use abstract Unix sockets** instead of TCP ports (#179), which removes the kernel-start race under load. Sessions that share an environment are not isolated from each other's kernels; D41 and the threat model say so.
- **Control store.** Tables check their values when written, and reads take rows as they are (B-10, decision 3, #177–#183): migrations rebuild a table with `CHECK` constraints, and a store a migration refuses is left unchanged with a message that names the table. Checks and saves record the macro revision they used and derive the context when read (B-12, decision 7). Receipts, the event log, leased notices, feed cursors, text revisions and the math-check queue are one piece each (B-04, #240–#248).
- **Worker.** One worker per state directory, enforced by a lock (decision 9, #224). The worker store has a version and an `application_id` (#217). The daemon pushes events to its bridge, and replay stays for reconnects (#246). `manager.py` is a 543-line facade (C-03).
- **CI** (decision 11, #182, #256). Pull requests run static checks and the fast and slow tiers on Python 3.13. Main and the nightly run add the serial full suite on 3.12 and 3.13. Per-test limits and a deadline make a stalled run print what was stuck.
- **Approvals** (#188, #192, #223). A typed `PermissionRequest`, one `resolve` state machine for the middleware and the MCP proxy, and one request for concurrent identical escalations.

## 2026-10-14, earlier (PRs #141–#162)

The owner asked for autonomous work through the [refactoring plan](refactor-plan/PLAN.md), with its recommendations adopted. Each item marked *adopted* was adopted on 2026-10-14 under that instruction.

Decisions made while carrying out Phase 1 and Phase 2 (PRs #141–#162):

- **Save pattern (#162, E-05):** a browser save keeps its operation ID after every failure, a 401 or sign-out included, and forgets it only after a result, so a save whose outcome was lost is never applied twice.
- **Project lock files stay (#160, A-07)** until a plan edit checks its session in the plan's own transaction (B-17); then they go.
- **Control store re-baselined (#152, B-02; decision 6):** one migration list from a baseline at version 2, with `application_id` `0x53536373`. Stores from before it are refused, never changed, with the export path in [living documents](../docs/LIVING-DOCUMENTS.md). Project JSON files are not imported (#160).
- **Worker protocol (#148, #161; decision 8):** protocol 2 with D-01's check, then 3. Any change to what crosses the worker API raises `PROTOCOL_VERSION` ([compatibility](../docs/COMPATIBILITY.md)).
- **Kernel check (#156, A-04; decision 4):** the check keeps nothing between runs, so each run is told again about every kernel that still predates the packages.
- **Worker usability (#153, C-04):** one record per environment with each fact as its own field, not the notes' single state enum, since recovery, fencing and releases overlap.
- **No deep re-validation in the browser (#154, #157, E-04; decision 3):** the server's answers are checked only for scope; their types come from the server's models through `scripts/generate_api_types.py`.
- **No user-message hook yet (#150, A-02):** nothing writes messages during a run until the run service (A-06), so the hook waits for it (rule 1).
- **Run records (#143, #149, A-02):** `RunRecord.delivered` (notice IDs by source) and `RunRecord.summaries` (by hook) replace the per-source fields; older records load through a validator.
- **Test support (#141, C-13):** `WorkerApi`, the worker API as tests use it, lives in `packages/testing` ([CONVENTIONS](CONVENTIONS.md#repository-layout)).

- **Convention: an injected clock and one test-support package** (#137, plan Phase 1). Components with timing take a `Clock` (`contracts/clock.py`) and their timeouts as constructor parameters, defaulting to `SystemClock` and today's values; tests use `FakeClock` and `eventually()` from the dev-only member `packages/testing` instead of sleeps and patched constants.
- **Confirmed (adopted):** D84 (provisioning operations), D85 (the SQLite control store), D59 (pdf_oxide) and D43 (isolation tiers and the probe rule); PSF-2.0 and MIT-0 in tier 1 (D45). Plan decision 12.
- **Specs approved as drafted (adopted):** [tools](../docs/specs/tools.md) and [PDF sources](../docs/specs/pdf-sources.md), with their proposed answers. This settles D83's details: jobs run 24 hours by default and 7 days at most, 4 at once in a session and 8 in an environment, and closing a session cancels its jobs.
- **Kernel notices (adopted, decision 4):** the durable kernel-notice inbox is replaced by a stateless check before each turn, plus the disclosure every execution result already carries (A-04).
- **GUI chat (adopted, decision 5):** a small orchestrator run service, pulled forward from W5, which the workspace proxies; one active run per session, with messages queued to its next safe point; a new message carries earlier tasks and final answers, not transcripts, and may start a run within the project's model budget.
- **Development data (adopted, decision 6; D85 amended):** documents are exported, then pre-M2 `control.sqlite3` files may be discarded and the schema re-baselined, dropping legacy migrations and reader branches.
- **Math checks (adopted, decision 7; D82 amended):** only the current revision is checked, when the checker reaches it; one Node process per batch of formulas; a check records the macro revision it used.
- **Compatibility (adopted, decision 8):** worker and orchestrator release in lockstep until M3, with a protocol-version check when they connect and a written policy for when independent upgrades are needed.
- **Worker operations (adopted, decision 9):** one worker per state directory, enforced by a lock file; the stdio transport for development and tests only; an unreachable environment fences only itself; for M2 the whole-VM restart on daemon loss stays, and the user is told which kernels were lost.
- **Planning fence and plan approval (adopted, decision 10; D86 amended):** a policy revision does not block a running session's effect tools, since the worker enforces the current policy; the unused `awaiting_approval` state goes until plan approval's default is decided.
- **CI tiers (adopted, decision 11):** PRs run Python 3.13 fast and slow tiers; 3.12 and the serial full run move to main or nightly (Phase 1, after the tests are deterministic). New dev dependencies: pytest-xdist, pytest-timeout, pytest-repeat, prettier and prettier-plugin-svelte.
- **Short tracking files (adopted, decision 11; F-02):** STATUS (under 10 KB) and the handoff (under 8 KB) are replaced, not appended to; this file lists decisions only; the plan's working rules and the way work is done move into [CONVENTIONS](CONVENTIONS.md#working-rules); commits and PRs carry no attribution lines.
- **Other plan recommendations (adopted, decision 12):** approvals re-authenticate by re-entering the login token (D54 amended); the display-repair input descriptor is to be removed (D86); Anchor and Mutation go in `contracts`; work on passive inspectors stops until the M2 path exists.
- **The owner's answers to the plan:** M2's exit is a testing checkpoint, with delegation and MCP connectors right after it as the first milestone for real use; GUI approvals and document files in environments are deferred. The unwired delegation and document-sync code is kept on the tags `archive/delegation-2026-10` and `archive/document-sync-2026-10` and deleted from main; defenses beyond the threat model are retired.
- **Delegation's shape (the owner):** roles of the one tool loop; at least 8–10 subagents at once in a session; one extra level of nesting; no kernel sharing between parent and child. Recorded in the plans-and-delegation spec (#225).
- **Raw lab evidence out of git (the owner):** removed from all history and kept privately on babbage; reports name archived files by SHA-256, and `scripts/check_repo_hygiene.py` guards CI. This was the one exception to the rule against rewriting published history.

## 2026-10-13

- **Parallel Conda work (the user):** provisioning slices (pip license metadata, installer observations, named upgrades) run in parallel with delegated execution.
- **Overnight autonomy (the user):** continue autonomously; a heartbeat task, *Retry stalled orchestrator*, resumes stalled work after five minutes with the same model.
- **Delegation prerequisites built unwired:** records, run-kernel bindings, the child dispatcher, own-track progress, the child-attempt journal, the controller and its transport. The refactoring plan later archives them (decision 2).
- **CP-26 split:** the provisioning view ships as a read-only viewer, a two-lockfile comparison and a package and license inventory; operator permission answers stay separate.

## 2026-10-12

- **D86 (the user):** a planning phase with live plan progress, and original system and stock prompts written independently of Claude Science (D56). The user added: test scripts on a small scale before long computations.
- **More parallel agents (the user):** independent backlog items run in separate worktrees, with authors rotating onto each other's reviews.
- **D84's implementation amended:** an install that changed packages, or whose effect nobody saw, leaves sticky `needs_recovery`, and the CP-13 admission step refuses new ordinary work until recovery ([recovery fence](../docs/RECOVERY-FENCE.md)).
- **D85's store extended:** job control records, project macro revisions, export-input snapshots and operation receipts share `control.sqlite3`.
- **Document files need writer proof first:** a protected-filesystem feasibility step precedes runtime document replacement (LD-49). FUSE is a candidate, not a choice.
- **Artifacts:** explicit harness publication comes before automatic execution-output inventory and its consumers.
- **D82's checker:** the existing Node runtime runs a standalone KaTeX bundle in the control plane.
- **GUI distribution notices:** builds ship complete license notices for bundled code; OFL fonts and glyph-derived SVGs keep their terms, and the tier 1 allowlist is unchanged.
- **Workspace exception clarified:** the workspace reads harness records and makes authenticated same-origin USER changes to documents, annotations and findings through the orchestrator's stores, until W5.
- **Roadmap:** M1 passed; M0's review and M2 remain open.
- **The user resumed work** after a pause before a break (no new work had been started during it).

## 2026-10-11

- **The user's decisions:** D68–D78 confirmed; shared data across a project's environments by worker-synced copies (D79); the read-only root filesystem deferred (D48); the Conda provisioning, research-artifacts and consistency-review specs approved as drafted; D80 and D81 confirmed.
- **D81 (the user):** delegated agents are runs within their session, with kernels of their own; an environment of their own is a later opt-in.
- **D65 amended and D82 (the user):** living documents are also Markdown files under `documents/` in every environment, with direct edits merged; KaTeX-rejected math in an agent's text becomes a `math` finding for that agent.
- **Living documents and ledger drafts (the user):** their main questions approved as drafted; RA-1, RA-24, CR-10, CR-17, CR-25, CR-27, CR-28 and CR-29 amended, and CR-36 to CR-38 added.
- **D83 (the user):** long computations run as background jobs; agents usually wait on them while a low-cost watcher model checks in, a fourth role in PD-27.
- **D84 (proposed then):** provisioning operations settle the Conda spec's open questions 3, 5 and 6: one permission request per install, AGPL exceptions in the policy, a catalog only the platform extends. Orchestrators upgrade before workers.
- **D85 (proposed then):** living documents in one SQLite control store, `control.sqlite3`; projects and runs stay JSON for now. markdown-it-py and mdit-py-plugins are pinned to match the browser's parser.
- **D80 (the user):** the workspace GUI is a Svelte app served by aiohttp. Browser apps live in `web/<app>/`; the workspace may use the orchestrator's stores until W5; CI gains a web job and an npm license check.
- **Math repairs (the user):** the user's own LaTeX repair code from `cscience-crypto` is adapted under MIT; failures are kept for an hour (LD-46); 60 model repairs per browser session per hour. Open for the user: the escaped-bracket rule, macro definitions shown as written, front-matter macro limits.
- **Living documents' question 15:** an ambiguous `\(…\)` stays as written rather than becoming `$$…$$`; recommended, for the user.
- **D69 amended:** the worker, not the orchestrator, admits hosts on demand, since it holds the gateway's credentials; the compiler no longer emits a host-less endpoint.
- **D73 (proposed then):** the worker creates research sandboxes, keeping gateway credentials out of the orchestrator.
- **D74 (proposed then):** a hash-chained SQLite event store, used by the policy middleware.
- **D75 (proposed then):** the MCP proxy has its own JSON-RPC layer on aiohttp instead of the MCP SDK (D67); the policy gains `mcp.proxy.max_argument_bytes`.
- **D77 (proposed then):** the base profile image contract for `minimal` and `crypto-base`; tier 2 names conda-forge Python's runtime libraries.
- **D43 amended and D52:** the worker probes each executor's isolation tier, and evidence only lowers the declared tier; a backend conformance suite runs through the worker API.
- **W1:** proposed that shell calls keep no state and that a cell exception is its own error kind; the first slice uses its own tool loop, an aiohttp model client and jupyter_client.
- **M1's exit run passed** on babbage ([lab 9](docs/agent-reports/babbage-openshell-lab-9-2026-10-11.md)).
- **Handoff and labs:** a handoff file for the next agent; lab scripts live in `.agents/labs/`.
- **Upstream reports (the user):** bugs found in upstream projects are described in `.agents/upstream/`; filing is the user's call.

## 2026-10-10

- **D57 amended (the user's choice):** the project policy names the hosts each provider credential may reach; the middleware refuses credentials over plain HTTP.
- **Threat model:** monitoring no longer relies on OpenShell's prover for policies with middleware or MCP endpoints (A3).
- **D71 (direction confirmed by the user):** a pull-through package cache instead of a mirror, for PyPI, conda, Cargo, Go and npm, then opam and Hackage.
- **D68 extended (the user's request):** `allow_credentialed` lets a read-only rule with `on_timeout: allow` also pass credentialed requests as alerts.
- **D69 amended (the user's choice):** the compiler names every public host the policy mentions, and admits others on demand later.
- **D69 (direction confirmed by the user):** OpenShell's proxy carries traffic and the platform's policy middleware decides each request; the separate egress proxy (D44) is dropped for HTTP and git.
- **D70 (proposed then):** the policy middleware service.
- **D68 (proposed then):** threshold alerts move to a `monitors` section; tool patterns resolve exact names first; `on_timeout` is `deny` policy-wide, with alerts only for read-only rules.
- **Policy file dialect:** policies are strict YAML: one-line ASCII values, bounded line widths, no backslash escapes, so a file reads as it is enforced.
- **Example policy:** crypto-research-mcp tools classified one by one; feedback, crawl and refresh tools ask first; portal configuration is denied.
- **Tier 1 licenses** add PSF-2.0 (typing-extensions) and MIT-0 (cffi); CI checks installed licenses and generated protobuf code.
- **Conventions adopted:** pyright strict for `packages/*/src`, standard mode for tests.
- **Plan snapshots tagged:** `plan-03-pre-review` and `plan-04-reviewed`.
