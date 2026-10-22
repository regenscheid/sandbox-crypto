# Parallel refactor follow-up

## Scope and status

The owner authorized two or three further items from the development refactoring
review. Three independent GPT-6.1-Sol agents implemented capability assembly (item 4),
review snapshot planning (item 10) and generated document/comment/job API contracts
(item 6), starting from `agent-context-refactor` at `68160668`. Root integrated and
reviewed the results. All three are integrated with signed commits on this branch.
Publication is separate from merging into `dev`, deployment and live qualification.

The remote was fetched before starting. `origin/dev` remains `1ad689e3`; earlier
hook and workspace-contract refactors are already ancestors. Review planning also
contains the earlier #219 split and terminal-refresh fix under rewritten commits.
Those are foundations to retain, not missing work to repeat. The primary checkout,
its untracked design work and running services remain untouched.

## Required invariants

- Capabilities must preserve tool names, schemas, order, effects, role restrictions,
  dynamic connector behavior, ordered hook construction and early cleanup ownership
  on partial setup failure. A typed bundle must remove parallel assembly paths,
  rather than hide the existing paths behind another aggregate.
- Review planning must preserve finding priority, bounded reads, materialization
  order, immutable evidence and response pins, confirmation keys and coverage.
  ReviewStore retains transaction ownership, commit-time rechecks, watermarks,
  leases, recovery and paid-call accounting. Golden dispatch snapshots remain valid.
- API contracts must describe current public wire payloads, including optional and
  legacy fields, without inserting defaults or changing omission into null. Request
  types preserve validation-mode optional/default fields; existing serialized
  response types keep their behavior. Domain clients retain shared HTTP behavior.

## Baseline and validation plan

The baseline selection passed **164 tests in 28.75 seconds**: review golden dispatch,
consistency, review limits, turn hooks, loop characterization and harness behavior.
Raw logs and XML are outside Git under `/var/tmp/ss-parallel-refactor-baseline.*`.
Baseline source-size measurements are descriptive only; file splitting by itself
does not establish simpler ownership or better performance.

## Delivered changes and simplification

- **Capabilities:** typed definitions own tool entries, effects, reservation names,
  hook construction/order and cleanup registration. The harness binds scoped
  controllers and chooses main/task/check bundles. Toolbox retains worker dispatch,
  argument handling and dynamic MCP loading; its keyword constructor adapts existing
  callers. Fresh adapters, mutable controller replacement/removal and current notice
  bounds remain supported. Cleanup is registered before later setup can fail.
- **Review planning:** immutable inputs/groups/plans separate admission, deduplication,
  budgets, ordering, confirmation keys and coverage from scoped store reads.
  ReviewStore retains evidence materialization, commit-time rechecks and the entire
  transaction/lease/accounting lifecycle. Both finding materialization passes and
  golden dispatch outputs are preserved. Grant size/completeness is calculated once.
- **API contracts:** document/comment/job response types and mutation unions are
  generated from server schemas. Existing dictionary routes are not reserialized;
  omitted fields, nulls, aliases and optional request defaults keep their meaning.
  Three focused browser clients share the existing HTTP transport and remain exported
  through `api.ts`. Legacy job waits explicitly show an unavailable start time.

Descriptive size comparison against `68160668`:

| File or boundary | Before | After |
| --- | ---: | ---: |
| `tools.py` | 1,790 lines | 1,570 lines |
| `Toolbox.__init__` | 254 lines | 182 lines |
| `consistency/store.py` | 2,339 lines | 1,839 lines |
| Browser `api.ts` | 985 lines | 696 lines |
| Handwritten interfaces in `api.ts` | 31 | 11 |

This is not a net line-count or speed claim: the capability definitions, immutable
planner values and explicit server contracts add code. Harness length stays about
the same. The useful changes are fewer parallel registration paths, independently
testable evidence policy, and removal of a second handwritten browser schema for
the selected domains. Worker lifecycle and other API domains remain separate work.

## Review and corrections

A separate GPT-6.1-Sol reviewer examined the planner, API changes and capability
assembly against pinned commits. Root also reviewed and integrated each author's work.
The planner review found no remaining issue; root requested cached grant metadata to
avoid repeated decoding during admission. API review found missing normalization and
range-attachment fields; author inspection found warning paths. All were added with
public edit/read/list/thread regressions and independently rechecked. No remaining
medium-or-higher findings were reported.

Capability characterization caught post-construction controller replacement and
initially absent controllers. Root identified the need to read the current job-notice
bound rather than a captured default. Hook definitions now cover these transitions
without a second registration table. Partial setup failure, role exclusions and
reserved-name compatibility are tested. A final root-reviewed follow-up preserves
the stateless harness's original offer/denial boundary. Root corrected its synthetic
parent fixture to include the required delegation and used named Pyright suppressions
only for intentional frozen-value mutation tests. Production guards were unchanged.

## Validation and limits

- Before implementation: **164 passed**, plus **12** capability characterization
  cases against the original assembly.
- Reviewer author: **241 passed**, followed by **83** checks after cached metadata.
- Capability author: **214 passed** on its final initial product commit.
- Workspace author: **862 passed** across its full fast suite.
- Root combined selection: **804 passed, 3 failed** in 167.44 seconds. All three
  failures are in `test_ledger_tools.py`: frontier category creation, grouped frontier
  reads and frontier history. Their exact node IDs and error messages match the saved
  base-`391cb7a7` run: `KeyError('category')` and missing SQLite WAL sidecars after a
  fixture closes its final writer. No ledger/store implementation was changed here.
- Root final compatibility/planner/math-checker selection: **92 passed** after the
  stateless fixture correction and math build.
- Root frontend: **2,289 tests in 129 files passed**; Svelte/TypeScript, formatting,
  production and math builds passed. Dependencies installed from the committed lock
  with scripts disabled; the final install used the local cache without audit/network.

These selections overlap and must not be added as a unique-test total. Ruff, format,
strict Pyright, generated types/protocols, license, tier and repository hygiene checks
are recorded with their final logs below. Three existing fixture failures mean this
is not a whole-repository green claim. The earlier unresolved serving timeout and
SIGTERM fixture limitations remain in the context-refactor report. No live OpenShell,
paid model call, performance benchmark, scientific recall qualification, migration,
service deployment or merge into `dev` was performed.

## Evidence

Raw logs, XML and descriptive metrics remain outside Git under `/var/tmp`; hashes
are listed below. `ss-parallel-refactor-baseline-comparison.json` records the three
exact matches against the earlier `ss-context-baseline-fast.xml` evidence.

| File | SHA-256 |
| --- | --- |
| `ss-parallel-refactor-baseline.log` | `3ee5ca95994f7cb8ae9c3ecc2afbd4fdcf7fa969b601621a205d1585f2f5cbe3` |
| `ss-parallel-refactor-baseline.xml` | `b1dd7a274d4f06e14c8add1bf7a18c53281676950e3e9b56c6a70548189b1d8f` |
| `ss-parallel-refactor-integrated.log` | `9b02e111f0c84a5113e0d5b8d59c04c503273083db0045bebf4e95c6b29afd38` |
| `ss-parallel-refactor-integrated.xml` | `43c96442f642e593c8f0c41dbcd1fc2117e407fa3f24759082107593be854e53` |
| `ss-parallel-refactor-baseline-comparison.json` | `83897b632c2084a4f7b91f22cd95711c98c29cab1a4c6e57afea33a66eb3e52e` |
| `ss-parallel-refactor-final-2.log` | `7bfae8ca0b394fcd1bf8fb6672e5735eaf8cff5c1e21ad2228d648f867e63f81` |
| `ss-parallel-refactor-final-2.xml` | `ee67d672d4778063f7d4ba06e766ca9f3828f7d70314d51e34e010b16241370e` |
| `domain-api-workspace.log` | `8c015b8fdc73722f16fec6a29d92125a2262a404fdf696bfd83b67ee5b7e2614` |
| `ss-parallel-refactor-web-tests.log` | `d2018e697c0675136510b65fa003e6a8481192a5302978a1b9cb424313f39421` |
| `ss-parallel-refactor-web.log` | `dbcac10f0f6b7d89734ecdce2b0d94097821e91e09d1faa0405c54d7615d9787` |
| `ss-parallel-refactor-baseline-metrics.json` | `2c59579a8adb5a344fc4c8f2537fadb959449ee794ba2bb5071a3b44a4655014` |
| `ss-parallel-refactor-final-metrics.json` | `a0373c5784bd5fcf40d0b57f9491658cc769acbf193874552f6d5c4fb4300149` |
| `ss-parallel-refactor-final-static.log` | `92ce228c1332eb8ce6a84fefe45daec37327fd780831a0e5e9d78475bb875732` |
