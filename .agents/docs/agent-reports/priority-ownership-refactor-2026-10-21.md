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

Final integrated verification is in progress.
