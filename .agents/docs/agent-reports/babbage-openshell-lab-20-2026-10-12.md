# Executor recovery in OpenShell on babbage

Run on 2026-10-12. Worker and bridge restart recovery passed against real
OpenShell VMs. The initial daemon-loss check failed; the corrected lifecycle
fence then passed the expanded checks. The lab was completely removed.

[Initial restart evidence](babbage-openshell-lab-20-initial-evidence-2026-10-12.json),
the [initial daemon-loss failure](babbage-openshell-lab-20-initial-failure-2026-10-12.json)
and the [final evidence](babbage-openshell-lab-20-final-evidence-2026-10-12.json)
are retained separately. These are API records and observed process identities,
without client or daemon tokens. The reusable [check script](../../labs/recovery.py)
runs research commands only inside the VMs.

## What ran where

| Component | Configuration |
| --- | --- |
| Host | babbage, Ubuntu 26.04 |
| Initial worker | `85fda95`, including daemon implementation `d5b68cc` and transport guard `59304ac` |
| Final worker | `d62f615`, including the reviewed lifecycle fence `e64b02e` (authored as `7b4bf1c`) |
| Gateway | OpenShell 0.1.3, MicroVM driver, mTLS, loopback port 17670 |
| Sandboxes | `sandbox-science/minimal:02f85b3`, environments `lab20-a` and `lab20-b` |
| Services | Transient `openshell-lab-*` user units: gateway, policy middleware, package cache and worker |
| Policy and credentials | Example policy; synthetic provider and project-restricted client credentials |

The worker used the reconnectable daemon transport. The real middleware and
package cache remained in the path. No real model or browser was needed for
this worker recovery slice. The previous lab's harness and browser checks are
recorded in [lab 19](babbage-openshell-lab-19-2026-10-12.md).

## Worker restart and offline execution

These eight checks passed in the initial run and again with the final worker.

| Check | Observed result |
| --- | --- |
| Running job across worker restart | The same execution and attempt continued; the attempt marker appeared once. Output before and after the restart was retained, events were contiguous, and exactly one terminal result was recorded |
| Completion while offline | The daemon completed the original job while the worker was stopped; restarting the worker replayed its result |
| Connected output budget | A successful result retained all 600,000 requested bytes within its 1 MiB limit, without truncation |
| Definite refusal | A nonexistent working directory ended the execution `failed`, rather than remaining queued |
| Cancel after reconnect | Cancellation ended the original recovered job `cancelled` |
| Kernel continuity | Python globals survived a worker restart: `17` became `18` in the same kernel |
| Noisy offline completion | The whole log contained exactly 3,000,018 bytes with SHA-256 `56e35d61df938d29c25426f9b2dd1aa5ffee83db7063da6a3256b5d008dfbacd`; the 1,024-byte inline output retained head and tail and reported truncation |
| Offline wall limit | A job with a three-second limit ended `timed_out` while the worker was offline, retaining its preceding output |

The noisy check establishes continued execution and full log retention through
bounded replay. It does not promise that every evicted stream chunk remains
available as an event; the daemon's bounded-history limitations are described
in [the worker](../../../docs/WORKER.md#sandbox-daemon-and-recovery).

## Failure found before merge

The initial daemon-loss probe launched a Python parent and a `setsid sleep`
child, then killed only the unique daemon PID after checking its start identity.
The original job became `lost` and a fresh shell succeeded, but the escaped
child remained alive. The observed parent/child PIDs were 798/800; the
replacement daemon was 830. This check **failed**.

Read-only diagnosis found that OpenShell permitted peer `/proc/stat`, `status`
and `cmdline` reads but denied peer `environ`, `maps` and `exe` reads. The new
daemon's owner-marker sweep therefore could not establish ownership of the old
processes. Local process tests had not exposed this permission boundary.
Replacement readiness was premature.

The correction fences an authenticated new daemon incarnation with a verified
stop/start of the same managed sandbox. It first persists recycle intent tied
to the original sandbox name and immutable ID, refuses new submissions, confirms
that exact VM is `Stopped`, then starts it and waits for `Ready`. The original
endpoint remains fenced until cleanup is proved. Worker/bridge reconnects to
the same daemon do not stop the VM; transport or authentication failures alone
never authorize a recycle. An unmanaged or unidentified legacy binding refuses
automatic replacement until the operator establishes cleanup.

## Corrected daemon and sandbox loss

Before killing the final worker's daemon, a same-incarnation reconnect filled
the legacy endpoint's original sandbox binding. The expanded probe then passed:

| Check | Observed result |
| --- | --- |
| Daemon death with two live jobs | Both original attempts became `lost`; neither command was replayed |
| Admission during cleanup | New work received definite HTTP 503 `unavailable` while fenced. The fixture retried that refusal under the same operation ID, with a 60-second bound; 33 refusals were recorded before readiness |
| Whole VM fence | The sandbox name `ss-edcf0ecf1858a969` and immutable ID `15800d5e-e582-4244-9cff-2e426efc472b` were unchanged, while its boot ID changed |
| Escaped child | The old child's boot/start identity no longer existed. Its numeric PID was reused in the new VM boot, which the identity check distinguished from a survivor |
| Writable disk | The workspace marker and an importable module written into the Python prefix both survived stop/start |
| Kernel loss | The former global was absent with `NameError`; the result reported `state_lost` and an advanced kernel generation |
| Confirmed sandbox deletion | The removed VM's live attempt became `lost`. Worker startup continued; `lab20-b` retained its sandbox identity and Python kernel state |
| Idle endpoint without unfinished rows | Killing its daemon still triggered the fence and a changed boot ID. The same sandbox identity resumed; the former kernel global was absent and `state_lost` was reported |

The first expanded run stopped early because the fixture assumed immediate
replacement readiness. The worker correctly returned the new admission fence;
its journal showed the same VM ready again about 6.5 seconds later. The fixture
now retries only a definite fenced 503, never a transport error, uncertain
acceptance, other API refusal or terminal failure. The complete revised loss,
sandbox-loss and idle-endpoint sequence then passed. The final evidence combines
the final eight core checks with that successful revised sequence.

## Integrated validation and limits

Final integrated validation passed: 9,051 Python tests (43 integration/environment
skips), 414 web tests, Ruff format/lint (337 files), strict Pyright, Svelte and
TypeScript checks, production build, generated-proto consistency and Python/npm
license checks. Independent adversarial review cleared the daemon,
binding-pinned lifecycle source, final fixture and report. Remote CI remains
the merge gate.

Local tests cover authenticated/fenced controllers, admission bursts, retained
refusals, cancellation deadlines, replay rollover, atomic source cursors, corrupt
recovery rows, unknown installers, lifecycle failures, concurrent opens and
crash recovery. Those injected checks are distinct from the real VM observations
above. Daemon memory and kernel globals do not survive daemon or VM death; old
ended records may age out of the bounded journal. A remembered live daemon that
cannot be reconciled delays pool startup. This run does not validate relocation
to another host, Kubernetes placement or real model behavior.

## Cleanup

The gateway reported zero sandboxes. All Lab20 transient units were stopped and
1.1 GiB of disposable lab state was removed. No Lab20 processes, driver socket
directories or lab listener ports remained; no OpenShell paths remained under
the lab's XDG locations. A separate document-comments browser fixture was still
running and was not part of this lab. Shared Docker images and persistent user
configuration were preserved.
