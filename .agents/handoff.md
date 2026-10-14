# Handoff

Updated 2026-10-14, main `96380a4`. For the next agent: where things stand, how work is done, and what is next. This file is replaced, not appended to, and stays under 8 KB. Read it with [STATUS.md](STATUS.md), the [refactoring plan](refactor-plan/PLAN.md) and [CONVENTIONS.md](CONVENTIONS.md).

## Where things stand

- **The code:** M1 passed on babbage. Much of M2 is built (see [STATUS](STATUS.md#what-works-today)), but the GUI cannot yet start or chat with a run, no real model has run, and the ledger, board and PDF viewer are missing.
- **The refactoring plan:** Phases 0 and 1 are done, apart from F-03's CI tiers. Phase 2 runs in five lanes; lane C is done and O, S, W and G are partway. The plan's [progress section](refactor-plan/PLAN.md#progress) lists each finding with its PR, and [PLAN-CHANGES](PLAN-CHANGES.md) records the decisions made on the way.
- **Open PRs and tasks in flight:** lane O step 5 (#165, prompt pins, A-08; #166, dead code and `awaiting_approval`, A-13, A-14); lane W's C-03 stages 2 and 3 (#163, #164); lane G step 4 (E-14 and the small items); lane S step 4 (B-14, B-17/E-12); and the F-03 CI tiers, which also fix the kernel-start flake and the leaked daemons ([known issues](STATUS.md#known-issues)).
- **Lab state:** no lab, VM or private state is running.
- **Heartbeat:** a conversation task, *Retry stalled orchestrator*, checks every five minutes and resumes a stalled session with the same model, leaving active work alone.

## What a new agent needs to know about the code now

- **Loop:** features plug in as turn hooks (`orchestrator/hooks.py`); the loop owns delivery, response rounds and closing. A run's delivered notice IDs and hook summaries are in `RunRecord.delivered` and `RunRecord.summaries`. Tools are `ToolEntry` rows with an effect class; an unclassified tool is an `effect`. `Harness` (`orchestrator/harness.py`) is what the CLI builds and what the run service will build.
- **Control store:** `orchestrator/control/` holds `ControlStore` (`read()`/`write()`) and the migration list. To change the schema, append a step to `MIGRATIONS`; never edit `baseline.sql` or a shipped step. Projects, sessions and runs live there; the project lock files stay until B-17.
- **Worker:** `manager.py` is a facade being split (C-03): `usability.py` and `connections.py` are out; publication and the kernel registry are in open PRs. Worker tests go through `tests/harness.py`; only `ManagerView` reads the manager's private state.
- **Contracts:** any change to what crosses the worker API raises `PROTOCOL_VERSION` (now 3); see [compatibility](../docs/COMPATIBILITY.md). Shared primitives are in `contracts/model.py`.
- **Workspace:** routes are plain handlers with one error mapping (`workspace/errors.py`); the browser uses `lib/http.ts`, `lib/poller.ts` and `lib/mutation.ts`. The typed answers come from `scripts/generate_api_types.py`; rerun it when a response model changes.

## How work is done now

- **Follow the plan and its [working rules](CONVENTIONS.md#working-rules),** and put them in every brief: production callers only, PRs of about 800 product lines at most, the [threat model](../docs/THREAT-MODEL.md) as the bar for defenses, characterization tests first, evidence out of git, no new subsystem in `loop.py`, `manager.py` or `create_app`.
- **Re-check each finding** against current `origin/main` before acting on it. The notes were written against `6e9608a`, and much has moved.
- **Parallel lanes:** tasks own disjoint files. One agent per task, in its own worktree, with a written brief. Stacked PRs say so and name their base.
- **Tracking files:** only the agent coordinating a wave edits STATUS, the handoff and PLAN-CHANGES. Other PRs put their summary in the PR description and update the reference docs and BACKLOG items their change affects.
- **Review:** a separate read-only agent reviews each substantial code PR, until nothing critical, high or medium remains.
- **Merging:** green PRs are merged by the coordinating agent, as the owner asked. After a merge, fast-forward the main checkout and run `uv sync --locked`, then remove clean merged worktrees.

## What is next

1. Land the open PRs and the tasks in flight above.
2. F-03: the fast and full CI tiers. Phase 1's exit needs 50 repeated runs without a flake, so the kernel-start race and the leaked daemons come first.
3. Phase 2's remaining steps, in each lane's order: lane S (B-10, B-05 and B-03, then the long functions); lane W (C-03's remaining stages, C-06 to C-09, C-02, C-16); lane G (E-15, E-17, E-19, E-20). Then check Phase 2's exit against the plan.
4. Record in the plans-and-delegation spec the owner's answers of 2026-10-14 to its questions 3 and 4 when delegation is rebuilt (Phase 4 item 7).

## Gotchas

- **The lab's `env.sh` breaks `gh`:** it points `XDG_CONFIG_HOME` at the lab. Use another shell.
- **Watching CI too early:** `gh pr checks N --watch` right after `gh pr create` can exit with "no checks reported". Wait a few seconds.
- **Parallel test runs:** kernel tests can fail under `-n 8` or more (the known flake), and `--count` runs need `-o tmp_path_retention_policy=failed`.
- **The main checkout is live during a lab:** the lab's cache and middleware run from its editable install. Use worktrees.
- **AppArmor:** babbage restricts unprivileged user namespaces; Claude Desktop's shells are exempt, so qualification checks run through `systemd-run --user`.
- **The forward unit restarts forever;** `labs/lab_down.sh` stops it. Lab scripts run only at the commit their report names.
