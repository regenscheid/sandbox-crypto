# Remaining priority ownership refactors

Work is isolated on `agent-context-refactor`, starting at `150da8f2`.
The original review's remaining high-priority executor item and medium-high ledger
and provisioning items are the scope. Existing context, finalization, workspace,
capability, reviewer-planning and initial API-contract work stays intact.
No live service, research state, storage schema or public protocol is intentionally
changed; publication and deployment are separate.

## Baseline

Before the new product changes, the executor/run-kernel/recovery-gate/payload,
provisioner/observation/recovery and ledger/ledger-tools selection passed 263 tests
and failed five in 24.99 seconds. Four reported missing WAL sidecars after fixture
writers closed; the fifth reported a missing category in the tool response.
The fixture causes and refactor validation are tracked separately below.

Failing nodes:

- `test_ledger.py::test_links_to_another_projects_records_are_not_found`
- `test_ledger.py::test_a_finding_link_names_the_session_the_finding_is_in`
- `test_ledger_tools.py::test_the_agent_reads_a_frontier_a_group_at_a_time`
- `test_ledger_tools.py::test_the_agent_reads_a_frontiers_history`
- `test_ledger_tools.py::test_the_agent_defines_and_reads_frontier_categories`

## Evidence

Raw logs stay outside Git. Baseline at `150da8f2` plus the tracking-only commit:

- `/var/tmp/ss-priority-baseline.log`: SHA-256 `e69d8311456ff1092529be6547b8b9a2639d4f41f7678ad8c334715c3adf9ba6`.
- `/var/tmp/ss-priority-baseline.xml`: SHA-256 `390e2ccd61ff6131f3813dad47c62ed6909e0916f4de74da5e2a709e3091c866`.

## Ownership changes

- The executor protocol coordinator delegates accepted-work and kernel lifetime to
  `executor_lifecycle.Lifecycle`, and launch permits, gate generations and ordered
  package observations to `executor_admission.Admission`. Existing file and capture
  owners now translate their own request payloads. Standalone payload manifests
  include the new standard-library modules. Monotonic admission timestamps and
  deadlines share the injected clock; protocol replies remain coordinator-owned.
- The ledger facade owns each transaction. Queries, preparation, domain rules and
  persistence take explicit transaction inputs; a typed prepare/evaluate/commit
  pipeline retains replay receipts and the fold callback in the same write
  transaction. Session membership checks use that transaction's scoped session
  table rather than opening another project-store connection.
- The provisioner owns queues, records, waiters and task drain. Stage execution,
  exact-file permission interaction and reconciliation have concrete collaborators
  and typed completion values. Queue release still precedes terminal observation
  completion; store/task ownership lasts until the observation drains.

| Coordinator | Before | After |
| --- | ---: | ---: |
| `executor.py` | 2,879 lines | 2,174 lines |
| `ledger/store.py` | 3,462 lines | 643 lines |
| `provisioner.py` | 1,878 lines | 578 lines |

These changes reduce the responsibilities of each coordinator; they are not a
claim of less total code or measured throughput improvement. No generic workflow
framework, coordinator backreferences, new dependency or storage migration was added.

## Findings and corrections

- The five baseline ledger failures came from fixture writers closing before
  immutable WAL readers. Fixture writers now live through the test; production's
  missing-sidecar guard is unchanged.
- Independent review found a pre-existing provisioning recovery defect: retention
  could evict an operation after its coroutine ended but its terminal save failed.
  Retirement now requires durable terminal state and drained work. A later
  `forget()` can retry the save and release the owner. A new public regression
  failed before the fix, then passed in the 73-test recovery selection.
- Broad executor checking found three stale test-owner setup references; full
  repository typing found three more in kernel-revision fixtures. They were
  updated to the new owners without removing assertions. The cancellation review
  also preserved ownership through the close reply, with a repeated-cancellation
  regression test.

## Independent review

A separate read-only GPT-6.1-Sol reviewer cleared integration
`33bfe01a340921164f2b64c70e582378572dba95`, plus the three kernel-revision fixture
setup references, with no introduced Critical, High or Medium findings. The
provisioning retention finding was fixed and re-reviewed. The root separately
inspected adapters, cancellation acknowledgements, transaction ownership and test
migrations. A SQL-literal audit retains all 69 original ledger statements, adding
only the scoped session-membership query; this is supporting evidence, not a
substitute for behavior tests.

## Validation

Checks are cumulative evidence, not a sum of unique tests; focused selections overlap.

| Check | Result |
| --- | --- |
| Ledger, tools, fold proposals, frontier citations and review ledger | 339 passed |
| Provisioning, observations, permissions, recovery and adjacent contracts | 476 passed; 73 passed after the retention correction |
| Executor boundaries and adapters | 76 passed; 83 passed after test setup corrections; 38 final cancellation/recovery checks passed |
| Both process-staging modules, separately with one worker | 8 passed |
| Full integrated fast tier | 13,782 passed, 23 failed, 10 setup errors, 24 skipped |
| Exact failed/error nodes on pre-refactor `150da8f2` | 22 failed, 10 setup errors, 1 passed |
| Artifact review with forced garbage collection after fixture setup, after correction | 38 passed |
| Worker slow tier, excluding separately tested staging | 826 passed, 26 live-worker skips |
| Web tests | 2,289 passed in 129 files |

The fast tier is **not green**. Of its 33 failed/error nodes, 31 reproduced the same
failure category and message at the pre-refactor commit. The SIGTERM node timed out
in the broad run, and isolated runs on both the baseline and final branch failed
with the same missing `interrupted` event assertion. Existing WAL/fixture/schema
expectations and two image filesystem-policy assertions remain open; this refactor
does not relax their production guards or change those image policies.

The remaining artifact-review failure passed in a normal isolated baseline run.
Forcing garbage collection after fixture setup reproduced its missing-WAL failure
on the unchanged baseline. That fixture relied on an unretained project writer
surviving until its lazy document writer opened. The corrected fixture explicitly
initializes the document writer before closing the project writer, and initializes
its reopened writer in the restart case. All 38 artifact-review tests pass with the
same forced collection. No assertions or production checks were removed. The
independent reviewer also cleared this fixture-only correction.

Ruff format/check and full strict Pyright pass (zero errors or warnings). Locked
Python sync, Python/web license checks, protobuf and API generation freshness,
web formatting/type checking, production/math builds and repository hygiene pass.
Tier coverage assigns all 14,831 collected tests exactly once (13,839 fast and 992 slow).

All pytest runs had live-model and integration flags unset, unique temporary/cache
paths, and at most four workers; process-staging modules ran separately with one.
The 26 worker skips require a configured running conformance worker. No live
conformance result is inferred from them.

## Reproducible evidence

Additional raw evidence, all outside Git:

| File under `/var/tmp/` | SHA-256 |
| --- | --- |
| `ss-priority-fast.log` | `885a7798d4d440a450594a92044c81592f64851ac98c762379db2a9510014b8e` |
| `ss-priority-failures-baseline.log` | `a2b55ba2c6437a40b392166b2d799b649df0a3bd00198dd6561cc95429b0a989` |
| `ss-priority-artifact-gc-baseline.log` | `8bfce7567db3580abf00ee847d4db55812788bfbb8546830bc9ff727a8e8b779` |
| `ss-priority-artifact-gc-fixed.log` | `3468aa7b6655c231f634f1fa2d2779976aa2034f5ec5f0023875d42addc8594a` |
| `ss-priority-sigterm-final.log` | `f1a25bd63139871882178f87e7c960ba23da24f1c8ff76eba5a463cf31fe538a` |
| `ss-priority-structure-audit.json` | `7396ddb3fc00a7f6b57b87081afd06880d18f0422a1017c62a1d15f25534db1a` |
| `ss-ledger-full-green.log` | `dd6ba0ebb791c7950e3e82701c4319b145ab0293827d26755c2f8540f5c35d9b` |
| `ss-provision-validation.log` | `82b5b931373a6cae70f8b10c2b5ef20d00111ec79227fe27ca66528253bb730a` |
| `ss-executor-staging.log` | `e7b3407954c3c3ea474e5f340d6a8fcd5d655f87d9593b04fad8fb0021269b21` |
| `ss-priority-web-tests.log` | `ca2f56147059da2ee9cbe54af84aee7107fa983b0b19b58046f60e8eecba4619` |
| `ss-priority-worker-slow.log` | `0ca7cc75f0c38ffe209ec30019826f7c9ba86ccd293cd2a9ce041d8c929b8dcd` |
| `ss-priority-pyright-final.log` | `6d88a1b220adb7a3d62092b6e38431f0b3fe8babe9864fab90e5849766260332` |
| `ss-priority-tiers.log` | `754f89f0f1b3d0c1ade15970939a62767577b8dc90170a8fd4aa5129c96e0001` |

## Limits

Implementation and independent source review are complete on the isolated branch.
This is not a merge into `dev`, a service rollout, a rebuilt OpenShell image or
real-model/scientific acceptance. Standalone payload and local process tests do not
replace a live OpenShell qualification run before merging runtime-sensitive changes.
No throughput or model-cost improvement was benchmarked. The review's
medium-priority UI work and extending API contracts beyond its initial document,
comment and job domains remain separate follow-ups.
