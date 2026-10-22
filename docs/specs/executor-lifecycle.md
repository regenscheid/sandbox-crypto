<!-- SPDX-License-Identifier: MIT -->
# Executor lifecycle and package admission

The executor is a protocol coordinator. Its lifecycle owner tracks accepted work,
kernels, ended runs, control requests and shutdown. Package admission owns the gate,
launch permits and package observations. File and capture request adapters use the
existing bounded implementations. Every collaborator shipped beside the executor
uses the standard library only; Jupyter remains an optional lazy kernel dependency.

## Observable guarantees

- Shell commands and cells acknowledge acceptance before their state and end events.
  Cells for one kernel execute in submission order. A session and each delegated run
  have separate kernels; closing a run retires it and refuses late cells and restarts.
- Closing a session stops its jobs and kernels, preserves session kernel generations,
  and retires its run kernels. Closing a run affects only that run's kernels.
- Restart stops the previous generation before starting the next. A conditional
  restart refuses a kernel that became live before its control lock was acquired.
- Shutdown closes admission before draining accepted work and control requests,
  then stops kernels and descendants. Launch and cleanup remain owned through
  repeated cancellation; they cannot be abandoned with a live process behind them.
- Admission starts held. Only an exact operation and generation exemption passes a
  hold; recovery refuses all launches. A hold ACK follows all earlier launch work,
  including kernel readiness and cancellation cleanup. Stale generations conflict.
- Observations serialize fingerprint changes and journal consumption. A revision is
  sent before updating the known fingerprint or removing its journal records, and
  before any result referring to that revision. Kernel birth holds the shared package
  lock through readiness, then records the observed fingerprint and package epoch.

## Ownership and lock order

A cell holds its kernel queue lock for execution and result preparation. Kernel
start, stop, retirement and restart hold its control lock. The order is kernel queue
(if a cell), kernel control (for lifecycle work), admission launch permit, package
shared file lock, observation lock, then binding inventory lock where required.
A package hold takes only the launch permit; waits for the gate release it before
sleeping. Observation reporting never tries to acquire a launch permit. Close does
not take the queue lock, so it can retire a kernel with queued cells; those cells
recheck retirement under control before starting.

The lifecycle owner owns active work and control request registries. A cancelled
launch marks its work cancelled but retains its permit until the actual launch or
cleanup finishes. Close and shutdown drain their owned operations. Protocol replies,
stream framing, result assembly and feature validation remain with Executor.

All admission deadlines and polling, process grace periods and idle reaping use the
injected monotonic clock. Admission timestamps are recorded in that same clock's
domain. Wall timestamps and filesystem observation timestamps retain their existing
real-time meaning. Async I/O wait timeouts remain enforced by the event loop.

## Verification

Public protocol characterization covers gate generations, run retirement and session
isolation. Existing run-close races, recovery-gate cancellation/ACK tests, executor
process tests and payload import/manifest checks remain required. Fake-clock tests
cover held admission without real polling delays. No protocol or policy changes are
part of this refactor. There are no open design questions.
