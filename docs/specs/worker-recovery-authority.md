# Worker recovery authority

Implementation contract for tree Resume (worker protocol 8). This extends
[tree Resume](tree-resume.md); it does not replay uncertain effects.

- The trusted control service installs a durable, monotonically increasing epoch
  for an exact `(project, session)` through a new authenticated recovery route.
  Its client needs an explicit `recovery_control` permission scoped to configured
  projects. Ordinary execution credentials cannot install or advance epochs.
- Execution context optionally carries typed work, attempt and epoch authority.
  When absent, existing serialized contexts retain their bytes. Before a session
  is fenced, legacy execution remains compatible; after fencing, unowned agent
  mutations fail closed. A separate explicit authenticated operator permission
  authorizes scoped user maintenance. The caller's `actor` string grants nothing.
- Worker-owned admission capabilities bind the authenticated request's context.
  Agent tool arguments cannot construct or replace them. Every deferred task
  keeps its original authority; provisioning must not replace it with the current
  epoch when it submits an installer step.
- Every effect checks authority again after connection, package admission and
  channel-lock awaits, immediately before writing a command to the executor.
  A durable fence update and the final check/write admission are serialized on
  the worker event loop. Once accepted, a newer fence blocks old queued commands;
  it does not cancel or replay commands admitted earlier. This is an admission
  boundary, not a process-lifetime lock.
- Execution, jobs, provisioning installer steps, file writes and shared sync,
  staging/sealing, kernel restart and session close use that boundary. Passive
  observations do not acquire effect authority. Cleanup that changes guest state
  needs current authority or the explicit operator path; stale cleanup cannot
  terminate a newer attempt's kernel.
- A passive operation lookup takes typed project/session/environment context and
  the original operation ID. It returns an existing execution or provisioning
  record, or absence, without connection, wake, dispatch, resubmit or digest replay.
  A matching project alone is insufficient: session and environment must match.
- Epoch state survives worker restart. Invalid or unreadable state fails closed.
  Worker state has one serving process; final dispatch checks use that process's
  owning event loop. Deployment of concurrent servers against one state directory
  is outside the existing worker lifecycle contract.

Deterministic fixtures cover persisted monotonic updates, wrong projects and
permissions, absent/stale authority, legacy behavior before/after fencing,
requests paused in channel admission, inherited provisioning authority, explicit
operator flows, passive lookup isolation and no-wake behavior. No host or live
research environment is needed.

## Public seam

`contracts.context.ExecutionAuthority(work, run, epoch)` is attached to
`ExecutionContext.authority`; when absent it is omitted from serialization so
legacy full-context encodings and request identities remain stable. Work and run
names allow the full timestamp plus a 128-character run ID.

`contracts.recovery.RecoveryFence(project_id, session_id, epoch)` is sent through
`WorkerClient.fence()` to `POST /v1/recovery/fence`. Equal epochs are idempotent;
a lower epoch is a conflict. Only the authenticated `recovery_control` grant
permits installation. The launcher's host harness token receives that grant. Its
workspace and launcher maintenance tokens receive explicit `operator` permission;
the pool token remains administrative. The VM receives none of these credentials.

`WorkerClient.lookup(context, operation_id)` calls `POST /v1/operations/lookup`
and returns `OperationLookup(execution=None, provision=None)` or one existing
record. Ambiguous execution/provision IDs are refused. It authorizes the project
and compares stored session/environment, allowing lookup after environment retirement.
Cancellation may carry `EffectControl(context)`; legacy empty bodies carry no
ownership, and cannot cancel a fenced session without operator permission.

The worker's existing state-directory `StateLock` enforces one serving process.
Recovery epochs are worker schema 5, independent of the control store's schema.
Admission's final synchronous database read and channel write have no intervening
await. A definitive local refusal is distinguished from an uncertain write, so it
cannot tear down a newer owner's shared executor channel. Canceled admitted scans,
results and records remain observable; authority never synthesizes a receipt.
