# Revision and refactoring plan

> **Baseline after the history rewrite.** These reviews ran on main `fa456bc`. On 2026-10-14 raw lab evidence was removed from all history, so that commit is now `6e9608a`. The rewrite changed no code, so every `file:line` cited here still holds.

**Status:** adopted on 2026-10-14; the owner answered decisions 1–3 and 11 and asked for the rest of the recommendations to be adopted ([plan changes](../PLAN-CHANGES.md)). Baseline: main at `fa456bc`. Progress is tracked [below](#progress).

**Sources:** six read-only reviews run in parallel on the baseline ([brief](REVIEW-BRIEF.md)), with 97 findings in [`notes/`](notes/):
- A, the orchestrator core: 14 findings;
- B, living documents and the consistency review: 17;
- C, the worker: 18;
- D, the infrastructure packages: 13;
- E, the workspace GUI and its server: 21;
- F, tests, CI, repository hygiene and docs: 13.

Each finding there has its evidence with `file:line`, a concrete proposal, effort, risk and verification; this plan orders them and refers to them by ID.

## Progress

Findings done, by phase, with their PRs (main `959267c`). Leftovers of a done finding are in [BACKLOG](../BACKLOG.md#repository); decisions that refined the plan on the way are in [PLAN-CHANGES](../PLAN-CHANGES.md).

- **Phase 0, done:** A-01, A-10 (#125); E-02's 503 (#124); C-01, C-10, C-11, C-15 (#133); A-03, C-05, D-10, B-11, C-18, F-07 and `reviews.py` removed (#129, #138); F-01 (#123); F-02 (#126); F-10 (#126); F-12 and CI guards (#128); E-16 (#127). Pulled forward from later phases: the reference docs synced with the code, with inventory and link checks, which covers C-17, F-08 and most of F-09 (#131, #135, #136, #140).
- **Phase 1, done:** F-04 steps 1–3 and F-06 (#137, #139, #144); C-13 (#141); F-05 for the loop (#134), the workspace routes (#132) and the worker manager (#141); C-14 (#146); C-12 (#144); F-03 (#182, with the kernel-start fix #179, the leftover-process check #180 and the stall diagnostics #256). **Left:** F-04 step 4, the fast tier's remaining wall-clock waits, and recording the nightly repeat's flake rate.
- **Phase 2, lane O, done:** A-02 (#143, #149, #150), A-11 (#151), A-12 (#149), A-04 (#156), A-05 (#158), A-08 (#165), A-13 and A-14 (#166).
- **Phase 2, lane S, done apart from leftovers:** B-01 (#145), B-02 (#152, #159), A-07 (#160), B-14 (#168), B-17/E-12 (#176), B-10 in four parts (#177, #178, #181, #183), B-05 (#197), B-03 (#200), B-15 (#211), B-09 (#214), B-08 (#215), B-07 (#216), B-06 (#219), B-12 (#221). **Left:** B-10 for the findings tables and the prompt-provenance columns, and B-14's per-thread reads.
- **Phase 2, lane W, done apart from leftovers:** C-04 (#153), C-03 in all its stages (#155, #163, #164, #185, #186, #191, #196, #201), C-06 (#212), C-07 (#217), C-08's first slice (#222), C-09 (#224), C-02 (#246), C-16 in five steps (#226, #229, #236, #239, #242), C-17 (#245). Bugs found by reviews on the way: #210, #238. **Left:** C-08 for the executor endpoints, recycles and kernels, versions for the worker's other databases, and the local launcher (in progress, #261).
- **Phase 2, lane C, done:** D-01 (#148), D-02, D-07, D-11 (#161).
- **Phase 2, lane G, done:** E-01, E-02 (#147), E-03 (#154), E-04 (#154, #157, #174), E-13 (#157, #174), E-05 (#162), E-17, E-19 (#167), E-14 (#170, #171, #172, #184), E-15 (#173, #187, #189, #199, #203, #204, #205, #208), E-20 (#175).
- **Phase 2's exit:** `_run_task` is 55 lines with no subsystem checks; no source file disables private-usage checks, and the hygiene check refuses it; the control store has one schema version (18); `manager.py` is a 543-line facade; the workspace has one handler pattern. The characterization tests of each lane are unchanged.
- **Phase 3, done:** B-04 (#240, #241, #243, #244, #247, #248), D-03 (#188), D-04 (#192), D-06 (#195), D-09 (#223), E-10 (#187, #189, #199, #203, #204, #205). **Left:** `Anchor` and `Mutation` in `contracts` (they wait for their first caller), D-05 and D-12 (Phase 5).
- **Phase 4, started:** A-09 (#193); A-06, the run service (spec #194; #206, #207, #209); E-06 and E-07 in the browser (#213, #202); E-11 (#218, #220); E-08, the PDF viewer (#228, #234, #235); D-08, the harness's MCP client (#227, #231, #232); delegation's first steps, the run tree's parent link (#233) and kernels per run (#237), after its spec (#225); the ledger's backend in four steps (#252 to #255, after the spec decisions in #250); the specs for file recording and project memory (#251) and the worker's deleted-file report (#258, protocol 5). **In progress:** the ledger's views (#259, #260), the harness's file recorder, and project memory.

## The state of the code

**The foundations are sound.**
- The package boundaries hold: no deployable imports another, apart from the documented workspace→orchestrator exception.
- ruff and strict pyright are clean, and 11,222 tests pass, all six areas' suites included.
- The security boundaries match the threat model: the worker distrusts executors, the daemon authenticates its bridge, the middleware fails closed, and the GUI has its login, origin and Host checks and content policy.
- The core algorithms are well made and well tested: the document parser and patch engine, the policy evaluator, the package cache's adapters, the event store.
- The *Keep* section below lists what not to disturb.

**What is wrong is mostly structure, scope and process, plus a few real bugs:**

1. **Bugs found by the reviews,** each reproduced or confirmed in code:
   - A-01, on the M2 path: when a second workbook opens in a project, the running session's agent loses every tool with side effects for the rest of its run.
   - C-01: a bridge loss during a recovery orphans executions dispatched meanwhile; a job stays `running` until its limit.
   - A-10: a job notice on the last turn discards the final answer.
   - C-10: stopping the worker hangs while a client holds an event stream open.
   - E-02: with the worker unreachable, a GUI route answers 500 and shows the worker's address.
   - C-11: the CI flake's mechanism, a 50 ms wall-clock race.
2. **About 9,800 lines of production code nothing reaches** (A-03, B-11, C-05, C-18, D-10; F-07 counts 266 tests on them):
   - delegation: 4,501 orchestrator lines, plus about 2,900 in the worker, the worker client and the contracts, built on the unreviewed plans-and-delegation draft;
   - document files in environments and exports: about 2,400 lines.

   They add tables to production databases, 63 touch points in the worker's manager, and a publication hook that writes 3–4 rows per document revision.
3. **Missing seams at the centers, so each feature adds branches there:**
   - the agent loop: 29 subsystem checks, A-02;
   - the control store: 146 call sites through private transaction methods, about 74 tables at schema version 1, B-01 and B-02;
   - the worker's manager: one 3,114-line class, with environment usability tracked by seven flags, C-03 and C-04;
   - the workspace: four copies of one proxy pattern, seven error mappings and seven copies of the save pattern, E-01, E-02 and E-05;
   - the contracts: implicit compatibility rules, with operation identity that changes whenever a field is added, D-01.
4. **Defenses beyond the threat model, which cost code and tests without protecting against a modeled threat:**
   - the GUI proxies' per-login ownership proofs, E-01;
   - re-validating the stores' own rows on read, B-10 and C-08;
   - prompt self-verification on every turn, A-08;
   - nine tables pinning one macro setting, B-12;
   - a durable, leased inbox for a notice every result already carries, A-04.
5. **Process and hygiene:**
   - evidence files make up 72% of the repository's tracked bytes, in a public repository that records local paths, F-01;
   - STATUS, the handoff and PLAN-CHANGES are 95, 69 and 146 KB append-only logs, F-02;
   - CI runs the whole suite serially, twice, with no timeouts, F-03;
   - about 70 test files depend on wall time, F-04;
   - 861 private-attribute references in tests make refactoring expensive, F-05;
   - lab scripts are 23,500 lines of copy-forward code, F-11;
   - built code depends on decisions still marked proposed (D84, D85, D59, D43), F-10.
6. **Gaps on the M2 exit path:**
   - the GUI cannot start or chat with a run, and no backend supports it, E-06 and A-06;
   - no live transcript, E-07;
   - no ledger or board, E-10;
   - no PDF viewer or second origin, E-08;
   - no GUI approvals, E-09 and D-04;
   - the loop is not ready for a real model (no context limit, no token accounting, no retries), A-09;
   - the shared contracts the ledger and anchors need were never written, D-03;
   - the harness has no MCP client, D-08.

By the reviewers' estimates, about 45% of the orchestrator core and about 1,000 lines of workspace source plus 2,000 of its tests can go without losing a user-facing feature.

## Decisions needed before starting

Each phase below depends on some of these. The reviewers' questions are merged here, each with a recommendation; the source questions are in the notes.

1. **What the M2 exit includes** (E Q3, D Q2, D Q3, B Q2, A Q1, C Q1). *Answered by the owner on 2026-10-14:* as recommended, as a testing checkpoint. Real research needs MCP connectors and delegation, so they come immediately after the M2 exit run, as the first milestone for real use (Phase 4, items 7 and 9).
   - *In:* GUI chat with a live transcript, a real-model run, the ledger and board, user-grade review views, the PDF viewer, Sage.
   - *After the checkpoint:* delegation and MCP connectors.
   - *Deferred:* document files in environments and exports, approvals in the GUI (answer them with the CLI on babbage meanwhile).
2. **The unwired code** (A Q1, C Q1, D Q4, F Q2, B Q2). *Answered by the owner on 2026-10-14: as recommended.* The features are planned, but the two pieces differ:
   - **Delegation** (about 7,500 lines: A-03, C-05, D-10). The feature is confirmed (D81), but this code is the wrong shape to wire up later.
     - It is a second agent loop and dispatcher, outside the main loop's rules.
     - Kernels of its own for each delegated run are confirmed (PD-14, D81). The open questions decide how much machinery that needs: depth and concurrency (question 3), and whether a parent may share its kernel with a child (question 4, whose "proposed" label is stale). With one level, a few runs at a time and no sharing, extending the existing kernel path so a kernel belongs to the session and the run is likely enough. The separate 2,200-line run-kernel API, with its own enrollment, permissions, recovery and cancel paths between two trusted components, would then be unnecessary.
     - It reaches into the internals Phase 2 replaces.

     *Recommended:* archive it on a tag (`archive/delegation-2026-10`) and delete it from main, with its tests, tables and manager touch points. Rebuild delegation as a role of the one loop once the spec questions are answered, using the archived records design and tests as reference.
   - **Document files in environments and exports** (about 2,400 lines: B-11). The spec questions are settled (D65 amended), so this is genuinely planned work. It is built on the document store's private methods, which B-01 replaces, and its publication hook writes rows on every revision for a consumer that does not run.

     *Recommended:* either park it in a separate subpackage, with the hook off and a rule that no production module imports it, or archive it and port it to B-01's API when LD-49 to LD-52 are scheduled. The worker's document catalog (C-18) is self-contained and stays parked.
3. **Calibrate to the threat model** (E Q1, B Q5, A Q6). *Answered by the owner on 2026-10-14:* retire the defenses in point 4 above. Some defense in depth can be justified, but these items cost too much for what they protect.
   - Keep constraints at write time and the real boundaries: the executor, the daemon, the middleware, the GUI's login and origin checks.
   - Record per-section prompt pins in run records, without self-verification.
4. **Kernel notices** (A Q2). *Recommended:* replace the durable inbox with a stateless check before each turn, plus the disclosure every execution result already carries.
5. **GUI chat** (A Q3–Q5, E Q2).
   - A small orchestrator run service, pulled forward from W5, which the workspace proxies; not agent runs inside the workspace server.
   - One active run per session, with messages queued to its next safe point.
   - A new message carries earlier tasks and final answers, not whole transcripts.
   - A message may start a run within the project's model budget.
6. **Development data** (B Q1). *Recommended:* export the documents, then discard pre-M2 `control.sqlite3` files and re-baseline the schema, which drops the legacy migrations and reader branches.
7. **Math checks** (B Q3, B Q4). *Recommended:*
   - check only the current revision when the worker reaches it;
   - one Node process per batch of formulas;
   - record which macro revision a check used, not the whole macro context.
8. **Compatibility** (D Q1). *Recommended:* lockstep releases until M3, with a protocol-version check when the worker and orchestrator connect, and a written policy for when independent upgrades are needed.
9. **Worker operations** (C Q2–Q5). *Recommended:*
   - one worker per state directory, enforced by a lock file;
   - the stdio transport for development and tests only;
   - one unreachable environment fences itself, not the whole pool;
   - for M2, the whole-VM restart on daemon loss stays, and the user is told which kernels were lost.
10. **The planning fence and plan approval** (A Q7, A Q8). *Recommended:*
    - a policy revision does not block a running session's effect tools, since the worker enforces the current policy;
    - remove the unused `awaiting_approval` state until plan approval's default is decided.
11. **Repository hygiene** (F Q1, F Q4, F Q5, E Q6).
    - *Answered by the owner on 2026-10-14:* raw evidence moves out of git, and history is rewritten and force-pushed.
      - **Where it goes:** raw evidence goes to a private archive on babbage, outside the repository; git keeps hashes and short summaries. GitHub release assets would republish it on this public repository. A private GitHub repository is an option for an off-machine copy.
      - **Procedure** (Phase 0, only with the editing token):
        1. Wait until no other agent is working and no PR is open.
        2. Make a full mirror backup on babbage.
        3. Remove the raw evidence files from all history with `git filter-repo`, and replace home-directory paths with `~` in the remaining text.
        4. Confirm the timing with the owner right before the force-push.
        5. Force-push every branch and tag.
        6. Re-clone or reset every clone and worktree, the other agent's included.
        7. Record the one-time exception to the convention against rewriting published history, in CONVENTIONS and PLAN-CHANGES.
      - **Consequences, accepted:**
        - every commit SHA changes, so SHAs cited in docs and reports point nowhere;
        - rewritten commits lose their signatures;
        - open PRs must be recreated;
        - GitHub keeps old objects reachable through pull-request refs until GitHub Support purges them.
      - The username itself stays public through the repository's name and the commit authors.
    - STATUS and the handoff replace content instead of appending to it, with size caps.
    - PRs run Python 3.13 fast and slow tiers; 3.12 and the serial full run move to main or nightly.
    - Add as development dependencies: pytest-xdist, pytest-timeout, pytest-repeat, prettier and prettier-plugin-svelte.
12. **Pending confirmations** (F Q3).
    - Decisions: D84 (provisioning), D85 (the SQLite control store), D59, D43, and PSF-2.0 and MIT-0 in tier 1.
    - Approve the tools and PDF-sources specs.
    - Answer plans-and-delegation open questions 1–3 when delegation comes back.
    - Also (E Q4, E Q7, D Q5, E Q5): approvals re-authenticate by re-entering the login token; remove the display-repair provenance descriptor; put Anchor and Mutation in `contracts`, since the worker's PDF operations produce anchors; stop work on the passive inspectors until the M2 path exists.

## The plan

Five phases. Within a phase, work is split into lanes with disjoint files, so several agents can run in parallel without conflicts. Every refactor PR keeps behavior, is pinned by tests written first, and stays small: see the working rules below. Efforts are agent-days and rough.

### Phase 0: stop the bleeding (about 3–4 agent-days; one or two days of wall time)

- **Fix the bugs:**
  - A-01: compare only the run's own session record;
  - C-01: recover everything belonging to the environment, with backoff;
  - A-10;
  - C-10: close event streams on shutdown;
  - E-02's 503 and address leak;
  - C-11: wait on an event;
  - C-15: log background-task failures.

  Each comes with a regression test first.
- **Remove the unwired code,** per decision 2: A-03, C-05, D-10, B-11, C-18, F-07, and the dead `orchestrator/reviews.py`. Its tables go with it; until the re-baseline (Phase 2), its migrations stay.
- **Hygiene:**
  - F-01: an evidence policy plus a CI size guard, which stops the growth now; then the archive moves out of git and history is rewritten by the procedure in decision 11 (owner approved, 2026-10-14);
  - F-02: rewrite STATUS and the handoff to the short templates;
  - F-10: record the decisions confirmed under 12;
  - F-12: drop dead suppressions; make `pytest-asyncio` a direct dependency;
  - E-16: add the web formatter, applied in one mechanical PR.
- **CI guards** (part of F-03): job timeouts, `pytest-timeout`, and cancelling superseded runs.

*Done when:* the bug tests pass; the unwired modules are gone and the suite still passes; STATUS is under 10 KB; the size guard is in CI.

### Phase 1: test seams before refactors (about 8–10 agent-days; about one week of wall time)

The refactors in Phase 2 rewrite central objects whose tests reach into their privates. These seams come first, so that each refactor is checked by tests that do not change with it.

- **F-04 steps 1–3:**
  - a `Clock` protocol in `contracts`, with `SystemClock` and a `FakeClock`;
  - timeouts as constructor parameters instead of module constants;
  - waiting on events instead of sleeps.

  Do the worker and orchestrator first.
- **F-06:** a development-only test-support package, with one `FakeClock`, one worker test client and one `eventually()` helper. **C-13:** one worker harness instead of four.
- **F-05:** characterization tests through the public APIs of the agent loop (the message sequence of a run, to pin A-02's migration), the worker manager (for C-03) and the workspace routes (for E-01).
- **C-14:** run the conformance suite over the daemon transport too. **C-12:** remove the fixed grace periods (about 30 s off the suite).
- **F-03:** a fast tier (libraries and services, parallel; target under two minutes) and a full tier, with markers for slow and performance tests.

*Done when:* no test in the converted files sleeps for synchronization; 50 repeated runs show no flake; the fast tier runs on every PR.

### Phase 2: the core seams (about 25–30 agent-days in five lanes; about two weeks of wall time)

**Lane O, the orchestrator loop and harness.** In order:
1. **A-02:** the `TurnHook` interface. The loop alone owns delivery, response rounds and closing; the six subsystems move to hooks in today's order (the notes give a nine-step migration). With it: **A-11**, table-driven tool dispatch with effect classes, and **A-12**, one lease and thread helper.
2. **A-04:** the stateless kernel check, which removes two SQLite databases and a write path.
3. **A-05:** a `Harness` object that both the CLI and the run service build; `cli.py` becomes thin.
4. **A-08:** simplify the prompt records (decision 3) and version the notice framings in one module.
5. **A-13, A-14:** dead code, docstrings, and the remaining private access.

**Lane S, the control store.** In order:
1. **B-01:** a `ControlStore` with public `read()`/`write()` transactions, about 150 lines. Repositories keep their SQL. This removes the 26 file-level private-usage pragmas.
2. **B-02:** one ordered migration list with a single version; re-baselined per decision 6. Readers refuse a mismatched version, so the runtime existence checks go.
3. **A-07:** projects and sessions move from JSON into the store, which also removes A-01's cause. Coordinate with lane O.
4. **B-14, B-17 / E-12:** connection details, and the workspace through public repositories instead of SQL.
5. **B-10:** remove read-time re-validation, with `CHECK` constraints at write time.
6. **B-05:** a math-check package, with save hooks turned into subscriptions. **B-03:** batched math checks of the current revision only.
7. **B-12, B-15, B-06 to B-09:** the long functions split as the notes describe. Leave `table_rule.table` alone.

**Lane W, the worker.** In order:
1. **C-04:** one per-environment state object instead of seven flags.
2. **C-03:** split the manager behind its facade, lowest coupling first: connections, publication, the kernel registry, package admission, daemon recovery, then the journal and dispatcher. Adopt now the rule that no new subsystem goes into `manager.py`.
3. **C-06, C-07, C-08:** construction-time wiring; a versioned store schema; no read-time re-validation.
4. **C-09:** a lock on the state directory, which simplifies capture leases, plus a command that clears a wedged hold.
5. **C-02:** the daemon pushes events, with replay kept for reconnects; this removes 100 ms polling per running execution.
6. **C-16:** split `executor.py` into sibling modules shipped the same way, with a test that the sandbox side uses only the standard library. **C-17:** the worker docs as reference, with history moved to the lab reports.

**Lane C, the contracts.** Small, and best done first in the phase:
- **D-01:** one canonical digest helper for operation identity, an explicit "policy default" value, and the protocol check at connect;
- **D-02:** shared base types and one paging check;
- **D-07:** shared vocabulary in one place;
- **D-11:** the docs.

**Lane G, the workspace.** After lane S's step 1 for E-12:
- **E-01, E-02:** plain handlers per decision 3, one error mapping, and flat cleanup. This removes about 1,000 source and 2,000 test lines.
- **E-03, E-04, E-13:** one fetch and polling client, one API contract, consistent payloads and status codes.
- **E-05:** one save pattern (draft, operation ID, revision check, conflict) that chat, the ledger and approvals reuse.
- **E-14:** split `mathrepair.ts`, and stop shipping the API client in the server-side checker.
- **E-15:** interaction tests for the core views.
- **E-17, E-19, E-20:** small items and the doc.

*Done when:*
- `loop.py` handles features through hooks, and `_run_task` is under 80 lines;
- no file disables private-usage checks;
- every store has a single schema version;
- `manager.py` is a facade under 1,000 lines;
- the workspace has one handler pattern;
- the full suite still passes, and each lane's characterization tests are unchanged.

### Phase 3: shared building blocks for the M2 features (about 8–10 agent-days)

- **B-04:** the persistence pieces that now exist in two to nine copies, extracted once:
  - operation receipts for safe retries;
  - event tables;
  - notices leased to agents (these align with A-02's hooks);
  - versioned Markdown text with math checks;
  - a feed cursor.

  The ledger becomes their first new user; the notes map each ledger requirement to a piece.
- **D-03:** the Anchor, Mutation and typed PermissionRequest contracts.
- **D-04:** split `approvals.py` into the protocol, file-set grants and display; one approval resolver shared by the middleware and the MCP proxy. **D-06, D-09:** the package-cache cross-checks, and approval-path races.
- **E-10:** the generic GUI pieces the ledger and board will use: list, detail, history and the shared save pattern.

### Phase 4: the M2 exit path, thinnest first (feature work; about 15–20 agent-days)

1. **Chat and a live transcript:**
   - A-06 and E-06: the run service, one run per session, messages queued to safe points;
   - E-07: run events written as they happen and streamed to the GUI.
2. **A real model:**
   - A-09: a context limit, token accounting and retries for gateway errors;
   - then a first run through the owner's LiteLLM gateway. The first live call worked on 2026-10-14 ([PLAN-CHANGES](../PLAN-CHANGES.md)); the alias `sandbox-orchestrator` serves every role until the owner sets one per role.
3. **The ledger and board** on the Phase 3 pieces, with the views: `docs/specs/ledger.md` and its decided questions.
4. **E-11:** user-grade review and finding views for the "user feedback → automatic review" step.
5. **E-08:** the PDF viewer, on a second origin.
6. **Approvals:** for M2 through the CLI on babbage (decision 1), with E-09 later.
7. **Delegation, as in Claude Science** (the owner's intent, D81, PD-14). Children are frames in the same runtime: each delegated run is a role of the one tool loop, with its own prompt, tool table, hooks and budget (A-02). Its kernels come from the existing worker kernel path, keyed by session and run. The parent gets a delegate tool and a handback record, and plan steps name the delegate (as Claude Science's delegation tracks do). This comes after the loop's hooks; it is part of M2 only if the owner says so. The owner answered open question 3 on 2026-10-14 (recorded in the [spec](../../docs/specs/plans-and-delegation.md), PD-15):
   - **Concurrency:** support at least 8–10 subagents at once in a session; 4–5 is the typical number.
   - **Nesting:** one additional level. The main agent's subagents may delegate once more, and that second level may not. Claude Science does not nest. Claude Code's current documentation allows three layers below the main conversation by default, adjustable, and at the limit it withholds the delegation tool so the deepest subagent works alone ([subagents](https://code.claude.com/docs/en/sub-agents), checked 2026-10-14). Its default concurrency is 20 subagents per session.
   - **Proposed rules for nesting:**
     - depth enforced by withholding the delegate tool at the limit, as Claude Code does;
     - default depth 2 (one level below the subagents), as a per-project setting;
     - delegation can be switched off per subagent role;
     - one concurrency cap for the whole session's tree, not per parent;
     - budgets carved from the parent's;
     - cancellation cascading down the tree;
     - handbacks to the direct parent;
     - the GUI shows runs as a tree.
   - **Build order:** the run tree is built with a parent link from the start; one level ships first, and deeper levels are enabled behind the depth limit.
   - **The worker:** it stays unaware of the tree, since kernels belong to a run at any depth.
   - **Resources:** 8–10 runs with their own kernels do not fit the labs' 4 vCPU, 4 GiB sandboxes. Kernels start lazily, on a run's first cell, with a per-session kernel memory budget and larger VMs for delegating sessions. A babbage lab measures the cost per kernel first. The background-job limits (4 per session, 8 per environment) and the subagent cap are set together.
   - **Kernel sharing (open question 4, answered by the owner on 2026-10-14):** no sharing. A parent never lends its kernel to a subagent. In-memory state is handed off through saved files in the session workspace, such as Sage's `save()`/`load()` or pickle, so the handoff is explicit and recorded. Very large state stays with the parent, which does that part itself instead of delegating it. This keeps per-run kernel support small: the worker keys kernels by session and run, with no handoff or lending protocol.
   - **Still open:** questions 1 and 2, plan approval and progress across versions.

   The archived code is reference for the records design and the tests.
8. **The M2 exit lab on babbage**, a testing checkpoint (decision 1): two workbooks share the project container with distinct kernels, a third uses its own, and the path paper → Sage → document → user feedback → automatic review → board update runs with a real model.
9. **MCP connectors, for real research** (D-08; decision 1): an MCP client in the harness through the project's proxy (built, #231), the proxy's policy compiled by the platform instead of a lab script (`compile-policy`, #232), and the owner's Cloudflare Access service token for crypto-research-mcp, which is enough on its own: the connector auth service and OAuth are off the path (#227, [PLAN-CHANGES](../PLAN-CHANGES.md)). What is left is the owner registering the provider and a first call through the proxy. With delegation (item 7), this is the first milestone for real use.

### Phase 5: later, and opportunistic

- **F-09:** restructure the 72 doc pages into a reference set, with history in the lab reports and PR descriptions. **F-08 / B-16:** remaining drift.
- **F-11:** consolidate the lab scripts into a small type-checked library; the product test that loads a lab script stops doing so.
- **E-21:** KaTeX in a Web Worker, which lets the main-thread mitigations shrink. **D-05:** `compile_sandbox` when it is next touched. **D-12:** worker–middleware file coupling, before M3.
- **E-18:** a narrower worker token for the workspace. **B-13:** run the whole-document parser comparison in CI. **F-13:** license-check gaps.
- **Domain packages out of the orchestrator** (the owner agreed on 2026-10-14; after the M2 exit lab). `orchestrator/control` and the domain packages (documents, mathchecks, consistency, ledger) move into packages of their own. The orchestrator keeps thin agent tool adapters, possibly gathered in `orchestrator/tools/`. The workspace depends on the domain packages directly instead of on the orchestrator, which ends the documented workspace-to-orchestrator exception. The layering test (`test_package_layers.py`) already pins the direction of the dependencies between them.

## Working rules from now on

These address how the code got here, as much as the code itself.

1. **No production module without a production caller.** Prerequisites wait in a branch until the feature that wires them is ready. This is the "unwired prerequisites" pattern of #114 and its successors.
2. **Small PRs:** at most about 800 changed lines of product code, excluding generated files and tests. Bigger changes are split into steps that each keep behavior.
3. **Calibrate to the threat model.** A defense against something the threat model does not cover needs a decision record first. Reviews flag over-hardening as findings.
4. **Characterization tests before a refactor,** through public APIs; no new tests on private attributes.
5. **Evidence stays out of git.** Lab reports stay in git, short and with hashes; raw archives live outside it.
6. **STATUS and the handoff replace content instead of appending it, with size caps.** History goes in PR descriptions and lab reports.
7. **No new subsystem in `loop.py`, `manager.py` or the workspace's `create_app`.** Features plug in through the seams above.
8. **Specs before code:** nothing is built on a spec whose open questions affect it.

## Measures

| Measure | Baseline | Target |
| --- | --- | --- |
| Production code reached by nothing | about 9,800 lines | 0 |
| Files disabling private-usage checks | 26 | 0 |
| `loop.py` `_run_task` | 175 lines, 29 subsystem checks | under 80 lines, no subsystem checks |
| `manager.py` | 3,549 lines | a facade under 1,000 lines |
| Control-store schema versions | 7 markers, version 1, runtime existence checks | one ordered migration list |
| Test suite | 6.5 min locally; about 10 min in CI, serial, twice | fast tier under 2 min on PRs; full tier on main |
| Timing-dependent test files | about 70 | 0 in fast tier; flake rate measured nightly |
| Tracked repository bytes from evidence | 72% | under 10%, guarded in CI |
| STATUS.md | 95 KB | under 10 KB |
| M2 exit run | not possible | passes on babbage with a real model |

## Keep

Do not disturb these while refactoring; the notes give the details.

- **Orchestrator:**
  - no local execution path;
  - idempotent operation IDs;
  - bounded tool results;
  - the small, provider-neutral `model.py` with `ScriptedModel`;
  - delivery counted only after a successful model call, acknowledgement only after a durable save;
  - untrusted-data framing on every notice;
  - tests against an in-process worker.
- **Documents and review:**
  - the document store's change protocol (prepare outside the lock, check and commit inside it);
  - the parser, identity and patch engine, and `table_rule.py` close to upstream;
  - the reviewer's boundaries and budgets;
  - `mathcheck.py`'s process hygiene.
- **Worker:**
  - executions stored before dispatch and events stored before delivery;
  - `_finish`'s fallbacks and the watchdog;
  - untrusted-executor handling and its hostile tests;
  - the daemon's security design;
  - `provisioner.py`'s use of only the public API, the model for the split.
- **Infrastructure:**
  - the layering;
  - the policy library's design;
  - the event store;
  - the package cache's adapter contract;
  - fail-closed recording.

  Freeze these packages for M2 apart from D-03, D-04, D-06 and D-09.
- **Workspace:**
  - the local security boundary;
  - the read-only/writer split;
  - the strict document-change endpoint;
  - the renderer;
  - the math-repair service.
- **Process:**
  - CI's security basics;
  - the injected clocks the library packages already use;
  - the lab reports and the hash inventory;
  - `.agents/upstream/`.

## Before starting

- Get the owner's answers to the decisions above; several phases change with them.
- Re-check each finding against the main of that day: the other agent keeps merging (provisioning recovery and upgrades since the baseline), and some findings may have moved.
- Then start Phase 0 with one agent per lane, in separate worktrees, with these working rules in every brief.
