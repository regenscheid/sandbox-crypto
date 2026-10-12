# Executor recovery in OpenShell on babbage

Run started 2026-10-12. **In progress: daemon-loss cleanup failed its initial
runtime probe; the revised recovery fence has not yet been accepted.**

The disposable lab uses OpenShell v0.1.3 with its MicroVM driver, synthetic
provider/client tokens, and the minimal profile. The initial runtime was the
executor-recovery worktree at `85fda95` (daemon implementation `d5b68cc` and
follow-up `59304ac` integrated over the background-job/package cleanup work).
All services use that isolated checkout, so syncing the primary checkout does
not change this running lab. The real policy middleware and package cache are
in the path. No real model is used.

[Initial restart evidence](babbage-openshell-lab-20-initial-evidence-2026-10-12.json)
and [failed daemon-loss probe](babbage-openshell-lab-20-initial-failure-2026-10-12.json)
are preserved separately from the pending corrected rerun.

## Initial checks

The reusable [recovery checks](../../labs/recovery.py) observed:

- A running job survives a worker restart under the same execution/attempt.
  Its attempt marker appears once, output before and after the restart is
  retained, public event sequences are contiguous, and one terminal result
  is recorded.
- Completion while the worker is offline is replayed after restart.
- A connected result retains all 600,000 requested output bytes within its
  1 MiB budget without being marked truncated.
- An invalid working directory ends as a definite failure.
- Cancellation after reconnect ends the original job as cancelled.
- Python kernel state survives a worker restart (`17` becomes `18`).
- An offline job's 3,000,018-byte full log matches the expected bytes/hash;
  its 1,024-byte inline output retains the head and tail and reports truncation.
- A job's three-second timeout takes effect while the worker is offline.

## Failure found before merge

The daemon-loss probe launched a Python parent and a `setsid sleep` child,
then killed only the unique daemon PID after checking its start identity.
The original job became `lost` and a fresh shell succeeded, but the escaped
child remained alive. The initial observed parent/child PIDs were 798/800;
the replacement daemon was 830.

OpenShell permits peer `/proc/stat`, `status` and `cmdline` reads but denies
peer `environ`, `maps` and `exe` reads. The replacement daemon's owner-marker
sweep therefore cannot establish ownership of these old processes. Local
process tests did not expose this runtime boundary. Replacement readiness
was premature and this check **failed**.

The planned correction fences a confirmed daemon-incarnation change with a
verified stop/start of the same managed sandbox, preserving its writable disk.
The old persisted endpoint remains a fence until lifecycle recovery succeeds.
A transient connection/authentication failure must not restart a healthy owner.
Static bindings without a managed lifecycle must refuse replacement readiness.

## Pending acceptance and cleanup

The revised probe will check both affected jobs become `lost`, prior kernel
state is gone, the sandbox name/ID remains the same, and workspace plus Python
prefix files persist. It records boot ID and PID/start identity so PID reuse
cannot masquerade as a survivor. A deleted sandbox, a healthy second environment,
and final integrated checks still need acceptance.

The lab is still running. No teardown or merge is claimed here. The final
report will retain the failed probe separately from the corrected rerun and
record cleanup before this slice is merged.
