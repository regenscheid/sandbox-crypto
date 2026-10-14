# Review notes: F, cross-cutting (tests, CI, repository hygiene, documentation)

Baseline: main at fa456bc (2026-10-14). Reviewed: as listed below.

**Read in depth:**
- `.github/workflows/ci.yml`, root `pyproject.toml`, `.gitignore`, `.gitattributes`, `scripts/check_licenses.py`, `.agents/CONVENTIONS.md`.
- The tracking files: `.agents/STATUS.md`, `handoff.md`, `PLAN-CHANGES.md` (structure and opening), `BACKLOG.md` (structure and W4).
- `docs/DECISIONS.md` (status column and key rows), and the status lines and open questions of `docs/specs/*.md`.
- Every test directory, by measurement scripts: timing constructs, duplicate helpers, private access, cross-package imports, and import reachability from the console-script entry points. I also read the timing-heavy worker tests in places.
- `.agents/docs` and `.agents/labs`, by inventory, hashing, JSON breakdown and duplication ratios.

**Ran:**
- `--durations` on 11 worker and orchestrator files: 549 tests in 201 s.
- `--durations` on six library and service packages: 7,335 tests in 30 s.
- pyright on `packages` with `reportUnnecessaryTypeIgnoreComment` turned on, from a scratch config.

**Delegated:** two read-only spot checks of docs against code: `WORKER.md` and `WORKSPACE.md`; and `LIVING-DOCUMENTS.md`, `CONSISTENCY-REVIEW.md` and `ORCHESTRATOR-PROMPTS.md`. I verified a sample of their findings myself.

**Skimmed:** `README.md`, `ROADMAP.md` (M2), `IMPLEMENTATION-PLAN.md` (structure), lab report Markdown, `web/workspace` test counts.

**Skipped:** per-package code quality (areas A to E), the web app's own tests in depth, `images/`.

## Summary

The code is held to good mechanical gates. CI pins its actions, uses a locked `uv sync`, and checks npm licenses and sources before installing. ruff and pyright are clean.

The libraries and small services are tested the way we want everything tested: injected clocks, black-box tests, 7,335 tests in 30 s. The worker, orchestrator and workspace tests are the opposite. They are slow, timing-dependent and white-box: 861 private-attribute references and 422 `monkeypatch.setattr`. That will make the A and C refactors expensive unless seams come first.

Around the code, the repository has become an append-only log:
- 32.8 MB of evidence in git, about 70% of the object store, growing 10–22 MB per lab day.
- Tracking files of 95, 69 and 146 KB, each repeating the same garbled progress paragraph four to five times across files.
- 72 docs pages, 46 of them written in two days, one per slice.

Docs drift is mild on numbers, which nearly all match the code. It is systematic on "not built yet" statements and inventories, because docs are appended to rather than revised.

Decisions D84, D85 and D59, and the draft plans-and-delegation spec, carry real code while still marked proposed or draft.

The changes that matter most:
1. **Stop the evidence growth (F-01)** and shrink STATUS, handoff and PLAN-CHANGES to short, replace-not-append forms (F-02).
2. **Settle the decisions and specs that code already depends on** before building more on them (F-10).
3. **Add a clock and timeout seam, then remove wall-clock waits (F-04).** Pair this with public seams for the white-box tests (F-05) before refactoring `manager.py` and `loop.py`.
4. **Split CI into a fast PR tier and a full tier (F-03)**, with shared test helpers (F-06) and the unwired-code tests marked or removed (F-07).
5. **Consolidate the per-slice docs into per-component docs** and fix the stale "not built" statements (F-08, F-09).

## Findings

### F-01: The lab evidence archive in git is 70% of the repository and grows by megabytes per lab
- **Where:**
  - `.agents/docs/agent-reports/` (188 files, 32.8 MB).
  - The largest files: `babbage-openshell-lab-32-evidence-2026-10-13.json` (4.07 MB), `…lab-33…` (3.90 MB), `…lab-30…` and `…lab-31…` (1.9 MB each), `…lab-29…` (1.5 MB), `delegated-plan-progress-evidence-2026-10-13.json` (1.06 MB).
  - `.agents/docs/INVENTORY.json`.
- **Category:** other (repository hygiene)
- **Priority:** P1
- **Evidence:**
  - **Share of the repository.** `.agents/docs` is 32.8 of the 45.3 MB of tracked bytes (72%). Its historical blobs take 10.8 of the 15.4 MB in the git object store (70%; 420 blobs, 37 MB uncompressed).
  - **Growth.** It went from 0.23 MB on 10-11 to 10.2 MB on 10-12 and 32.3 MB on 10-13.
  - **Composition.**
    - 51 JSON files make up 23.6 MB. Of that, 18.7 MB (79%) is embedded copies of whole files: draft and failed helper scripts, `/tmp` logs, inputs, and source files that are already in git.
    - For example, lab 32's `files` array holds 469 files: a 424 KB `/tmp/lab32-qualified-runtime-independent-inputs.json`, three about-59 KB copies of `provisioning_revision_packages.py`, and so on.
    - 66 JPGs and 3 PNGs make up 8.6 MB, including near-duplicates (`lab-31-page-1` and `lab-31-generation2-page-1` differ by 1 byte in size). One pair is byte-identical.
  - **Local paths.** The evidence `origin` fields record local absolute paths, such as `~/.codex/worktrees/project-macros/…`, plus `/tmp` names.
  - **Little is linked from docs.** Only `docs/IMPLEMENTATION-PLAN.md` links any evidence JSON or images (5 links). The 67 Markdown lab reports total 0.5 MB.
  - **Hashes already exist.** `INVENTORY.json` already records a sha256 for every file.
- **Proposal:**
  1. **Retention policy, written in CONVENTIONS:**
     - Git keeps each lab's Markdown report, an entry in `INVENTORY.json` (path, bytes, sha256), and a summary JSON of at most about 50 KB: gates, IDs, verdicts, and hashes of every retained file.
     - Raw archives go out of git: GitHub release assets per lab (`evidence-lab36`), Git LFS, or a private evidence repository or babbage directory. See question 1. Git keeps only their sha256 and location.
  2. **Never embed files in evidence.** Reference a file already in git by commit, path and blob sha. Reference drafts and failed scripts by sha256 only, unless the report needs them to explain a failure.
  3. **Screenshots:** at most one or two per acceptance, compressed, or outside git.
  4. **A CI guard (small script):**
     - Fail when a file added under `.agents/` exceeds 256 KB.
     - Fail when a PR adds more than 1 MB there.
     - Fail on absolute home paths in evidence.
  5. **Do not rewrite history** unless the owner decides to, since CONVENTIONS says "Never rewrite published history". Stopping the growth is the important part; the pack stays at about 16 MB.
- **Effort:** S (policy and guard), M (re-homing existing archives and slimming them to manifests).
- **Risk and verification:**
  - Reports link evidence JSON by relative path (for example `STATUS.md:3`, `IMPLEMENTATION-PLAN.md`). Moving archives breaks those links unless they are rewritten to the new location plus a hash.
  - One test depends on a lab script, not on evidence (see F-11).
  - Verify with a link check over `.agents` and `docs`, and an `INVENTORY.json` hash re-check of whatever stays.
- **Depends on / enables:** question 1. It enables a reviewable repository and faster clones and CI checkouts.

### F-02: STATUS, handoff and PLAN-CHANGES are append-only logs that copy one another
- **Where:**
  - `.agents/STATUS.md` (94.5 KB, 262 lines, lines up to 2.9 KB long).
  - `.agents/handoff.md` (68.5 KB).
  - `.agents/PLAN-CHANGES.md` (146 KB, 64 dated entries).
  - `.agents/BACKLOG.md` (73.8 KB).
  - `.agents/docs/README.md` (35 KB), the preamble of `docs/IMPLEMENTATION-PLAN.md` (lines 3–25), and the status paragraph of `README.md`.
- **Category:** other (repository hygiene)
- **Priority:** P1
- **Evidence:**
  - **Growth.** STATUS was 1.3 KB on 10-10, 26.6 KB on 10-11, 59.9 KB on 10-12, 85.5 KB on 10-13 and 93–95 KB on 10-14. handoff grew from 12 to 69 KB, and PLAN-CHANGES from 2 to 146 KB.
  - **Copies.** The same Lab36 paragraph opens STATUS, handoff, PLAN-CHANGES and `IMPLEMENTATION-PLAN.md`. The Lab34 paragraph also appears in `.agents/docs/README.md`, so there are four or five copies of each progress paragraph. I confirmed this by grepping distinctive phrases.
  - **Garbled text.**
    - The opening paragraph has missing spaces: "pass11,464", "at2026-10-14T04:49:51", "EXIT0/57.266s,23-record", "SymPy1.13.3", "explicit1.14.0", "all17".
    - The pattern started around Lab34 (10-13). It has spread to about 140 places in 15 files, including `docs/IMPLEMENTATION-PLAN.md` (20), `docs/PROVISIONING-UPGRADES.md` (5) and the Lab35 and Lab36 reports.
  - **Stale content.**
    - STATUS "Next" (`STATUS.md:219-226`) still lists "M2 (W4): the research workspace (living documents, ledger and board, comments, automatic consistency review)" as next, although documents, comments and review are built.
    - handoff "Queued work" (`handoff.md:184`) still lists Labs 18–20 and W4 slices that are complete as items 1–4.
    - "Where things stand" is about 30 stacked "historical checkpoint" bullets, each up to about 3 KB.
    - `handoff.md:133` still has a "Merged on 2026-10-11" table.
  - **Wrong file for progress.** PLAN-CHANGES is defined as the log of changes to the plan, decisions and conventions (`CONVENTIONS.md:11`). Most of its 64 entries are lab and progress narrative.
  - **Conflict magnet.** 81 of the 103 merges since 10-11 touched STATUS. Since 10-11 there were 164 commits to STATUS, 170 to BACKLOG, 146 to PLAN-CHANGES and 121 to handoff, so every parallel branch conflicts there.
  - **Attribution conflict.**
    - `CONVENTIONS.md:117` says "End with the attribution trailers in use for the session".
    - `handoff.md:160` says "No attribution lines in commits or pull request descriptions".
  - **Backlog gaps.** BACKLOG has no W3 section. The M2 exit path (chat in the GUI, skills, the PDF viewer, ledger and board, the real-model run) is scattered across single lines at `BACKLOG.md:126,136` and others, not tracked as a prioritized path.
- **Proposal:** replace rather than append, with size caps checked in CI (for example STATUS and handoff at most 8 KB each).
  - **STATUS.md:** `Updated <date>, main <sha>`, then:
    - an M2-exit checklist table (step, state, link);
    - "In flight", at most five one-line items (branch, owner, next step);
    - "Blocked on the user", at most five;
    - "Decisions awaiting the user", as links to DECISIONS rows;
    - "Last five merges", one line each.
    - History lives in PR descriptions, lab reports and `git log`.
  - **handoff.md:**
    - Move the stable parts (standing instructions, how work is done, gotchas; lines 149–251, about 6 KB) into a `WORKFLOW.md` or CONVENTIONS.
    - Keep only "Where the last session stopped" (at most 15 lines, overwritten each session) and "Next three actions".
  - **PLAN-CHANGES.md:**
    - Archive the current file as `PLAN-CHANGES-2026-10.md`.
    - From now on, add entries only for changes to the plan, decisions or conventions, one or two sentences each with a PR link.
    - Lab results go to lab reports and the PR.
  - **Other files.** Remove the progress preambles from `IMPLEMENTATION-PLAN.md`, `.agents/docs/README.md` and the specs' status lines (see F-09). Keep `README.md`'s status paragraph to one sentence.
  - **Garbled tokens.** Add a CI regex check for glued word-number tokens, such as `\b(pass|passed|at|all|explicit)\d`, in Markdown.
  - **Attribution.** Make CONVENTIONS state one rule.
- **Effort:** S
- **Risk and verification:**
  - Some information is lost unless the history is kept: archive the files instead of deleting them.
  - Agents' habits are driven by CONVENTIONS' "Keeping documentation and tracking current" rule (`CONVENTIONS.md:121-131`), which asks every PR to refresh STATUS. Rewrite that rule too, or the logs regrow.
  - Verify with a CI size check and the garble regex.
- **Depends on / enables:** It removes the main source of merge conflicts between parallel agents, and makes the M2 exit path visible (supports the owner's concern).

### F-03: There is no fast tier; CI runs the whole suite serially twice, with no guards against hangs
- **Where:**
  - `.github/workflows/ci.yml:13-34` (matrix 3.12 and 3.13, `uv run pytest`) and `:36-63` (web).
  - `pyproject.toml:76-83`.
- **Category:** performance, tests
- **Priority:** P2
- **Evidence:**
  - **Suite size.** 11,507 tests collected at fa456bc (11,464 passed and 43 skipped per the latest STATUS gate).
  - **Where the time goes.**
    - The six library and service packages (policy, policy-middleware, package-cache, contracts, event-store, mcp-proxy) ran 7,335 tests in **29.9 s**: 64% of the tests in about 8% of the time.
    - Eleven worker and orchestrator files (`test_daemon`, `test_capture`, `test_capture_ownership`, `test_executor`, `test_service`, `test_conformance`, `test_provisioning`, `test_environments`, `test_run_dispatch`, `test_mathcheck`, `test_harness`) ran 549 tests in **201 s**: 5% of the tests, about half of the 6.5-minute local run.
  - **The cost is real processes, not literal sleeps.** Kernels and executors, fake package managers run as subprocesses, and a `python -m venv` for every fixture (`test_provisioning.py:111` `make_prefix`, used by the `sandbox` fixture at `:176`).
    - Slowest tests: `test_m6_many_installs_in_one_call_are_one_revision_with_its_lockfile` (7.98 s), `test_h2_heartbeats_keep_a_quiet_stream_open_and_silence_ends_it` (4.17 s), and a dozen worker tests at 2–3.7 s.
  - **Cheap waste.** `packages/policy-middleware/tests/test_archives.py:60` starts `serve_forever` with the default 0.5 s poll interval. Each of the 17 tests pays a 0.50 s teardown, about 8.5 s of the 30 s.
  - **CI gaps.**
    - No `concurrency` group, so superseded PR pushes keep running.
    - No `timeout-minutes`, so a hung async test runs until GitHub's 6-hour default.
    - No pytest-timeout, no `--durations`, no xdist, no npm cache.
    - The web job runs `uv sync` again only for one test file (`ci.yml:58-63`).
- **Proposal:**
  1. **PR tier (required checks; target about 4 minutes of wall time):**
     - `static` (3.13): locked sync, protos `--check`, ruff, pyright (about 16 s locally on `packages`), and the Python license check.
     - `test-fast` (3.13): `pytest -m "not slow and not unwired" -n auto --timeout=60 --durations=25`.
     - `test-slow` (3.13): `pytest -m slow -n 2 --timeout=180`.
     - `web`: as now, with `cache: npm`.
     - Add `concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}` and `timeout-minutes: 20`.
  2. **Full tier (push to main, merge queue, or nightly):**
     - 3.12 and 3.13, serial, every marker, with `--durations=50` saved as an artifact.
     - Nightly, run the timing-sensitive list from F-04 20 times with pytest-repeat to measure flake rates.
  3. **Markers:**
     - `slow`: module-level `pytestmark` on the about 15 process- and kernel-heavy files.
     - `unwired`: see F-07.
     - Make `integration` actually enforced by a `conftest` hook, since today each file has its own `skipif` (`test_clients_integration.py:29`, `test_package_clients_integration.py:42`).
  4. **Cheap fixes:**
     - `serve_forever(poll_interval=0.01)` in `test_archives.py`.
     - Build the fake Conda prefix once per session (`tmp_path_factory`) and copy it for each test, instead of running `python -m venv` for each one.
- **Effort:** M
- **Risk and verification:**
  - xdist raises contention. Do F-04 steps 3 and 4 first: tight wall-clock asserts, the CPU-time test `test_service.py:418`, and the `free_port()` TOCTOU at `test_harness.py:75`.
  - The provisioning tests already namespace their `/proc` scans per fixture (`test_provisioning.py:189-191`), which is good.
  - Adding pytest-xdist, pytest-timeout and pytest-repeat (all MIT) needs LICENSING table rows (CONVENTIONS).
  - Verify by running both tiers on a branch and comparing their counts against the serial full run.
- **Depends on / enables:** depends on F-04, F-06 and F-07 (markers). It enables faster review loops.

### F-04: About 70 test files rely on wall time; a concrete plan to make them deterministic
- **Where:**
  - 69 test files. 85% of the timing constructs are in `packages/worker/tests` (167 sleep matches, 82 timeout-based waits), `orchestrator/tests` (59 and 28) and `workspace/tests` (40 and 11).
  - Known flake: `packages/worker/tests/test_capture_ownership.py:143` `test_cancel_ack_deadline_includes_actual_channel_write_lock`. It sets `capture_timeout = 0.05` and races a real channel write lock.
- **Category:** tests
- **Priority:** P2. The clock and timeout seam (steps 1–2) is P1 *if* the A and C refactors of `manager.py` and `executor.py` start first, because it defines the seams those refactors need.
- **Evidence:** measured with AST and regex. The brief's 253 and 42 were by a different method.
  - **Literal sleeps.** 264 literal `sleep()` calls in test code:
    - 128 are `sleep(0)` yield-counting, for example `test_run_dispatch.py:271-274,734-736,842-844`: two yields, then `assert not task.done()`. These depend on the exact number of awaits in the implementation.
    - 92 are under 0.1 s.
    - 39 are 0.1–2 s, 14.3 s in total. Many are "negative waits": sleep, then assert that something has not happened yet (`test_provisioning.py:714` (0.5 s), `:804` (1 s); `test_service.py:392,433` (1.5 s)).
    - 5 are 60–3600 s stand-ins for code that is meant to be cancelled, which is fine.
  - **Other waits.** 124 `wait_for`/`timeout=`/`join(timeout=)` waits, and 11 hand-written `while monotonic() < deadline` poll loops (`test_provisioning.py:101,158,202,567-599,773,1230,1417,1541`).
  - **Shrunk module constants.** 29 `monkeypatch.setattr` calls shrink module timeout constants to 10–50 ms, for example:
    - `test_kernel_delivery.py:635,663`: `PROOF_TIMEOUT` 0.02, `RENEW_SECONDS` 0.01;
    - `test_environments.py:1108-1109`;
    - `workspace/tests/test_plans.py:565`;
    - `test_lockfile_diffs.py:355`.
  - **Wall-clock performance asserts.** 17 of them, some very tight:
    - `workspace/tests/test_mathrepair.py:461,477` (< 0.05 s);
    - `event-store/tests/test_store.py:220` (`elapsed < timeout + 0.15`);
    - `policy/tests/test_review_regressions.py:505` (< 0.5 s);
    - `worker/tests/test_channel.py:41` and `test_provisioning.py:1509` (< 1 s).
  - **Real-time reapers.** `test_service.py:383-392` runs `--kernel-idle 0.5` and then sleeps 1.5 s, expecting the kernel to have been reaped. `test_service.py:418-433` asserts more than 0.5 s of CPU used in 1.5 s of wall time.
  - **The pattern to copy already exists.** The packages with injected clocks have no wall-time waits: `mcp_proxy/sessions.py:116`, `package_cache/upstream.py:156`, `package_cache/store.py:176`, `orchestrator/documents/store.py:393`, `event_store/store.py:112`. The worker's `manager.py`, `executor.py`, `environments.py`, `suspend.py` and `provisioner.py` call `time.monotonic()` directly (11, 24, 4, 3 and 4 call sites).
- **Proposal:** in this order.
  1. **Clock seam.**
     - Add a `Clock` protocol (`monotonic()`, `now()`, `async sleep()`) in `contracts`, with `SystemClock` and a test `FakeClock` whose `advance()` wakes sleepers.
     - Inject it into the worker manager, executor's idle reaper, environments, suspend and provisioner, and into the orchestrator's `jobstate`, `kernel_delivery` and `child_controller`.
     - Rewrite the reaper and idle tests (`test_service.py:383`, the `test_environments` suspender tests) to advance the clock.
  2. **Timeouts as constructor parameters, not module constants.** This removes the 29 module monkeypatches. The timeout-path tests then use a fake that never answers, so a timeout is the only possible outcome.
  3. **Wait on events, not on time.**
     - Replace `sleep(0)` counting and negative waits with explicit "reached the blocking point" probes. The tests already use this pattern: `ch.entered.wait()` (`test_run_dispatch.py:269`), and `until_waiting(lock)` over `/proc/locks` (`test_provisioning.py:201`).
     - Where the production code has no observable state, add one: a queue snapshot showing "waiting for revision lock".
     - Fix the known flake first. Make the capture deadline a parameter, park the publish on the write lock behind an `entered` event, then assert the `held` lease.
  4. **Performance asserts.** Turn them into deterministic bounds: count operations or regex steps, or compare scaling ratios (min-of-k timings of n against 4n). Move the rest to a `perf` marker that runs only in the full tier.
  5. **One helper.** A single `eventually(predicate, timeout=30, describe=...)` replaces the 11 hand-written poll loops (see F-06).
  6. **Hangs.** Use pytest-timeout so hangs fail rather than stall. A nightly repeat run measures the flake rate (F-03).
  - **Optional:** evaluate the `looptime` pytest plugin, which runs asyncio on virtual time, for pure-asyncio tests. Check its license and maintenance first. It does not help with real subprocesses.
- **Effort:** L in total. Steps 1–2 are M; the flake fix is S; the rest is incremental, file by file.
- **Risk and verification:**
  - Injected clocks touch production signatures in the worker. Keep the defaults at `SystemClock`, so call sites do not change.
  - For each converted file, run it 50 times (`pytest-repeat`) and under `-n 4` before and after.
  - The behavior being tested (cancellation and deadlines) is security-relevant (fail-closed). Keep the negative cases.
- **Depends on / enables:** It enables F-03's xdist tier and the A and C refactors (the seams). It overlaps F-05.

### F-05: Worker, orchestrator and workspace tests are white-box, which makes refactoring them expensive
- **Where:**
  - Private-attribute references in tests: `packages/worker/tests` (482), `orchestrator/tests` (283), `workspace/tests` (96).
  - `monkeypatch.setattr` calls: worker 201, orchestrator 156, workspace 65.
  - The heaviest `._db` users: `test_run_dispatch.py` (39), `test_provision_observations.py` (39), `test_run_kernels.py` (34).
- **Category:** tests, coupling
- **Priority:** P2. Do it before refactors A-0x and C-0x land.
- **Evidence:**
  - **Most-touched internals.** `_db` is referenced 231 times (raw SQL against store internals), `_environments` 74, `_write` 72, `_read` 66 and `_tasks` 38.
  - **The contrast.** policy, contracts, mcp-proxy, package-cache, policy-middleware and event-store have 19 private references and 6 monkeypatches combined.
  - **Cross-deployable imports.** 4 orchestrator test files import `sandbox_science.worker` (`test_artifact_review.py`, `test_artifact_publication.py`, `test_document_delivery*.py`). 6 worker test files import the orchestrator, including `test_child_controller_http.py`, `test_capture.py` and `test_environments.py`. These are integration tests living in unit-test directories of deployables that must not import each other (CONVENTIONS).
- **Proposal:**
  1. Before each refactor (the loop safe-point interface, manager split), write characterization tests through the public API or HTTP surface for the behavior being moved, then refactor.
  2. Give the stores a small test-support surface instead of `._db`: named corruption and tamper helpers, as event-store's `conftest.py` `tamper` fixture does, or `Store.debug_execute` behind a test-only subclass.
  3. Move the cross-deployable tests to a root `tests/integration/` directory (add it to `testpaths`), so package test directories respect the import rule.
  4. Track private references and monkeypatch counts per package in CI output, so the number goes down rather than up.
- **Effort:** M (incremental, file by file, alongside each refactor)
- **Risk and verification:**
  - Characterization tests can freeze accidental behavior. Review them against the specs.
  - Measure the counts before and after.
- **Depends on / enables:** It enables the A and C refactors. It depends on F-04 steps 1–2 (seams).

### F-06: Duplicated test helpers, and no shared test-support package
- **Where:**
  - 7 `class Clock` fakes in three shapes (float `now`; datetime `now` with `advance`; real time plus offset): `package-cache/tests/test_opam_archives.py:52`, `test_app.py:68`, `test_store.py:26`, `test_cli.py:30`, `orchestrator/tests/test_document_store.py:69`, `policy-middleware/tests/test_approvals.py:44`, `worker/tests/test_provisioner.py:377`.
  - 7 `class Worker` doubles. Three are near-identical worker API clients: `worker/tests/test_service.py:50`, `test_robustness.py:67`, `test_provisioning.py:537` (post, get, submit, finish, run).
  - Constants and fixtures: 13 `store` fixtures, 27 `CONTEXT` constants, 26 `TOKEN` constants.
  - 3 copies of `free_port` (`orchestrator/tests/test_harness.py:75`, plus two integration files).
  - Byte-identical 19-line `worker_url` fixtures in `test_artifact_publication.py` and `test_artifact_review.py`.
  - The orchestrator's 50 test files have no `conftest.py`.
- **Category:** duplication, tests
- **Priority:** P2
- **Evidence:**
  - From the AST scan: exactly identical bodies total only about 60 lines; the duplication is in near-copies.
  - `--import-mode=importlib` (`pyproject.toml:78`) means test modules cannot import shared helpers from `conftest.py`. `worker/tests/conftest.py:3` says so.
  - So each file grows its own `Clock`, `Worker` and `until`.
- **Proposal:**
  - Add a dev-only uv workspace member, `packages/testing` (`sandbox_science.testing`, not a deployable), holding:
    - `FakeClock` (float and datetime views) for F-04;
    - `eventually()`;
    - `context()` and `token()` factories with obviously synthetic values;
    - `WorkerApiClient` (the shared post, get, submit, follow-events, finish and run);
    - `serve_once()` and `http_server()` helpers that bind port 0 and return the bound port (no TOCTOU);
    - store tamper helpers (F-05).
  - Add an orchestrator `conftest.py` for its common fixtures (`store`, `worker_url`).
- **Effort:** M
- **Risk and verification:**
  - Libraries must not import deployables (CONVENTIONS). Keep `testing` depending only on `contracts`, and keep deployable-specific doubles in that package's `conftest`.
  - Verify that test counts are unchanged.
- **Depends on / enables:** It enables F-04 and F-03.

### F-07: 266 tests cover modules that nothing in production reaches
- **Where:** modules not reachable from any `[project.scripts]` entry point (my import-graph walk from the seven `*.cli` modules):
  - `orchestrator/child_attempts.py` (1,187 lines)
  - `child_controller.py` (768)
  - `child_tools.py` (579)
  - `child_prompt.py` (197)
  - `delegated_plan_tools.py` (747)
  - `delegation_records.py` (1,023)
  - `documents/delivery.py` (459)
  - `documents/worker_source.py` (552)
  - `orchestrator/reviews.py` (88; its docstring calls it a "historical trigger helper")
  - `contracts/document_edits.py` (44)

  In total, 10 modules and 5,644 lines.
- **Category:** dead code, tests
- **Priority:** P2
- **Evidence:** the tests are:
  - `test_child_attempts.py` (41 tests)
  - `test_child_controller.py` (43)
  - `test_delegated_plan_tools.py` (41)
  - `test_delegation_records.py` (56)
  - `test_worker_document_source.py` (33)
  - `test_document_delivery.py` (32)
  - `test_document_delivery_fences.py` (13)
  - `test_reviews.py` (3)
  - `worker/tests/test_child_controller_http.py` (4; it imports the orchestrator)

  That is 266 tests and about 5,150 lines. Together with the modules, about 10,800 lines must be kept green on every refactor of the stores and the worker client they touch.
- **Proposal:**
  - Delete `reviews.py` and `test_reviews.py` now. The live trigger is in `consistency/`.
  - For the delegation and document-source stack, it is the owner's call (question 2):
    - **Either wire it** behind the safe-point and notice-source interface (lead A),
    - **or keep it** under a `@pytest.mark.unwired` module marker that runs only in the full tier, with a dated note naming its spec dependency.
  - Under either option, write no more code on it until the plans-and-delegation open questions are settled (F-10).
- **Effort:** S (marking and the `reviews.py` deletion); M or L (wiring)
- **Risk and verification:**
  - Deleting `reviews.py`: check that `docs/ORCHESTRATOR-PROMPTS.md:73-76` mentions it only historically.
  - Re-run the import-graph script (in the scratch directory) after the change.
- **Depends on / enables:** It depends on the A-area notice-source interface and on F-10.

### F-08: Docs match the code on numbers but drift on "not built" statements and inventories
- **Where:** checked by two delegated reviewers (about 75 claims) and verified by sampling:
  - `docs/WORKER.md:31,70-96,107,749`
  - `docs/WORKSPACE.md:74,244-256,280,308,351,358,405`
  - `docs/LIVING-DOCUMENTS.md:9-23,79,135,187,193,236-237,261`
  - `docs/CONSISTENCY-REVIEW.md:216-218,226`
  - `docs/ORCHESTRATOR-PROMPTS.md:42-49,342-353,362-363,470,517`
- **Category:** docs drift
- **Priority:** P2
- **Evidence:**
  - **What matches.** About 60 numeric limits, defaults, flags and state paths match the code exactly: WORKER's channel, capture, jobs, admission, suspend, sync, lockfile and daemon limits; WORKSPACE's CSP, KaTeX and math-repair limits; LIVING-DOCUMENTS' text, publication, delivery and comment limits; CONSISTENCY-REVIEW's trigger, snapshot and retry rules.
  - **What drifts:**
    1. **Stale "not yet" statements that the same doc contradicts.**
       - `WORKSPACE.md:308`: jobs are lost on worker restart "until the executor daemon" is built. The daemon exists and is on by default.
       - `WORKSPACE.md:351`: an errored run "keeps no record yet".
       - `WORKSPACE.md:358`: no environment state from the worker.
       - `WORKSPACE.md:280,405`: no plan editor. `workspace/plan_edits.py` and `PlanPanel.svelte` exist.
       - `WORKER.md:749`: "No connection yet to … the event store". `worker/permissions.py` writes to it.
       - `LIVING-DOCUMENTS.md:193`: "Until the GUI can edit".
       - `LIVING-DOCUMENTS.md:236-237,261`.
       - `CONSISTENCY-REVIEW.md:216-218`.
       - `ORCHESTRATOR-PROMPTS.md:470,517`: USER plan editing is "future".
    2. **Incomplete inventories.**
       - The WORKER route table omits `POST /v1/kernels/lineage` and six provisioning-observation routes.
       - The WORKSPACE API table omits the jobs, kernels, provisioning, reviews, findings and plan-changes routes.
       - `WORKSPACE.md:74` says POST exists only for logout, repair and cancel.
       - The LIVING-DOCUMENTS table list omits about 15 tables (`annotation_body_revisions`, eight `math_*`, `document_export_inputs`, `worker_source_receipts`, …), and says "five tools" where there are 11.
    3. **Line-number pins have gone stale.** `ORCHESTRATOR-PROMPTS.md:42-49` (`SYSTEM_PROMPT` at 23, now 28; `run_task` at 286, now 354).
    4. **The prompt inventory misses a built prompt.** `child_prompt.py:21` `sandbox-delegated-child-1` is used by `child_controller.py`, but is never listed as a built prompt; the doc presents it only as a draft (362-363). The proposal table (342-353) still uses IDs that were never built.
    5. **Small errors.** `WORKER.md:107`: the example omits the required `--settings`. `WORKER.md:31`: the executor is "two files", but five are shipped (`backends.py:28-32`).
  - No dangling links were found.
- **Proposal:**
  - Fix the list above in one docs PR.
  - Prevent recurrence:
    - Add a test that compares the router's routes (`worker/app.py`, `workspace/app.py`) with the route tables in WORKER and WORKSPACE.
    - Add a test that lists the `CREATE TABLE` names against the LIVING-DOCUMENTS and CONSISTENCY-REVIEW table lists.
    - Ban line-number pins in favor of symbol names.
    - Give each doc a single "Not built" section, which the PR that builds a feature must edit (CONVENTIONS rule 1 already asks for this; a PR template checkbox makes it concrete).
- **Effort:** S (fixes), S (two inventory tests)
- **Risk and verification:** Low. The inventory tests are the verification.
- **Depends on / enables:** F-09 (structure).

### F-09: 72 docs pages, many written one per slice, mixing as-built text, history and progress logs
- **Where:**
  - `docs/` holds 72 top-level pages. 46 were created on 10-12 and 10-13, including families such as:
    - `SESSION-PLAN-{EDITING,EDITOR,WORKSPACE}` plus `SESSION-PLANS`;
    - six `PROVISIONING-*`/`PROVISION-*` pages;
    - four `KERNEL-NOTICE*`/`KERNEL-REVISIONS`;
    - three `LOCKFILE-DIFF*`;
    - three `DELEGATED-*` and three `*-PROMPT-PROVENANCE`.
  - `PLANNING-EVALUATION.md` and `REVISION-PACKAGES-WORKSPACE.md` are linked from nowhere in `docs/` or `README.md`. About 20 others are linked once. README's "Read first" table lists about 25.
  - `docs/CONSISTENCY-REVIEW.md` has two H1 headings. Its lines 223–298 are the 10-10 *planning* consistency review, which `README.md` presents as "Resolved planning drift".
  - `docs/WORKER.md` is 118 KB, 25 headings, no table of contents. It opens with a changelog (lines 3–13), and appended sections are out of order (the child-controller API before "Worker API").
  - `docs/specs/conda-provisioning.md:3`: the status line is an about-400-word progress paragraph with PR and lab numbers.
  - `docs/specs/README.md:3` still says "Implementation has begun with living documents".
- **Category:** docs drift, other
- **Priority:** P2
- **Evidence:**
  - **Progress narrative in docs.** The delegated reviewers measured narrative bytes (commit hashes, test counts, lab and PR numbers) at about 10% of WORKER, 15% of WORKSPACE, 5% of LIVING-DOCUMENTS, 12% of CONSISTENCY-REVIEW and 7% of ORCHESTRATOR-PROMPTS. ORCHESTRATOR-PROMPTS is about 50% proposals and drafts interleaved with as-built text.
  - `IMPLEMENTATION-PLAN.md:3-25` is a copy of STATUS (F-02).
  - **DECISIONS.** It is one 86-row table, with rows up to 3.2 KB (D69, D86, D84, D71, D78).
- **Proposal:**
  1. **One page per component**, each with "As built", "Limits", "Not built" and "Design notes": worker (split into execution, provisioning and sync if needed), workspace, documents, consistency review, plans and delegation, prompts. Fold the slice pages into sections and leave a redirect line in each old page for one release.
  2. **A `docs/README.md` index** listing every page with its kind: as-built, design or proposal, or historical.
  3. **Move history out.** The 10-10 planning review goes to `.agents/` as historical. Commit hashes, test counts and lab numbers leave `docs/` for the lab reports and PRs.
  4. **One-sentence spec status lines** plus a link to the as-built page.
  5. **DECISIONS:** keep one line of decision per row and move long reasons into a `### Dxx` notes section below the table.
- **Effort:** M
- **Risk and verification:**
  - Link churn: run a link checker (all links resolve today, per the delegated checks).
  - Do it after F-08's fixes, so content is corrected before it moves.
- **Depends on / enables:** It depends on F-02 and F-08. It enables reviewability (the owner's concern).

### F-10: Code is built on decisions still marked proposed and on draft specs
- **Where:**
  - `docs/DECISIONS.md:90` (D84, Proposed), `:91` (D85, Proposed), `:65` (D59, Proposed), `:49` (D43, Proposed) and `:60` (D54, Proposed).
  - `scripts/check_licenses.py:28-43` (ALLOWED includes PSF-2.0 and MIT-0), against `STATUS.md:231` ("Decisions to confirm: PSF-2.0 and MIT-0 in tier 1").
  - `docs/specs/plans-and-delegation.md:3` (Draft) and its open questions at `:139-148`; `docs/specs/skills.md:3` (Draft).
- **Category:** docs drift, architecture (governance)
- **Priority:** P1
- **Evidence:**
  - **D84** (provisioning operations): fully implemented and accepted in Labs 18, 21, 35 and 36.
  - **D85** (the SQLite control store): every control-store module depends on it: documents, annotations, consistency review, plans, jobs and delegation records.
  - **D59** (pdf_oxide): `packages/worker/pyproject.toml:24` depends on `pdf_oxide>=0.3.78`, while the decision says to "verify … on real cryptography papers before relying on it".
  - **D43** (isolation tiers and the probe rule): implemented.
  - **Messy status vocabulary.** The column holds 50 Confirmed, 17 Proposed, 12 Recommended, plus "Evaluate first", "Located, assessment partial" and "Working name".
  - **plans-and-delegation** is a draft, with seven open questions (approval default, progress across versions, the depth-1 / four-concurrent limit, kernel sharing, …). Yet `session_plans.py:51` implements `awaiting_approval`, and about 5,600 lines of delegation code plus 266 tests exist (F-07).
  - **skills** is a draft with zero code, which is consistent with the gap in the M2 exit path.
  - **tools and pdf-sources** are built "as drafted" and still await approval (`handoff.md:180`).
- **Proposal:** one owner session of about 30 minutes, prepared as plain-language decision requests (each with its effect, an example and a recommendation):
  - Confirm D84, D85, D59 (or mark it "accepted for phase 1"), D43, and PSF-2.0 and MIT-0.
  - Approve the tools and pdf-sources specs as drafted.
  - Answer plans-and-delegation questions 1–3, before any more delegation code.
  - Normalize the status vocabulary to Confirmed, Proposed, Deferred and Superseded, with an "evidence" column.
- **Effort:** S (preparation), plus the owner's time
- **Risk and verification:**
  - An owner's "no" on D85 or D84 would be expensive now, which is the argument for asking soon.
  - Verify by checking the DECISIONS status column with a small test that knows the allowed words.
- **Depends on / enables:** It unblocks F-07's decision. It supports the M2 exit focus.

### F-11: Lab scripts are 23,000 lines of copy-forward code that is not type-checked, and a product test depends on one
- **Where:**
  - `.agents/labs/` holds 41 files and 23,491 lines (22,777 Python). The largest:
    - `delegated_child.py` (2,677 lines)
    - `run_kernel_dispatch.py` (2,145)
    - `provisioning_upgrades.py` (1,586)
    - `provisioning_revision_packages.py` (1,483)
    - `provisioning_observations.py` (1,417)
    - `provisioning_comparison.py` (1,387)
    - `provisioning_workspace.py` (1,372)
    - `kernel_main.py` (1,204)
  - `packages/worker/tests/test_output_observations.py:824` loads `.agents/labs/output_observations.py` by path.
- **Category:** duplication, tests, other
- **Priority:** P2
- **Evidence:**
  - **Size.** The labs are larger than the orchestrator's 50 test files (26,296 lines). 84 of their commits landed on 10-12 to 10-14.
  - **Copy-forward duplication**, measured by line alignment:
    - `provisioning_upgrades` shares 84% of its lines with `provisioning_observations`;
    - `provisioning_revision_packages` shares 78% with `provisioning_comparison`;
    - `delegated_child` shares 29% with `run_kernel_dispatch`.
    - `main` is defined in 33 files, `bounded` in 10, `write_new` in 9 and `marked` in 8.
  - **Checks.** pyright's `include` (`pyproject.toml:69`) omits `.agents/labs`. ruff lints it, but with `E501` off (`pyproject.toml:66`).
  - **Coupling.** The product test above makes the lab directory part of the test surface.
- **Proposal:**
  1. Extract a shared lab harness, `labs/common.py` (VM up and down, owned teardown, evidence capture with hashing per F-01, marked guards). Keep one script per active lab family.
  2. Delete superseded scripts. The reports already pin the helper's sha256 and commit, so git history preserves them.
  3. Move the recipe that `test_output_observations.py:824` needs into that test's fixtures.
  4. Add `.agents/labs` to pyright in `basic` mode.
  5. Consider moving the labs out of `.agents/` into `tools/labs/`, since they are code, not notes.
- **Effort:** M
- **Risk and verification:**
  - Labs run only on babbage. Verify the refactored harness with each lab's existing "describe"/dry-run mode (`provisioning_upgrades.py` "defaults to description", per the Lab36 report) and with the existing guard tests.
- **Depends on / enables:** It depends on F-01 (evidence format).

### F-12: pyright, ruff and pytest configuration carry dead suppressions and implicit plugins
- **Where:**
  - `pyproject.toml:62-83`.
  - Inline suppressions in tests and src.
- **Category:** other (tooling)
- **Priority:** P3
- **Evidence:**
  - **Unnecessary ignore comments.** Running pyright on `packages` with `reportUnnecessaryTypeIgnoreComment` turned on reports 81 unnecessary ignore comments:
    - 70 in tests: 48 `ignore[reportPrivateUsage]` (tests run in "standard" mode, which never reports private use), 17 `# type: ignore`, and a few others;
    - 11 in src, for example `package_cache/adapters/{cargo,conda,go,hackage,npm,opam}.py` `reportMissingTypeStubs`, `documents/delivery.py:188,191`, `documents/parser.py:822`.
  - **File-wide disables in tests.** Five test files set `reportPrivateUsage=false` file-wide; in standard mode these do nothing.
  - **Inline noqa in tests.** Tests carry 102 inline `noqa` comments (S603 ×30, S608 ×23, S105 ×23, …). `per-file-ignores` for `packages/*/tests/**` lists only S101.
  - **Async test setup.**
    - `asyncio_mode` is unset, so there are 659 `@pytest.mark.asyncio` decorators plus 28 module-level `pytestmark` lines.
    - `pytest-asyncio` is used 659 times but is only a transitive dependency of `pytest-aiohttp` (`uv.lock`); it is not declared in `[dependency-groups].dev`.
  - **Asserts in production code.** src has 86 `noqa: S101`. These are asserts used for type narrowing in production code (for example `orchestrator/consistency/store.py` ×15, `documents/store.py` ×10). They are stripped under `python -O`; areas A and B own the code.
- **Proposal:**
  1. Set `reportUnnecessaryTypeIgnoreComment = "error"` and delete the 81 comments.
  2. Add `S105, S106, S108, S310, S603, S607, S608` to the tests' `per-file-ignores` and drop the inline noqa.
  3. Declare `pytest-asyncio` directly and set `asyncio_mode = "auto"` (pytest-aiohttp's recommended setup). Remove the decorators mechanically.
  4. Enforce the `integration` marker in a root `conftest.py`.
  5. Consider strict mode, or at least `reportPrivateUsage` as a warning, for tests once F-05 provides seams.
- **Effort:** S
- **Risk and verification:**
  - Auto mode can change how async fixtures are collected. Run the full tier once.
  - Verify that pyright and ruff stay clean.
- **Depends on / enables:** none.

### F-13: The license checks are sound, with small gaps
- **Where:** `scripts/check_licenses.py`, `packages/workspace/tests/test_licenses.py:20-30`, `ci.yml:33,46-48`.
- **Category:** other (tooling)
- **Priority:** P3
- **Evidence:**
  - The npm path checks the version 3 lockfile's source, its SHA-512 integrity hash, its license and the absence of install scripts, all *before* `npm ci --ignore-scripts`. It checks the installed `package.json` licenses after. Good.
  - The Python path checks the *installed CI environment* (Linux, dev group included, `kernels` and `pdf` extras). It does not check `uv.lock`, so a dependency that only installs on another platform (the macOS and arm64 phases) or under an unused extra is never checked.
  - `ALLOWED` contains PSF-2.0 and MIT-0, which are still "to confirm" (F-10). `CLASSIFIERS` maps the generic "BSD License" classifier to BSD-3-Clause, which is lenient but acceptable.
  - The checker's tests live in a deployable's test directory (`packages/workspace/tests`) and load the script by path.
- **Proposal:**
  - Move `test_licenses.py` to a root `tests/` directory (or `scripts/tests/`) and add it to `testpaths`.
  - Note in LICENSING that the Python check covers only the CI platform. Add a phase-3 backlog item for checking from the lockfile (for example `uv export` plus a metadata cache).
  - Settle PSF-2.0 and MIT-0 under F-10.
- **Effort:** S
- **Risk and verification:** Low.
- **Depends on / enables:** F-10.

## Keep

- **CI security basics.** Action SHAs are pinned with version comments, `persist-credentials: false`, `permissions: contents: read`, `uv sync --locked`, and the npm lockfile check runs before install with an installed-package check after. Generated code is checked with `generate_protos.py --check`, and `examples/openshell` staleness is checked by `test_openshell.py:1395-1405`.
- **The library and service packages' test style.** They use injected clocks (`mcp_proxy/sessions.py:116`, `package_cache/upstream.py:156`, `package_cache/store.py:176`, `documents/store.py:393`, `event_store/store.py:112`), black-box tests, and almost no private access or monkeypatching. 7,335 tests run in 30 s. This is the template for the worker and orchestrator tests.
- **Deterministic patterns that already exist in worker tests:**
  - helpers that follow the event stream instead of polling (`test_service.py:82-103` `finish` and `wait_running`);
  - `until_waiting(lock)` over `/proc/locks` (`test_provisioning.py:201`);
  - `entered` events in fakes (`test_run_dispatch.py:269`);
  - per-fixture `/proc` namespacing (`test_provisioning.py:189-191`);
  - event-store's `tamper` fixture.
- **Accurate numbers in the docs.** About 60 numeric limits and defaults checked in WORKER, WORKSPACE, LIVING-DOCUMENTS and CONSISTENCY-REVIEW match the code, and all relative links resolve.
- **Lab reports and the inventory.** The Markdown lab reports (0.5 MB in total) and `INVENTORY.json` with its sha256 values are the right core for hash-only retention.
- **Other good practices:** `.agents/upstream/` (one ready-to-file description per upstream bug), and tests that load `examples/` to keep them valid.

## Questions for the owner

1. **Where should raw lab evidence live** (GitHub release assets, Git LFS, a private evidence repository, or babbage only)? Is the GitHub repository public? Evidence records local paths and `/tmp` contents. Should past archives be purged from history, which breaks the "never rewrite published history" convention, or is stopping the growth enough?
2. **The unwired delegation and document-source code** (about 5,600 lines, plus 266 tests): wire it next, keep it as full-tier-only `unwired` code, or remove it until the plans-and-delegation questions are settled?
3. **Pending decisions.**
   - Confirm D84, D85, D59 and D43, and PSF-2.0 and MIT-0 in tier 1.
   - Approve the tools and pdf-sources specs.
   - Answer plans-and-delegation open questions 1–3: plan approval default, progress across versions, delegation limits.
4. **CI tiers.** Is it acceptable for PRs to require only Python 3.13 (fast plus slow tiers), with 3.12 and the serial full run on main, a merge queue or nightly? And may pytest-xdist, pytest-timeout and pytest-repeat (all MIT) be added as dev dependencies?
5. **Tracking files.** Do you agree to replace-not-append STATUS and handoff with size caps, and to keep progress narrative in PR descriptions and lab reports rather than in STATUS, PLAN-CHANGES and the docs?
