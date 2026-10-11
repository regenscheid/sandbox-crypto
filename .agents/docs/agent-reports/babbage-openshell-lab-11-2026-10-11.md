# OpenShell v0.1.3 lab 11 on babbage, 2026-10-11: idle suspension

This lab tried the worker's idle suspension (W3) on a real gateway, then tore everything down. The worker stops the sandbox of an environment nobody uses, and starts it again on the next use. Like the earlier labs, this report records observed behaviour.

## What ran where

| | |
| --- | --- |
| Host | babbage: AMD Ryzen AI Max+ 395, Ubuntu 26.04.1 LTS, kernel 7.0.0-38-generic |
| Runtime | OpenShell 0.1.3, VM driver (KVM); the sandbox had 4 vCPUs, 4 GiB of memory and an 8 GiB overlay disk |
| Sandbox | `ss-env-sus-bb08f4b7`, which the worker created from `sandbox-science/minimal:02f85b3` in 15.1 s |
| Policy | Lab 10's: the example policy without its provider, plus `deny` rules for `www.wikipedia.org` |
| Worker | `suspend_after = 60`, so it checked every 15 s; admission on demand on |

## The run

Before suspension, in one session:
- a cell set `x = 42`;
- a shell call wrote `keep.txt` and installed `six` 1.17.0 with pip through the package cache, into `/opt/sandbox-science`;
- a fetch got `www.iana.org` admitted on demand (16.2 s).

| Check | Result |
| --- | --- |
| Suspension | 66 s after the last use. OpenShell reported the sandbox `Stopped`, and none of its VM processes were left on the host |
| First call after suspension | 7.6 s, of which the sandbox's start took 6.7 s. `keep.txt` and `six` 1.17.0 were still there |
| Kernel | The next cell got `NameError` for `x`, with `state_lost` and kernel generation 2 |
| Policy | `www.iana.org`, admitted before the suspension, answered at once (HTTP 200, 0.1 s) |
| A busy kernel | A cell started a thread that computed for 150 s and returned. The environment stayed up throughout. It was suspended 66 s after the thread ended |
| Deletion | A suspended environment was deleted normally |

The sandbox kept its ID and its middleware binding across the stop and start.

## Findings

1. **OpenShell's stop keeps what an environment needs.** The VM driver keeps the sandbox's writable overlay disk, so the workspaces, the shared area and tools installed into the image's prefix survive. Its identity and policy survive too. This is the "retain" option of the confirmed idle behaviour ([projects and sessions](../../../docs/PROJECTS-AND-SESSIONS.md)).
2. **Resuming costs about 7 s on the first call.** Later calls are as fast as before.
3. **CPU sampling protects running work.** Measuring the CPU time below the executor caught a computation in a kernel thread, which a check of running calls alone would have missed.

## Cleanup

- **Removed:**
  - the sandbox;
  - the units: worker, gateway, middleware and cache;
  - the lab directory (1.1 GB).
- **Checked afterwards:**
  - no lab processes;
  - the lab ports free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
- **Kept:** the two images in the local Docker store.
