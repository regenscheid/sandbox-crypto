# OpenShell v0.1.3 lab 4 on babbage, 2026-10-11

Runtime validation of the W1 first slice (PR #17) on a real gateway, followed by a full teardown. The harness drove the [worker service](../../../docs/WORKER.md), which ran its executor in an OpenShell VM sandbox. Like the earlier labs, this report records observed behaviour.

## Setup

- **Gateway, policy middleware, package cache.** As in labs 2 and 3, on loopback in transient systemd user units, brought up and torn down by scripts. Sandboxes had 4 vCPUs, 4 GiB of memory and an 8 GiB overlay disk.
- **Research sandbox.** `docker.io/library/buildpack-deps:noble-scm` with the compiled example policy. It also needed the policy's `github-readonly` provider attached, with a synthetic token, because OpenShell refuses a policy that binds a provider not attached to the sandbox. A Conda environment from conda-forge through the cache supplied Python 3.13.16, ipykernel 7.4.0 and jupyter_client 8.10.0, in 16 s.
- **Worker service.** It ran on the host with the OpenShell backend (`sandbox-science-worker serve`), mapping environment `env-1` to the sandbox, with tier 3 declared.
- **Harness.** `sandbox-science-harness run` with a scripted model. It required tier 3, so `--allow-unisolated` was not given.

## Results

| Check | Result |
| --- | --- |
| Executor start | `openshell sandbox exec` started the executor in about 0.5 s, and it answered from host `research-1` (the VM), not babbage |
| Shell | `hostname`, `uname -r` and `id -u` gave `research-1`, the guest kernel 6.12.76 and uid 1000. A sentinel file on the host was not visible in the sandbox |
| Stateful cells | After the scheduler shim (finding 1): `x = 41`, then `x + 1` gave 42, in generation 1 |
| Publication | `out.csv` was published with the right SHA-256 and read back. After the fix in finding 3, `produced_by` named the shell call that wrote it |
| `finite-field-identity-v1` | The roadmap's fixture, computed in plain Python in the kernel, gave the expected canonical JSON |
| Interrupt | A sleeping cell ended `cancelled` in 0.55 s, and the next cell still saw its variable |
| Cancel | After the fix in finding 2: a shell call that left a background process ended `cancelled`, and the background process was gone |
| Streaming | A cell printing every 0.5 s produced events at 0.01, 0.51, 1.01, 1.51 and 2.01 s, so `openshell sandbox exec` passes output through as it comes |
| Restart and kernel death | Both reported `state_lost`, with the generation still growing |
| Output bounds | 5 MiB of output kept its head and tail and reported 5,242,880 bytes |
| Confinement | The file tools refused a symlink and a `..` path |
| Network from a kernel | The package cache answered 200 and `pypi.org` was refused. In the VM, 127.0.0.1 is the VM's own loopback, so the worker API on the host was out of reach |
| Worker restart | A running cell became `lost` ("the worker restarted during this attempt"). The next cell reported `state_lost` and a higher generation |
| Latency | Medians over 10 calls through the HTTP API: 14 ms for a shell call, 20 ms for a cell |

## Findings

1. **No Jupyter kernel could start: libzmq aborts under OpenShell's seccomp filter.**
   - libzmq calls `pthread_setschedparam` on each thread it starts and aborts if that fails. glibc names the thread by its ID, so the call becomes `sched_setscheduler(tid, …)`, which OpenShell's filter refuses with EPERM. The same call with pid 0, meaning the calling thread, is allowed.
   - Every cell's executor died with `Operation not permitted (src/thread.cpp:315)`. The worker reported each attempt `lost`, as designed.
   - **Fix:** the worker builds a shim (`shim/sched_self.c`) with the host's compiler, uploads it beside the executor and preloads it. For the calling thread, the shim makes the allowed pid-0 call. It needs only glibc 2.2.5 symbols.
   - Upstream descriptions are in `.agents/upstream/`.
2. **Process groups cannot be signalled.** The filter also refuses `kill(-pgid, …)`, while `kill(pid, …)` works. Cancellation and timeouts of shell calls never ended, and the worker's watchdog gave the attempt up as `lost` after 3.5 minutes. Kernel interrupts worked only because jupyter_client falls back to signalling the kernel process alone. **Fix:** when `killpg` is refused, the executor signals each member of the group, found through `/proc`.
3. **Modification times cannot attribute files.** The first attribution compared a file's modification time with each attempt's start and end on the sandbox clock. The guest kernel stamps files with a coarse clock (ticks of a few milliseconds), and a cell and the next shell call ran within one tick, so publication named the wrong attempt. **Fix:** the executor lists the files in the session workspace and the shared area before and after each attempt. A result now carries the files the attempt wrote (`written_files`), and publication names the last finished attempt that wrote the file. These lists are also the per-attempt provenance the artifact spec asks for.
4. **Smaller details.**
   - The workload cannot read `/dev/zero`, as lab 1 found; test commands use `yes | head -c`.
   - The shared area is now created when the executor starts, so commands can write to `$PROJECT_SHARED` before any file tool has touched it.
   - A sandbox compiled from a policy with providers needs those providers attached, which the orchestrator must do (backlog).

## Cleanup

- **Removed:**
  - the sandbox and the provider;
  - the units: worker, gateway, middleware and cache;
  - the lab directory (844 MB).
- **Checked afterwards:**
  - no OpenShell, middleware, cache, worker or executor processes;
  - the five lab ports free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
- **Kept:** the journal entries of the transient units, and the OpenShell binaries.
