# Review notes: C, the worker

Baseline: main at fa456bc (2026-10-14). Reviewed:

- **Read in depth:** all of `manager.py` (3,549 lines); `daemon.py`, `channel.py` and `capture.py` (`owned`); `backends.py` (open and upload); the wiring in `cli.py` `serve` and `app.py` `create_app`; the schema, migrations, transactions and capture leases in `store.py`; `test_capture_ownership.py`; the slow tests in `test_robustness.py`; the fixtures in `test_daemon.py`.
- **Skimmed for structure and coupling:** `executor.py` (outline, `Executor.__init__`/`handle`/`execute`), `run_dispatch.py`, `run_api.py`, `shared_sync.py`, `provisioner.py`, `environments.py` (hooks), `docs/WORKER.md`, `docs/specs/tools.md` and `docs/specs/conda-provisioning.md`.
- **Skipped:** the internals of `provisioning.py`, `resolution.py`, `lockfile_diffs.py`, `installer_records.py`, `provision_observations.py`/`provision_events.py`, `documents_catalog.py`, `pdfhelper.py`, `admission.py`, `suspend.py` and `isolation.py`.

Measured or run (scratch under `scratchpad/refactor-review/C/`):
- The worker tests on their own, run serially: 1,344 passed and 32 skipped in 310.8 s, with `--durations`.
- A probe that times worker SIGTERM shutdown.
- A probe that reproduces the flaky capture test.
- A probe that reproduces a recovery race (C-01).
- A micro-benchmark of `Store.commit_event` on tmpfs.

## Summary

The worker works and is well tested. Its central invariants are sound and worth keeping:
- every execution is stored before dispatch;
- every event is stored before delivery;
- a terminal state is reached exactly once;
- nothing an executor sends is trusted.

The structure has not kept up with three days of feature growth:
- `manager.py` grew from 661 to 3,549 lines in 37 commits since 2026-10-11.
- Every subsystem was added to the same class: daemon recovery, package admission fencing, capture leases, kernel lineage and observation, and selected child kernels.
- Their guards are spread over seven independent sets and flags. The same 12-clause "is this environment healthy" check now exists three times, in three files.

I confirmed one real recovery bug (C-01) and one real shutdown hang (C-10), and found the mechanism of the CI flake (C-11).

The changes that matter most:
1. **Fix recovery so it is keyed by environment (C-01).** Do this together with C-04, giving each environment one explicit state object.
2. **Decide whether the selected child controller (run-kernels) stays (C-05).** It is about 2,200 worker lines plus 63 touch points in the manager, with no production caller. Park it or put it behind an interface before splitting the manager.
3. **Split `ExecutionManager` along its nine real responsibilities behind an unchanged facade (C-03).** Do it in the order given there, after a shared test harness (C-13) cuts the tests' 65 direct `manager._environments` accesses.
4. **Push daemon events instead of polling each running execution every 100 ms (C-02).** At the same time, run the conformance suite over the daemon transport, which is the production default (C-14).
5. **Make shutdown and the tests deterministic (C-10, C-11, C-12).** Together these remove about 32 s from the worker suite and fix the known flake.

## Findings

### C-01: A bridge loss during a live recovery orphans executions dispatched meanwhile
- **Where:**
  - `manager.py:544-545`: the reentrancy guard returns early.
  - `manager.py:1375-1381`: `_on_closed` spawns `_recover(environment_id, lives)` with a list it captured.
  - `manager.py:664-674`: the retry covers only the original list.
  - `manager.py:740-748`: the poller replays only executions whose `live.channel` is its own channel.
- **Category:** correctness, concurrency
- **Priority:** P1
- **Evidence:**
  - `_recover` takes a list of executions captured by the caller, and drops the whole call when a recovery for that environment is already running.
  - An execution dispatched on the reopened channel while recovery is still waiting or replaying is in no recovery's list. If that channel closes, the second `_recover` returns at once, and nothing ever replays the execution again.
  - Reproduced with a scratch test built on `test_daemon.py`'s fixtures:
    1. Hold `confirmed_daemon_loss` on the first call.
    2. Submit a second job.
    3. Close its bridge.
    4. Release the hold.

    The first job ended `succeeded`. The second stayed `running` with a closed channel (`state=running, channel closed=True`) until the test gave up.
  - In production such an execution ends `lost` only through its watchdog. That comes at its time limit plus 90 s, which for a job is 24 hours by default.
  - The window is a live recovery, which happens exactly when the gateway or bridge is unstable.
- **Proposal:**
  - Make recovery environment-keyed: `_recover(environment_id)` collects, on every attempt, all non-terminal dispatched daemon executions of that environment whose channel is missing or closed.
  - Replace the early return with a "recover again" flag that the running recovery checks before it exits.
  - Merge `_retry_recovery` and the five-attempt inner loop into one capped exponential backoff, for example 0.2 s doubling to 30 s. Today it retries every 1 s forever.
- **Effort:** S
- **Risk and verification:** Add the scratch scenario as a regression test in `test_daemon.py` first. The existing daemon tests (`test_daemon.py`, `test_run_dispatch.py:821-827`, `test_environments.py:1194`) call `_recover(env, lives)` directly, so they need a signature change.
- **Depends on / enables:** C-04 and C-03 (recovery becomes its own unit).

### C-02: The daemon transport is poll-only: a replay RPC per running execution every 100 ms
- **Where:**
  - `daemon.py:134-201`: event messages are journalled and never written to the bridge.
  - `daemon.py:247-307`: replay pages of at most `PAGE_EVENTS = 32`.
  - `manager.py:740-753`: `_poll_daemon` sleeps 0.1 s between sweeps.
  - `manager.py:729-738`: one replay per execution, `reply_timeout=30`.
- **Category:** architecture, performance
- **Priority:** P2
- **Evidence:**
  - With `[openshell] daemon = true`, the default (`settings.py:129`), the worker learns of output only by asking.
  - Each running execution costs one `replay` round trip, through `openshell sandbox exec` and the in-sandbox bridge, every 100 ms for as long as it runs. A 24-hour job is about 864,000 RPCs.
  - Throughput is capped at 32 events per 100 ms per execution, so catching up after a disconnect is paced.
  - One sweep is sequential over every execution on the channel. With the daemon's `MAX_ACTIVE = 128`, and taking WORKER.md's 14 ms median shell-call round trip as an estimate for one replay, one sweep can take about 1.8 s. Latency therefore grows linearly with active work, and one hung replay stalls every execution for 30 s.
  - Each event is also its own FULL-sync SQLite transaction on the event loop (`store.py:1053-1059`). This costs 0.02 ms on tmpfs; the fsync cost on real disk was not measured.
- **Proposal:**
  - Have the daemon forward each journalled event line to the active bridge as it is produced. `source_seq` is already attached, and the worker already deduplicates by `source_seq` and detects gaps through `source_from` (`manager.py:1708-1725`).
  - Keep `replay` only for reconnects and detected gaps.
  - Drop `_poll_daemon`, or keep a slow liveness poll of a few seconds.
  - Commit replayed pages in one transaction.
- **Effort:** M
- **Risk and verification:**
  - The ordering between pushed lines and a replay page during reconnect must be checked.
  - Write backpressure exists: the daemon's 10 s drain timeout closes the bridge (`daemon.py:102-105`).
  - Run `test_daemon.py` and the conformance suite over the daemon transport (C-14), then a lab check that output latency stays flat with 50 concurrent jobs.
- **Depends on / enables:** C-14. It removes the 100 ms polling sleep that the daemon tests depend on.

### C-03: `ExecutionManager` is a 3,114-line god class; split it along its real responsibilities
- **Where:** `manager.py:315-3428`: 106 methods and 39 instance attributes set in `__init__`, five of them callbacks assigned from outside (`manager.py:370-378`).
- **Category:** architecture, complexity
- **Priority:** P1. Stop the growth now; the full split is staged.
- **Evidence:**
  - Size by responsibility (approximate line ranges):

    | Responsibility | Lines | Approx. size |
    | --- | --- | --- |
    | Execution journal: track, emit, finish, the storable final result, lose; get, events, wait; result sanitizers | 925-1050, 2028-2097, 2638-2810, 3431-3549 | 520 |
    | Dispatch and cancellation: submit with inline job-quota policy, send, watch, the cancel family, jobs | 1394-1693, 2657-2761, 2812-2900 | 560 |
    | Executor events: `_on_event`, `_apply`, `_end`, `_revised` | 1695-1937, 2143-2212 | 380 |
    | Daemon recovery and fencing | 494-753, 1088-1128 | 300 |
    | Environment connections: `_environment`, `_open`, `_using`, `_on_closed`, release, activity, idle | 755-923, 1054-1209, 1347-1390 | 360 |
    | Kernels: generations, birth pins, status, passive observation, lineage, restart, close session | 2228-2634, 2902-2978 | 490 |
    | Package admission gate and lockfile fetches | 386-492, 1233-1311 | 230 |
    | Files and shared-sync glue | 1939-1994, 2995-3106 | 170 |
    | Artifact capture and publication | 3108-3428 | 320 |

  - Branchiest methods:

    | Method | Lines | Branch nodes |
    | --- | --- | --- |
    | `_recover` | 134 | 53 |
    | `_apply` | 130 | 36 |
    | `_capture_range` | 136 | 35 |
    | `_kernel` | 113 | 34 |
    | `_send` | 128 | 30 |

  - Duplication inside the class:
    - "cancelled before it started" `_finish` blocks appear four times (`1548`, `1564`, `1619`, `2852`).
    - `_package_origin` runs twice around the gate ACK (`1630`, `1640`).
    - The "no legacy gate" refusal appears three times (`470`, `1634`, `2906`).
  - Duck typing on backend internals: `getattr(getattr(self.backend, "settings", None), "daemon", False)` at `496` and `894`, and `getattr(self.backend, "root", "")` at `429` and `1240`.
- **Proposal:** Keep `ExecutionManager` as the facade that `app.py`, `provisioner.py` and `environments.py` already call. Move each responsibility into its own object in this order (lowest coupling first):
  1. `EnvironmentConnections` with an explicit per-environment state (C-04).
  2. `Publication` (capture and leases).
  3. `KernelRegistry` (generations, births, status, lineage).
  4. `PackageAdmission` (`package_gate.py` already holds its value type).
  5. `DaemonRecovery` (C-01, C-02).
  6. `ExecutionJournal` and `Dispatcher` last.

  Also:
  - Turn the job-quota block (`1433-1494`) into a pure `job_limits(request, resources, running, session)` function with table tests.
  - Add `daemon: bool` and `binding_key()` to the `Backend` protocol instead of using `getattr`.
  - Adopt a rule now: no new subsystem goes directly into `manager.py`.
- **Effort:** L (staged; each step M)
- **Risk and verification:**
  - Tests reach into internals (65 `manager._environments`, 21 `manager._live`, 15 `manager._kernel_observations`, 9 `manager._apply`), so build the shared harness and accessors first (C-13).
  - Each step is a move with the facade unchanged. The full worker suite (about 5 min) and `test_conformance.py` cover it.
- **Depends on / enables:** C-04 and C-05 before; C-01 and C-02 become local changes.

### C-04: "Is this environment usable?" is seven independent flags, checked by a 12-clause predicate copied into three files
- **Where:**
  - The flags: `manager.py:365-375` (`_closing`, `_recovering`, `_daemon_fences`, `_quarantined`, `_released_environments`, `_closing_sessions`) and `store.recycle_needed`.
  - The predicate: `manager.py:2416-2439`, `run_dispatch.py:86-106` and `run_api.py:225-249`.
  - Partial versions: `manager.py:1424-1432` and `3302`.
- **Category:** duplication, coupling
- **Priority:** P2
- **Evidence:**
  - The three predicates differ only slightly (`recovery` exempts `_recovering` in `run_dispatch`).
  - All three read manager privates under `# pyright: reportPrivateUsage=false` (`run_dispatch.py:52`, `run_api.py:52`).
  - `_quarantined`, `_daemon_fences` and `store.recycle_needed` are three representations of one "fenced" fact. They are set and cleared in different places (`1089`, `1128`, `1158`, `574`, `590`).
- **Proposal:** Add one `EnvironmentState` per environment, with a small enum: opening, ready, recovering, fencing, releasing, closed. Give it explicit transition methods and a single `usable(scope, *, allow_recovering=False)` query. Persisted recycle intent stays in the store, but is read in one place.
- **Effort:** M
- **Risk and verification:** `test_kernel_observation.py:345` already parametrizes over these flags; extend it to the new states. `test_run_dispatch.py` and `test_run_api.py` cover the copies.
- **Depends on / enables:** First step of C-03; makes C-01 simpler.

### C-05: The selected child controller (run-kernels) is unwired, yet threaded through the manager, executor and store
- **Where:**
  - `run_dispatch.py` (544), `run_api.py` (585), `run_kernels.py` (334), `run_kernel_state.py` (739). That is 2,202 lines, all with `reportPrivateUsage=false`.
  - 63 references in `manager.py`: `is_run`, `run_dispatcher`, `RunCellRequest`, `_cancel_selected`, and others.
  - Run operations in `executor.py:2600-2780` (`run_closed` … `stop_run_kernels`); `kernel_scope` and `run_recovery` in `store.py:596-614` and `995-1030`; routes at `app.py:329-330`.
- **Category:** dead code (unwired), coupling
- **Priority:** P1 (decide before the manager split)
- **Evidence:**
  - The only consumer, `orchestrator/child_controller.py`, is imported only by `packages/orchestrator/tests/test_child_controller.py` (grep across `packages/*/src`).
  - The routes are enabled only by a client with `run_controller = true`, which no deployed configuration sets (WORKER.md, "Selected child controller API").
  - The subsystem reaches `manager._live`, `_environments`, `_track`, `_state`, `_spawn`, `_dispatch`, `_lose`, `_passive_kernels`, `_sync_package_gate` and `store._db`/`store._transaction` (`run_dispatch.py:91-194, 232-235, 392-526`; `run_api.py:76-330`).
  - It adds a second recovery path into `start()` and `_recover` (`510-530`, `622-633`), and a second cancel path (`2815-2900`).
  - This matches the brief's lead that delegation work is "unwired prerequisites".
- **Proposal:**
  - Recommended: park it. Move the four modules and the executor's `run_*` operations behind a `RunKernelExtension` interface. The manager would call it at four named hooks: admission, kernel mapping, recovery, cancel.
  - Alternatively, delete it from main and keep it on a branch until plans and delegation reach M2.
  - Either way, the manager split (C-03) should not have to carry it inline.
- **Effort:** M (interface) or S (removal, plus schema handling for any retained `run-v1` rows)
- **Risk and verification:**
  - The `kernel_scope` column and the startup marker check (`store.py:211-226`) must still refuse `run-v1` rows if old state exists.
  - `test_run_dispatch.py`, `test_run_api.py`, `test_run_kernels.py` and `test_child_controller_http.py` (about 2,500 test lines) move with it.
- **Depends on / enables:** C-03 and C-04. This is a product decision; see the questions.

### C-06: Wiring by assigning callbacks after construction, split between `cli.serve` and `app.create_app`
- **Where:**
  - `manager.py:370-378`: `job_resources`, `confirmed_daemon_loss`, `daemon_identity`, `recycle_daemon` and `notices` default to `None`.
  - `cli.py:146-164`: `confirmed_daemon_loss`, `recycle_daemon`, `daemon_identity` and `notices`.
  - `app.py:228-251`: `job_resources` and five `Environments` hook lists.
  - `cli.py:217-265`: `_confirmed_daemon_loss`, about 50 lines of lifecycle policy.
- **Category:** architecture, coupling
- **Priority:** P2
- **Evidence:**
  - `self.daemon_identity` appears 13 times in the manager, nearly always as `if self.daemon_identity is not None`.
  - The HTTP app factory mutates the manager. Production wiring is therefore half in `serve` and half in `create_app`, and the four test harnesses (C-13) each wire their own subset; for example, `test_daemon.py:92` sets only `daemon_identity`.
  - Daemon-loss policy lives in the CLI module. It builds a throwaway `OpenShellBackend` just to get `cli_env()` (`cli.py:244`).
- **Proposal:**
  - Add one composition function, `build_worker(settings) -> WorkerParts`, used by `serve` and by the test harness.
  - Pass the lifecycle collaborators as one `EnvironmentLifecycle` protocol in the manager's constructor (identity, confirmed loss, recycle, notices, job resources). Its no-op implementation serves the local backend.
  - Move `_confirmed_daemon_loss` into `environments.py`.
- **Effort:** M
- **Risk and verification:** A startup smoke test (`test_robustness.py::test_c1…` already launches `serve`) plus a test that builds a worker through `build_worker` with OpenShell settings and a fake CLI.
- **Depends on / enables:** C-13 (one harness), C-03.

### C-07: Store schema has no version; migrations and backfills run on every start; tables are owned by six modules
- **Where:**
  - `store.py:51-159`: `_SCHEMA`.
  - `store.py:203-287`: `__init__`, 85 lines of create, ALTER and backfill.
  - `CREATE TABLE` in `kernel_revisions.py`, `output_observations.py`, `run_kernel_state.py`, `run_kernels.py` and `run_api.py`.
  - Private access to `store._db` and `store._transaction` in `run_*` (about 30 sites).
- **Category:** persistence
- **Priority:** P2
- **Evidence:**
  - No `PRAGMA user_version` anywhere.
  - Column additions are detected with `PRAGMA table_info` (`store.py:211-237`).
  - The package-gate backfill from legacy tables reruns at every start (`241-287`).
  - The worker keeps four SQLite files: `worker.sqlite3`, `environments.sqlite3`, `shared.sqlite3` and `documents-catalog.sqlite3` (`cli.py:122-130`, `312`).
  - Several SQL literals are split mid-identifier by line wrapping, which defeats grep (`store.py:216-219` `'run_dispatch_met' 'a'`, `1008-1009` `req` `uest`).
  - All Store calls run synchronously on the event loop. That is acceptable at current rates (0.02 ms per event on tmpfs), but undocumented.
- **Proposal:**
  - Add a `schema.py` with an ordered migration list keyed by `user_version`, and turn backfills into one-time migrations.
  - Expose `Store.transaction()` publicly, and pass a `Database` handle to sub-repositories instead of letting them reach `_db`.
  - Add a lint test (or a ruff rule via `ISC`) that forbids implicit string concatenation inside SQL literals.
- **Effort:** M
- **Risk and verification:** Upgrading existing lab state directories needs a fixture database from the current schema. Add a test that opens a copy of today's schema and checks `user_version` advances and nothing else changes.
- **Depends on / enables:** C-05 (fewer owners), C-08.

### C-08: Read-time re-validation of the worker's own SQLite goes beyond the threat model
- **Where:**
  - `typeof(` and `substr(` projections: store 16/31, `run_kernel_state` 25/14, `provision_observations` 23/8, `output_observations` 21/9, `run_kernels` 12/4, `run_api` 10/2, `kernel_revisions` 3/5, `documents_catalog` 0/15.
  - Examples: `store.py:292-346` (`capture_leases`: 40 lines of regex checks on its own rows) and `store.py:596-614`.
- **Category:** over-engineering
- **Priority:** P3
- **Evidence:**
  - THREAT-MODEL places the worker and its state in Z1 (trusted).
  - What the sandbox sends is already checked and bounded at ingestion (`manager._result`, `channel.decode_line`).
  - Re-validating and byte-clipping every column on read protects only against someone who can already write the worker's state directory.
  - The cost is readability, roughly 200 defensive projections, and unusual failure modes, for example `{"unknown-corrupt-state": 4}` blocking all publication at `manager.py:3261`.
- **Proposal:** Validate once at write time with typed row helpers. Treat a corrupt row as an operator-visible integrity error that holds only the affected scope. Drop clipping except where a row can legitimately be large.
- **Effort:** M (opportunistic, alongside C-07)
- **Risk and verification:** Keep the negative tests that matter (corrupt lease fails closed). Many "corrupt row" tests in `test_provision_observations.py` and `test_output_observations.py` would be simplified.
- **Depends on / enables:** C-07.

### C-09: Capture leases can wedge publication with no exit, and assume a second worker on the same state directory
- **Where:**
  - `manager.py:3118-3253` (`_capture_range`), `3264-3293` (`_reconcile_captures`, called only from `_open` at `1209`) and `3295-3333` (`publish`).
  - Hard-coded 1 s cleanup ACK at `3172` and `3287`.
  - `store.py:348-510` (leases: four global, two per environment).
  - `test_capture_ownership.py:97-139` asserts that an `active` row survives restart.
- **Category:** over-engineering, correctness (availability)
- **Priority:** P2
- **Evidence:**
  - A capture whose cleanup ACK misses its 1 s window becomes a durable `held` lease.
  - Held leases are reconciled only when a channel is opened, when the VM is recycled, or when deletion is confirmed. On a healthy long-lived channel, two such misses block publication in that environment until the bridge reconnects, and four block the whole pool.
  - An `active` row left by a crashed worker never clears without the VM going away. The reason given is that "another process may still own host I/O". Host threads cannot outlive their process, so this only matters if two workers share one state directory, and nothing prevents that: there is no lock file.
  - There is no operator command to inspect or clear leases. `cli.py` offers only `serve`, `token`, `policy add` and `admissions`.
  - Publication is on the M2 path (roadmap fixture, step 3).
- **Proposal:**
  1. Take an exclusive `flock` on `state_dir/worker.lock` in `serve`. Then at startup every `active` lease is from a dead process, so convert it to `held`.
  2. Retry `capture_cancel` for `held` leases periodically on the healthy channel, not only on open.
  3. Add `sandbox-science-worker captures --settings … [--clear ID]`.
  4. Longer term, consider running capture reads as ordinary bounded internal executions, which already guarantee termination and cleanup, instead of a parallel lease protocol.
- **Effort:** S (lock, retry, command); M (redesign)
- **Risk and verification:**
  - Adjust the three ownership tests (`test_capture_ownership.py:41-139`).
  - Add tests: a second `serve` on the same state directory is refused; held leases clear after one successful retry.
- **Depends on / enables:** Simplifies `test_capture_ownership.py`; see C-11.

### C-10: SIGTERM shutdown blocks on open event streams: about 15 s after a client left, over 60 s while one is connected
- **Where:**
  - `cli.py:193-206`: `runner.cleanup()` runs before `manager.close()`.
  - `app.py:1047-1071`: the stream handler.
  - `manager.py:2793-2799`: it waits on `changed` with `EVENT_HEARTBEAT_SECONDS`.
- **Category:** correctness (operations)
- **Priority:** P2
- **Evidence (measured with a scratch probe against `sandbox-science-worker serve`, local backend):**

  | Stream state at SIGTERM | Time to exit |
  | --- | --- |
  | No open stream | 0.22 s |
  | Client read one event, then left | 15.3 s (the next heartbeat write) |
  | Client still connected | Not within 60 s (probe gave up; aiohttp's default `shutdown_timeout` is 60 s) |

  - Streams end only when the execution changes, and `manager.close()`, which would end them, runs after the HTTP runner's cleanup has waited.
  - A harness streaming a long job therefore makes every worker restart take a minute or more, close to systemd's 90 s stop timeout.
  - It also accounts for 15 s of `test_c1_sigterm_stops_the_worker_cleanly_and_it_starts_again` (16.9 s).
- **Proposal:** On shutdown:
  1. Mark the manager closing.
  2. Resolve every live `changed` future, or give `events()` a closing event, so streams end at once (clients reconnect with `after`, which the protocol already supports).
  3. Only then run `runner.cleanup()`.
  4. Optionally set `shutdown_timeout` explicitly.
- **Effort:** S
- **Risk and verification:** Clients must treat a stream ending without a `result` as a reconnect. The orchestrator client already treats a dropped stream as one (WORKER.md, Event streams). Add a test: SIGTERM with a connected stream exits in under 2 s.
- **Depends on / enables:** C-12.

### C-11: The flaky `test_cancel_ack_deadline_includes_actual_channel_write_lock` races a 50 ms wall-clock budget
- **Where:** `test_capture_ownership.py:142-163` sets `capture_timeout = 0.05`, holds `_write_lock` and expects "unknown". In the manager, `_capture_range` raises `TimeoutError` before dispatch if the budget is already spent (`manager.py:3128-3130`), and that ends as "budget expired; cleanup completed" (`3411-3414`).
- **Category:** tests
- **Priority:** P2
- **Evidence:**
  - The budget starts at `publish` (`3304`). It also covers `_environment`, `_using` and creating the blob writer in the thread pool (`3355-3360`).
  - With a 60 ms delay injected into `Blobs.writer` to stand in for a loaded CI box, the test fails with `Actual message: 'artifact capture operation budget expired; cleanup completed'`; with no delay it passes.
  - The CI log of the one failure was not available to compare.
  - The outer `asyncio.timeout(2)` is also tight. The path includes the hard-coded 1 s ACK wait.
- **Proposal:**
  - Make the test wait for an explicit point instead of racing a clock:
    - monkeypatch `store.dispatch_capture` to set an `asyncio.Event`;
    - use a generous `capture_timeout` (for example 5 s);
    - once dispatched, let the budget expire through an injectable clock, or cancel the publish task.
  - Make the 1 s cleanup ACK a module constant (`CAPTURE_ACK_TIMEOUT`) the test can shrink.
  - Treat outer `asyncio.timeout`s as hang guards only (at least 10 s).
- **Effort:** S
- **Risk and verification:** Run the test 200 times under `stress-ng` or with the injected delay.
- **Depends on / enables:** C-13 pattern.

### C-12: The two slowest worker tests spend 31 of their 32.5 s in fixed grace periods
- **Where:**
  - `test_robustness.py:844-861` (15.6 s): it patches `WRITE_TIMEOUT` but not `CLOSE_GRACE`, so `manager.close()` waits `channel.py:35` `CLOSE_GRACE = 15` for the deaf executor before killing it (`channel.py:242`).
  - `test_robustness.py:265-319` (16.9 s): see C-10.
- **Category:** tests, performance
- **Priority:** P3
- **Evidence:**
  - Worker suite, serial: 310.8 s. The slowest 40 tests sum to 122.9 s: `test_robustness` 44.2, `test_provisioning` 26.3, `test_service` 19.1, `test_conformance` 12.5, `test_daemon` 12.2.
  - Other long tails are deliberate subprocess waits, for example `test_m6_many_installs_in_one_call_are_one_revision_with_its_lockfile` at 7.9 s.
- **Proposal:** Monkeypatch `channel_module.CLOSE_GRACE` to 0.5 s in the deaf-executor test, and fix C-10 for the SIGTERM test. Expected saving: about 30 s.
- **Effort:** S
- **Risk and verification:** None beyond the two tests.
- **Depends on / enables:** C-10.

### C-13: Tests synchronize on sleeps inside research scripts, use four separate worker harnesses, and reach into privates
- **Where:**
  - 167 `sleep(` calls in worker tests, 76 of them `sleep(0)` yields. The largest counts are in `test_provisioning` (24), `test_run_dispatch` (16) and `test_capture` (13).
  - Research-script timing such as `test_daemon.py:806-816` (`sleep .3` in the job, then `await asyncio.sleep(0.8)` in the test before restarting, to force "omitted while disconnected"), `144`, `174`, `250`, `278` and `835`.
  - Four `Worker` harness classes: `test_provisioning.py:537`, `test_robustness.py:67`, `test_service.py:50`, `test_daemon.py:78`.
  - Three `manager` fixtures, two `running` helpers and three `until` helpers.
  - 145 `manager._…` private accesses in the tests.
- **Category:** tests
- **Priority:** P2
- **Evidence:**
  - Several assertions depend on the order of real subprocess output relative to worker actions. Under load, the 100 ms poller (C-02) can consume events before the test's `close()`, so the rollover marker never appears.
  - The tests also accumulate zombie child processes in the pytest process (45 at about 80% of the run; not attributed further).
  - `fake_manager.py` is a fake *package* manager (pip/micromamba). The name invites confusion with `ExecutionManager`.
- **Proposal:**
  - Add a shared `tests/harness.py` built on `build_worker` (C-06), parametrized by transport (stdio or daemon). It provides:
    - `until(predicate, timeout=30)`;
    - gate helpers: research scripts wait on a file or FIFO the test creates, as `fake_manager.py`'s `FAKEPM_GATE` already does;
    - read-only accessors (`harness.channel(env)`, `harness.live(id)`) so tests stop touching `_environments` and `_live`.
  - Rename `fake_manager.py` to `fake_package_manager.py`.
- **Effort:** M
- **Risk and verification:** Migrate file by file; the counts of sleeps and private accesses are the progress measure.
- **Depends on / enables:** C-03 (splitting the manager without rewriting 2,000 test lines), C-14.

### C-14: The conformance suite covers only the stdio transport; production runs the daemon
- **Where:**
  - `test_conformance.py:192` uses `LocalBackendSettings()`, where `daemon` defaults to `False` (`settings.py:85`).
  - The OpenShell default is `daemon = True` (`settings.py:129`).
  - Six test files use `daemon=True`.
- **Category:** tests
- **Priority:** P2
- **Evidence:** The 23 D52 fixtures run in CI never exercise replay-based delivery, reconnection to the same daemon, or source-cursor acknowledgement, which is the path every OpenShell run uses. Only Lab 23 ran them over the daemon, against OpenShell.
- **Proposal:** Parametrize the suite's local worker over `daemon in (False, True)`. Also consider making the local backend default to the daemon, keeping stdio as a development option.
- **Effort:** S
- **Risk and verification:** Adds roughly 15-20 s. It will expose C-02 latency if any test assumes immediate events.
- **Depends on / enables:** C-02.

### C-15: Failures in background tasks are silent, and the poller dies on anything but `WorkerError`
- **Where:**
  - `manager.py:927-931`: `_spawn` only discards finished tasks.
  - `740-753`: `_poll_daemon` catches only `WorkerError`.
  - `676-678`: `_retry_recovery` retries every 1 s forever.
  - `880`: `self._blob_pool.shutdown(wait=True)` runs on the event loop thread.
- **Category:** correctness (error handling)
- **Priority:** P3
- **Evidence:**
  - An `sqlite3.Error` or `ValidationError` in `_recover`, `_poll_daemon`, `_watch` or `_fetch_lockfile` ends the task. It is reported only as asyncio's "Task exception was never retrieved", at garbage collection.
  - If the poller dies, its executions stop advancing until their watchdog fires.
  - `capture.owned` (`capture.py:45-55`) is a host-side asyncio helper that lives in a module uploaded into sandboxes. `executor.py:49-58` has a second, different `_owned`.
- **Proposal:**
  - Give `_spawn` a done-callback that logs any exception other than `CancelledError`, naming the task.
  - Make the poller catch `Exception`, log it and close the channel, so recovery runs.
  - Use capped backoff (see C-01).
  - Move the host-side `owned` into a host `asyncutil.py`.
  - Shut the blob pool down with `await asyncio.to_thread(pool.shutdown, wait=True)`.
- **Effort:** S
- **Risk and verification:** Add a test that injects a store error into a replay and checks for a log record and a recovered execution.
- **Depends on / enables:** C-01.

### C-16: `executor.py` (3,861 lines) can be split into sibling modules without changing how it ships
- **Where:**
  - `executor.py:61-77`: `_sibling()` already loads `capture.py` and `provisioning.py` "beside this file" in a sandbox.
  - `backends.py:228-239`: five files uploaded one by one on every `open`.
  - `executor.py:2857` `execute`: 178 lines, 56 branch nodes, the worst in the package.
  - `executor.py:2303` `run_shell`: 147 lines.
- **Category:** complexity
- **Priority:** P2 for the guard test and the first two moves; P3 for the rest.
- **Evidence:**
  - The "one file by design" constraint already does not hold. The sandbox payload is `executor.py`, `provisioning.py`, `pdfhelper.py`, `daemon.py` and `capture.py`, plus the shim. WORKER.md still says "two files" (C-17).
  - Natural seams that are already module-level functions:

    | Seam | Lines | Size |
    | --- | --- | --- |
    | Confined file operations (`check_relative` … `list_directory`) | 288-1009 | about 720 |
    | Processes (`process_table` … `protect_process`) | 1020-1290 | about 270 |
    | Protocol framing (`encode_line`, `fit_line`, `shrink_result`, `serve_stdio`) | 3694-3829 | about 140 |
    | Probe and configuration (`probe_facts` … `_replace_file`) | 3525-3668 | about 140 |

  - Within `Executor`, the remaining split is shell/jobs, kernels (with `Kernel`, 1370-1497), package gate and revision reporting, and, if kept, the `run_*` operations (C-05).
  - No test enforces the standard-library-only rule. An AST check finds only `jupyter_client` (lazy) and `pdf_oxide` (pdfhelper) today.
  - Every `open` costs one `sandbox exec mkdir` plus five or six sequential `sandbox upload` CLI calls.
- **Proposal:**
  1. Add a test that parses the uploaded modules and allows only `sys.stdlib_module_names`, siblings, and the two named lazy imports.
  2. Move the file and process functions into `sandbox_files.py` and `sandbox_processes.py`, loaded through `_sibling`.
  3. Keep the upload list in one `SANDBOX_FILES` manifest, shared by `backends.py` and the test.
  4. Later, upload one archive, or skip the upload when a digest file in `executor_dir` matches.
  5. Pure-function the iopub message handling out of `execute`.
- **Effort:** M
- **Risk and verification:**
  - Sandbox imports by path (`_sibling`, `daemon.py:33-40`, `capture.py:266`, `provisioning.py:211`) need one integration run on babbage, because uploaded file names change.
  - `test_executor.py` (852 lines) and `test_provisioning.py` cover behavior locally.
- **Depends on / enables:** C-05 (the run operations leave), C-17.

### C-17: Worker docs drift and read as a lab diary
- **Where:**
  - `docs/specs/tools.md:39`: "Worker restart survival … still open". Also `:56` and `:124`: "until it exists, a worker restart makes running jobs `lost`".
  - `docs/WORKER.md:31`: "two files that depend only on the standard library".
  - `WORKER.md:1-13`: a paragraph about one narrow route comes before the introduction.
  - `WORKER.md:596-663`: replay is described, but not that events reach the worker only by 100 ms polling.
  - The status paragraph of `docs/specs/conda-provisioning.md:3`, about 600 words of PR and lab history.
- **Category:** docs drift
- **Priority:** P3
- **Evidence:**
  - The daemon is the default and jobs survive worker restarts (WORKER.md "Behavior worth knowing → Restarts"; lab 20), so tools.md contradicts it.
  - WORKER.md (750 lines) mixes reference material with lab and PR evidence ("signed `35863776`", "Lab24", CI run IDs), which makes the behavior hard to find.
- **Proposal:**
  - Correct tools.md items 10 and Lifecycle 5.
  - Move per-PR and per-lab evidence from WORKER.md and the spec status lines into `.agents/docs/agent-reports/`, leaving a single "validated by" link per section.
  - State the polling transport (until C-02 lands), and update the component list (C-16).
- **Effort:** S
- **Risk and verification:** None.
- **Depends on / enables:** Area F (docs).

### C-18: The worker's document catalog is a parked prerequisite
- **Where:** `documents_catalog.py` (703 lines), `app.py:211-224` and `335-431` (routes and handlers), `contracts/documents.py`.
- **Category:** dead code (unwired)
- **Priority:** P3
- **Evidence:**
  - WORKER.md says it "writes no environment files, advances no environment's written revision, and advertises no mirror capability".
  - Its client methods (`orchestrator/worker_client.py:290-337`) are called from `documents/delivery.py`, which the brief lists as nearly unreferenced.
  - Nothing on the M2 path uses it yet.
- **Proposal:** Keep it, since it is self-contained with its own SQLite file and executor. Do not extend it until environment publication is designed, and record it in the backlog as parked. Coordinate with area B.
- **Effort:** S (bookkeeping)
- **Risk and verification:** None.
- **Depends on / enables:** Area B (documents).

## Keep

- **The execution invariants and their implementation:**
  - stored before dispatch, events stored before delivery;
  - `_finish` with `_final`'s storable-or-shrunk-or-replaced fallback (`manager.py:971-1050`);
  - the watchdog that always ends an execution;
  - idempotent submission keyed per project with request digests (`1404-1422`).
- **Untrusted-executor handling:**
  - per-line decoding bounds (`channel.py:51-93`);
  - result sanitization (`manager.py:2028-2097`, `3463-3549`);
  - output budgets (`_admit`);
  - the hostile executor and robustness tests (`hostile_executor.py`, `test_robustness.py`).
- **The daemon's security design:** an abstract Unix socket, HMAC mutual authentication with nonces, a token never placed in argv or the environment, a single active bridge, and handlers fenced to their bridge (`daemon.py:52-58`, `309-444`). The design is sound; only its delivery model (C-02) and the worker-side recovery shape (C-01) need work.
- **Provisioner boundary:** `provisioner.py` uses only the manager's public API (`submit`, `wait`, `cancel`, `package_admission`, `sync_package_gate`, `refresh_package_gate`). This is the model for the other extractions.
- **`SharedSync` (D79):** well encapsulated behind `up`, `down`, `write` and `unchanged_writes`, with explicit budgets. It is on the M2 path (a third workbook in its own container accessing shared data).
- **The conformance suite (D52):** it runs against a live worker through environment variables. Extend it (C-14); do not replace it.
- **The `_sibling` loader:** it already makes a multi-file sandbox payload possible (C-16).

## Questions for the owner

1. **Selected child controller (C-05):** is restricted child-kernel delegation needed for the M2 exit? If not, may it be parked behind an interface or moved to a branch? Today it accounts for about 2,200 worker lines plus run operations in the executor and 63 touch points in the manager.
2. **Startup availability:** today one unreachable environment refuses readiness for the whole pool (`manager.py:597-613`, WORKER.md). Is that the intended trade-off, or should the pool serve other environments while fencing only the unreachable one?
3. **One worker per state directory (C-09):** is that the only supported deployment? If so, a lock file lets the capture-lease and "foreign owner" logic become much simpler.
4. **Transports (C-14):** should the stdio executor remain a supported production transport, or become development and test only? Each extra transport doubles the recovery state space (16 `daemon_token` branches in the manager).
5. **Daemon loss (WORKER.md, Sandbox daemon and recovery):** any daemon crash stops and starts the whole VM, losing every session's kernels in a shared project container. This was chosen because OpenShell can hide `/proc/<pid>/environ`. Is that acceptable for M2's two workbooks sharing one container, or should an operator confirm first?
6. **WORKER.md (C-17):** may lab and PR evidence move out of the reference document into the agent reports?
