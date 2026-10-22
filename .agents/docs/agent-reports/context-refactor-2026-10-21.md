# Context-focused refactor: local review and validation

## Scope and provenance

The owner requested a separate worktree based on current remote `dev`, using the
attached development refactoring review, with context ownership as the priority.
Fetched base: `391cb7a73fc28546bc78f864219007b2408480c6`. Work branch:
`codex/context-lifecycle-refactor`, in the attached `agent-context-refactor` worktree.
The primary checkout and remote branches were not changed. Existing branches were
checked for overlapping work before implementation.

The base already folded both root and delegated conversations. This refactor moves
the implementation and consolidates its ownership; it does not claim to introduce
child compaction or a new summarization algorithm. See the
[ownership map](../../../docs/specs/agent-context-refactor.md).

Three GPT-6.1-Sol agents handled context preparation/design, attempt finalization,
and workspace requests. The integrating agent owns active context, integration,
validation and this report. All delivered commits are signed locally.

## Delivered boundaries

- `orchestrator/context/agent.py`: one `AgentContext` owns working messages, prompt
  provenance, usage, rebuilt views and offsets, archive-first appends and limits.
- `context/preparation.py`: one trusted scope constructs the folder, history logger
  and retrieval tool for root, child and resumed attempts. `context/history/` holds
  the existing archive, full-history reads/search, fold engine and diagnostics.
- `loop.Transcript` composes that context and retains turns, tools, delivery and stop
  decisions. Confined state restoration uses the same context owner.
- `finalization.py`: fresh/resumed children share checkpoint, save/ack, receipt,
  settlement and terminal-kernel cleanup. Fresh cleanup now follows settlement;
  persistence failures leave cleanup retryable. `harness.save` retains its export.
- Workspace `requests.py`: shared scoped threaded execution, JSON encoding and
  document access. Feature handlers retain their authorization and response shapes;
  `WorkspaceContext` remains the sole store registration.

No database, archive or confined protocol schema changed. Prompts, role policy and
history visibility are retained. Root folding now receives the configured clock as
child folding already did; production defaults still use the system clock.

## Independent review

The finalization agent reviewed the integrated context code read-only: original
working/sent/rebuild behavior, archive-first writes and usage attribution, scoped
preparation, fold lease ordering and confined state restoration. No actionable
finding remained. This was source and deterministic evidence, not live-provider
qualification.

The workspace agent independently reviewed finalization read-only, including
save/ACK/settlement ordering, cancellation and cleanup after failed persistence.
No actionable finding remained. Its targeted checks passed 46 cases; an additional
compatibility selection passed 46 with one SIGTERM failure also reproduced on the
base. The integrating agent reviewed workspace scope/error/timeout behavior and
ran its full suite.

## Integrated checks

- **426 passed**: integrated context, history/folding, delegated/resumed/confined
  paths, archive limits, finalization/crash handling, harness and prompt tests.
- **855 passed**: the full workspace Python suite.
- **30 passed**: digest tests repeated three times after correcting fixture lifetime.
- Ruff and format checks; strict Pyright (zero errors); generated API and protocol
  freshness; repository hygiene; test tiers (14,693 collected, each in one tier);
  license check (81 installed distributions): passed.

Integration testing found an eager optional `observe_kernels` lookup in preparation.
It is lazy again, matching the old path and allowing public no-kernel CLI fixtures.
Two memory-limit fault probes moved to `AgentContext`, preserving their assertions.
Workspace and digest fixtures now hold their control-store writer through the
test, matching the live harness rather than relying on SQLite connection cleanup.
No production WAL validation was weakened.

## Broad-suite failures and limits

The broad integrated orchestrator fast run before those last integration fixes
reported 79 failures, 4,278 passes, 13 skips and 11 errors. A separate `git archive`
of the fetched base, using the same interpreter/dependencies and verified baseline
imports, produced 66 orchestrator failures, 4,278 passes, 13 skips and 10 errors.
All existing orchestrator nodes finished in that baseline run; its combined
workspace tail stalled and was interrupted, so it is not a completed baseline
workspace suite.

Seventy-four failing nodes overlapped. The baseline failures include missing WAL
sidecars after fixtures close their last writer, a ledger `KeyError("category")`,
and a SIGTERM test missing its `stopped` event. Of the 16 integrated-only failures,
13 came from the eager optional method lookup, two from fault probes at the old
owner, and one from the digest fixture's WAL lifetime. The first 15 pass in the final
426-test selection; the digest fixture passes the repeated 30-test selection.
The exact digest case passed on the base in isolation: its cleanup sensitivity
must not be presented as an exact reproduced baseline failure merely because other
base fixtures fail with the same WAL error. Its fixture was corrected explicitly.

The broad suite was not rerun after these final focused repairs. An entirely green
repository suite is therefore not claimed. Remaining baseline fixture and SIGTERM
work is recorded in the backlog. No live OpenShell, real model, UI-browser or
performance benchmark was run; no paid research, migration or rollout occurred.
Worker lifecycle/capability decomposition remains outside this branch.

## Evidence retained outside Git

Local logs are under `/var/tmp/`. These SHA-256 values identify the reviewed files:

| File | SHA-256 |
| --- | --- |
| `ss-context-final-focused.log` | `e4ae5e8c5a919c091763efab631c1caf8285b377050b4363ebb5dc60ebef76cd` |
| `ss-context-workspace-tests.log` | `f8a3484d63558b62c16aa72f5d686ade565a1af9dc5a6f6ae6543594943ef873` |
| `ss-context-integrated-fast.xml` | `cae85694d0d262a8b3080fcc622e57721234cc42262d281f667a733c13761ff8` |
| `ss-context-baseline-fast.xml` | `7a0c9b08ae104713bb60868ceacc84a691e29e8bf110c67eb44a32d7d75d9fe0` |
| `ss-context-failure-comparison.json` | `c3716af8df83c2c9d8a97549a9d08e1d9dedd28af08bedbf3faf6fac01c165a3` |

Additional diagnostic logs: `ss-context-baseline-wal-target.log`,
`ss-context-digest-final.log`, `ss-context-baseline-digest.log`, and the final
`ss-context-digest-owned.log`. No synthetic-test processes were left running.
