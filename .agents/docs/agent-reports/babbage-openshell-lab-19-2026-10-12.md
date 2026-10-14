# OpenShell lab 19 on babbage: background jobs

Run on 2026-10-12. The worker/API checks for D83 passed in real OpenShell VMs.
The recorded evidence (archived outside git, sha256 `a450d8466017`) contains
the API records, numbered events, output samples and logs, without client tokens.

## What ran where

| Component | Configuration |
| --- | --- |
| Host | babbage, Ubuntu 26.04, kernel `7.0.0-38-generic` |
| Harness and GUI | `600d688`; built production web assets, loopback port 8101 |
| Worker code | `4d563f7` in the isolated background-jobs worktree, based on main `720111d` |
| Gateway | OpenShell 0.1.3, VM driver, mTLS, loopback port 17670 |
| Sandboxes | `sandbox-science/minimal:02f85b3`, 4 vCPUs, 4,096 MiB RAM and 8,192 MiB overlays |
| Services | Transient `openshell-lab-*` user units: gateway, policy middleware, package cache and worker |
| Policy | The example policy, synthetic GitHub provider credentials, client restricted to project `lab19` |
| Idle setting | `suspend_after = 3` in the disposable worker's settings, to exercise suspension in a short check |

The [check script](../../labs/jobs.py) uses the worker API. Shell commands and jobs
ran in the VMs; their results reported the sandbox's hostname, separate kernel,
hidden host sentinel, enforced seccomp, no capabilities and observed isolation tier 3.
The host inspected records and compared logs. No research command ran on the host.

## Worker checks

| Check | Observed result |
| --- | --- |
| Fast submission | A warmed environment accepted a five-second job in 0.014 s. Repeating the same operation returned the same execution |
| Foreground concurrency | A foreground shell call finished while the job was still running. The job then succeeded with `done` |
| Event reconnection | Closed the original stream after its first event; resumed after that sequence. The combined events had every sequence exactly once through the terminal result |
| Live output and usage | While the job was still running, status showed `head`, 0.02 CPU seconds and 9,756,672 memory bytes in the sampled process tree |
| Whole logs | A 16,011-byte stdout log matched its expected bytes. The 1,024-byte inline budget kept head and tail with truncation reported |
| Timeout | A one-second limit ended `timed_out`, with the preceding output preserved |
| Concurrent quota | Four simultaneous submissions were accepted; the fifth received HTTP 403 `policy_denied`, naming the active executions. The session listing contained exactly the four accepted jobs; cancelling them ended each `cancelled` |
| Session close | Cancelled a running job and its child, even though the child started a separate OS session. A later sandbox check found the child stopped |
| Delete while queued | Deleting an environment immediately after accepting its job ended the job `cancelled`. Recreating the same environment ID allowed a new job to succeed |
| Busy versus idle | A twelve-second job kept the environment `ready` beyond its three-second idle period. After it ended, the environment became `stopped`; the next shell resumed it |
| Worker restart before the daemon | Observed a running job's output, restarted the worker, then reconciled the same ID as `lost`. Its whole log still contained the output from before the restart |

These checks passed twice, with the final pass including the direct-cancellation
deadline fix. A watcher cannot infer a job's success from its retained log: `lost`
still means the attempt's outcome is unknown.

## Executor daemon prerequisite

A separate one-shot command was detached inside the VM with its standard file
descriptors redirected. The OpenShell CLI returned in 0.042 s; another connection
three seconds later read the command's completion marker. Both temporary files
were removed. This establishes that the daemon can outlive that CLI connection;
it does not establish event replay, worker restart survival or kernel recovery.
Those remain the next implementation slice.

## Harness and GUI

The [scripted harness check](../../labs/job_harness.py) ran against the same
OpenShell worker, using a separate project-record environment and disposable
state. It started a three-minute job and called `wait_jobs`. Shortened ten-second
check intervals exercised two scripted watcher replies, `fine` then
`needs_attention`, with recorded model alias and token usage. The main model
made three calls in all: start, wait and the answer after attention; it made no
call during the wait. This validates the model-role plumbing with scripted
replies, not the judgment of a real low-cost model.

The real browser displayed the running job, elapsed time, CPU and memory,
expanded output, the wait and its progress note. The next check-in changed to
needs-attention and the wait disappeared. Clicking **Cancel job** changed the
state through `cancelling` to `cancelled`. The saved GUI view (archived outside git, sha256 `982e2a6f1ae1`) shows that terminal state and the attention note. A fresh harness process then
received exactly one completion notice, saved its transcript and acknowledged
the notice. A subsequent run received none. The evidence JSON includes these
records. GUI chat and a live CLI inbox are not implemented; the injected-message
boundary is covered by automated tests and independent race probes.

Integrated validation passed: 8,966 Python tests (43 integration/environment
skips), 408 web tests, Ruff format/lint, strict Pyright, Svelte checking, production
build, generated-proto consistency and Python/npm license checks. Independent
adversarial review left no unresolved medium or high findings, including
log-write failure, cancellation without acknowledgments, queued lifecycle races,
notice-delivery failures, pagination, wait/message races and watcher-budget races.

## CI follow-up: package cleanup

The first PR run passed web and Python 3.13, but Python 3.12 reproduced an
existing killed-wrapper cleanup race. `/proc/environ` can lose a process's turn
marker before that process has stopped. Cleanup now follows observed PID/start
identities through exit, treats uncertain reads or denied signals as pending,
and retains the holder when bounded cleanup fails. Python package-client startup
fails closed even if its diagnostic cannot be written. A subsequent locked
observer retries the retained marker, including a live former wrapper that has
released its lock. Thirteen new regressions and three existing real-process
checks pass on both Python versions. Independent review cleared the final fix.
The updated full local suite passed 8,979 tests (43 skips); 408 web tests and all
static, build, generated-proto and license checks passed again. These follow-up
checks are local process tests; the OpenShell results above retain their original
code versions and are not presented as a repeat lab run.

A second CI run passed the process cleanup checks and reproduced the already
recorded lockfile responsiveness flake on Python 3.12 (0.293 s against 0.25 s).
The original hostile inputs and bound now run in a fresh interpreter, excluding
prior tests' heap history. Six deterministic cases check canonical JSON byte
compatibility, exact framing-inclusive limits, early refusal of many entries and
refusal of single oversized platform/subdir strings before serialization. The
worker serializes entries incrementally and retains the final cache-address
check. Profiling measured only 17 ms in the old whole-model serializer, so this
change does not establish that serializer as the cause of the timing failure.
Final local validation passed 8,985 Python tests (43 skips), 408 web tests and all
static/build/license checks; the nine focused lockfile checks passed on Python
3.12 and 3.13. Independent review cleared the final change.

## Cleanup

All sandboxes were deleted (the gateway reported zero). All transient lab units,
including the workspace server, were stopped; 1.1 GiB of disposable state was
removed. No driver socket directories, lab processes or OpenShell state paths
remained. Ports 8090, 8080, 8100, 8101, 50051 and 17670 were free. Shared Docker
images and persistent user configuration were preserved.
