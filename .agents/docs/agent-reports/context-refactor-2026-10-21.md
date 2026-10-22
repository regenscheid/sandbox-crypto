# Context-focused refactor: local review and validation

## Scope and provenance

The owner requested a separate worktree based on current remote `dev`, using the
attached development refactoring review, with context ownership as the priority.
Fetched base: `391cb7a73fc28546bc78f864219007b2408480c6`. Work branch:
`agent-context-refactor`, in the attached `agent-context-refactor` worktree.
The owner subsequently requested this branch name and publication to origin.
The primary checkout and `dev` were not changed. Existing branches were checked
for overlapping work before implementation.

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

No database, archive or confined protocol schema changed. The first stage retained
prompts, role policy and history visibility; the follow-up below changes folding
instructions. Root folding now receives the configured clock as
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

## Follow-up: context-only folding and useful retention

The owner requested further simplification and folds that preserve important
takeaways, remove retrievable bulk and keep recent context whole. The latest
independent `origin/dev` recovery update `1ad689e3` was inspected and merged into
this branch without changing the primary checkout or dev. Its new transport test
was adapted to read usage from `result.context`.

### Structural and behavioral changes

- Removed Transcript's context forwarding properties. Hooks use the read-only
  `RunView.context`; the shared folder takes `FoldPoint(context, call_index)` and
  has no execution-transcript dependency. `FoldLifecycle` retains the ordered hook
  slot, background cleanup, accounting and post-commit overflow reset. Confined
  calls reconstruct current wire state at dispatch; host authority is unchanged.
- New folds protect complete recent assistant/tool-result units covering the raw
  floor, including the whole unit crossing the boundary, beyond the old five-result
  minimum. Tests exercise real folding through `AgentContext` without Transcript,
  native replay, restart, original-history retrieval and an oversized recent unit.
  The last case retains the full unit and reports the existing irreducible limit.
  Previously saved stubs remain unchanged; archive originals are never rewritten.
- Independently written `sandbox-fold-enrich-3` and `sandbox-fold-overview-2`
  instructions prioritize supported findings, scope and assumptions, constraints,
  failed approaches with reasons, unresolved questions and next actions. They retain
  exact message references and search keys instead of copying long tool/document
  bodies, and distinguish hypotheses from established findings.
- Overview checks now stream original text from the admitted span through metadata
  and UTF-8 byte pages. They do not load native replay payloads or all historical
  bodies, and stop early only when every requested candidate fact is established.
  Scope tests keep child/sibling evidence out of root or unrelated continuation
  checks. Histories exceeding the previous 64 MiB aggregate or 8,192-row bound are
  covered. Exceptional numeric fragments and oversized tool-call metadata remain
  visibly unproved; they do not become an extra research stop.
- Enrichment measures mandatory outline/key/framing space before dispatch. No
  narrative room produces a diagnostic code fallback without admission, a paid
  summarizer attempt or invented zero-cost usage.

### Independent findings and fixes

The workspace agent reviewed the integrated engine/lifecycle boundary. It found
that research-state holds still searched for a bare Folder hook after introduction
of FoldLifecycle. The service now inspects the wrapped engine, and the public
research-state hold test passes.

The finalization agent reviewed evidence validation read-only. Two findings were
fixed and checked again: ordinary large comma-separated numeric arrays must not be
treated as one unprovable fragment, and signed-decimal proof must wait for enough
real trailing characters at a page boundary. The latter could otherwise accept
`-.5` from a page ending in `-0.5.` followed by a digit, when a separate positive
`0.5` already established the normalized numeric value. Four public regression
cases cover the rejection and valid sentence/EOF cases. No actionable finding
remained in the reviewed paths. The evidence author independently reviewed recent
retention; root added real-fold/native integration coverage and inspected the code.

### Follow-up validation and remaining limits

- **743 passed** in the final combined selection of changed orchestrator tests,
  including context, confinement, resume, prompts, affected consumers and the
  research-state hold path (158.47 seconds).
- **42 passed** after the final page-boundary fix, covering evidence and enrichment,
  including the large original-history checks (77.21 seconds). This overlaps the
  combined selection and includes four additional regression cases.
- Full Ruff and formatting checks pass; strict Pyright reports zero errors.
- Test-tier coverage (14,759 tests, each in one tier), installed licenses (81
  distributions), generated protocol freshness and repository hygiene pass.

An earlier compatibility selection passed 225 cases and failed two subprocess
service tests. Reproduction found intermittent missing SIGTERM `stopped` events
also on the original base. The serving test also intermittently timed out with a
model intent but no observed response/final record; its baseline probe passed once,
so **that timeout is unresolved and must not be described as a reproduced baseline
failure**. The final combined selection is not a whole-repository green claim.
The valid diagnostic runs are listed below; a separate malformed instrumentation
attempt was excluded from evidence. No owned diagnostic processes remain.

These deterministic checks establish mechanics and bounded reads, not semantic
research quality, lower end-to-end latency or stronger long-session recall. Real
multi-fold continuity and cache/performance comparisons remain open. No paid model,
live gateway, OpenShell lab, running-service migration or deployment was performed.

### Follow-up evidence outside Git

Files are retained under `/var/tmp/`:

| File | SHA-256 |
| --- | --- |
| `ss-context-final-integration.log` | `3090ed30445e2414fecf09303ac4136a1149f1d371f3775e6f27a1b69934d378` |
| `ss-context-final-integration.xml` | `f2e00d85f240037366707d0465f6d67447437a04cbd075fe3eab716d5efffec2` |
| `ss-context-evidence-final.log` | `d0817454b19adea30bb5cead0ae79e5ce4fc3ab1949ddf0d5e7a1255dac646ac` |
| `ss-context-evidence-final.xml` | `4e504496e0ec09c8c8b9d5632ca1e1a572d6b038a55c6fe18dd8539f90f8b51f` |
| `ss-context-final-pyright.log` | `6d88a1b220adb7a3d62092b6e38431f0b3fe8babe9864fab90e5849766260332` |
| `ss-context-final-repository-checks.log` | `0f1c6c46b8f6a5bfbbbb6ac5e5882750ea36d2ced241dd12d36fe161fb04cda3` |
| `ss-context-subprocess-baseline-summary.txt` | `9dab73e3e9df5dcfec66712ef5185c1881d6c3347345437c0caa85b992791fdf` |
| `ss-context-subprocess-repro-1.log` | `9857a8fbb052ddda45ce3d964793fd562580e0686116fb9910f6ef22f695f468` |
| `ss-context-subprocess-diag-1.log` | `85f1143f04ad44127b6bcc25717eb71b6a9da25d2786b35f2b7d6333ab8015c3` |
