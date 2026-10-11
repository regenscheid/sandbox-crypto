# OpenShell v0.1.3 lab 8 on babbage, 2026-10-11

Runtime validation of the W1 hardening (PR #30) and the package-client configuration (PR #26), with the backend conformance suite run against a real OpenShell sandbox. The lab was torn down afterwards. Like the earlier labs, this report records observed behaviour.

## Setup

- **Services.** As in labs 2 to 7: gateway (VM driver), policy middleware and package cache on loopback in transient systemd user units.
- **Worker.** It ran from PR #30's branch with tier 3 declared, `package_cache = "http://host.openshell.internal:8090"` and an `[openshell.environments]` table. A marker variable in its own environment showed whether that environment leaks into sandboxes.
- **Sandbox.** The worker created `ss-env-c-642482ed` from `docker.io/library/python:3.13-slim` under the example policy. Its kernel environment (`/sandbox/kenv`, ipykernel 7.4.0) came from the package cache.

## Results

| Check | Result |
| --- | --- |
| Executor startup | The executor started under OpenShell's seccomp filter. Both `prctl` calls (subreaper, not dumpable) succeeded |
| Kernels, before the fix | Every cell failed: `cannot start a python kernel: [Errno 13] Permission denied` (finding 1). Ten of the 22 conformance tests failed |
| Conformance suite, after the fix | All 22 passed against the sandbox (`SS_CONFORMANCE_*` variables), in 23 s |
| Probe | Tier 3 declared and in effect: kernel separate, sentinel hidden, no findings |
| Leftover processes | A shell call started sleepers through `setsid`, `set -m` and `env -i sh -c '… &'`. All were gone when the call ended |
| Worker environment | The marker variable from the worker's environment did not reach the sandbox: `openshell sandbox exec` does not pass the caller's environment through |
| Killing the CLI | The host's `openshell sandbox exec` process for the executor was killed with SIGKILL. The executor and its ten kernels in the sandbox ended with it, and the next cell started a new executor |
| `/proc/<executor>` | Readable by research code (`fd`, `environ`), since the executor stays dumpable under OpenShell (finding 1) |
| Package clients | The executor wrote `pip.conf` and the other client files. `pip install six` in a new virtual environment, with no index options, came from the cache |

## Findings

1. **A process that is not dumpable cannot use sockets in an OpenShell sandbox.**
   - Right after `prctl(PR_SET_DUMPABLE, 0)`, `bind()` failed with EACCES for a loopback TCP socket and for a Unix-domain socket in `/tmp`. The same calls work in a dumpable process.
   - OpenShell's supervisor evidently cannot inspect a process whose `/proc` files are closed to it, and refuses its socket calls.
   - The executor is the client of its kernels, and jupyter_client binds sockets while choosing ports, so no kernel could start. ZeroMQ's IPC transport does not help, since Unix sockets are refused too.
   - **Fix (in PR #30):** the executor makes itself not dumpable only if it can still bind a loopback socket afterwards; otherwise it becomes dumpable again. Its `hello` reports `not_dumpable`, and the worker logs it.
   - Under OpenShell v0.1.3 the executor therefore stays dumpable, and research code in the same environment can reach its file descriptors. The worker treats what the executor sends as untrusted, and sessions sharing an environment are not isolated from each other anyway.
2. **Per-session kernels accumulate.** The executor held 257 descriptors after the conformance run: ten live kernels, one per session the suite used. Kernels end only when restarted, when they die or when their executor ends, so a long-lived environment with many sessions keeps them all. This needs a way to close a session's kernels, or to stop idle ones (backlog).
3. **Earlier questions settled.**
   - `prctl(PR_SET_CHILD_SUBREAPER)` and `prctl(PR_SET_DUMPABLE, 0)` are allowed by OpenShell's filter.
   - `openshell sandbox exec` passes no environment from its caller.
   - Ending the CLI ends the executor and its kernels inside the sandbox.

## Cleanup

- **Removed:**
  - the sandbox and the provider;
  - the units: worker, gateway, middleware and cache;
  - the lab directory (529 MB).
- **Checked afterwards:**
  - no lab processes;
  - the lab ports free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
