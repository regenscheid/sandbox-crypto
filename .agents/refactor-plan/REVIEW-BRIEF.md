# Codebase review brief

> **Baseline after the history rewrite.** These reviews ran on main `fa456bc`. On 2026-10-14 raw lab evidence was removed from all history, so that commit is now `6e9608a`. The rewrite changed no code, so every `file:line` cited here still holds.

A read-only review of Sandbox Science at main `fa456bc` (2026-10-14), to plan revisions and refactors. Another agent is developing in this repository right now; nothing in this review may touch its work.

## Rules

- **Read-only.** Work only in the worktree `~/Git_Projects/sandbox-crypto/.claude/worktrees/refactor-plan`. Do not edit code there or anywhere else. The one file you write is your area's notes file under `.agents/refactor-plan/notes/` in that worktree. Never touch `~/Git_Projects/sandbox-crypto` itself (the main checkout) or `~/.codex/worktrees/`.
- **No side effects.** No commits, pushes, PRs, labs, OpenShell, Docker, systemd units or network access. You may run targeted tests with `.venv/bin/python -m pytest <paths> -q` from the worktree; do not run the whole suite (it was run on this baseline: 11,222 passed, 44 opt-in skips, 6.5 minutes). ruff and pyright are clean on this baseline.
- **Scratch files** go in `<session scratchpad> area letter>/`.

## Context

- Read first, briefly: `README.md`, `docs/ARCHITECTURE.md`, `.agents/CONVENTIONS.md`, `docs/THREAT-MODEL.md`, the decision register `docs/DECISIONS.md` (skim), and the roadmap's M2 exit in `docs/ROADMAP.md`: "two workbooks share the project container with distinct kernels; a third uses its own; the complete paper → Sage → document → user feedback → automatic consistency review path works locally".
- About 50 PRs (#69 to #122) landed in three days, mostly agent-written, about 180,000 lines including evidence files. The owner is concerned that work went deep on components (provenance records, kernel notices, delegation internals) before the M2 exit path exists (no ledger or board, no skills, no chat from the GUI, no PDF viewer, no real-model run), and that the code is becoming hard to review.
- Leads already found, to verify and extend rather than rediscover:
  - `orchestrator/loop.py` `_run_task` (~175 lines) special-cases six subsystems (reviews, math checks, plans, jobs, documents, kernel notices) with `toolbox.x is not None` checks and hand-maintained seen/delivered bookkeeping; the job-notice block is duplicated. Proposed: one safe-point / notice-source interface.
  - Production modules imported only by tests: `orchestrator/child_attempts.py`, `delegated_plan_tools.py`, `documents/worker_source.py`, and `documents/delivery.py` (one reference). PR #114 called delegation work "unwired prerequisites" built on the still-draft plans-and-delegation spec.
  - 54 of 3,240 functions exceed 100 lines; worst: `consistency/store.py` `dispatch` (356 lines, 55 branches, raw SQL with magic numbers), `documents/annotations.py` `change` (268 lines, 67 branches), `cli.py` `_run_main`, `worker/executor.py` `execute`, `worker/manager.py` `_recover`, `worker/shared_sync.py` `_up`.
  - 26 files disable pyright's private-usage check file-wide (`# pyright: reportPrivateUsage=false`); about 120 `self.x._y` accesses across objects; 130 `except Exception`; 185 lint suppressions.
  - `loop.py` `SYSTEM_PROMPT` is one ~400-word string.
  - Tests: 253 `sleep` calls and 42 files relying on timeouts; a timing test (`test_capture_ownership.py::test_cancel_ack_deadline_includes_actual_channel_write_lock`) failed once on main CI.

## What to look for

Architecture and boundaries (what owns what; coupling; layering against CONVENTIONS' rule that deployables never import each other), dead or unwired code, duplication, missing abstractions, overly long or branchy functions, error handling, concurrency and persistence correctness (SQLite transactions, asyncio, cancellation), security relative to `docs/THREAT-MODEL.md` (also flag hardening that exceeds the threat model as possible over-engineering), drift between code and the docs/specs/decisions, test quality (brittle timing, over-mocking, gaps, slow tests), and what is good and should be kept.

Prefer depth on the riskiest and most central code over a shallow pass over everything. Measure where you can (line counts, call sites, import graphs) and cite `file:line`.

## Output

Write your notes file, replacing its placeholder, in this shape, then end with a short summary of the top findings in your final message:

```markdown
# Review notes: <area>

Baseline: main at fa456bc. Reviewed: <what you read in depth, what you skimmed, what you skipped>.

## Summary
<5–10 lines: the state of this area, and the 3–5 changes that matter most>

## Findings
### <ID, e.g. A-01>: <title>
- Where: <file:line, ...>
- Category: architecture | coupling | dead code | duplication | complexity | correctness | concurrency | persistence | security | over-engineering | performance | tests | docs drift | other
- Priority: P1 (fix before building more on it) | P2 (should do soon) | P3 (when convenient)
- Evidence: <what you saw; numbers>
- Proposal: <the concrete change>
- Effort: S (< half a day) | M (1–2 days) | L (more)
- Risk and verification: <what could break; which tests cover it; what to add first>
- Depends on / enables: <other findings, or features such as the ledger>

## Keep
<what is good and should not be disturbed>

## Questions for the owner
<product decisions a refactor would need, if any>
```
