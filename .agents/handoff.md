# Handoff

Updated 2026-10-14, main `3cc20f6`. For the next agent: where things stand, how work is done, and what is next. This file is replaced, not appended to, and stays under 8 KB. Read it with [STATUS.md](STATUS.md), the [refactoring plan](refactor-plan/PLAN.md) and [CONVENTIONS.md](CONVENTIONS.md).

## Where things stand

- **The code:** M1 passed on babbage. Much of M2 is built (see [STATUS](STATUS.md#what-works-today)), but the GUI cannot yet start or chat with a run, no real model has run, and the ledger, board and PDF viewer are missing.
- **The refactoring plan:** six reviews of main at `6e9608a` found 97 findings ([notes](refactor-plan/notes/)). The [plan](refactor-plan/PLAN.md) orders them in five phases. The owner answered its scope questions on 2026-10-14 and asked for autonomous work through the plan, with its recommendations adopted; [PLAN-CHANGES](PLAN-CHANGES.md) records each decision.
- **Phase 0 is in progress** in parallel lanes, each in its own worktree with its own PR. Merged: A-01 and A-10 (#125). In flight: removing the unwired delegation and document-sync code (A-03, C-05, D-10, B-11, C-18, F-07; kept on the tags `archive/delegation-2026-10` and `archive/document-sync-2026-10`), E-02 (#124), the web formatter E-16 (#127), the tracking files and decision records (#126), and the CI guards and dead suppressions (#128). [STATUS](STATUS.md#refactoring-plan) keeps the list current.
- **Raw lab evidence** left git on 2026-10-14 and history was rewritten (#123). The pre-rewrite repository and the evidence are kept privately on babbage; reports name archived files by SHA-256.
- **Lab state:** no lab, VM or private state is running.
- **Heartbeat:** a conversation task, *Retry stalled orchestrator*, was created at the owner's request on 2026-10-13. It checks every five minutes and resumes a stalled session with the same model, leaving active work alone.

## How work is done now

- **Follow the plan and its [working rules](CONVENTIONS.md#working-rules).** In short: no production module without a production caller; PRs of at most about 800 changed lines of product code; defenses only against what the [threat model](../docs/THREAT-MODEL.md) covers; characterization tests through public APIs before a refactor; evidence out of git; short tracking files; no new subsystem in `loop.py`, `manager.py` or the workspace's `create_app`; no code on a spec whose open questions affect it.
- **Re-check each finding** against current `origin/main` before acting on it. The notes were written against `6e9608a`.
- **Parallel lanes:** within a phase, tasks own disjoint files. One agent per task, in a worktree of its own, with a written brief. Agents report in their final message; follow-ups go to the same agent.
- **Tracking files:** only the agent coordinating a wave edits STATUS, the handoff and PLAN-CHANGES. Other PRs put their summary in the PR description and update the reference docs and BACKLOG items their change affects.
- **Review:** a separate agent reviews each substantial code PR without modifying the repository, reporting findings by severity with a file, a scenario and a fix; repeat until nothing critical, high or medium remains.
- **Merging:** green PRs are merged by the coordinating agent, as the owner asked ([conventions](CONVENTIONS.md#how-work-is-done)). After a merge, fast-forward the main checkout and run `uv sync --locked`, then remove clean merged worktrees.

## What is next

1. Finish Phase 0: merge each lane's PR once CI is green and a review is clear; then the worker bug fixes C-01, C-10, C-11 and C-15. Then check the plan's Phase 0 exit: the bug tests pass, the unwired modules are gone and the suite still passes, STATUS is under 10 KB, the size guard runs in CI.
2. Start Phase 1 (test seams): F-04 steps 1–3 (a `Clock` protocol, timeouts as parameters, waiting on events), F-06 and C-13 (shared test support, one worker harness), F-05 (characterization tests for the loop, the worker manager and the workspace routes), C-14 and C-12, then F-03's CI tiers.
3. Record in the plans-and-delegation spec the owner's answers of 2026-10-14 to its questions 3 and 4 (the plan, Phase 4 item 7) when delegation is rebuilt.

## Gotchas

- **The lab's `env.sh` breaks `gh`:** it points `XDG_CONFIG_HOME` at the lab. Use another shell.
- **Watching CI too early:** `gh pr checks N --watch` right after `gh pr create` can exit with "no checks reported". Wait a few seconds.
- **Agent transcripts are huge.** Never read an agent's output file whole.
- **The main checkout is live during a lab:** the lab's cache and middleware run from its editable install. Use worktrees.
- **AppArmor:** babbage restricts unprivileged user namespaces; Claude Desktop's shells are exempt, so qualification checks run through `systemd-run --user`.
- **The forward unit restarts forever;** `labs/lab_down.sh` stops it.
- **The scratchpad is per session.** Lab scripts live in [`labs/`](labs/README.md).
- **A memory directory is loaded each session,** with notes on babbage's AppArmor, decision requests and autonomous merging.
