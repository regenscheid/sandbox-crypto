# OpenShell v0.1.3 lab 7 on babbage, 2026-10-11

The W1 boundary end to end: the harness ran in its own OpenShell sandbox (container A), under the compiled orchestrator policy. It drove the worker service, which ran every tool in a research sandbox it had created itself (container B). The lab was torn down afterwards. Like the earlier labs, this report records observed behaviour.

## Setup

- **Services.** As in labs 2 to 6: gateway (VM driver), policy middleware and package cache on loopback in transient systemd user units. The worker ran on the host with an `[openshell.environments]` table and tier 3 declared.
- **Container B.** The worker created `ss-env-b-e69a8496` from `docker.io/library/python:3.13-slim` under the example policy, with the `github-readonly` provider (synthetic token).
  - It was ready in 11.3 s.
  - A kernel environment (`ipykernel` 7.4.0, `jupyter_client` 8.10.0) was installed into `/sandbox/kenv` from the package cache, standing in for the base image's (D46).
- **Container A.** The sandbox `orch-a` came from the same image under `compile_orchestrator()`'s policy. That policy admits only the worker API (`host.openshell.internal:8100`), and only for the harness's interpreter.
  - The harness was installed offline from a 5.7 MB wheelhouse: the project's two wheels plus aiohttp, pydantic and their dependencies for CPython 3.13.
  - The worker's bearer token was uploaded as a file.

## Results

| Check | Result |
| --- | --- |
| Egress from A | The worker API answered 200 with the token and 401 without it. The package cache and `pypi.org` were refused |
| Harness in A | `sandbox-science-harness run` with a scripted model ran a shell call and Python cells through the worker API, from A |
| Location | Every tool ran in B: hostname `ss-env-b-e69a8496`, uid 1000, B's own boot ID, isolation tier 3. A host path (`~`) did not exist there |
| State | `x = 41`, then `print(x + 1)` printed 42, in kernel generation 1 |
| Worker API from B | Refused: the research policy does not admit it |
| Probe | Tier 3 declared and in effect, with no findings: kernel separate, the worker's sentinel file hidden. In B the executor ran with a seccomp filter, no-new-privs and no capabilities; the CPU reported a hypervisor, and `/sys` was out of reach, so the DMI vendor and product were unknown |
| Reconnect | The harness was killed (SIGKILL) 4 s into a cell that appended to a file and printed for 8 s. Two reruns with the same `--run-id` each reattached to the same execution and returned its full output. The file held one line, and the worker's store had one execution per operation |

## Findings

- **The orchestrator needs only the worker API, and OpenShell enforces that.** The compiled orchestrator policy is enough to keep a compromised harness from reaching the package cache or the internet. That is the threat model's rule for Z2: egress only to the model gateway and MCP endpoints under policy, plus the worker API. The model gateway was not part of this lab, since the scripted model needs none.
- **No package index in the orchestrator's sandbox.** A wheelhouse built on the host installs the harness offline. An orchestrator image (D46 style) would do the same at build time.
- **Event streams pass OpenShell's REST inspection.** NDJSON responses streamed through the inspected `rest` endpoint, including the 8 s cell and the reattachment.

## Cleanup

- **Removed:**
  - both sandboxes and the provider;
  - the units: worker, gateway, middleware and cache;
  - the lab directory (523 MB).
- **Checked afterwards:**
  - no lab processes;
  - the lab ports free;
  - no OpenShell paths under `~/.config`, `~/.local/state` or `~/.local/share`;
  - no driver socket directories in `/tmp`.
