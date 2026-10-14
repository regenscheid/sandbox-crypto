# Review notes: A-orchestrator-core

Baseline: main at fa456bc (2026-10-14). Reviewed: **read in depth:** `loop.py`, `tools.py`, `cli.py`, `model.py`, `projects.py`, `plan_tools.py`, `prompt_bundle.py`, `stock_prompts.py`, `kernel_delivery.py`, and the parts of `jobs.py`, `kernel_notices.py`, `kernel_notice_receipts.py`, `kernel_notice_bindings.py`, `session_plans.py`, `worker_client.py` that the loop and the CLI touch. I also read the interfaces the loop calls in `consistency/controller.py`, `documents/mathcontroller.py` and `documents/tool.py`. **Skimmed:** the delegation family (`child_*`, `delegated_plan_tools.py`, `delegation_records.py`): headers, imports, schema and call graph only. Also skimmed: `artifacts.py`, `artifact_publisher.py`, `mathcheck.py`, `watcher_prompt.py`, `jobstate.py`, `reviews.py`, the related `docs/` pages, the plans-and-delegation spec and D81/D83/D85/D86. **Skipped:** `provision.py` beyond its wiring, the bodies of `documents/` and `consistency/` (area B), and the worker side of the run-kernel and lineage routes (area C).

**Measurements and probes.** I built an AST import graph and function-length scan (scripts in the scratchpad, `refactor-review/A/`). I ran two probes against the real modules: `probe_plan_snapshot.py` and `probe_turn_limit.py`. Targeted tests: the delegation and kernel tests (306 passed in 11 s) and the loop, CLI, plan, job and prompt tests (307 passed in 24 s).

## Summary

The core works and keeps two good disciplines: it never executes locally, and it acknowledges notices only after the run is saved. But it has grown around the run loop instead of through it.

- **Six subsystems are hand-wired.** Reviews, math, plans, jobs, document requests and kernel notices are each wired in up to four places: `loop.py` (29 `toolbox.x is None` checks), `tools.py` (10), `cli._run_main` (269 lines, a try/finally pyramid six levels deep) and `cli._keep` (ACKs). Each has its own seen/delivered sets, its own lease heartbeat and its own error handling.
- **About 45% of the core's 15,819 lines is unreachable or disproportionate.**
  - The delegation family: 4,501 lines with no production importer.
  - The kernel-notice family: 1,894 lines, plus two extra SQLite databases, for a derived advisory the tool results already carry.
  - Five prompt-provenance implementations.
  - A dead `reviews.py`.
- **There is a P1 bug on the M2 path.** The planning fence compares the whole project JSON file. When a second workbook opens in the same project, every effect tool of the running session is refused for the rest of its run (probe confirmed).

The changes that matter most:

1. Fix the plan/session fence (A-01).
2. Freeze the delegation work until the spec's open questions are answered (A-03).
3. Introduce one turn-hook (safe-point/notice-source) interface and migrate the six sources onto it (A-02).
4. Move the composition root out of `cli.py` into a `Harness` the CLI and a GUI chat can both call (A-05, A-06).
5. Consolidate state into one control store with a public transaction API, with projects and sessions moved into it (A-07).

## Findings

### A-01: The planning fence fails on any change to the project file, so a second workbook disables the first one's tools
- Where:
  - `projects.py:146-157` (`SessionSnapshot.matches`: whole-file inode, size, mtime, ctime and SHA-256)
  - `plan_tools.py:248-272` (`_owned` sets `invalid=True` permanently)
  - `plan_tools.py:332-345` (`check_dispatch`)
  - the same pattern in `kernel_delivery.py:249-272` (`_control`) and `child_attempts.py:651`
- Category: correctness
- Priority: P1
- Evidence:
  - Every `--state-dir` run gets plan tools (`cli.py:314-315`).
  - `probe_plan_snapshot.py` created project p1, opened session `a`, built `PlanTools` and passed `check_dispatch("shell")`. It then opened session `b` in the same project. After that, every effect tool call of `a` fails with `policy_denied: planning session registry changed; start a new run`, and the failure is permanent for the run.
  - The same happens on `project revise`, on closing or reopening any other session, and on a baseline write for another session.
  - Kernel notices degrade silently for the same reason: `_observe` returns `None`.
  - This hits the M2 exit scenario directly: "two workbooks share the project container".
- Proposal:
  - Short term: compare only the run's own `SessionRecord` (session_id, environment_id, dedicated, opened_at, closed_at, kernel_notice_lifetime) and the project's policy, not file bytes. Re-read under the existing lock instead of holding descriptors.
  - Long term: sessions become rows in the control store (A-07), so the check is one `SELECT` inside the same `BEGIN IMMEDIATE` as the plan change.
- Effort: S (short-term fix); the long-term fix is part of A-07.
- Risk and verification:
  - `test_plan_tools.py` covers the existing fence. Some of its tests deliberately rewrite the file; they need to rewrite *this* session to keep the refusal.
  - Add first: two sessions in one project, B opens while A runs, A's `shell` is still allowed. Closing and reopening A still refuses. A policy revision still refuses, if that is the intended rule (owner question 7).
- Depends on / enables: enables the M2 multi-workbook fixture. Simplified further by A-07.

### A-02: Wire subsystems into the tool loop through one turn-hook interface
- Where:
  - `loop.py:111-174` (`_requests`, `_reviews`, `_plans`)
  - `loop.py:177-351` (`_run_task`, 175 lines, 34 branches)
  - `loop.py:371-382` (four lease contexts) and `loop.py:419-424`
  - `tools.py:338-384` (job and document notices attached to tool results)
  - `cli.py:285-311` (plan and kernel lifetimes owned by the CLI instead)
  - `cli.py:466-503` (four per-source ACK blocks, each opening its own `DocumentStore`)
  - `projects.py:186-195` (per-source ID fields in `RunRecord`)
- Category: architecture
- Priority: P1. The ledger, skills, user chat messages and the delegation handbacks would each become a seventh to tenth hand-wired source.
- Evidence:
  - **Duplicated blocks.** The job-notice block is duplicated verbatim (`loop.py:189-198` and `252-262`). The plan recheck is duplicated too (`242-249` and `323-331`).
  - **Seen/delivered bookkeeping repeated per source.** `reviews.seen/delivered` and `math_checks.seen/delivered`. Documents keep `request_seen/request_delivered`, jobs keep `seen` and copy it into `transcript.job_notice_ids` after every model call (`loop.py:222-223`), and kernels keep `_injected/delivered`.
  - **Five independent heartbeat loops with different failure behavior.** In reviews (`consistency/controller.py:582-596`) and jobs (`jobs.py:226-247`), the first renewal exception kills the heartbeat and is re-raised at exit. In jobs, that skips `end_run`, leaving a dangling lease row. Documents retry (`documents/tool.py:266-277`). Math gathers with `return_exceptions`. Kernel notices stop silently (`kernel_delivery.py:419-438`).
  - **Lifecycle owners differ by source.** Jobs, reviews, math and requests are owned by `run_task`. Plans and kernel notices are owned by `cli._run_main`.
  - **Bug: a last-turn job notice throws the answer away** (`loop.py:252-262`, no `turn + 1 < max_turns` guard). With `max_turns=1`, a job ending at the closing boundary appends a user message the model never sees, sets `final_text=None` and `stopped="turn_limit"`, and the CLI exits 3. Reproduced by `probe_turn_limit.py`.
  - **A blocking SQLite read runs on the event loop**: `toolbox.reviews.store.config(...)` at `loop.py:273`.
  - **Job notices cut the end off tool results.** Attaching them halves the result with `text[: len(text)//2]` (`tools.py:379-381`), which drops the tail, where tracebacks usually are.
- Proposal: a `hooks.py` module, with the loop the only place that knows the delivery rules.

  ```python
  Point = Literal["start", "turn", "tool"]


  @dataclass(frozen=True)
  class Notice:
      source: str  # "requests" | "plans" | "reviews" | "math" | "jobs" | "kernels" | "user"
      ids: tuple[
          str, ...
      ]  # promoted to delivered only after model.complete returns; ACKed after save
      framing: str  # versioned constant from prompts/notices.py, e.g. "jobs-1"
      payload: JsonValue  # untrusted data, dumped after the framing
      attach: bool = False  # merge into the current tool result instead of a user message


  @dataclass(frozen=True)
  class Closing:
      action: Literal["accept", "continue", "respond", "stop"] = "accept"
      notices: tuple[
          Notice, ...
      ] = ()  # continue = new input (not counted); respond = counted response round
      stopped: str | None = None  # for "stop", e.g. "user_message"


  class TurnHook(Protocol):  # a Hook base class supplies no-op defaults
      name: str
      recheck_after_accept: bool  # plans: veto again after other hooks' closing awaits

      def tools(self) -> Sequence[ToolEntry]: ...  # tool group contributed (A-11)
      def lease(
          self,
      ) -> AbstractAsyncContextManager[None]: ...  # built on one shared Lease helper (A-12)
      async def notices(self, point: Point, run: RunView) -> Sequence[Notice]: ...
      async def model_returned(self, run: RunView) -> None: ...  # plans' read pin, kernel claim
      async def gate(self, tool: ToolEntry) -> str | None: ...  # refusal text (plans)
      async def observe_tool(self, run: RunView, record: ToolRecord) -> None: ...  # reviewer trigger
      async def closing(self, run: RunView, answer: str | None) -> Closing: ...
      async def accept(self, run: RunView) -> JsonValue | None: ...  # bound open findings; summary
      async def acknowledge(self, record: RunRecord) -> None: ...  # delivered ids only, after save
      async def aclose(self) -> None: ...
  ```

  - **The loop owns the invariants**, implemented once:
    - It keeps the offered and delivered sets for each source; offered IDs become delivered only after a successful `model.complete`.
    - It owns the turn and response-round budget.
    - It decides closings: "stop" wins; "continue" and "respond" apply only while turns remain; then it calls `accept` for every hook, then runs a final veto pass for hooks with `recheck_after_accept`.
    - It attaches notices to tool results with head-and-tail bounding.
  - **`RunRecord`** gets `delivered: dict[str, list[Identifier]]` and `summaries: dict[str, JsonValue]`. A before-validator maps the legacy `*_notice_ids`, `work_request_ids` and summary fields. Keep emitting `review_summary` and `math_summary`, or update `web/workspace/src/lib/api.ts:146-147` and `RunSection.svelte:34-37`, the only GUI readers.
  - **Hook order preserves today's order.**
    - Before the model: requests, plans, reviews, math, kernels.
    - At closing: plans, requests, jobs, reviews, math, then the plans recheck.
  - **Migration order**, one PR per step, each behavior-preserving except where noted:
    0. **Characterization tests.** Write tests with a scripted model and fake sources for all six, covering: a normal answer; a job ending at closing; a review response round; a USER plan edit mid-run; a model failure (offered but undelivered IDs must not be ACKed); and the turn limit at closing. Snapshot the exact message sequence, the `RunRecord` ID fields and the ACK calls.
    1. **Driver and adapters.** Add `hooks.py` and a driver that runs a hook list. Thin adapters wrap today's controllers. Remove the two duplicate blocks. Fix the last-turn job bug in its own commit.
    2. **Requests, then jobs.** Move their ACKs from `cli._keep` into `acknowledge`.
    3. **Math.**
    4. **Reviews** (the observer, terminal summary, response rounds and bound).
    5. **Plans** (`gate`, `model_returned`, closing veto).
    6. **Kernel notices**, replaced per A-04.
    7. **Cleanup.** Remove the per-source attributes from `Toolbox` and `Transcript`. `cli._keep` becomes "save, then `acknowledge` for each hook".
    8. **New `user` hook** for messages written while a run is in progress (D83's "resumes when the user writes"; needed by A-06).
- Effort: L in total (steps 1–5 are each S–M).
- Risk and verification:
  - The safety rule is ordering: ACK after save, nothing delivered on a failed call. It is covered today by `test_failed_runs.py`, `test_jobs.py`, `test_document_annotations.py`, `test_consistency.py` and `test_plan_tools.py`.
  - 14 test sites touch `.seen`/`.delivered` internals and need rewriting against `RunRecord.delivered`.
  - Step 0 must land first.
- Depends on / enables: A-04, A-05, A-06, A-11 and A-12. The ledger and skills add hooks instead of editing `loop.py`.

### A-03: Freeze the delegation family; nothing in production can reach it
- Where:
  - `child_attempts.py` (1,187 lines), `child_controller.py` (768), `child_tools.py` (579), `child_prompt.py` (197), `delegated_plan_tools.py` (747) and `delegation_records.py` (1,023): 4,501 lines in total.
  - `worker_client.py:442-677` (run-kernel client, ~235 lines). `run_events`, `observe_run` and `restart_run` have no callers at all.
  - `contracts/run_api.py`, `run_cells.py` and `run_kernels.py` (567 lines).
  - The worker's `run_api.py`, `run_kernels.py`, `run_dispatch.py` and `run_kernel_state.py` (2,202 lines, area C).
- Category: dead code / over-engineering
- Priority: P1 for the freeze decision; P2 for removal.
- Evidence:
  - Import graph: no source module imports `child_controller`. The other five modules are imported only from inside the family.
  - Tests: 3,392 lines (`test_child_attempts`, `test_child_controller`, `test_delegation_records`, `test_delegated_plan_tools`). Docs: five pages (660 lines).
  - It is a **second tool loop** (`child_controller.run/drive`, 160+115 lines) with a **second dispatcher** (`child_tools`), a separate worker "run-kernel" API and its own prompt provenance (`child_prompt.py`). None of the main loop's notices, plan fence or ACK rules apply to it.
  - All six modules disable `reportPrivateUsage` and reach into other modules' privates (`child_attempts._json`, `_checked` and the `projects._locked` / `SessionSnapshot` descriptors).
  - It rests on the still-draft plans-and-delegation spec. Open question 3 (depth, and four concurrent runs) and open question 4 (whether delegated runs get kernels of their own) decide whether the run-kernel transport is needed at all.
  - Its schema already adds five tables to the production `control.sqlite3` (`delegation_record_*`, `child_attempt_*`).
- Proposal:
  - Now: decide to freeze. Move the six modules and their tests to an archive tag or branch (`archive/delegation-foundations-fa456bc`), and move their docs under `.agents/docs/archive/`.
  - If they stay in the tree, put them in `orchestrator/_incubating/` with an import-lint rule (no production import, no new features) and keep their tables out of the production schema until wired.
  - When delegation resumes, build it as a *role* of the one loop (MAIN, delegated, reviewer), with a role-specific tool table and hook set, not a second loop.
- Effort: S to freeze or archive; M to remove, including the worker and contracts side with area C.
- Risk and verification:
  - Lab34 evidence becomes historical.
  - Removing it shrinks `session_plans`' delegated-authority branches only if the owner also defers PD-12; keep those, they are small.
  - The full suite drops about 180 cases.
- Depends on / enables: simplifies A-01, A-07 and A-14. Needs owner question 1.

### A-04: Replace the kernel-notice inbox with a stateless pre-turn check
- Where:
  - `kernel_notices.py` (742 lines), `kernel_notice_bindings.py` (396), `kernel_notice_receipts.py` (232) and `kernel_delivery.py` (524)
  - `projects.py:463-548` (`kernel_notice_baseline`, a second, fsyncing write path for the project JSON)
  - `projects.py:203-235` (`RunRecord` validation)
  - `cli.py:136-157`, `285-294` and `411-428` (ownership, save lane, ACK)
  - `loop.py:210-219`
  - the existing in-band disclosure at `tools.py:736-750`
- Category: over-engineering / duplication
- Priority: P2
- Evidence:
  - **What is delivered.** It tells MAIN, before a model call, that a kernel started before the latest package revision (CP-12). Every execution result already reports the same fact (`kernel_revision_notice` from `running_kernels[].revision_status == "predates"`, `tools.py:740-750`), and the GUI panel shows it.
  - **What the inbox adds.** At-least-once durable delivery with claims, leases and a 10 s heartbeat. It also adds two thread lanes, file-identity checks with `fstat`/`O_NOFOLLOW`, an HMAC "binding" of the worker URL and bearer token, a 32-byte key file, and two SQLite files: `kernel-notices.sqlite3` and `kernel-notice-bindings.sqlite3`. The extra files go against D85's "one database for the control application".
  - **It reaches into WorkerClient privates.** It reads `_owns_session` and `_headers["Authorization"]` to clone the client (`kernel_delivery.py:162-167`, `197-201`, `228-232`).
  - **Its docstring is stale.** `kernel_notices.py:2` still says "transport and run delivery are unwired".
  - **Size.** Tests: 1,860 lines (`monkeypatch` 34× in `test_kernel_delivery.py`, much of it patching CLI module names). Docs: three pages, 388 lines.
- Proposal:
  - The notice is derived state that can be recomputed from the worker at any time, so telling it twice is harmless and it needs no durable inbox or ACK.
  - Implement a `KernelsHook` (A-02) whose `notices("turn")` calls `client.observe_kernels(context)`, compares each kernel's start revision with the current revision, and emits one short versioned notice per (kernel, revision) not yet told in this run. State is held in memory.
  - Delete the four modules, the baseline write path and the CLI ownership plumbing.
  - Keep a tolerant reader for legacy `RunRecord.kernel_notice_delivery`: accept and preserve it, never require it.
- Effort: M
- Risk and verification:
  - Cross-session staleness is still delivered at the next turn. What is lost is the guarantee across a crash between injection and save, which is harmless because the next run recomputes it.
  - Add a test: session B installs a package; A's next turn gets one notice; it does not repeat within the run.
- Depends on / enables: A-02. Simplifies A-07 (two fewer databases and no second JSON write path) and A-14. Owner question 2.

### A-05: `cli.py` is the composition root; extract a `Harness`
- Where:
  - `cli.py:160-428` (`_run_main`: 269 lines, 56 branches; the pyramid at `337-370`)
  - `cli.py:431-514` (`_keep`)
  - `cli.py:737-890` (`main`: 154 lines, of which `831-885` validates flag combinations)
- Category: architecture / complexity
- Priority: P2
- Evidence:
  - The 890-line file holds: argument parsing; cross-flag validation; model, watcher and reviewer construction; store construction; the assembly of six controllers in a required order (kernel baseline "before other MAIN controllers pin registry snapshots", `cli.py:291-294`); a six-level try/finally close pyramid; three `Interruption` objects (`cli.py:139,170`, `loop.py:367`); save-then-ACK for five sources; and the user's document and project commands.
  - `ChatModel` has no `close()`, so the CLI checks `isinstance(..., OpenAICompatibleModel)` three times.
  - Tests reach the run path only through privates and argparse Namespaces: `cli._run` and `cli._keep` at `test_harness.py:314`, `test_kernel_delivery.py:812,825`, `test_plan_tools.py:579,702`, `test_artifact_publication.py:375`, `test_consistency.py:1156`, `test_document_annotations.py:400`, `test_stock_prompts.py:293`. They also monkeypatch module globals (`cli.WorkerClient`, `cli.OpenAICompatibleModel`, `cli.KernelDeliveryController`).
- Proposal:
  - Add `harness.py`:
    - `HarnessConfig` (pydantic; the cross-flag rules become validators).
    - `Harness` (process lifetime: worker client, role models, control store; one `AsyncExitStack`-based `Resources` helper that keeps the first interruption).
    - `RunRequest(project, session, text, template)`.
    - `Harness.run(request, sink) -> RunRecord`, which assembles hooks (A-02) and tool groups (A-11), then saves and ACKs.
  - `cli.py` keeps only `argparse → HarnessConfig` and printing (a stdout `RunSink`). Move the document and project commands to `cli_documents.py`.
  - Tests construct `Harness` with injected factories instead of patching module globals.
- Effort: M
- Risk and verification:
  - The exit codes (0/1/2/3) and the first-interruption behavior (`test_kernel_delivery` cancellation cases) must be kept.
  - Port `test_harness.py`'s CLI cases to `Harness` first.
- Depends on / enables: A-02 (best done after step 1). Enables A-06.

### A-06: What a minimal GUI chat/run entry point needs from this layer
- Where: `loop.py:395-404` (every run is `[system, task]`); `cli.py:194-195` (the only progress hook prints tool calls); `projects.py:576-616` (the record is written once, at the end); `workspace/` (no run route: `reviews.py` says "Idle sessions do not start a model turn")
- Category: architecture (missing capability for M2)
- Priority: P2
- Evidence:
  - A "chat" does not exist today: each `run` is a one-shot task with no earlier turns.
  - There is no streaming or event output except `on_tool`.
  - There is no single-active-run guard per session (no exclusivity code anywhere). Two runs on one session would split notices between their leases.
  - A crash loses the whole transcript, though jobs can keep a turn open for hours (D83).
  - Workspace has no way to start a model turn.
- Proposal, the minimum:
  1. `Harness.run` (A-05) plus a `RunSink` protocol: `assistant(text)`, `tool_started/ended(call, outcome)`, `notice(source, ids)`, `ended(record)`.
  2. **Conversation context.** Build messages from the session's earlier runs: their user text and final answers, within a character budget, with tool transcripts left out. This is the place for the compaction later described in `ORCHESTRATOR-PROMPTS.md`.
  3. **A per-session run lease** (one active MAIN run per session) in the control store. A message written while a run is active goes to a `user_messages` queue that the A-02 `user` hook delivers at the next safe point.
  4. **Incremental persistence**: append `run_events` (turn, tool record, notice IDs) to the control store and finalize the `RunRecord` at the end. The GUI reads a live run from the events.
  5. **`cancel(run_id)`.** The interruption and lease release logic already exists.
  6. **Placement.** A small orchestrator run service (`sandbox-science-harness serve`) that the workspace proxies (W5), with routes such as `POST .../sessions/{s}/messages`, SSE `GET .../runs/{r}/events` and `POST .../runs/{r}/cancel`. Alternatively, a temporary in-process host inside the workspace server, which the W4 exception in CONVENTIONS does not currently allow ("does not grant the workspace … agent authority").
- Effort: M (items 1, 3, 4 and 5) plus M (item 6).
- Risk and verification: a fixture would open a GUI chat, send two messages, write while the first is running, cancel, and restart the orchestrator mid-run. That last case pairs with the M4 "closed browsers" exit checks.
- Depends on / enables: A-02 (step 8), A-05, A-07 (session table, run events), A-09. Owner questions 3–5.

### A-07: Too many stores and schema owners; give the control store a public transaction API and move projects and sessions into it
- Where:
  - `projects.py:289-676` (JSON registry and run files)
  - `projects.py:121-166` (`SessionSnapshot`)
  - `projects.py:663-676` (`_locked` and a `_write` without fsync)
  - `projects.py:463-548` (a second write path, with fsync and `O_NOFOLLOW`)
  - `documents/store.py:399,504-541` (RLock; private `_write`/`_read`)
  - `jobstate.py:37-71` (its own DDL and `ALTER TABLE`)
  - `session_plans.py:384-392`, `delegation_records.py:507-512`, `child_attempts.py:432-500`, `artifacts.py:73-121` (per-module `*_meta` version tables)
  - `kernel_notice_bindings.py:31-34`
- Category: persistence / coupling
- Priority: P2
- Evidence:
  - **The orchestrator now writes six kinds of state:**
    1. `projects/<p>.json` plus `.lock`.
    2. `runs/<p>/<s>/*.json`.
    3. `control.sqlite3`, also written by the workspace process.
    4. `kernel-notices.sqlite3`.
    5. `kernel-notice-bindings.sqlite3` with `kernel-notice.key` and its lock.
    6. A document delivery journal, wired in tests only (area B).
  - **Schema ownership is scattered.** 82 `CREATE TABLE` statements across 19 modules. Only the document schema uses `user_version`; the rest add tables on open, each with its own meta-singleton version scheme.
  - **Everyone uses the private transaction seam.** `DocumentStore._write()`/`_read()` is used 173 times in 25 files, including `workspace/plans.py`, `plan_edits.py` and `reviews.py`. This is why 11 of the 30 top-level modules disable `reportPrivateUsage`.
  - **The JSON registry sits outside SQLite.** That is the reason for the descriptor-holding `SessionSnapshot` machinery (8 modules, plus a workspace re-implementation at `workspace/plans.py:89`). It also forces a lock-ordering rule (project flock, then SQL) and two inconsistent write disciplines for the same file, and it caused A-01.
- Proposal:
  1. Split `ControlStore` out of `DocumentStore`. It owns the connection, WAL, `synchronous=FULL`, the RLock, a **public** `write()`/`read()`, and a single ordered migration list keyed by `user_version`. Each logical module registers its own DDL as a migration.
  2. Move `ProjectRecord` and `SessionRecord` into `projects` and `sessions` tables. A session's identity is then checked inside the same transaction as the plan, ledger or delegation change, and `SessionSnapshot` and the flock-then-SQL rule go away. Run records can stay write-once files for now, with the run index and events in SQL (A-06).
  3. Drop the kernel databases (A-04).
  4. Give the workspace an explicit `orchestrator.api` facade, instead of importing 15 orchestrator modules (including the agent's `JobTools`, `workspace/jobs.py:16,49`), until W5.
- Effort: L (in steps: 1 is M, 2 is M, 4 is S)
- Risk and verification:
  - Multi-process writes (harness plus workspace) must keep `BEGIN IMMEDIATE` semantics.
  - Migrating existing state directories needs a one-shot importer for `projects/*.json`.
  - `test_projects.py`, `test_session_snapshot.py`, `test_document_store.py`, the workspace plan and kernel tests, and a new migration test from a fa456bc-era state directory.
- Depends on / enables: coordinate with B (documents/store.py, consistency/store.py). Enables A-01 (long-term), A-06 and A-14.

### A-08: The prompt machinery is disproportionate in places and leaves gaps elsewhere
- Where:
  - `loop.py:28-58` (one ~400-word `SYSTEM_PROMPT` covering six topics)
  - `prompt_bundle.py:66-152` (two near-duplicate provenance classes for assembly versions 1 and 2)
  - `prompt_bundle.py:199-268` (a byte-budget walker over tool schemas)
  - `prompt_bundle.py:282-291` and `loop.py:208-209` (`verify` re-hashes all 44 tool schemas and the system prompt on every turn)
  - `prompt_bundle.py:294-411` (`assemble_main` versus `_assemble_selected`)
  - `watcher_prompt.py`, `child_prompt.py`, `consistency/prompt_bundle.py`, `workspace/repair_prompt.py`
  - notice framings at `loop.py:119-121, 140-145, 152-155, 169-171, 195, 284-289`
- Category: over-engineering / docs drift
- Priority: P2
- Evidence:
  - **Five independent provenance implementations** (MAIN 411 lines, watcher 144, child 197, reviewer 194, display repair 228; 1,174 in all), each with its own canonical-JSON and SHA helpers and digest domains. There are about 12 private `_json`/`_canonical`/`_sha` helpers across the orchestrator.
  - **It hardens against threats outside the model.** The tool specs are trusted constants in code, and the model cannot mutate the Python objects the per-turn `verify` guards.
  - **It leaves the real gaps open.**
    - The notice framings injected as user messages are unversioned and unpinned.
    - The `"sandbox-main-2"` label is not tied to the prompt text by any test (`test_pilot_guidance.py:171` checks the label only). Editing `SYSTEM_PROMPT` without bumping it records a stale version.
    - `RunRecord` refuses to *load* a record whose `messages[0]` does not match its pins (`projects.py:210-211`), so a damaged record becomes unreadable in the GUI instead of being flagged.
  - Design doc: `ORCHESTRATOR-PROMPTS.md`, 629 lines.
- Proposal:
  - A `prompts/` package:
    - `main.py` with sections (execution routing and honesty, pilots, network admission, untrusted data and work requests). Each section is included only when its tool group is offered.
    - `notices.py` with versioned framings.
    - `roles/` for the watcher, reviewer and child.
  - **One generic pin**: `PromptPin(id, version, sha256, bytes)` for each section, plus `tools_sha256` and tool names. Compute it the same way for every role.
  - Add one golden test that maps each version to its SHA-256.
  - Drop the per-turn `verify` and the schema budget walker.
  - Read legacy bundle-1 and bundle-2 provenance tolerantly: keep it, and flag a mismatch rather than refusing to load.
  - Put the canonical JSON helper in `contracts` (it already has `document_exports.canonical`).
- Effort: M
- Risk and verification: `test_prompt_bundle.py`, `test_stock_prompts.py`, `test_pilot_guidance.py`, `test_prompt_inputs.py`, `test_reviewer_prompt_provenance.py` and `test_watcher_prompt.py` (about 2,060 lines) pin today's formats. Keep fixtures of historical records (`BUILTIN_1_RECORD`) as read-compatibility tests.
- Depends on / enables: A-02 (notice framings move with the hooks). Owner question 6.

### A-09: The main loop is not ready for a real model
- Where:
  - `loop.py:215` (no context budget)
  - `loop.py:415-418` (any exception becomes a phase label with no log)
  - `cli.py:381-382` (prints only `run failed during model`)
  - `model.py:100-138` (no retry)
  - compare `consistency/runner.py:300-330`
- Category: correctness / other (operability)
- Priority: P2. It should land before the first real-model run, which is an M2 gap.
- Evidence:
  - The reviewer loop checks `max_input_chars`, reserves a token estimate and records `input_tokens`/`output_tokens`.
  - MAIN does none of that. Thirty turns of tool results of up to 20,000 characters each, plus notices, can exceed common context windows.
  - `ModelTurn.input_tokens` is dropped. A 429, a 5xx or a context-length error from LiteLLM ends the run, and only the phase name is shown; the cause is in no log. `loop.py` has no logger.
- Proposal:
  - Record usage per turn in `Transcript` and `RunRecord`.
  - Add a context budget that stops cleanly with a `context_limit` status (compaction comes later).
  - Retry 429, 502, 503 and 504 with bounded backoff in `OpenAICompatibleModel`.
  - Log the exception type and message to the logger (stderr), keeping the rule that the record holds no exception text.
- Effort: S–M
- Risk and verification: extend `test_harness.py` adapter tests (status codes) and `test_failed_runs.py` (the record still holds no exception text).
- Depends on / enables: A-06.

### A-10: The last-turn job notice discards the final answer
- Where: `loop.py:252-262`
- Category: correctness
- Priority: P3. Rare, but a GUI chat would show a lost answer.
- Evidence: see A-02. `probe_turn_limit.py` returns `stopped=turn_limit`, `final_text=None`, and a last message the model never saw.
- Proposal: guard with `turn + 1 < max_turns`, as requests and plans already do, or let A-02's generic "continue only while turns remain" rule cover it.
- Effort: S
- Risk and verification: add the probe as a test in `test_jobs.py`.
- Depends on / enables: A-02 step 1.

### A-11: Make tool dispatch table-driven and put effect classes on the tool entry
- Where: `tools.py:273-336` (spec assembly), `tools.py:386-455` (the `_call` if-chain: plans, then JSON parse, then reviews, documents and jobs, then a handler dict, then a `publish` special case at `447-448`), `plan_tools.py:59-104` (separate `READ_TOOLS`, `CANCEL_TOOLS` and `EFFECT_TOOLS` name sets)
- Category: coupling / duplication
- Priority: P3
- Evidence:
  - 44 tools in all.
  - Adding a tool means editing the spec list, the dispatch chain and plan_tools' classification. The last is enforced by `test_plan_tools.py:248` and fails closed, which is good, but it is a parallel registry.
  - Results are bounded inconsistently: plan and document outcomes are not passed through `_bound`, review outcomes are.
- Proposal:
  - `ToolEntry(spec, handler, effect: Literal["read", "cancel", "effect"], bound: bool)`.
  - Groups (execution, files, pdf, admission, provisioning, jobs, documents, reviews, plans) are contributed by `Toolbox` and by hooks (A-02).
  - The plan gate reads `entry.effect`.
  - `Toolbox.call` becomes a lookup, then validation, then the handler, then error mapping.
- Effort: S–M
- Risk and verification: `test_harness.py` (refusals, unknown tools) and `test_plan_tools.py` (classification coverage becomes a check that every entry has an effect).
- Depends on / enables: A-02.

### A-12: One async/thread toolkit
- Where: `documents/mathjobs.py:48-67` (`owned`, `threaded`, imported by the loop, plans, CLI and consistency); private single-thread executors at `kernel_delivery.py:67-128`, `artifact_publisher.py:57`, `documents/delivery.py:174` and `consistency/artifacts.py:352`; raw `asyncio.to_thread` in five heartbeat loops (see A-02)
- Category: concurrency
- Priority: P3
- Evidence: there are four patterns for off-loop SQLite work with different cancellation semantics. `jobs.py:242-243` documents the hazard of cancelling `to_thread`. The heartbeats handle errors inconsistently, and one path skips `end_run` (A-02).
- Proposal: `orchestrator/aio.py` holding:
  - `owned` and `threaded`.
  - `Lane`: a serial executor that drains on close.
  - `Lease(renew, interval, ttl)`: retry until the TTL, never re-raise at exit, always run release.

  Hooks use `Lease`.
- Effort: S
- Risk and verification: lease and heartbeat tests in `test_jobs.py`, `test_consistency.py` and `test_document_annotations.py`.
- Depends on / enables: A-02.

### A-13: Dead code and stale module docstrings
- Where:
  - `reviews.py` (88 lines; its docstring says "historical trigger helper"; imported only by `test_reviews.py` and `test_document_tool.py`)
  - `worker_client.py` `run_events`, `observe_run` and `restart_run` (no callers)
  - `kernel_notices.py:2` ("transport and run delivery are unwired"; they are wired)
  - `session_plans.py:2` ("without runtime wiring"; it is wired through `plan_tools` and the CLI)
- Category: dead code / docs drift
- Priority: P3
- Evidence: import graph (scratchpad `imports.py`).
- Proposal: delete `reviews.py` and its tests, or move the window logic into `consistency/` if CR-10 still needs it. Correct the docstrings.
- Effort: S
- Risk and verification: none beyond the tests named.
- Depends on / enables: none.

### A-14: Cross-object private access is the norm in the stateful modules
- Where:
  - 11 of 30 top-level modules have `# pyright: reportPrivateUsage=false` (artifacts, plan_tools, projects, session_plans, kernel_delivery and the six delegation modules)
  - 40 `x.y._z` accesses (`session_plans` 10, `artifacts` 8, `child_attempts` 7)
  - `plan_tools.py:252` (`projects._locked`)
  - `kernel_delivery.py:163-166, 199-200, 229-231` (`WorkerClient` credentials)
- Category: coupling
- Priority: P3. It is a symptom; the causes are A-03, A-04 and A-07.
- Evidence: see above.
- Proposal:
  - Most of it disappears with a public `ControlStore` API (A-07), the delegation freeze (A-03) and the kernel replacement (A-04).
  - After that, ban the file-wide disable with a ruff/CI grep, and allow only line-level ignores with a reason.
- Effort: S, after the others.
- Risk and verification: pyright strict.
- Depends on / enables: A-03, A-04, A-07.

## Keep

- **No local execution path.** Every research tool is a worker call (`tools.py:459-696`). Errors become tool results and never end the task (`tools.py:347-354`).
- **Idempotent operation IDs.** `operation_id` digests the whole call (`tools.py:259-270`), so a replayed call returns the earlier result and is marked `replayed`.
- **Bounded tool results.** The halving bounds keep head and tail; `_summary`, `_list` and `format_pdf` give the model citable PDF lines.
- **`model.py`.** It is small and provider-neutral, with bounded reads, no redirects and a strict completion parser. `ScriptedModel` makes the harness testable without a gateway.
- **Save-then-ACK, at-least-once delivery.** Notices count as delivered only after a successful model call, and ACKs happen only after a durable save (fsync, link, directory fsync in `projects.py:576-616`). Keep the rule; centralize it (A-02).
- **Fail-closed planning dispatch** with a coverage test. **Untrusted-data framing** on every injected notice. **The isolation-tier refusal** in the CLI (`cli.py:91-110, 179-187`).
- **Session plans' data model**: immutable versions, CAS, attributed progress. It matches D86 and PD-1 to PD-10. Keep it, but do not extend it before the spec questions are answered.
- **Tests run against an in-process worker** (`test_harness.py`) rather than mocks. The core tests are fast (about 300 cases in 24 s).

## Questions for the owner

1. **Delegation.** Archive the 4,500-line delegation family on a tag and remove it from main, or keep it in-tree but frozen? Before resuming, which answers do you want to spec open questions 3 (limits) and 4 (kernels of their own per delegated run)?
2. **Kernel notices.** Is "told at the start of the next turn that a kernel predates a package change" worth durable at-least-once delivery? The alternative is a stateless pre-turn check plus the existing in-band disclosure in every execution result.
3. **GUI chat placement.** Should chat runs execute in a small orchestrator run service that the workspace proxies (W5)? Or should the W4 exception be extended, temporarily, to let the workspace server host agent runs?
4. **Conversation context.** What should a new chat message in a session carry: earlier tasks and final answers only (proposed), or full transcripts with compaction?
5. **Runs per session.** Should there be one active MAIN run per session, with messages written meanwhile queued for the next safe point (proposed)?
6. **Provenance.** Are per-section content pins in the run record enough? That would mean dropping bundle-level self-verification and versioned assembly formats, and reading damaged legacy records tolerantly instead of refusing them.
7. **Planning fence.** Should a project policy revision during a run block that run's effect tools (today it does), or only changes to the run's own session?
8. **Plan approval.** Plan approval (PD-6) is still an open question, but an `awaiting_approval` state already exists with no USER approval route. Remove it until the default is decided?
