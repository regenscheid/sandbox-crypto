# Live context and adaptive research check-ins

Development is local `dev`, integrated through signed `a8ef3c5d`. This report
does not establish deployment into a personal or preserved research service.

## Implemented scope

- An authenticated context inspector opens live attempts before final-record save.
  It plots journaled response observations and timestamped fold events, refreshes
  while visible, and shows a separately labeled latest auxiliary-call snapshot.
  Opening begins at recent activity rather than replaying the whole event stream.
  Exact working-context composition remains unavailable; the graph does not
  pretend auxiliary input tokens measure the main agent's context occupancy.
- Resource warnings and parent decisions are durable across attempts. Parents can
  continue, redirect, request a report, pause or extend a child's turn allowance
  within configured policy. Token increases are explicit USER decisions, exactly
  10,000,000 more each time, starting from new defaults of 20,000,000.
- Chat exposes progress, checkpoints, decisions and current token allowances.
  Nested ancestor blockers have an actionable allowance review. Each approval
  pins its reviewed revision; a lost response triggers a read, never an automatic
  second increase. Existing spending/security controls retain their authority.

## Independent review

The read-only GPT-6.1-Sol review identified slow timeline startup, graceful
pause/report handling with descendants, an inaccessible ancestor approval,
omitted completed-descendant usage in ancestor accounting, and own-warning wakeups
that could spend an unnecessary model call. Integration includes focused fixes
and regressions. Final composition review cleared all critical/high/medium findings.
Exhaustion after an earlier continue decision has its own durable notice so the
parent can make a fresh decision; renewed grants supersede the old notice.
Additional checks corrected late-receipt commitment double-counting and preserved
immutable prompt-version pins. The scientific lab received a separate light review;
its failed-startup cleanup now always stops exact owned units and reports unresolved
VM ownership honestly.

## Validation boundaries

- Initial timeline slice: 25 Python route/artifact tests and 14 DOM tests passed.
- Combined browser suite: 2,228 tests passed, with no unhandled errors. An earlier
  pass exposed four outdated question-panel fixture responses; those fixtures now
  answer the resource route explicitly.
- Adaptive backend: 114 focused resource/lifecycle tests and 85 resource/prompt
  tests pass. Integrated workspace/context/generated-type coverage: 16 tests pass
  across the initial run and the two fixture corrections; 32 affected DOM checks
  pass after the final frontend type fix. An initial broad run was stopped after
  four old-default assertions failed (3,743 passed, 2 skipped before interruption).
  Those assertions now expect the intended new defaults; the affected
  delegation/tree modules pass all 52 tests.
- The next complete Python pass had 14,408 passes, five failures and 50 skips.
  It caught two more old default assertions, a legitimate notification-order race,
  a reporting fixture whose expanded tool schemas consumed its report room, a
  missing documented timeline route, and forbidden workspace `.control` access
  (two default assertions belonged to one failing test). The route now passes its
  scoped public document store to the orchestrator adapter. The reporting fixture
  registers one synthetic tool and retains the actual dispatch/report path. All
  75 affected tests pass; the parent/child notification test also passes 50 repeats
  on four workers. Independent light review is clear. The final source-frozen
  full suite passed against `a8ef3c5d`: **14,413 passed, 50 skipped**, with the
  two expected serializer warnings from deliberately malformed legacy metadata.
  Runtime was 672.84 seconds on four workers. Live/integration opt-ins were unset.
- All 14,463 collected tests belong to one tier: 13,504 fast and 959 slow.
- Full Ruff/format, Pyright (zero errors/warnings), hygiene, protocol/API generation,
  Python licenses, strict Svelte/TypeScript, Prettier and production build pass.
- Authenticated disposable browser acceptance: the unsaved-attempt inspector
  displayed two timestamped answers, a fold and settled auxiliary usage. Appending
  a third synthetic observation updated the same open dialog without reload; cache
  coverage became 3 of 3 at 50%. A separate synthetic paused branch showed its
  checkpoint and exact 20M-to-30M approval, then removed the approval button and
  offered root Resume. No Resume or model dispatch was made.
- The owned browser tab, listener and temporary state were removed and absence
  verified. Evidence under `~/.cache/pt/context-resources-20261018/`:
  - `python-full.log`: SHA-256
    `62715945435728ea24b3ecce3e73fde7bb758a37999da41da543e8354561d118`.
  - `web-full.log`: SHA-256
    `b0821baf0c33d36f2a40ecc95bbf30b73178c026dafe118a9b325d1d63f3ebbf`.
  - `live-timeline.png`: SHA-256
    `16139df5019088d26b105c3aa7ca29d833a7b9a3da2128011321d0bcc7d72570`.
  - `token-approval.png`: SHA-256
    `16259d9ee9b65b02594b0850ee2b2853a7581556a8b5aea26f39f5a8b494fb26`.
- Scientific validation is prepared separately in
  [the experiment plan](../../labs/scientific-quality-plan.md). Its independent
  oracle and offline wiring checks passed again after integration, with temporary
  state removed and zero paid calls. These checks do not establish real reviewer
  quality or long-context scientific retention. No paid inference has been dispatched.
- The personal instance, preserved SNOVA archive/service and stopped SNOVA monitor
  are untouched. No push or live service upgrade is implied by these local commits.

## Remaining scope

Exact captured working-context composition, indexed original-history search,
adaptive time/storage policy and root self-renewal are not implemented here.
The timeline shows recorded observations rather than a live tokenizer estimate.
Real-model reviewer correction and long-context scientific retention remain
unqualified until the separately authorized evaluation runs.
